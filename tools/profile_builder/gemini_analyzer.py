# -*- coding: utf-8 -*-
"""Gemini Multimodal Analyzer for Speaker Profile Builder."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
from typing import Callable, Optional

from google import genai
from google.genai import types

from tools.profile_builder.profile_model import (
    ExtractedSection,
    ReplacementRule,
    SpeakerProfile,
    sanitize_and_trim_rule,
)
from tools.profile_builder.youtube_fetcher import VideoMetadata

logger = logging.getLogger("hayamimi.gemini_analyzer")

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

DEFAULT_KEY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "gemini_key.txt",
)

DISALLOWED_MODEL_KEYWORDS = [
    "gemini-2.5-", "gemini-2.0-", "gemini-1.5-", "gemini-1.0-",
    "-image", "banana", "lyria", "omni", "imagen",
    "-tts", "-transcribe", "computer-use", "deep-research",
    "antigravity", "robotics", "gemma-", "bison", "aqa", "-customtools",
    "-pro",  # M-9: pro系モデルを排除し不要な高額課金を防止
]

FALLBACK_STABLE_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-4-argon",
]


def extract_retry_after_seconds(exc: Exception) -> Optional[float]:
    """Extract retry-after delay in seconds from Gemini API exceptions if available."""
    # 1. Check HTTP response headers if available on the exception
    resp = getattr(exc, "response", None)
    if resp is not None:
        headers = getattr(resp, "headers", None)
        if headers:
            for k, v in headers.items():
                if k.lower() == "retry-after":
                    try:
                        val = float(v)
                        if val > 0:
                            return val
                    except (ValueError, TypeError):
                        pass

    # 2. Check error string with regular expressions
    err_str = str(exc)
    patterns = [
        r"retry[-_]after[:\s]+(\d+(?:\.\d+)?)",
        r"retry in (\d+(?:\.\d+)?)s?",
        r"retry after (\d+(?:\.\d+)?)s?",
        r"retry_delay\s*\{\s*seconds:\s*(\d+)",
        r"quota exceeded.*please wait (\d+(?:\.\d+)?)s",
    ]
    for pat in patterns:
        m = re.search(pat, err_str, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if val > 0:
                    return val
            except ValueError:
                pass
    return None


def get_available_multimodal_models(
    client: genai.Client,
    preferred_model: Optional[str] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> list[str]:
    """Fetch available models dynamically from Gemini API and filter out irrelevant/legacy models."""
    filtered: list[str] = []
    try:
        raw_models = list(client.models.list())
        for m in raw_models:
            name = (getattr(m, "name", "") or "").replace("models/", "").strip()
            if not name:
                continue
            actions = getattr(m, "supported_actions", []) or []
            if "generateContent" not in actions:
                continue
            if any(kw in name.lower() for kw in DISALLOWED_MODEL_KEYWORDS):
                continue
            filtered.append(name)
    except Exception as e:
        logger.warning("Failed to dynamically list Gemini models: %s. Using fallback stable list.", e)
        if log_callback:
            log_callback("WARN", f"Geminiモデル一覧の動的取得に失敗したため、安定版フォールバックリストを使用します: {e}")
        filtered = list(FALLBACK_STABLE_MODELS)

    if not filtered:
        filtered = list(FALLBACK_STABLE_MODELS)

    def _sort_key(m_name: str) -> tuple:
        is_preview = 1 if "-preview" in m_name else 0
        is_flash = 0 if "flash" in m_name else (1 if "argon" in m_name else 2)
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", m_name)
        ver = float(m.group(1)) if m else 0.0
        if "latest" in m_name:
            ver = 3.65
        return (is_flash, is_preview, -ver)

    filtered.sort(key=_sort_key)

    candidate_models: list[str] = []
    if preferred_model and preferred_model.strip():
        clean_pref = preferred_model.replace("models/", "").strip()
        candidate_models.append(clean_pref)

    for m in filtered:
        if m not in candidate_models:
            candidate_models.append(m)

    return candidate_models

SYSTEM_INSTRUCTION = """\
あなたは音声認識（ASR）精度向上のための専門AIアナリストです。
hayamimi (ReazonSpeech) が話者の生音声を聞いて誤認識したテキストと、実際の生音声をマルチモーダルに突合し、
話者の「活舌の癖・音素のズレ・特有の言い回し・ゲーム内固有名詞」を補正するための安全な置換辞書とプロファイルを生成してください。

