"""
scripts/speaker_lora.py
話者特化 LoRA (PEFT) 高速・低負荷学習エンジン (v2.2.0)

配信者やユーザーのマイク録音、およびプロファイル連携 YouTube 動画（最新3件）から、
他プロセス（ゲーム・配信 OBS）と安全に共存する動的 GPU リソース上限
（空き VRAM 50% / 最大 2.5GB 制限、WDDM 10ms スロットリング、BELOW_NORMAL 優先度）
の下で、1〜2分以内に話者適応 LoRA アダプタを生成・継続学習します。
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
from typing import Callable, Optional

import numpy as np

try:
    import psutil
except ImportError:
    psutil = None

try:
    import torch
    from transformers import WhisperProcessor, WhisperForConditionalGeneration
    from peft import LoraConfig, get_peft_model, PeftModel
    HAVE_TRAIN_DEPENDENCIES = True
except ImportError:
    torch = None
    WhisperProcessor = None
    WhisperForConditionalGeneration = None
    LoraConfig = None
    get_peft_model = None
    PeftModel = None
    HAVE_TRAIN_DEPENDENCIES = False

logger = logging.getLogger("speaker_lora")

DEFAULT_MODEL_ID = "kotoba-tech/kotoba-whisper-v2.2"


def _maybe_reexec_with_gpu_venv():
    """CPU 仮想環境 (.venv) から実行された場合、.venv_gpu が存在すれば自動的に再実行する"""
    if HAVE_TRAIN_DEPENDENCIES:
        return
    if os.environ.get("_HAYAMIMI_LORA_REEXEC"):
        return
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    gpu_python = os.path.join(project_root, ".venv_gpu", "Scripts", "python.exe")
    if os.path.exists(gpu_python) and os.path.abspath(sys.executable) != os.path.abspath(gpu_python):
        import subprocess
        env = os.environ.copy()
        env["_HAYAMIMI_LORA_REEXEC"] = "1"
        try:
            res = subprocess.run([gpu_python] + sys.argv, env=env)
            sys.exit(res.returncode)
        except Exception as e:
            logger.warning(f"Failed to re-exec with .venv_gpu: {e}")


class SpeakerLoRATrainer:
    """
    エンコーダ出力事前キャッシュ技術 & 動的 GPU 制限を用いた超低負荷・爆速な話者 LoRA 学習クラス。
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        allow_cuda: bool = False,
        max_cpu_threads: int = 4,
        max_vram_gb: float = 2.5,
    ):
        if not HAVE_TRAIN_DEPENDENCIES:
            raise RuntimeError(
                "SpeakerLoRATrainer requires torch, transformers, and peft. "
                "GPU仮想環境 (.venv_gpu) をセットアップするか、ランチャーの「⚡ GPU環境セットアップ」を実行してください。"
            )

        self.model_id = model_id
        self.allow_cuda = allow_cuda

        # プロセス優先度を低 (BELOW_NORMAL) に設定してゲームや配信の邪魔をしない
        if psutil is not None and sys.platform == "win32":
            try:
                p = psutil.Process()
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            except Exception:
                pass

        # デバイスの決定と動的 GPU リソース上限の設定
        if self.allow_cuda and torch.cuda.is_available():
            self.device = torch.device("cuda")
            self.threads = 1

            # 他プロセス共存のための動的 VRAM 上限設定
            try:
                free_bytes, total_bytes = torch.cuda.mem_get_info()
                # 空き VRAM の 50% 以下、かつ最大 2.5GB (2560MB)
                max_alloc_bytes = min(int(free_bytes * 0.5), int(max_vram_gb * 1024 * 1024 * 1024))
                fraction = max(0.1, min(0.95, max_alloc_bytes / total_bytes))
                torch.cuda.set_per_process_memory_fraction(fraction, 0)
                logger.info(
                    f"SpeakerLoRATrainer: CUDA GPU (dGPU) 共存学習モード (VRAM上限: {max_alloc_bytes // (1024*1024)}MB / {total_bytes // (1024*1024)}MB, fraction={fraction:.2f})"
                )
            except Exception as e:
                logger.warning(f"Could not set CUDA memory fraction: {e}")
        else:
            self.device = torch.device("cpu")
            # CPU コア数に応じた動的スレッド制限 (最大 4 スレッド、または CPU コア数の 1/4)
            cpu_count = os.cpu_count() or 4
            self.threads = max(1, min(max_cpu_threads, max(1, cpu_count // 4)))
            torch.set_num_threads(self.threads)
            logger.info(
                f"SpeakerLoRATrainer: CPU 学習モード (スレッド制限: {self.threads}/{cpu_count} コア - 負荷 15% 以下)"
            )

        self.processor = None
        self.model = None
        self.peft_model = None

    def _ensure_model_loaded(self, base_lora_dir: Optional[str] = None):
        """モデルとプロセッサを遅延ロードする (既存 LoRA があれば継続学習用にロード)"""
        if self.model is None:
            logger.info(f"Loading base model {self.model_id} on {self.device}...")
            self.processor = WhisperProcessor.from_pretrained(self.model_id)
            self.model = WhisperForConditionalGeneration.from_pretrained(self.model_id)
            self.model.to(self.device)

            if base_lora_dir and os.path.exists(os.path.join(base_lora_dir, "adapter_config.json")):
                logger.info(f"Loading existing LoRA adapter from {base_lora_dir} for continuation fine-tuning...")
                self.peft_model = PeftModel.from_pretrained(self.model, base_lora_dir, is_trainable=True)
                self.peft_model.to(self.device)

    def train(
        self,
        audio_samples: np.ndarray,
        sample_rate: int,
        training_pairs: list[tuple[float, float, str]],
        epochs: int = 15,
        lr: float = 1e-3,
        lora_rank: int = 16,
        lora_alpha: int = 32,
        base_lora_dir: Optional[str] = None,
        on_progress: Optional[Callable[[int, int, float], None]] = None,
    ) -> float:
        """
        マイク音声と正解テキストペアから話者 LoRA を学習する。
        training_pairs: [(start_s, end_s, text), ...]
        戻り値: 学習所要時間 (秒)
        """
        self._ensure_model_loaded(base_lora_dir=base_lora_dir)

        # 16kHz float32 に変換
        if audio_samples.dtype == np.int16:
            audio_samples = audio_samples.astype(np.float32) / 32768.0
        elif audio_samples.dtype != np.float32:
            audio_samples = audio_samples.astype(np.float32)

        if sample_rate != 16000:
            target_len = int(len(audio_samples) * 16000 / sample_rate)
            audio_samples = np.interp(
                np.linspace(0, len(audio_samples), target_len),
                np.arange(len(audio_samples)),
                audio_samples,
            ).astype(np.float32)

        # 1. PEFT LoRA アダプタのアタッチ (新規学習時のみ)
        if self.peft_model is None:
            lora_config = LoraConfig(
                r=lora_rank,
                lora_alpha=lora_alpha,
                target_modules=["q_proj", "v_proj"],
                lora_dropout=0.05,
                bias="none",
            )
            self.peft_model = get_peft_model(self.model, lora_config)

        # 2. エンコーダ出力の事前キャッシュ (1-pass forward with torch.no_grad)
        t_cache_start = time.perf_counter()
        logger.info(f"Caching encoder outputs for {len(training_pairs)} segments...")
        cached_data = []

        with torch.no_grad():
            for start_s, end_s, ref_text in training_pairs:
                start_idx = int(start_s * 16000)
                end_idx = min(len(audio_samples), int(end_s * 16000))
                if end_idx <= start_idx:
                    continue

                chunk = audio_samples[start_idx:end_idx]
                feat = self.processor(
                    chunk, sampling_rate=16000, return_tensors="pt"
                ).input_features.to(self.device)

                # エンコーダの順伝播のみ実行して特徴量をキャッシュ
                encoder_outputs = self.peft_model.model.model.encoder(feat)

                labels = self.processor.tokenizer(
                    ref_text, return_tensors="pt"
                ).input_ids.to(self.device)
                labels = labels.masked_fill(
                    labels == self.processor.tokenizer.pad_token_id, -100
                )

                cached_data.append((encoder_outputs, labels))

        t_cache = time.perf_counter() - t_cache_start
        logger.info(f"Encoder caching completed in {t_cache:.2f}s")

        if not cached_data:
            logger.warning("No valid audio segments to train.")
            return 0.0

        # 3. LoRA デコーダのみの超軽量学習 (他プロセス共存スロットリング付き)
        effective_lr = lr if base_lora_dir is None else (lr * 0.2)  # 継続学習時は学習率を微小化
        optimizer = torch.optim.AdamW(self.peft_model.parameters(), lr=effective_lr)
        self.peft_model.train()

        t_train_start = time.perf_counter()
        avg_loss = 0.0

        for epoch in range(epochs):
            total_loss = 0.0
            for encoder_outputs, labels in cached_data:
                optimizer.zero_grad()
                outputs = self.peft_model(
                    encoder_outputs=encoder_outputs, labels=labels
                )
                loss = outputs.loss
                loss.backward()
                optimizer.step()
                total_loss += loss.item()

                # WDDM スケジューラスロットリング: 他プロセス (ゲーム/OBS) の描画キューに道を譲る
                if self.device.type == "cuda":
                    time.sleep(0.01)

            avg_loss = total_loss / len(cached_data)
            if on_progress:
                try:
                    on_progress(epoch + 1, epochs, avg_loss)
                except Exception:
                    pass

        self.peft_model.eval()
        t_train = time.perf_counter() - t_train_start
        logger.info(f"LoRA training completed in {t_train:.2f}s (Final Loss: {avg_loss:.4f})")
        return t_train

    def save_adapter(self, output_dir: str) -> None:
        """学習した LoRA アダプタを保存する"""
        if self.peft_model is None:
            raise RuntimeError("No trained LoRA model to save")
        os.makedirs(output_dir, exist_ok=True)
        self.peft_model.save_pretrained(output_dir)
        logger.info(f"LoRA adapter saved to {output_dir}")


def main():
    _maybe_reexec_with_gpu_venv()
    parser = argparse.ArgumentParser(description="Whisper LoRA Speaker Adaptation Trainer")
    parser.add_argument("--audio", required=True, help="Input wav file path")
    parser.add_argument("--transcript", required=True, help="Transcript text or json file")
    parser.add_argument("--output", required=True, help="Output directory for LoRA adapter")
    parser.add_argument("--allow-cuda", action="store_true", help="Allow CUDA GPU for training")
    parser.add_argument("--base-lora", type=str, default=None, help="Base LoRA dir for continuation training")
    parser.add_argument("--epochs", type=int, default=15, help="Training epochs")
    parser.add_argument("--threads", type=int, default=4, help="Max CPU threads")

    args = parser.parse_args()

    import soundfile as sf
    samples, sr = sf.read(args.audio)
    if len(samples.shape) > 1:
        samples = samples.mean(axis=1)

    trainer = SpeakerLoRATrainer(
        allow_cuda=args.allow_cuda,
        max_cpu_threads=args.threads,
    )

    pairs = []
    with open(args.transcript, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            text = line.strip()
            if text:
                pairs.append((float(idx * 5.0), float((idx + 1) * 5.0), text))

    def print_progress(epoch, total, loss):
        print(f"[{epoch}/{total}] Loss: {loss:.4f}", flush=True)

    trainer.train(
        audio_samples=samples,
        sample_rate=sr,
        training_pairs=pairs,
        epochs=args.epochs,
        base_lora_dir=args.base_lora,
        on_progress=print_progress,
    )

    trainer.save_adapter(args.output)
    print("Done!")


if __name__ == "__main__":
    main()
