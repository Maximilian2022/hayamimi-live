"""Local subtitle overlay server for OBS (Mojicast-style).

Serves an overlay page at http://localhost:<port>/ suitable for an OBS
browser source (transparent background), and streams partial/final subtitle
events over SSE at /events. Designed to be driven by realtime_transcribe.py.
"""
import sys
# Windows Defender の「コントロールされたフォルダー アクセス」(Controlled Folder Access)
# による OneDrive/保護フォルダー配下での __pycache__ 書き込みブロックを根本から防止
sys.dont_write_bytecode = True

# Windows cmd.exe リダイレクト環境での日本語ログ文字化け (CP932) を完全防止
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import http.server
import json
import logging
import os
import queue
import socket
import threading
from typing import Callable

logger = logging.getLogger("hayamimi.subtitle_server")

# リポジトリルートを sys.path に確実に追加 (tools モジュール等の解決用)
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.profile_builder.profile_exporter import is_safe_speaker_id

_MIC_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mic_page.html")
_PROFILE_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile_page.html")
_STYLE_FILENAME = "overlay_style.json"
_CONFIG_FILENAME = "asr_config.json"
_GEMINI_KEY_FILENAME = "gemini_key.txt"

def _get_profile_html() -> str:
    if os.path.exists(_PROFILE_HTML_PATH):
        try:
            with open(_PROFILE_HTML_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return "<html><body><h1>Profile page not found</h1></body></html>"

_generation_task = {
    "status": "idle",
    "progress": 0.0,
    "message": "",
    "done": True,
    "error": None,
    "url": None,
    "speaker_id": None,
    "display_name": None,
    "mode": None,
    "logs": [],
}
_generation_lock = threading.Lock()

def _append_generation_log(level: str, text: str) -> None:
    import datetime
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    with _generation_lock:
        logs = _generation_task.setdefault("logs", [])
        logs.append({"time": now_str, "level": level, "text": text})
        if len(logs) > 300:
            _generation_task["logs"] = logs[-300:]


def _get_gemini_key() -> str:
    storage_dir = _get_storage_dir()
    p = os.path.join(storage_dir, _GEMINI_KEY_FILENAME)
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return os.environ.get("GEMINI_API_KEY", "").strip()

def _save_gemini_key(key: str) -> None:
    storage_dir = _get_storage_dir()
    p = os.path.join(storage_dir, _GEMINI_KEY_FILENAME)
    if not key or not key.strip():
        if os.path.isfile(p):
            try:
                os.remove(p)
            except Exception:
                pass
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    try:
        fd = os.open(p, flags, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(key.strip())  # codeql[py/clear-text-storage-sensitive-data]
    except OSError:
        with open(p, "w", encoding="utf-8") as f:
            f.write(key.strip())  # codeql[py/clear-text-storage-sensitive-data]
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass

def _get_profiles_dir() -> str:
    storage_dir = _get_storage_dir()
    pdir = os.path.join(storage_dir, "profiles")
    os.makedirs(pdir, exist_ok=True)
    return pdir

_CACHED_STORAGE_DIR = None

def _get_storage_dir() -> str:
    """Return a persistent storage directory.
    Priority:
    1. HAYAMIMI_CONFIG_DIR environment variable (used for test isolation / custom config)
    2. /app/config or /app/models (Docker/container environment)
    3. project root config/
    4. system tempdir fallback
    Cached after the first probe to avoid repetitive disk write testing.
    """
    global _CACHED_STORAGE_DIR
    env_dir = os.environ.get("HAYAMIMI_CONFIG_DIR")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir

    if _CACHED_STORAGE_DIR and os.path.isdir(_CACHED_STORAGE_DIR):
        return _CACHED_STORAGE_DIR

    for candidate in ("/app/config", "/app/models"):
        if os.path.isdir(candidate):
            try:
                test_file = os.path.join(candidate, ".write_test")
                with open(test_file, "w") as f:
                    f.write("ok")
                os.remove(test_file)
                _CACHED_STORAGE_DIR = candidate
                return candidate
            except Exception:
                pass
    local_cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
    try:
        os.makedirs(local_cfg, exist_ok=True)
        test_file = os.path.join(local_cfg, ".write_test")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        _CACHED_STORAGE_DIR = local_cfg
        return local_cfg
    except Exception:
        pass
    import tempfile
    _CACHED_STORAGE_DIR = tempfile.gettempdir()
    return _CACHED_STORAGE_DIR


_maintenance_lock = threading.Lock()
_maintenance_status = {
    "scanning": False,
    "last_scan_at": None,
    "pending_count": 0,
    "pending_videos": [],
    "stale_rules_count": 0,
    "error": None,
    "running": False,
    "run_message": "",
    "status": "idle",
    "last_result": None,
}


_synced_session_hits: dict[str, int] = {}

def _sync_stats_to_profile(server, profile=None):
    """Synchronize ASR engine's runtime hit stats into profile incrementally and persist to disk."""
    if server is None or getattr(server, "asr", None) is None:
        return
    prof = profile or getattr(server, "_active_profile", None)
    if not prof:
        return
    stats_fn = getattr(server.asr, "get_replacement_stats", None)
    if not stats_fn:
        return
    stats = stats_fn()
    if not stats:
        return
    changed = False
    for r in prof.replacements:
        if r.pattern in stats:
            st = stats[r.pattern]
            session_hits = int(st.get("hit_count", 0))
            last_synced = _synced_session_hits.get(r.pattern, 0)
            delta = max(0, session_hits - last_synced)
            if delta > 0:
                r.hit_count = (r.hit_count or 0) + delta
                _synced_session_hits[r.pattern] = session_hits
                changed = True
            if st.get("last_hit_at"):
                r.last_hit_at = st["last_hit_at"]
                changed = True
    if changed:
        try:
            from tools.profile_builder.profile_exporter import save_profile
            pdir = _get_profiles_dir()
            save_profile(prof, pdir)
            logger.info("Synchronized runtime hit stats to profile '%s'", prof.speaker_id)
        except Exception as e:
            logger.warning("Failed to save synced stats to profile: %s", e)


def _scan_channels_for_maintenance(server):
    """Scan YouTube channels for new videos across profiles and check for stale rules."""
    with _maintenance_lock:
        if _maintenance_status["scanning"] or _maintenance_status["running"]:
            return
        _maintenance_status["scanning"] = True
        _maintenance_status["error"] = None

    def _worker():
        import datetime
        try:
            from tools.profile_builder.profile_exporter import list_profiles, save_profile
            from tools.profile_builder.youtube_fetcher import YouTubeFetcher
            pdir = _get_profiles_dir()
            profs = list_profiles(pdir)
            fetcher = YouTubeFetcher()
            pending_vids = []
            stale_count = 0

            for prof in profs:
                # Count stale rules (>=180 days with hit_count == 0)
                now = datetime.datetime.now(datetime.timezone.utc)
                cutoff = now - datetime.timedelta(days=180)
                for r in prof.replacements:
                    ts_str = r.last_hit_at or r.added_at
                    if r.hit_count == 0 and ts_str:
                        try:
                            rdt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if rdt < cutoff:
                                stale_count += 1
                        except Exception:
                            pass

                # Auto-infer channel_url from source video if missing
                chan_url = prof.youtube_channel_url
                if not chan_url and prof.source_video_urls:
                    try:
                        meta = fetcher.fetch_metadata(prof.source_video_urls[0])
                        if meta.channel_url:
                            chan_url = meta.channel_url
                            prof.youtube_channel_url = chan_url
                            save_profile(prof, pdir)
                    except Exception:
                        pass

                if chan_url:
                    try:
                        exclude = set(prof.processed_video_ids) | set(prof.skipped_video_ids)
                        skipped_in_scan: list[str] = []
                        # 1. 新作動画 (直近新着) を最大3本取得
                        try:
                            new_vids = fetcher.fetch_channel_videos(
                                channel_url=chan_url,
                                limit=3,
                                candidate_pool=15,
                                exclude_video_ids=exclude,
                                sort_by="recent",
                                filter_collab=getattr(prof, "collab_filter_enabled", True),
                                collab_keywords=getattr(prof, "collab_exclude_keywords", None),
                                skipped_video_ids_out=skipped_in_scan,
                            )
                        except TypeError:
                            new_vids = fetcher.fetch_channel_videos(
                                channel_url=chan_url,
                                limit=3,
                                candidate_pool=15,
                                exclude_video_ids=exclude,
                                sort_by="recent",
                            )

                        # 2. 新作が3本未満の場合、直近半年以内 (180日以内) の未実施アーカイブから合計3本になるよう補充
                        if len(new_vids) < 3:
                            needed_archive = 3 - len(new_vids)
                            already_queued = exclude | {v["id"] for v in new_vids}
                            try:
                                archive_vids = fetcher.fetch_maintenance_candidates(
                                    channel_url=chan_url,
                                    target_count=needed_archive,
                                    max_age_days=180,
                                    exclude_video_ids=already_queued,
                                    candidate_pool=60,
                                    filter_collab=getattr(prof, "collab_filter_enabled", True),
                                    collab_keywords=getattr(prof, "collab_exclude_keywords", None),
                                    skipped_video_ids_out=skipped_in_scan,
                                )
                                new_vids.extend(archive_vids)
                            except Exception as ae:
                                logger.warning("Failed to fetch archive candidates for %s: %s", chan_url, ae)

                        if skipped_in_scan:
                            for svid in skipped_in_scan:
                                if svid not in prof.skipped_video_ids:
                                    prof.skipped_video_ids.append(svid)
                            save_profile(prof, pdir)

                        for nv in new_vids:
                            pending_vids.append({
                                "id": nv["id"],
                                "title": nv["title"],
                                "url": nv["url"],
                                "channel": nv["channel"],
                                "speaker_id": prof.speaker_id,
                                "display_name": prof.display_name,
                                "is_archive": nv.get("is_archive", False),
                            })
                    except Exception as fe:
                        logger.warning("Failed to scan channel %s: %s", chan_url, fe)

            with _maintenance_lock:
                _maintenance_status["pending_videos"] = pending_vids
                _maintenance_status["pending_count"] = len(pending_vids)
                _maintenance_status["stale_rules_count"] = stale_count
                _maintenance_status["last_scan_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            logger.info("Maintenance scan completed: %d pending videos, %d stale rules", len(pending_vids), stale_count)
        except Exception as e:
            logger.error("Maintenance scan failed: %s", e)
            with _maintenance_lock:
                _maintenance_status["error"] = str(e)
        finally:
            with _maintenance_lock:
                _maintenance_status["scanning"] = False

    threading.Thread(target=_worker, daemon=True).start()


def _execute_maintenance(server, log_callback=None):
    """Execute all pending maintenance tasks: learn new videos and archive stale rules."""
    with _maintenance_lock:
        if _maintenance_status["running"]:
            return {"ok": False, "message": "Already running"}
        _maintenance_status["running"] = True
        _maintenance_status["status"] = "running"
        _maintenance_status["run_message"] = "メンテナンスを開始しました..."
        _maintenance_status["last_result"] = None

    import datetime
    from tools.profile_builder.pipeline import CollabVideoSkippedError, ProfileBuilderPipeline
    from tools.profile_builder.profile_exporter import list_profiles, load_profile, save_profile
    pdir = _get_profiles_dir()
    vids_to_process = list(_maintenance_status.get("pending_videos", []))

    try:
        # 1. First sync live stats to active profile
        _sync_stats_to_profile(server)

        # 2. Process pending new videos automatically (continuous growing)
        key = _get_gemini_key()
        learned_videos = 0
        skipped_collab = 0
        if vids_to_process and key:
            pipeline = ProfileBuilderPipeline(api_key=key)
            for idx, v_info in enumerate(vids_to_process):
                kind_label = "未実施アーカイブ" if v_info.get("is_archive") else "新着動画"
                msg = f"{kind_label}を自動学習中 [{idx+1}/{len(vids_to_process)}]: {v_info['title']}"
                if log_callback:
                    log_callback(msg)
                with _maintenance_lock:
                    _maintenance_status["run_message"] = msg
                try:
                    pipeline.run(
                        youtube_url=v_info["url"],
                        speaker_id=v_info["speaker_id"],
                        existing_profile_id=v_info["speaker_id"],
                        duration_sec=720.0,
                    )
                    learned_videos += 1
                except CollabVideoSkippedError as cse:
                    skipped_collab += 1
                    logger.info("Maintenance skipped collab video %s: %s", v_info["url"], cse)
                    try:
                        p_skip = load_profile(v_info["speaker_id"], pdir)
                        if p_skip and v_info.get("id") and v_info["id"] not in p_skip.skipped_video_ids:
                            p_skip.skipped_video_ids.append(v_info["id"])
                            save_profile(p_skip, pdir)
                    except Exception:
                        pass
                except Exception as ve:
                    logger.error("Maintenance video %s failed: %s", v_info["url"], ve)

        # 3. Migrate legacy profiles & tags (schema 1.2.0, one-time automatic execution)
        from tools.profile_builder.youtube_fetcher import extract_hashtags_from_metadata, YouTubeFetcher
        fetcher = None
        profs = list_profiles(pdir)
        for p in profs:
            if p.schema_version != "1.2.0" or any(not getattr(r, "tags", None) for r in p.replacements):
                p.migrate_from_legacy()
                for r in p.replacements:
                    if r.source_video_id and (not r.tags or r.tags == ["共通"]):
                        try:
                            if fetcher is None:
                                fetcher = YouTubeFetcher()
                            meta = fetcher.fetch_metadata(f"https://www.youtube.com/watch?v={r.source_video_id}")
                            if meta:
                                tags = extract_hashtags_from_metadata(meta)
                                if tags:
                                    r.tags = tags
                        except Exception:
                            pass
                save_profile(p, pdir)

        # 4. Archive stale rules (180 days / 6 months) across profiles
        archived_total = 0
        for p in profs:
            cnt = p.archive_stale_rules(days=180)
            if cnt > 0:
                archived_total += cnt
                save_profile(p, pdir)
                if server and getattr(server, "_active_profile", None) and server._active_profile.speaker_id == p.speaker_id:
                    server._active_profile = p
                    active_tags = getattr(server, "_active_profile_tags", None)
                    if server.asr:
                        mapping = p.get_active_replacements(active_tags)
                        server.asr.set_replacements(mapping)

        final_msg = f"メンテナンス完了: {learned_videos}本の新着動画を学習、{archived_total}件の古いルールを休眠整理しました。"
        with _maintenance_lock:
            _maintenance_status["pending_videos"] = []
            _maintenance_status["pending_count"] = 0
            _maintenance_status["stale_rules_count"] = 0
            _maintenance_status["run_message"] = final_msg
            _maintenance_status["status"] = "completed" 

        logger.info(final_msg)
        return {
            "ok": True,
            "learned_videos": learned_videos,
            "archived_rules": archived_total,
            "message": final_msg,
        }
    except Exception as e:
        logger.error("Maintenance execution failed: %s", e)
        with _maintenance_lock:
            _maintenance_status["run_message"] = f"メンテナンス失敗: {e}"
            _maintenance_status["status"] = "error"
            _maintenance_status["error"] = str(e)
        return {"ok": False, "error": str(e)}
    finally:
        with _maintenance_lock:
            _maintenance_status["running"] = False

_DEFAULT_STYLE = {
    "mode": "unified",  # "unified" (全体一括) or "split" (メイン/翻訳個別)
    "align": "center",
    "bottom": 4,
    "bgColor": "#000000",
    "bgOpacity": 0,
    "radius": 8,
    "showTrans": True,
    # 一括設定 (unified) 用パラメータ
    "font": "'Zen Kaku Gothic New', sans-serif",
    "size": 42,
    "color": "#ffffff",
    "strokeColor": "#000000",
    "strokeWidth": 3,
    "effect": "stroke",  # "stroke" | "double" | "glow"
    # 個別設定 (split) 時のメイン字幕
    "main": {
        "font": "'Zen Kaku Gothic New', sans-serif",
        "size": 42,
        "color": "#ffffff",
        "strokeColor": "#000000",
        "strokeWidth": 3,
        "effect": "stroke",
    },
    # 個別設定 (split) 時の翻訳字幕（共通）
    "trans": {
        "font": "'Zen Kaku Gothic New', sans-serif",
        "size": 32,
        "color": "#ffd166",
        "strokeColor": "#000000",
        "strokeWidth": 2.5,
        "effect": "stroke",
    },
    # 言語別オーバーライド（オプション）
    "langOverrides": {
        "en": {"color": "#70d6ff"},
        "zh": {"color": "#ff9770"},
        "zh-tw": {"color": "#ff9770"},
        "zh-hk": {"color": "#ff9770"},
        "ko": {"color": "#a8dadc"},
    },
}

def _deep_merge_dict(base: dict, update: dict) -> dict:
    """Recursively merge update dict into a deepcopy of base dict."""
    import copy
    res = copy.deepcopy(base)
    for k, v in update.items():
        if k in res and isinstance(res[k], dict) and isinstance(v, dict):
            res[k] = _deep_merge_dict(res[k], v)
        else:
            res[k] = copy.deepcopy(v)
    return res


def _sanitize_overlay_style(style: dict) -> dict:
    """Sanitize and clamp overlay style settings within safe boundaries."""
    import copy
    if not isinstance(style, dict):
        return copy.deepcopy(_DEFAULT_STYLE)
    s = copy.deepcopy(style)

    def _clamp_num(val, low, high, default):
        try:
            num = float(val)
            return max(low, min(high, num))
        except (ValueError, TypeError):
            return default

    if "size" in s:
        s["size"] = int(_clamp_num(s["size"], 8, 200, 42))
    if "strokeWidth" in s:
        s["strokeWidth"] = round(_clamp_num(s["strokeWidth"], 0.0, 30.0, 3.0), 1)
    if "bottom" in s:
        s["bottom"] = round(_clamp_num(s["bottom"], 0.0, 100.0, 4.0), 1)
    if "bgOpacity" in s:
        # bgOpacity is percentage (0% to 100%)
        s["bgOpacity"] = round(_clamp_num(s["bgOpacity"], 0.0, 100.0, 0.0), 1)
    if "radius" in s:
        s["radius"] = int(_clamp_num(s["radius"], 0, 50, 8))
    if "mode" in s and s["mode"] not in ("unified", "split"):
        s["mode"] = "unified"
    if "align" in s and s["align"] not in ("left", "center", "right"):
        s["align"] = "center"
    if "effect" in s and s["effect"] not in ("stroke", "double", "glow"):
        s["effect"] = "stroke"

    for sub in ("main", "trans"):
        if sub in s and isinstance(s[sub], dict):
            sub_d = s[sub]
            if "size" in sub_d:
                sub_d["size"] = int(_clamp_num(sub_d["size"], 8, 200, 42 if sub == "main" else 32))
            if "strokeWidth" in sub_d:
                sub_d["strokeWidth"] = round(_clamp_num(sub_d["strokeWidth"], 0.0, 30.0, 3.0 if sub == "main" else 2.5), 1)

    # Sanitize langOverrides (M-5: prevent giant strokeWidth crashing OBS via (2w+1)^2 shadows)
    if "langOverrides" in s and isinstance(s["langOverrides"], dict):
        for lang_code, lang_d in list(s["langOverrides"].items()):
            if not isinstance(lang_d, dict):
                continue
            if "size" in lang_d:
                lang_d["size"] = int(_clamp_num(lang_d["size"], 8, 200, 32))
            if "strokeWidth" in lang_d:
                lang_d["strokeWidth"] = round(_clamp_num(lang_d["strokeWidth"], 0.0, 30.0, 2.5), 1)
            if "effect" in sub_d and sub_d["effect"] not in ("stroke", "double", "glow"):
                sub_d["effect"] = "stroke"

    return s

def _get_overlay_style() -> dict:
    storage_dir = _get_storage_dir()
    path = os.path.join(storage_dir, _STYLE_FILENAME)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
                return _deep_merge_dict(_DEFAULT_STYLE, saved)
        except Exception:
            pass
    import tempfile
    temp_style = os.path.join(tempfile.gettempdir(), _STYLE_FILENAME)
    if os.path.exists(temp_style):
        try:
            with open(temp_style, "r", encoding="utf-8") as f:
                saved = json.load(f)
                return _deep_merge_dict(_DEFAULT_STYLE, saved)
        except Exception:
            pass
    return _deep_merge_dict(_DEFAULT_STYLE, {})

def _save_overlay_style(style: dict) -> None:
    try:
        from scripts.config_store import save_overlay_style
        save_overlay_style(style)
    except Exception:
        storage_dir = _get_storage_dir()
        path = os.path.join(storage_dir, _STYLE_FILENAME)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(style, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            sys.stderr.write(f"[subtitle_server] overlay style save failed ({path}): {exc}\n")

def _get_saved_asr_config() -> dict:
    try:
        from scripts.config_store import get_saved_asr_config
        return get_saved_asr_config()
    except Exception:
        storage_dir = _get_storage_dir()
        path = os.path.join(storage_dir, _CONFIG_FILENAME)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

def _save_asr_config(cfg: dict) -> None:
    try:
        from scripts.config_store import save_asr_config
        save_asr_config(cfg)
    except Exception:
        storage_dir = _get_storage_dir()
        path = os.path.join(storage_dir, _CONFIG_FILENAME)
        try:
            existing = _get_saved_asr_config()
            save_keys = ("lang", "translate", "punctuate", "dual_confirm", "lid_switch_confirm", "min_switch_s", "vad")
            for k in save_keys:
                if k in cfg:
                    existing[k] = cfg[k]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            sys.stderr.write(f"[subtitle_server] asr config save failed ({path}): {exc}\n")

def _get_saved_obs_config() -> dict:
    try:
        from scripts.config_store import get_saved_obs_config
        return get_saved_obs_config()
    except Exception:
        return {
            "enabled": True,
            "url": "ws://127.0.0.1:4455",
            "password": "",
            "target_source": "",
        }

def _save_obs_config(cfg: dict) -> dict:
    try:
        from scripts.config_store import save_obs_config
        return save_obs_config(cfg)
    except Exception:
        return cfg

def _save_active_speaker_id(speaker_id) -> None:
    try:
        from scripts.config_store import save_active_speaker_id
        save_active_speaker_id(speaker_id)
    except Exception:
        pass

def _get_saved_active_speaker_id():
    try:
        from scripts.config_store import get_saved_active_speaker_id
        return get_saved_active_speaker_id()
    except Exception:
        return None

_MIC_HTML_CACHE = {"mtime": 0.0, "content": ""}

def _get_mic_html() -> str:
    if os.path.exists(_MIC_HTML_PATH):
        try:
            mtime = os.path.getmtime(_MIC_HTML_PATH)
            if mtime != _MIC_HTML_CACHE["mtime"] or not _MIC_HTML_CACHE["content"]:
                with open(_MIC_HTML_PATH, "r", encoding="utf-8") as f:
                    _MIC_HTML_CACHE["content"] = f.read()
                _MIC_HTML_CACHE["mtime"] = mtime
            return _MIC_HTML_CACHE["content"]
        except OSError:
            pass
    return "<!doctype html><title>Mic</title><p>mic_page.html not found</p>"

import ipaddress
import re
import urllib.parse

def is_safe_host_or_ip(host_str: str) -> bool:
    """Validate that host_str is a legitimate loopback, localhost, or private RFC1918 IPv4/IPv6 literal.
    Rejects attacker domains like '10.evil.example' or '192.168.attacker.com'.
    """
    if not host_str or not isinstance(host_str, str):
        return False
    h = host_str.strip().lower().strip("[]")
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def _get_safe_origin(origin: str | None, host_header: str | None = None) -> str | None:
    """Validate and sanitize Origin header against CRLF injection and untrusted hosts.

    Returns reconstructed safe origin string ('scheme://hostname[:port]') if valid, else None.
    """
    if not origin:
        return None
    # Block CRLF injection attempts
    if "\r" in origin or "\n" in origin or "\0" in origin:
        return None
    try:
        parsed = urllib.parse.urlparse(origin)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        ohost = (parsed.hostname or "").lower()
        if not ohost:
            return None

        if is_safe_host_or_ip(ohost):
            port_suffix = f":{parsed.port}" if parsed.port else ""
            fmt_host = f"[{ohost}]" if ":" in ohost and not ohost.startswith("[") else ohost
            res = f"{scheme}://{fmt_host}{port_suffix}"
            return re.sub(r"[\r\n\0]", "", res)
    except Exception:
        pass
    return None

def _is_safe_origin(origin: str | None, host_header: str | None) -> bool:
    if not origin:
        return True
    return _get_safe_origin(origin, host_header) is not None

OVERLAY_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>Hayamimi Live - OBS 字幕オーバーレイ</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=Bungee&family=Dela+Gothic+One&family=Do+Hyeon&family=DotGothic16&family=Fredoka:wght@500;700&family=Hachi+Maru+Pop&family=IBM+Plex+Sans+JP:wght@500;700&family=IBM+Plex+Sans+KR:wght@500;700&family=IBM+Plex+Sans:wght@500;700&family=Jua&family=Kaisei+Decol:wght@700&family=Kiwi+Maru:wght@500&family=Kosugi+Maru&family=LINE+Seed+JP:wght@400;700;800&family=M+PLUS+1p:wght@500;700&family=M+PLUS+Rounded+1c:wght@500;700;800&family=Ma+Shan+Zheng&family=Montserrat:wght@500;700;800&family=Noto+Sans+JP:wght@500;700&family=Noto+Sans+KR:wght@500;700&family=Noto+Sans+SC:wght@500;700&family=Noto+Sans+TC:wght@500;700&family=Oswald:wght@500;700&family=Rampart+One&family=Reggae+One&family=Righteous&family=Shippori+Mincho+B1:wght@600;800&family=Yuji+Boku&family=ZCOOL+KuaiLe&family=Zen+Kaku+Gothic+New:wght@500;700&display=swap" rel="stylesheet">
<style>
  @font-face {
    font-family: 'LINE Seed KR';
    src: local('LINE Seed KR Bold'), local('LINE Seed KR-Bold'), local('LINESeedKR-Bd'),
         url('https://cdn.jsdelivr.net/gh/projectnoonnu/noonfonts_11-01@1.0/LINESeedKR-Bd.woff2') format('woff2');
    font-weight: 700;
    font-display: swap;
  }
  @font-face {
    font-family: 'LINE Seed KR';
    src: local('LINE Seed KR Regular'), local('LINE Seed KR-Regular'), local('LINESeedKR-Rg'),
         url('https://cdn.jsdelivr.net/gh/projectnoonnu/noonfonts_11-01@1.0/LINESeedKR-Rg.woff2') format('woff2');
    font-weight: 400;
    font-display: swap;
  }

  html, body { margin: 0; background: transparent; overflow: hidden; width: 100%; height: 100%; }
  #box {
    position: absolute; left: 0; right: 0; bottom: 4vh;
    text-align: center;
    font-family: 'Zen Kaku Gothic New', sans-serif;
    font-size: 5vh; line-height: 1.4; color: #fff;
    text-shadow: 0 0 8px #000, 0 0 4px #000, 2px 2px 2px #000;
    transition: all 0.2s ease;
    box-sizing: border-box;
    padding: 0 2vw;
  }
  #final-line, #partial-line { display: block; min-height: 1.4em; }
  #partial-line { opacity: 0.75; font-style: italic; font-size: 0.85em; }
  #trans-line { display: block; opacity: 0.85; font-size: 0.75em; margin-top: 0.2em; min-height: 1.2em; }
</style>
</head>
<body>
<div id="box">
  <span id="final-line"></span>
  <span id="partial-line"></span>
  <span id="trans-line"></span>
</div>
<script>
  const mode = new URLSearchParams(location.search).get("show") || "both";
  const box = document.getElementById("box");
  const fin = document.getElementById("final-line");
  const par = document.getElementById("partial-line");
  const trans = document.getElementById("trans-line");
  if (mode === "final") par.style.display = "none";
  if (mode === "partial") { fin.style.display = "none"; trans.style.display = "none"; }

  function hexToRgba(hex, alphaPercent) {
    hex = (hex || "#000000").replace("#", "");
    if (hex.length === 3) hex = hex.split("").map(c => c+c).join("");
    const r = parseInt(hex.substring(0, 2), 16) || 0;
    const g = parseInt(hex.substring(2, 4), 16) || 0;
    const b = parseInt(hex.substring(4, 6), 16) || 0;
    return `rgba(${r}, ${g}, ${b}, ${(alphaPercent || 0) / 100})`;
  }

  function generateTextShadow(color, width, effect) {
    if (width <= 0) return "none";
    if (effect === "glow") {
      return `0 0 ${width * 2}px ${color}, 0 0 ${width * 4}px ${color}, 0 0 ${width * 7}px ${color}`;
    }
    if (effect === "double") {
      let s = [];
      for (let x = -width; x <= width; x++) {
        for (let y = -width; y <= width; y++) {
          if (x !== 0 || y !== 0) s.push(`${x}px ${y}px 0 #000000`);
        }
      }
      const w2 = width + 2;
      for (let x = -w2; x <= w2; x += 2) {
        for (let y = -w2; y <= w2; y += 2) {
          s.push(`${x}px ${y}px 0 ${color}`);
        }
      }
      s.push(`0 0 ${w2 * 2}px ${color}`);
      return s.join(", ");
    }
    let s = [];
    for (let x = -width; x <= width; x++) {
      for (let y = -width; y <= width; y++) {
        if (x !== 0 || y !== 0) s.push(`${x}px ${y}px 0 ${color}`);
      }
    }
    s.push(`0 0 ${width * 2}px ${color}`);
    return s.join(", ");
  }

  let currentConfig = null;

  function applyStyle(s) {
    if (!s) return;
    currentConfig = s;

    // 全体コンテナ配置 & 背景
    if (s.align) box.style.textAlign = s.align;
    if (s.bottom != null) box.style.bottom = s.bottom + "vh";
    if (s.bgColor && s.bgOpacity != null) {
      box.style.backgroundColor = hexToRgba(s.bgColor, parseInt(s.bgOpacity));
      box.style.padding = parseInt(s.bgOpacity) > 0 ? "0.6rem 1.2rem" : "0 2vw";
    }
    if (s.radius != null) box.style.borderRadius = s.radius + "px";
    if (s.showTrans != null && mode !== "partial") {
      trans.style.display = s.showTrans ? "block" : "none";
    }

    const isSplit = s.mode === "split";
    const mainCfg = isSplit && s.main ? s.main : s;
    const transCfg = isSplit && s.trans ? s.trans : s;

    // メイン字幕 (確定 & ドラフト)
    fin.style.fontFamily = mainCfg.font || s.font || "'Zen Kaku Gothic New', sans-serif";
    fin.style.fontSize = (mainCfg.size || s.size || 42) + "px";
    fin.style.color = mainCfg.color || s.color || "#ffffff";
    fin.style.textShadow = generateTextShadow(
      mainCfg.strokeColor || s.strokeColor || "#000000",
      parseInt(mainCfg.strokeWidth != null ? mainCfg.strokeWidth : (s.strokeWidth != null ? s.strokeWidth : 3)),
      mainCfg.effect || s.effect || "stroke"
    );

    par.style.fontFamily = fin.style.fontFamily;
    par.style.fontSize = ((mainCfg.size || s.size || 42) * 0.85) + "px";
    par.style.color = fin.style.color;
    par.style.textShadow = fin.style.textShadow;

    // 翻訳字幕コンテナの基本スタイル
    trans.style.fontFamily = transCfg.font || s.font || "'Zen Kaku Gothic New', sans-serif";
    trans.style.fontSize = (isSplit ? (transCfg.size || 32) : ((s.size || 42) * 0.75)) + "px";
    trans.style.color = transCfg.color || (isSplit ? "#ffd166" : s.color);
    trans.style.textShadow = generateTextShadow(
      transCfg.strokeColor || s.strokeColor || "#000000",
      parseInt(transCfg.strokeWidth != null ? transCfg.strokeWidth : (s.strokeWidth != null ? s.strokeWidth : 2.5)),
      transCfg.effect || s.effect || "stroke"
    );

    // 既存翻訳行のスタイル更新
    for (const child of trans.children) {
      applyTransLineStyle(child);
    }
  }

  function applyTransLineStyle(el) {
    if (!currentConfig) return;
    const lang = el.dataset.lang || "";
    const isSplit = currentConfig.mode === "split";
    const transCfg = isSplit && currentConfig.trans ? currentConfig.trans : currentConfig;
    const overrides = (isSplit && currentConfig.langOverrides && currentConfig.langOverrides[lang]) ? currentConfig.langOverrides[lang] : {};

    el.style.fontFamily = overrides.font || transCfg.font || currentConfig.font || "'Zen Kaku Gothic New', sans-serif";
    if (overrides.size != null) el.style.fontSize = overrides.size + "px";
    el.style.color = overrides.color || transCfg.color || (isSplit ? "#ffd166" : currentConfig.color);
    const strokeColor = overrides.strokeColor || transCfg.strokeColor || currentConfig.strokeColor || "#000000";
    const strokeWidth = overrides.strokeWidth != null ? overrides.strokeWidth : (transCfg.strokeWidth != null ? transCfg.strokeWidth : currentConfig.strokeWidth);
    const effect = overrides.effect || transCfg.effect || currentConfig.effect || "stroke";
    el.style.textShadow = generateTextShadow(strokeColor, parseInt(strokeWidth != null ? strokeWidth : 2.5), effect);
  }

  let clearTimer = null;
  let es = null;
  let heartbeatTimer = null;
  let reconnectTimer = null;
  let retryCount = 0;

  function resetHeartbeat() {
    if (heartbeatTimer) clearTimeout(heartbeatTimer);
    // サーバーは15秒ごとに keepalive を送るため、25秒間無音・無通信なら再接続
    heartbeatTimer = setTimeout(() => {
      connectEventSource();
    }, 25000);
  }

  function connectEventSource() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (es) {
      try { es.close(); } catch (_) {}
      es = null;
    }
    resetHeartbeat();

    try {
      es = new EventSource("/events?_t=" + Date.now());
    } catch (_) {
      scheduleReconnect();
      return;
    }

    es.onopen = () => {
      retryCount = 0;
      resetHeartbeat();
      fetch("/overlay-style?_t=" + Date.now()).then(r => r.json()).then(applyStyle).catch(() => {});
    };

    es.onmessage = (e) => {
      resetHeartbeat();
      let ev;
      try {
        ev = JSON.parse(e.data);
      } catch (_) {
        return;
      }
      if (ev.type === "style_update") {
        applyStyle(ev.style);
        return;
      }
      if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; }
      if (ev.type === "partial") {
        par.textContent = ev.text;
      } else if (ev.type === "final") {
        fin.textContent = ev.text;
        par.textContent = "";
        trans.innerHTML = "";
      } else if (ev.type === "translation") {
        const line = document.createElement("div");
        line.dataset.lang = ev.lang;
        line.textContent = `→ [${ev.lang}] ${ev.text}`;
        applyTransLineStyle(line);
        trans.appendChild(line);
      }
      clearTimer = setTimeout(() => {
        fin.textContent = "";
        trans.innerHTML = "";
      }, 6000);
    };

    es.onerror = () => {
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    if (es) {
      try { es.close(); } catch (_) {}
      es = null;
    }
    retryCount++;
    const delay = Math.min(1000 * Math.pow(1.5, retryCount - 1), 5000);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectEventSource();
    }, delay);
  }

  // シーン復帰時（OBSでシーンがアクティブになった時）の即時ヘルスチェック
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      if (!es || es.readyState !== 1) { // 1 = OPEN
        connectEventSource();
      }
    }
  });

  // 初回スタイル取得 & SSE接続
  fetch("/overlay-style?_t=" + Date.now()).then(r => r.json()).then(applyStyle).catch(() => {});
  connectEventSource();
