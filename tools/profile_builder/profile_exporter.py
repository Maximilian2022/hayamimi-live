# -*- coding: utf-8 -*-
"""Profile storage, persistence, export and import utilities for hayamimi."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from typing import List, Optional

from tools.profile_builder.profile_model import SpeakerProfile

logger = logging.getLogger("hayamimi.profile_exporter")

DEFAULT_PROFILES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "profiles",
)


def get_default_profiles_dir() -> str:
    """Resolve profiles directory considering environment variables and monkeypatches."""
    global DEFAULT_PROFILES_DIR
    env_prof = os.environ.get("HAYAMIMI_PROFILES_DIR")
    if env_prof:
        return env_prof
    env_cfg = os.environ.get("HAYAMIMI_CONFIG_DIR")
    if env_cfg:
        return os.path.join(env_cfg, "profiles")
    return DEFAULT_PROFILES_DIR


_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def is_safe_speaker_id(speaker_id: str) -> bool:
    """Validate that speaker_id is alphanumeric with underscores/hyphens and within length limits."""
    if not isinstance(speaker_id, str):
        return False
    return bool(_SAFE_ID_PATTERN.fullmatch(speaker_id))


def _atomic_write(target_path: str, content: str) -> None:
    """Write content to target_path atomically using a temp file and os.replace."""
    dir_name = os.path.dirname(target_path)
    os.makedirs(dir_name, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_prof_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target_path)
    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


def _get_safe_profile_path(speaker_id: str, suffix: str, storage_dir: str) -> str:
    """Build and verify that the target path does not escape storage_dir."""
    if not is_safe_speaker_id(speaker_id):
        raise ValueError(f"Invalid speaker_id format: {speaker_id!r}")
    real_storage = os.path.realpath(storage_dir)
    target = os.path.realpath(os.path.join(real_storage, f"{speaker_id}{suffix}"))
    # Ensure target is strictly inside real_storage
    if os.path.commonpath([real_storage, target]) != real_storage:
        raise ValueError(f"Path traversal detected for speaker_id: {speaker_id!r}")
    return target


def ensure_profiles_dir(directory: Optional[str] = None) -> str:
    """Ensure the profiles directory exists."""
    target_dir = directory or get_default_profiles_dir()
    os.makedirs(target_dir, exist_ok=True)
    return target_dir


def save_profile(profile: SpeakerProfile, storage_dir: Optional[str] = None) -> str:
    """Save profile atomically as both <speaker_id>.json and <speaker_id>_replace.txt."""
    effective_dir = storage_dir or get_default_profiles_dir()
    ensure_profiles_dir(effective_dir)

    # 1. Save JSON atomically
    json_path = _get_safe_profile_path(profile.speaker_id, ".json", effective_dir)
    _atomic_write(json_path, profile.model_dump_json(indent=2))

    # 2. Save replace.txt for direct hayamimi --replace compatibility
    txt_path = _get_safe_profile_path(profile.speaker_id, "_replace.txt", effective_dir)
    _atomic_write(txt_path, profile.to_replace_text())

    logger.info("Saved speaker profile '%s' to %s", profile.speaker_id, json_path)
    return json_path


def load_profile(speaker_id: str, storage_dir: Optional[str] = None) -> Optional[SpeakerProfile]:
    """Load a single speaker profile by ID."""
    effective_dir = storage_dir or get_default_profiles_dir()
    try:
        json_path = _get_safe_profile_path(speaker_id, ".json", effective_dir)
    except ValueError as ve:
        logger.warning("Rejected invalid speaker_id '%s': %s", speaker_id, ve)
        return None

    if not os.path.isfile(json_path):
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        prof = SpeakerProfile.model_validate(data)
        prof.migrate_from_legacy()
        return prof
    except Exception as e:
        logger.error("Failed to load profile %s: %s", json_path, e)
        return None


def list_profiles(storage_dir: Optional[str] = None) -> List[SpeakerProfile]:
    """List all available speaker profiles in storage directory."""
    effective_dir = storage_dir or get_default_profiles_dir()
    if not os.path.isdir(effective_dir):
        return []

    profiles: List[SpeakerProfile] = []
    for fname in os.listdir(effective_dir):
        if fname.endswith(".json"):
            sid = fname[:-5]
            if not is_safe_speaker_id(sid):
                continue
            prof = load_profile(sid, effective_dir)
            if prof is not None:
                profiles.append(prof)

    # Sort by display name or speaker_id
    profiles.sort(key=lambda p: p.display_name or p.speaker_id)
    return profiles


def delete_profile(speaker_id: str, storage_dir: Optional[str] = None) -> bool:
    """Delete profile files."""
    effective_dir = storage_dir or get_default_profiles_dir()
    try:
        json_path = _get_safe_profile_path(speaker_id, ".json", effective_dir)
        txt_path = _get_safe_profile_path(speaker_id, "_replace.txt", effective_dir)
    except ValueError as ve:
        logger.warning("Rejected delete for invalid speaker_id '%s': %s", speaker_id, ve)
        return False

    deleted = False
    if os.path.isfile(json_path):
        try:
            os.remove(json_path)
            deleted = True
        except Exception as e:
            logger.error("Failed to remove %s: %s", json_path, e)

    if os.path.isfile(txt_path):
        try:
            os.remove(txt_path)
            deleted = True
        except Exception as e:
            logger.error("Failed to remove %s: %s", txt_path, e)

    return deleted


def export_profile_json(profile: SpeakerProfile) -> str:
    """Export profile as formatted JSON string."""
    return profile.model_dump_json(indent=2)


def import_profile_json(
    json_content: str,
    storage_dir: Optional[str] = None,
    overwrite: bool = True,
) -> SpeakerProfile:
    """Validate and import a profile from JSON string."""
    effective_dir = storage_dir or get_default_profiles_dir()
    data = json.loads(json_content)
    profile = SpeakerProfile.model_validate(data)

    if not is_safe_speaker_id(profile.speaker_id):
        raise ValueError(f"Invalid speaker_id in imported profile: {profile.speaker_id!r}")

    existing = load_profile(profile.speaker_id, effective_dir)
    if existing and not overwrite:
        # Create a non-conflicting ID
        candidate_id = f"{profile.speaker_id}_imported"
        if not is_safe_speaker_id(candidate_id):
            candidate_id = "imported_profile"
        profile.speaker_id = candidate_id

    save_profile(profile, effective_dir)
    return profile
