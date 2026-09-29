"""
Every agent's tools, with the words that call for each one and what Jarvis
says as he starts on it.

    AGENT_TOOLS = {agent class name: {tool name: Tool(words, ack, needs)}}

Tool selection is almost all prompt reading: qwen2.5:7b on an M4 reads about
200 tokens a second, and Ollama's cache rarely survives between turns. All 14
Gmail schemas with the full system prompt measured 10.3s; the two or three a
request actually needs, with a short prompt, about 3s. So a request gets only
the tools whose words it contains — picked by pick() below — and the rest of
its agent only if the model finds nothing to call among them.

    words  regex fragments, matched whole-word against the lowercased request
    ack    what Jarvis says the moment he starts, before the model has decided
           anything. Phrased for the action, so "add lunch on Friday" hears
           "Sure, I'll put that in" rather than a generic "One moment".
    needs  tools sent alongside, for tools that take an id from another one:
           moving an event needs the calendar read first to find it.

Order inside an agent matters twice: the first tools are the fallback when no
word matches, so the most common reads go first, and on a tie the earlier tool
gives the acknowledgement.

A tool may appear under a second agent to be offered in that agent's group too
(Apple Calendar with the Google Calendar tools). Every public method of every
agent must appear somewhere — jarvis.py refuses to start otherwise, so a new
tool can't be silently unreachable.
"""

import re
from typing import NamedTuple


class Tool(NamedTuple):
    words: tuple
    ack: tuple
    needs: tuple = ()


