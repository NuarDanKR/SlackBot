#!/usr/bin/env python3
"""decision_export.py 시험. **임시 파일에서만 돌고 저장소는 안 건드린다.**

  python .claude/skills/archive-inbox/scripts/test_decision_export.py

종료코드: 0 전부 통과 / 1 실패 있음

── 무엇을 지키는가 ──

여기가 지키는 것은 **「생략해도 되는가」의 경계**다. 틀리는 방향이 둘인데 값이
완전히 다르다.

  · 묻는 쪽으로 틀리면 — 사람이 같은 것을 한 번 더 본다
  · 생략하는 쪽으로 틀리면 — **아무도 모른다**

그래서 좌표를 못 내는 경우, 해시가 달라진 경우, 보류, 모순, 기록을 못 읽는 경우를
전부 「생략하지 않음」으로 고정한다. 좌표를 **지어내지 않는 것**도 같은 축이다 —
틀린 좌표는 승인하지 않은 것에 승인을 붙인다.

node 를 쓰지 않는다. `summary_hash` 만 node 를 부르고 이 파일은 그 자리를 안 지난다.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_export as X  # noqa: E402
import review_work as R  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok &= bool(cond)
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))


MD = """# 자금

## 요약
- 집행액은 12억이다

---

## 2026-10

**2026-10-01 10:00 · 김과장**
금주 집행액은 12억원으로 확정되었습니다.
추가 변동은 없습니다.

---

**2026-10-01 14:00 · 이대리**
회의실 예약은 내일로 미룹니다.

