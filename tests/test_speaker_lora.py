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


class TestTrainProfileLora:
    @staticmethod
    def _run(tmp_path, segments, cuda_error=None, cuda_available=True):
        import soundfile as sf
        import scripts.speaker_lora as sl
        from tools.profile_builder.local_transcribe import TranscribedSegment

        rng = np.random.default_rng(0)
        wav = tmp_path / "a.wav"
        sf.write(str(wav), rng.uniform(-0.3, 0.3, 16000 * 30).astype(np.float32), 16000)
        segs = None if segments is None else [TranscribedSegment(*s) for s in segments]
        profile = MagicMock(voiceprint=None)
        logs = []
        with patch.object(sl, "SpeakerLoRATrainer") as trainer_cls, \
             patch.object(sl, "_cuda_execution_error", return_value=cuda_error), \
             patch.object(sl.torch.cuda, "is_available", return_value=cuda_available):
            ok = sl.train_profile_lora(
                str(wav), profile, str(tmp_path / "lora"),
                log_callback=lambda lvl, msg: logs.append((lvl, msg)),
                transcript_segments=segs,
            )
        return ok, trainer_cls, logs

    def test_labels_come_from_transcript(self, tmp_path):
        ok, trainer_cls, _ = self._run(
            tmp_path, [(0.0, 12.0, "こんにちは"), (12.0, 24.0, " "), (24.0, 30.0, "またね")]
        )
        assert ok is True
        pairs = trainer_cls.return_value.train.call_args.kwargs["training_pairs"]
        assert pairs == [(0.0, 12.0, "こんにちは"), (24.0, 30.0, "またね")]

    @pytest.mark.parametrize("segments", [None, []])
    def test_skips_without_transcript(self, tmp_path, segments):
        ok, trainer_cls, logs = self._run(tmp_path, segments)
        assert ok is False
        trainer_cls.assert_not_called()
        assert logs[-1][0] == "WARN"

    def test_skips_before_model_load_when_cuda_cannot_execute(self, tmp_path):
        ok, trainer_cls, logs = self._run(
            tmp_path, [(0.0, 12.0, "こんにちは")], cuda_error="no kernel image"
        )
        assert ok is False
        trainer_cls.assert_not_called()
        assert "no kernel image" in logs[-1][1]


def test_pipeline_has_no_undefined_names():
    """pipeline.py の LoRA 分岐は GPU 実行時にしか通らないため、未定義名を静的に検出する"""
    import builtins
    import symtable
    import tools.profile_builder.pipeline as pipeline

    with open(pipeline.__file__, encoding="utf-8") as f:
        top = symtable.symtable(f.read(), pipeline.__file__, "exec")
    known = set(vars(pipeline)) | set(dir(builtins))

    def walk(table):
        for sym in table.get_symbols():
            if sym.is_global() and sym.is_referenced():
                assert sym.get_name() in known, f"undefined name: {sym.get_name()}"
        for child in table.get_children():
            walk(child)

    walk(top)


def test_ct2_export_keeps_tokenizer_and_mel_config(tmp_path):
    """変換モデルに語彙とメル設定が無いと faster-whisper が誤った既定値で動く"""
    pytest.importorskip("ctranslate2")
    trainer = SpeakerLoRATrainer(allow_cuda=False)
    trainer.peft_model = MagicMock()
    trainer.processor = MagicMock()
    with patch("ctranslate2.converters.transformers.TransformersConverter") as conv:
        trainer.merge_and_export_ct2(str(tmp_path))
    copied = conv.call_args.kwargs["copy_files"]
    assert "tokenizer.json" in copied
    assert "preprocessor_config.json" in copied


def test_train_profile_lora_reports_failure_when_ct2_export_fails(tmp_path):
    """CT2 変換に失敗したら成功扱いにしない (推論に反映されないため)"""
    ok, logs = _run_with_export_result(tmp_path, None)
    assert ok is False
    assert logs[-1][0] == "WARN"


def _run_with_export_result(tmp_path, export_result):
    import soundfile as sf
    import scripts.speaker_lora as sl
    from tools.profile_builder.local_transcribe import TranscribedSegment

    wav = tmp_path / "a.wav"
    sf.write(str(wav), np.random.default_rng(0).uniform(-0.3, 0.3, 16000 * 5).astype(np.float32), 16000)
    logs = []
    with patch.object(sl, "SpeakerLoRATrainer") as trainer_cls, \
         patch.object(sl.torch.cuda, "is_available", return_value=False):
        trainer_cls.return_value.merge_and_export_ct2.return_value = export_result
        ok = sl.train_profile_lora(
            str(wav), MagicMock(voiceprint=None), str(tmp_path / "lora"),
            log_callback=lambda lvl, msg: logs.append((lvl, msg)),
            transcript_segments=[TranscribedSegment(0.0, 5.0, "こんにちは")],
        )
    return ok, logs