</script>
</body>
</html>
"""


TRANSCRIPT_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Transcript</title>
<style>
  body { margin: 0; padding: 2rem; background: #111; color: #eee;
         font-family: "Yu Gothic UI", "Meiryo", sans-serif; font-size: 1.1rem; line-height: 1.8; }
  #lines p { margin: 0.2rem 0; }
  .lang { color: #888; font-size: 0.8em; margin-right: 0.6em; }
</style>
<div id="lines"></div>
<script>
  const box = document.getElementById("lines");
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type !== "refine") return;
    const p = document.createElement("p");
    const langSpan = document.createElement("span");
    langSpan.className = "lang";
    langSpan.textContent = "[" + (ev.lang || "?") + "] ";
    p.appendChild(langSpan);
    p.appendChild(document.createTextNode(ev.text));
    box.appendChild(p);
    while (box.children.length > 500) { box.removeChild(box.firstChild); }
    window.scrollTo(0, document.body.scrollHeight);
  };
</script>
"""


DASHBOARD_HTML = '<!doctype html>\n<html lang="ja">\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>hayamimi</title>\n<link rel="preconnect" href="https://fonts.googleapis.com">\n<link href="https://fonts.googleapis.com/css2?family=Shippori+Mincho+B1:wght@600;800&family=Zen+Kaku+Gothic+New:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">\n<style>\n  :root {\n    --ink: #0d0f13; --ink-2: #14171d; --line: #2a2f3a;\n    --paper: #ece5d8; --paper-dim: #b0a898;\n    --shu: #e04f2f;\n    --c-ja: #e04f2f; --c-en: #5b7fd4; --c-zh: #d4a13c; --c-ko: #3fae9d; --c-yue: #b06bc6; --c-xx: #7a8194;\n    --mono: "IBM Plex Mono", monospace;\n  }\n  * { box-sizing: border-box; }\n  html, body { height: 100%; }\n  body {\n    margin: 0; background: var(--ink); color: var(--paper);\n    font-family: "Zen Kaku Gothic New", "Yu Gothic UI", sans-serif;\n    background-image:\n      radial-gradient(1200px 500px at 85% -10%, rgba(224,79,47,.07), transparent 60%),\n      repeating-linear-gradient(0deg, transparent 0 2px, rgba(255,255,255,.006) 2px 4px);\n    display: flex; flex-direction: column; overflow: hidden;\n  }\n  header {\n    display: flex; align-items: baseline; gap: 1.1rem;\n    padding: .9rem 1.6rem .7rem; border-bottom: 1px solid var(--line);\n    flex: none;\n  }\n  .logotype { font-family: "Shippori Mincho B1", serif; font-weight: 800;\n    font-size: 1.7rem; letter-spacing: .12em; }\n  .logotype em { font-style: normal; color: var(--shu); }\n  .romaji { font-family: var(--mono); font-size: .72rem; letter-spacing: .3em;\n    color: var(--paper-dim); }\n  .spacer { flex: 1; }\n  .chip { font-family: var(--mono); font-size: .72rem; color: var(--paper-dim);\n    border: 1px solid var(--line); border-radius: 3px; padding: .25rem .6rem; }\n  .chip b { color: var(--paper); font-weight: 600; }\n  #live-dot { width: .55rem; height: .55rem; border-radius: 50%;\n    background: var(--c-xx); align-self: center; }\n  #live-dot.on { background: var(--shu);\n    animation: pulse 1.6s ease-out infinite; }\n  @keyframes pulse {\n    from { box-shadow: 0 0 0 0 rgba(224,79,47,.5); }\n    to { box-shadow: 0 0 0 10px rgba(224,79,47,0); } }\n\n  #partial-strip {\n    flex: none; padding: 1.2rem 1.6rem 1.1rem; min-height: 5.6rem;\n    border-bottom: 1px solid var(--line); background: var(--ink-2);\n    position: relative; overflow: hidden;\n  }\n  #partial-strip::before {\n    content: "いま聞き取り中"; position: absolute; top: .55rem; left: 1.65rem;\n    font-size: .62rem; letter-spacing: .35em; color: var(--paper-dim);\n  }\n  #partial-text {\n    font-family: "Shippori Mincho B1", serif; font-weight: 600;\n    font-size: 1.5rem; line-height: 1.5; margin-top: 1rem; min-height: 2.2rem;\n  }\n  #partial-text.idle { color: var(--paper-dim); opacity: .5; }\n  .caret { display: inline-block; width: .5em; height: 1.05em;\n    background: var(--shu); vertical-align: -0.12em; margin-left: .18em;\n    animation: blink 1s steps(2) infinite; }\n  @keyframes blink { 50% { opacity: 0; } }\n\n  main { flex: 1; display: grid; grid-template-columns: minmax(0, 7fr) minmax(0, 5fr);\n    min-height: 0; }\n  section { display: flex; flex-direction: column; min-height: 0; }\n  section + section { border-left: 1px solid var(--line); }\n  h2 { flex: none; margin: 0; padding: .7rem 1.6rem; font-size: .68rem;\n    font-weight: 700; letter-spacing: .35em; color: var(--paper-dim);\n    border-bottom: 1px solid var(--line); }\n  h2 span { color: var(--shu); }\n  .scroll { flex: 1; overflow-y: auto; padding: 1rem 1.6rem 2rem;\n    scrollbar-width: thin; scrollbar-color: var(--line) transparent; }\n\n  .final-card {\n    display: grid; grid-template-columns: auto 1fr auto; gap: .7rem;\n    align-items: baseline; padding: .55rem 0;\n    border-bottom: 1px dashed rgba(42,47,58,.7);\n    animation: rise .28s cubic-bezier(.2,.9,.3,1) both;\n  }\n  @keyframes rise { from { opacity: 0; transform: translateY(8px); } }\n  .badges { display: flex; gap: .35rem; align-items: center; }\n  .lang-badge { font-family: var(--mono); font-size: .62rem; font-weight: 600;\n    padding: .12rem .42rem; border-radius: 2px; color: var(--ink);\n    letter-spacing: .08em; }\n  .spk { font-family: var(--mono); font-size: .62rem; color: var(--paper-dim);\n    border: 1px solid var(--line); padding: .1rem .34rem; border-radius: 2px; }\n  .final-text { font-size: 1.02rem; line-height: 1.65; }\n  .lat { font-family: var(--mono); font-size: .66rem; color: var(--paper-dim); }\n  .trans-line { grid-column: 2; font-size: .85rem; color: var(--paper-dim);\n    line-height: 1.5; }\n  .trans-line b { font-family: var(--mono); font-size: .6rem; font-weight: 600;\n    color: var(--shu); margin-right: .45em; letter-spacing: .1em; }\n\n  .refine-p { margin: 0 0 1rem; font-family: "Shippori Mincho B1", serif;\n    font-weight: 600; font-size: 1.02rem; line-height: 1.9;\n    animation: rise .3s ease both; }\n  .refine-p .meta { font-family: var(--mono); font-size: .62rem; font-weight: 400;\n    color: var(--paper-dim); display: block; margin-bottom: .18rem;\n    letter-spacing: .12em; }\n  .refine-p .meta i { font-style: normal; color: var(--shu); }\n\n  footer { flex: none; display: flex; gap: 1.4rem; padding: .5rem 1.6rem;\n    border-top: 1px solid var(--line); font-family: var(--mono);\n    font-size: .66rem; color: var(--paper-dim); }\n  @media (max-width: 860px) { main { grid-template-columns: 1fr; }\n    section + section { border-left: 0; border-top: 1px solid var(--line); } }\n</style>\n<body>\n<header>\n  <div id="live-dot"></div>\n  <div class="logotype">早<em>耳</em></div>\n  <div class="romaji">hayamimi</div>\n  <div class="spacer"></div>\n  <div class="chip">確定 <b id="n-finals">0</b></div>\n  <div class="chip">平均応答 <b id="mean-lat">&ndash;</b> ms</div>\n  <div class="chip">言語 <b id="langs-seen">&ndash;</b></div>\n</header>\n\n<div id="partial-strip">\n  <div id="partial-text" class="idle">マイクの音声を待っています<span class="caret"></span></div>\n</div>\n\n<main>\n  <section>\n    <h2>確定フィード <span>LIVE</span></h2>\n    <div class="scroll" id="feed"></div>\n  </section>\n  <section>\n    <h2>清書 <span>REFINED</span></h2>\n    <div class="scroll" id="refined"></div>\n  </section>\n</main>\n\n<footer>\n  <div id="conn">connecting&hellip;</div>\n  <div>overlay: /</div>\n  <div>transcript: /transcript</div>\n</footer>\n\n<script>\n  const LANG_COLOR = { ja: "var(--c-ja)", en: "var(--c-en)", zh: "var(--c-zh)",\n                       ko: "var(--c-ko)", yue: "var(--c-yue)" };\n  const feed = document.getElementById("feed");\n  const refined = document.getElementById("refined");\n  const partial = document.getElementById("partial-text");\n  const dot = document.getElementById("live-dot");\n  let finals = 0, latSum = 0, langs = new Set(), lastCard = null, idleTimer = null;\n\n  function badge(lang) {\n    const b = document.createElement("span");\n    b.className = "lang-badge";\n    b.style.background = LANG_COLOR[lang] || "var(--c-xx)";\n    b.textContent = lang || "??";\n    return b;\n  }\n  function stick(el) { el.scrollTop = el.scrollHeight; }\n\n  const es = new EventSource("/events");\n  es.onopen = () => { document.getElementById("conn").textContent = "connected"; dot.classList.add("on"); };\n  es.onerror = () => { document.getElementById("conn").textContent = "reconnecting…"; dot.classList.remove("on"); };\n  es.onmessage = (e) => {\n    const ev = JSON.parse(e.data);\n    if (ev.type === "partial") {\n      partial.classList.remove("idle");\n      partial.textContent = ev.text;\n      const c = document.createElement("span"); c.className = "caret";\n      partial.appendChild(c);\n      clearTimeout(idleTimer);\n      idleTimer = setTimeout(() => partial.classList.add("idle"), 4000);\n    } else if (ev.type === "final") {\n      const card = document.createElement("div"); card.className = "final-card";\n      const badges = document.createElement("div"); badges.className = "badges";\n      badges.appendChild(badge(ev.lang));\n      if (ev.speaker) { const s = document.createElement("span");\n        s.className = "spk"; s.textContent = ev.speaker; badges.appendChild(s); }\n      const t = document.createElement("div"); t.className = "final-text";\n      t.textContent = ev.text;\n      const lat = document.createElement("div"); lat.className = "lat";\n      lat.textContent = ev.latency_ms != null ? Math.round(ev.latency_ms) + "ms" : "";\n      card.append(badges, t, lat);\n      feed.appendChild(card); lastCard = card; stick(feed);\n      while (feed.children.length > 300) { feed.removeChild(feed.firstChild); }\n      finals++; langs.add(ev.lang);\n      if (ev.latency_ms != null) latSum += ev.latency_ms;\n      document.getElementById("n-finals").textContent = finals;\n      document.getElementById("mean-lat").textContent = Math.round(latSum / finals);\n      document.getElementById("langs-seen").textContent = [...langs].join(" ");\n    } else if (ev.type === "translation") {\n      if (!lastCard) return;\n      const tr = document.createElement("div"); tr.className = "trans-line";\n      const b = document.createElement("b"); b.textContent = "→" + ev.lang;\n      tr.appendChild(b); tr.appendChild(document.createTextNode(ev.text));\n      lastCard.appendChild(tr); stick(feed);\n    } else if (ev.type === "refine") {\n      const p = document.createElement("p"); p.className = "refine-p";\n      const meta = document.createElement("span"); meta.className = "meta";\n      const spk = ev.speaker ? ev.speaker + " · " : "";\n      if (ev.speaker) { meta.appendChild(document.createTextNode(ev.speaker + " · ")); } const itag = document.createElement("i"); itag.textContent = ev.lang || "?"; meta.appendChild(itag);\n      p.appendChild(meta); p.appendChild(document.createTextNode(ev.text));\n      refined.appendChild(p); stick(refined);\n      while (refined.children.length > 200) { refined.removeChild(refined.firstChild); }\n    }\n  };\n</script>\n</body>\n</html>\n'


