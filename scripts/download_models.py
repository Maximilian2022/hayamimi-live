#!/usr/bin/env python
"""Download all pretrained models hayamimi needs into models/.

Idempotent: any model whose target already exists is skipped, so re-running
this after an interrupted download only fetches what's missing.

Two model sets:
  --minimal   ja/en core only: ReazonSpeech (ja), whisper-tiny (LID + en
              fallback via VAD), Silero VAD, Japanese punctuation. ~1.1 GB.
  (default)   Everything the runtime routing in asr_engine.py can reach:
              minimal + zh/ko/yue/multilingual-EU/1600-language-fallback ASR,
              speaker embeddings, and ja->en/zh/ko translation. ~3.1 GB.

Add --eval-baselines to additionally fetch two extra models that are only
used as comparison baselines by scripts/eval_accuracy.py and
scripts/make_realset_zhko.py (not used by the live pipeline). ~1 GB more.

All models are pulled from their original publishers (via k2-fsa/sherpa-onnx's
GitHub release mirrors, or directly from Hugging Face). See
THIRD_PARTY_NOTICES.md for what you're agreeing to by downloading each one --
in particular, the ja->en translation model is CC BY-SA 4.0 (share-alike),
not permissive like the rest.
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

import argparse
import io
import os
import shutil
import tarfile
import urllib.request

MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")

GITHUB_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
ASR_TAG = "asr-models"
# Yes, "recongition" -- that's the actual (misspelled) tag name upstream.
SPEAKER_TAG = "speaker-recongition-models"
SEGMENTATION_TAG = "speaker-segmentation-models"
HF_RESOLVE = "https://huggingface.co/{repo}/resolve/main/{path}"


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _download_to(url: str, dest: str, max_retries: int = 3) -> None:
    """Stream url to dest with a progress print. Overwrites if dest exists."""
    import time
    is_tty = sys.stdout.isatty()
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hayamimi-download-models/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                tmp = dest + ".part"
                read = 0
                chunk = 1 << 20
                last_bucket = -10
                last_non_pct_mb = 0
                with open(tmp, "wb") as f:
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        f.write(buf)
                        read += len(buf)
                        if total:
                            pct = 100 * read / total
                            if is_tty:
                                print(f"\r  {os.path.basename(dest)}: {pct:5.1f}% ({_human(read)}/{_human(total)})",
                                      end="", flush=True)
                            else:
                                # GUIランチャー等のパイプ受け: 5%刻み、または完了時に改行付きで出力してデッドロック防止&進捗可視化
                                bucket = int(pct // 5) * 5
                                if bucket > last_bucket or read >= total:
                                    last_bucket = bucket
                                    print(f"  {os.path.basename(dest)}: {pct:5.1f}% ({_human(read)}/{_human(total)})", flush=True)
                        else:
                            if is_tty:
                                print(f"\r  {os.path.basename(dest)}: {_human(read)}", end="", flush=True)
                            else:
                                cur_mb = read // (10 * 1024 * 1024)
                                if cur_mb > last_non_pct_mb:
                                    last_non_pct_mb = cur_mb
                                    print(f"  {os.path.basename(dest)}: {_human(read)}", flush=True)
                if is_tty:
                    print()
                os.replace(tmp, dest)
                return
        except Exception as e:
            if attempt < max_retries:
                print(f"\n  [WARN] ダウンロードが中断されました ({e})。再試行中 ({attempt}/{max_retries})...", flush=True)
                time.sleep(2)
            else:
                raise


def _fetch_bytes(url: str, max_retries: int = 3) -> bytes:
    import time
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hayamimi-download-models/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2)
            else:
                raise


def download_file(url: str, dest_path: str, label: str) -> None:
    """Single-file model (e.g. silero_vad.onnx, campplus_sv.onnx)."""
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        print(f"[skip] {label} (already present: {dest_path})")
        return
    print(f"[get ] {label}")
    _download_to(url, dest_path)


def download_and_extract_tarbz2(url: str, dest_dir: str, label: str) -> None:
    """sherpa-onnx release tarball: extracts to a top-level dir matching the
    tarball's basename; we re-home that under models/<dest_dir_name>."""
    target = os.path.join(MODELS_DIR, dest_dir)
    if os.path.isdir(target):
        has_valid_files = any(
            os.path.getsize(os.path.join(dp, f)) > 0
            for dp, dn, fn in os.walk(target)
            for f in fn if not f.startswith(".")
        )
        if has_valid_files:
            print(f"[skip] {label} (already present: {target})")
            return
        print(f"[warn] {label}: {target} exists but contains no valid model files. Re-fetching...")
        shutil.rmtree(target, ignore_errors=True)
    print(f"[get ] {label}")
    tmp = os.path.join(MODELS_DIR, f".{dest_dir}.tar.bz2")
    os.makedirs(MODELS_DIR, exist_ok=True)
    _download_to(url, tmp)
    print(f"  extracting -> {target}")
    with tarfile.open(tmp, "r:bz2") as tf:
        # sherpa-onnx tarballs have a single top-level directory; extract it
        # then rename to our canonical dest_dir name (usually the same, but
        # e.g. omnilingual's release name differs from our shorter dir name).
        members = tf.getmembers()
        top = members[0].name.split("/")[0]
        if hasattr(tarfile, "data_filter"):
            tf.extractall(MODELS_DIR, filter="data")
        else:
            tf.extractall(MODELS_DIR)
    extracted = os.path.join(MODELS_DIR, top)
    if extracted != target:
        os.replace(extracted, target)
    os.remove(tmp)