AGENT_TOOLS = {
    "WeatherSearch": {
        "get_current_weather": Tool(
            (r"now", r"right now", r"outside", r"currently", r"today", r"at the moment",
             r"how hot", r"how cold", r"temperature"),
            ("Let me take a look outside.", "Checking the weather now.", "Let me see what it's doing out there.")),
        "get_daily_forecast": Tool(
            (r"forecast", r"tomorrow", r"this week", r"next week", r"weekend", r"next few days",
             r"coming days", r"(mon|tues|wednes|thurs|fri|satur|sun)day"),
            ("Let me pull up the forecast.", "Let me see what the next few days look like.")),
        "get_weather_with_time": Tool(
            (r"tonight", r"this evening", r"this afternoon", r"this morning", r"later",
             r"o'clock", r"noon", r"\d+ ?(am|pm|a\.m|p\.m)"),
            ("Let me see what it'll be like then.", "Checking the hourly forecast.")),
        "get_weather_alerts": Tool(
            (r"alerts?", r"warnings?", r"storms?", r"hurricane", r"tornado", r"severe", r"flood\w*"),
            ("Let me check for any warnings.", "Checking for weather alerts.")),
    },

    "Calendar_Agents": {
        "get_calendar_events": Tool(
            (r"what's on", r"whats on", r"what do i have", r"am i free", r"free", r"busy",
             r"calendar", r"schedule", r"agenda", r"meetings?", r"events?", r"appointments?"),
            ("Let me take a look at your calendar.", "Let me see what you've got on.",
             "Pulling up your schedule.")),
        "create_event": Tool(
            (r"add", r"schedule an?", r"book", r"create", r"set up", r"put", r"new event", r"block"),
            ("Sure, I'll put that in your calendar.", "Sure, adding that now.", "Of course, let me set that up.")),
        "update_calendar_event": Tool(
            (r"move", r"reschedule", r"change", r"push", r"update", r"rename", r"shift"),
            ("Sure, let me move that.", "Let me find that event and change it."),
            needs=("get_calendar_events",)),
        "delete_calendar_event": Tool(
            (r"delete", r"cancel", r"remove", r"clear"),
            ("Sure, let me take that off your calendar.", "Let me find it and cancel it."),
            needs=("get_calendar_events",)),
        # Owned by EverydayToolsAgent; offered here too for "what's on my calendar"
        "get_apple_calendar_events": Tool(
            (r"apple calendar", r"icloud", r"family calendar", r"shared calendar"),
            ("Let me check your Apple calendar.",)),
    },

    "GmailAgent": {
        "get_unread_emails": Tool(
            (r"unread", r"new e?mails?", r"new mail", r"inbox", r"any e?mails?", r"check my (e)?mail"),
            ("Let me see what's come in.", "Checking your inbox now.", "Let me take a look at your email.")),
        "search_email": Tool(
            (r"from", r"about", r"find", r"search", r"look for", r"last e?mail", r"latest e?mail"),
            ("Let me dig through your email.", "Let me find that email.")),
        "get_email_by_id": Tool(
            (r"read me", r"read it", r"open", r"what does it say", r"full e?mail", r"that e?mail"),
            ("Let me open that up.", "Pulling that email up."),
            needs=("search_email",)),
        "send_email": Tool(
            (r"send", r"write", r"e?mail to", r"compose", r"shoot", r"message to"),
            ("Sure, let me write that up.", "Sure, I'll get that sent.")),
        "reply_to_email": Tool(
            (r"reply", r"respond", r"answer", r"write back"),
            ("Sure, let me write that reply.", "Let me find it and reply."),
            needs=("search_email",)),
        "mark_as_read": Tool(
            (r"mark\b.*\bread", r"read already"),
            ("Sure, marking that read.",),
            needs=("search_email",)),
        "mark_as_unread": Tool(
            (r"mark\b.*\bunread",),
            ("Sure, marking that unread.",),
            needs=("search_email",)),
        "trash_email": Tool(
            (r"trash", r"delete", r"bin", r"get rid of"),
            ("Sure, let me bin that.", "Let me find it and delete it."),
            needs=("search_email",)),
        "remove_email_from_trash": Tool(
            (r"restore", r"untrash", r"out of the trash", r"recover"),
            ("Let me fish that out of the trash.",)),
        "get_drafts": Tool(
            (r"drafts?",),
            ("Let me look at your drafts.",)),
        "send_draft": Tool(
            (r"send\b.*\bdraft",),
            ("Sure, sending that draft.",),
            needs=("get_drafts",)),
        "get_sent_emails": Tool(
            (r"sent", r"i sent", r"did i send", r"outbox"),
            ("Let me check what you've sent.",)),
        "get_all_labels": Tool(
            (r"labels?", r"folders?"),
            ("Let me look at your labels.",)),
        "get_sender_profile": Tool(
            (r"my e?mail address", r"which account", r"signed in", r"what account"),
            ("Let me check which account I'm using.",)),
    },

    "RemindersAgent": {
        "get_reminders": Tool(
            (r"what reminders", r"my reminders", r"reminders", r"to-?do", r"task list", r"tasks",
             r"on my list"),
            ("Let me check your reminders.", "Let me see what's on your list.")),
        "add_reminder": Tool(
            (r"remind me", r"add", r"don't let me forget", r"new reminder", r"put",
             r"create a reminder", r"set a reminder"),
            ("Sure, I'll remind you.", "Sure, adding that now.", "Consider it noted, let me add it.")),
        "get_due_reminders": Tool(
            (r"due", r"today", r"tomorrow", r"overdue", r"this week", r"coming up"),
            ("Let me see what's due.", "Checking what's coming up.")),
        "complete_reminder": Tool(
            (r"done", r"complete", r"completed", r"finished", r"tick off", r"check off", r"mark"),
            ("Nice, let me tick that off.", "Sure, marking that done.")),
        "delete_reminder": Tool(
            (r"delete", r"remove", r"cancel", r"get rid of"),
            ("Sure, let me take that off your list.",)),
        "update_reminder": Tool(
            (r"change", r"move", r"update", r"rename", r"push", r"reschedule"),
            ("Sure, let me change that.",)),
        "get_reminder_lists": Tool(
            (r"lists", r"which lists?", r"what lists?"),
            ("Let me see which lists you have.",)),
        "create_reminder_list": Tool(
            (r"new list", r"create an? list", r"make an? list", r"create list"),
            ("Sure, let me set up that list.",)),
    },

    "SpotifyAgent": {
        "search_song_and_queue": Tool(
            (r"play", r"queue", r"put on", r"listen to"),
            ("Sure, let me put that on.", "Coming right up.", "Good choice, one second.")),
        "get_current_track": Tool(
            (r"what's playing", r"whats playing", r"what song", r"this song", r"who sings",
             r"what is this", r"currently playing", r"name of"),
            ("Let me see what's playing.",)),
        "skip_song": Tool(
            (r"skip", r"next", r"next song", r"next track"),
            ("Skipping.", "Sure, next one.")),
        "previous_song": Tool(
            (r"previous", r"go back", r"last song", r"back one", r"replay"),
            ("Sure, going back.",)),
        "pause_song": Tool(
            (r"pause", r"stop", r"stop the music", r"hold"),
            ("Pausing.", "Sure, pausing that.")),
        "resume_song": Tool(
            (r"resume", r"unpause", r"continue", r"keep playing", r"play again"),
            ("Picking up where you left off.",)),
        "set_volume": Tool(
            (r"volume", r"louder", r"quieter", r"turn it (up|down)", r"turn (up|down)"),
            ("Sure.", "Adjusting the volume.")),
        "shuffle": Tool(
            (r"shuffle",),
            ("Sure, shuffling.",)),
        "recently_played": Tool(
            (r"recently played", r"recent", r"played earlier", r"last played", r"listening history"),
            ("Let me look at what you've played lately.",)),
        "create_playlist": Tool(
            (r"new playlist", r"create an? playlist", r"make an? playlist", r"create playlist"),
            ("Sure, let me make that playlist.",)),
        "add_song_to_playlist": Tool(
            (r"add\b.*\bplaylist", r"to my playlist", r"to the playlist", r"save this song"),
            ("Sure, adding it to the playlist.",)),
    },

    "WebSearchAgents": {
        "search_web": Tool(
            (r"search", r"look up", r"google", r"find", r"who is", r"what is", r"news", r"latest"),
            ("Let me look that up.", "Searching now.", "Let me see what I can find.")),
        "research": Tool(
            (r"research", r"deep dive", r"in depth", r"thorough\w*", r"dig into", r"compare"),
            ("Let me dig into that properly.", "Give me a moment to research that.")),
        "extract_webpages": Tool(
            (r"this page", r"the page", r"article", r"extract", r"summari[sz]e the", r"read the"),
            ("Let me read through that page.",)),
        "crawl_webpages": Tool(
            (r"crawl", r"website", r"site", r"https?", r"www", r"\w+\.(com|org|net|io)"),
            ("Let me go through that site.",)),
    },

    "EverydayToolsAgent": {
        "resolve_date": Tool(
            (r"what date", r"what's the date", r"whats the date", r"what day", r"date",
             r"which day", r"when is"),
            ("Let me work that out.", "One second.")),
        "get_current_time": Tool(
            (r"what time", r"time is it", r"the time"),
            ("Let me check.",)),
        "days_between": Tool(
            (r"how many (days|weeks)", r"(days|weeks) (until|since|till)", r"how long (until|since|till)"),
            ("Let me count that up.", "Let me work that out.")),
        "find_contact": Tool(
            (r"contacts?", r"phone number", r"number for", r"address for", r"e?mail address",
             r"birthday"),
            ("Let me check your contacts.", "Let me look them up.")),
        "search_notes": Tool(
            (r"notes?", r"apple notes", r"wrote down", r"jotted"),
            ("Let me look through your notes.", "Let me check your notes.")),
        "read_note": Tool(
            (r"read", r"open", r"read me", r"what does"),
            ("Let me pull that note up.",),
            needs=("search_notes",)),
        "get_apple_calendar_events": Tool(
            (r"apple calendar", r"icloud", r"calendar"),
            ("Let me check your Apple calendar.",)),
    },

    "PreferencesAgent": {
        "set_preference": Tool(
            (r"prefer", r"from now on", r"call me", r"always", r"never", r"i live in",
             r"my home is", r"remember that"),
            ("Noted.", "Got it.", "Understood.")),
        "forget_preference": Tool(
            (r"forget", r"stop calling", r"don't call me", r"remove", r"clear"),
            ("Sure, forgetting that.", "Got it.")),
        "list_preferences": Tool(
            (r"my preferences", r"what do you know", r"what are my", r"list"),
            ("Let me see what I've got.",)),
    },
}

