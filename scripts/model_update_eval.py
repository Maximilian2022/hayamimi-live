"""Standard test for a model update: run the pinned and the candidate revision side by side.

Problem: when an upstream repo in scripts/model_pins.json moves, someone has
to decide whether the new revision is safe to ship. This runs the same small,
fixed test on both revisions and prints a Markdown report with a verdict, so
the weekly model-updates workflow can put it straight into the issue it files.

Inputs are already in the repo, so this needs no testdata/ directory:
  * tests/golden/ja/  -- 8 FLEURS ja clips with reference text (CC BY 4.0)
  * mobile/hayamimi_core/test/fixtures/punct_ja_parity.json -- 51 sentences

Tests (the "test" field of each pin):
  punct_bert / punct_4class  punctuation F1 against the 8 references, plus how
                             many of 59 inputs come out identical
  translate_en / translate_m2m  identical outputs and chrF(old, new) over 59
                             sentences; differences are listed for a human
  faster_whisper             CER on the 8 clips (CPU, int8)
  manual                     nothing automatic; the report says so

Usage:
    python scripts/model_update_eval.py --key fugumt-ja-en --new <sha> [--out report.md]
Exit code: 0 = no regression found, 1 = needs a look, 2 = test could not run.
"""

from __future__ import annotations

import argparse
import json
import os

import sys
import tempfile
import unicodedata
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import model_pins  # noqa: E402

GOLDEN_DIR = os.path.join(ROOT, "tests", "golden", "ja")
PUNCT_FIXTURE = os.path.join(ROOT, "mobile", "hayamimi_core", "test", "fixtures", "punct_ja_parity.json")
PUNCT_MARKS = "、。？！"

# A candidate is flagged when it is worse than the pinned revision by more than this.
CER_REGRESSION = 0.01        # absolute CER (1 point)
PUNCT_F1_REGRESSION = 0.02   # absolute F1 (2 points)
CHRF_FLAG = 0.90             # old-vs-new similarity below this is a large change


def golden_clips() -> list[dict]:
    with open(os.path.join(GOLDEN_DIR, "golden.json"), encoding="utf-8") as f:
        return json.load(f)["clips"]


def fixture_sentences() -> list[str]:
    with open(PUNCT_FIXTURE, encoding="utf-8") as f:
        return [c["expected"] for c in json.load(f)["cases"]]


def strip_punct(text: str) -> str:
    return "".join(ch for ch in text if ch not in PUNCT_MARKS + "，．,.?!")


def punct_positions(text: str) -> set[tuple[int, str]]:
    """(index into the unpunctuated text, mark) for every punctuation mark."""
    out, i = set(), 0
    for ch in text:
        if ch in PUNCT_MARKS:
            out.add((i, ch))
        else:
            i += 1
    return out


def punct_f1(refs: list[str], hyps: list[str]) -> float:
    tp = fp = fn = 0
    for r, h in zip(refs, hyps):
        rp, hp = punct_positions(r), punct_positions(h)
        tp += len(rp & hp)
        fp += len(hp - rp)
        fn += len(rp - hp)
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0


def chrf(a: str, b: str, n: int = 6) -> float:
    """Character n-gram F-score (beta=2) between two strings, 0..1."""
    def grams(s, k):
        s = s.replace(" ", "")
        return Counter(s[i:i + k] for i in range(len(s) - k + 1))
    scores = []
    for k in range(1, n + 1):
        ga, gb = grams(a, k), grams(b, k)
        if not ga or not gb:
            continue
        overlap = sum((ga & gb).values())
        p, r = overlap / sum(gb.values()), overlap / sum(ga.values())
        scores.append(0.0 if p + r == 0 else 5 * p * r / (4 * p + r))
    return sum(scores) / len(scores) if scores else (1.0 if a == b else 0.0)


def ja_cer(ref: str, hyp: str) -> float:
    from make_ja_golden import ja_cer as _ja_cer
    return _ja_cer(ref, hyp)