def extract_members_only(url: str, dest_dir: str, wanted_basenames: set, label: str) -> None:
    """Like download_and_extract_tarbz2, but keeps only specific files from
    the tarball (used for omnilingual, where we only need the int8 weights
    and tokens, not the README/test_wavs).

    "Present" means every wanted file exists, not just the directory: an
    earlier version skipped on the bare directory, so a tarball that lacked
    one of the wanted files left a half-filled directory behind that every
    later run then treated as complete (the 2026-09-04 omni incident: the
    fp32 tarball has no model.int8.onnx, so models/omnilingual-300m-ctc-int8
    held only tokens.txt and the live fallback route had no model to load).
    A tarball missing a wanted file is now an error, and the partial
    directory is removed so the next run retries instead of skipping."""
    target = os.path.join(MODELS_DIR, dest_dir)
    have = {b for b in wanted_basenames if os.path.exists(os.path.join(target, b))}
    if have == wanted_basenames:
        print(f"[skip] {label} (already present: {target})")
        return
    if have:
        print(f"  {target} is incomplete (has {sorted(have)}, "
              f"needs {sorted(wanted_basenames)}): re-fetching")
    print(f"[get ] {label}")
    tmp = os.path.join(MODELS_DIR, f".{dest_dir}.tar.bz2")
    os.makedirs(target, exist_ok=True)
    _download_to(url, tmp)
    print(f"  extracting (selected files) -> {target}")
    got = set()
    with tarfile.open(tmp, "r:bz2") as tf:
        for member in tf.getmembers():
            base = os.path.basename(member.name)
            if base in wanted_basenames:
                member.name = base
                tf.extract(member, target)
                got.add(base)
    os.remove(tmp)
    missing = wanted_basenames - got
    if missing:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"{label}: the tarball at {url} does not contain {sorted(missing)} "
            f"(it has no file by that name); refusing to leave a partial "
            f"{target} behind. The URL or the wanted file list is wrong.")


def download_hf_repo(repo: str, dest_dir: str, label: str, ignore_patterns=None) -> None:
    """Snapshot-download a full Hugging Face repo (used for the Mojicast
    translation/punctuation models, which are distributed as small repos)."""
    target = os.path.join(MODELS_DIR, dest_dir)
    if os.path.isdir(target):
        has_valid_files = any(
            os.path.getsize(os.path.join(dp, f)) > 0
            for dp, dn, fn in os.walk(target)
            for f in fn if not f.startswith(".")
        )
        if has_valid_files:
            print(f"[skip] {label} (already present: {target})")
            return
        print(f"[warn] {label}: {target} exists but contains no valid model files. Re-fetching...")
        shutil.rmtree(target, ignore_errors=True)
    print(f"[get ] {label}")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  ERROR: huggingface_hub not installed. `pip install huggingface_hub` "
              "or `pip install -r requirements.txt` and re-run.", file=sys.stderr)
        raise
    snapshot_download(repo_id=repo, local_dir=target, ignore_patterns=ignore_patterns)


