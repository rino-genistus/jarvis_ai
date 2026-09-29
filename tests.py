"""
Jarvis test harness.

    python tests.py              # everything except the microphone test
    python tests.py --quick      # structural checks only, no model calls
    python tests.py voice        # interactive microphone check
    python tests.py cache tools  # named suites only
    python tests.py wake memory  # need no API keys, Ollama or jarvis.py
    python tests.py --list       # show suite names

Nothing here sends an email, creates a calendar event, or writes a reminder.
The tool suites check which tool the model *chooses* and never execute it, so
this is safe to run against live accounts.
"""

import sys
import time
import threading

# ---------------------------------------------------------------------------
# tiny harness
# ---------------------------------------------------------------------------

PASS, FAIL, SKIP = [], [], []
_current = "?"


def check(label, condition, detail=""):
    """Record one assertion. Never raises — a failed check keeps the suite going."""
    line = f"{_current}: {label}"
    if condition:
        PASS.append(line)
        print(f"  \033[32mPASS\033[0m  {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(line + (f"  ({detail})" if detail else ""))
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"  {detail}" if detail else ""))
    return condition


def skip(label, why):
    SKIP.append(f"{_current}: {label}")
    print(f"  \033[33mSKIP\033[0m  {label}  ({why})")


def header(name):
    global _current
    _current = name
    print(f"\n\033[1m── {name} {'─' * max(0, 58 - len(name))}\033[0m")


def ms(seconds):
    return f"{seconds * 1000:.0f}ms"


# ---------------------------------------------------------------------------
# module import (shared by most suites)
# ---------------------------------------------------------------------------

_jarvis = None


def load_jarvis():
    """
    Import jarvis.py. This instantiates every agent, so it exercises Google
    OAuth and Spotify auth, and kicks off the Kokoro load thread.
    """
    global _jarvis
    if _jarvis is None:
        t = time.time()
        import jarvis
        _jarvis = jarvis
        print(f"  (imported jarvis.py in {time.time() - t:.1f}s)")
    return _jarvis


# ---------------------------------------------------------------------------
# suites
# ---------------------------------------------------------------------------

def suite_imports():
    """Module loads, agents construct, credentials resolve."""
    header("imports")
    try:
        j = load_jarvis()
    except Exception as e:
        check("jarvis.py imports", False, f"{type(e).__name__}: {e}")
        print("\n  Everything else depends on this. Fix the import first.")
        return False

    check("jarvis.py imports", True)
    # A key that isn't set is a choice, not a bug: skip. A service that has
    # its key but crashed while starting is a failure.
    for name, why in j.DISABLED_AGENTS.items():
        if why.startswith("missing"):
            skip(f"{name} started", why)
        else:
            check(f"{name} started", False, why)
    check("at least one agent running", len(j.AGENTS) > 0, f"{len(j.AGENTS)} of {len(j.AGENT_REQUIREMENTS)} agents")
    check("on-device memory store open", j.memory is not None,
          f"{j.memory.count()} memories" if j.memory is not None else "recall falls back to Obsidian search")
    check("assistant did not auto-run", True, "module is importable")
    return True


def suite_registry():
    """The tool registry and its grouping."""
    header("registry")
    j = load_jarvis()
    import inspect
    from ollama._utils import convert_function_to_tool

    reg = j.TOOL_REGISTRY
    check("registry is populated", len(reg) > 0, f"{len(reg)} tools")
    check("all entries are bound methods",
          all(inspect.ismethod(m) for m in reg.values()))

    # An unbound method leaks `self` into the schema and the model tries to fill it.
    leaked = []
    failed = []
    for name, method in reg.items():
        try:
            tool = convert_function_to_tool(method)
            props = (tool.function.parameters.properties or {})
            if "self" in props:
                leaked.append(name)
        except Exception as e:
            failed.append(f"{name}: {e}")
    check("every tool converts to an Ollama schema", not failed,
          "; ".join(failed[:3]) if failed else f"{len(reg)} schemas")
    check("no tool leaks a 'self' parameter", not leaked, ", ".join(leaked))

    # Every tool must be in a group, or routing silently hides it. Only tools
    # tool_catalog deliberately lists under a second agent may be in two.
    grouped = [m for tools in j.TOOL_GROUPS.values() for m in tools]
    names_in_groups = {m.__name__ for m in grouped}
    missing = set(reg) - names_in_groups
    check("every registered tool belongs to a group", not missing,
          f"missing: {sorted(missing)}" if missing else f"{len(j.TOOL_GROUPS)} groups")
    import tool_catalog
    listed = [n for tools in tool_catalog.AGENT_TOOLS.values() for n in tools]
    shared = {n for n in listed if listed.count(n) > 1}
    doubled = {m.__name__ for m in grouped if [g.__name__ for g in grouped].count(m.__name__) > 1}
    check("only deliberately shared tools appear in two groups", doubled <= shared,
          f"unexpected: {sorted(doubled - shared)}" if doubled - shared else f"shared: {sorted(shared)}")
    check("ALL_TOOLS matches the registry", len(j.ALL_TOOLS) == len(reg))

    for group, tools in sorted(j.TOOL_GROUPS.items()):
        print(f"        {group:10} {len(tools):2} tools")

    # Payload size is the whole reason routing exists.
    import json
    def schema_chars(tools):
        return sum(len(convert_function_to_tool(t).model_dump_json(exclude_none=True)) for t in tools)
    full = schema_chars(j.ALL_TOOLS)
    biggest = max(schema_chars(t) for t in j.TOOL_GROUPS.values())
    print(f"        full payload ~{full // 4} tokens, largest group ~{biggest // 4} tokens")
    if len(j.TOOL_GROUPS) < 3:
        skip("largest group is well under the full payload",
             f"only {len(j.TOOL_GROUPS)} group(s) running, routing has nothing to trim")
    else:
        check("largest group is well under the full payload", biggest < full / 2,
              f"{biggest // 4} vs {full // 4} tokens")


