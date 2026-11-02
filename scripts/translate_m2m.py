"""Japanese -> multilingual (any M2M-100 target) subtitle translation module.

Uses M2M-100 418M (facebook/m2m100_418M), converted to CTranslate2 int8 by
the Mojicast project (ishiki-emo/mojicast-m2m100-ct2, MIT license).
See docs/design/translate_m2m.md for model source, license, and measured latency.

Target acceptance and validation tiers:
- Any target language present in the model's own vocabulary (i.e. one with an
  `__<lang>__` token in `shared_vocabulary.json`) is accepted --
  `is_supported_target()` checks this directly against the model files rather
  than a hardcoded allowlist, so this module works for any of M2M-100's ~100
  languages without code changes.
- Only a subset of those targets have been *measured* for translation
  quality against a reference (see `scripts/eval_translate.py` and
  docs/design/translate_m2m.md's "Validation tiers" section). `VALIDATED_TARGETS`
  tracks that subset. Constructing a `TranslatorM2M` for a target outside
  `VALIDATED_TARGETS` prints a one-line note to stderr -- it still works
  (M2M-100 is trained on all these languages), just with unmeasured quality.

Design notes (mirroring scripts/translate_ja_en.py):
- M2M-100 is a multilingual model: the source SentencePiece tokens must be
  prefixed with the source language token (__ja__) and suffixed with the
  end-of-sentence token (</s>), and translate_batch is called with
  target_prefix=[["__<lang>__"]] to select the output language. The decoded
  hypothesis starts with that same target-language token, which is stripped
  before detokenizing (this is the standard CTranslate2 M2M-100 usage
  pattern -- see https://opennmt.net/CTranslate2/guides/transformers.html#m2m-100).
- no_repeat_ngram_size=3 is used, matching Mojicast's reported setting. On
  this conversion it does NOT fully eliminate repetition (unlike the ja-en
  FuguMT module, which needed n=1) but it reliably prevents the catastrophic
  multi-second degenerate loops (e.g. 50+ repeated tokens) seen without it.
  See docs/design/translate_m2m.md for the measurements behind this choice.
- beam_size differs per target: zh uses greedy (beam_size=1), ko uses
  beam_size=4 -- matching Mojicast's reported configuration, which held up
  in measurement here (see docs/design/translate_m2m.md).
- Any exception, or empty/garbage output, falls back to returning the
  original Japanese text unchanged -- a subtitle line must never go blank.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time


try:
    from scripts.cuda_utils import setup_cuda_dll_path
    setup_cuda_dll_path()
except Exception:
    try:
        from cuda_utils import setup_cuda_dll_path
        setup_cuda_dll_path()
    except Exception:
        pass

_MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "mojicast-m2m100-ct2")

SOURCE_LANG_TOKEN = "__ja__"
EOS_TOKEN = "</s>"

# Targets with a *measured* chrF score against a FLEURS reference (see
# scripts/eval_translate.py and docs/design/translate_m2m.md's "Validation tiers"
# section). Any other M2M-100 target is still accepted (see
# is_supported_target() below) but its quality has not been checked.
VALIDATED_TARGETS = {"en", "zh", "zh-tw", "zh-hk", "zh-cn", "tw", "hk", "cn", "ko", "es"}

# Regional variants of Chinese mapped to OpenCC conversion configurations:
# zh-tw / tw: Traditional Chinese (Taiwan standard phrases & characters: s2twp)
# zh-hk / hk: Traditional Chinese (Hong Kong standard characters: s2hk)
# zh-cn / cn: Simplified Chinese (Mainland China standard, base M2M-100 zh)
CHINESE_VARIANTS = {
    "zh-tw": "s2twp",
    "tw": "s2twp",
    "zh-hk": "s2hk",
    "hk": "s2hk",
    "zh-cn": None,
    "cn": None,
}

# Regional / colloquial aliases mapped to base M2M-100 language codes:
# pt-br / br: Brazilian Portuguese -> base M2M-100 pt
LANG_ALIASES = {
    "pt-br": "pt",
    "br": "pt",
    "pt-pt": "pt",
}

# Per-target settings, matching Mojicast's reported configuration (zh:
# greedy, ko: beam_size=4). See docs/design/translate_m2m.md for the measurements
# that confirmed these hold up on this specific model conversion. Targets not
# listed here fall back to DEFAULT_BEAM_SIZE (untuned).
BEAM_SIZE_BY_TARGET = {
    "en": 2,
    "zh": 1,
    "zh-tw": 1,
    "zh-hk": 1,
    "zh-cn": 1,
    "tw": 1,
    "hk": 1,
    "cn": 1,
    "ko": 4,
}
# Fallback beam size for a target not in BEAM_SIZE_BY_TARGET. Matches ko's
# beam_size=4 (the more cautious of the two measured settings) rather than
# zh's greedy beam_size=1, since it has not been re-measured per-target --
# see docs/design/translate_m2m.md.
DEFAULT_BEAM_SIZE = 4

_vocab_cache: "dict[str, set[str]]" = {}
_translator_cache: "dict[tuple[str, str, str, int], object]" = {}
_sp_cache: "dict[str, object]" = {}
_cache_lock = threading.Lock()


def _load_vocab(model_dir: str = _MODEL_DIR) -> "set[str]":
    """Load (and cache) the set of token strings in the model's CTranslate2 vocabulary."""
    if model_dir not in _vocab_cache:
        vocab_path = os.path.join(model_dir, "shared_vocabulary.json")
        with open(vocab_path, "r", encoding="utf-8") as f:
            tokens = json.load(f)
        _vocab_cache[model_dir] = set(tokens)
    return _vocab_cache[model_dir]


