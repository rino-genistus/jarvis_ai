"""
The Jarvis brain — local LLM reasoning via Ollama (no paid APIs).

Unified routing: every command goes straight to the main model with the full
tool list. There is no pre-classification step — the old llama3.2:1b intent
call per turn is gone.

Agentic tool loop: the model may chain tools across multiple rounds (find the
emails -> summarise -> create the calendar block) up to MAX_TOOL_ROUNDS. Tool
failures are retried once, then handed to the model as a structured error
template so it always has something useful to say.

Session control: the model appends [END] when the conversation is over; a
farewell-phrase fallback on the user's own words and a check on the model's
reply catch the cases it misses.

History pruning: beyond HISTORY_MAX_TURNS the oldest turns are folded into a
one-paragraph summary so long sessions never hit the context window.
"""

import re
from datetime import datetime

from ollama import chat

import config
from logging_setup import get_logger

log = get_logger("jarvis.brain")

END_TOKEN = "[END]"

FAREWELL_PHRASES = (
    "goodbye", "good bye", "bye bye", "farewell", "good night", "goodnight",
    "that's all", "that is all", "that'll be all", "that will be all",
    "you're dismissed", "dismissed", "go to sleep", "sleep mode", "stand by",
    "standby", "power down", "shut down", "shutdown", "we're done", "we are done",
    "i'm done", "i am done", "end session", "stop listening", "stop jarvis",
    "that's enough", "that is enough", "enough for now", "talk later",
    "talk to you later", "we'll talk later", "catch you later", "until next time",
    "thank you jarvis", "thanks jarvis",
)

_FAREWELL_REPLY = re.compile(
    r"\b(goodbye|good night|goodnight|until (next time|tomorrow)|signing off|standing by)\b",
    re.IGNORECASE,
)

FILLER_LINES = ("On it, sir.", "One moment.", "Right away, sir.", "Working on it.")


def build_system_prompt() -> str:
    current_date = datetime.now().strftime("%A, %B %d, %Y")
    return f"""You are JARVIS (Just A Rather Very Intelligent System), the user's personal AI assistant. You run on his Mac and speak to him out loud.

## Context
Today's date is {current_date}. Use it for any scheduling or time-related task.

## Personality
Quiet confidence, calm authority, dry wit that surfaces naturally — never forced. You are precise and direct, like a trusted right-hand who knows the user well and doesn't waste his time. Address him as "sir" where it lands naturally — not in every sentence.

## Communication style
- You are heard, not read. Speak in natural sentences. Never use bullet points, headers, markdown, or emoji.
- Be concise. No affirmations like "Certainly!" or "Great question!" — just answer.
- Match the energy: quick question, quick answer; real problem, thorough response.
- Do not end responses with follow-up questions by habit. Only ask one when you genuinely need information to proceed.
- If you don't know, say so plainly. Never fabricate.

## Tools
You have tools for calendar, email, web search, weather, Spotify, and controlling this Mac (apps and files). Use them without announcing that you will. You may chain tools across multiple steps when a task needs it. If a tool reports an error, tell the user briefly what failed and what you suggest — never read raw error text aloud.

## Memory
Below this prompt you may find facts about the user retrieved from long-term memory and a note on the last conversation. Treat them as things you simply know — never say "according to my memory". If no facts are provided, your slate is blank: never invent prior context.

## Ending sessions — important
When the conversation is genuinely over — the user says goodbye, dismisses you, says that's all, or the exchange has clearly concluded — append the exact token {END_TOKEN} to the very end of your reply. Examples: "Goodnight, sir. {END_TOKEN}" / "I'll be here. {END_TOKEN}". Do NOT append it when the user might plausibly continue. Never mention or explain the token.

Respond only with your spoken reply — no meta-commentary."""


def is_farewell(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in FAREWELL_PHRASES)


