"""Model pinning: scripts/model_pins.json, install markers, and the update checker.

No network: snapshot_download and the HF API are replaced with fakes.
"""

import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_model_updates  # noqa: E402
import download_models  # noqa: E402
import model_pins  # noqa: E402
import model_update_eval  # noqa: E402


def test_every_pin_is_a_full_commit_sha():
    for key, pin in model_pins.load_pins().items():
        assert len(pin["revision"]) == 40 and int(pin["revision"], 16) >= 0, key
        assert pin.get("test") in {"punct_bert", "punct_4class", "translate_en",
                                   "translate_m2m", "faster_whisper", "manual"}, key


def test_runtime_model_ids_are_pinned():
    """Every id the app passes to WhisperModel / from_pretrained resolves to a revision."""
    from speaker_lora import DEFAULT_MODEL_ID
    from whisper_cuda_engine import DEFAULT_MODEL_SIZE, FALLBACK_MODEL_SIZE
    for name in (DEFAULT_MODEL_SIZE, FALLBACK_MODEL_SIZE, DEFAULT_MODEL_ID):
        repo, rev = model_pins.resolve(name)
        assert rev, name
    assert model_pins.resolve("large-v3-turbo")[0] == "dropbox-dash/faster-whisper-large-v3-turbo"


def test_resolve_passes_local_dirs_and_unknown_ids_through(tmp_path):
    assert model_pins.resolve(str(tmp_path)) == (str(tmp_path), None)
    assert model_pins.resolve("someone/unpinned") == ("someone/unpinned", None)


def test_install_state(tmp_path):
    pin = {"revision": "a" * 40, "legacy_ok": True}
    assert model_pins.install_state(pin, str(tmp_path / "nope")) == "missing"
    assert model_pins.install_state(pin, str(tmp_path)) == "legacy"
    assert model_pins.install_state({**pin, "legacy_ok": False}, str(tmp_path)) == "stale"
    model_pins.write_marker(str(tmp_path), "b" * 40)
    assert model_pins.install_state(pin, str(tmp_path)) == "stale"
    model_pins.write_marker(str(tmp_path), "a" * 40)
    assert model_pins.install_state(pin, str(tmp_path)) == "current"


@pytest.fixture
def fake_hub(monkeypatch, tmp_path):
    """models/ in tmp, one pinned repo, and a snapshot_download that records calls."""
    calls = []

    def snapshot_download(repo_id, local_dir, revision=None, **kw):
        calls.append((repo_id, revision))
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, "model.bin"), "w") as f:
            f.write(f"{repo_id}@{revision}")

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        types.SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setattr(download_models, "MODELS_DIR", str(tmp_path))
    pins = {"m": {"repo": "org/m", "revision": "1" * 40, "dest": "m", "legacy_ok": False, "test": "manual"}}
    monkeypatch.setattr(model_pins, "_cache", pins)
    return calls, tmp_path, pins


def test_download_records_revision_and_skips_when_current(fake_hub):
    calls, models, _ = fake_hub
    download_models.download_pinned("m", "m")
    assert calls == [("org/m", "1" * 40)]
    assert model_pins.read_marker(str(models / "m")) == "1" * 40
    download_models.download_pinned("m", "m")
    assert len(calls) == 1


def test_download_replaces_dir_when_pin_moves(fake_hub):
    calls, models, pins = fake_hub
    download_models.download_pinned("m", "m")
    (models / "m" / "stale_file.txt").write_text("old")
    pins["m"]["revision"] = "2" * 40
    download_models.download_pinned("m", "m")
    assert calls[-1] == ("org/m", "2" * 40)
    assert model_pins.read_marker(str(models / "m")) == "2" * 40
    assert not (models / "m" / "stale_file.txt").exists()
    assert not (models / "m.download").exists()


def test_pre_pinning_install_is_adopted_without_refetch(fake_hub):
    calls, models, pins = fake_hub
    pins["m"]["legacy_ok"] = True
    (models / "m").mkdir()
    (models / "m" / "model.bin").write_text("from before pinning")
    download_models.download_pinned("m", "m")
    assert calls == []
    assert model_pins.read_marker(str(models / "m")) == "1" * 40


def test_find_changes_classifies_updates_moves_and_errors(monkeypatch):
    pins = {
        "same": {"repo": "o/same", "revision": "1" * 40},
        "upd": {"repo": "o/upd", "revision": "1" * 40},
        "moved": {"repo": "o/old", "revision": "1" * 40},
        "gone": {"repo": "o/gone", "revision": "1" * 40},
    }
    upstream = {
        "o/same": {"id": "o/same", "sha": "1" * 40},
        "o/upd": {"id": "o/upd", "sha": "2" * 40},
        "o/old": {"id": "n/new", "sha": "1" * 40},
        "o/gone": {"error": "HTTP 404"},
    }
    monkeypatch.setattr(model_pins, "_cache", pins)
    monkeypatch.setattr(check_model_updates, "upstream_info", lambda repo: upstream[repo])
    kinds = {c["key"]: c["kind"] for c in check_model_updates.find_changes()}
    assert kinds == {"upd": "updated", "moved": "moved", "gone": "unreachable"}
    titles = [check_model_updates.issue_title(c) for c in check_model_updates.find_changes()]
    assert "モデル更新: upd 1111111 → 2222222" in titles


def test_punct_f1_and_chrf():
    assert model_update_eval.punct_f1(["あい、う。"], ["あい、う。"]) == 1.0
    assert model_update_eval.punct_f1(["あい、う。"], ["あいう"]) == 0.0
    assert model_update_eval.chrf("hello world", "hello world") == 1.0
    assert model_update_eval.chrf("hello world", "goodbye") < 0.5


def test_pins_json_is_valid_for_powershell_reader():
    """The launcher reads the same file with ConvertFrom-Json: keep it plain JSON."""
    with open(model_pins.PINS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    assert set(data) == {"_doc", "models"}