def read_wav16k(path: str):
    """16 kHz mono 16-bit wav -> float32 array, without PyAV (whose versions vary between envs)."""
    import wave
    import numpy as np
    with wave.open(path, "rb") as f:
        if (f.getframerate(), f.getnchannels(), f.getsampwidth()) != (16000, 1, 2):
            raise ValueError(f"{path}: expected 16 kHz mono 16-bit PCM")
        data = f.readframes(f.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def fetch_snapshot(pin: dict, revision: str, workdir: str) -> str:
    from huggingface_hub import snapshot_download
    dest = os.path.join(workdir, revision[:12])
    snapshot_download(repo_id=pin["repo"], revision=revision, local_dir=dest,
                      ignore_patterns=pin.get("ignore_patterns"),
                      allow_patterns=pin.get("allow_patterns"))
    return dest


# --- tests -----------------------------------------------------------------

def run_punct(pin: dict, old_dir: str, new_dir: str, four_class: bool) -> tuple[bool, list[str]]:
    from punct_ja import PunctuatorJa, PunctuatorJa4Class
    cls = PunctuatorJa4Class if four_class else PunctuatorJa
    refs = [unicodedata.normalize("NFKC", c["reference"]) for c in golden_clips()]
    inputs = [strip_punct(r) for r in refs] + [strip_punct(s) for s in fixture_sentences()]
    outs = {}
    for name, d in (("old", old_dir), ("new", new_dir)):
        p = cls(model_dir=d)
        outs[name] = [p.restore(t) for t in inputs]
    n_ref = len(refs)
    f1_old = punct_f1(refs, outs["old"][:n_ref])
    f1_new = punct_f1(refs, outs["new"][:n_ref])
    same = sum(a == b for a, b in zip(outs["old"], outs["new"]))
    ok = f1_new >= f1_old - PUNCT_F1_REGRESSION
    lines = [
        "| 指標 | 固定版 | 候補 |",
        "|---|---|---|",
        f"| 句読点 F1（FLEURS 正解 {n_ref} 文） | {f1_old:.3f} | {f1_new:.3f} |",
        f"| 出力が一致した文 | - | {same} / {len(inputs)} |",
    ]
    lines += diff_section(inputs, outs["old"], outs["new"])
    return ok, lines


def run_translate(pin: dict, old_dir: str, new_dir: str, m2m: bool) -> tuple[bool, list[str]]:
    sentences = [c["reference"] for c in golden_clips()] + fixture_sentences()
    targets = ["zh", "ko"] if m2m else ["en"]
    lines, ok = [], True
    for tgt in targets:
        outs = {}
        for name, d in (("old", old_dir), ("new", new_dir)):
            if m2m:
                from translate_m2m import TranslatorM2M
                tr = TranslatorM2M(tgt, model_dir=d)
            else:
                from translate_ja_en import TranslatorJaEn
                tr = TranslatorJaEn(model_dir=d)
            outs[name] = [tr.translate(s) for s in sentences]
        same = sum(a == b for a, b in zip(outs["old"], outs["new"]))
        sim = sum(chrf(a, b) for a, b in zip(outs["old"], outs["new"])) / len(sentences)
        # Only outputs the candidate lost count; some inputs translate to "" on the pinned one too.
        empty = sum(1 for a, b in zip(outs["old"], outs["new"]) if a.strip() and not b.strip())
        if sim < CHRF_FLAG or empty:
            ok = False
        lines += [
            f"**ja→{tgt}**",
            "",
            "| 指標 | 値 |",
            "|---|---|",
            f"| 出力が一致した文 | {same} / {len(sentences)} |",
            f"| chrF（固定版と候補の類似度、1.0 で同一） | {sim:.3f} |",
            f"| 候補で新たに空になった出力 | {empty} |",
            "",
        ]
        lines += diff_section(sentences, outs["old"], outs["new"])
    return ok, lines


def run_faster_whisper(pin: dict, old_rev: str, new_rev: str, workdir: str) -> tuple[bool, list[str]]:
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model
    clips = golden_clips()
    res = {}
    for name, rev in (("old", old_rev), ("new", new_rev)):
        path = download_model(pin["repo"], revision=rev, cache_dir=os.path.join(workdir, "hf"))
        model = WhisperModel(path, device="cpu", compute_type="int8")
        cers = []
        for c in clips:
            segs, _ = model.transcribe(read_wav16k(os.path.join(GOLDEN_DIR, c["wav"])), language="ja", beam_size=1)
            text = "".join(s.text for s in segs)
            cers.append(ja_cer(c["reference"], text))
        res[name] = cers
        del model
    mo, mn = sum(res["old"]) / len(clips), sum(res["new"]) / len(clips)
    ok = mn <= mo + CER_REGRESSION
    lines = [
        f"| 指標 | 固定版 | 候補 |",
        "|---|---|---|",
        f"| 平均 CER（FLEURS ja {len(clips)} クリップ、CPU int8） | {mo:.4f} | {mn:.4f} |",
        "",
        "<details><summary>クリップ別 CER</summary>",
        "",
        "| clip | 固定版 | 候補 |",
        "|---|---|---|",
    ] + [f"| {c['wav']} | {a:.4f} | {b:.4f} |" for c, a, b in zip(clips, res["old"], res["new"])] + [
        "", "</details>"]
    return ok, lines


def diff_section(inputs, old, new, limit: int = 10) -> list[str]:
    diffs = [(i, o, n) for i, o, n in zip(inputs, old, new) if o != n]
    if not diffs:
        return ["", "固定版と候補の出力は全文一致しました。", ""]
    out = ["", f"<details><summary>出力が変わった文（{len(diffs)} 件中 最大 {limit} 件）</summary>", ""]
    for i, o, n in diffs[:limit]:
        out += [f"- 入力: {i}", f"  - 固定版: {o}", f"  - 候補: {n}"]
    return out + ["", "</details>", ""]


def evaluate(key: str, new_rev: str) -> tuple[int, str]:
    pin = model_pins.load_pins()[key]
    old_rev = pin["revision"]
    test = pin.get("test", "manual")
    head = [
        f"### 定型テスト: `{test}`",
        "",
        f"- 固定版: `{old_rev[:12]}` / 候補: `{new_rev[:12]}`",
        "",
    ]
    if test == "manual":
        return 1, "\n".join(head + [
            "このモデルには自動テストがありません。手元で動作と精度を確認してください。"])
    try:
        with tempfile.TemporaryDirectory(prefix="hayamimi-model-eval-") as work:
            if test == "faster_whisper":
                ok, body = run_faster_whisper(pin, old_rev, new_rev, work)
            else:
                old_dir = fetch_snapshot(pin, old_rev, work)
                new_dir = fetch_snapshot(pin, new_rev, work)
                if test in ("punct_bert", "punct_4class"):
                    ok, body = run_punct(pin, old_dir, new_dir, four_class=test == "punct_4class")
                elif test in ("translate_en", "translate_m2m"):
                    ok, body = run_translate(pin, old_dir, new_dir, m2m=test == "translate_m2m")
                else:
                    return 2, "\n".join(head + [f"未知のテスト種別です: `{test}`"])
    except Exception as e:  # report, don't crash the whole weekly run
        return 2, "\n".join(head + [
            "**テストを実行できませんでした。** 候補が壊れている（ファイル構成の変更など）可能性があります。",
            "", "```", f"{type(e).__name__}: {e}"[:2000], "```"])
    verdict = ("**判定: 問題なし** — 精度の低下は基準内です。リリースに含めてよい候補です。" if ok else
               "**判定: 要確認** — 精度の低下か大きな出力変化があります。差分を確認してください。")
    return (0 if ok else 1), "\n".join(head + [verdict, ""] + body)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key", required=True, help="entry name in scripts/model_pins.json")
    ap.add_argument("--new", required=True, help="candidate revision (commit sha)")
    ap.add_argument("--out", help="also write the Markdown report here")
    args = ap.parse_args()
    code, report = evaluate(args.key, args.new)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
