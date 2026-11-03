# -*- coding: utf-8 -*-
"""End-to-end Speaker Profile generation pipeline for hayamimi."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
from typing import Callable, Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.profile_builder.gemini_analyzer import DEFAULT_GEMINI_MODEL, GeminiProfileAnalyzer, load_gemini_api_key
from tools.profile_builder.local_transcribe import LocalTranscriber
from tools.profile_builder.profile_exporter import get_default_profiles_dir, load_profile, save_profile
from tools.profile_builder.profile_model import ExtractedSection, SpeakerProfile
from tools.profile_builder.voiceprint_extractor import VoiceprintExtractor
from tools.profile_builder.youtube_fetcher import (
    YouTubeFetcher,
    extract_video_id,
    format_seconds_to_timestamp,
    is_channel_url,
    is_collab_or_guest_video,
    is_playlist_url,
)

logger = logging.getLogger("hayamimi.profile_pipeline")


class CollabVideoSkippedError(Exception):
    """Raised when a video is skipped because collaboration/guest content was detected."""
    pass


class ProfileBuilderPipeline:
    """Orchestrates YouTube metadata fetching, section extraction, local transcription,
    and Gemini multimodal analysis to produce a complete speaker profile.
    Supports individual video URLs, channel URLs, and playlist URLs (batch learning).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = DEFAULT_GEMINI_MODEL,
    ):
        self.api_key = load_gemini_api_key(api_key)
        self.model_name = model_name
        self.fetcher = YouTubeFetcher()

    def run(
        self,
        youtube_url: str,
        speaker_id: Optional[str] = None,
        display_name: Optional[str] = None,
        manual_start_sec: Optional[float] = None,
        duration_sec: float = 720.0,  # 12 minutes
        max_videos: int = 10,
        existing_profile_id: Optional[str] = None,
        status_callback: Optional[Callable[[str, float, str], None]] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
        collab_filter_enabled: Optional[bool] = None,
        collab_exclude_keywords: Optional[List[str]] = None,
    ) -> SpeakerProfile:
        """Execute end-to-end profile generation or incremental training.
        If youtube_url is a channel or playlist URL, fetches videos and processes them in batch.
        Callback signature: (stage: str, progress: float, message: str)
        """
        def log(level: str, msg: str):
            logger.info("[%s] %s", level, msg)
            if log_callback:
                log_callback(level, msg)

        def report(stage: str, progress: float, msg: str):
            logger.info("[%s] (%.0f%%) %s", stage, progress * 100, msg)
            if status_callback:
                status_callback(stage, progress, msg)
            log("STEP", f"({int(progress * 100)}%) {msg}")

        target_pid = existing_profile_id or speaker_id
        existing_profile = load_profile(target_pid) if target_pid else None

        # Determine effective collab filter settings
        filter_collab = True if collab_filter_enabled is None else bool(collab_filter_enabled)
        collab_kws = collab_exclude_keywords
        if existing_profile:
            if collab_filter_enabled is None:
                filter_collab = getattr(existing_profile, "collab_filter_enabled", True)
            if collab_exclude_keywords is None:
                collab_kws = getattr(existing_profile, "collab_exclude_keywords", None)

        is_channel = is_channel_url(youtube_url)
        is_playlist = is_playlist_url(youtube_url)

        # Check if URL is a YouTube channel or playlist URL (Batch Mode)
        if is_channel or is_playlist:
            batch_kind = "再生リスト" if is_playlist else "チャンネル"
            report("batch", 0.02, f"{batch_kind}URLを検出しました。動画一覧をスキャン中: {youtube_url}")

            # Collect already processed and skipped video IDs to prevent duplicate work
            exclude_ids = set()
            if existing_profile:
                exclude_ids.update(existing_profile.processed_video_ids)
                exclude_ids.update(existing_profile.skipped_video_ids)
                for u in existing_profile.source_video_urls:
                    vid = extract_video_id(u)
                    if vid:
                        exclude_ids.add(vid)

            skipped_collab_ids: list[str] = []
            if is_playlist:
                try:
                    videos = self.fetcher.fetch_playlist_videos(
                        playlist_url=youtube_url,
                        limit=max_videos,
                        candidate_pool=50,
                        exclude_video_ids=exclude_ids,
                        log_callback=log,
                        filter_collab=filter_collab,
                        collab_keywords=collab_kws,
                        skipped_video_ids_out=skipped_collab_ids,
                    )
                except TypeError:
                    videos = self.fetcher.fetch_playlist_videos(
                        playlist_url=youtube_url,
                        limit=max_videos,
                        candidate_pool=50,
                        exclude_video_ids=exclude_ids,
                        log_callback=log,
                    )
            else:
                try:
                    videos = self.fetcher.fetch_channel_videos(
                        channel_url=youtube_url,
                        limit=max_videos,
                        candidate_pool=50,
                        exclude_video_ids=exclude_ids,
                        log_callback=log,
                        filter_collab=filter_collab,
                        collab_keywords=collab_kws,
                        skipped_video_ids_out=skipped_collab_ids,
                    )
                except TypeError:
                    videos = self.fetcher.fetch_channel_videos(
                        channel_url=youtube_url,
                        limit=max_videos,
                        candidate_pool=50,
                        exclude_video_ids=exclude_ids,
                        log_callback=log,
                    )

            if existing_profile and skipped_collab_ids:
                for sid_v in skipped_collab_ids:
                    if sid_v not in existing_profile.skipped_video_ids:
                        existing_profile.skipped_video_ids.append(sid_v)
                try:
                    save_profile(existing_profile)
                except Exception:
                    pass

            if not videos:
                if exclude_ids or skipped_collab_ids:
                    msg = f"{batch_kind}内の対象動画はすべて学習済みまたは除外済みです（学習・除外済: {len(exclude_ids) + len(skipped_collab_ids)}本、スキップ完了）。"
                else:
                    msg = f"{batch_kind}から対象動画が見つかりませんでした。"
                report("complete", 1.0, msg)
                if existing_profile:
                    return existing_profile
                raise ValueError(msg)

            total_vids = len(videos)
            report(
                "batch",
                0.05,
                f"{batch_kind}から {total_vids} 本の動画を抽出しました（重複 {len(exclude_ids)} 本・コラボ {len(skipped_collab_ids)} 本は自動除外）。一括学習を開始します...",
            )

            cur_prof = existing_profile
            successful_count = 0
            skipped_collab_in_loop = len(skipped_collab_ids)
            failed_errors = []
            consecutive_failures = 0

            for idx, vid_info in enumerate(videos):
                v_url = vid_info["url"]
                v_title = vid_info.get("title", "動画")
                v_chan = vid_info.get("channel", "")

                p_start = (idx / total_vids) * 0.95
                p_end = ((idx + 1) / total_vids) * 0.95

                def _sub_cb(stage: str, sub_prog: float, msg: str):
                    overall = p_start + (sub_prog * (p_end - p_start))
                    report(stage, overall, f"[{idx+1}/{total_vids}本目] 『{v_title[:22]}...』: {msg}")

                log("BATCH", f"========== [{idx+1}/{total_vids}本目] 『{v_title}』 の処理を開始 ==========")
                try:
                    cur_prof = self._process_single_video(
                        youtube_url=v_url,
                        speaker_id=speaker_id or (cur_prof.speaker_id if cur_prof else None),
                        display_name=display_name or (cur_prof.display_name if cur_prof else v_chan),
                        duration_sec=duration_sec,
                        existing_profile=cur_prof,
                        status_callback=_sub_cb,
                        log_callback=log_callback,
                        collab_filter_enabled=filter_collab,
                        collab_exclude_keywords=collab_kws,
                    )
                    successful_count += 1
                    consecutive_failures = 0
                except CollabVideoSkippedError as cse:
                    skipped_collab_in_loop += 1
                    log("WARN", f"🛡️ [{idx+1}/{total_vids}本目] 『{v_title}』 はコラボ・他者混入アーカイブのためスキップしました: {cse}")
                    if cur_prof is not None:
                        try:
                            save_profile(cur_prof)
                        except Exception:
                            pass
                except Exception as e:
                    logger.error("Error processing video %d/%d (%s): %s", idx + 1, total_vids, v_url, e, exc_info=True)
                    log("ERROR", f"[{idx+1}/{total_vids}本目] 『{v_title}』の処理中にエラーが発生しました: {e}")
                    failed_errors.append(f"『{v_title[:20]}』: {e}")
                    consecutive_failures += 1

                    # 途中保存の担保: ここまでに学習成功したプロファイルがあれば即座に永続化
                    if cur_prof is not None:
                        try:
                            saved_path = save_profile(cur_prof)
                            log("WARN", f"⚠️ ここまでに学習済みのプロファイルを安全に自動保存しました: {saved_path}")
                        except Exception as save_err:
                            logger.error("Failed to save interim profile: %s", save_err)

                    # 連続3回失敗した場合は致命的（API無効等）と判断して中断
                    if consecutive_failures >= 3:
                        log("ERROR", f"連続で {consecutive_failures} 回のエラーが発生したため、以降のバッチ処理を中断します。")
                        break

            # バッチ処理終了後の判定
            if cur_prof is not None and (successful_count > 0 or skipped_collab_in_loop > 0):
                learned_count = len(cur_prof.processed_video_ids) or len(cur_prof.source_video_urls)
                status_msg = f"🎉 {batch_kind}一括学習が完了しました！ (今回学習: {successful_count}/{total_vids}本, 累計学習: {learned_count}本, 置換ルール: {len(cur_prof.replacements)}件)"
                if skipped_collab_in_loop > 0:
                    status_msg += f" ※コラボ除外: {skipped_collab_in_loop}本"
                if failed_errors:
                    status_msg += f" ※スキップされたエラー動画: {len(failed_errors)}本"
                report("complete", 1.0, status_msg)
                return cur_prof
            elif cur_prof is not None:
                # 既存プロファイルはあるが今回の動画は1本も成功しなかった場合
                report("complete", 1.0, f"⚠️ 動画の処理に失敗しましたが、既存のプロファイルは安全に保持されています: {failed_errors[0] if failed_errors else '不明なエラー'}")
                return cur_prof
            else:
                # 1本も成功せず新規プロファイルも作成できなかった場合
                if skipped_collab_in_loop > 0 and not failed_errors:
                    raise CollabVideoSkippedError(f"{batch_kind}内の候補動画 ({skipped_collab_in_loop}本) がすべてコラボ・他者混入アーカイブとして除外されました。")
                err_detail = "; ".join(failed_errors) if failed_errors else "全動画の処理に失敗しました"
                raise RuntimeError(f"{batch_kind}の一括学習に失敗しました: {err_detail}")


        # Single video URL processing
        return self._process_single_video(
            youtube_url=youtube_url,
            speaker_id=speaker_id,
            display_name=display_name,
            manual_start_sec=manual_start_sec,
            duration_sec=duration_sec,
            existing_profile=existing_profile,
            status_callback=status_callback,
            log_callback=log_callback,
            collab_filter_enabled=filter_collab,
            collab_exclude_keywords=collab_kws,
        )

    def _process_single_video(
        self,
        youtube_url: str,
        speaker_id: Optional[str] = None,
        display_name: Optional[str] = None,
        manual_start_sec: Optional[float] = None,
        duration_sec: float = 720.0,
        existing_profile: Optional[SpeakerProfile] = None,
        status_callback: Optional[Callable[[str, float, str], None]] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
        collab_filter_enabled: Optional[bool] = None,
        collab_exclude_keywords: Optional[List[str]] = None,
    ) -> SpeakerProfile:
        """Process a single YouTube video end-to-end and merge into existing profile if given."""
        def log(level: str, msg: str):
            logger.info("[%s] %s", level, msg)
            if log_callback:
                log_callback(level, msg)

        def report(stage: str, progress: float, msg: str):
            logger.info("[%s] (%.0f%%) %s", stage, progress * 100, msg)
            if status_callback:
                status_callback(stage, progress, msg)
            log("STEP", f"({int(progress * 100)}%) {msg}")

        temp_dir = tempfile.mkdtemp(prefix="hayamimi_prof_")
        try:
            # 1. Fetch metadata
            report("metadata", 0.05, "動画メタデータを取得中...")
            metadata = self.fetcher.fetch_metadata(youtube_url)
            log("INFO", f"メタデータ取得完了: タイトル『{metadata.title}』 / チャンネル: {metadata.channel} / 長さ: {int(metadata.duration_sec)}秒")

            # 1.5 Check collaboration / guest exclusion filter
            filter_enabled = collab_filter_enabled if collab_filter_enabled is not None else (getattr(existing_profile, "collab_filter_enabled", True) if existing_profile else True)
            collab_kws = collab_exclude_keywords if collab_exclude_keywords is not None else (getattr(existing_profile, "collab_exclude_keywords", None) if existing_profile else None)
            own_chan_url = (existing_profile.youtube_channel_url if existing_profile else None) or metadata.channel_url
            own_chan_name = (existing_profile.display_name if existing_profile else None) or display_name or metadata.channel

            is_collab, collab_reason = is_collab_or_guest_video(
                metadata=metadata,
                filter_enabled=filter_enabled,
                exclude_keywords=collab_kws,
                own_channel_url=own_chan_url,
                own_channel_name=own_chan_name,
            )
            if is_collab:
                log("WARN", f"🛡️ コラボ・他者混入アーカイブとして自動除外しました: 『{metadata.title}』 ({collab_reason})")
                if existing_profile and metadata.video_id:
                    if metadata.video_id not in existing_profile.skipped_video_ids:
                        existing_profile.skipped_video_ids.append(metadata.video_id)
                raise CollabVideoSkippedError(f"{collab_reason} (タイトル: 『{metadata.title}』)")

            # Determine speaker_id and display_name
            if existing_profile:
                speaker_id = existing_profile.speaker_id
                display_name = existing_profile.display_name
                report("metadata", 0.15, f"🌱 既存プロファイル '{display_name}' に新動画を追加学習: {metadata.title}")
            else:
                if not display_name:
                    display_name = metadata.channel or metadata.title[:20]
                if not speaker_id:
                    speaker_id = SpeakerProfile.sanitize_speaker_id(display_name)
                report("metadata", 0.15, f"プロファイル生成: {metadata.title} (話者: {display_name})")

            # 2. Determine best audio section
            if manual_start_sec is not None and manual_start_sec >= 0:
                best_start = manual_start_sec
                best_end = min(metadata.duration_sec, best_start + duration_sec)
                report("section", 0.25, f"手動指定区間を採用: {format_seconds_to_timestamp(best_start)} 〜 {format_seconds_to_timestamp(best_end)}")
            else:
                report("section", 0.20, "字幕メタデータをスキャン中...")
                sub_dir = os.path.join(temp_dir, "subtitles")
                os.makedirs(sub_dir, exist_ok=True)
                sub_segments = self.fetcher.fetch_subtitles(youtube_url, sub_dir)
                log("INFO", f"字幕メタデータ取得完了: {len(sub_segments)} 件の字幕セグメント")

                report("section", 0.25, "スライディングウィンドウ法で最高発話密度区間を計算中...")
                best_start, best_end, score = self.fetcher.find_best_speech_section(
                    sub_segments,
                    video_duration_sec=metadata.duration_sec,
                    window_sec=duration_sec,
                )
                start_str = format_seconds_to_timestamp(best_start)
                end_str = format_seconds_to_timestamp(best_end)
                report("section", 0.30, f"最適区間を特定: {start_str} 〜 {end_str} (密度スコア: {score:.2f})")

            extracted_section = ExtractedSection(
                start_time=format_seconds_to_timestamp(best_start),
                end_time=format_seconds_to_timestamp(best_end),
                duration_sec=best_end - best_start,
            )

            # 3. Download audio section
            wav_path = os.path.join(temp_dir, f"{speaker_id}_raw.wav")
            report("download", 0.35, "該当区間の音声のみをピンポイント取得中...")

            def _dl_status(msg: str):
                import re
                m = re.search(r"(\d+(\.\d+)?)%", msg)
                if m:
                    pct = float(m.group(1)) / 100.0
                    overall = 0.35 + (pct * 0.10)
                    clean_dl = msg.replace("[download]", "").strip()
                    report("download", overall, f"音声区間をDL中: {clean_dl}")
                else:
                    report("download", 0.38, msg)

            self.fetcher.download_audio_section(
                url=youtube_url,
                start_sec=best_start,
                end_sec=best_end,
                output_wav_path=wav_path,
                status_callback=_dl_status,
                log_callback=log_callback,
            )

            # 3.5 Voiceprint preparation for speaker verification
            target_voiceprint = None
            vp_threshold = 0.40
            voice_extractor = VoiceprintExtractor()
            extracted_current_vp = None
            extracted_current_vp_count = 0

            if existing_profile and existing_profile.voiceprint:
                target_voiceprint = existing_profile.voiceprint
                vp_threshold = existing_profile.voiceprint_threshold or 0.40
                log("VOICE", f"既存プロファイルの話者声紋を適用 (閾値: {vp_threshold:.2f})。本人の発話のみを厳選して文字起こしします。")
            elif voice_extractor.is_available:
                report("voiceprint", 0.42, "動画内の支配的話者から初期声紋を抽出中...")
                try:
                    extracted_current_vp, extracted_current_vp_count = voice_extractor.extract_voiceprint(
                        wav_path=wav_path,
                        log_callback=log_callback,
                    )
                    if extracted_current_vp:
                        target_voiceprint = extracted_current_vp
                        vp_threshold = 0.38
                        log("VOICE", f"初期声紋を抽出完了。メイン話者の発話区間を優先抽出します。")
                except Exception as e:
                    logger.warning("Initial voiceprint extraction failed: %s", e)

            # 4. Local transcription with hayamimi ReazonSpeech
            report("transcribe", 0.45, "hayamimi (ReazonSpeech) によるテスト文字起こしを開始...")
            transcriber = LocalTranscriber()
            try:
                trans_res = transcriber.transcribe_file(
                    wav_path=wav_path,
                    target_voiceprint=target_voiceprint,
                    voiceprint_threshold=vp_threshold,
                    status_callback=lambda msg, prog: report("transcribe", 0.45 + (prog * 0.25), msg),
                    log_callback=log_callback,
                )
            finally:
                transcriber.close()

            report("transcribe", 0.70, f"文字起こし完了 ({len(trans_res.segments)} セグメント)")

            # 5. Gemini Multimodal Analysis
            report("analyze", 0.75, "Gemini API に接続しマルチモーダル分析を開始...")
            analyzer = GeminiProfileAnalyzer(
                api_key=self.api_key,
                model_name=self.model_name,
            )
            profile = analyzer.analyze(
                audio_wav_path=wav_path,
                local_asr_text=trans_res.full_text,
                metadata=metadata,
                speaker_id=speaker_id,
                display_name=display_name,
                extracted_section=extracted_section,
                existing_profile=existing_profile,
                status_callback=lambda msg: report("analyze", 0.85, msg),
                log_callback=log_callback,
            )
            profile.source_video_url = youtube_url
            if metadata.channel_url:
                profile.youtube_channel_url = metadata.channel_url
            if filter_enabled is not None:
                profile.collab_filter_enabled = bool(filter_enabled)
            if collab_kws is not None:
                profile.collab_exclude_keywords = list(collab_kws)

            # Track video ID for duplicate prevention
            vid_id = metadata.video_id or extract_video_id(youtube_url)
            if vid_id and vid_id not in profile.processed_video_ids:
                profile.processed_video_ids.append(vid_id)

            # 5.5 Extract/Save Voiceprint (CAM++ 512-dim embedding)
            report("voiceprint", 0.88, "CAM++ による話者声紋（512次元ベクトル）を確定中...")
            try:
                if extracted_current_vp is None and voice_extractor.is_available:
                    extracted_current_vp, extracted_current_vp_count = voice_extractor.extract_voiceprint(
                        wav_path=wav_path,
                        log_callback=log_callback,
                    )
                if extracted_current_vp:
                    profile.voiceprint = extracted_current_vp
                    profile.voiceprint_sample_count = extracted_current_vp_count
                    log("VOICE", f"声紋登録完了: {extracted_current_vp_count} サンプルから話者特徴量を学習しました。")
                elif not voice_extractor.is_available:
                    log("INFO", "声紋モデル (campplus_sv.onnx) が未検出のため声紋抽出はスキップされました。")
            except Exception as e:
                logger.warning("Voiceprint extraction failed: %s", e)
                log("WARN", f"声紋抽出で例外が発生したためスキップしました: {e}")

            # 6. Save Profile (with merge if growing)
            if existing_profile:
                report("save", 0.92, "既存プロファイルに新動画の知見（辞書・活舌・声紋）を統合・マージ中...")
                existing_profile.merge_profile(profile)
                final_profile = existing_profile
            else:
                final_profile = profile

            # 5.8 GPU モードかつ声紋登録済みの場合、本人の発話区間から LoRA を自動学習
            is_gpu_mode = os.environ.get("HAYAMIMI_DEVICE") == "gpu"
            if is_gpu_mode and final_profile.voiceprint:
                report("lora", 0.93, "🔥 GPU モード: 本人の声紋区間から話者 LoRA アダプタを生成中...")
                try:
                    from scripts.speaker_lora import train_profile_lora
                    pdir = get_default_profiles_dir()
                    lora_out_dir = os.path.join(pdir, f"{final_profile.speaker_id}_lora")
                    lora_ok = train_profile_lora(
                        wav_path=wav_path,
                        profile=final_profile,
                        output_dir=lora_out_dir,
                        allow_cuda=True,
                        log_callback=log_callback,
                        transcript_segments=trans_res.segments,
                    )
                    if lora_ok and os.path.isdir(lora_out_dir):
                        final_profile.lora_adapter_path = lora_out_dir
                        final_profile.lora_trained_video_count += 1
                        log("LORA", f"✔ 話者特化 LoRA アダプタの学習が完了しました！ ({lora_out_dir})")
                except Exception as le:
                    logger.warning("LoRA training error: %s", le)
                    log("WARN", f"LoRA 学習中に警告が発生しました (スキップして継続): {le}")

            report("save", 0.95, "プロファイルおよび置換辞書を保存中...")
            saved_path = save_profile(final_profile)
            v_count = len(final_profile.processed_video_ids) or len(final_profile.source_video_urls) or (1 if final_profile.source_video_url else 0)
            report("complete", 1.0, f"完了: {saved_path} (学習動画: {v_count}本, 置換ルール: {len(final_profile.replacements)}件)")
            log("SUCCESS", f"プロファイルを正常に保存しました: {final_profile.display_name} ({len(final_profile.replacements)} 件の置換ルール)")

            return final_profile

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)