# cap on a POST body for the live-dictionary endpoints below: these are
# small hand-edited JSON dicts, not file uploads, so anything past this is
# almost certainly a mistake (or worth rejecting rather than blocking on).
_MAX_DICT_BODY_BYTES = 1_000_000


EVENT_QUEUE_MAXSIZE = 500


class EventHub:
    """Pub/sub core for structured session events (GitHub issue #29).

    Split out of SubtitleServer so every structured event realtime_
    transcribe.py's pipeline produces (partial/final/translation/refine/
    model_load/model_fallback/warning/session_summary/recluster/
    session_reset) is available to an app embedding the engine whether or
    not `--serve`'s HTTP/SSE layer exists: realtime_transcribe.main()
    always constructs one of these, and SubtitleServer optionally fronts
    it with an HTTP server on top (see that class below).

    Two independent ways to consume events:
      * subscribe()/unsubscribe() -- pull a queue.Queue of JSON strings,
        one per event, in publish order. This is what the /events SSE
        handler and ws_ingest.py's WebSocket mirror use.
      * add_listener() -- push a plain Python dict to a synchronous
        in-process callback, no serialization round-trip. This is what an
        embedding app that links against this module directly (rather
        than talking HTTP) should use.
    """

    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._listeners: list[Callable[[dict], None]] = []
        self._refines: list[str] = []  # recent refine events, replayed to new clients
        self._lock = threading.Lock()
        self._listener_errored: set[int] = set()  # id(callback) already reported to stderr

    def publish(self, event: dict) -> None:
        data = json.dumps(event, ensure_ascii=False)
        with self._lock:
            if event.get("type") == "refine":
                self._refines.append(data)
                del self._refines[:-200]
            clients = list(self._clients)
            listeners = list(self._listeners)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass
        # Listener callbacks run AFTER the lock is released (and over a
        # snapshot of the listener list) so a slow, re-entrant, or
        # exception-raising listener can never block publish()'s own
        # bookkeeping, or a concurrent subscribe()/add_listener() call.
        for cb in listeners:
            try:
                cb(event)
            except Exception as exc:
                key = id(cb)
                if key not in self._listener_errored:
                    # Reported once per callback, not once per event: a
                    # listener that fails on every publish() would
                    # otherwise spam stderr once per subtitle segment for
                    # the rest of the session.
                    self._listener_errored.add(key)
                    print(f"[hayamimi] event listener raised (further errors from "
                          f"this listener are suppressed): {exc!r}", file=sys.stderr)

    def partial(self, text: str) -> None:
        self.publish({"type": "partial", "text": text})

    def final(self, text: str, lang: str = "", speaker: str = "",
              latency_ms: float | None = None, tier: str = "",
              audio_s: float | None = None, lid_ms: float | None = None,
              decode_ms: float | None = None, switched: bool = False,
              device: str = "") -> None:
        self.publish({"type": "final", "text": text, "lang": lang, "speaker": speaker,
                      "latency_ms": latency_ms, "tier": tier, "audio_s": audio_s,
                      "lid_ms": lid_ms, "decode_ms": decode_ms, "switched": switched,
                      "device": device})

    def subscribe(self) -> queue.Queue:
        """Register a new consumer; past `refine` events are replayed first.

        Used by the /events SSE handler and by ws_ingest.py to mirror the
        same broadcast onto a WebSocket ingest client.
        """
        q: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
        with self._lock:
            for past in self._refines:
                try:
                    q.put_nowait(past)
                except queue.Full:
                    break
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        """Register an in-process callback invoked synchronously on every
        publish(), once per event, with the raw event dict (not the JSON
        string subscribe()'s queue carries). See the class docstring for
        when to use this instead of subscribe()."""
        with self._lock:
            self._listeners.append(callback)


