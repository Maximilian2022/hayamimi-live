"""
scripts/lora_state.py
話者 LoRA ディレクトリの状態管理。

推論エンジンとサーバーから読むため、torch などの学習用依存を持たない。
LoRA は実験的機能として既定で有効。ユーザーが無効にした場合のみ使わない。
"""

from __future__ import annotations

import json
import os

# 学習方法を変えて過去のモデルが使えなくなったら上げる。
# 1 (メタ情報なし): ラベルの言語指定なし・開始トークンのずれあり。字幕が悪化するため適用しない
LORA_FORMAT_VERSION = 2
LORA_META_FILENAME = "lora_meta.json"
LORA_DISABLED_FILENAME = "lora_disabled.json"
LORA_CT2_DIRNAME = "ctranslate2"


def write_lora_meta(lora_dir: str, **info) -> None:
    os.makedirs(lora_dir, exist_ok=True)
    with open(os.path.join(lora_dir, LORA_META_FILENAME), "w", encoding="utf-8") as f:
        json.dump({"format_version": LORA_FORMAT_VERSION, **info}, f, ensure_ascii=False, indent=2)


def is_current_format(lora_dir: str) -> bool:
    try:
        with open(os.path.join(lora_dir, LORA_META_FILENAME), encoding="utf-8") as f:
            return json.load(f).get("format_version") == LORA_FORMAT_VERSION
    except (OSError, ValueError, AttributeError):
        return False


def has_usable_model(lora_dir: str) -> bool:
    return is_current_format(lora_dir) and os.path.isdir(os.path.join(lora_dir, LORA_CT2_DIRNAME))


def is_lora_enabled(lora_dir: str) -> bool:
    return not os.path.exists(os.path.join(lora_dir, LORA_DISABLED_FILENAME))


def set_lora_enabled(lora_dir: str, enabled: bool) -> None:
    marker = os.path.join(lora_dir, LORA_DISABLED_FILENAME)
    if enabled:
        if os.path.exists(marker):
            os.remove(marker)
    else:
        with open(marker, "w", encoding="utf-8") as f:
            json.dump({"disabled_by": "user"}, f)


def should_apply_lora(lora_dir: str) -> bool:
    return has_usable_model(lora_dir) and is_lora_enabled(lora_dir)