---
"""


def _repo(md_text=MD, name="자금", eol="\n"):
    """임시 자료 저장소 하나. `R` 의 경로 상수를 갈아 끼운다.

    **바이트로 쓴다.** `write_text` 는 윈도우에서 `\\n` 을 `\\r\\n` 으로 바꿔 놓는데,
    그러면 「CRLF 로 쓴다」는 시험이 실제로는 `\\r\\r\\n` 을 쓰고 읽는 쪽에서 빈 줄이
    늘어난다 — 줄끝을 재려던 시험이 줄 번호를 재는 시험으로 바뀐다.
    """
    tmp = Path(tempfile.mkdtemp())
    channels = tmp / "slack-export" / "channels"
    channels.mkdir(parents=True)
    (channels / f"{name}.md").write_bytes(
        md_text.replace("\n", eol).encode("utf-8"))
    R.ROOT, R.CHANNELS, R.STATE = tmp, channels, tmp / "slack-export" / ".sync-state.json"
    return tmp


def _item(**over):
    it = {
        "id": "자금|summary|수치|집행액", "kind": "summary",
        "channel": "자금", "file": "자금",
        "evidence": '"금주 집행액은 12억원으로 확정되었습니다"',
    }
    it.update(over)
    return it


_keep = (R.ROOT, R.CHANNELS, R.STATE)
try:
    # ── ① 좌표 ──────────────────────────────────────────────────────────
    print("\n원문 좌표 — 지어내지 않고, 못 내면 비운다")
    _repo()
    loc, dig, why = X.coordinate_for(_item())
    check("인용이 한 메시지에 있으면 좌표가 나온다", bool(loc and dig), f"{loc} {dig[:8]}")
    check("  좌표가 그 메시지 블록을 가리킨다",
          loc.startswith("slack-export/channels/자금.md#L") and "-L" in loc, loc)

    _repo()
    loc2, dig2, _ = X.coordinate_for(_item())
    check("같은 원문이면 같은 해시다", (loc, dig) == (loc2, dig2))

    # **인용 줄은 그대로 두고 블록의 다른 줄을 고친다.** 인용 자체를 고치면 좌표가
    # 아예 안 나와서(= 못 찾음) 「해시가 달라진다」를 재지 못한다. 재려는 것은
    # 「사람이 본 메시지가 그 뒤에 바뀌었다」 쪽이다.
    _repo(MD.replace("추가 변동은 없습니다.", "추가 변동이 있습니다."))
    loc3, dig3, why3 = X.coordinate_for(_item())
    check("같은 메시지의 다른 줄이 바뀌면 해시가 달라진다",
          bool(dig3) and dig3 != dig, f"{dig[:8]} vs {(dig3 or '')[:8]} {why3}")
    check("  좌표(줄 범위)는 그대로다", loc3 == loc, f"{loc} vs {loc3}")

    _repo(eol="\r\n")
    _, dig4, _ = X.coordinate_for(_item())
    check("줄끝(CRLF)은 해시를 가르지 않는다", dig4 == dig, f"{dig[:8]} vs {(dig4 or '')[:8]}")

    _repo()
    loc5, dig5, why5 = X.coordinate_for(_item(evidence='"원문에 없는 문장입니다 정말로"'))
    check("원문에서 못 찾으면 좌표가 없다", not loc5 and not dig5, why5)

    loc6, dig6, why6 = X.coordinate_for(
        _item(evidence='"금주 집행액은 12억원으로 확정" "회의실 예약은 내일로 미룹니다"'))
    check("근거가 두 메시지에 걸치면 좌표가 없다", not loc6 and not dig6, why6)
    check("  사유가 그 사실을 말한다", "여러 메시지" in why6, why6)

    loc7, _, why7 = X.coordinate_for(
        _item(evidence='"금주 집행액은 12억원으로 확정" "지어낸 근거 문장이다 아주"'))
    check("원문에 없는 조각이 섞이면 좌표가 없다", not loc7, why7)

    loc8, _, why8 = X.coordinate_for(_item(kind="new-channel"))
    check("요약 계열이 아니면 좌표를 안 낸다", not loc8, why8)

    # ── ② 채널 ID ───────────────────────────────────────────────────────
    print("\n채널 ID — 애매하면 안 쓴다")
    ids = X.channel_ids({"channels": {"C1": {"name": "자금", "file": "자금"}}})
    check("파일명으로 ID 를 찾는다", ids.get("자금") == "C1", ids)
    ids = X.channel_ids({"channels": {
        "C1": {"name": "자금", "file": "자금"},
        "C2": {"name": "자금신규", "file": "자금"},
    }})
    check("한 파일에 ID 가 둘이면 안 담는다", "자금" not in ids, ids)

    # ── ③ 파일 자리 ─────────────────────────────────────────────────────
    print("\n파일 자리 — 출처마다, 워크스페이스마다 다르다")
    root = Path(tempfile.mkdtemp())
    mine = X.export_path(root, workspace="TEC")
    other = X.export_path(root, workspace="TEC", source="tybot-dm")
    check("출처가 다르면 파일이 다르다", mine != other and mine.parent == other.parent)
    check("워크스페이스가 다르면 디렉터리가 다르다",
          X.export_path(root, workspace="A").parent != X.export_path(root, workspace="B").parent)
    _bad = 0
    for bad in ("", "..", "a/b", "a\\b", ".", "   "):
        try:
            X.export_path(root, workspace=bad)
        except X.UnsafeExportTarget:
            _bad += 1
    check("경로 모양 이름은 거절한다", _bad == 6, f"{_bad}/6")

    # ── ④ 쓰기 ──────────────────────────────────────────────────────────
    print("\n쓰기 — 통째로, 찌꺼기 없이")
    X.write(mine, [{"workspace": "TEC"}])
    payload = json.loads(mine.read_text(encoding="utf-8"))
    check("스키마 이름이 계약대로다", payload["schema"] == X.EXPORT_SCHEMA, payload["schema"])
    check("출처가 적힌다", payload["source"] == X.SOURCE)
    X.write(mine, [])
    check("다시 쓰면 통째로 바뀐다(증분이 아니다)",
          json.loads(mine.read_text(encoding="utf-8"))["decisions"] == [])
    check("찌꺼기(.tmp)가 없다", not list(mine.parent.glob("*.tmp")))

    class _Bad:
        def __repr__(self):
            raise RuntimeError("직렬화 실패")

    try:
        X.write(mine, [{"x": {1, 2}}])          # set 은 JSON 이 아니다
    except (TypeError, ValueError):
        pass
    check("쓰기 실패 뒤에도 찌꺼기가 없다", not list(mine.parent.glob("*.tmp")),
          [p.name for p in mine.parent.glob("*.tmp")])

    # ── ⑤ 생략 판정 ─────────────────────────────────────────────────────
    print("\n생략 판정 — 좌표와 해시로만, 확정된 것만")
    base = {"workspace": "TEC", "channel_id": "C1",
            "evidence_locator": "자금.md#L10-L12", "evidence_hash": "deadbeef"}

    def _d(**over):
        row = dict(base, state="approved")
        row.update(over)
        return row

    def _ask(decisions, **over):
        args = dict(workspace="TEC", channel_id="C1",
                    locator="자금.md#L10-L12", digest="deadbeef")
        args.update(over)
        return X.decide(decisions=decisions, **args)

    check("같은 좌표·해시의 승인은 생략한다", _ask([_d()]) == (True, X.SKIP), _ask([_d()]))
    check("같은 좌표·해시의 거절도 생략한다", _ask([_d(state="rejected")])[0] is True)
    check("보류는 생략하지 않는다", _ask([_d(state="deferred")]) == (False, X.NOT_FINAL))
    check("승인과 거절이 함께면 생략하지 않는다",
          _ask([_d(), _d(state="rejected")]) == (False, X.CONFLICT))
    check("해시가 달라지면 생략하지 않는다",
          _ask([_d(evidence_hash="other")]) == (False, X.EVIDENCE_CHANGED))
    check("워크스페이스가 다르면 생략하지 않는다",
          _ask([_d(workspace="OTHER")]) == (False, X.SOURCE_MISMATCH))
    check("채널이 다르면 생략하지 않는다",
          _ask([_d(channel_id="C9")]) == (False, X.SOURCE_MISMATCH))
    check("좌표가 없으면 생략하지 않는다", _ask([_d()], locator="") == (False, X.NO_COORDINATE))
    check("기록을 못 읽으면 생략하지 않는다", _ask(None) == (False, X.RECORDS_UNREADABLE))
    check("그 좌표의 결정이 없으면 생략하지 않는다", _ask([]) == (False, X.NO_MATCH))

    # ── ⑥ 상대편 기록 ───────────────────────────────────────────────────
    print("\n상대편 기록 — 내 것은 안 읽고, 하나라도 못 읽으면 전부 멈춘다")
    root2 = Path(tempfile.mkdtemp())
    X.write(X.export_path(root2, workspace="TEC"), [_d(evidence_locator="mine")])
    theirs = X.export_path(root2, workspace="TEC", source="tybot-dm")
    theirs.write_text(json.dumps(
        {"schema": X.EXPORT_SCHEMA, "source": "tybot-dm", "decisions": [_d()]}),
        encoding="utf-8")
    rows = X.counterpart(root2, workspace="TEC")
    check("상대편 것만 읽는다", [r["evidence_locator"] for r in rows] == [base["evidence_locator"]],
          rows)

    (root2 / "TEC" / "other.json").write_text("{not json", encoding="utf-8")
    check("하나라도 못 읽으면 None", X.counterpart(root2, workspace="TEC") is None)
    check("디렉터리가 없으면 0건(못 읽음이 아니다)",
          X.counterpart(Path(tempfile.mkdtemp()), workspace="TEC") == [])

    future = root2 / "TEC2" / "tybot-dm.json"
    future.parent.mkdir(parents=True)
    future.write_text(json.dumps({"schema": "v2", "decisions": []}), encoding="utf-8")
    check("모르는 형식은 「결정 없음」이 아니라 「못 읽음」",
          X.counterpart(root2, workspace="TEC2") is None)

    # ── ⑦ 기록 만들기 ───────────────────────────────────────────────────
    print("\n기록 만들기 — 본문은 안 담고, 적어 둔 좌표를 쓴다")
    _repo()
    state = {
        "channels": {"C1": {"name": "자금", "file": "자금"}},
        "applied": {"자금|summary|수치|집행액": {
            "at": "2026-10-05T09:00:00+09:00", "state": "approved",
            "workspace": "TEC", "channel_id": "C1",
            "evidence_locator": "자금.md#L10-L12", "evidence_hash": "deadbeef",
        }},
        "deferred": {"영업|summary|수치|매출": {
            "at": "2026-10-05T10:00:00+09:00", "until": "2026-10-19", "reason": "확인중",
        }},
    }
    rows, skipped = X.records(state, workspace="TEC", items_by_id={})
    check("적어 둔 좌표를 그대로 쓴다(다시 재지 않는다)",
          len(rows) == 1 and rows[0]["evidence_hash"] == "deadbeef", rows)
    check("키 집합이 계약대로다", set(rows[0]) == set(X.RECORD_KEYS),
          sorted(set(rows[0]) ^ set(X.RECORD_KEYS)))
    check("좌표 없는 기록은 빠지고 사유가 센다", bool(skipped), skipped)
    raw = json.dumps(rows, ensure_ascii=False)
    check("본문·인용이 안 실린다",
          all(bad not in raw for bad in ("12억", "집행액은", "확인중", "금주")), raw[:200])

    # ── ⑧ 목록에서 빼기 ─────────────────────────────────────────────────
    print("\n목록 — 끝난 것만 빠지고, 보류는 그대로 남는다")
    _repo()
    R.STATE.write_text(json.dumps(
        {"channels": {"C1": {"name": "자금", "file": "자금"}}}), encoding="utf-8")
    loc, dig = X.coordinate_for(_item())[:2]
    # 둘째 항목의 근거는 **다른 메시지**에 있다. 같은 블록의 다른 줄을 쓰면 좌표가
    # 같아져서(블록 하나가 좌표다) 「끝난 것만 빠진다」를 재지 못한다.
    items = [_item(),
             _item(id="자금|summary|수치|회의실", evidence='"회의실 예약은 내일로 미룹니다"')]
    check("두 항목의 좌표가 실제로 다르다",
          X.coordinate_for(items[1])[0] not in ("", loc), X.coordinate_for(items[1])[0])

    root3 = Path(tempfile.mkdtemp())
    (root3 / "TEC").mkdir(parents=True)
    (root3 / "TEC" / "tybot-dm.json").write_text(json.dumps({
        "schema": X.EXPORT_SCHEMA, "source": "tybot-dm",
        "decisions": [{"workspace": "TEC", "channel_id": "C1",
                       "evidence_locator": loc, "evidence_hash": dig, "state": "approved"}],
    }), encoding="utf-8")

    _env, _ws = X.export_root, X.workspace_label
    try:
        X.export_root = lambda env=None: root3
        X.workspace_label = lambda: "TEC"
        settled, why = R._settled_elsewhere(items)
        check("상대편이 승인한 건은 빠진다", items[0]["id"] in settled, settled)
        check("  다른 건은 남는다", items[1]["id"] not in settled, settled)
        check("  안 뺀 사유가 센다", bool(why), why)

        (root3 / "TEC" / "tybot-dm.json").write_text("{not json", encoding="utf-8")
        settled2, why2 = R._settled_elsewhere(items)
        check("기록을 못 읽으면 하나도 안 뺀다", not settled2,
              f"{settled2} {why2}")
        check("  사유가 「못 읽음」이다", why2.get(X.RECORDS_UNREADABLE) == len(items), why2)
    finally:
        X.export_root, X.workspace_label = _env, _ws
finally:
    R.ROOT, R.CHANNELS, R.STATE = _keep

print("\n전부 통과" if _ok else "\n실패 있음")
sys.exit(0 if _ok else 1)