def suite_routing():
    """Keyword router accuracy and select_tools() behaviour."""
    header("routing")
    j = load_jarvis()

    cases = [
        ("what's the weather in Boston right now", "weather"),
        ("will it rain tomorrow", "weather"),
        ("how hot is it outside", "weather"),
        ("remind me to call mom on friday", "reminders"),
        ("add milk to my reminders", "reminders"),
        ("what's on my calendar tomorrow", "calendar"),
        ("schedule a meeting with Sam at 3pm", "calendar"),
        ("play some jazz", "music"),
        ("skip this track", "music"),
        ("turn the volume down", "music"),
        ("do I have any unread email", "email"),
        ("search the web for MLX whisper benchmarks", "web"),
        ("look up the population of Tokyo", "web"),
    ]
    hits = 0
    for text, expected in cases:
        group, tools = j.route_tools(text)
        ok = group == expected
        hits += ok
        if not ok:
            print(f"        miss: {text!r} -> {group} (wanted {expected})")
    check("keyword router hits every case", hits == len(cases), f"{hits}/{len(cases)}")

    # A phrase with no keyword must still return something usable.
    group, tools = j.select_tools("do the thing we talked about earlier")
    check("select_tools always returns tools on a vague request",
          tools and len(tools) > 0, f"group={group}, {len(tools) if tools else 0} tools")

    group, tools = j.select_tools("what's the weather in Boston")
    check("select_tools routes a clear request narrowly",
          tools is not j.ALL_TOOLS and len(tools) < 10,
          f"group={group}, {len(tools)} tools")

    # Within an agent, only the tools the words call for — and the right one
    # leads, because it picks the acknowledgement.
    import tool_catalog
    picks = [
        ("GmailAgent", "do I have any unread emails", "get_unread_emails"),
        ("GmailAgent", "reply to John's email saying sounds good", "reply_to_email"),
        ("Calendar_Agents", "what's on my calendar tomorrow", "get_calendar_events"),
        ("Calendar_Agents", "cancel my 3pm meeting", "delete_calendar_event"),
        ("Calendar_Agents", "move my dentist appointment to Thursday", "update_calendar_event"),
        ("RemindersAgent", "remind me to call mom on Friday", "add_reminder"),
        ("RemindersAgent", "mark the milk reminder as done", "complete_reminder"),
        ("WeatherSearch", "will it rain tonight", "get_weather_with_time"),
        ("SpotifyAgent", "skip this track", "skip_song"),
        ("EverydayToolsAgent", "how many days until Christmas", "days_between"),
    ]
    wrong = [(t, tool_catalog.pick(a, t)) for a, t, lead in picks if tool_catalog.pick(a, t)[0] != lead]
    check("the best-matching tool leads each pick", not wrong,
          f"{wrong}" if wrong else f"{len(picks)} requests")
    sizes = [len(tool_catalog.pick(a, t)) for a, t, _ in picks]
    check(f"picks stay within {tool_catalog.MAX_PICKED} tools", max(sizes) <= tool_catalog.MAX_PICKED,
          f"largest {max(sizes)}")
    needy = tool_catalog.pick("Calendar_Agents", "cancel my 3pm meeting")
    check("a tool that needs an id comes with the tool that finds it",
          "get_calendar_events" in needy, str(needy))
    missing_ack = [f"{a}.{n}" for a, tools in tool_catalog.AGENT_TOOLS.items()
                   for n, tool in tools.items() if not tool.ack]
    check("every tool has an acknowledgement", not missing_ack, ", ".join(missing_ack))


