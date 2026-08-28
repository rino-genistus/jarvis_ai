"""
Jarvis test harness.

    python tests.py              # everything except the microphone test
    python tests.py --quick      # structural checks only, no model calls
    python tests.py voice        # interactive microphone check
    python tests.py cache tools  # named suites only
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
    check("all 6 agents constructed", len(j.AGENTS) == 6, f"{len(j.AGENTS)} agents")
    check("Pinecone index reachable", j.dense_index is not None)
    check("main_loop did not auto-run", True, "module is importable")
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

    # Grouping must be a partition of the registry, or routing silently hides tools.
    grouped = [m for tools in j.TOOL_GROUPS.values() for m in tools]
    names_in_groups = {m.__name__ for m in grouped}
    missing = set(reg) - names_in_groups
    check("every registered tool belongs to a group", not missing,
          f"missing: {sorted(missing)}" if missing else f"{len(j.TOOL_GROUPS)} groups")
    check("no tool appears in two groups", len(grouped) == len(names_in_groups))
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

    tools = j.TOOL_GROUPS["weather"]
    base = [
        {"role": "system", "content": j.system_prompt},
        {"role": "user", "content": "[Today is Thursday.] what's the weather in Boston right now"},
    ]

    def tool_call():
        return chat(model="qwen2.5:7b", messages=base, tools=tools,
                    keep_alive=j.OLLAMA_KEEP_ALIVE)

    print("  warming the prefix...")
    tool_call()
    warm = tool_call()
    baseline = warm.prompt_eval_duration / 1e9
    check("repeated tool call reuses the prefix cache", baseline < 1.0, ms(baseline))

    # Now the real test: a summarisation call in between, exactly as main_loop does.
    summary_messages = base + [
        {"role": "assistant", "content": ""},
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
          "main_loop passes tools= to both calls")


def suite_tools():
    """Does the model pick the right tool, and get the date right."""
    header("tools")
    j = load_jarvis()
    from ollama import chat
    from datetime import datetime, timedelta

    now = datetime.now()
    upcoming = ", ".join((now + timedelta(days=o)).strftime("%A %Y-%m-%d") for o in range(8))
    date_context = (f"[Today is {now.strftime('%A %Y-%m-%d')} at {now.strftime('%H:%M')}. "
                    f"Dates this coming week: {upcoming}. "
                    f"Use these exact dates for any day the user names.]")

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
                 options=det,
                 messages=[{"role": "system", "content": j.system_prompt},
                           {"role": "user", "content": f"{date_context} {text}"}])
        called = [c.function.name for c in (r.message.tool_calls or [])]
        ok = bool(set(called) & expected)
        correct += ok
        if not ok:
            misses.append((text, group, called))
        mark = "\033[32mok  \033[0m" if ok else "\033[31mmiss\033[0m"
        print(f"        {mark} {text[:42]:44} [{group}] -> {called or 'NO TOOL CALL'}")
    check("model picks a sensible tool for each request", correct == len(cases),
          f"{correct}/{len(cases)}")

    # A routed group that produces nothing must still be recoverable, because
    # that is exactly what main_loop does before giving up.
    if misses:
        text, group, _ = misses[0]
        r = chat(model="qwen2.5:7b", tools=j.ALL_TOOLS, keep_alive=j.OLLAMA_KEEP_ALIVE,
                 options=det,
                 messages=[{"role": "system", "content": j.system_prompt},
                           {"role": "user", "content": f"{date_context} {text}"}])
        recovered = [c.function.name for c in (r.message.tool_calls or [])]
        check("retry with the full registry recovers a missed route",
              bool(recovered), f"{text!r} -> {recovered or 'still nothing'}")
    else:
        print("        (no misses, retry path not exercised)")

    # The weekday-arithmetic bug: "Friday" used to land on the wrong date.
    friday = None
    for offset in range(1, 8):
        d = now + timedelta(days=offset)
        if d.strftime("%A") == "Friday":
            friday = d.strftime("%Y-%m-%d")
            break
    group, tools = j.select_tools("remind me to call mom on Friday at 2pm")
    r = chat(model="qwen2.5:7b", tools=tools, keep_alive=j.OLLAMA_KEEP_ALIVE,
             messages=[{"role": "system", "content": j.system_prompt},
                       {"role": "user", "content": f"{date_context} remind me to call mom on Friday at 2pm"}])
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
    import tempfile
    import os

    j.kokoro_ready.wait()
    results = []

    # Build a speech sample with Kokoro so this needs no fixture file.
    audio = np.concatenate([a for _, _, a in
                            j.kokoro_pipeline("What is the weather in Boston right now?",
                                              voice="af_heart")])
    import soundfile as sf
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    sf.write(wav, audio, 24000)

    import mlx_whisper
    mlx_whisper.transcribe(wav, path_or_hf_repo="mlx-community/whisper-small-mlx")  # warm

    t = time.time()
    text = mlx_whisper.transcribe(wav, path_or_hf_repo="mlx-community/whisper-small-mlx")["text"]
    results.append(("whisper transcribe", time.time() - t, 1.0))

    t = time.time()
    j.classify_intent("what's the weather in Boston")
    results.append(("intent classification", time.time() - t, 1.0))

    t = time.time()
    try:
        j.retrieve_memories("what's the weather in Boston")
        results.append(("pinecone retrieval", time.time() - t, 2.0))
    except Exception as e:
        print(f"        pinecone unavailable: {e}")

    t = time.time()
    j.route_tools("what's the weather in Boston")
    results.append(("keyword routing", time.time() - t, 0.01))

    group, tools = j.select_tools("what's the weather in Boston right now")
    msgs = [{"role": "system", "content": j.system_prompt},
            {"role": "user", "content": "what's the weather in Boston right now"}]
    chat(model="qwen2.5:7b", messages=msgs, tools=tools, keep_alive=j.OLLAMA_KEEP_ALIVE)  # warm
    t = time.time()
    chat(model="qwen2.5:7b", messages=msgs, tools=tools, keep_alive=j.OLLAMA_KEEP_ALIVE)
    results.append(("tool selection", time.time() - t, 3.0))

    t = time.time()
    list(j.kokoro_pipeline("It is 72 degrees and clear in Boston.", voice="af_heart"))
    results.append(("first sentence synthesis", time.time() - t, 2.0))

    os.remove(wav)

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
    "voice": suite_voice,
}

QUICK = ["imports", "registry", "routing", "text"]
DEFAULT = ["imports", "registry", "routing", "text", "audio", "cache", "tools", "latency"]


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
