#!/usr/bin/env python3
"""
슬랙에서 고쳐지거나 지워진 것을 **번호로 골라** 아카이브에 반영한다.

자동 반영이 매일 찾아 자료 저장소의 `slack-export/.pending-edits.json` 에 적어 두고 DM 으로
알린다. 거기에는 **`before`·`after` 가 이미 들어 있는데**, 예전에는 사람이 그 값을 손으로
임시 파일에 옮겨 적고 `insert_messages.py` 의 알맞은 모드를 골라 부르는 일을 건마다 반복했다
(2026-08-10 에 2건 반영하는 데 그 과정을 그대로 밟았다). 이 스크립트가 그 사이를 잇는다.

사용:
  python apply_edits.py                                   # 목록만 (아무것도 안 고친다)
  python apply_edits.py --only 1,2 --dry-run              # 바뀔 자리만 본다
  python apply_edits.py --only 1,2 --apply                # 반영한다
  python apply_edits.py --only 2 --later 14 --reason "판정 불가"   # 미뤄 둔다
  python apply_edits.py --clear 2                         # 보류 취소

종료코드: 0 성공 / 1 실패·못 맞춘 토큰

── 지키는 것 ──

  · **`--apply` 없이는 아무것도 안 고친다.** 목록만 찍고 끝난다 (doc-archive 와 같은 규약).
  · **`--expect-file` 로 지금 md 와 대조한 뒤에만 손댄다.** 같은 분·같은 사람 헤더가
    아카이브에 118건(57곳·파일 20개, 2026-09-22 실측) 있어 헤더만으로는 어느 블록인지 못 고른다. 안 맞으면 그 건은 멈춘다.
  · **못 맞춘 토큰을 조용히 넘기지 않는다.** 오타 하나로 안 고쳐진 건이 생기면, 슬랙과
    아카이브가 다른 채로 아무 표시 없이 남는다.
  · **`manual` 표시가 붙은 건은 골라도 거부한다.** 본문이 헤더 줄에 있는 「한 줄로 줄여 적은
    메시지」라 `--replace-body` 가 헤더는 그대로 두고 아래에 본문을 새로 넣는다.
  · **보류에는 만기가 붙는다.** 만기 없는 보류는 삭제와 같다 (위키 `[[적어둔-일은-돌아오지-않는다]]`).
    만기가 지나면 저절로 목록으로 돌아오고 09:00 위생 점검이 다시 알린다.
  · **반영에 성공한 건은 `.pending-edits.json` 에서 그 자리에서 뺀다** (`drop_pending`).
    감지는 07:00 회차에만 돌아서, 안 빼면 이튿날 아침까지 09:00 위생 점검과 `archive-run`
    상황판이 이미 끝낸 일을 「미반영 N건 · N일째」라고 부른다 — 2026-08-20·2026-08-25 에
    두 번 났다. 실패한 건은 남긴다.
  · 반영 뒤 `sync_index.py` 로 헤더 건수를 다시 맞춘다 — 메시지를 지우면 건수가 바뀐다.

이 스크립트는 **사람이 대화형으로만** 부른다. VM 은 부르지 않는다 — 반영할지는 판단이다.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import unicodedata
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "doc-archive" / "scripts"))

from sync_index import ARCHIVE, ROOT, STATE  # noqa: E402
# 만기 판정은 **문서 쪽과 같은 함수를 그대로 쓴다.** 옮겨 적으면 갈리고, 갈리면 한쪽은
# 「N건 남음」 한쪽은 「0건」이라고 알린다 (fetch_slack_files.py 의 docstring 참조).
from fetch_slack_files import active_deferred, today_kst  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PENDING = ARCHIVE / ".pending-edits.json"
CHANNELS = ARCHIVE / "channels"
INSERT = _HERE / "insert_messages.py"
SYNC = _HERE / "sync_index.py"
DEFAULT_LATER_DAYS = 14

DEFERRED_COMMENT = (
    "반영을 미룬 슬랙 수정·삭제. `until` 이 만기이고, 만기가 지나면 저절로 목록으로 "
    "돌아와 09:00 위생 점검이 다시 알린다. 만기 없는 보류는 삭제와 같아서 필수로 뒀다. "
    "`.pending-edits.json` 이 아니라 여기 두는 이유: 그 파일은 자동 반영이 회차마다 "
    "통째로 다시 써서 여기 적으면 다음 07:00 에 지워진다."
)


# ── 표 (한글 폭) — review_batch.py 에서 가져왔다 ─────────────────────────

def _w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, n):
    s = str(s)
    if _w(s) <= n:
        return s + " " * (n - _w(s))
    out = ""
    for c in s:
        if _w(out) + _w(c) > n - 1:
            break
        out += c
    return out + "…" + " " * max(0, n - _w(out) - 1)


def table(headers, rows, caps):
    cols = [
        min(caps[i], max(_w(h), *(_w(r[i]) for r in rows)) if rows else _w(h))
        for i, h in enumerate(headers)
    ]
    out = ["  ".join(_pad(h, w) for h, w in zip(headers, cols)).rstrip()]
    for r in rows:
        out.append("  ".join(_pad(c, w) for c, w in zip(r, cols)).rstrip())
    return "\n".join(out)


def select(items, tokens):
    """`--only` 로 고른 것만. (고른 목록, 아무것도 못 맞춘 토큰).

    번호는 화면에 찍힌 순서(1부터)다. `--only 2,4` 도 `--only 2 --only 4` 도 된다.
    숫자가 아니면 채널명·키 조각으로 본다.

    **못 맞춘 토큰을 조용히 넘기지 않는다** — 오타 하나로 안 고쳐진 건이 생기면 슬랙과
    아카이브가 다른 채로 아무 표시 없이 남는다. (apply_approvals.py 의 select 와 같은 규약)
    """
    picked, unmatched = [], []
    for token in [t.strip() for raw in tokens for t in str(raw).split(",") if t.strip()]:
        if token.isdigit():
            i = int(token)
            hits = [items[i - 1]] if 1 <= i <= len(items) else []
        else:
            low = token.lower()
            hits = [r for r in items
                    if low in str(r.get("channel", "")).lower() or low in str(r.get("key", "")).lower()]
        if not hits:
            unmatched.append(token)
        for r in hits:
            if r not in picked:
                picked.append(r)
    return picked, unmatched


# ── 상태 ────────────────────────────────────────────────────────────────

def load_json(p, fallback):
    """읽기 전용으로만 쓴다. **되돌려 쓸 파일에는 쓰지 말 것** — load_state 를 쓴다."""
    try:
        return json.loads(p.read_bytes().decode("utf-8"))
    except Exception:
        return fallback


def load_state():
    """`.sync-state.json` 을 읽는다. **못 읽으면 멈춘다.**

    예전에는 `load_json(STATE, {})` 로 읽어 실패하면 `{}` 를 돌려줬고, `--later`·`--clear` 가
    그 빈 값을 그대로 덮어썼다. 한 번의 읽기 실패로 48개 채널의 `last_ts` 가 통째로 사라지고,
    그러면 자동 반영이 `.sync-state.json 을 읽지 못했습니다` 로 **영영** 멈춘다. 되돌릴 수 없는
    값이라 fail-closed 로 둔다 (2026-08-10).
    """
    try:
        return json.loads(STATE.read_bytes().decode("utf-8"))
    except Exception as e:
        raise SystemExit(
            f"✗ 상태 파일을 읽지 못했습니다 — {STATE}\n  {e}\n"
            "  이 파일에는 채널별 마지막 동기화 시각이 들어 있어 덮어쓰면 되돌릴 수 없습니다. "
            "아무것도 하지 않았습니다."
        )


def save_state(state):
    """`ingest/util.js` 의 writeJson 과 **같은 모양**으로 쓴다 (indent 2 · 끝 개행 · LF).
    JS 와 파이썬이 번갈아 써도 diff 가 안 부풀도록.

    tmp 에 쓰고 바꿔 끼운다 — 중간에 죽으면 반쪽짜리 상태 파일이 남고, 그건 위 load_state 가
    막는 그 상황을 스스로 만드는 것이다.
    """
    if not state.get("channels"):
        raise SystemExit(
            "✗ 상태에 channels 가 비어 있어 쓰지 않았습니다 — 덮어쓰면 동기화 기준점이 사라집니다."
        )
    body = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    tmp = STATE.with_suffix(STATE.suffix + ".tmp")
    tmp.write_bytes(body.encode("utf-8"))
    tmp.replace(STATE)


def pending_items():
    """반영 안 된 것 중 **만기가 안 지난 보류를 뺀** 목록. 채널·키 순으로 번호를 고정한다."""
    items = load_json(PENDING, {}).get("items", [])
    # 여기도 fail-closed 다. 상태를 못 읽어 `{}` 로 넘어가면 **사람이 「나중에」로 미뤄 둔 것이
    # 살아 있는 후보로 되살아나고**, 그 상태에서 `--only N --apply` 를 치면 미뤄 둔 것을 반영한다.
    held = active_deferred(load_state(), today_kst())
    items = [it for it in items if it.get("id") not in held]
    items.sort(key=lambda r: (str(r.get("channel", "")), str(r.get("key", ""))))
    return items


# ── 반영 ────────────────────────────────────────────────────────────────

def run_insert(args, dry):
    cmd = [sys.executable, str(INSERT), *args] + (["--dry-run"] if dry else [])
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0, (r.stdout or "").strip() or (r.stderr or "").strip()


def apply_one(item, tmpdir, dry):
    """한 건을 반영. (성공 여부, 설명)"""
    # 갈래 ③ (2026-09-22) — 같은 1분에 글이 여럿이라 기계가 어느 블록인지 못 고르는 건.
    # manual 검사보다 먼저 가른다: 같은 manual=true 라도 사람이 할 일이 다르다
    # (압축 건은 「풀어 쓰기」, 이 건은 「원문과 대보고 고치기」).
    if item.get("kind") == "ambiguous":
        return False, ("같은 1분에 글이 여럿이라 기계가 못 짚는 건입니다 — 그 분의 md 블록을 "
                       "슬랙 원문과 직접 대보고 고치면 다음 대조에서 저절로 빠집니다")
    if item.get("manual"):
        return False, ("한 줄로 줄여 적은 메시지라 스크립트로 못 고칩니다 — "
                       "Edit 으로 '헤더 줄 + 본문 줄' 형태로 풀어 쓰면 다음 대조에서 빠집니다")

    md = CHANNELS / f"{item.get('file') or item.get('channel')}.md"
    if not md.exists():
        return False, f"채널 md 가 없습니다: {md.name}"

    def put(name, text):
        p = Path(tmpdir) / name
        p.write_bytes((str(text).rstrip("\n") + "\n").encode("utf-8"))
        return str(p)

    base = ["--file", str(md), "--header", item["header"]]
    expect = put("before.txt", item.get("before", ""))
    kind, scope = item.get("kind"), item.get("scope")

    if kind == "edited" and scope == "message":
        args = [*base, "--replace-body", "--expect-file", expect,
                "--content-file", put("after.txt", item.get("after", ""))]
    elif kind == "deleted" and scope == "message":
        args = [*base, "--delete-message", "--expect-file", expect]
    elif kind == "edited" and scope == "reply":
        args = [*base, "--replace-reply", "--reply-key", item["replyKey"],
                "--expect-file", expect, "--content-file", put("after.txt", item.get("after", ""))]
    elif kind == "deleted" and scope == "reply":
        args = [*base, "--delete-reply", "--reply-key", item["replyKey"], "--expect-file", expect]
    else:
        return False, f"알 수 없는 종류입니다 (kind={kind}, scope={scope})"

    return run_insert(args, dry)


def drop_pending(ids):
    """반영에 **성공한** 건을 `.pending-edits.json` 에서 뺀다. (뺀 개수 / 못 했으면 None)

    감지는 07:00 회차에만 돈다 — 17:00 은 수정·삭제 대조를 끄고 이 파일에 손대지 않는다.
    그래서 여기서 안 빼면 반영이 끝난 뒤에도 **이튿날 아침까지** 09:00 위생 점검과
    `archive-run` 상황판이 「미반영 N건 · N일째」라고 말한다. 둘 다 이 파일 하나를 읽기
    때문이다 (`archive-health.js` 의 `stalePendingEdits`, `board.py` 의 `probe_edits`).
    2026-08-20 과 2026-08-25 에 실제로 두 번 났고, 사람이 이미 한 일을 다시 하러 갔다.

    다음 07:00 이 이 파일을 통째로 다시 쓰므로 여기서 빼도 감지 결과와 어긋나지 않는다 —
    고친 것은 본문이 같아져 어차피 그때 빠진다. 앞당길 뿐이다.

    **남은 것이 없으면 파일을 지운다** (`slack-archive.js` 의 쓰기와 같은 규약 —
    "빈 목록을 남겨 두면 볼 것이 있다로 읽힌다"). 모양도 그쪽 `writeJson` 과 맞춘다.

    **못 써도 멈추지 않는다.** md 는 이미 고쳐졌고 여기서 죽으면 그 사실이 화면에서
    사라진다. 못 빼야 유령이 하루 남을 뿐이고, 그건 이 함수가 없던 때와 같은 상태다.
    대신 **조용히 넘어가지 않는다** — 부르는 쪽이 None 을 받아 말한다.
    """
    data = load_json(PENDING, None)
    if not isinstance(data, dict):
        return None
    items = data.get("items") or []
    left = [it for it in items if it.get("id") not in ids]
    if len(left) == len(items):
        return 0
    try:
        if left:
            body = json.dumps({**data, "items": left}, ensure_ascii=False, indent=2) + "\n"
            tmp = PENDING.with_suffix(PENDING.suffix + ".tmp")
            tmp.write_bytes(body.encode("utf-8"))
            tmp.replace(PENDING)
        else:
            PENDING.unlink()
    except OSError:
        return None
    return len(items) - len(left)


def defer(items, days, reason):
    state = load_state()
    if "_deferred" not in state:
        state["_deferred"] = DEFERRED_COMMENT
    state.setdefault("deferred", {})
    until = (today_kst() + timedelta(days=days)).isoformat()
    for it in items:
        state["deferred"][it["id"]] = {
            "channel": it.get("channel"), "key": it.get("key"),
            "kind": it.get("kind"), "scope": it.get("scope"),
            "reason": reason, "until": until,
            "decided": f"{today_kst().isoformat()} (사람 결정)",
        }
    save_state(state)
    return until


def clear(tokens):
    """보류 취소. 번호는 **보류 목록**의 순서다."""
    state = load_state()
    held = state.get("deferred") or {}
    rows = [{"id": k, **v} for k, v in sorted(held.items(), key=lambda kv: str(kv[1].get("until")))]
    picked, unmatched = select(rows, tokens)
    for r in picked:
        held.pop(r["id"], None)
    if picked:
        state["deferred"] = held
        save_state(state)
    return picked, unmatched


def show_deferred():
    held = (load_json(STATE, {}).get("deferred") or {})
    if not held:
        return
    today = today_kst()
    rows = []
    for i, (k, v) in enumerate(sorted(held.items(), key=lambda kv: str(kv[1].get("until"))), 1):
        try:
            alive = date.fromisoformat(str(v.get("until"))) >= today
        except ValueError:
            alive = False
        rows.append([str(i), "" if alive else "만기지남",
                     f"#{v.get('channel')}", str(v.get("key")),
                     str(v.get("until")), str(v.get("reason", ""))[:40]])
    print(f"\n⏸ 미뤄 둔 것 {len(rows)}건")
    print(table(["#", "상태", "채널", "언제 것", "만기", "이유"], rows, [3, 8, 18, 28, 12, 40]))
    print("  만기가 지난 것은 이미 위 목록으로 돌아와 있습니다. 취소는 `--clear <번호>`.")


# ── main ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="슬랙 수정·삭제를 아카이브에 반영")
    ap.add_argument("--only", action="append", metavar="번호|조각",
                    help="이것만. `--only 2,4` 또는 채널명·날짜 조각")
    ap.add_argument("--apply", action="store_true", help="실제로 고친다 (없으면 목록만)")
    ap.add_argument("--dry-run", action="store_true", help="바뀔 자리만 본다")
    ap.add_argument("--later", type=int, nargs="?", const=DEFAULT_LATER_DAYS, metavar="일수",
                    help=f"미뤄 둔다. 만기까지의 일수, 생략하면 {DEFAULT_LATER_DAYS}")
    ap.add_argument("--reason", help="--later 에 필수 — 왜 미뤘는지")
    ap.add_argument("--clear", action="append", metavar="번호", help="보류 취소")
    args = ap.parse_args()

    if args.clear:
        picked, unmatched = clear(args.clear)
        for p in picked:
            print(f"보류 취소 — #{p.get('channel')} {p.get('key')}")
        for t in unmatched:
            print(f"✗ 못 맞춘 것: {t}", file=sys.stderr)
        show_deferred()
        return 1 if unmatched else 0

    items = pending_items()
    if not items:
        print("반영할 것이 없습니다.")
        show_deferred()
        return 0

    KIND_LABEL = {"edited": "수정", "deleted": "삭제", "ambiguous": "중복"}
    rows = [[str(i), KIND_LABEL.get(it.get("kind"), str(it.get("kind"))),
             "답글" if it.get("scope") == "reply" else "메시지",
             f"#{it.get('channel')}", str(it.get("key")),
             ("⚠ 손으로" if it.get("manual") else ""),
             str(it.get("before", "")).replace("\n", " ")[:60]]
            for i, it in enumerate(items, 1)]
    print(f"슬랙에서 고쳐지거나 지워진 것 {len(items)}건 — 아직 아카이브에 반영 안 됨\n")
    print(table(["#", "종류", "대상", "채널", "언제 것", "", "지금 md 에 있는 내용"],
                rows, [3, 4, 6, 18, 28, 8, 60]))

    if not args.only:
        # **하겠다고 말한 것을 조용히 안 하지 않는다.** `--later`·`--apply` 는 「무엇을」이
        # 있어야 뜻이 서는데, 예전에는 `--only` 없이 주면 목록만 찍고 **종료코드 0** 으로
        # 끝났다. 미룬 줄 알고 넘어가면 다음 날 같은 경고가 또 오고, 그때는 스크립트가
        # 고장 난 것으로 보인다 (2026-08-10 코드 점검. 09:00 DM 이 `--only` 없는 명령을
        # 안내하고 있어서 실제로 그 길로 가게 돼 있었다).
        for flag, given in (("--later", args.later is not None), ("--apply", args.apply)):
            if given:
                print(f"\n✗ {flag} 에는 --only 가 필요합니다 — 무엇을 고를지 없으면 "
                      "아무것도 하지 않습니다. 위 번호로 고르세요.", file=sys.stderr)
                print(f"  예: apply_edits.py --only 1 {flag}"
                      + (f" {args.later} --reason \"…\"" if flag == "--later" else ""),
                      file=sys.stderr)
                show_deferred()
                return 1
        print("\n  반영:   --only 1,2 --apply        (먼저 --dry-run 으로 볼 수 있습니다)")
        print(f"  미루기: --only 3 --later {DEFAULT_LATER_DAYS} --reason \"…\"")
        show_deferred()
        return 0

    picked, unmatched = select(items, args.only)
    for t in unmatched:
        print(f"✗ 못 맞춘 것: {t}", file=sys.stderr)
    if not picked:
        print("✗ 고른 것이 없습니다.", file=sys.stderr)
        return 1

    if args.later is not None:
        if not args.reason:
            print("✗ --later 에는 --reason 이 필요합니다 — 왜 미뤘는지가 없으면 "
                  "다음 사람이 되돌릴 수 없습니다.", file=sys.stderr)
            return 1
        until = defer(picked, args.later, args.reason)
        for p in picked:
            print(f"미뤄 둠 (만기 {until}) — #{p.get('channel')} {p.get('key')}")
        print("  만기가 지나면 저절로 목록으로 돌아오고 09:00 위생 점검이 다시 알립니다.")
        return 1 if unmatched else 0

    dry = not args.apply or args.dry_run
    ok_n = fail_n = 0
    done = []
    with tempfile.TemporaryDirectory(prefix="apply-edits-") as td:
        print()
        for p in picked:
            ok, note = apply_one(p, td, dry)
            mark = "✓" if ok else "✗"
            print(f"{mark} #{p.get('channel')} {p.get('key')} — {note}")
            ok_n, fail_n = ok_n + int(ok), fail_n + int(not ok)
            if ok:
                done.append(p.get("id"))

    if dry:
        print(f"\n({'--apply 가 없어' if not args.apply else '--dry-run 이라'} 아무것도 안 고쳤습니다)")
        return 1 if fail_n or unmatched else 0

    # 반영한 것만 목록에서 뺀다. 실패한 건을 함께 빼면 슬랙과 md 가 다른 채로 아무 표시 없이
    # 사라진다 — 이 파일이 막으려는 바로 그 상태다.
    if done and drop_pending(set(done)) is None:
        print(f"\n⚠ {PENDING.name} 에서 반영한 건을 빼지 못했습니다 — md 는 고쳐졌습니다.\n"
              "  내일 07:00 대조가 지울 때까지 09:00 점검과 상황판에 남아 보입니다.")

    # 메시지를 지우면 헤더 건수가 바뀐다. 파생값이라 여기서 세지 않고 그 스크립트에 맡긴다.
    if ok_n:
        r = subprocess.run([sys.executable, str(SYNC), "--all"], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print("\n" + ((r.stdout or "").strip() or "파생값: 맞출 것 없음"))

    print(f"\n반영 {ok_n}건 · 실패 {fail_n}건 · 나머지 {len(items) - len(picked)}건은 그대로 둡니다.")
    print("커밋·push 는 사람 승인입니다 — push 돼야 봇이 고쳐진 문장을 봅니다.")
    return 1 if fail_n or unmatched else 0


if __name__ == "__main__":
    sys.exit(main())
