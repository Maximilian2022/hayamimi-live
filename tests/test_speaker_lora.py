"""
tests/test_speaker_lora.py
話者特化 LoRA 学習エンジンの単体テスト
"""

import os
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from scripts.speaker_lora import SpeakerLoRATrainer


def test_speaker_lora_trainer_init():
    """SpeakerLoRATrainer がスレッド数制限とデバイス解決を正しく行うこと"""
    trainer = SpeakerLoRATrainer(
        allow_cuda=False,
        max_cpu_threads=4,
    )
    assert trainer.device.type == "cpu"
    assert trainer.threads <= 4


def test_speaker_lora_training_mocked():
    """エンコーダ出力キャッシュと LoRA 学習ループがモック環境で正常に実行されること"""
    with patch("scripts.speaker_lora.WhisperProcessor") as mock_proc_cls, \
         patch("scripts.speaker_lora.WhisperForConditionalGeneration") as mock_model_cls, \
         patch("scripts.speaker_lora.get_peft_model") as mock_peft_cls:

        mock_proc = MagicMock()
        mock_proc.return_value.input_features = MagicMock()
        mock_proc.tokenizer.return_value.input_ids = MagicMock()
        mock_proc.tokenizer.pad_token_id = 50257
        mock_proc_cls.from_pretrained.return_value = mock_proc

        mock_model = MagicMock()
        mock_model_cls.from_pretrained.return_value = mock_model

        mock_peft = MagicMock()
        mock_peft.model.model.encoder.return_value = MagicMock()
        
        # optimizer 用にパラメータを返す
        import torch
        dummy_param = torch.nn.Parameter(torch.zeros(2, 2, requires_grad=True))
        mock_peft.parameters.return_value = [dummy_param]

        # outputs.loss
        mock_outputs = MagicMock()
        mock_outputs.loss = MagicMock()
        mock_outputs.loss.item.return_value = 0.123
        mock_peft.return_value = mock_outputs
        mock_peft_cls.return_value = mock_peft

        trainer = SpeakerLoRATrainer(allow_cuda=False, max_cpu_threads=2)
        
        dummy_audio = np.zeros(16000 * 5, dtype=np.float32)
        pairs = [
            (0.0, 2.5, "テスト文1"),
            (2.5, 5.0, "テスト文2"),
        ]

        progress_calls = []
        def on_prog(epoch, total, loss):
            progress_calls.append((epoch, total, loss))

        trainer.train(
            audio_samples=dummy_audio,
            sample_rate=16000,
            training_pairs=pairs,
            epochs=2,
            on_progress=on_prog,
        )

        assert len(progress_calls) == 2
        assert mock_peft.train.called
        assert mock_peft_cls.called


def test_maybe_reexec_with_gpu_venv():
    """HAVE_TRAIN_DEPENDENCIES が False で .venv_gpu が存在する場合に再実行が試行されること"""
    from scripts.speaker_lora import _maybe_reexec_with_gpu_venv
    import scripts.speaker_lora as sl

    with patch.object(sl, "HAVE_TRAIN_DEPENDENCIES", False), \
         patch("os.path.exists", return_value=True), \
         patch("subprocess.run") as mock_run, \
         patch("sys.exit") as mock_exit:
        mock_run.return_value.returncode = 0
        _maybe_reexec_with_gpu_venv()
        assert mock_run.called
        assert mock_exit.called