def ensure_campplus_model(log_callback=None) -> str:
    """Ensure campplus_sv.onnx exists in models/ dir, downloading if needed (~25MB)."""
    cam_dest = os.path.join(MODELS_DIR, "campplus_sv.onnx")
    if os.path.isfile(cam_dest) and os.path.getsize(cam_dest) > 1000000:
        return cam_dest
    os.makedirs(MODELS_DIR, exist_ok=True)
    if log_callback:
        log_callback("INFO", "CAM++ 話者声紋モデル (campplus_sv.onnx, ~25MB) をダウンロード中...")
    else:
        print("Downloading CAM++ speaker embedding model (campplus_sv.onnx, ~25MB)...")
    url = f"{GITHUB_RELEASES}/{SPEAKER_TAG}/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
    download_file(url, cam_dest, "CAM++ speaker embedding")
    return cam_dest


HAAS_JA_REPO = "https://huggingface.co/Sanami/haas-ja-onnx/resolve/354baedfd9612b00e74cc1ae87dff7f2c558db6f"
HAAS_JA_FILES = {
    "int8dyn_mm/encoder.onnx": "c7b06fc8c46a6f81c19042c951c044f328b146e98bff7ec8b597ce5054ab4d21",
    "int8dyn_mm/encoder.onnx.data": "7f685192e80f31896df46a6230a0d7a2fd3e77eeaa452f5b84c4956383f32301",
    "int8dyn_mm/decoder_joint.onnx": "f98b6a11912296d7939825cd72ce599fbdcbdbb8f9bf01085aa2c60c37bd0d76",
    "int8dyn_mm/decoder_joint.onnx.data": "fd6e3e4431a3d4aa6264a20376f9e1da7f8667b7817b7ea6edc0ec8de2b7928c",
}


def download_haas_ja() -> None:
    """Haas-ja-v1.0 quantized ONNX (unofficial export, Apache-2.0), pinned and hash-checked."""
    import hashlib

    dest = os.path.join(MODELS_DIR, "haas-ja-onnx-int8dyn_mm")
    os.makedirs(dest, exist_ok=True)
    for remote, expected in HAAS_JA_FILES.items():
        path = os.path.join(dest, os.path.basename(remote))
        if not os.path.exists(path):
            download_file(f"{HAAS_JA_REPO}/{remote}", path, f"Haas-ja {os.path.basename(remote)}")
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            os.remove(path)
            raise RuntimeError(f"Haas-ja hash mismatch: {remote}")
    for name in ("tokenizer.model", "NOTICE", "LICENSE"):
        path = os.path.join(dest, name)
        if not os.path.exists(path):
            download_file(f"{HAAS_JA_REPO}/{name}", path, f"Haas-ja {name}")