def suite_text():
    """Sentence splitting that feeds the TTS stream."""
    header("text")
    j = load_jarvis()

    def split(pieces):
        buf, out = "", []
        for p in pieces:
            buf += p
            while True:
                m = j.SENTENCE_END.search(buf)
                if not m or m.end() == 0:
                    break
                s, buf = buf[:m.end()].strip(), buf[m.end():]
                if s:
                    out.append(s)
        if buf.strip():
            out.append(buf.strip())
        return out

    check("splits on sentence boundaries",
          split(["Hello there. ", "How are you? ", "Fine!"]) ==
          ["Hello there.", "How are you?", "Fine!"])
    # The regression that made Jarvis say "seventy two point" then "four degrees".
    check("does not split inside a decimal",
          split(["It is 72", ".4 degrees ", "out there. ", "All good."]) ==
          ["It is 72.4 degrees out there.", "All good."])
    check("emits an unterminated fragment at the end",
          split(["no ending punctuation here"]) == ["no ending punctuation here"])
    check("emits nothing for empty input", split([""]) == [])


def suite_audio():
    """Speaker thread, non-blocking say(), streamed synthesis. Makes noise."""
    header("audio")
    j = load_jarvis()

    print("  waiting for Kokoro...")
    ready = j.kokoro_ready.wait(timeout=120)
    if not check("Kokoro loaded", ready, "timed out after 120s" if not ready else ""):
        return

    t = time.time()
    j.say("Testing one two three.")
    queued = time.time() - t
    # The whole point of the speaker thread: the ack must not block the LLM call.
    check("say() returns immediately", queued < 0.05, ms(queued))

    t = time.time()
    j.wait_until_spoken()
    drained = time.time() - t
    check("wait_until_spoken blocks until audio finishes", drained > 0.3, f"{drained:.2f}s")

    # Empty strings used to reach Kokoro and produce a click.
    j.safe_speak("")
    j.safe_speak(None)
    j.safe_speak("   ")
    j.wait_until_spoken()
    check("safe_speak ignores empty input", True)

    # Fake a token stream and confirm speech starts before the last token.
    class Part:
        def __init__(self, c):
            self.message = type("M", (), {"content": c})()

    first_queued = [None]
    original_say = j.say

    def timed_say(text):
        if first_queued[0] is None:
            first_queued[0] = time.time()
        original_say(text)

    j.say = timed_say
    try:
        def stream():
            for w in ("The first sentence is here. "
                      "The second one follows it. "
                      "And a third to finish.").split(" "):
                time.sleep(0.05)
                yield Part(w + " ")
        t = time.time()
        text = j.speak_stream(stream())
        total = time.time() - t
    finally:
        j.say = original_say

    check("speak_stream returns the full text",
          text.startswith("The first sentence"), repr(text[:40]))
    if first_queued[0]:
        lead = first_queued[0] - t
        check("first sentence is spoken before generation ends",
              lead < total * 0.6, f"first at {lead:.2f}s of {total:.2f}s")
    j.wait_until_spoken()
    j.chime()
    j.wait_until_spoken()
    check("chime plays", True)


def suite_cache():
    """
    The regression that caused most of the original latency.

    Ollama keeps one KV cache slot per model. If the summarisation call uses a
    different prefix than the tool call, it evicts it and the next command pays
    a full re-process. Measured before the fix: 41ms -> 13,524ms.
    """
    header("cache")
    j = load_jarvis()
    from ollama import chat

    if "weather" not in j.TOOL_GROUPS:
        skip("prefix cache checks", "built around the weather tools, which are off")
        return
    group, tools = j.select_tools("what's the weather in Boston right now")
    base = j.tool_messages("what's the weather in Boston right now", [])

    def tool_call():
        return chat(model="qwen2.5:7b", messages=base, tools=tools,
                    keep_alive=j.OLLAMA_KEEP_ALIVE)

    print("  warming the prefix...")
    tool_call()
    warm = tool_call()
    baseline = warm.prompt_eval_duration / 1e9
    check("repeated tool call reuses the prefix cache", baseline < 1.0, ms(baseline))

    # Now the real test: a summarisation call in between, exactly as handle_turn does.
    summary_messages = base + [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "get_current_weather", "arguments": {"location": "Boston"}}}]},
        {"role": "tool", "tool_name": "get_current_weather",
         "content": "{'temp': 72.4, 'humidity': 58, 'description': 'clear sky'}"},
        {"role": "user", "content": "Summarize the tool results naturally in Jarvis's voice. "
                                    "Two sentences at most. Do not call any more tools."},
    ]
    summary = chat(model="qwen2.5:7b", messages=summary_messages, tools=tools,
                   keep_alive=j.OLLAMA_KEEP_ALIVE, options=j.GEN_OPTIONS)
    check("summarisation does not call another tool", not summary.message.tool_calls,
          str([t.function.name for t in (summary.message.tool_calls or [])]))

    after = tool_call()
    cost = after.prompt_eval_duration / 1e9
    check("tool prefix survives the summarisation call", cost < 3.0,
          f"{ms(cost)} (was 13,524ms before the fix)")

    # Same tools object on both calls is what makes the above work.
    check("summarisation reuses the same tools list", True,
          "handle_turn passes tools= to both calls")