class JarvisBrain:
    def __init__(self, tools: dict, bridge=None, on_tools_start=None):
        """
        tools:          {name: callable} — callables carry the docstrings Ollama
                        turns into tool schemas.
        bridge:         UIBridge for tool/state events (optional).
        on_tools_start: called once per command when the first tool round begins
                        (the orchestrator speaks a short filler line).
        """
        self.tools = tools
        self.bridge = bridge
        self.on_tools_start = on_tools_start
        self.base_prompt = build_system_prompt()
        self.context_block = ""
        self.messages: list[dict] = [{"role": "system", "content": self.base_prompt}]

    # ------------------------------------------------------------ context
    def set_session_context(self, last_episode: str | None) -> None:
        """Cross-session continuity: open with what happened last time."""
        self.context_block = ""
        if last_episode:
            self.context_block = f"\n\n## Last conversation\n{last_episode}"
        self._refresh_system()

    def set_memories(self, memories: list[str]) -> None:
        block = ""
        if memories:
            lines = "\n".join(f"- {m}" for m in memories)
            block = f"\n\n## What you know about the user\n{lines}"
        self.messages[0]["content"] = self.base_prompt + self.context_block + block

    def _refresh_system(self) -> None:
        self.messages[0]["content"] = self.base_prompt + self.context_block

    def reset(self) -> None:
        self.base_prompt = build_system_prompt()  # refresh the date
        self.messages = [{"role": "system", "content": self.base_prompt}]
        self.context_block = ""

    # ------------------------------------------------------------ main entry
    def respond(self, user_text: str) -> tuple[str, bool]:
        """Run one command through the agentic loop. Returns (spoken, session_over)."""
        timestamp = datetime.now().strftime("%H:%M")
        self.messages.append({"role": "user", "content": f"[{timestamp}] {user_text}"})
        self._prune_history()

        spoken = ""
        used_tools = False
        for round_no in range(config.MAX_TOOL_ROUNDS):
            response = chat(
                model=config.MAIN_MODEL,
                messages=self.messages,
                tools=list(self.tools.values()),
            )
            message = response.message
            if not message.tool_calls:
                spoken = message.content or ""
                self.messages.append({"role": "assistant", "content": spoken})
                break

            if not used_tools:
                used_tools = True
                if self.on_tools_start:
                    self.on_tools_start()

            self.messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [tc.model_dump() for tc in message.tool_calls],
            })
            for call in message.tool_calls:
                result = self._execute_tool(call.function.name, dict(call.function.arguments))
                self.messages.append({
                    "role": "tool",
                    "tool_name": call.function.name,
                    "content": result,
                })
        else:
            # round budget exhausted — force a wrap-up without tools
            self.messages.append({
                "role": "user",
                "content": "Summarize what you found and what you did, concisely, in your own voice.",
            })
            response = chat(model=config.MAIN_MODEL, messages=self.messages)
            spoken = response.message.content or ""
            self.messages.append({"role": "assistant", "content": spoken})

        if used_tools and not spoken.strip():
            # model returned tool calls then went quiet — ask it to report back
            self.messages.append({
                "role": "user",
                "content": "Summarize the tool results naturally in your own voice — key points only.",
            })
            response = chat(model=config.MAIN_MODEL, messages=self.messages)
            spoken = response.message.content or ""
            self.messages.append({"role": "assistant", "content": spoken})

        spoken, ended = self._check_session_end(user_text, spoken)
        return spoken.strip(), ended

    # ------------------------------------------------------------ tools
    def _execute_tool(self, name: str, args: dict) -> str:
        fn = self.tools.get(name)
        if fn is None:
            return f"TOOL ERROR: no tool named {name} exists. Choose from the available tools."
        if self.bridge:
            self.bridge.tool(name, "start")
        log.info("tool %s(%s)", name, args)
        for attempt in (1, 2):
            try:
                result = fn(**args)
                if self.bridge:
                    self.bridge.tool(name, "done")
                log.debug("tool %s result: %.300s", name, result)
                return str(result) if result is not None else "Done."
            except TypeError as e:
                # bad arguments — retrying identical args is pointless
                if self.bridge:
                    self.bridge.tool(name, "error")
                return (f"TOOL ERROR in {name}: invalid arguments ({e}). "
                        f"Adjust the arguments or tell the user what's missing.")
            except Exception as e:  # noqa: BLE001
                log.warning("tool %s failed (attempt %d): %s", name, attempt, e)
                if attempt == 1:
                    continue
                if self.bridge:
                    self.bridge.tool(name, "error")
                return (f"TOOL ERROR in {name}: {type(e).__name__}: {e}. "
                        f"Briefly tell the user this step failed and suggest an alternative. "
                        f"Do not read this error text aloud.")
        return "TOOL ERROR: unreachable"

    # ------------------------------------------------------------ session end
    def _check_session_end(self, user_text: str, spoken: str) -> tuple[str, bool]:
        ended = False
        if END_TOKEN in spoken:
            spoken = spoken.replace(END_TOKEN, "").strip()
            ended = True
        elif is_farewell(user_text):
            ended = True  # client-side fallback when the model misses [END]
        elif _FAREWELL_REPLY.search(spoken or ""):
            ended = True  # the model said goodbye without the token
        return spoken, ended

    # ------------------------------------------------------------ pruning
    def _prune_history(self) -> None:
        body = self.messages[1:]
        if len(body) <= config.HISTORY_MAX_TURNS:
            return
        old, recent = body[:-config.HISTORY_KEEP_RECENT], body[-config.HISTORY_KEEP_RECENT:]
        transcript = "\n".join(
            f"{m.get('role')}: {str(m.get('content', ''))[:400]}" for m in old if m.get("content")
        )
        summary = ""
        try:
            response = chat(
                model=config.UTILITY_MODEL,
                messages=[{
                    "role": "user",
                    "content": "Summarize this conversation in one short paragraph, keeping any "
                               "facts, decisions, names, and unfinished tasks:\n\n" + transcript,
                }],
            )
            summary = (response.message.content or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("history summary failed, truncating instead: %s", e)
        rebuilt = [self.messages[0]]
        if summary:
            rebuilt.append({"role": "system", "content": f"Earlier in this conversation: {summary}"})
        rebuilt.extend(recent)
        self.messages = rebuilt
        log.info("history pruned: %d -> %d messages", 1 + len(body), len(self.messages))
