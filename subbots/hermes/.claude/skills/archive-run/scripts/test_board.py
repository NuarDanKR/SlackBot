#!/usr/bin/env python3
"""
board.py 시험. **임시 파일에서만 돌고 저장소는 안 건드린다.**

  python .claude/skills/archive-run/scripts/test_board.py

종료코드: 0 전부 통과 / 1 실패 있음

── 왜 남겨 두는가 ──

이 상황판은 **「할 일이 없다」를 말하는 화면**이다. 그래서 가장 위험한 고장은 에러가
아니라 **못 세었는데 0건으로 보이는 것**이다. 슬랙 조회는 18초짜리 네트워크 호출이라
토큰 만료·연결 끊김으로 언제든 실패하는데, 그때 0을 적으면 화면은 「오늘 할 일 없음」이
되고 사람은 그대로 닫는다. 39건이 밀려 있어도 그렇게 보인다.

이 파이프라인이 조용히 틀리는 방식이 정확히 그것이라(에러 없이 화면만 좁아지는 것,
2026-08-12 아침 보고에서 셋), 「조회 실패가 0건으로 렌더되지 않는다」를 시험으로 못 박는다.

마감 계산도 같은 이유로 경계 앞뒤를 다 본다 — 16:59 와 17:01 이 같은 답을 내면
첨부를 오늘 넣을 수 있는지가 하루 어긋나고, 그 어긋남은 요약이 나간 뒤에야 보인다.
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board as B  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

KST = timezone(timedelta(hours=9))
_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok &= bool(cond)
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=KST)


def find(rows, key):
    for r in rows:
        if r["key"] == key:
            return r
    return None


def _line_of(text, needle):
    """그 낱말이 든 첫 줄. 실패했을 때 무엇이 보였는지 함께 찍으려고 쓴다."""
    for line in text.splitlines():
        if needle in line:
            return line.strip()
    return f"(「{needle}」이 든 줄이 없습니다)"


def _row_says(text, label, needle):
    return needle in _line_of(text, label)


# ─────────────────────────────────────────────────────────────
print("\nnext_at — 다음 그 시각")

# 마감이 하루 어긋나면 「오늘 안에 되나」가 통째로 뒤집힌다. 경계를 앞뒤로 다 본다.
check("16:59 → 오늘 17:00", B.next_at(at(2026, 8, 12, 16, 59), 17) == at(2026, 8, 12, 17))
check("17:01 → 내일 17:00", B.next_at(at(2026, 8, 12, 17, 1), 17) == at(2026, 8, 13, 17))
# 정각은 **이미 지난 것**으로 본다 — 17:00 자동 반영이 그 순간 돌고 있다.
check("17:00 정각 → 내일 17:00", B.next_at(at(2026, 8, 12, 17, 0), 17) == at(2026, 8, 13, 17))
check("06:59 → 오늘 07:00", B.next_at(at(2026, 8, 12, 6, 59), 7) == at(2026, 8, 12, 7))
check("07:01 → 내일 07:00", B.next_at(at(2026, 8, 12, 7, 1), 7) == at(2026, 8, 13, 7))
# 자정을 넘어가는 경우
check("23:30 → 내일 07:00", B.next_at(at(2026, 8, 12, 23, 30), 7) == at(2026, 8, 13, 7))
# 분을 버리면 `30 7 * * *` 마감이 07:00 으로 선다 — 이른 쪽이지만 30분 어긋난 채로.
check("07:29 → 오늘 07:30", B.next_at(at(2026, 8, 12, 7, 29), 7, 30) == at(2026, 8, 12, 7, 30))
check("07:30 정각 → 내일 07:30", B.next_at(at(2026, 8, 12, 7, 30), 7, 30) == at(2026, 8, 13, 7, 30))
check("07:31 → 내일 07:30", B.next_at(at(2026, 8, 12, 7, 31), 7, 30) == at(2026, 8, 13, 7, 30))


print("\n_cron_hour — 마감 시각은 설정에서 온다")

# ★ 이 시험은 **실제 config.json** 에 대고 돈다. 크론을 바꾸면 여기가 먼저 깨져서,
# 상황판이 옛 마감을 조용히 보여주는 일이 안 생긴다 (그게 이 시험의 목적이다).
h, mnt, why = B._cron_hour("ingest", 99)
check("07:00 자동 반영을 설정에서 읽는다", (h, mnt, why) == (7, 0, None), repr((h, mnt, why)))
h2, mnt2, why2 = B._cron_hour("ingestPre", 99)
check("17:00 회차도 설정에서 읽는다", (h2, mnt2, why2) == (17, 0, None), repr((h2, mnt2, why2)))

with tempfile.TemporaryDirectory() as d:
    cfg = Path(d) / "config.json"

    def cron(expr):
        cfg.write_text(json.dumps({"digest": {"ingest": {"cron": expr}}}, ensure_ascii=False),
                       encoding="utf-8")
        return B._cron_hour("ingest", 7, cfg_path=cfg)

    check("`0 9 * * *` 는 9시", cron("0 9 * * *") == (9, 0, None), repr(cron("0 9 * * *")))
    # **분 칸도 값이다** (2026-09-04 리뷰). 버리면 `30 7 * * *` 이 07:00 으로 보인다.
    check("`30 7 * * *` 는 07:30", cron("30 7 * * *") == (7, 30, None), repr(cron("30 7 * * *")))
    r = cron("60 7 * * *")
    check("분이 말이 안 되면 기본값", r[0] == 7 and r[-1], repr(r))
    # 마감이 하나가 아닌 크론은 이 칸에 적을 수가 없다. **기본값으로 가되 말은 한다.**
    r = cron("0 7,17 * * *")
    check("여러 번 도는 크론은 안 읽고 사유를 준다", r[0] == 7 and r[2], repr(r))
    r = cron("*/30 * * * *")
    check("`*/30` 도 안 읽고 사유를 준다", r[0] == 7 and r[2], repr(r))
    r = cron("0 99 * * *")
    check("시각이 말이 안 되면 기본값", r[0] == 7 and r[2], repr(r))

    # **요일·날짜 제한도 「매일 그 시각」이 아니다** (2026-08-13 리뷰). 전에는 뒤 세 칸을
    # `\S+` 로 받아 `0 7 * * 1-5` 를 매일 07:00 으로 읽었다 — 토요일 화면이 오지 않을 마감을
    # 약속하고, 사유가 없으니 사람은 그 값을 믿는다. 마감 계산이 「다음 날 같은 시각」을
    # 가정하므로 이런 크론은 이 칸으로 표현할 수 없다.
    r = cron("0 7 * * 1-5")
    check("평일만 도는 크론은 안 읽고 사유를 준다", r[0] == 7 and r[2], repr(r))
    r = cron("0 7 1 * *")
    check("매월 1일만 도는 크론도 사유를 준다", r[0] == 7 and r[2], repr(r))
    r = cron("0 7 * 3 *")
    check("특정 달만 도는 크론도 사유를 준다", r[0] == 7 and r[2], repr(r))
    # 칸 사이 공백이 여러 개인 것은 정상으로 읽는다 (크론 파일에서 흔하다)
    check("공백이 여러 개여도 읽는다", cron("0   9   *   *   *") == (9, 0, None), repr(cron("0   9   *   *   *")))

    cfg.write_text(json.dumps({"digest": {}}, ensure_ascii=False), encoding="utf-8")
    r = B._cron_hour("ingest", 7, cfg_path=cfg)
    check("키가 없으면 기본값 + 사유", r[0] == 7 and r[2], repr(r))
    cfg.write_text("{ 깨진", encoding="utf-8")
    r = B._cron_hour("ingest", 7, cfg_path=cfg)
    check("설정이 깨져도 멈추지 않는다", r[0] == 7 and r[2], repr(r))
    r = B._cron_hour("ingest", 7, cfg_path=Path(d) / "없는파일.json")
    check("설정 파일이 없어도 멈추지 않는다", r[0] == 7 and r[2], repr(r))


print("\ndeadline_text — 남은 시간")

t = B.deadline_text(at(2026, 8, 12, 14, 20), at(2026, 8, 12, 17, 0))
check("시각과 남은 시간이 함께 나온다", "17:00" in t and "2시간" in t, repr(t))
t2 = B.deadline_text(at(2026, 8, 12, 16, 20), at(2026, 8, 12, 17, 0))
check("한 시간 미만은 분으로", "40분" in t2 and "시간" not in t2, repr(t2))
t3 = B.deadline_text(at(2026, 8, 12, 14, 20), at(2026, 8, 13, 7, 0))
check("날을 넘기면 그렇게 보인다", "07:00" in t3, repr(t3))


# ─────────────────────────────────────────────────────────────
print("\nrows — 조회 실패는 0건이 아니다")

NOW = at(2026, 8, 12, 14, 20)

OK_PROBES = dict(
    work={"items": [], "generated": "2026-08-12T07:00:00+09:00"},
    edits={"items": []},
    sync={"last_sync": "2026-08-12T07:00:00+09:00"},
    docs={"total": 39, "scanDays": 60, "approvals": [], "deferred": [], "failed": [],
          "byChannel": [
              {"channel": "사업장마", "count": 24, "newest": "확인서.hwp"},
              {"channel": "비공개사", "count": 14, "newest": "점검.hwpx"},
              {"channel": "사업장다", "count": 1, "newest": "의견요청.pdf"},
          ]},
)

rows = B.rows(NOW, **OK_PROBES)

r = find(rows, "docs")
check("첨부 39건을 그대로 센다", r["count"] == 39, repr(r["count"]))
# 이 숫자는 **최근 60일 범위**다 (`archive-health.js` 의 scanDays — "창 밖까지 세면 매일
# 줄지 않는 숫자가 된다"). 화면에 그 범위가 없으면 doc-archive 수집이 더 많이 찾아올 때
# (수집은 기간 제한이 없다) 두 숫자가 어긋나 보이고, 어느 쪽이 맞는지 알 방법이 없다.
check("몇 일 범위인지 화면에 적힌다", any("60일" in d for d in r["detail"]), repr(r["detail"]))
check("채널별 내역이 붙는다", any("사업장마 24" in d for d in r["detail"]), repr(r["detail"]))
check("첨부 마감은 17:00", r["deadline_at"] == at(2026, 8, 12, 17), repr(r["deadline_at"]))

# ★ 이 시험이 이 파일의 이유다.
bad = dict(OK_PROBES, docs={"error": "슬랙 토큰이 거부됐습니다 (invalid_auth)"})
rows_bad = B.rows(NOW, **bad)
rb = find(rows_bad, "docs")
check("조회 실패면 count 가 0 이 아니다", rb["count"] is None, repr(rb["count"]))
check("실패 사유를 들고 있다", "invalid_auth" in (rb["error"] or ""), repr(rb["error"]))

out_bad = B.render(NOW, rows_bad, prep={"clean": True, "ahead": 0, "behind": 0, "lock": {"held": False}})
check("화면에 「조회 실패」가 보인다", "조회 실패" in out_bad)
check("실패한 줄이 0건으로 안 적힌다", "미변환 첨부" in out_bad and not _row_says(out_bad, "미변환 첨부", "0건"),
      _line_of(out_bad, "미변환 첨부"))

# 승인 미반영도 같은 조회에 딸려 있으므로 함께 실패해야 한다 — 한쪽만 0이면 더 위험하다
ra = find(rows_bad, "approvals")
check("승인 미반영도 함께 실패로 간다", ra["count"] is None and ra["error"], repr(ra["count"]))


print("\nrows — 일부만 센 것은 0건도 아니고 실패도 아니다")

# ★ 이것도 이 파일의 이유다 (2026-08-12 리뷰에서 나온 구멍).
# 슬랙 조회는 채널별로 도는데, 몇 개가 실패해도 `pendingDocuments()` 는 **성공한 채널만
# 세서 total 을 돌려준다** (`archive-health.js` 의 `pendingDocuments` 의 채널 순회). 그 값을 확정값으로 찍으면 채널이
# 전부 실패한 날 화면이 「0건 · 지금 할 일이 없습니다」가 된다 — 실패했는데 할 일이
# 없다고 말하는 것이라, 이 상황판이 막으려던 바로 그 화면이다.
part = dict(OK_PROBES, docs={"total": 0, "scanDays": 60, "approvals": [], "deferred": [],
                             "failed": ["#사업장마(not_in_channel)", "#비공개가(ratelimited)"],
                             "byChannel": []})
rows_part = B.rows(NOW, **part)
rp = find(rows_part, "docs")
check("일부 실패는 실패로 안 뭉갠다", rp["error"] is None and rp["partial"], repr(rp["partial"]))
check("못 읽은 채널 이름이 붙는다", any("not_in_channel" in d for d in rp["detail"]), repr(rp["detail"]))
# 승인 미반영은 같은 조회에서 온다. 여기에 표시가 없으면 그 줄만 흔적 없이 0건이 된다.
rpa = find(rows_part, "approvals")
check("승인 미반영에도 일부 실패가 표시된다", bool(rpa["partial"]), repr(rpa["partial"]))

out_part = B.render(NOW, rows_part, prep={"clean": True, "ahead": 0, "behind": 0, "lock": {"held": False}})
# 「0건」이 그냥 보이면 훑어보는 사람은 그걸로 닫는다. 「최소」가 숫자 앞에 와야 한다.
check("일부만 센 줄은 그냥 「0건」으로 안 적힌다",
      "최소 0건" in _line_of(out_part, "미변환 첨부"), _line_of(out_part, "미변환 첨부"))
check("셌을 때와 화면이 다르다", B._amount(rp) != "0건", B._amount(rp))
check("일부만 센 줄이 있으면 「지금 할 일이 없습니다」로 닫지 않는다",
      "지금 할 일이 없습니다" not in out_part, _line_of(out_part, "없습니다"))
check("일부만 셌다는 것을 마무리에서 말한다", "일부만 센 줄" in out_part, _line_of(out_part, "일부만"))
# 0건이어도 번호가 붙어야 한다 — 가서 봐야 하는 줄이다.
check("일부만 센 줄에는 번호가 붙는다", B.is_actionable(rp, warn=7) is True, B._amount(rp))

# 문서 폴더가 없으면 `pendingDocuments()` 는 total 0 에 disabled 를 붙여 돌려준다.
# 0 으로 읽으면 「첨부는 다 됐다」가 된다.
#
# ⚠️ **이 시험은 `rows()` 에 값을 직접 먹인다 — `board-docs.js` 를 부르지 않는다.**
# 그래서 2026-08-12 까지 이 시험은 통과하는데 실물은 안 통했다: 투영이 `disabled` 를
# 안 넘겨서 아래 방어가 실전 경로에서 한 번도 참이 될 수 없었다. **투영이 필드를
# 넘기는지는 여기서 못 본다** — 그건 `21-hermes/scripts/check-board-docs.js` 가 본다.
dis = dict(OK_PROBES, docs={"total": 0, "scanDays": 60, "approvals": [], "deferred": [],
                            "failed": [], "byChannel": [], "restricted": [], "disabled": True})
rd = find(B.rows(NOW, **dis), "docs")
check("문서 폴더가 없으면 0건이 아니라 실패", rd["count"] is None and rd["error"], repr(rd))

# 봇이 못 받는 첨부(권한 제한)는 미변환 숫자 **밖**이다. 함께 세면 `doc-archive` 를 돌려도
# 안 줄어들어 매일 같은 값이 오고, 곧 화면 전체를 안 읽게 된다. 그렇다고 감추면 자료가
# 없는 것과 구별이 안 된다 — 숫자에서는 빼고 화면에는 남긴다.
restr = dict(OK_PROBES, docs={"total": 2, "scanDays": 60, "approvals": [], "deferred": [],
                              "failed": [], "byChannel": [],
                              "restricted": [{"channel": "비공개나", "name": "d.docx"}]})
rr = find(B.rows(NOW, **restr), "docs")
check("권한 제한 첨부는 미변환 숫자에 안 더해진다", rr["count"] == 2, repr(rr["count"]))
check("그래도 화면에는 남는다", any("못 받는 첨부 1건" in d for d in rr["detail"]), repr(rr["detail"]))
check("끝내는 길을 함께 적는다", any("decide.py --skip" in d for d in rr["detail"]), repr(rr["detail"]))

# 「안 셌다(--fast)」는 「못 셌다(실패)」와도, 「0건」과도 달라야 한다. 셋이 같은 모양이면
# --fast 를 쓴 날의 화면과 토큰이 만료된 날의 화면이 구별되지 않는다.
skipped = dict(OK_PROBES, docs={"skipped": "--fast"})
rs_skip = B.rows(NOW, **skipped)
rk = find(rs_skip, "docs")
check("--fast 는 count 가 0 이 아니다", rk["count"] is None, repr(rk["count"]))
check("--fast 는 실패가 아니다", rk["error"] is None and rk["skipped"] == "--fast", repr(rk))
out_skip = B.render(NOW, rs_skip, prep={"clean": True, "ahead": 0, "behind": 0, "lock": {"held": False}})
check("화면에 「안 셈」으로 나온다", "안 셈" in out_skip, _line_of(out_skip, "미변환 첨부"))
check("--fast 는 「조회 실패」로 안 적힌다", "조회 실패" not in out_skip)
# 안 센 줄이 있는데 「할 일이 없습니다」로 닫으면 그날의 첨부가 통째로 묻힌다.
none_but_skipped = B.render(NOW, B.rows(NOW, **dict(OK_PROBES, docs={"skipped": "--fast"},
                                                    work={"items": []})),
                            prep={"clean": True, "ahead": 0, "behind": 0, "lock": {"held": False}})
check("안 센 줄이 있으면 「지금 할 일이 없습니다」로 닫지 않는다",
      "지금 할 일이 없습니다" not in none_but_skipped, _line_of(none_but_skipped, "없습니다"))
check("무엇을 안 셌는지 이름을 댄다", "미변환 첨부" in _line_of(none_but_skipped, "안 센 줄"),
      _line_of(none_but_skipped, "안 센 줄"))


print("\nrows — 마감 시각을 못 읽으면 그 사실을 적는다")

# 기본값으로 물러선 것을 말없이 넘기면, 화면의 마감이 설정과 다른데 아무도 모른다.
keep_cron = B._cron_hour
try:
    B._cron_hour = lambda key, default, cfg_path=None: (default, 0, f"{key} 크론을 읽지 못했습니다")
    rs_cron = B.rows(NOW, **OK_PROBES)
finally:
    B._cron_hour = keep_cron
rc = find(rs_cron, "docs")
check("첨부 줄에 기본값을 썼다고 적힌다", any("기본값" in d for d in rc["detail"]), repr(rc["detail"]))
check("마감은 기본값으로 그대로 선다", rc["deadline_at"] == at(2026, 8, 12, 17), repr(rc["deadline_at"]))
rw2 = find(rs_cron, "work")
check("아침 보고 줄에도 적힌다", any("기본값" in d for d in rw2["detail"]), repr(rw2["detail"]))
# 정상일 때는 이 줄이 없어야 한다 — 늘 뜨면 곧 아무도 안 읽는다.
check("설정을 읽었으면 그 줄이 없다",
      not any("기본값" in d for d in find(B.rows(NOW, **OK_PROBES), "docs")["detail"]),
      repr(find(B.rows(NOW, **OK_PROBES), "docs")["detail"]))


print("\nrows — 크론의 분도 마감에 실린다")

# `30 17 * * *` 인데 화면이 17:00 이라 말하면 30분 이르다 — 이른 쪽이라 사고는 아니지만,
# 「설정에서 읽는다」면서 반만 읽는 것이다.
with tempfile.TemporaryDirectory() as d:
    cfgm = Path(d) / "config.json"
    cfgm.write_text(json.dumps({"digest": {
        "health": {"pendingEditDays": 3, "syncWarnDays": 7},
        "ingest": {"cron": "30 7 * * *"},
        "ingestPre": {"cron": "30 17 * * *"},
    }}, ensure_ascii=False), encoding="utf-8")
    keep_cfg = B.CONFIG
    try:
        B.CONFIG = cfgm
        rs_min = B.rows(NOW, **OK_PROBES)
    finally:
        B.CONFIG = keep_cfg
    rm = find(rs_min, "docs")
    check("첨부 마감이 17:30 으로 선다", rm["deadline_at"] == at(2026, 8, 12, 17, 30), repr(rm["deadline_at"]))
    check("첨부 문구도 17:30 이라 말한다", "17:30" in rm["note"], repr(rm["note"]))
    rn = find(rs_min, "new-channel")
    check("새 채널 마감도 07:30", rn["deadline_at"] == at(2026, 8, 13, 7, 30), repr(rn["deadline_at"]))
    check("새 채널 문구도 07:30", "07:30" in rn["note"], repr(rn["note"]))


print("\nrows — 아침 보고와 새 채널은 겹쳐 세지 않는다")

work = {"items": [
    {"id": "a", "kind": "summary", "channel": "사업장나", "firstSeen": "2026-08-12"},
    {"id": "b", "kind": "note", "channel": "비공개가", "firstSeen": "2026-08-12"},
    {"id": "c", "kind": "new-channel", "channel": "신규채널", "firstSeen": "2026-08-12"},
], "generated": "2026-08-12T07:00:00+09:00"}
rows2 = B.rows(NOW, **dict(OK_PROBES, work=work))
check("아침 보고는 새 채널을 뺀 수", find(rows2, "work")["count"] == 2,
      repr(find(rows2, "work")["count"]))
check("새 채널은 따로 1건", find(rows2, "new-channel")["count"] == 1)
check("새 채널 마감은 다음 07:00", find(rows2, "new-channel")["deadline_at"] == at(2026, 8, 13, 7))
check("새 채널은 slack-sync 로 넘긴다", "slack-sync" in find(rows2, "new-channel")["skill"])


print("\nrows — 수정·삭제의 나이는 firstSeen 에서 온다")

edits = {"items": [
    {"id": "e1", "firstSeen": "2026-08-09"},
    {"id": "e2", "firstSeen": "2026-08-11"},
]}
r3 = find(B.rows(NOW, **dict(OK_PROBES, edits=edits)), "edits")
check("수정·삭제 2건", r3["count"] == 2)
check("가장 오래된 것의 나이가 붙는다", any("3일" in d for d in r3["detail"]), repr(r3["detail"]))

# 미뤄 둔 건은 만기까지 조용한 것이 「나중에」의 약속이다 (07:00 보고·09:00 점검이 그렇게 한다).
held_edits = {"items": [{"id": "e1", "firstSeen": "2026-08-09"}],
              "deferred": [{"id": "e2", "firstSeen": "2026-08-01"}]}
r3b = find(B.rows(NOW, **dict(OK_PROBES, edits=held_edits)), "edits")
check("미뤄 둔 것은 세는 수에서 빠진다", r3b["count"] == 1, repr(r3b["count"]))
check("미뤄 둔 것이 있다는 것은 보인다", any("보류 1건" in d for d in r3b["detail"]), repr(r3b["detail"]))
check("나이도 미뤄 둔 것을 빼고 잰다", any("3일" in d for d in r3b["detail"]), repr(r3b["detail"]))


print("\nrows — 수정·삭제의 「N일 넘기면」도 설정에서 온다")

# 09:00 점검은 `pendingEditDays` 로 움직인다 (`archive-health.js` 의 DEFAULTS). 여기 3을
# 박아 두면 설정을 바꾼 날부터 화면과 점검이 서로 다른 날을 말한다 — 에러 없이.
# ★ 실제 config.json 에 대고도 돈다 (`_cron_hour` 시험과 같은 이유) — 값을 바꾸면 여기가 먼저 깨진다.
check("지금 설정(3일)대로 적힌다", "3일 넘기면" in (r3["note"] or ""), repr(r3["note"]))

with tempfile.TemporaryDirectory() as d:
    cfg5 = Path(d) / "config.json"
    cfg5.write_text(json.dumps({"digest": {
        "health": {"pendingEditDays": 5, "syncWarnDays": 7},
        "ingest": {"cron": "0 7 * * *"},
        "ingestPre": {"cron": "0 17 * * *"},
    }}, ensure_ascii=False), encoding="utf-8")
    keep_cfg = B.CONFIG
    try:
        B.CONFIG = cfg5
        r3c = find(B.rows(NOW, **dict(OK_PROBES, edits=edits)), "edits")
        check("설정이 5일이면 문구도 5일", "5일 넘기면" in (r3c["note"] or ""), repr(r3c["note"]))
        # 키가 없으면 기본값으로 물러선다 (`_sync_warn_days` 와 같은 모양).
        cfg5.write_text(json.dumps({"digest": {}}, ensure_ascii=False), encoding="utf-8")
        r3d = find(B.rows(NOW, **dict(OK_PROBES, edits=edits)), "edits")
        check("키가 없으면 기본값 3일", "3일 넘기면" in (r3d["note"] or ""), repr(r3d["note"]))
    finally:
        B.CONFIG = keep_cfg


print("\nrows — 동기화 밀림")

r4 = find(B.rows(NOW, **dict(OK_PROBES, sync={"last_sync": "2026-08-05T07:00:00+09:00"})), "sync")
check("7일 밀린 것을 센다", r4["count"] == 7, repr(r4["count"]))
r5 = find(B.rows(NOW, **dict(OK_PROBES, sync={})), "sync")
check("상태 파일이 없으면 0일이 아니다", r5["count"] is None, repr(r5["count"]))

# `last_sync` 는 **UTC 로 적힌다** (`slack-archive.js` 의 toISOString). 07:00 KST 반영은 UTC 로
# 전날 22:00 이라, 날짜끼리 빼면 아침 내내 「1일 밀림」이 뜬다. 같은 순간 09:00 점검은
# 경과 시간 기준이라 0일이라고 말한다 — 두 화면이 다른 숫자를 말하면 어느 쪽도 못 믿는다.
r6 = find(B.rows(at(2026, 8, 12, 8, 0), **dict(OK_PROBES, sync={"last_sync": "2026-08-11T22:00:00Z"})), "sync")
check("UTC 로 적힌 07:00 반영을 하루로 세지 않는다", r6["count"] == 0, repr(r6["count"]))
check("마지막 동기화 날짜도 KST 로 적는다", any("2026-08-12" in d for d in r6["detail"]), repr(r6["detail"]))
# 08-09 22:00Z = 08-10 07:00 KST → 08-12 08:00 KST 까지 이틀 하고 한 시간
r7 = find(B.rows(at(2026, 8, 12, 8, 0), **dict(OK_PROBES, sync={"last_sync": "2026-08-09T22:00:00Z"})), "sync")
check("이틀 지난 것은 그대로 2일", r7["count"] == 2, repr(r7["count"]))


# ─────────────────────────────────────────────────────────────
print("\nrender — 순서와 0건 줄")

full = B.rows(NOW, **OK_PROBES)
out = B.render(NOW, full, prep={"clean": True, "ahead": 0, "behind": 0, "lock": {"held": False}})

check("남은 것이 있는 줄에만 번호가 붙는다", out.count("\n1  ") == 1 or "\n1  " in out)
check("첨부가 맨 위에 온다 (마감이 가깝다)",
      out.index("미변환 첨부") < out.index("아침 보고"), "")
check("0건 줄도 남는다", "아침 보고" in out and "0건" in out)
check("다음은 몇 번인지 말한다", "다음은 1번" in out, _line_of(out, "다음은"))

# 할 일이 하나도 없을 때 — 「없다」를 분명히 말해야 한다
none_probes = dict(OK_PROBES, docs={"total": 0, "scanDays": 60, "approvals": [], "deferred": [],
                                    "failed": [], "byChannel": []})
out_none = B.render(NOW, B.rows(NOW, **none_probes),
                    prep={"clean": True, "ahead": 0, "behind": 0, "lock": {"held": False}})
check("할 일이 없으면 그렇게 말한다", "없습니다" in out_none, _line_of(out_none, "없습니다"))


print("\nrender — 준비 상태")

out_dirty = B.render(NOW, full, prep={"clean": False, "dirty": ["50-resources/x.md"],
                                      "ahead": 0, "behind": 0, "lock": {"held": False}})
check("작업 트리가 더러우면 멈추라고 한다", "멈춥" in out_dirty or "정리" in out_dirty)
out_behind = B.render(NOW, full, prep={"clean": True, "ahead": 0, "behind": 3,
                                       "lock": {"held": False}})
check("origin 이 앞서 있으면 pull 하라고 한다", "pull" in out_behind)
out_ahead = B.render(NOW, full, prep={"clean": True, "ahead": 2, "behind": 0,
                                      "lock": {"held": False}})
check("안 나간 커밋이 있으면 보인다", "2" in _line_of(out_ahead, "준비"), _line_of(out_ahead, "준비"))


# ─────────────────────────────────────────────────────────────
print("\nlock_state — 자동으로 풀지 않는다")

with tempfile.TemporaryDirectory() as d:
    lp = Path(d) / ".archive-run-lock.json"

    check("파일이 없으면 안 잠겨 있다", B.lock_state(lp, NOW)["held"] is False)

    lp.write_text(json.dumps({"pid": 1234, "since": at(2026, 8, 12, 13, 20).isoformat(),
                              "section": "doc-archive"}, ensure_ascii=False), encoding="utf-8")
    st = B.lock_state(lp, NOW)
    check("1시간 전 잠금은 살아 있다", st["held"] and st["stale"] is False, repr(st))
    check("어느 구간인지 들고 있다", st["section"] == "doc-archive")

    lp.write_text(json.dumps({"pid": 1234, "since": at(2026, 8, 12, 9, 20).isoformat(),
                              "section": "doc-archive"}, ensure_ascii=False), encoding="utf-8")
    st2 = B.lock_state(lp, NOW)
    # 자동 해제는 안 한다 — 실제로 도는 다른 창의 세션을 밟는다.
    check("5시간 지난 잠금은 오래된 것으로 표시만", st2["held"] and st2["stale"] is True, repr(st2))
    check("파일을 지우지 않는다", lp.exists())

    lp.write_text("{ 깨진 json", encoding="utf-8")
    st3 = B.lock_state(lp, NOW)
    # 못 읽는 잠금을 「없음」으로 읽으면 두 세션이 동시에 돈다. 있는 것으로 본다.
    check("못 읽는 잠금은 없는 것으로 보지 않는다", st3["held"] is True, repr(st3))

    out_lock = B.render(NOW, full, prep={"clean": True, "ahead": 0, "behind": 0, "lock": st2})
    check("오래된 잠금이면 해제 명령을 보인다", "--unlock" in out_lock, _line_of(out_lock, "잠금"))


print("\n--lock — 이미 잠겨 있으면 종료코드 2")

# 이 스킬의 잠금은 **종료코드로만** 다음 세션을 막는다. 그런데 여기까지 시험이 없어서
# 그 숫자가 한 번도 검증된 적이 없었다 (2026-08-12 리뷰). 실제 잠금 파일은 안 건드리고
# 임시 경로로 갈아끼워서만 돈다.
with tempfile.TemporaryDirectory() as d:
    keep_lock = B.LOCK
    try:
        B.LOCK = Path(d) / ".archive-run-lock.json"

        check("처음 걸면 0", B.main(["--lock", "doc-archive"]) == 0)
        check("잠금 파일이 생긴다", B.LOCK.exists())
        # ★ 이것이 이 구간의 이유다 — 두 세션이 겹치면 상태 파일이 통째로 덮인다.
        check("이미 잠겨 있으면 2", B.main(["--lock", "slack-sync"]) == 2)

        held = json.loads(B.LOCK.read_text(encoding="utf-8"))
        check("앞 세션의 구간이 안 덮인다", held["section"] == "doc-archive", repr(held["section"]))

        # 확인과 쓰기가 나뉘어 있으면 그 사이에 들어온 세션의 잠금을 덮어쓴다. `"x"` 는
        # 「없을 때만 만든다」가 한 동작이라 그 사이가 없다 — 파일이 이미 있으면 **손도
        # 안 댄다**는 것을 여기서 못 박는다 (2026-08-12 WHK 결정으로 닫은 M10).
        before = B.LOCK.read_bytes()
        st = B.take_lock("slack-sync", B.LOCK)
        check("이미 있으면 못 걸었다고 답한다", st.get("acquired") is False, repr(st.get("acquired")))
        check("남의 잠금 파일을 한 글자도 안 고친다", B.LOCK.read_bytes() == before)
        check("누가 걸고 있는지는 그대로 알려준다", st.get("section") == "doc-archive", repr(st.get("section")))

        check("풀면 0", B.main(["--unlock"]) == 0)
        check("풀면 파일이 사라진다", not B.LOCK.exists())
        check("푼 뒤에는 다시 걸린다", B.main(["--lock", "slack-sync"]) == 0)
        B.release_lock(B.LOCK)

        check("안 걸린 것을 풀어도 멈추지 않는다", B.main(["--unlock"]) == 0)

        # 전에는 `--unlock` 만 돌고 `--lock` 을 말없이 버렸다 — 잠금이 안 걸렸는데 오류도
        # 안 나서, 걸린 줄 알고 구간을 시작하게 된다.
        check("--lock 과 --unlock 을 함께 주면 멈춘다", B.main(["--lock", "doc-archive", "--unlock"]) == 2)
        check("그때 잠금 파일을 만들지 않는다", not B.LOCK.exists())

        B.take_lock("doc-archive", B.LOCK)
        check("걸려 있을 때 함께 줘도 풀지 않는다",
              B.main(["--lock", "slack-sync", "--unlock"]) == 2 and B.LOCK.exists())
        B.release_lock(B.LOCK)

        # **`--json` 을 잠금과 함께 주면 거절한다** (2026-08-13 리뷰). 전에는 「잠금을
        # 걸었습니다 — … (pid …)」라는 한국어 산문을 내고 0 으로 끝나서, JSON 을 기다리던
        # 쪽은 파싱에 실패했다. 기계용 출력을 달라고 했는데 사람 말이 나오면 안 된다.
        check("--lock 과 --json 을 함께 주면 멈춘다", B.main(["--lock", "doc-archive", "--json"]) == 2)
        check("그때도 잠금 파일을 만들지 않는다", not B.LOCK.exists())
        check("--unlock 과 --json 을 함께 줘도 멈춘다", B.main(["--unlock", "--json"]) == 2)
    finally:
        B.LOCK = keep_lock

check("실제 잠금 파일은 안 건드렸다", B.LOCK.name == ".archive-run-lock.json", str(B.LOCK))


# ─────────────────────────────────────────────────────────────
print("\nprobe — 파일이 없는 것은 「0건」이지 「실패」가 아니다")

# `.pending-edits.json`·`.pending-work.json` 은 **남은 것이 없으면 쓰는 쪽이 지운다**
# (`slack-archive.js` 의 `ingestConversations` 안 `PENDING_FILE` 쓰기 · `pending-work.js` 의 `runPendingWork`, "빈 목록을 남겨 두면 볼 것이
# 있다로 읽힌다"). 그래서 없음 = 0건이다. 이걸 실패로 읽으면 **할 일 없는 날마다
# 「조회 실패」가 떠서** 곧 아무도 안 읽게 되고, 그때 진짜 실패도 같이 묻힌다.
with tempfile.TemporaryDirectory() as d:
    gone = Path(d) / ".pending-edits.json"
    r = B.probe_edits(gone)
    check("없는 수정·삭제 파일은 0건", r.get("items") == [] and not r.get("error"), repr(r))

    gone_w = Path(d) / ".pending-work.json"
    rw = B.probe_work(gone_w)
    check("없는 아침 보고 파일도 0건", rw.get("items") == [] and not rw.get("error"), repr(rw))

    broken = Path(d) / ".pending-edits.json"
    broken.write_text("{ 깨진", encoding="utf-8")
    rb2 = B.probe_edits(broken)
    # 있는데 못 읽는 것은 다르다 — 그건 진짜 실패다.
    check("있는데 못 읽으면 실패로 간다", rb2.get("error") and "items" not in rb2, repr(rb2))


print("\nprobe_edits — 미뤄 둔 건은 만기까지 조용하다")

# `.pending-edits.json` 은 미룬 것을 `items` 에 **그대로 남긴다** (`slack-archive.js` 의 `ingestConversations` 안 「나중에」 제외 —
# "파일에는 그대로 남긴다, 빼는 것은 보고뿐"). 그래서 파일을 그냥 세면 07:00·09:00 이
# 조용한 건이 상황판에만 되살아난다. 거르는 규칙은 `.sync-state.json` 의 `deferred`
# (`util.js` 의 activeDeferred — `until` 이 오늘 이후인 것만 살아 있다).
with tempfile.TemporaryDirectory() as d:
    pe = Path(d) / ".pending-edits.json"
    ss = Path(d) / ".sync-state.json"
    today = B.now_kst().date()
    pe.write_text(json.dumps({"items": [
        {"id": "keep", "firstSeen": "2026-08-09"},
        {"id": "held", "firstSeen": "2026-08-01"},
        {"id": "expired", "firstSeen": "2026-08-02"},
    ]}, ensure_ascii=False), encoding="utf-8")
    ss.write_text(json.dumps({"deferred": {
        "held": {"until": (today + timedelta(days=3)).isoformat()},
        "expired": {"until": (today - timedelta(days=1)).isoformat()},
    }}, ensure_ascii=False), encoding="utf-8")

    got = B.probe_edits(pe, state_path=ss)
    ids = [it["id"] for it in got["items"]]
    check("만기 안 지난 것은 빠진다", "held" not in ids, repr(ids))
    check("만기 지난 것은 돌아온다", "expired" in ids, repr(ids))
    check("미룬 적 없는 것은 그대로", "keep" in ids, repr(ids))
    check("뺀 것은 따로 들고 있는다", len(got["deferred"]) == 1, repr(got["deferred"]))

    # 상태 파일을 못 읽는다고 미룬 것이 없다고 볼 수는 없지만, 여기서 멈추면 화면이 통째로
    # 안 뜬다. 못 읽으면 **전부 보이는 쪽**으로 간다 — 덜 보이는 것보다 낫다.
    ss.write_text("{ 깨진", encoding="utf-8")
    got2 = B.probe_edits(pe, state_path=ss)
    check("상태 파일이 깨지면 전부 보인다", len(got2["items"]) == 3, repr(len(got2["items"])))


print("\n상태 파일이 아예 없을 때 (새 PC)")

empty = B.rows(NOW, work={}, edits={}, sync={}, docs={"total": 0, "scanDays": 60, "approvals": [],
                                                      "deferred": [], "failed": [], "byChannel": []})
check("멈추지 않고 행을 만든다", len(empty) >= 5, repr(len(empty)))
check("아침 보고는 0건 (파일이 없으면 할 일이 없는 것)", find(empty, "work")["count"] == 0)


print("\nprobe_git — 한글 파일명이 읽히는가")

# git 은 기본값에서 한글 경로를 `"\354\262\255..."` 로 감싸 내보낸다. 준비 줄은 그 이름을
# 사람에게 보여 「내 것인지 남의 것인지」 확인하게 하는 자리라, 이름이 안 읽히면 그 줄이
# 제 일을 못 한다. 이 저장소는 파일명이 거의 다 한글이다 (2026-08-12 실측으로 발견).
# **완전히 별개인 임시 저장소**에서만 돈다 — 워크스페이스 저장소는 건드리지 않는다.
import os  # noqa: E402
import subprocess  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    repo = Path(d)
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, env=env)
    run("init", "-q")
    (repo / "사업장다.md").write_text("본문", encoding="utf-8")
    run("add", "사업장다.md")

    keep = B.ROOT
    try:
        B.ROOT = repo
        prep = B.probe_git(fetch=False)
    finally:
        B.ROOT = keep

    dirty = prep.get("dirty") or []
    check("스테이징된 파일을 잡는다", len(dirty) == 1, repr(dirty))
    check("한글 이름이 그대로 나온다", any("사업장다" in p for p in dirty), repr(dirty))
    check("이스케이프된 채로 나오지 않는다", not any("\\3" in p for p in dirty), repr(dirty))


print("\nprobe_git — 명령이 실패한 것을 「깨끗함」으로 읽지 않는다")

# 종료코드를 안 보면 `git status` 가 실패했을 때 출력이 비어 dirty=[] 가 되고, 화면은
# 「git 깨끗함 · origin 과 같음」이라고 단정한다. 표에서만 셋으로 나누고 준비 줄이
# 단정하면 같은 고장이 자리만 옮긴 것이다 (2026-08-12 리뷰).
class _Fake:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _with_run(fake_run, fn=None):
    keep = B._run
    try:
        B._run = fake_run
        return (fn or (lambda: B.probe_git(fetch=True)))()
    finally:
        B._run = keep


st_fail = _with_run(lambda cmd, **kw: _Fake(128, "", "fatal: not a git repository")
                    if "status" in cmd else _Fake(0, "0\n"))
check("status 가 실패하면 clean 은 참이 아니다", st_fail["clean"] is None, repr(st_fail["clean"]))
line = _line_of(B.render(NOW, full, prep=dict(st_fail, lock={"held": False})), "준비")
check("화면에 「확인 못 함」이라고 적는다", "확인 못 함" in line, line)
check("「깨끗함」이라고 적지 않는다", "깨끗함" not in line, line)

fetch_fail = _with_run(lambda cmd, **kw: _Fake(128, "", "fatal: could not read Username")
                       if "fetch" in cmd else _Fake(0, "0\n"))
check("fetch 가 실패하면 대조를 못 한 것으로", fetch_fail["compare"] == "failed", repr(fetch_fail["compare"]))
line2 = _line_of(B.render(NOW, full, prep=dict(fetch_fail, lock={"held": False})), "준비")
check("「origin 과 같음」이라고 단정하지 않는다", "origin 과 같음" not in line2, line2)
check("대조 못 했다고 적는다", "대조 못 함" in line2, line2)

# --fast 는 fetch 를 아예 건너뛴다. 그때도 문구가 「origin 과 같음」이면 --fast 를 쓴 날과
# 정말 같은 날이 구별되지 않는다.
keep_run = B._run
try:
    B._run = lambda cmd, **kw: _Fake(0, "0\n")
    fast = B.probe_git(fetch=False)
finally:
    B._run = keep_run
line3 = _line_of(B.render(NOW, full, prep=dict(fast, lock={"held": False})), "준비")
check("--fast 는 origin 을 안 봤다고 적는다", "대조 안 함" in line3, line3)

# 추적 안 되는 파일: dirty 로는 안 세되(받은편지함에 상시로 있다) 없는 셈 치지도 않는다.
untracked = _with_run(lambda cmd, **kw: _Fake(0, "?? documents/사업장나/계약서.md\n?? 메모.md\n")
                      if "status" in cmd else _Fake(0, "0\n"))
check("추적 안 되는 것은 dirty 가 아니다", untracked["clean"] is True, repr(untracked["dirty"]))
check("그래도 개수는 들고 있다", len(untracked["untracked"]) == 2, repr(untracked["untracked"]))
# 추적 브랜치가 없는 브랜치에서 돌리면 `origin/main` 으로 물러서는데, 그 수는 「내 브랜치가
# 얼마나 앞섰나」가 아니다. 말없이 물러서면 엉뚱한 수를 「안 나간 커밋」으로 읽는다.
no_up = _with_run(lambda cmd, **kw: _Fake(128, "", "fatal: no upstream configured")
                  if "@{upstream}" in cmd else _Fake(0, "0\n"))
check("추적 브랜치가 없어도 대조는 한다", no_up["compare"] == "ok", repr(no_up["compare"]))
out_nu = B.render(NOW, full, prep=dict(no_up, lock={"held": False}))
check("무엇과 견줬는지 적는다", "origin/main 과 견줌" in out_nu, _line_of(out_nu, "견줌"))
# 추적 브랜치가 있으면 그 줄이 없어야 한다 (늘 뜨면 곧 아무도 안 읽는다).
with_up = _with_run(lambda cmd, **kw: _Fake(0, "origin/main\n") if "@{upstream}" in cmd else _Fake(0, "0\n"))
check("추적 브랜치가 있으면 그 줄이 없다", with_up.get("compare_note") is None, repr(with_up.get("compare_note")))

out_u = B.render(NOW, full, prep=dict(untracked, lock={"held": False}))
check("추적 안 되는 파일 개수가 보인다", "추적 안 되는 파일 2개" in out_u, _line_of(out_u, "준비"))
check("아카이브 폴더면 다른 창을 의심하라고 한다", "다른 창이 문서 변환 중" in out_u,
      _line_of(out_u, "다른 창"))

print("\n커밋·push 관문이 켜져 있나 — 꺼진 것을 알려주는 신호가 없었다")

# 훅은 저장소 안에 있지만 git 은 훅을 복제하지 않는다. `core.hooksPath` 를 안 걸면
# 관문이 통째로 꺼지는데, 그 사실을 알려주는 신호가 「커밋이 그냥 통과하는 것」뿐이었다 —
# 정상 동작과 구별되지 않는다. 그동안 스킬 문서 넷은 「건너뛰면 커밋이 막힌다」고 적었다.
_base = {"clean": True, "ahead": 0, "behind": 0, "lock": {"held": False}}

on = B.render(NOW, full, prep=dict(_base, hooks={"on": True, "value": ".githooks"}))
# 정상인 것을 매번 적으면 준비 줄이 길어지고, 길어지면 안 읽힌다.
check("켜져 있으면 아무 말도 안 한다", "관문" not in on, _line_of(on, "준비"))

off = B.render(NOW, full, prep=dict(_base, hooks={"on": False, "value": ""}))
check("꺼져 있으면 준비 줄에 적는다", "커밋·push 관문 꺼져 있음" in off, _line_of(off, "준비"))
# 스위치는 하나라 `pre-push` 도 함께 꺼진다. 화면이 커밋만 말하면 실제보다 좁게 알린다.
check("push 관문도 함께 꺼진다고 적는다", "pre-push" in off, _line_of(off, "pre-commit"))
check("무엇이 안 지켜지는지 적는다", "그냥 통과합니다" in off, _line_of(off, "그냥 통과"))
# **상대경로를 적어 주면 안 된다** (2026-08-31 이사). 자료 저장소에는 `.githooks/` 가
# 없어서 `core.hooksPath .githooks` 는 아무 훅도 안 켜고, 제대로 걸려 있던 것을 덮어
# 꺼 버린다. 안내대로 따랐더니 관문이 꺼지는 것이 이 검사가 막으려는 것이다.
check("켜는 명령을 적는다",
      f'git config core.hooksPath "{B.HOOKS_TARGET.as_posix()}"' in off, _line_of(off, "켜기"))
check("켜는 명령이 상대경로가 아니다",
      "core.hooksPath .githooks" not in off, _line_of(off, "켜기"))

# 다른 값이 걸려 있는 것과 아예 없는 것은 손볼 곳이 다르다.
other = B.render(NOW, full, prep=dict(_base, hooks={"on": False, "value": "my-hooks"}))
check("다른 값이 걸려 있으면 그 값을 보인다", "지금 값: my-hooks" in other, _line_of(other, "지금 값"))

# git 을 아예 못 부른 회차. 「꺼져 있다」고 단정하면 안 된다 — 모르는 것이다.
unk = B.render(NOW, full, prep=dict(_base, hooks={"on": None, "error": "git 없음"}))
check("못 봤으면 「꺼짐」이라 안 한다", "커밋·push 관문 꺼져 있음" not in unk, _line_of(unk, "준비"))
check("못 봤다고 적는다", "커밋·push 관문 확인 못 함" in unk, _line_of(unk, "준비"))

# `hooks` 키가 아예 없는 prep. **조용히 넘어가지 않는다** — 안 본 것을 「켜져 있다」로
# 읽으면 이 줄을 넣은 이유가 사라진다. 모르면 모른다고 적는 쪽이 이 화면의 규칙이다.
_none = B.render(NOW, full, prep=dict(_base))
check("hooks 가 없으면 「모른다」로 적는다", "커밋·push 관문 확인 못 함" in _none, _line_of(_none, "준비"))
check("그때 「꺼짐」이라 단정하지 않는다", "커밋·push 관문 꺼져 있음" not in _none, _line_of(_none, "준비"))

# probe_hooks: **판정은 `git rev-parse --git-path hooks` 로 한다** (2026-08-13 리뷰).
#
# 전에는 `git config --get core.hooksPath` 값을 `".githooks"` 라는 **글자와 그대로 대봤다.**
# 그래서 같은 자리를 가리키는 다른 표기(`./.githooks`·절대경로·역슬래시)를 「꺼져 있음」으로
# 읽었다 — 훅은 도는데 화면이 걸으라고 시킨다. 표기 차이는 git 이 흡수하게 맡긴다.
def _hooks_run(where_rc, where_out, cfg_out=""):
    """rev-parse 와 config 두 호출에 각각 다른 답을 주는 스텁."""
    def run(cmd, **kw):
        if "rev-parse" in cmd:
            return _Fake(where_rc, where_out)
        return _Fake(0 if cfg_out else 1, cfg_out)
    return run


# **훅은 코드 저장소에 있고, 이 검사는 자료 저장소에서 돈다** (2026-08-31 이사).
# 커밋이 일어나는 곳은 자료 저장소인데 훅 스크립트는 코드 저장소에 있어서, 자료 쪽에는
# `core.hooksPath` 를 코드 저장소의 **절대경로**로 걸어 둔다. 그래서 「켜짐」의 뜻은
# 「자료 저장소의 훅 경로가 **코드 저장소의** .githooks 를 가리킨다」이다.
#
# 이사 전에는 `ROOT / ".githooks"` 와 댔고, 이 시험도 `B.ROOT` 로 절대경로를 만들었다.
# 그래서 **제대로 걸어 둔 상태를 「꺼짐」이라 말하는데도 시험은 초록이었다.**
_h_off = _with_run(_hooks_run(0, ".git/hooks\n"), fn=B.probe_hooks)
check("설정이 없으면 꺼진 것으로 본다", _h_off["on"] is False and _h_off["value"] == "", repr(_h_off))

_CODE_HOOKS = B.HERMES / ".githooks"
_h_code = _with_run(_hooks_run(0, _CODE_HOOKS.as_posix() + "\n", _CODE_HOOKS.as_posix()),
                    fn=B.probe_hooks)
check("코드 저장소의 .githooks 를 가리키면 켜진 것", _h_code["on"] is True, repr(_h_code))

# 같은 자리를 가리키는 다른 표기. 표기 차이는 git 과 resolve() 가 흡수해야 한다.
_h_back = _with_run(_hooks_run(0, str(_CODE_HOOKS) + "\n", str(_CODE_HOOKS)), fn=B.probe_hooks)
check("역슬래시 표기도 켜진 것", _h_back["on"] is True, repr(_h_back))

# **자료 저장소 안의 .githooks 는 켜진 것이 아니다.** 거기엔 훅이 없다 —
# 상대경로 `.githooks` 를 자료 저장소에 걸면 정확히 이 상태가 되고, 훅은 안 돈다.
_h_data_rel = _with_run(_hooks_run(0, ".githooks\n", ".githooks"), fn=B.probe_hooks)
check("자료 저장소의 .githooks 는 꺼진 것", _h_data_rel["on"] is False, repr(_h_data_rel))

# 정말 다른 디렉터리를 걸어 둔 것은 꺼진 것이다 — 우리 관문은 안 돈다.
_h_other = _with_run(_hooks_run(0, "my-hooks\n", "my-hooks"), fn=B.probe_hooks)
check("다른 디렉터리는 꺼진 것", _h_other["on"] is False and _h_other["value"] == "my-hooks",
      repr(_h_other))

_h_bad = _with_run(_hooks_run(128, "", ""), fn=B.probe_hooks)
check("git 이 죽으면 「모른다」", _h_bad["on"] is None and _h_bad.get("error"), repr(_h_bad))
# 빈 답도 「모른다」다 — 빈 문자열을 경로로 쓰면 ROOT 자신이 되어 「꺼짐」으로 단정하게 된다.
_h_empty = _with_run(_hooks_run(0, "\n"), fn=B.probe_hooks)
check("빈 답도 「모른다」", _h_empty["on"] is None and _h_empty.get("error"), repr(_h_empty))

print()
print("전부 통과" if _ok else "실패 있음")
sys.exit(0 if _ok else 1)