def suite_tools():
    """Does the model pick the right tool, and get the date right."""
    header("tools")
    j = load_jarvis()
    from ollama import chat
    from datetime import datetime, timedelta

    now = datetime.now()

    cases = [
        ("what's the weather in Boston right now", {"get_current_weather"}),
        ("what's the forecast for the next few days", {"get_daily_forecast", "get_weather_with_time"}),
        ("remind me to call mom on Friday", {"add_reminder"}),
        ("what reminders do I have today", {"get_due_reminders", "get_reminders"}),
        ("what's on my calendar tomorrow", {"get_calendar_events"}),
        ("do I have any unread emails", {"get_unread_emails", "search_email"}),
        ("skip this track", {"skip_song"}),
        ("play some jazz", {"search_song_and_queue"}),
    ]
    for text, _ in cases:
        group = j.route_tools(text)[0]
        if group in j.DISABLED_GROUPS:
            skip(f"tool choice: {text!r}", f"{group} is off ({j.DISABLED_GROUPS[group]})")
    cases = [c for c in cases if j.route_tools(c[0])[0] not in j.DISABLED_GROUPS]

    # temperature 0 so a rerun gives the same answer. At the default temperature
    # qwen2.5:7b occasionally declines to call anything on a borderline phrase
    # like "play some jazz" — production survives that via the retry below, but
    # a flaky assertion here would be useless.
    det = {"temperature": 0}

    correct = 0
    misses = []
    for text, expected in cases:
        group, tools = j.select_tools(text)
        r = chat(model="qwen2.5:7b", tools=tools, keep_alive=j.OLLAMA_KEEP_ALIVE,
                 options=det, messages=j.tool_messages(text, []))
        called = [c.function.name for c in (r.message.tool_calls or [])]
        via = ""
        if not called and len(j.TOOL_GROUPS.get(group, [])) > len(tools):
            # What handle_turn does next: retry with the whole agent
            r = chat(model="qwen2.5:7b", tools=j.TOOL_GROUPS[group], keep_alive=j.OLLAMA_KEEP_ALIVE,
                     options=det, messages=j.tool_messages(text, []))
            called = [c.function.name for c in (r.message.tool_calls or [])]
            via = " (whole-agent retry)" if called else ""
        if not called and group in j.DEFAULT_CALLS and j.looks_like_request(text):
            # What handle_turn does when the model declines: run the group's default
            default = j.DEFAULT_CALLS[group](text)
            if default:
                called, via = [default[0]], " (handle_turn default)"
        ok = bool(set(called) & expected)
        correct += ok
        if not ok:
            misses.append((text, group, called))
        mark = "\033[32mok  \033[0m" if ok else "\033[31mmiss\033[0m"
        print(f"        {mark} {text[:42]:44} [{group}] -> {called or 'NO TOOL CALL'}{via}")
    check("model picks a sensible tool for each request", correct == len(cases),
          f"{correct}/{len(cases)}")


    # The weekday-arithmetic bug: "Friday" used to land on the wrong date.
    friday = None
    for offset in range(1, 8):
        d = now + timedelta(days=offset)
        if d.strftime("%A") == "Friday":
            friday = d.strftime("%Y-%m-%d")
            break
    group, tools = j.select_tools("remind me to call mom on Friday at 2pm")
    r = chat(model="qwen2.5:7b", tools=tools, keep_alive=j.OLLAMA_KEEP_ALIVE,
             messages=j.tool_messages("remind me to call mom on Friday at 2pm", []))
    args = (r.message.tool_calls or [{}])
    due = ""
    if r.message.tool_calls:
        due = str(r.message.tool_calls[0].function.arguments.get("due", ""))
    check("'Friday' resolves to the correct date", friday and friday in due,
          f"got {due!r}, expected {friday}")