def main():
    parser = argparse.ArgumentParser(description="hayamimi Speaker Profile Builder Pipeline")
    parser.add_argument("--url", required=True, help="YouTube video or stream archive URL")
    parser.add_argument("--speaker-id", default=None, help="Speaker identifier (e.g. val_channel)")
    parser.add_argument("--name", default=None, help="Display name (e.g. ヴァル閣下)")
    parser.add_argument("--start", type=float, default=None, help="Manual start offset in seconds")
    parser.add_argument("--duration", type=float, default=720.0, help="Duration in seconds (default: 720 = 12min)")
    parser.add_argument("--key", default=None, help="Gemini API Key")
    parser.add_argument("--model", default=DEFAULT_GEMINI_MODEL, help="Gemini model name")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    pipeline = ProfileBuilderPipeline(api_key=args.key, model_name=args.model)
    profile = pipeline.run(
        youtube_url=args.url,
        speaker_id=args.speaker_id,
        display_name=args.name,
        manual_start_sec=args.start,
        duration_sec=args.duration,
        status_callback=lambda stage, prog, msg: print(f"[{prog*100:.0f}%] {msg}"),
    )
    print("\n=== 生成された話者プロファイル ===")
    print(f"話者: {profile.display_name} ({profile.speaker_id})")
    print(f"活舌特徴: {', '.join(profile.articulation_traits)}")
    print(f"補正辞書 ({len(profile.replacements)} 件):")
    for r in profile.replacements:
        print(f"  {r.pattern} -> {r.replacement} ({r.reason or ''})")


if __name__ == "__main__":
    main()