class RuntimeControls:
    """Bundles the runtime-configurable pieces of a live realtime_
    transcribe.py session so SubtitleServer's GET/POST /config and POST
    /reset handlers can read and apply them without this module importing
    realtime_transcribe.py (this module stays a leaf: stdlib imports
    only, so it keeps importing cleanly in the CI environment described in
    the project's contributing docs).

    Attach an instance to `server.controls` after construction (same
    pattern as `server.asr` for /replacements and /itn_overrides); every
    field defaults to None so a caller can wire up only the pieces it
    actually has (e.g. no `live_vad` handle when driving the pipeline from
    a fixed --wav source with no live capture VAD to reconfigure). GET/POST
    /config and POST /reset are 404 when no controls are attached at all,
    and each individual key's setter call is skipped in favor of a 400 if
    the specific handle it needs (asr / translator_pool / live_vad /
    reset_fn) wasn't wired.

      asr             -- RoutedASR instance: forced_lang/dual_confirm/
                         punctuate/lid_switch_confirm/min_switch_s setters
      translator_pool -- TranslatorPool instance: add_target/remove_target
      live_vad        -- realtime_transcribe.LiveVad instance:
                         set_sensitivity
      reset_fn        -- zero-argument callable performing a full session
                         reset (a closure over reset_live_session() and
                         its other arguments, built by the caller)
    """

    def __init__(self, asr=None, translator_pool=None, live_vad=None, reset_fn=None):
        self.asr = asr
        self.translator_pool = translator_pool
        self.live_vad = live_vad
        self.reset_fn = reset_fn

    def get_config(self) -> dict:
        cfg = {"engine": "reazonspeech", "lang": None, "dual_confirm": None, "punctuate": None,
               "lid_switch_confirm": None, "min_switch_s": None,
               "translate": [], "vad": None}
        if self.asr is not None:
            cfg["engine"] = getattr(self.asr, "engine_name", "reazonspeech")
            cfg["device_name"] = getattr(self.asr, "device_name", "")
            cfg["lang"] = self.asr.forced_lang
            cfg["dual_confirm"] = self.asr.dual_confirm
            cfg["punctuate"] = self.asr._punctuate
            cfg["lid_switch_confirm"] = self.asr.lid_switch_confirm
            cfg["min_switch_s"] = self.asr.min_switch_s
        if self.translator_pool is not None:
            cfg["translate"] = self.translator_pool.targets
        if self.live_vad is not None:
            cfg["vad"] = self.live_vad.current_params()
        return cfg

    def apply_config(self, payload: dict) -> None:
        """Apply any subset of GET /config's keys from `payload`.

        Each key is applied in the order below and validated by the
        underlying setter it calls; the first invalid value raises
        ValueError immediately (the caller turns that into a 400), and
        whichever keys were already applied before that point stay
        applied -- this is a thin pass-through over each piece's own
        setter, not a transaction, matching how the individual setters
        already validate their own single value with no cross-key
        coordination needed.
        """
        if "lang" in payload:
            if self.asr is None:
                raise ValueError("no ASR engine attached")
            self.asr.set_forced_lang(payload["lang"])
        if "dual_confirm" in payload:
            if self.asr is None:
                raise ValueError("no ASR engine attached")
            self.asr.set_dual_confirm(bool(payload["dual_confirm"]))
        if "punctuate" in payload:
            if self.asr is None:
                raise ValueError("no ASR engine attached")
            self.asr.set_punctuate(bool(payload["punctuate"]))
        if "lid_switch_confirm" in payload:
            if self.asr is None:
                raise ValueError("no ASR engine attached")
            self.asr.set_lid_switch_confirm(payload["lid_switch_confirm"])
        if "min_switch_s" in payload:
            if self.asr is None:
                raise ValueError("no ASR engine attached")
            self.asr.set_min_switch_s(payload["min_switch_s"])
        if "translate" in payload:
            if self.translator_pool is None:
                raise ValueError("no translator pool attached")
            wanted = payload["translate"]
            if not isinstance(wanted, list) or not all(isinstance(x, str) for x in wanted):
                raise ValueError('"translate" must be a list of language codes')
            current = set(self.translator_pool.targets)
            wanted_set = set(wanted)
            for lang in wanted_set - current:
                self.translator_pool.add_target(lang)  # raises ValueError on an unsupported code
            for lang in current - wanted_set:
                self.translator_pool.remove_target(lang)
        if "vad" in payload:
            if self.live_vad is None:
                raise ValueError("no VAD handle attached")
            vad_cfg = payload["vad"]
            if not isinstance(vad_cfg, dict):
                raise ValueError('"vad" must be an object')
            self.live_vad.set_sensitivity(
                threshold=vad_cfg.get("threshold"),
                min_silence=vad_cfg.get("min_silence"),
                max_speech=vad_cfg.get("max_speech"))

    def reset(self) -> bool:
        """Run the session reset. Returns True if it completed, False if
        `reset_fn` reports it's still pending (GitHub issue #29 review
        round 1: reset_live_session() has to run on the same thread as the
        decode loop, not this HTTP handler thread -- see that function's
        own docstring -- so `reset_fn` typically submits it to a control
        queue and waits with a timeout; a timeout means it's still queued,
        not that it failed). The HTTP handler below turns False into a
        202, not a 500 or a false-positive 200.
        """
        if self.reset_fn is None:
            raise ValueError("no reset function attached")
        return bool(self.reset_fn())