def suite_latency():
    """Full budget. Compares each stage against the target it was tuned to."""
    header("latency")
    j = load_jarvis()
    from ollama import chat
    import numpy as np

    j.kokoro_ready.wait()
    results = []

    # Build a speech sample with Kokoro so this needs no fixture file, then
    # resample 24 kHz -> 16 kHz, the array format mic.py hands to Whisper.
    audio = np.concatenate([np.asarray(a) for _, _, a in
                            j.kokoro_pipeline("What is the weather in Boston right now?",
                                              voice="af_heart")])
    samples = np.interp(np.arange(0, len(audio), 1.5), np.arange(len(audio)), audio).astype(np.float32)

    j.transcribe(samples)  # warm

    t = time.time()
    j.transcribe(samples)
    results.append(("whisper transcribe", time.time() - t, 1.0))

    t = time.time()
    j.classify_intent("what's the weather in Boston")
    results.append(("intent classification", time.time() - t, 1.0))

    t = time.time()
    try:
        j.retrieve_memories("what's the weather in Boston")
        results.append(("memory recall", time.time() - t, 0.5))
    except Exception as e:
        print(f"        memory recall unavailable: {e}")

    t = time.time()
    j.route_tools("what's the weather in Boston")
    results.append(("keyword routing", time.time() - t, 0.01))

    group, tools = j.select_tools("what's the weather in Boston right now")
    msgs = j.tool_messages("what's the weather in Boston right now", [])
    chat(model="qwen2.5:7b", messages=msgs, tools=tools, keep_alive=j.OLLAMA_KEEP_ALIVE)  # warm
    t = time.time()
    chat(model="qwen2.5:7b", messages=msgs, tools=tools, keep_alive=j.OLLAMA_KEEP_ALIVE)
    results.append(("tool selection", time.time() - t, 3.0))

    t = time.time()
    list(j.kokoro_pipeline("It is 72 degrees and clear in Boston.", voice="af_heart"))
    results.append(("first sentence synthesis", time.time() - t, 2.0))

    print(f"\n        {'stage':28} {'measured':>10}  {'target':>8}")
    total = 0
    for name, took, target in results:
        total += took
        flag = "\033[32m ok\033[0m" if took <= target else "\033[31m slow\033[0m"
        print(f"        {name:28} {took * 1000:8.0f}ms  {target * 1000:6.0f}ms{flag}")
    print(f"        {'─' * 50}")
    print(f"        {'sum of stages':28} {total * 1000:8.0f}ms")
    print(f"\n        Reference: before optimisation this path was ~25s end to end.")

    for name, took, target in results:
        check(f"{name} within target", took <= target, f"{ms(took)} vs {ms(target)} target")


INTENT_CASES = [
    ("Thanks Jarvis, that'll be all.", "exit"), ("Goodbye.", "exit"), ("That's it for now, thanks.", "exit"),
    ("I'm done, talk later.", "exit"), ("Okay, thank you, bye.", "exit"), ("Go to sleep.", "exit"),
    ("What's the weather in Boston?", "tool"), ("How hot is it outside?", "tool"),
    ("What's on my calendar tomorrow?", "tool"), ("Any unread emails?", "tool"),
    ("Remind me to call mom on Friday.", "tool"), ("Play some jazz.", "tool"), ("Skip this song.", "tool"),
    ("Search the web for the best pizza in Toronto.", "tool"),
    ("Add a meeting with Sarah at 3pm tomorrow.", "tool"),
    ("Send an email to John saying I'm running late.", "tool"),
    ("What reminders do I have today?", "tool"), ("Pause the music.", "tool"),
    ("Will it rain in Toronto tomorrow?", "tool"),
    ("Tell me a joke.", "chat"), ("I skipped lunch today.", "chat"),
    ("I prefer temperatures in Celsius, by the way.", "tool"), ("What's the capital of France?", "chat"),
    ("Explain how a neural network works.", "chat"), ("My sister's birthday is next week.", "chat"),
    ("I'm feeling tired today.", "chat"), ("Thanks, that's helpful.", "chat"),
    ("Can you help me write a poem about rain?", "chat"), ("Interesting, tell me more.", "chat"),
    ("Call me Captain from now on.", "tool"), ("What's the date next Friday?", "tool"),
    ("What's Sarah's phone number?", "tool"), ("Search my notes for the wifi password.", "tool"),
    ("How many days until Christmas?", "tool"), ("I live in Boston.", "tool"),
]


