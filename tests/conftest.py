# -*- coding: utf-8 -*-
"""Global test configuration and fixtures for Hayamimi test suite.
Ensures test environment isolation so running pytest never touches or pollutes
the real config/, profiles/, or runtime configurations.
"""

from __future__ import annotations

import os
import pytest


@pytest.fixture(autouse=True)
def isolate_test_environment(tmp_path, monkeypatch):
    """Automatically isolate configuration and profile storage directories to tmp_path."""
    test_cfg_dir = str(tmp_path / "config")
    test_prof_dir = str(tmp_path / "profiles")
    os.makedirs(test_cfg_dir, exist_ok=True)
    os.makedirs(test_prof_dir, exist_ok=True)

    # 1. Isolate subtitle_server storage directory via HAYAMIMI_CONFIG_DIR
    monkeypatch.setenv("HAYAMIMI_CONFIG_DIR", test_cfg_dir)

    try:
        import scripts.subtitle_server as ss
        monkeypatch.setattr(ss, "_CACHED_STORAGE_DIR", None)
    except Exception:
        pass

    # 2. Isolate profile_exporter DEFAULT_PROFILES_DIR
    try:
        import tools.profile_builder.profile_exporter as pe
        monkeypatch.setattr(pe, "DEFAULT_PROFILES_DIR", test_prof_dir)
    except Exception:
        pass

    # 3. Isolate comment_sync config file
    try:
        import scripts.comment_sync as cs
        monkeypatch.setattr(cs, "_CONFIG_FILE", os.path.join(test_cfg_dir, "comment_sync_config.json"))
    except Exception:
        pass

    # 4. Isolate audio_dsp config file
    try:
        import scripts.audio_dsp as ad
        monkeypatch.setattr(ad, "_CONFIG_FILE", os.path.join(test_cfg_dir, "dsp_config.json"))
    except Exception:
        pass

    yield

