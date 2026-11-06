"""SenseVoice successor evaluation: current SenseVoice small vs. Fun-ASR-Nano (sherpa-onnx export).

Also scores the Omnilingual 300M CTC v1 -> v2 update on the same clips.

Question: sherpa-onnx published `sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17`
(converted from FunAudioLLM/Fun-ASR-Nano-2512, loadable with the same
`OfflineRecognizer.from_sense_voice` call). Should it replace the 2024-07-17
SenseVoice export that serves ko/yue ASR and the 5-language LID second opinion?
docs/eval/sv_candidates.md is the write-up; this script produces its numbers.

Data: testdata/fleurs_bench/<lang>/ (scripts/make_fleursset.py, FLEURS test split,
100 clips per language). Every model decodes each clip whole, without LID hints,
4 threads, int8 -- the same call shape asr_engine.py uses.

Scoring matches scripts/eval_fleurs_bench.py: en = WER, others = CER, yue
t2s-normalized with opencc, punctuation and whitespace stripped (the same
_PUNCT_RE as scripts/eval_accuracy.py). Reported as corpus rates (total edits
/ total reference length). A second CER over clips whose reference has no
digits isolates the effect of inverse text normalization: Fun-ASR-Nano writes
numbers as words (五十) where FLEURS references and SenseVoice use digits (50).

Usage:
    python scripts/eval_sv_candidates.py --model sv2024 --model nano --model omni1 --model omni2
    python scripts/eval_sv_candidates.py --report      # Markdown tables from cached results
"""
from __future__ import annotations

import argparse
import json
import os
import re
import string
import sys
import time
import unicodedata

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")
BENCH = os.path.join(ROOT, "testdata", "fleurs_bench")
LANGS = ["ko", "yue", "zh", "ja", "en"]
# Languages no specialist tier covers (live audio in them goes to Omnilingual).
# Scored as CER, as Meta reports Omnilingual: word boundaries in th are not
# marked and ar/hi spelling variants make WER noisy on 100 clips.
OMNI_ONLY_LANGS = ["hi", "th", "vi", "id", "ar", "tr"]
THREADS = 4

MODEL_DIRS = {
    "sv2024": ("sense_voice", "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"),
    "nano": ("sense_voice", "sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17"),
    "omni1": ("omni", "sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc-int8-2025-11-12"),
    "omni2": ("omni", "sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc-v2-int8-2026-02-05"),
}

_PUNCT_RE = re.compile(
    "[" + re.escape(string.punctuation) + "　-〿！-／：-＠［-｀｛-･" + "]")
try:
    from opencc import OpenCC
    _t2s = OpenCC("t2s").convert
except Exception:
    _t2s = None


def lev(a, b) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def units(lang: str, text: str):
    text = unicodedata.normalize("NFKC", text)
    if lang == "en":
        text = _PUNCT_RE.sub(" ", text.lower())
        return text.split()
    if lang == "yue" and _t2s:
        text = _t2s(text)
    return list(re.sub(r"\s+", "", _PUNCT_RE.sub("", text)))


def find_model(d: str) -> str:
    for name in ("model.int8.onnx", "model.onnx"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(d)


def build(kind: str, d: str):
    import sherpa_onnx
    if kind == "sense_voice":
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=find_model(d), tokens=os.path.join(d, "tokens.txt"),
            num_threads=THREADS, use_itn=True, language="")
    return sherpa_onnx.OfflineRecognizer.from_omnilingual_asr_ctc(
        model=find_model(d), tokens=os.path.join(d, "tokens.txt"), num_threads=THREADS)


def lid_code(tag: str) -> str:
    for c in ("yue", "ja", "en", "zh", "ko"):
        if c in (tag or ""):
            return c
    return ""


def results_path(model: str) -> str:
    return os.path.join(BENCH, f"results_svcand_{model}.json")