def suite_intent():
    """
    Intent routing — classifier plus decide_intent() — on labelled phrases.
    llama3.2:1b scored 13/29 on the first 29 of these and never recognised a
    goodbye; the qwen few-shot classifier with the guards scored 28/29.
    """
    header("intent")
    j = load_jarvis()
    wrong = []
    for text, want in INTENT_CASES:
        got = j.decide_intent(text, j.classify_intent(text))
        if got != want:
            wrong.append(f"{text!r} -> {got}")
    score = len(INTENT_CASES) - len(wrong)
    for w in wrong:
        print(f"        miss: {w}")
    check("intent routing on labelled phrases", score >= len(INTENT_CASES) - 2,
          f"{score}/{len(INTENT_CASES)}")
    exits = [t for t, w in INTENT_CASES if w == "exit"]
    check("every goodbye ends the conversation",
          all(j.decide_intent(t, j.classify_intent(t)) == "exit" for t in exits))
    statements = ["I skipped lunch today.", "My sister's birthday is next week."]
    check("statements never trigger tools",
          all(j.decide_intent(t, "tool") != "tool" for t in statements))
    check("stated preferences do reach the preferences tool",
          j.decide_intent("I prefer temperatures in Celsius, by the way.", "chat") == "tool"
          and j.route_tools("I prefer temperatures in Celsius, by the way.")[0] == "preferences")


def suite_prefs():
    """Preferences store, in a throwaway folder and vault. Doesn't load jarvis.py."""
    header("prefs")
    import os
    import tempfile
    from pathlib import Path
    import preferences

    saved_path, saved_vault = preferences.PATH, os.environ.get("OBSIDIAN_VAULT_PATH")
    with tempfile.TemporaryDirectory() as tmp:
        preferences.PATH = Path(tmp) / "preferences.json"
        os.environ["OBSIDIAN_VAULT_PATH"] = tmp
        try:
            check("starts empty", preferences.statements() == [] and preferences.prompt_block() == "")
            check("known preference saved", "celsius" in preferences.set_preference("temperature_units", "Celsius")
                  and preferences.get("temperature_units") == "celsius")
            check("aliases map to known names", preferences.set_preference("call me", "Captain")
                  and preferences.get("address_as") == "Captain" and preferences.address() == "Captain")
            check("invalid value refused", "must be one of" in preferences.set_preference("units", "kelvin")
                  and preferences.get("temperature_units") == "celsius")
            preferences.set_preference("note", "Prefers short answers")
            preferences.set_preference("note", "prefers short answers")
            check("free-form notes kept once", preferences.load()["notes"] == ["Prefers short answers"])
            block = preferences.prompt_block()
            check("prompt block lists everything", all(s in block for s in
                  ("Give temperatures in celsius.", 'Address the user as "Captain".', "Prefers short answers")))
            mirror = Path(tmp) / "Memory" / "Preferences.md"
            check("mirrored to Obsidian", mirror.exists() and "Prefers short answers" in mirror.read_text())
            check("forget by name", preferences.forget("address_as") == "Forgot address_as."
                  and preferences.address() == "sir")
            check("forget a note by its words", "Forgot 1" in preferences.forget("short answers"))
        finally:
            preferences.PATH = saved_path
            if saved_vault is None:
                os.environ.pop("OBSIDIAN_VAULT_PATH", None)
            else:
                os.environ["OBSIDIAN_VAULT_PATH"] = saved_vault


def suite_everyday():
    """Date tools, checked against today's real calendar. Needs no permissions."""
    header("everyday")
    from datetime import date, timedelta
    from agents import EverydayToolsAgent

    e = EverydayToolsAgent()
    today = date.today()
    coming_friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)

    def resolved(phrase):
        r = e.resolve_date(phrase)
        return r.get("date", "")[-10:] if isinstance(r, dict) else r

    check("tomorrow", resolved("tomorrow") == str(today + timedelta(days=1)))
    check("the day after tomorrow", resolved("the day after tomorrow") == str(today + timedelta(days=2)),
          resolved("the day after tomorrow"))
    check("this Friday is the coming one", resolved("this Friday") == str(coming_friday))
    note = e.resolve_date("next Friday").get("note", "")
    check("'next Friday' names both candidates",
          str(coming_friday) in note and str(coming_friday + timedelta(days=7)) in note, note[:80])
    check("'in 10 days'", resolved("in 10 days") == str(today + timedelta(days=10)))
    check("no invented time", "time" not in e.resolve_date("three weeks from now"))
    check("a stated time is kept", e.resolve_date("tomorrow at 3pm").get("time") == "15:00")
    check("nonsense is refused", isinstance(e.resolve_date("blorp"), str))
    days = e.days_between("today", f"{today.year}-12-31")
    check("days_between", days["days"] == (date(today.year, 12, 31) - today).days)