def download_opt_ins(args) -> None:
    """Opt-in downloads (--en-parakeet-v2, --punct-4class, --eval-baselines,
    --lid-candidates, --translate-candidates).

    Called from BOTH the --minimal early return and the end of the full run:
    an opt-in flag must never be silently ignored just because it was
    combined with --minimal (the docs/eval/*_candidates.md records tell
    people to add these flags; a model that then never arrives degrades
    routing without any warning at download time).
    """
    if args.en_parakeet_v2:
        download_and_extract_tarbz2(
            f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
            "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
            "Parakeet TDT 0.6B v2 (en-only, opt-in --en-tier v2)")

    if getattr(args, "haas_ja", False):
        download_haas_ja()

    if getattr(args, "whisper_dml", False):
        try:
            from huggingface_hub import snapshot_download
            dest = os.path.join(MODELS_DIR, "kotoba-whisper-v2.2-onnx")
            print(f"Downloading Kotoba-Whisper-v2.2 ONNX for DirectML into {dest}...")
            snapshot_download(
                repo_id="onnx-community/kotoba-whisper-v2.2-ONNX",
                local_dir=dest,
                allow_patterns=["onnx/*int8.onnx", "*.json"],
            )
            print("Kotoba-Whisper-v2.2 ONNX download completed.")
        except Exception as e:
            print(f"Failed to download Kotoba-Whisper-v2.2 ONNX: {e}")

    if getattr(args, "translators", False):
        download_hf_repo(
            "ishiki-emo/mojicast-m2m100-ct2",
            "mojicast-m2m100-ct2",
            "M2M-100 418M CTranslate2 (ja->zh/ko/es/... translation, MIT)")

        download_hf_repo(
            "ishiki-emo/mojicast-fugumt-ja-en-ct2",
            "mojicast-fugumt-ja-en-ct2",
            "FuguMT CTranslate2 (ja->en translation, CC BY-SA 4.0)")

    if args.punct_4class:
        # Same layout as the local models/punct-ja-4class-permissive/ this
        # was trained into (punct_4class.onnx, quantized_ort/*.int8.onnx,
        # hf/tokenizer.json, README.md, SHA256SUMS) -- train_log.jsonl is
        # not part of the published repo. See docs/eval/punct_retrain.md.
        download_hf_repo(
            "oboroge0/hayamimi-punct-ja-4class",
            "punct-ja-4class-permissive",
            "4-class ja punctuation model (opt-in --punct-model 4class, MIT)")

    if args.eval_baselines:
        download_and_extract_tarbz2(
            f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8.tar.bz2",
            "sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8",
            "Parakeet tdt_ctc 0.6B ja (eval baseline only)")

        download_and_extract_tarbz2(
            f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-zipformer-korean-2024-06-24.tar.bz2",
            "sherpa-onnx-zipformer-korean-2024-06-24",
            "Zipformer Korean (eval baseline only)")

    if args.lid_candidates:
        # whisper-base: candidate (b) in docs/eval/lid_candidates.md (LID
        # replacement track). int8 only (~160MB); the tarball also ships
        # fp32 encoder/decoder and test_wavs/, which scripts/eval_lid_candidates.py
        # never touches, so they're skipped here.
        extract_members_only(
            f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-whisper-base.tar.bz2",
            "sherpa-onnx-whisper-base",
            {"base-encoder.int8.onnx", "base-decoder.int8.onnx", "base-tokens.txt"},
            "whisper-base (LID candidate (b), eval only -- see docs/eval/lid_candidates.md)")
        print("\nNote: LID candidates (a) speechbrain VoxLingua107-ECAPA and (d) its "
              "target-language classifier need a SEPARATE torch venv (torch/speechbrain "
              "aren't in this project's runtime requirements) -- see "
              "scripts/eval_lid_voxlingua.py's docstring for setup:\n"
              "  python -m venv .venv-train\n"
              "  .venv-train/Scripts/pip install torch --index-url https://download.pytorch.org/whl/cpu\n"
              "  .venv-train/Scripts/pip install speechbrain soundfile scikit-learn huggingface_hub fsspec\n"
              "  .venv-train/Scripts/python scripts/eval_lid_voxlingua.py")

    if args.translate_candidates:
        # Track B (docs/eval/translate_candidates.md): none of these were
        # adopted -- no shipped script reads models/*-src/. Downloaded as
        # the *upstream* (pre-CTranslate2) repo so a future re-evaluation
        # pass doesn't need to re-locate/re-verify the source models; run
        # the ct2-transformers-converter commands in that doc's "Candidates
        # and setup" section afterwards to get a usable *-ct2/ directory.
        download_hf_repo(
            "Helsinki-NLP/opus-mt-ja-en",
            "opus-mt-ja-en-src",
            "opus-mt-ja-en source repo (Track B candidate, Apache-2.0, not adopted)")

        download_hf_repo(
            "Helsinki-NLP/opus-mt-ja-es",
            "opus-mt-ja-es-src",
            "opus-mt-ja-es source repo (Track B candidate, Apache-2.0, not adopted)")

        download_hf_repo(
            "facebook/m2m100_1.2B",
            "m2m100-1.2B-src",
            "M2M-100 1.2B source repo (Track B candidate, MIT, not adopted)")

        download_hf_repo(
            "NiuTrans/LMT-60-0.6B",
            "lmt60-0.6b-src",
            "LMT-60-0.6B source repo (Track B candidate, Apache-2.0, not adopted)")

        print("\n--translate-candidates done. These are upstream transformers-format repos, "
              "not usable directly -- see docs/eval/translate_candidates.md's 'Candidates and "
              "setup' section for the ct2-transformers-converter commands to produce "
              "models/<name>-ct2/.")

    if getattr(args, "campplus", False):
        ensure_campplus_model()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minimal", action="store_true",
                     help="only download the ja/en core (~1.1GB): ReazonSpeech, whisper-tiny, "
                          "Silero VAD, Japanese punctuation. Skips zh/ko/yue/EU/omnilingual "
                          "ASR, speaker embeddings, and translation models.")
    ap.add_argument("--eval-baselines", action="store_true",
                     help="also download 2 extra models (~1GB) used only by scripts/eval_accuracy.py "
                          "and scripts/make_realset_zhko.py as comparison baselines -- not needed "
                          "to run realtime_transcribe.py.")
    ap.add_argument("--lid-candidates", action="store_true",
                     help="also download whisper-base (~160MB int8), the sherpa-onnx-only LID "
                          "replacement candidate evaluated by scripts/eval_lid_candidates.py "
                          "(docs/eval/lid_candidates.md). Not needed to run realtime_transcribe.py.")

    ap.add_argument("--translate-candidates", action="store_true",
                     help="also download the source (pre-CTranslate2-conversion) Hugging Face repos "
                          "for the 4 Track B translation replacement candidates evaluated in "
                          "docs/eval/translate_candidates.md (~5GB total, none adopted -- not used by "
                          "any shipped script). These are the *upstream* transformers-format repos, "
                          "not ready-to-use CTranslate2 models: run the ct2-transformers-converter "
                          "commands in that doc's 'Candidates and setup' section afterwards (needs "
                          "torch/transformers, e.g. in a separate .venv-train -- see that doc's "
                          "'Environment' section) to reproduce models/<name>-ct2/.")
    ap.add_argument("--en-parakeet-v2", action="store_true",
                     help="also download the opt-in en-only Parakeet v2 tier (~460MB, "
                          "see docs/eval/en_candidates.md and --en-tier v2). Not part of "
                          "--minimal or the default set -- en stays on v3 unless you "
                          "both download this and pass --en-tier v2.")
    ap.add_argument("--haas-ja", action="store_true",
                    help="also download the Haas-ja-v1.0 tier (~900MB), the Japanese default in CPU "
                         "mode. The launcher fetches it when starting in CPU mode.")
    ap.add_argument("--punct-4class", action="store_true",
                     help="also download the opt-in 4-class ja punctuation model "
                          "(~40MB, see docs/eval/punct_retrain.md and --punct-model "
                          "4class). Not part of --minimal or the default set -- ja "
                          "punctuation stays on the bert model unless you both "
                          "download this and pass --punct-model 4class.")
    ap.add_argument("--translators", action="store_true",
                     help="also download ja->en (FuguMT) and ja->multilingual (M2M-100) "
                          "translation models (~600MB total)")
    ap.add_argument("--whisper-dml", action="store_true",
                     help="download Kotoba-Whisper-v2.2 ONNX for DirectML iGPU mode (~500MB)")
    ap.add_argument("--campplus", action="store_true",
                    help="download CAM++ speaker embedding model for voiceprint learning (~25MB)")
    args = ap.parse_args()

    os.makedirs(MODELS_DIR, exist_ok=True)

    total_gb = "~1.1GB" if args.minimal else ("~4.1GB" if args.eval_baselines else "~3.1GB")
    if args.en_parakeet_v2:
        total_gb += " + ~460MB (--en-parakeet-v2)"
    if args.punct_4class:
        total_gb += " + ~40MB (--punct-4class)"
    print(f"hayamimi model download: this will fetch {total_gb} into {MODELS_DIR}")
    print("(see THIRD_PARTY_NOTICES.md for each model's license)\n")

    # --- ja/en core (--minimal stops after this block) ---
    download_and_extract_tarbz2(
        f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-zipformer-ja-en-reazonspeech-2025-01-17.tar.bz2",
        "sherpa-onnx-zipformer-ja-en-reazonspeech-2025-01-17",
        "ReazonSpeech k2 Zipformer (ja, primary ASR route)")

    download_and_extract_tarbz2(
        f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-whisper-tiny.tar.bz2",
        "sherpa-onnx-whisper-tiny",
        "whisper-tiny (spoken-language ID)")

    download_file(
        f"{GITHUB_RELEASES}/{ASR_TAG}/silero_vad.onnx",
        os.path.join(MODELS_DIR, "silero_vad.onnx"),
        "Silero VAD")

    # Punctuation model: only the fp32 export. The int8 export on this HF repo
    # (punct_bert.int8.onnx) was found non-functional (near-constant logits)
    # on onnxruntime 1.29 CPU EP during development -- see docs/design/punct_ja.md.
    # Skipped here to save ~90MB and avoid confusion; punct_ja.py only ever
    # loads punct_bert.onnx.
    download_hf_repo(
        "ishiki-emo/mojicast-punct-onnx",
        "mojicast-punct-onnx",
        "Japanese punctuation restoration (Mojicast/tohoku-nlp, fp32 only)",
        ignore_patterns=["*.int8.onnx"])

    if args.minimal:
        print("\n--minimal done. zh/ko/yue/EU/omnilingual ASR, speaker labels, and "
              "translation are unavailable until you re-run without --minimal.")
        download_opt_ins(args)
        return

    # --- full multilingual routing ---
    download_and_extract_tarbz2(
        f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-paraformer-zh-int8-2025-10-07.tar.bz2",
        "sherpa-onnx-paraformer-zh-int8-2025-10-07",
        "Paraformer-zh (zh ASR)")

    # IMPORTANT: must be the 2024-07-17 export. The newer 2025-09-09 export
    # was found broken during development (see docs/results/benchmarks.md) -- do not
    # substitute it.
    download_and_extract_tarbz2(
        f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        "SenseVoice small (ko/yue ASR) -- 2024-07-17 export, NOT 2025-09-09")

    download_and_extract_tarbz2(
        f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
        "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        "Parakeet TDT 0.6B v3 (en + 24 EU languages)")

    # The int8 weights live in their own "-int8-" tarball upstream; the
    # plain "300M-ctc-2025-11-12" tarball is fp32 only (model.onnx, 1.3GB)
    # and has no model.int8.onnx at all.
    extract_members_only(
        f"{GITHUB_RELEASES}/{ASR_TAG}/sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc-int8-2025-11-12.tar.bz2",
        "omnilingual-300m-ctc-int8",
        {"model.int8.onnx", "tokens.txt"},
        "Meta Omnilingual ASR 300M CTC int8 (~1600-language fallback)")

    download_file(
        f"{GITHUB_RELEASES}/{SPEAKER_TAG}/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
        os.path.join(MODELS_DIR, "campplus_sv.onnx"),
        "CAM++ speaker embedding (--speakers)")

    # pyannote segmentation-3.0 (MIT license, ~6.6MB compressed -- much smaller
    # than campplus_sv.onnx above): speaker-change/voice-activity detection
    # for the --speakers refine-pass re-diarization (scripts/diarize.py's
    # GroupDiarizer). Only used when --speakers is on; realtime_transcribe.py
    # degrades gracefully to fast-path-only labeling (majority-vote per
    # refine group, no per-turn split) if this directory is missing, so it's
    # not fatal to skip. See docs/design/diarization.md sections 2-3 and 7-8.
    download_and_extract_tarbz2(
        f"{GITHUB_RELEASES}/{SEGMENTATION_TAG}/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
        "sherpa-onnx-pyannote-segmentation-3-0",
        "pyannote segmentation-3.0 (speaker-turn detection for --speakers refine pass)")

    download_hf_repo(
        "ishiki-emo/mojicast-m2m100-ct2",
        "mojicast-m2m100-ct2",
        "M2M-100 418M CTranslate2 (ja->zh/ko translation, MIT)")

    download_hf_repo(
        "ishiki-emo/mojicast-fugumt-ja-en-ct2",
        "mojicast-fugumt-ja-en-ct2",
        "FuguMT CTranslate2 (ja->en translation, CC BY-SA 4.0 -- see THIRD_PARTY_NOTICES.md)")

    download_opt_ins(args)

    print("\nDone. Run `python scripts/realtime_transcribe.py --wav testdata/ja_test.wav` to smoke-test.")


if __name__ == "__main__":
    main()
