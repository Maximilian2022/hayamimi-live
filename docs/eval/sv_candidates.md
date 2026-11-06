# SenseVoice 後継候補の評価（Fun-ASR-Nano）と Omnilingual v2 の評価

評価日: 2026-11-06 / 評価スクリプト: `scripts/eval_sv_candidates.py` / データ: FLEURS test split（`scripts/make_fleursset.py`、各言語 100 クリップ）

## 結論

| 対象 | 判断 | 理由（要点） |
|---|---|---|
| **SenseVoice → Fun-ASR-Nano** | **置き換えない** | 5 言語すべてで精度が悪化（ko は CER 8.0% → 36.7%）。言語タグを返さないため、SenseVoice が担う言語判定の役割を代替できない |
| **Omnilingual 300M v1 → v2** | **v1 のまま据え置く** | v2 の sherpa-onnx 版は語彙からタイ語・ラオス語・チベット語の母音記号などが欠けており、`<unk>` が字幕に出る。他の言語でも総合的な改善はない |

どちらも「上流が新しい版を出した＝更新すべき」ではなかった例です。採用を再検討する条件は、末尾の「再評価の条件」に書きました。

## 背景: SenseVoice が担っている 3 つの役割

`scripts/asr_engine.py` で SenseVoice（`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`）は文字起こし以外にも使われています。後継はこの 3 つ全部を見て判断する必要があります。

1. **ko・yue の文字起こし**（`SV_LANGS`）
2. **言語切り替えの 2 段目の判定**: whisper-tiny の言語判定と SenseVoice 内蔵の言語タグが一致したときだけ切り替えを確定する（`resolve_dual_confirm`、根拠は `docs/eval/lid.md`）
3. **zh と yue の振り分け**: whisper-tiny は広東語を zh と判定するため、SenseVoice の言語タグで yue を見分ける

なお、GUI ランチャーの自動取得（`download_models.py --minimal ...`）には SenseVoice も Omnilingual も含まれていません。これらが動くのは、`download_models.py` を `--minimal` なしで実行した環境（CLI 利用者など）です。

## 候補: Fun-ASR-Nano（sherpa-onnx 版）

- 配布物: `sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17.tar.bz2`（約 188MB、2026-01-05 公開）
- 元モデル: [FunAudioLLM/Fun-ASR-Nano-2512](https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512)（31 言語、中国語方言・雑音環境に強いとされる）
- 製品と同じ `OfflineRecognizer.from_sense_voice(...)` でそのまま読み込める（sherpa-onnx 1.13.6 で確認）

## 評価条件

- 各モデルにクリップ全体を 1 回で渡す。言語の指定なし、4 スレッド、int8。`asr_engine.py` と同じ呼び出し方
- 採点は `scripts/eval_fleurs_bench.py` と同じ: en は WER、それ以外は CER。句読点と空白は除去し、yue は opencc で簡体字に揃える。率はコーパス全体で計算（総編集数 ÷ 正解の総文字数）
- 「数字なし」列: 正解文に数字を含まないクリップだけの CER。Fun-ASR-Nano は数字を漢数字・単語で出す（50 → 五十）ため、その影響を除いた比較
- 計測環境: 開発機（Windows 11、CPU）。RTF は処理時間 ÷ 音声長（小さいほど速い）

## 結果 1: SenseVoice 対象の 5 言語

| モデル | ko (CER) | yue (CER) | zh (CER) | ja (CER) | en (WER) | RTF | 読み込み | メモリ増分 |
|---|---|---|---|---|---|---|---|---|
| **SenseVoice 2024-07-17（現行）** | **8.0%** | **7.7%** | **8.0%** | **7.4%** | **8.9%** | 0.013 | 4.6 秒 | 320MB |
| Fun-ASR-Nano 2025-12-17 | 36.7% | 11.9% | 10.1% | 12.4% | 14.3% | 0.012 | 5.6 秒 | 329MB |

数字を含まないクリップだけで比べた場合:

| モデル | ko | yue | zh | ja | en |
|---|---|---|---|---|---|
| SenseVoice（現行） | 5.7% (n=71) | 6.2% (n=87) | 7.5% (n=85) | 6.5% (n=57) | 8.3% (n=81) |
| Fun-ASR-Nano | 33.1% (n=71) | 10.5% (n=87) | 8.2% (n=85) | 9.9% (n=57) | 11.7% (n=81) |

数字表記の差を除いても、全言語で現行の SenseVoice が上回りました。速度とメモリはほぼ同じで、置き換えによる軽量化の利点もありません。

### Fun-ASR-Nano の問題点

1. **言語タグを返さない**: 結果の `lang` が常に空（500 クリップ中 0 件）。SenseVoice は 500 クリップすべてで正しいタグを返しました。役割 2 と 3 を代替できないため、置き換えても SenseVoice は言語判定用に残す必要があり、モデルが 1 つ増えるだけになります
2. **韓国語の出力が崩れる**: 音節の脱落に加え、韓国語に存在しない字の組み合わせが出ます。採点の問題ではなく、出力そのものの品質です

   | | 例（ko_001） |
   |---|---|
   | 正解 | 낭만주의는 괴테나 피히테, 슐레겔과 같은 작가들로부터 영향을 받은 … |
   | SenseVoice | 낭만주의  괴테나 피히태  신뢰겔과 같은 작가들로부터 영향을 받은 … |
   | Fun-ASR-Nano | 만주는 탂 피 신래 과 같은 작들 영향 받은 … |