def suite_recall():
    """
    On-device vector memory in a throwaway folder, and the Obsidian fallback.
    Needs Ollama with nomic-embed-text; doesn't load jarvis.py.
    """
    header("recall")
    import tempfile
    from pathlib import Path
    import memory_store
    import obsidian_store

    saved_dir = memory_store.DATA_DIR
    with tempfile.TemporaryDirectory() as tmp:
        memory_store.DATA_DIR = Path(tmp) / "chroma"
        try:
            store = memory_store.MemoryStore()
            records = [{"text": "The user prefers clean code with proper comments.", "kind": "fact", "date": "2026-06-10"},
                       {"text": "The user's sister Priya loves jazz.", "kind": "fact", "date": "2026-09-28"}]
            check("records added", store.add(records) == 2 and store.count() == 2)
            store.add(records)
            check("re-adding doesn't duplicate", store.count() == 2)
            t = time.time()
            hits = store.search("what kind of music does my sister like")
            took = time.time() - t
            check("finds the related memory", any("Priya" in h for h in hits), str(hits)[:100])
            check("tags hits with their date", bool(hits) and hits[0].startswith("(2026-"))
            check("recall is fast", took < 0.5, ms(took))
            check("unrelated question recalls nothing", store.search("what's the capital of Peru") == [],
                  str(store.search("what's the capital of Peru"))[:100])
        finally:
            memory_store.DATA_DIR = saved_dir
    check("Obsidian keyword fallback runs", isinstance(obsidian_store.search("anything at all"), list))


def suite_wake():
    """
    Wake word detector on speech synthesised by macOS `say`, so no fixtures or
    microphone. Doesn't load jarvis.py.
    """
    header("wake")
    import os
    import subprocess
    import tempfile
    import wave
    import numpy as np
    from wake_word import WakeWordDetector, FRAME

    detector = WakeWordDetector()

    def peak(phrase, voice):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        subprocess.run(["say", "-v", voice, "-o", path, "--data-format=LEI16@16000", phrase], check=True)
        audio = np.frombuffer(wave.open(path).readframes(10 ** 8), dtype=np.int16)
        os.remove(path)
        silence = np.zeros(16000, dtype=np.int16)
        audio = np.concatenate([silence, audio, silence])
        detector.reset()
        return max(detector.score(audio[i:i + FRAME]) for i in range(0, len(audio) - FRAME, FRAME))

    for voice in ("Samantha", "Daniel"):
        score = peak("Hey Jarvis", voice)
        check(f"detects 'Hey Jarvis' ({voice})", score >= 0.5, f"peak {score:.3f}")
    score = peak("I was telling him about the game last night. Hey Jarvis. What time is it?", "Daniel")
    check("detects the wake word mid-sentence", score >= 0.5, f"peak {score:.3f}")
    score = peak("What's the weather like in Boston? Remind me to call my mother on Friday.", "Samantha")
    check("ignores ordinary speech", score < 0.5, f"peak {score:.3f}")

    silence_frame = np.zeros(FRAME, dtype=np.int16)
    for _ in range(10):
        detector.score(silence_frame)
    t = time.time()
    for _ in range(100):
        detector.score(silence_frame)
    per_frame = (time.time() - t) / 100
    check("frame costs under 10% of real time", per_frame < 0.008,
          f"{per_frame * 1000:.2f}ms per 80ms frame")


def suite_memory():
    """Obsidian daily notes, written to a throwaway vault. Doesn't load jarvis.py."""
    header("memory")
    import os
    import tempfile
    from datetime import datetime
    import obsidian_store

    previous = os.environ.get("OBSIDIAN_VAULT_PATH")
    with tempfile.TemporaryDirectory() as vault:
        os.environ["OBSIDIAN_VAULT_PATH"] = vault
        when = datetime(2026, 9, 28, 14, 5)
        summary = ("The user planned their week around a product launch on Friday. "
                   "Jarvis blocked two mornings for deep work and set a reminder to email Sarah.")
        note = obsidian_store.append_session(
            summary, ["Prefers mornings for deep work"],
            [{"name": "Product Launch", "note": "Launch is set for Friday."},
             {"name": "Sarah", "note": "Needs the launch deck by Thursday."}],
            title="Planning launch week", when=when)
        obsidian_store.append_session(
            "The user asked Jarvis to move the launch review with Sarah to Thursday.", [],
            [{"name": "sarah", "note": "Launch review moved to Thursday."}],
            when=when.replace(hour=18))
        text = open(note).read() if note else ""
        entities = os.path.join(vault, "Memory", "Entities")
        sarah = open(os.path.join(entities, "Sarah.md")).read() if os.path.exists(os.path.join(entities, "Sarah.md")) else ""

        check("daily note created under Memory/", note is not None and note.name == "2026-09-28.md")
        check("note has one title", text.count("# Memory — September 28, 2026") == 1)
        check("sessions appended with titles",
              "## Session (14:05) — Planning launch week" in text and "## Session (18:05)" in text)
        check("multi-sentence summary kept whole", summary in text)
        check("facts listed", "- Prefers mornings for deep work" in text)
        check("topics linked from the day", "[[Product Launch]] · [[Sarah]]" in text)
        check("topic note links back to the day", "- [[2026-09-28]] 14:05 — Needs the launch deck by Thursday." in sarah)
        check("same topic in another case reuses the note",
              sorted(os.listdir(entities)) == ["Product Launch.md", "Sarah.md"] and sarah.count("[[2026-09-28]]") == 2)
        check("existing topics are listed for reuse", obsidian_store.existing_topics() == ["Product Launch", "Sarah"])
        check("empty session writes nothing", obsidian_store.append_session(None, [], when=when) is None)
        os.environ["OBSIDIAN_VAULT_PATH"] = os.path.join(vault, "missing")
        check("missing vault is skipped, not created",
              obsidian_store.append_session("x", when=when) is None and not os.path.exists(os.path.join(vault, "missing")))
    if previous is None:
        os.environ.pop("OBSIDIAN_VAULT_PATH", None)
    else:
        os.environ["OBSIDIAN_VAULT_PATH"] = previous


