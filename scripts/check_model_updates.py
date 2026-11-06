"""Compare every pin in scripts/model_pins.json with upstream; file an issue per update.

Run weekly by .github/workflows/model-updates.yml. For each pinned Hugging
Face repo whose `main` has moved (or that was renamed or removed), this runs
scripts/model_update_eval.py's standard test on pinned vs. latest and opens a
GitHub issue with the result. An issue is opened once per (model, upstream
revision): the title carries both, and an existing open or closed issue with
the same title is not duplicated.

Usage:
    python scripts/check_model_updates.py              # report only
    python scripts/check_model_updates.py --issues     # also run tests and open issues (needs gh)
    python scripts/check_model_updates.py --issues --skip-tests
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import model_pins  # noqa: E402

LABEL = "model-update"
HF_API = "https://huggingface.co/api/models/{repo}"


def upstream_info(repo: str) -> dict:
    """{'id': current repo id, 'sha': main commit, 'last_modified': ..} or {'error': ..}."""
    req = urllib.request.Request(HF_API.format(repo=repo), headers={"User-Agent": "hayamimi-model-check"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # follows the 307 a renamed repo returns
            d = json.load(r)
        return {"id": d.get("id", repo), "sha": d.get("sha"), "last_modified": d.get("lastModified")}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def find_changes() -> list[dict]:
    changes = []
    for key, pin in model_pins.load_pins().items():
        info = upstream_info(pin["repo"])
        if "error" in info:
            changes.append({"key": key, "pin": pin, "kind": "unreachable", "info": info})
        elif info["id"] != pin["repo"]:
            changes.append({"key": key, "pin": pin, "kind": "moved", "info": info})
        elif info["sha"] and info["sha"] != pin["revision"]:
            changes.append({"key": key, "pin": pin, "kind": "updated", "info": info})
        print(f"{key:32} {pin['revision'][:7]} -> "
              f"{info.get('sha', '')[:7] if 'sha' in info else info.get('error')}"
              f"{'  (moved to ' + info['id'] + ')' if info.get('id') not in (None, pin['repo']) else ''}")
    return changes


def issue_title(ch: dict) -> str:
    key, info = ch["key"], ch["info"]
    if ch["kind"] == "updated":
        return f"モデル更新: {key} {ch['pin']['revision'][:7]} → {info['sha'][:7]}"
    if ch["kind"] == "moved":
        return f"モデルの移動: {key} ({ch['pin']['repo']} → {info['id']})"
    return f"モデルに到達できません: {key} ({info['error']})"


def issue_body(ch: dict, report: str | None) -> str:
    pin, info = ch["pin"], ch["info"]
    lines = [
        f"`scripts/model_pins.json` の `{ch['key']}` について、上流に変化がありました。",
        "",
        f"- リポジトリ: https://huggingface.co/{pin['repo']}",
        f"- 固定中の版: `{pin['revision']}`",
    ]
    if ch["kind"] == "updated":
        lines += [
            f"- 上流の最新: `{info['sha']}`（更新日時 {info.get('last_modified')}）",
            f"- 差分: https://huggingface.co/{pin['repo']}/commits/main",
        ]
    elif ch["kind"] == "moved":
        lines += [
            f"- 移動先: https://huggingface.co/{info['id']}（最新 `{info.get('sha')}`）",
            "- 今はリダイレクトで取得できますが、リダイレクトが切れると新規導入が失敗します。"
            "`repo` を移動先に書き換えてください（中身が同じなら `revision` はそのままで構いません）。",
        ]
    else:
        lines += [f"- エラー: {info['error']}（削除・非公開化・一時的な障害のいずれか）"]
    if report:
        lines += ["", report]
    lines += [
        "",
        "#### 採用する場合",
        "1. `scripts/model_pins.json` の `revision` を新しい版に書き換え、`legacy_ok` を `false` にする",
        "2. 手元で動作を確認してからリリースする（ランチャーが既存ユーザーの環境でも自動で取り直します）",
        "",
        "_この Issue は `.github/workflows/model-updates.yml` が自動で作成しました。_",
    ]
    return "\n".join(lines)


def gh(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def issue_exists(title: str) -> bool:
    out = gh("issue", "list", "--label", LABEL, "--state", "all", "--limit", "200",
             "--json", "title")
    return any(i["title"] == title for i in json.loads(out or "[]"))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--issues", action="store_true", help="open GitHub issues via gh")
    ap.add_argument("--skip-tests", action="store_true", help="do not run model_update_eval")
    args = ap.parse_args()

    changes = find_changes()
    print(f"\n{len(changes)} change(s)")
    if not args.issues:
        return

    try:
        gh("label", "create", LABEL, "--color", "5319e7",
           "--description", "上流モデルの更新（自動検出）")
    except subprocess.CalledProcessError:
        pass  # already exists

    for ch in changes:
        title = issue_title(ch)
        if issue_exists(title):
            print(f"[skip] issue exists: {title}")
            continue
        report = None
        if ch["kind"] == "updated" and not args.skip_tests:
            import model_update_eval
            print(f"[test] {ch['key']} ...")
            _, report = model_update_eval.evaluate(ch["key"], ch["info"]["sha"])
        gh("issue", "create", "--title", title, "--label", LABEL, "--body-file", "-",
           input_text=issue_body(ch, report))
        print(f"[new ] {title}")


if __name__ == "__main__":
    main()