3. **数字の正規化と句読点がない**: 「50円」が「五十円」、「9点」が「九点」になり、句読点も付きません。字幕では読みにくくなります
4. 英語は小文字だけで出力されます（同梱のサンプル音声で確認）

Fun-ASR-Nano は本来、LLM のデコーダーで文字を出すモデルです。sherpa-onnx の版は、そのうち CTC の出力だけを切り出したものと推測され、本来の性能が出ていない可能性があります（推測で、未確認）。

## 結果 2: Omnilingual 300M CTC v1 → v2

Omnilingual は、専用モデルのない言語（約 1600 言語）を受け持つ汎用モデルで、専用モデルが空を返したときの保険も兼ねています。評価したのは次の 2 つです。

- v1: `sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc-int8-2025-11-12`（現行）
- v2: `sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc-v2-int8-2026-02-05`（Meta の 2025 年 12 月の精度改善版を変換したもの）

本来の担当である、専用モデルのない 6 言語（CER）:

| モデル | hi | th | vi | id | ar | tr | 平均 |
|---|---|---|---|---|---|---|---|
| v1（現行） | 16.5% | **13.8%** | **16.0%** | 7.4% | **10.1%** | 7.5% | **11.9%** |
| v2 | **12.2%** | 21.0% | 16.8% | **7.2%** | 10.8% | **7.0%** | 12.5% |

SenseVoice 対象の 5 言語（保険として使われる場面）:

| モデル | ko | yue | zh | ja | en (WER) |
|---|---|---|---|---|---|
| v1（現行） | 14.4% | **28.1%** | **17.8%** | **22.0%** | 17.1% |
| v2 | **14.2%** | 28.6% | 21.9% | 23.8% | **16.4%** |

クリップ単位では、v2 が悪化したクリップの方が多くなりました（6 言語: 改善 180 / 悪化 290 / 同じ 130、5 言語: 改善 148 / 悪化 235 / 同じ 117）。

### v2 の不具合: 語彙からの文字の欠落

v2 の `tokens.txt` には、v1 にあった 19 文字がありません。

- タイ語: ิ（SARA I）、ุ（SARA U）、ฺ（PHINTHU）、๊（MAI TRI）
- ラオス語: 母音記号・声調記号 7 文字（ັ ິ ຶ ຸ ຼ ້ ໍ）
- チベット語: 母音記号 4 文字（ི ེ ཻ ྀ）
- その他: ゼロ幅接合子（ZWJ、一部のインド系文字の表記に使う）、ソフトハイフン、結合文字 2 つ

このためタイ語では 100 クリップ中 96 クリップで、該当の文字が `<unk>` という記号のまま出力されました（v1 では 0 件）。字幕にそのまま出るため、精度の数字以上に実害があります。

> 例（th_029）: …ซึ่งมักรักษาอ`<unk>`ณภูมณได้… （正解: …ซึ่งมักรักษาอุณหภูมิได้…）

sherpa-onnx と omnilingual-asr の Issue を検索しましたが、この件の報告は見つかりませんでした（2026-11-06 時点）。

v2 で明確に良くなったのはヒンディー語（16.5% → 12.2%）だけでした。

## 再評価の条件

- **Fun-ASR-Nano**: 言語タグを出力する版か、LLM デコーダー込みの版が sherpa-onnx に入ったとき。少なくとも ko が SenseVoice と同等にならない限り、置き換えの候補にはなりません
- **Omnilingual v2**: `tokens.txt` が修正された版が出たとき。再評価すれば、ヒンディー語の改善が使えるかを判断できます
- どちらも `python scripts/eval_sv_candidates.py --model <名前> --report` で同じ表を再現できます（下記）

## 再現手順

```bash
# 評価データ（FLEURS test、各 100 クリップ。pyarrow と fsspec が必要なので別の環境で）
python scripts/make_fleursset.py --lang all          # ja en zh ko yue
for l in hi th vi id ar tr; do python scripts/make_fleursset.py --lang $l; done

# モデル（models/ に展開。MODEL_DIRS のフォルダ名どおりに置く）
#   sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
#   sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17
#   sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc-int8-2025-11-12
#   sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc-v2-int8-2026-02-05
# 取得元: https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models

python scripts/eval_sv_candidates.py --model sv2024 --model nano --model omni1 --model omni2
python scripts/eval_sv_candidates.py --model omni1 --model omni2 \
    --lang hi --lang th --lang vi --lang id --lang ar --lang tr
python scripts/eval_sv_candidates.py --report
```

## 制約

- FLEURS は朗読音声で、実際の配信の音声（雑音、くだけた話し方、BGM）とは違います。Fun-ASR-Nano が得意とする雑音環境での比較はしていません。ただし、言語タグがないことと韓国語の出力が崩れることは、音声の条件によらない問題です
- 各言語 100 クリップです。1〜2 ポイント程度の差は誤差の範囲と考えてください
- 言語判定の評価は、クリップ全体を渡したときのタグだけです。短い区間での判定精度（`docs/eval/lid.md` の方式）は測っていません