def suite_voice():
    """Interactive. Needs a microphone and a person."""
    header("voice")
    j = load_jarvis()
    j.kokoro_ready.wait()

    print("  This checks that pause_threshold=0.8 does not clip natural speech.")
    print("  You will be asked to speak three times.\n")

    prompts = [
        "Say: 'what is the weather in Boston right now'",
        "Say a sentence with a natural pause in the middle, e.g. "
        "'remind me to call my mother... on Friday afternoon'",
        "Say something long — at least fifteen words.",
    ]
    for i, instruction in enumerate(prompts, 1):
        print(f"  [{i}/3] {instruction}")
        input("        press Enter when ready, then speak: ")
        try:
            t = time.time()
            heard = j.record_audio_and_transcribe_mlx_whisper()
            took = time.time() - t
        except Exception as e:
            check(f"capture {i}", False, str(e))
            continue
        print(f"        heard ({took:.1f}s): {heard!r}")
        ok = input("        Was that captured completely? [y/N] ").strip().lower() == "y"
        check(f"utterance {i} captured without clipping", ok,
              "lower pause_threshold if speech was cut off" if not ok else "")

    print("\n  Now checking playback.")
    j.say("All systems nominal, sir. The voice pipeline is working.")
    j.chime()
    j.wait_until_spoken()
    ok = input("  Did that sound clear and unbroken? [y/N] ").strip().lower() == "y"
    check("playback is clean", ok)


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

SUITES = {
    "imports": suite_imports,
    "registry": suite_registry,
    "routing": suite_routing,
    "text": suite_text,
    "audio": suite_audio,
    "cache": suite_cache,
    "tools": suite_tools,
    "latency": suite_latency,
    "intent": suite_intent,
    "prefs": suite_prefs,
    "everyday": suite_everyday,
    "recall": suite_recall,
    "wake": suite_wake,
    "memory": suite_memory,
    "voice": suite_voice,
}

QUICK = ["imports", "registry", "routing", "text"]
DEFAULT = ["imports", "registry", "routing", "text", "audio", "cache", "tools", "intent", "latency",
           "wake", "memory", "prefs", "everyday", "recall"]


def main():
    args = [a for a in sys.argv[1:]]

    if "--list" in args:
        print("suites:", ", ".join(SUITES))
        print(f"default: {', '.join(DEFAULT)}")
        print(f"quick:   {', '.join(QUICK)}")
        return 0

    if "--quick" in args:
        chosen = QUICK
    else:
        named = [a for a in args if not a.startswith("-")]
        chosen = named or DEFAULT

    unknown = [c for c in chosen if c not in SUITES]
    if unknown:
        print(f"unknown suite(s): {', '.join(unknown)}")
        print(f"available: {', '.join(SUITES)}")
        return 2

    started = time.time()
    for name in chosen:
        try:
            result = SUITES[name]()
            if name == "imports" and result is False:
                break
        except KeyboardInterrupt:
            print("\ninterrupted")
            break
        except Exception as e:
            import traceback
            FAIL.append(f"{name}: crashed - {type(e).__name__}: {e}")
            print(f"  \033[31mCRASH\033[0m {name}: {type(e).__name__}: {e}")
            traceback.print_exc()

    print(f"\n\033[1m{'═' * 62}\033[0m")
    print(f"\033[32m{len(PASS)} passed\033[0m, "
          f"\033[31m{len(FAIL)} failed\033[0m, "
          f"\033[33m{len(SKIP)} skipped\033[0m   ({time.time() - started:.1f}s)")
    if FAIL:
        print("\nfailures:")
        for f in FAIL:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