def is_supported_target(lang: str, model_dir: str = _MODEL_DIR) -> bool:
    """True if the model's vocabulary has a `__<lang>__` target-language token,
    or if lang is a supported regional Chinese variant (zh-tw, zh-hk, etc.)
    or regional alias (pt-br, br, etc.)."""
    norm = lang.lower()
    if norm in CHINESE_VARIANTS:
        return is_supported_target("zh", model_dir)
    if norm in LANG_ALIASES:
        return is_supported_target(LANG_ALIASES[norm], model_dir)
    try:
        vocab = _load_vocab(model_dir)
    except (OSError, json.JSONDecodeError):
        return False
    return f"__{norm}__" in vocab


# NOTE: Mojicast's report says no_repeat_ngram_size=3 "eliminated" repetition
# loops for their conversion. Re-measured here on filler-heavy casual lines
# (e.g. "そうそう、そうなんだよね。" and a longer hesitation-filled line):
# n=3 does NOT fully eliminate repetition (occasional "是啊,是啊!是啊?..."
# style runs remain), but it reliably prevents the catastrophic degenerate
# loops seen with n=0 -- e.g. one line ran to 50+ repeated tokens and took
# ~2.6-3.0s without it, vs. a bounded, mostly-sane output in ~0.3-0.8s with
# it. Kept at n=3 (unlike translate_ja_en.py's n=1) because a stricter value
# did not measurably improve quality further in spot checks and n=3 is the
# documented reference setting. See docs/design/translate_m2m.md.
NO_REPEAT_NGRAM_SIZE = 3
MAX_DECODING_LENGTH_CAP = 150
MAX_DECODING_LENGTH_MIN = 30
MAX_DECODING_LENGTH_PER_SOURCE_TOKEN = 6
MAX_DECODING_LENGTH_BASE = 20