class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer that silences expected client disconnect exceptions.

    Prevents noisy WinError 10054 (ConnectionResetError), ConnectionAbortedError,
    BrokenPipeError, and socket timeouts from flooding stderr when clients (browsers,
    OBS, polling probes) close tabs, reload, or terminate connections abruptly.
    """

    def handle_error(self, request, client_address):
        _, exc_value, _ = sys.exc_info()
        if isinstance(exc_value, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError, socket.timeout)):
            return
        super().handle_error(request, client_address)


class SubtitleServer:
    """HTTP/SSE front end over an EventHub.

    Fans a session's structured events out to any number of browser SSE
    clients (the OBS overlay, the dashboard, the transcript view) and
    exposes a handful of small JSON control endpoints. All of the actual
    pub/sub bookkeeping lives in EventHub (`self.hub`, constructed
    automatically if not passed in); this class is purely the HTTP
    plumbing on top, so realtime_transcribe.main() can create an EventHub
    unconditionally and only add this HTTP layer when `--serve` was
    passed.

    Optionally fronts a RoutedASR instance (set via `.asr` after
    construction, e.g. by realtime_transcribe.py once the engine is built)
    to expose its live replacement dictionary and ITN overrides over two
    tiny JSON endpoints, so an external UI can edit them mid-session:

      GET  /replacements            -> {"wrong": "right", ...}
      POST /replacements            -> replaces the whole dictionary
      GET  /itn_overrides           -> {"exclude": [...], "force": {...}}
      POST /itn_overrides           -> replaces the whole overrides object

    Both endpoints are no-ops (404) when no `.asr` is attached. Similarly,
    a RuntimeControls instance set via `.controls` (see that class) backs:

      GET  /config                  -> current runtime config
      POST /config                  -> apply any subset of it
      POST /reset                   -> run a full session reset

    all 404 when no `.controls` is attached.
    """

    def __init__(self, port: int = 8833, host: str = "127.0.0.1", hub: "EventHub | None" = None):
        self.port = port
        self.host = host
        self.hub = hub if hub is not None else EventHub()
        self._httpd = None
        self.asr = None       # RoutedASR, attached after construction if --serve
        self._controls = None # RuntimeControls, attached after construction if --serve

    @property
    def controls(self):
        return self._controls

    @controls.setter
    def controls(self, val):
        self._controls = val
        if val is not None and "PYTEST_CURRENT_TEST" not in os.environ:
            saved = _get_saved_asr_config()
            if saved:
                try:
                    val.apply_config(saved)
                    print(f"[subtitle_server] 永続化されたASR設定を復元しました: {saved}", file=sys.stderr)
                except Exception as exc:
                    print(f"[subtitle_server] 保存済みASR設定の復元に失敗: {exc}", file=sys.stderr)

    def publish(self, event: dict):
        self.hub.publish(event)

    def partial(self, text: str):
        self.hub.partial(text)

    def final(self, text: str, lang: str = "", speaker: str = "",
              latency_ms: float | None = None, tier: str = "",
              audio_s: float | None = None, lid_ms: float | None = None,
              decode_ms: float | None = None, switched: bool = False,
              device: str = ""):
        self.hub.final(text, lang, speaker, latency_ms, tier,
                       audio_s=audio_s, lid_ms=lid_ms, decode_ms=decode_ms, switched=switched,
                       device=device)

    def subscribe(self) -> queue.Queue:
        """Register a new consumer; past `refine` events are replayed first.

        Used by the /events SSE handler and by ws_ingest.py to mirror the
        same broadcast onto a WebSocket ingest client.
        """
        return self.hub.subscribe()

    def unsubscribe(self, q: queue.Queue):
        self.hub.unsubscribe(q)

    def start(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the console clean for subtitles
                pass

            def handle(self):
                """Wrap request handling to cleanly ignore client resets/aborts."""
                try:
                    super().handle()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError, socket.timeout):
                    self.close_connection = True
                except Exception:
                    raise

            def _validate_host(self) -> bool:
                """Prevent DNS rebinding attacks by validating the Host header."""
                host_hdr = self.headers.get("Host")
                if not host_hdr:
                    return True
                try:
                    parsed_h = urllib.parse.urlsplit(f"//{host_hdr}")
                    req_host = (parsed_h.hostname or "").lower()
                except Exception:
                    req_host = host_hdr.split(":")[0].strip().lower().strip("[]")

                bound_host = (server.host or "").lower()
                if bound_host and bound_host not in ("0.0.0.0", "127.0.0.1", "::1", "localhost"):
                    if req_host == bound_host:
                        return True

                if is_safe_host_or_ip(req_host):
                    return True

                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"403 Forbidden: Invalid Host header")
                return False

            def _read_json_body(self, max_bytes: int = _MAX_DICT_BODY_BYTES):
                """Safely read and parse incoming JSON body with strict size checks."""
                hdr = self.headers.get("Content-Length")
                if hdr is None:
                    return 200, {}
                try:
                    length = int(hdr)
                except ValueError:
                    self._json(400, {"error": "Invalid Content-Length header"})
                    return 400, None
                if length < 0:
                    self._json(400, {"error": "Negative Content-Length"})
                    return 400, None
                if length > max_bytes:
                    self._json(413, {"error": f"Payload exceeds size limit ({max_bytes} bytes)"})
                    return 413, None
                if length == 0:
                    return 200, {}
                raw = self.rfile.read(length)
                if len(raw) != length:
                    self._json(400, {"error": "Unexpected EOF reading request body"})
                    return 400, None
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    return 200, payload
                except Exception as exc:
                    self._json(400, {"error": f"Invalid JSON: {exc}"})
                    return 400, None

            def do_GET(self):
                if not self._validate_host():
                    return
                req_path = urllib.parse.urlsplit(self.path).path
                origin = self.headers.get("Origin")
                if req_path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Expires", "0")
                    safe_origin = _get_safe_origin(origin, self.headers.get("Host"))
                    if safe_origin:
                        self.send_header("Access-Control-Allow-Origin", safe_origin)  # codeql[py/http-response-splitting]
                        self.send_header("Vary", "Origin")
                    self.end_headers()
                    q = server.subscribe()
                    try:
                        while True:
                            try:
                                data = q.get(timeout=15.0)
                                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                                self.wfile.flush()
                            except queue.Empty:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError, Exception):
                        pass
                    finally:
                        server.unsubscribe(q)
                elif req_path == "/dashboard":
                    body = DASHBOARD_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif req_path in ("/mic", "/settings"):
                    body = _get_mic_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif req_path == "/profile":
                    body = _get_profile_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif req_path == "/api/gemini-key":
                    k = _get_gemini_key()
                    masked = (k[:6] + "..." + k[-4:]) if len(k) > 10 else ("設定済み" if k else "")
                    self._json(200, {"configured": bool(k), "masked": masked})
                elif req_path == "/api/dsp/config":
                    try:
                        from scripts.audio_dsp import get_global_dsp
                    except ImportError:
                        from audio_dsp import get_global_dsp
                    self._json(200, get_global_dsp().get_config())
                elif req_path == "/api/comments/status":
                    try:
                        from scripts.comment_sync import get_global_comment_sync
                    except ImportError:
                        from comment_sync import get_global_comment_sync
                    self._json(200, get_global_comment_sync().get_status())
                elif req_path == "/api/maintenance/status":
                    with _maintenance_lock:
                        st = dict(_maintenance_status)
                        st["total_pending_videos"] = st.get("pending_count", 0)
                        st["total_stale_rules"] = st.get("stale_rules_count", 0)
                        if st.get("running"):
                            st["status"] = "running"
                        elif st.get("scanning"):
                            st["status"] = "scanning"
                        elif st.get("error") and not st.get("run_message"):
                            st["status"] = "error"
                        elif st.get("last_result") is not None or st.get("status") == "completed":
                            st["status"] = "completed"
                        st["message"] = st.get("run_message", "")
                    self._json(200, st)
                elif req_path == "/api/profiles/task-status":
                    with _generation_lock:
                        self._json(200, dict(_generation_task))
                elif req_path == "/api/profiles":
                    try:
                        from tools.profile_builder.profile_exporter import list_profiles
                        pdir = _get_profiles_dir()
                        profs = list_profiles(pdir)
                        res_list = []
                        for p in profs:
                            d = p.model_dump()
                            d["available_tags"] = p.get_available_tags()
                            res_list.append(d)
                        self._json(200, res_list)
                    except Exception as e:
                        self._json(500, {"error": str(e)})
                elif req_path == "/api/obs-config":
                    self._json(200, _get_saved_obs_config())
                elif req_path == "/api/profiles/active":
                    act = getattr(server, "_active_profile", None)
                    active_tags = getattr(server, "_active_profile_tags", []) or []
                    if act is None:
                        saved_sid = _get_saved_active_speaker_id()
                        if saved_sid:
                            try:
                                from tools.profile_builder.profile_exporter import load_profile
                                pdir = _get_profiles_dir()
                                restored_prof = load_profile(saved_sid, pdir)
                                if restored_prof is not None:
                                    server._active_profile = restored_prof
                                    act = restored_prof
                                    if server.asr is not None:
                                        server.asr.set_replacements(restored_prof.get_active_replacements([]))
                            except Exception as ex:
                                logger.debug("Auto-restore profile failed: %s", ex)
                    if act:
                        self._json(200, {
                            "active": True,
                            "speaker_id": act.speaker_id,
                            "display_name": act.display_name,
                            "available_tags": act.get_available_tags(),
                            "active_tags": active_tags,
                            "youtube_channel_url": getattr(act, "youtube_channel_url", None),
                            "has_voiceprint": bool(act.voiceprint and len(act.voiceprint) > 0),
                            "voiceprint_samples": act.voiceprint_sample_count or 0,
                        })
                    else:
                        self._json(200, {"active": False, "speaker_id": None})
                elif req_path.startswith("/api/profiles/") and req_path.endswith("/export"):
                    parts = req_path.split("/")
                    if len(parts) == 5:
                        speaker_id = urllib.parse.unquote(parts[3])
                        if not is_safe_speaker_id(speaker_id):
                            self._json(400, {"error": "Invalid speaker_id format"})
                            return
                        try:
                            from tools.profile_builder.profile_exporter import load_profile, export_profile_json
                            pdir = _get_profiles_dir()
                            prof = load_profile(speaker_id, pdir)
                            if prof is None:
                                self._json(404, {"error": "profile not found"})
                                return
                            json_str = export_profile_json(prof)
                            body = json_str.encode("utf-8")
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header("Content-Length", str(len(body)))
                            safe_sid = re.sub(r"[^a-zA-Z0-9_\-]", "_", prof.speaker_id)
                            self.send_header("Content-Disposition", f'attachment; filename="{safe_sid}.profile.json"')
                            self.end_headers()
                            self.wfile.write(body)
                            return
                        except Exception as e:
                            self._json(500, {"error": str(e)})
                            return
                elif req_path == "/overlay-style":
                    self._json(200, _get_overlay_style())
                elif req_path == "/transcript":
                    body = TRANSCRIPT_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif req_path == "/replacements":
                    if server.asr is None:
                        self._json(404, {"error": "no ASR engine attached"})
                        return
                    self._json(200, dict(server.asr._replacements))
                elif req_path == "/itn_overrides":
                    if server.asr is None:
                        self._json(404, {"error": "no ASR engine attached"})
                        return
                    overrides = server.asr._itn_overrides
                    self._json(200, {"exclude": sorted(overrides.exclude),
                                     "force": overrides.force})
                elif req_path == "/config":
                    if server.controls is None:
                        self._json(404, {"error": "no runtime controls attached"})
                        return
                    self._json(200, server.controls.get_config())
                else:
                    body = OVERLAY_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Expires", "0")
                    self.end_headers()
                    self.wfile.write(body)

            def do_POST(self):
                if not self._validate_host():
                    return
                origin = self.headers.get("Origin")
                if origin:
                    safe_origin = _get_safe_origin(origin, self.headers.get("Host"))
                    if not safe_origin:
                        self.send_response(403)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(b"403 Forbidden: Untrusted Origin")
                        return

                req_path = urllib.parse.urlsplit(self.path).path
                ctype = self.headers.get("Content-Type", "").lower()
                if not ctype.startswith("application/json") and req_path != "/reset":
                    self._json(415, {"error": "Content-Type must be application/json"})
                    return
                if req_path == "/reset":
                    if server.controls is None:
                        self._json(404, {"error": "no runtime controls attached"})
                        return
                    try:
                        completed = server.controls.reset()
                    except ValueError as exc:
                        self._json(400, {"error": str(exc)})
                        return
                    if completed:
                        self._json(200, {"ok": True})
                    else:
                        # GitHub issue #29 review round 1: the reset was
                        # queued but hasn't run yet (reset_fn's own timeout
                        # elapsed -- see RuntimeControls.reset()'s
                        # docstring) -- 202 Accepted, not a failure.
                        self._json(202, {"ok": False, "pending": True})
                    return
                if req_path == "/overlay-style":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    cur = _get_overlay_style()
                    merged = _deep_merge_dict(cur, payload)
                    sanitized = _sanitize_overlay_style(merged)
                    _save_overlay_style(sanitized)
                    server.publish({"type": "style_update", "style": sanitized})
                    self._json(200, {"ok": True, "style": sanitized})
                    return
                if req_path == "/api/gemini-key":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    try:
                        _save_gemini_key(payload.get("key", ""))
                        self._json(200, {"ok": True})
                    except Exception as exc:
                        self._json(400, {"error": str(exc)})
                    return
                if req_path == "/api/profiles/generate":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    url = payload.get("url")
                    if not url:
                        self._json(400, {"error": "url is required"})
                        return
                    key = _get_gemini_key()
                    if not key:
                        self._json(400, {
                            "error": "Gemini API キーが設定されていません。プロファイルの自動生成・辞書育成には Google AI Studio の API キーが必要です。上部のキー設定欄から登録してください。"
                        })
                        return
                    display_name = payload.get("display_name")
                    speaker_id = payload.get("speaker_id")
                    existing_profile_id = payload.get("existing_profile_id")
                    if speaker_id and not is_safe_speaker_id(speaker_id):
                        self._json(400, {"error": "Invalid speaker_id format"})
                        return
                    if existing_profile_id and not is_safe_speaker_id(existing_profile_id):
                        self._json(400, {"error": "Invalid existing_profile_id format"})
                        return
                    manual_start = payload.get("manual_start")
                    duration = float(payload.get("duration") or 720.0)
                    max_videos = int(payload.get("max_videos") or 10)
                    collab_filter_enabled = payload.get("collab_filter_enabled")
                    collab_exclude_keywords = payload.get("collab_exclude_keywords")
                    if collab_exclude_keywords is not None and not isinstance(collab_exclude_keywords, list):
                        collab_exclude_keywords = None

                    with _generation_lock:
                        if not _generation_task["done"] and _generation_task["status"] == "running":
                            self._json(409, {"error": "Another profile generation is already in progress"})
                            return
                        _generation_task["status"] = "running"
                        _generation_task["progress"] = 0.05
                        _generation_task["message"] = "生成タスクを開始中..."
                        _generation_task["done"] = False
                        _generation_task["error"] = None
                        _generation_task["url"] = url
                        _generation_task["speaker_id"] = speaker_id or existing_profile_id
                        _generation_task["display_name"] = display_name
                        _generation_task["mode"] = "grow" if existing_profile_id else "create"
                        _generation_task["logs"] = []

                    _append_generation_log("INIT", f"タスク初期化: モード={_generation_task['mode']}, URL={url}")

                    def _run_gen():
                        try:
                            # 確実にリポジトリルートをインポートパスに含める
                            if _REPO_ROOT not in sys.path:
                                sys.path.insert(0, _REPO_ROOT)
                            from tools.profile_builder.pipeline import ProfileBuilderPipeline
                            def _cb(stage, prog, msg):
                                with _generation_lock:
                                    _generation_task["progress"] = prog
                                    _generation_task["message"] = msg

                            def _log_cb(level, text):
                                _append_generation_log(level, text)

                            key = _get_gemini_key()
                            pipeline = ProfileBuilderPipeline(api_key=key)
                            prof = pipeline.run(
                                youtube_url=url,
                                speaker_id=speaker_id,
                                display_name=display_name,
                                manual_start_sec=manual_start,
                                duration_sec=duration,
                                max_videos=max_videos,
                                existing_profile_id=existing_profile_id,
                                status_callback=_cb,
                                log_callback=_log_cb,
                                collab_filter_enabled=collab_filter_enabled,
                                collab_exclude_keywords=collab_exclude_keywords,
                            )
                            with _generation_lock:
                                _generation_task["status"] = "completed"
                                _generation_task["progress"] = 1.0
                                v_cnt = len(prof.processed_video_ids) or len(prof.source_video_urls) or 1
                                msg = f"完了: {prof.display_name} (学習動画: {v_cnt}本, ルール: {len(prof.replacements)}件)"
                                _generation_task["message"] = msg
                                _generation_task["done"] = True
                            _append_generation_log("SUCCESS", msg)
                        except Exception as e:
                            interim_info = ""
                            try:
                                from tools.profile_builder.profile_exporter import load_profile
                                target_p = speaker_id or existing_profile_id
                                if target_p:
                                    saved_prof = load_profile(target_p)
                                    if saved_prof:
                                        cnt = len(saved_prof.processed_video_ids) or len(saved_prof.source_video_urls) or 0
                                        interim_info = f" (※ここまでに学習されたプロファイル: {cnt}本・ルール{len(saved_prof.replacements)}件は安全に保存済みです)"
                            except Exception:
                                pass

                            with _generation_lock:
                                _generation_task["status"] = "failed"
                                _generation_task["done"] = True
                                _generation_task["error"] = str(e)
                                _generation_task["message"] = f"エラー: {e}{interim_info}"
                            _append_generation_log("ERROR", f"タスク失敗: {e}{interim_info}")

                    threading.Thread(target=_run_gen, daemon=True).start()
                    self._json(202, {"ok": True, "message": "Generation started"})
                    return
                if req_path == "/api/maintenance/run":
                    with _maintenance_lock:
                        if _maintenance_status["running"]:
                            self._json(200, {"ok": False, "message": "Already running", "status": "running"})
                            return

                    def _run_maint():
                        res = _execute_maintenance(server)
                        with _maintenance_lock:
                            _maintenance_status["last_result"] = res

                    threading.Thread(target=_run_maint, daemon=True).start()
                    self._json(202, {"ok": True, "message": "Maintenance started", "status": "running"})
                    return
                if req_path == "/api/maintenance/scan":
                    _scan_channels_for_maintenance(server)
                    self._json(200, {"ok": True, "message": "Scan started"})
                    return
                if req_path == "/api/profiles/sync-stats":
                    _sync_stats_to_profile(server)
                    self._json(200, {"ok": True})
                    return
                if req_path == "/api/dsp/config":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    try:
                        from scripts.audio_dsp import save_global_dsp_config
                    except ImportError:
                        from audio_dsp import save_global_dsp_config
                    updated = save_global_dsp_config(payload)
                    self._json(200, {"ok": True, **updated})
                    return
                if req_path == "/api/comments/config":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    try:
                        from scripts.comment_sync import get_global_comment_sync, save_global_comment_sync_config
                    except ImportError:
                        from comment_sync import get_global_comment_sync, save_global_comment_sync_config
                    syncer = get_global_comment_sync()
                    if server.asr is not None and syncer.on_dictionary_updated is None:
                        syncer.on_dictionary_updated = lambda m: server.asr.set_dynamic_comment_replacements(m)
                    status = save_global_comment_sync_config(payload)
                    if status.get("enabled"):
                        if "PYTEST_CURRENT_TEST" not in os.environ:
                            syncer.start()
                    else:
                        syncer.stop()
                        if server.asr is not None:
                            server.asr.set_dynamic_comment_replacements({})
                    self._json(200, {"ok": True, **status})
                    return
                if req_path == "/api/obs-config":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    saved = _save_obs_config(payload)
                    self._json(200, {"ok": True, **saved})
                    return
                if req_path == "/api/profiles/apply":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    try:
                        sid = payload.get("speaker_id")
                        if not sid:
                            _sync_stats_to_profile(server)
                            server._active_profile = None
                            server._active_profile_tags = []
                            _save_active_speaker_id(None)
                            if server.asr is not None:
                                server.asr.set_replacements({})
                            self._json(200, {"ok": True, "active": False, "speaker_id": None})
                            return
                        if not is_safe_speaker_id(sid):
                            self._json(400, {"error": "Invalid speaker_id format"})
                            return
                        active_tags = payload.get("active_tags")
                        if active_tags is not None and not isinstance(active_tags, list):
                            active_tags = [str(active_tags)]

                        # Sync existing active profile stats before switching
                        _sync_stats_to_profile(server)

                        from tools.profile_builder.profile_exporter import load_profile
                        pdir = _get_profiles_dir()
                        prof = load_profile(sid, pdir)
                        if prof is None:
                            self._json(404, {"error": f"profile {sid} not found"})
                            return
                        mapping = prof.get_active_replacements(active_tags)
                        if server.asr is not None:
                            server.asr.set_replacements(mapping)
                        server._active_profile = prof
                        server._active_profile_tags = active_tags or []
                        _save_active_speaker_id(prof.speaker_id)
                        has_vp = bool(prof.voiceprint and len(prof.voiceprint) > 0)

                        chan_url = getattr(prof, "youtube_channel_url", None)
                        if chan_url:
                            try:
                                from scripts.comment_sync import get_global_comment_sync
                            except ImportError:
                                from comment_sync import get_global_comment_sync
                            get_global_comment_sync().set_config({"youtube_channel_url": chan_url})

                        self._json(200, {
                            "ok": True,
                            "speaker_id": sid,
                            "display_name": prof.display_name,
                            "rules_count": len(mapping),
                            "total_rules_count": len(prof.replacements),
                            "available_tags": prof.get_available_tags(),
                            "active_tags": server._active_profile_tags,
                            "youtube_channel_url": chan_url,
                            "has_voiceprint": has_vp,
                            "voiceprint_samples": prof.voiceprint_sample_count or 0,
                        })
                    except Exception as exc:
                        self._json(400, {"error": str(exc)})
                    return
                if req_path == "/api/profiles/update-tags":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    act = getattr(server, "_active_profile", None)
                    if not act:
                        self._json(400, {"error": "No active profile"})
                        return
                    active_tags = payload.get("active_tags", [])
                    if not isinstance(active_tags, list):
                        active_tags = [str(active_tags)]
                    mapping = act.get_active_replacements(active_tags)
                    if server.asr is not None:
                        server.asr.set_replacements(mapping)
                    server._active_profile_tags = active_tags
                    self._json(200, {
                        "ok": True,
                        "speaker_id": act.speaker_id,
                        "active_tags": active_tags,
                        "rules_count": len(mapping),
                        "total_rules_count": len(act.replacements),
                        "available_tags": act.get_available_tags(),
                    })
                    return
                if req_path == "/api/profiles/update-rule-tags":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    sid = payload.get("speaker_id")
                    pattern = payload.get("pattern")
                    tags = payload.get("tags")
                    if not sid or not pattern or not isinstance(tags, list):
                        self._json(400, {"error": "speaker_id, pattern and tags list are required"})
                        return
                    if not is_safe_speaker_id(sid):
                        self._json(400, {"error": "Invalid speaker_id format"})
                        return
                    from tools.profile_builder.profile_exporter import load_profile, save_profile
                    pdir = _get_profiles_dir()
                    prof = load_profile(sid, pdir)
                    if prof is None:
                        self._json(404, {"error": f"profile {sid} not found"})
                        return
                    found = False
                    for r in prof.replacements + prof.archived_replacements:
                        if r.pattern == pattern:
                            r.tags = [t.strip() for t in tags if t and isinstance(t, str)]
                            found = True
                            break
                    if not found:
                        self._json(404, {"error": f"rule '{pattern}' not found"})
                        return
                    save_profile(prof, pdir)
                    # If active, reload replacements
                    if getattr(server, "_active_profile", None) and server._active_profile.speaker_id == sid:
                        server._active_profile = prof
                        if server.asr is not None:
                            active_tags = getattr(server, "_active_profile_tags", None)
                            server.asr.set_replacements(prof.get_active_replacements(active_tags))
                    self._json(200, {"ok": True, "speaker_id": sid, "pattern": pattern, "tags": r.tags})
                    return
                if req_path == "/api/profiles/update-collab-settings":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    sid = payload.get("speaker_id")
                    if not sid or not is_safe_speaker_id(sid):
                        self._json(400, {"error": "Valid speaker_id is required"})
                        return
                    from tools.profile_builder.profile_exporter import load_profile, save_profile
                    pdir = _get_profiles_dir()
                    prof = load_profile(sid, pdir)
                    if prof is None:
                        self._json(404, {"error": f"profile {sid} not found"})
                        return
                    if "collab_filter_enabled" in payload:
                        prof.collab_filter_enabled = bool(payload["collab_filter_enabled"])
                    if "collab_exclude_keywords" in payload and isinstance(payload["collab_exclude_keywords"], list):
                        prof.collab_exclude_keywords = [
                            str(k).strip() for k in payload["collab_exclude_keywords"] if str(k).strip()
                        ]
                    save_profile(prof, pdir)
                    if getattr(server, "_active_profile", None) and server._active_profile.speaker_id == sid:
                        server._active_profile = prof
                    self._json(200, {
                        "ok": True,
                        "speaker_id": sid,
                        "collab_filter_enabled": prof.collab_filter_enabled,
                        "collab_exclude_keywords": prof.collab_exclude_keywords,
                    })
                    return
                if req_path == "/api/profiles/import":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    try:
                        json_content = payload.get("json_content")
                        if not json_content:
                            self._json(400, {"error": "json_content is required"})
                            return
                        from tools.profile_builder.profile_exporter import import_profile_json
                        pdir = _get_profiles_dir()
                        prof = import_profile_json(json_content, storage_dir=pdir)
                        if getattr(server, "_active_profile", None) and server._active_profile.speaker_id == prof.speaker_id:
                            server._active_profile = prof
                            if server.asr is not None:
                                active_tags = getattr(server, "_active_profile_tags", None)
                                server.asr.set_replacements(prof.get_active_replacements(active_tags))
                        self._json(200, prof.model_dump())
                    except Exception as exc:
                        self._json(400, {"error": str(exc)})
                    return
                if req_path == "/api/profiles/delete":
                    code, payload = self._read_json_body()
                    if code != 200 or payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    try:
                        sid = payload.get("speaker_id")
                        if not sid:
                            self._json(400, {"error": "speaker_id is required"})
                            return
                        if not is_safe_speaker_id(sid):
                            self._json(400, {"error": "Invalid speaker_id format"})
                            return
                        from tools.profile_builder.profile_exporter import delete_profile
                        pdir = _get_profiles_dir()
                        deleted = delete_profile(sid, storage_dir=pdir)
                        self._json(200, {"ok": deleted})
                    except Exception as exc:
                        self._json(400, {"error": str(exc)})
                    return
                if req_path not in ("/replacements", "/itn_overrides", "/config"):
                    self._json(404, {"error": "not found"})
                    return
                if req_path == "/config":
                    if server.controls is None:
                        self._json(404, {"error": "no runtime controls attached"})
                        return
                elif server.asr is None:
                    self._json(404, {"error": "no ASR engine attached"})
                    return
                code, payload = self._read_json_body()
                if code != 200 or payload is None:
                    return
                if not isinstance(payload, dict):
                    self._json(400, {"error": "body must be a JSON object"})
                    return
                if req_path == "/replacements":
                    server.asr.set_replacements(payload)
                    self._json(200, {"ok": True})
                elif req_path == "/itn_overrides":
                    exclude = payload.get("exclude") or []
                    force = payload.get("force") or {}
                    if not isinstance(exclude, list) or not isinstance(force, dict):
                        self._json(400, {"error": '"exclude" must be a list, "force" a dict'})
                        return
                    server.asr.set_itn_overrides(exclude=set(exclude), force=force)
                    self._json(200, {"ok": True})
                else:  # /config
                    try:
                        server.controls.apply_config(payload)
                        _save_asr_config(payload)
                    except (ValueError, TypeError) as exc:
                        self._json(400, {"error": str(exc)})
                        return
                    self._json(200, {"ok": True})

            def _json(self, status: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                origin = self.headers.get("Origin")
                safe_origin = _get_safe_origin(origin, self.headers.get("Host"))
                if safe_origin:
                    self.send_header("Access-Control-Allow-Origin", safe_origin)  # codeql[py/http-response-splitting]
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(body)

        self._httpd = _QuietThreadingHTTPServer((self.host, self.port), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

        # Kick off background maintenance scan 3 seconds after startup (non-blocking)
        if "PYTEST_CURRENT_TEST" not in os.environ:
            threading.Timer(3.0, _scan_channels_for_maintenance, args=(self,)).start()
            try:
                from comment_sync import get_global_comment_sync
                csync = get_global_comment_sync()
                csync.on_dictionary_updated = lambda m: (
                    self.asr.set_dynamic_comment_replacements(m)
                    if self.asr is not None and hasattr(self.asr, "set_dynamic_comment_replacements")
                    else None
                )
                if csync.enabled:
                    csync.start()
            except Exception as ce:
                logger.debug("Comment sync startup skipped: %s", ce)

        return self
