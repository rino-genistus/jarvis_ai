"""
Jarvis's standing preferences — what the user has told him once and expects
to be kept, like "I prefer Celsius" or "call me Rino".

Stored in data/preferences.json on this Mac, and mirrored to
<vault>/Memory/Preferences.md so the list can be read in Obsidian. The JSON
file is the source of truth; the note is rewritten from it on every change.

Known preferences change behaviour directly (the weather tools read
temperature_units and home_location; replies use address_as). Anything else
is kept as a free-form note that Jarvis sees in every conversation.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import obsidian_store

PATH = Path(__file__).resolve().parent / "data" / "preferences.json"

# name -> (allowed values or None for free text, how to state it)
KNOWN = {
    "temperature_units": ({"celsius", "fahrenheit"}, "Give temperatures in {value}."),
    "home_location": (None, "Home is {value}; use it when a place isn't named."),
    "address_as": (None, 'Address the user as "{value}".'),
}
ALIASES = {
    "units": "temperature_units", "temperature": "temperature_units", "temperature_unit": "temperature_units",
    "home": "home_location", "location": "home_location", "city": "home_location",
    "name": "address_as", "call_me": "address_as", "title": "address_as",
}


def load():
    try:
        with open(PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("notes", [])
    return data


def get(name, default=None):
    return load().get(name) or default


def _save(data):
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, PATH)
    _mirror_to_obsidian(data)


def _normalise(name):
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    return ALIASES.get(key, key)


def set_preference(name, value):
    """Save one preference. Returns a sentence describing what was saved."""
    key, value = _normalise(name), str(value).strip()
    if not value:
        return "No value given."
    data = load()
    if key in KNOWN:
        allowed, _ = KNOWN[key]
        if allowed is not None:
            value = value.lower()
            if value not in allowed:
                return f"{key} must be one of: {', '.join(sorted(allowed))}."
        data[key] = value
        _save(data)
        return f"Saved: {KNOWN[key][1].format(value=value)}"
    note = value if key in ("note", "other", "preference") else f"{name.strip()}: {value}"
    if note.lower() not in (n.lower() for n in data["notes"]):
        data["notes"].append(note)
        _save(data)
    return f"Saved: {note}"


def forget(name_or_text):
    """Remove a known preference by name, or a note containing the text."""
    data = load()
    key = _normalise(name_or_text)
    if key in KNOWN and data.get(key):
        del data[key]
        _save(data)
        return f"Forgot {key}."
    needle = name_or_text.strip().lower()
    kept = [n for n in data["notes"] if needle not in n.lower()]
    removed = len(data["notes"]) - len(kept)
    if not removed:
        return f"No preference matching {name_or_text!r}."
    data["notes"] = kept
    _save(data)
    return f"Forgot {removed} matching note{'s' if removed > 1 else ''}."


def statements(data=None):
    """Every preference as a plain sentence."""
    data = data if data is not None else load()
    lines = [template.format(value=data[key]) for key, (_, template) in KNOWN.items() if data.get(key)]
    return lines + list(data.get("notes", []))


def prompt_block():
    """The section of the system prompt that keeps Jarvis consistent with them."""
    lines = statements()
    if not lines:
        return ""
    return ("\n## The user's standing preferences\nFollow these without being reminded:\n"
            + "\n".join(f"- {line}" for line in lines) + "\n")


def address(default="sir"):
    return get("address_as", default)


def _mirror_to_obsidian(data):
    """Rewrite Memory/Preferences.md from the JSON, if the vault exists."""
    vault = obsidian_store.vault_path()
    if not vault.is_dir():
        return
    lines = ["# Preferences", "",
             "What Jarvis keeps to without being reminded. Change these by telling Jarvis",
             '("I prefer Celsius", "forget my home location") — this note is rewritten from',
             "`data/preferences.json` in the Jarvis folder whenever they change.", ""]
    lines += [f"- {line}" for line in statements(data)] or ["- (none yet)"]
    lines += ["", f"_Updated {data.get('updated', '')}_", ""]
    (vault / "Memory").mkdir(exist_ok=True)
    (vault / "Memory" / "Preferences.md").write_text("\n".join(lines), encoding="utf-8")
