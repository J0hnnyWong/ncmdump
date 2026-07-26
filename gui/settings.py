"""Persistent user settings stored as JSON."""

import json
from pathlib import Path

_SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"

_DEFAULTS = {
    "mp3_convert": False,
    "fill_metadata": False,
}


def load() -> dict:
    if _SETTINGS_FILE.is_file():
        try:
            with open(_SETTINGS_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
        return {**_DEFAULTS, **data}
    return dict(_DEFAULTS)


def save(data: dict) -> None:
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)