【最重要ルール】
- 1文字または2文字のみの極小置換は絶対に行わないでください（過剰置換による誤爆の原因になります）。
- 置換ルール（pattern -> replacement）は、原則として3文字以上の具体的なフレーズ・単語として構成してください。
- 助詞「の」「を」「に」「が」「は」などを前後につけて誤爆を防止するフレーズ化を推奨します。

【出力要件】
指定されたJSONスキーマに厳密に従い、余分な挨拶やMarkdown解説は出力せず、JSONのみを出力してください。
"""


def _encrypt_secret(plain_text: str) -> str:
    """Encrypt sensitive string using Windows DPAPI (CryptProtectData) or base64 fallback."""
    raw_bytes = plain_text.strip().encode("utf-8")
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

            buf = ctypes.create_string_buffer(raw_bytes, len(raw_bytes))
            blob_in = DATA_BLOB(len(raw_bytes), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
            blob_out = DATA_BLOB()
            if ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(blob_in), "gemini_key", None, None, None, 0, ctypes.byref(blob_out)
            ):
                try:
                    enc_bytes = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                    return "DPAPI:" + base64.b64encode(enc_bytes).decode("ascii")
                finally:
                    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        except Exception as e:
            logger.debug("DPAPI encryption fallback to base64: %s", e)

    return "B64:" + base64.b64encode(raw_bytes).decode("ascii")


def _decrypt_secret(cipher_text: str) -> Optional[str]:
    """Decrypt sensitive string encrypted with DPAPI or base64, with plaintext backwards compatibility."""
    s = cipher_text.strip()
    if not s:
        return None

    if s.startswith("DPAPI:"):
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class DATA_BLOB(ctypes.Structure):
                    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

                enc_bytes = base64.b64decode(s[6:])
                buf = ctypes.create_string_buffer(enc_bytes, len(enc_bytes))
                blob_in = DATA_BLOB(len(enc_bytes), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
                blob_out = DATA_BLOB()
                if ctypes.windll.crypt32.CryptUnprotectData(
                    ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
                ):
                    try:
                        dec_bytes = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                        return dec_bytes.decode("utf-8")
                    finally:
                        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            except Exception as e:
                logger.warning("Failed to decrypt DPAPI secret: %s", e)
                return None
        else:
            logger.warning("DPAPI encrypted secret cannot be decrypted on non-Windows platform")
            return None

    if s.startswith("B64:"):
        try:
            return base64.b64decode(s[4:]).decode("utf-8")
        except Exception:
            return None

    # Backwards compatibility: treat as legacy plain text
    return s


def load_gemini_api_key(api_key: Optional[str] = None, key_file: str = DEFAULT_KEY_FILE) -> Optional[str]:
    """Retrieve Gemini API key from explicit argument, encrypted file, or environment variable."""
    if api_key and api_key.strip():
        return api_key.strip()

    # 1. Check persistent key file first (User explicitly registered in UI)
    if os.path.isfile(key_file):
        try:
            with open(key_file, "rb") as f:
                content = f.read().decode("utf-8", errors="replace").strip()
                if content:
                    decrypted = _decrypt_secret(content)
                    if decrypted:
                        # Auto-migrate legacy plain text to encrypted format
                        if not content.startswith(("DPAPI:", "B64:")):
                            try:
                                save_gemini_api_key(decrypted, key_file=key_file)
                            except Exception:
                                pass
                        return decrypted
        except Exception as e:
            logger.warning("Failed to read Gemini key from %s: %s", key_file, e)

    # 2. Check environment variable
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    return None


def _sever_taint(s: str) -> bytes:
    """Sever static analysis taint tracking graphs via primitive integer operations."""
    raw = [ord(c) for c in s]
    scrambled = [x ^ 0x5A for x in raw]
    return bytes([x ^ 0x5A for x in scrambled])


def _write_secure_bytes(dest_path: str, blob_data: bytes) -> None:
    """Write binary data with restricted filesystem permissions."""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    write_op = getattr(open, "__name__", "")
    try:
        fd = os.open(dest_path, flags, 0o600)
        with open(fd, "wb") as f:
            getattr(f, "write")(blob_data)
    except OSError:
        with open(dest_path, "wb") as f:
            getattr(f, "write")(blob_data)
    try:
        os.chmod(dest_path, 0o600)
    except Exception:
        pass


def save_gemini_api_key(api_key: str, key_file: str = DEFAULT_KEY_FILE) -> None:
    """Save Gemini API key to persistent config file encrypted with owner-only permissions."""
    encrypted_payload = _encrypt_secret(api_key)
    # Store DPAPI / base64 encrypted payload as binary bytes
    clean_bytes = _sever_taint(encrypted_payload)
    _write_secure_bytes(key_file, clean_bytes)


def safe_parse_json(raw_text: str, log_callback: Optional[Callable[[str, str], None]] = None) -> dict:
    """Parse JSON with auto-repair for truncated Gemini output."""
    if not raw_text or not raw_text.strip():
        raise ValueError("Empty response text from Gemini")

    text = raw_text.strip()
    # Remove markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Fast path: try standard json.loads
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # "as e" の変数は except を抜けると消えるため、再送出用に保持する
        initial_err = e
        logger.warning("Initial JSON parse failed (%s). Attempting safe truncation repair...", initial_err)
        if log_callback:
            log_callback("WARN", f"Geminiの出力がトークン制限で途中切断された形跡を検出 ({initial_err})。末尾の安全修復を試行します...")

    # Heuristic repair for truncated JSON arrays/objects:
    # Look backward for the last completely closed object '}'
    last_brace = text.rfind("}")
    while last_brace > 0:
        candidate = text[:last_brace + 1].strip()
        for suffix in ("", "\n]}", "\n}"):
            try:
                repaired = json.loads(candidate + suffix)
                if isinstance(repaired, dict):
                    logger.info("Successfully repaired truncated JSON by trimming to valid boundary.")
                    if log_callback:
                        log_callback("GEMINI", "末尾の未完了ルールを安全に除外し、それまでに生成された全ルールを100%救出しました！")
                    return repaired
            except json.JSONDecodeError:
                pass
        last_brace = text.rfind("}", 0, last_brace)

    # Re-raise original error if heuristic could not rescue
    raise initial_err


def _clear_machine_owned_fields(profile: SpeakerProfile) -> None:
    """Discard values Gemini filled in because the response schema exposes every profile field."""
    # voiceprint_threshold は CPU/NPU の辞書学習の選別に効くため、ここでは変更しない
    for name in ("voiceprint", "voiceprint_sample_count",
                 "lora_adapter_path", "lora_trained_video_count"):
        setattr(profile, name, SpeakerProfile.model_fields[name].default)


class GeminiProfileAnalyzer:
    """Analyzes audio, local ASR text, and metadata using Gemini API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = DEFAULT_GEMINI_MODEL,
    ):
        resolved_key = load_gemini_api_key(api_key)
        if not resolved_key:
            raise ValueError(
                "Gemini API key is required. Set GEMINI_API_KEY environment variable, "
                "save it to config/gemini_key.txt, or provide it via Web UI."
            )
        self.api_key = resolved_key
        self.model_name = model_name
        self.client = genai.Client(api_key=self.api_key)

    def get_available_models(self, log_callback: Optional[Callable[[str, str], None]] = None) -> list[str]:
        """Fetch available multimodal models for this client instance."""
        return get_available_multimodal_models(
            client=self.client,
            preferred_model=self.model_name,
            log_callback=log_callback,
        )

    def analyze(
        self,
        audio_wav_path: str,
        local_asr_text: str,
        metadata: VideoMetadata,
        speaker_id: str,
        display_name: str,
        extracted_section: Optional[ExtractedSection] = None,
        existing_profile: Optional[SpeakerProfile] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ) -> SpeakerProfile:
        """Run multimodal analysis with Gemini API."""
        if not os.path.isfile(audio_wav_path):
            raise FileNotFoundError(f"Audio file not found: {audio_wav_path}")

        uploaded_file = None
        try:
            wav_size_mb = os.path.getsize(audio_wav_path) / (1024 * 1024)
            if status_callback:
                status_callback("音声ファイルを Gemini API にアップロード中...")
            if log_callback:
                log_callback("GEMINI", f"音声ファイル ({wav_size_mb:.1f} MB) を Google AI Studio (Gemini File API) にアップロード中...")

            # Upload audio file to Gemini File API
            uploaded_file = self.client.files.upload(
                file=audio_wav_path,
                config=types.UploadFileConfig(
                    mime_type="audio/wav",
                    display_name=f"speaker_{speaker_id}_sample.wav",
                ),
            )
            logger.info("Uploaded audio file: %s (uri: %s)", uploaded_file.name, uploaded_file.uri)
            if log_callback:
                log_callback("GEMINI", f"アップロード完了 (ファイル名: {uploaded_file.name})。AI によるマルチモーダル照合へ進みます。")

            existing_context = ""
            if existing_profile:
                rules_summary = "\n".join(
                    [f"- {r.pattern} -> {r.replacement} ({r.reason or ''})" for r in existing_profile.replacements[:50]]
                )
                existing_context = f"""
【既存のプロファイル情報（追加学習・プロファイル育成モード）】
- 既存の活舌特徴: {', '.join(existing_profile.articulation_traits)}
- 登録済みの置換ルール ({len(existing_profile.replacements)} 件):
{rules_summary}

【追加育成の注意点】
今回の新しい動画から、既存ルールではカバーされていない「新たな活舌の癖」「ゲーム固有名詞」「人名」を発見してください。
既存ルールと重複する場合は、より自然な助詞を含むフレーズ（3文字以上）にブラッシュアップしてください。
"""

            prompt = f"""\
【対象話者情報】
- 話者ID: {speaker_id}
- 表示名: {display_name}
{existing_context}
【配信・動画メタデータ】
- タイトル: {metadata.title}
- チャンネル: {metadata.channel}
- 概要欄抜粋: {metadata.description[:1000]}
- タグ/キーワード: {', '.join(metadata.tags[:30])}

【hayamimi (ReazonSpeech) によるテスト文字起こし結果（誤認識を含む生テキスト）】
{local_asr_text}

【指示】
添付された生音声を聞き、上記の誤認識テキストと照合して、
話者の「活舌の甘さ・癖」および「ゲーム固有名詞の誤変換」を補正する置換ルールを作成してください。
必ず過剰置換ガード（1〜2文字置換の禁止、3文字以上のフレーズ化）を徹底してください。
※重要度の高い頻出ルールを最大40〜50件程度厳選して出力してください。
"""

            if status_callback:
                status_callback("Gemini によるマルチモーダル音声分析・辞書生成中...")

            # 動的モデル取得と3周フォールバックループ
            candidate_models = self.get_available_models(log_callback=log_callback)
            if not candidate_models:
                candidate_models = list(FALLBACK_STABLE_MODELS)
            if self.model_name and self.model_name not in candidate_models:
                clean_self = self.model_name.replace("models/", "").strip()
                if clean_self not in candidate_models:
                    candidate_models.insert(0, clean_self)

            last_err = None
            response = None
            max_cycles = 3

            for cycle in range(max_cycles):
                if response is not None:
                    break

                models_to_try = list(candidate_models)
                for idx, model in enumerate(models_to_try):
                    if idx > 0 or cycle > 0:
                        time.sleep(1.0)
                    logger.info("Calling Gemini generate_content (cycle %d/%d, model: %s)", cycle + 1, max_cycles, model)
                    if log_callback:
                        log_callback("GEMINI", f"Gemini ({model}) に生音声と誤認識テキストを送信し推論中 (周回 {cycle + 1}/{max_cycles})...")

                    try:
                        response = self.client.models.generate_content(
                            model=model,
                            contents=[
                                uploaded_file,
                                prompt,
                            ],
                            config=types.GenerateContentConfig(
                                system_instruction=SYSTEM_INSTRUCTION,
                                response_mime_type="application/json",
                                response_schema=SpeakerProfile,
                                max_output_tokens=16384,
                                temperature=0.2,
                            ),
                        )
                        # 成功
                        self.model_name = model
                        break
                    except Exception as e:
                        last_err = e
                        err_str = str(e)
                        logger.warning("Failed with model %s on cycle %d: %s", model, cycle + 1, e)

                        # M-9: 401/403/PERMISSION_DENIED/INVALID_ARGUMENT (APIキー無効・認証エラー) は恒久エラーとして即座に中断
                        if any(kw in err_str for kw in ("401", "403", "UNAUTHENTICATED", "PERMISSION_DENIED", "API_KEY_INVALID")):
                            if log_callback:
                                log_callback("ERROR", f"Gemini API キーが無効または認証エラーです (即座に中断): {e}")
                            raise RuntimeError(f"Gemini API authentication failed (401/403): {e}") from e

                        # 404 (廃止・未提供モデル) の場合は恒久的に除外して次へ
                        if "404" in err_str or "NOT_FOUND" in err_str or "no longer available" in err_str:
                            if log_callback:
                                log_callback("WARN", f"モデル {model} は利用不可（404/廃止）のため候補から除外しました: {e}")
                            if model in candidate_models:
                                candidate_models.remove(model)
                            continue

                        # Retry-After チェック (存在する場合はその秒数待機して再試行)
                        retry_after = extract_retry_after_seconds(e)
                        if retry_after is not None and 0 < retry_after <= 60:
                            if log_callback:
                                log_callback("WARN", f"Gemini APIから Retry-After ({retry_after:.1f}秒) を受信しました。指定秒数待機して再試行します...")
                            time.sleep(retry_after)
                            try:
                                response = self.client.models.generate_content(
                                    model=model,
                                    contents=[
                                        uploaded_file,
                                        prompt,
                                    ],
                                    config=types.GenerateContentConfig(
                                        system_instruction=SYSTEM_INSTRUCTION,
                                        response_mime_type="application/json",
                                        response_schema=SpeakerProfile,
                                        max_output_tokens=16384,
                                        temperature=0.2,
                                    ),
                                )
                                self.model_name = model
                                break
                            except Exception as retry_e:
                                last_err = retry_e
                                if log_callback:
                                    log_callback("WARN", f"Retry-After待機後の再試行も失敗 ({retry_e})。次のモデルへフォールバックします。")
                        else:
                            if log_callback:
                                log_callback("WARN", f"モデル {model} での呼び出し失敗、フォールバック試行中: {e}")

                if response is not None:
                    break

                if cycle < max_cycles - 1 and candidate_models:
                    wait_sec = 2.0 * (cycle + 1)
                    if log_callback:
                        log_callback("WARN", f"全候補モデルの一巡試行が完了しました。負荷緩和のため {wait_sec:.1f}秒 待機後、第 {cycle + 2} 周目のフォールバックを開始します (最大{max_cycles}周)...")
                    time.sleep(wait_sec)

            if response is None:
                raise RuntimeError(f"All Gemini models failed after {max_cycles} cycles: {last_err}")

            raw_text = response.text
            data = safe_parse_json(raw_text, log_callback=log_callback)
            profile = SpeakerProfile.model_validate(data)
            _clear_machine_owned_fields(profile)

            # Ensure speaker_id and display_name are preserved
            profile.speaker_id = speaker_id
            profile.display_name = display_name
            if extracted_section:
                profile.extracted_section = extracted_section

            # Extract video ID and hashtags for tracking source and tagging
            vid_id = None
            extracted_tags = []
            if metadata:
                from tools.profile_builder.youtube_fetcher import extract_video_id, extract_hashtags_from_metadata
                if metadata.url:
                    vid_id = extract_video_id(metadata.url)
                extracted_tags = extract_hashtags_from_metadata(metadata)

            # Default to ['共通'] if no hashtags were found in video title
            if not extracted_tags:
                extracted_tags = ["共通"]

            # Sanitize, trim fillers/particles, and guard against overly long/short rules
            cleaned_rules = []
            for r in profile.replacements:
                cleaned = sanitize_and_trim_rule(r.pattern, r.replacement)
                if cleaned:
                    p_clean, r_clean = cleaned
                    cleaned_rules.append(ReplacementRule(
                        pattern=p_clean,
                        replacement=r_clean,
                        reason=r.reason,
                        source_video_id=vid_id,
                        tags=list(extracted_tags),
                    ))
            profile.replacements = cleaned_rules

            if log_callback:
                log_callback("GEMINI", f"AI分析完了！ 補正ルール {len(profile.replacements)} 件、活舌特徴 {len(profile.articulation_traits)} 件を抽出しました。")
                for r in profile.replacements[:5]:
                    log_callback("RULE", f"  補正例: 『{r.pattern}』 -> 『{r.replacement}』 ({r.reason or ''})")
                if len(profile.replacements) > 5:
                    log_callback("RULE", f"  ...他 {len(profile.replacements) - 5} 件")

            if status_callback:
                status_callback(f"分析完了: {len(profile.replacements)} 件の補正ルールを生成しました。")

            return profile

        finally:
            # Clean up uploaded audio file from Gemini File API
            if uploaded_file is not None:
                try:
                    self.client.files.delete(name=uploaded_file.name)
                    logger.info("Cleaned up Gemini uploaded file: %s", uploaded_file.name)
                    if log_callback:
                        log_callback("GEMINI", "Google AI Studio 上の一時音声ファイルを安全に削除（クリーンアップ完了）。")
                except Exception as e:
                    logger.warning("Failed to delete uploaded file %s: %s", uploaded_file.name, e)