class TranslatorM2M:
    """Loads the M2M-100 CTranslate2 model once and translates ja->target_lang lines."""

    def __init__(
        self,
        target_lang: str,
        model_dir: str = _MODEL_DIR,
        device: str = "cpu",
        compute_type: str = "int8",
        intra_threads: int = 0,
    ):
        norm_lang = target_lang.lower()
        self.target_lang = target_lang
        self._opencc = None

        if norm_lang in CHINESE_VARIANTS:
            base_lang = "zh"
            opencc_cfg = CHINESE_VARIANTS[norm_lang]
            if opencc_cfg:
                try:
                    from opencc import OpenCC
                    self._opencc = OpenCC(opencc_cfg)
                except Exception as e:
                    print(
                        f"note: opencc not available for {target_lang} ({e}); "
                        "falling back to standard Simplified Chinese output",
                        file=sys.stderr,
                    )
        elif norm_lang in LANG_ALIASES:
            base_lang = LANG_ALIASES[norm_lang]
        else:
            base_lang = norm_lang

        if not is_supported_target(base_lang, model_dir):
            raise ValueError(
                f"Unsupported target_lang: {target_lang!r} "
                f"(no __{base_lang}__ token in {model_dir}'s shared_vocabulary.json)"
            )
        if target_lang not in VALIDATED_TARGETS:
            print(
                f"note: {target_lang!r} is an unvalidated translation target "
                "(quality not yet measured; see docs/design/translate_m2m.md)",
                file=sys.stderr,
            )

        self.model_dir = model_dir
        self.device = device
        self.compute_type = compute_type
        self.intra_threads = intra_threads
        self._target_token = f"__{base_lang}__"
        self._beam_size = BEAM_SIZE_BY_TARGET.get(target_lang, DEFAULT_BEAM_SIZE)

        # Imported lazily so vocabulary-only helpers (is_supported_target)
        # work in environments without the translation runtime installed
        # (e.g. minimal CI).
        import ctranslate2
        import sentencepiece as spm

        self._warned_fallback = False
        with _cache_lock:
            sp_path = os.path.join(model_dir, "sentencepiece.model")
            if sp_path not in _sp_cache:
                # Windows non-ASCII path protection
                with open(sp_path, "rb") as f:
                    _sp_cache[sp_path] = spm.SentencePieceProcessor(model_proto=f.read())
            self._sp = _sp_cache[sp_path]

            cache_key = (model_dir, device, compute_type, intra_threads)
            if cache_key not in _translator_cache:
                try:
                    _translator_cache[cache_key] = ctranslate2.Translator(
                        model_dir, device=device, compute_type=compute_type, intra_threads=intra_threads
                    )
                except Exception as exc:
                    if device != "cpu":
                        print(f"[translate_m2m] CUDA translator init failed ({exc}), falling back to cpu", file=sys.stderr)
                        self.device = "cpu"
                        cache_key = (model_dir, "cpu", compute_type, intra_threads)
                        if cache_key not in _translator_cache:
                            _translator_cache[cache_key] = ctranslate2.Translator(
                                model_dir, device="cpu", compute_type=compute_type, intra_threads=intra_threads
                            )
                    else:
                        raise
            self._translator = _translator_cache[cache_key]

    def translate(self, text: str) -> str:
        """Translate a single line of Japanese text to self.target_lang.

        Returns the original text unchanged (never raises, never blanks
        the line) if input is empty/whitespace-only, or if translation
        fails or produces empty/garbage output.
        """
        if text is None:
            return text

        stripped = text.strip()
        if not stripped:
            return text

        import ctranslate2

        for attempt in range(2):
            try:
                pieces = self._sp.encode(stripped, out_type=str)
                if not pieces:
                    return text

                source_tokens = [SOURCE_LANG_TOKEN] + pieces + [EOS_TOKEN]

                # Cap decode length relative to the source length. Without this,
                # a degenerate hypothesis can run all the way to a large fixed
                # max_decoding_length, turning one bad line into a multi-second
                # stall -- unacceptable for a live subtitle pipeline.
                max_decoding_length = min(
                    MAX_DECODING_LENGTH_CAP,
                    max(
                        MAX_DECODING_LENGTH_MIN,
                        len(pieces) * MAX_DECODING_LENGTH_PER_SOURCE_TOKEN + MAX_DECODING_LENGTH_BASE,
                    ),
                )

                results = self._translator.translate_batch(
                    [source_tokens],
                    target_prefix=[[self._target_token]],
                    beam_size=self._beam_size,
                    no_repeat_ngram_size=NO_REPEAT_NGRAM_SIZE,
                    max_decoding_length=max_decoding_length,
                )
                out_tokens = results[0].hypotheses[0]
                # The hypothesis starts with the target-language token we passed
                # as target_prefix -- strip it before detokenizing.
                if out_tokens and out_tokens[0] == self._target_token:
                    out_tokens = out_tokens[1:]
                if out_tokens and out_tokens[-1] == EOS_TOKEN:
                    out_tokens = out_tokens[:-1]
                if not out_tokens:
                    return text

                translated = self._sp.decode(out_tokens).strip()
                if not translated:
                    return text

                if self._opencc:
                    try:
                        translated = self._opencc.convert(translated)
                    except Exception:
                        pass

                return translated
            except Exception as exc:
                if self.device != "cpu" and attempt == 0:
                    if not self._warned_fallback:
                        print(f"[translate_m2m] CUDA execution failed ({exc}), permanently falling back to cpu", file=sys.stderr)
                        self._warned_fallback = True
                    self.device = "cpu"
                    with _cache_lock:
                        cache_key = (self.model_dir, "cpu", self.compute_type, self.intra_threads)
                        if cache_key not in _translator_cache:
                            _translator_cache[cache_key] = ctranslate2.Translator(
                                self.model_dir, device="cpu", compute_type=self.compute_type, intra_threads=self.intra_threads
                            )
                        self._translator = _translator_cache[cache_key]
                    continue
                return text


def _smoke_test() -> None:
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    samples = [
        "本日はお集まりいただきありがとうございます。",  # business
        "今日はマジで疲れたわー。",  # casual
        "会議は午後3時から始まります。",  # business/schedule, numbers
        "このプロジェクトの予算は500万円です。",  # numbers
        "明日は雨が降ると思いますか?",  # question
        "そうそう、そうなんだよね。",  # repetition-prone casual line
    ]
    targets = ["zh", "ko"]

    translators = {}
    for lang in targets:
        print(f"Loading M2M-100 model for target={lang} from: {_MODEL_DIR}")
        t0 = time.perf_counter()
        translators[lang] = TranslatorM2M(target_lang=lang)
        load_time = time.perf_counter() - t0
        beam = BEAM_SIZE_BY_TARGET[lang]
        print(f"  loaded in {load_time:.2f}s (beam_size={beam}, no_repeat_ngram_size={NO_REPEAT_NGRAM_SIZE})")
    print()

    latencies_by_lang = {lang: [] for lang in targets}
    for line in samples:
        print(f"JA: {line!r}")
        for lang in targets:
            t0 = time.perf_counter()
            result = translators[lang].translate(line)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies_by_lang[lang].append(elapsed_ms)
            print(f"  [{lang}] [{elapsed_ms:7.1f} ms] -> {result!r}")
        print()

    for lang in targets:
        latencies = latencies_by_lang[lang]
        mean_ms = sum(latencies) / len(latencies)
        print(f"[{lang}] Mean latency: {mean_ms:.1f} ms/line over {len(latencies)} lines "
              f"(min {min(latencies):.1f} ms, max {max(latencies):.1f} ms)")


if __name__ == "__main__":
    _smoke_test()
