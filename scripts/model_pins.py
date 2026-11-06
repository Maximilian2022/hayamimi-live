"""Pinned Hugging Face model revisions (scripts/model_pins.json) and install markers.

Problem: models were fetched from each repo's `main`, so a new install got
whatever upstream had that day while an old install kept what it had, and
faster-whisper could switch revisions on its own. Nobody, including the
author, could tell which revision a user was running.

Every repo hayamimi loads is pinned in model_pins.json. A downloaded
models/<dest>/ directory records the revision it holds in MARKER_NAME, so the
launcher and download_models.py can see when a pin moved and re-fetch.
"""

from __future__ import annotations

import json
import os

PINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_pins.json")
MARKER_NAME = ".hayamimi_revision"

_cache: dict | None = None


def load_pins() -> dict:
    """key -> pin entry (repo, revision, dest, ...)."""
    global _cache
    if _cache is None:
        with open(PINS_FILE, encoding="utf-8") as f:
            _cache = json.load(f)["models"]
    return _cache


def pin_for_repo(repo_or_alias: str) -> dict | None:
    """The pin whose repo or faster-whisper alias is repo_or_alias, else None."""
    for pin in load_pins().values():
        if repo_or_alias in (pin["repo"], pin.get("alias")):
            return pin
    return None


def revision_for(repo_or_alias: str) -> str | None:
    """Pinned revision for a repo id or alias; None for local paths and unpinned ids."""
    if not repo_or_alias or os.path.isdir(repo_or_alias):
        return None
    pin = pin_for_repo(repo_or_alias)
    return pin["revision"] if pin else None


def repo_for(repo_or_alias: str) -> str:
    """Resolve a faster-whisper alias (large-v3-turbo) to the pinned repo id."""
    pin = pin_for_repo(repo_or_alias)
    return pin["repo"] if pin else repo_or_alias


def resolve(repo_or_alias: str) -> tuple[str, str | None]:
    """(repo id or path, revision) to pass to WhisperModel / from_pretrained.

    A local directory passes through unchanged with no revision; a pinned id
    or alias becomes its pinned repo and revision; anything else is returned
    as given with revision None (upstream main).
    """
    if not repo_or_alias or os.path.isdir(repo_or_alias):
        return repo_or_alias, None
    pin = pin_for_repo(repo_or_alias)
    if not pin:
        return repo_or_alias, None
    return pin["repo"], pin["revision"]


def read_marker(target_dir: str) -> str | None:
    try:
        with open(os.path.join(target_dir, MARKER_NAME), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_marker(target_dir: str, revision: str) -> None:
    with open(os.path.join(target_dir, MARKER_NAME), "w", encoding="utf-8") as f:
        f.write(revision)


def install_state(pin: dict, target_dir: str) -> str:
    """'current', 'legacy' (pre-pinning install known to match), 'stale', or 'missing'."""
    if not os.path.isdir(target_dir):
        return "missing"
    marker = read_marker(target_dir)
    if marker == pin["revision"]:
        return "current"
    if marker is None and pin.get("legacy_ok"):
        return "legacy"
    return "stale"