# How many tools a request gets before the fallback to its whole agent
MAX_PICKED = 4
# Sent when no word matches: the agent's first (most common) tools
FALLBACK_COUNT = 2

_PATTERNS = {
    (agent, name): re.compile(r"\b(?:" + "|".join(tool.words) + r")\b")
    for agent, tools in AGENT_TOOLS.items()
    for name, tool in tools.items()
}


def _match_strength(agent, name, lowered):
    """Length of the longest phrase that matched — 'mark ... unread' beats 'unread'."""
    return max((len(m.group(0)) for m in _PATTERNS[(agent, name)].finditer(lowered)), default=0)


def pick(agent, text):
    """
    The tool names a request needs from one agent, best match first: the tools
    whose words it contains, plus what those tools need, at most MAX_PICKED. The
    agent's first FALLBACK_COUNT tools when nothing matches.
    """
    lowered = text.lower()
    tools = AGENT_TOOLS[agent]
    order = list(tools)
    strength = {name: _match_strength(agent, name, lowered) for name in tools}
    hits = [n for n in tools if strength[n]]
    # A tool another hit needs is there to support it, so it never leads:
    # "cancel my 3pm meeting" is a delete that reads the calendar first, and
    # should be acknowledged as one
    helpers = {need for n in hits for need in tools[n].needs}
    matched = sorted(hits, key=lambda n: (n in helpers, -strength[n], order.index(n)))
    if not matched:
        return order[:FALLBACK_COUNT]
    picked = []
    for name in matched:
        for wanted in (name, *tools[name].needs):
            if wanted not in picked:
                picked.append(wanted)
    return picked[:MAX_PICKED]


def acknowledgements(agent, tool_name):
    """The phrases for starting a tool, or () if the catalogue has none."""
    tool = AGENT_TOOLS.get(agent, {}).get(tool_name)
    return tool.ack if tool else ()