def run(model: str, langs: list[str]) -> None:
    import psutil
    import soundfile as sf
    kind, sub = MODEL_DIRS[model]
    d = os.path.join(MODELS, sub)
    proc = psutil.Process()
    rss0 = proc.memory_info().rss
    t = time.perf_counter()
    rec = build(kind, d)
    # one warm-up decode so lazy allocations count toward the resident figure
    s = rec.create_stream()
    s.accept_waveform(16000, np.zeros(16000, np.float32))
    rec.decode_stream(s)
    load_s = time.perf_counter() - t
    rss_mb = (proc.memory_info().rss - rss0) / 1e6
    out = {"model": model, "dir": sub, "load_s": load_s, "rss_mb": rss_mb, "clips": {}}
    if os.path.exists(results_path(model)):  # add languages to an earlier run
        with open(results_path(model), encoding="utf-8") as f:
            prev = json.load(f)
        out["clips"] = {k: v for k, v in prev["clips"].items() if v["lang"] not in langs}
        out["load_s"], out["rss_mb"] = prev["load_s"], prev["rss_mb"]
    for lang in langs:
        mdir = os.path.join(BENCH, lang)
        with open(os.path.join(mdir, "manifest.json"), encoding="utf-8") as f:
            entries = json.load(f)
        for e in entries:
            x, sr = sf.read(os.path.join(mdir, e["wav"]), dtype="float32", always_2d=False)
            if x.ndim > 1:
                x = x.mean(axis=1)
            s = rec.create_stream()
            s.accept_waveform(sr, x)
            t = time.perf_counter()
            rec.decode_stream(s)
            dt = time.perf_counter() - t
            r = s.result
            out["clips"][f"{lang}/{e['wav']}"] = {
                "lang": lang, "ref": e["ref"], "hyp": r.text, "tag": getattr(r, "lang", ""),
                "dur": len(x) / sr, "dec_s": dt,
            }
        print(f"[{model}] {lang} done")
    with open(results_path(model), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[{model}] load {load_s:.1f}s, +{rss_mb:.0f}MB RSS -> {results_path(model)}")


def summarize(res: dict) -> dict:
    per = {}
    for lang in LANGS + OMNI_ONLY_LANGS:
        clips = [c for c in res["clips"].values() if c["lang"] == lang]
        if not clips:
            continue
        edits = refl = edits_nd = refl_nd = 0
        dec = dur = 0.0
        lid_ok = 0
        for c in clips:
            r, h = units(lang, c["ref"]), units(lang, c["hyp"])
            e = lev(r, h)
            edits, refl = edits + e, refl + len(r)
            if not re.search(r"[0-9０-９]", c["ref"]):
                edits_nd, refl_nd = edits_nd + e, refl_nd + len(r)
            dec, dur = dec + c["dec_s"], dur + c["dur"]
            lid_ok += lid_code(c["tag"]) == lang
        per[lang] = {
            "n": len(clips), "err": edits / refl, "err_nodigit": edits_nd / refl_nd if refl_nd else None,
            "rtf": dec / dur, "lid": lid_ok / len(clips),
        }
    return per


def report() -> None:
    loaded = {}
    for m in MODEL_DIRS:
        p = results_path(m)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                loaded[m] = json.load(f)
    sums = {m: summarize(r) for m, r in loaded.items()}
    print("| model | " + " | ".join(f"{l} ({'WER' if l == 'en' else 'CER'})" for l in LANGS)
          + " | RTF (all) | load s | +RSS MB |")
    print("|---|" + "---|" * (len(LANGS) + 3))
    for m, s in sums.items():
        tot_dec = sum(c["dec_s"] for c in loaded[m]["clips"].values())
        tot_dur = sum(c["dur"] for c in loaded[m]["clips"].values())
        cells = [f"{s[l]['err']*100:.1f}%" if l in s else "-" for l in LANGS]
        print(f"| {m} | " + " | ".join(cells)
              + f" | {tot_dec/tot_dur:.3f} | {loaded[m]['load_s']:.1f} | {loaded[m]['rss_mb']:.0f} |")
    print("\nDigit-free references only:")
    print("| model | " + " | ".join(LANGS) + " |")
    print("|---|" + "---|" * len(LANGS))
    for m, s in sums.items():
        cells = [(f"{s[l]['err_nodigit']*100:.1f}% (n={sum(1 for c in loaded[m]['clips'].values() if c['lang']==l and not re.search(r'[0-9０-９]', c['ref']))})"
                  if l in s and s[l]["err_nodigit"] is not None else "-") for l in LANGS]
        print(f"| {m} | " + " | ".join(cells) + " |")
    print("\nLID tag accuracy (SenseVoice internal LID, no hint):")
    for m, s in sums.items():
        print(f"- {m}: " + ", ".join(f"{l} {s[l]['lid']*100:.0f}%" for l in LANGS if l in s))
    omni = {m: s for m, s in sums.items() if any(l in s for l in OMNI_ONLY_LANGS)}
    if omni:
        print("\nLanguages only Omnilingual serves (CER):")
        print("| model | " + " | ".join(OMNI_ONLY_LANGS) + " | mean |")
        print("|---|" + "---|" * (len(OMNI_ONLY_LANGS) + 1))
        for m, s in omni.items():
            vals = [s[l]["err"] for l in OMNI_ONLY_LANGS if l in s]
            cells = [f"{s[l]['err']*100:.1f}%" if l in s else "-" for l in OMNI_ONLY_LANGS]
            print(f"| {m} | " + " | ".join(cells) + f" | {sum(vals)/len(vals)*100:.1f}% |")
    if "omni1" in loaded and "omni2" in loaded:
        a, b = loaded["omni1"]["clips"], loaded["omni2"]["clips"]
        for group, langs in (("SenseVoice 5", LANGS), ("Omnilingual-only", OMNI_ONLY_LANGS)):
            better = worse = same = 0
            for k, ca in a.items():
                if k not in b or ca["lang"] not in langs:
                    continue
                lang = ca["lang"]
                ea = lev(units(lang, ca["ref"]), units(lang, ca["hyp"]))
                eb = lev(units(lang, b[k]["ref"]), units(lang, b[k]["hyp"]))
                better, worse, same = better + (eb < ea), worse + (eb > ea), same + (eb == ea)
            print(f"\nomni2 vs omni1, {group} languages: v2 better on {better}, worse on {worse}, equal on {same} clips")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", action="append", choices=list(MODEL_DIRS))
    ap.add_argument("--lang", action="append",
                    help="languages to run (default: the 5 SenseVoice languages); results are merged")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    for m in a.model or []:
        run(m, a.lang or LANGS)
    if a.report:
        report()


if __name__ == "__main__":
    main()
