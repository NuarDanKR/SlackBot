#!/usr/bin/env python3
"""
아카이브 한 바퀴 — 지금 사람이 손대야 하는 것을 한 표로 보인다.

  python board.py              # 상황판 (슬랙 조회 포함, 약 20초)
  python board.py --fast       # 슬랙을 안 훑는다 (그 두 줄은 「안 셈」으로 나온다)
  python board.py --json       # 기계용
  python board.py --lock doc-archive   # 잠금 걸기
  python board.py --unlock             # 잠금 풀기

종료코드: 0 (할 일 유무와 무관) / 2 = 잠금이 걸려 있음

── 새로 세지 않는다 ──

세는 코드는 이미 있다. 09:00 위생 점검이 쓰는 `src/archive-health.js` 의
`pendingDocuments()` 와, `archive-inbox` 의 `review_work.py --json` 이 그것이다.
여기서 다시 구현하면 두 숫자가 갈리고, 갈리면 한쪽은 「N건 남음」 한쪽은 「0건」이라고
말한다 (2026-08-04 에 문서 쪽에서 34건 vs 1건이 실제로 났다).

**DM 본문을 파싱하지 않는다.** 위생 점검 DM 은 사람이 읽는 한국어 산문이라 문구가
바뀌면 에러 없이 0건이 된다. 그래서 값은 함수에서 직접 받는다
(`scripts/board-docs.js`).

── 이 화면의 가장 위험한 고장 ──

에러가 아니라 **못 세었는데 0건으로 보이는 것**이다. 그러면 화면은 「오늘 할 일 없음」이
되고 사람은 그대로 닫는다. 39건이 밀려 있어도 그렇게 보인다. 그래서 값은 네 가지로
나뉜다 — **셌다(숫자) · 일부만 셌다(`+`) · 안 셌다(--fast) · 못 셌다(실패)**. 뒤 셋은
절대 0 으로 적지 않는다.

「일부만 셌다」가 넷째로 붙은 이유(2026-08-12 리뷰): 슬랙 조회는 채널별로 도는데 몇 개가
실패해도 `pendingDocuments()` 는 **성공한 채널만 세서 total 을 돌려준다**
(`archive-health.js` 의 `pendingDocuments` 안 채널 순회 — 실패는 `failed` 로만 나온다). 그 값을 확정값으로 찍으면 셋 다 실패한 날 화면이
「0건 · 지금 할 일이 없습니다」가 된다 — 이 스킬이 막으려던 바로 그 화면이다.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1] / "slack-sync" / "scripts"))
sys.path.insert(0, str(_HERE.parents[1] / "_shared"))

from paths import CODE_ROOT, CONFIG  # noqa: E402
from sync_index import ARCHIVE, ROOT  # noqa: E402
import tz as _tz  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 정본은 `_shared/tz.py` (2026-09-03) — config.json 의 timezone 을 읽는다. 전에는 여기서
# 고정 +09:00 을 직접 만들었는데, 그 값이 config.json 과 갈리는 날(예: timezone 을
# Asia/Seoul 이 아닌 값으로 바꾸는 날) 조용히 어긋난다.
KST = _tz.TZINFO

PENDING_WORK = ARCHIVE / ".pending-work.json"
PENDING_EDITS = ARCHIVE / ".pending-edits.json"
SYNC_STATE = ARCHIVE / ".sync-state.json"
# 「이 PC 에서 지금 한 바퀴가 돌고 있다」는 사실. 저장소에 안 들어간다 (.gitignore).
LOCK = ARCHIVE / ".archive-run-lock.json"

# 봇 소스는 **코드 저장소**에 있다. `ROOT` 는 자료 저장소라 여기 쓰면 안 된다.
HERMES = CODE_ROOT
BOARD_DOCS = HERMES / "scripts" / "board-docs.js"
REVIEW_WORK = _HERE.parents[1] / "archive-inbox" / "scripts" / "review_work.py"

# 커밋·push 관문을 켜는 설정값. `git config core.hooksPath` 가 이 자리를 가리켜야 훅이 돈다.
# **스위치는 이 하나이고 `.githooks/` 의 훅을 전부 켠다** — `pre-commit`(2026-08-12 부터)
# 과 `pre-push`(2026-08-30 부터). 둘 다 2026-08-12 부터 저장소 안에 있다.
#
# **훅은 코드 저장소에 있다** (2026-08-31 이사). 커밋이 일어나는 곳은 자료 저장소라,
# 자료 쪽에는 코드 저장소의 **절대경로**를 건다. 그래서 「걸어야 할 값」은 저장소마다
# 다르고, 이 설정은 기계마다 절대경로라 저장소에 실리지 않는다 — 새 PC 에서는 다시 건다.
HOOKS_DIR = ".githooks"
HOOKS_TARGET = (CODE_ROOT / HOOKS_DIR).resolve()

# 잠금을 자동으로 풀지 않는 대신, 이만큼 지나면 「오래된 것」으로 표시한다.
STALE_HOURS = 4
# 자동 반영 시각의 **기본값**. 실제 값은 자료 저장소의 `config.json` 크론에서 읽는다
# (`_cron_hour`). 여기 값만 두고 읽지 않으면 설정을 바꿨을 때 화면이 조용히 낡는다.
INGEST_HOUR = 7
PRE_DIGEST_HOUR = 17

DAYS = "월화수목금토일"


# ─────────────────────────────────────────────────────────── 시각

def now_kst() -> datetime:
    """지금. **기계 로컬 시간대로 재지 않는다.**

    마감이 07:00·17:00 이라 시간대가 어긋나면 「오늘 안에 되나」가 통째로 뒤집힌다.
    `_shared/tz.py` 로 위임한다 — config.json 의 timezone 을 읽고, 그 값이 Asia/Seoul 인
    동안만 유효한 고정 +09:00 폴백을 쓴다(윈도우에 IANA 자료가 없을 때).
    """
    return _tz.now()


def next_at(now: datetime, hour: int, minute: int = 0) -> datetime:
    """다음 그 시각. **정각은 이미 지난 것으로 본다** — 그 순간 자동 반영이 돌고 있다."""
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if now < today else today + timedelta(days=1)


def deadline_text(now: datetime, at: datetime) -> str:
    """`17:00 (2시간 40분)`. 시각만 적으면 오늘인지 내일인지가 안 보인다."""
    if at is None:
        return "—"
    hhmm = f"{at.hour:02d}:{at.minute:02d}"
    mins = int((at - now).total_seconds() // 60)
    if mins < 0:
        return f"{hhmm} (지났음)"
    if mins < 60:
        return f"{hhmm} ({mins}분)"
    h, m = divmod(mins, 60)
    left = f"{h}시간 {m}분" if m else f"{h}시간"
    return f"{hhmm} ({left})"


def _parse_dt(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


def _age_days(first_seen, now: datetime):
    """`firstSeen`(YYYY-MM-DD) 부터 오늘까지 며칠. 못 읽으면 None."""
    d = _parse_dt(first_seen)
    if d is None:
        return None
    return (now.date() - d.date()).days


def _sync_warn_days(default=7) -> int:
    """며칠 밀리면 09:00 점검이 경고하나. **여기 숫자를 적지 않고 설정에서 읽는다.**"""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return int(cfg["digest"]["health"]["syncWarnDays"])
    except Exception:
        return default


def _pending_edit_days(default=3) -> int:
    """수정·삭제가 며칠 넘으면 09:00 점검에 뜨나. **여기 숫자를 적지 않고 설정에서 읽는다**
    (`_sync_warn_days` 와 같은 이유 — 점검은 `pendingEditDays` 로 움직인다)."""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return int(cfg["digest"]["health"]["pendingEditDays"])
    except Exception:
        return default


def _cron_hour(key: str, default: int, cfg_path: Path = None):
    """자동 반영이 몇 시 몇 분에 도나. `(시, 분, 못 읽은 사유)` — 읽었으면 사유는 None.

    **분 칸도 값이다** (2026-09-04 리뷰). 전에는 분을 버려 `30 7 * * *` 이 07:00 으로
    보였다 — 이른 쪽이라 사고는 아니지만, 「설정에서 읽는다」면서 반만 읽는 것이다.

    **여기 숫자를 적지 않고 설정에서 읽는다** (`_sync_warn_days` 와 같은 이유). 마감은
    화면이 「오늘 안에 되나」를 말하는 근거라, 설정을 바꿨는데 화면이 옛 시각을 계속
    보여주면 **에러 없이** 하루가 어긋난다.

    읽는 것은 `"0 7 * * *"` 처럼 **분·시가 숫자 하나이고 날짜·달·요일이 전부 `*`** 인
    것뿐이다. `*/30` 이나 `0 7,17 * * *` 는 마감이 하나가 아니라 이 칸에 적을 수가 없으니
    읽지 않는다 — 그때는 기본값으로 가되 **사유를 돌려줘서 화면이 그 사실을 적게 한다.**

    **뒤 세 칸도 본다** (2026-08-13 리뷰). 전에는 `\\S+` 로 받아 `0 7 * * 1-5`(평일만)를
    「매일 07:00」으로 읽었다. 그러면 **토요일 화면이 오지 않을 마감을 약속하고**, 사유도
    없으니 사람은 그 값을 믿는다. 「매일 그 시각」이 아닌 것은 이 칸의 뜻과 다르므로 읽지
    않는다 — 마감 계산(`next_at`)이 다음 날 같은 시각을 가정하기 때문이다.
    """
    path = cfg_path or CONFIG
    try:
        cron = json.loads(path.read_text(encoding="utf-8"))["digest"][key]["cron"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return default, 0, f"{path.name} 에서 {key} 크론을 읽지 못했습니다 ({type(e).__name__})"
    m = re.fullmatch(r"(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*", str(cron).strip())
    if not m:
        return default, 0, f"{key} 크론이 「{cron}」이라 매일 같은 시각으로 못 읽습니다"
    minute, hour = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default, 0, f"{key} 크론의 시각이 {hour:02d}:{minute:02d} 입니다"
    return hour, minute, None


# ─────────────────────────────────────────────────────────── 행 만들기

def _row(key, label, skill, *, count=None, unit="건", detail=None,
         deadline_at=None, note="", error=None, skipped=None, partial=None):
    """`partial` = 숫자는 있지만 **그게 전부가 아닌** 경우의 사유. 「0건」이라고 말할 수 없다."""
    return {
        "key": key, "label": label, "skill": skill,
        "count": count, "unit": unit, "detail": detail or [],
        "deadline_at": deadline_at, "note": note,
        "error": error, "skipped": skipped, "partial": partial,
    }


def _probe_problem(p):
    """조회 결과가 값이 아니면 그 사유. 값이면 None.

    **여기서 0 을 만들지 않는 것이 이 함수의 전부다.** 못 센 것과 없는 것을 같은 모양으로
    적으면 화면이 「할 일 없음」이 되고, 이 파이프라인이 조용히 틀리는 방식이 그것이다.
    """
    if not isinstance(p, dict):
        return {"error": "조회 결과를 읽지 못했습니다"}
    if p.get("error"):
        return {"error": str(p["error"])}
    if p.get("skipped"):
        return {"skipped": str(p["skipped"])}
    return None


def rows(now: datetime, *, work, edits, sync, docs) -> list:
    """상황판의 행들. **네트워크를 안 본다** — 값은 전부 인자로 받는다.

    파일은 하나 읽는다: `config.json` — 경고선(`_sync_warn_days`·`_pending_edit_days`)과
    마감 시각(`_cron_hour`)이 거기서 온다. 그 값을 여기 적으면 설정과 갈리고, 갈리면 화면과 09:00 점검이 다른 날
    경고하고 마감도 조용히 어긋난다.
    """
    out = []
    warn = _sync_warn_days()
    ingest_h, ingest_m, ingest_why = _cron_hour("ingest", INGEST_HOUR)
    pre_h, pre_m, pre_why = _cron_hour("ingestPre", PRE_DIGEST_HOUR)
    # 기본값으로 물러섰으면 **그 사실을 그 행 밑에 적는다.** 말없이 물러서면 화면의 마감이
    # 설정과 다른데 아무도 모른다 — 이 상황판이 막으려는 고장과 같은 종류다.
    why = lambda w, h: [f"· 마감 시각을 설정에서 못 읽어 기본값 {h:02d}:00 을 씁니다 ({w})"] if w else []

    # ── 새 채널 · 아침 보고 (둘 다 `.pending-work.json` 에서 온다)
    wp = _probe_problem(work)
    if wp:
        out.append(_row("new-channel", "새 채널 과거 대화", "slack-sync 3.5",
                        deadline_at=next_at(now, ingest_h, ingest_m), **wp))
        out.append(_row("work", "아침 보고", "archive-inbox",
                        deadline_at=next_at(now, ingest_h, ingest_m), **wp))
    else:
        items = work.get("items") or []
        # 겹쳐 세지 않는다 — 새 채널은 아침 보고에서 뺀 수다. 둘을 더하면 전체가 된다.
        fresh = [it for it in items if it.get("kind") == "new-channel"]
        rest = [it for it in items if it.get("kind") != "new-channel"]
        out.append(_row(
            "new-channel", "새 채널 과거 대화", "slack-sync 3.5",
            count=len(fresh), deadline_at=next_at(now, ingest_h, ingest_m),
            detail=[f"· #{it.get('channel', '?')}" for it in fresh[:5]] + why(ingest_why, INGEST_HOUR),
            # 놓치면 다시 못 채운다 — 자동 반영이 기준점만 잡고 지나가기 때문이다.
            note=f"다음 {ingest_h:02d}:{ingest_m:02d} 을 넘기면 그 채널의 과거 대화가 안 들어온다",
        ))
        out.append(_row(
            "work", "아침 보고", "archive-inbox",
            count=len(rest), deadline_at=next_at(now, ingest_h, ingest_m),
            detail=_kind_detail(rest) + why(ingest_why, INGEST_HOUR),
        ))

    # ── 슬랙에서 고쳐지거나 지워진 것
    ep = _probe_problem(edits)
    if ep:
        out.append(_row("edits", "수정·삭제", "slack-sync 10", **ep))
    else:
        eitems = edits.get("items") or []
        ages = [a for a in (_age_days(it.get("firstSeen"), now) for it in eitems) if a is not None]
        detail = []
        if ages:
            detail.append(f"· 가장 오래된 것 {max(ages)}일째")
        if edits.get("deferred"):
            detail.append(f"· 보류 {len(edits['deferred'])}건 (만기 지나면 돌아온다)")
        out.append(_row("edits", "수정·삭제", "slack-sync 10",
                        count=len(eitems), detail=detail,
                        note=f"{_pending_edit_days()}일 넘기면 09:00 점검에도 뜬다"))

    # ── 미변환 첨부 · 승인 미반영 (같은 조회에서 온다 — 한쪽만 실패할 수 없다)
    dp = _probe_problem(docs)
    if not dp and docs.get("disabled"):
        # 문서 폴더가 없으면 `pendingDocuments()` 는 `total: 0` 에 `disabled` 를 붙여 돌려준다
        # (`archive-health.js` 의 `pendingDocuments` 머리말). 0 으로 읽으면 화면이 「첨부는 다 됐다」가 된다.
        dp = {"error": "문서 폴더(documents)가 없어 세지 못했습니다"}
    if dp:
        out.append(_row("docs", "미변환 첨부", "doc-archive",
                        deadline_at=next_at(now, pre_h, pre_m), **dp))
        out.append(_row("approvals", "승인 미반영", "doc-archive", **dp))
    else:
        by = docs.get("byChannel") or []
        # **범위를 화면에 적는다.** 이 숫자는 최근 N일만 센 것이고(`archive-health.js` 의
        # scanDays — "창 밖까지 세면 매일 줄지 않는 숫자가 된다"), `doc-archive` 수집은
        # 기간 제한이 없어 더 많이 찾아온다. 2026-08-12 에 상황판 40건 · 수집 47건이었고
        # 차이는 전부 60일보다 오래된 것이었다. 범위가 안 적혀 있으면 그 자리에서
        # 어느 쪽이 맞는지 알 방법이 없다.
        detail = [f"· 최근 {docs.get('scanDays', '?')}일 범위 — 그보다 오래된 것은 여기 안 나온다"]
        if by:
            detail.append("· " + " · ".join(f"#{c['channel']} {c['count']}" for c in by[:5]))
        if docs.get("deferred"):
            detail.append(f"· 보류 {len(docs['deferred'])}건 (만기 지나면 돌아온다)")
        # **봇이 못 받는 첨부는 위 숫자에 안 들어간다.** 함께 세면 `doc-archive` 를 돌려도
        # 안 줄어들어 매일 같은 값이 오고, 곧 이 화면 전체를 안 읽게 된다. 그렇다고 조용히
        # 빼면 자료가 없는 것과 구별이 안 되므로 이름을 붙여 남긴다 (`archive-health.js`
        # 의 `restricted`). 끝내는 길은 슬랙에 다시 올리거나 `decide.py --skip` 이다.
        #
        # **`--skip` 에는 `--manifest` 가 함께 필요하다** (2026-08-13 리뷰). 이 파일들은
        # 받아진 적이 없어 `.doc-state.json` 에 없고, `decide.py` 는 상태 파일과 매니페스트
        # 에서만 대상을 찾는다 — 매니페스트 없이 치면 「못 찾았습니다」로 끝난다.
        restricted = docs.get("restricted") or []
        if restricted:
            detail.append(f"· 봇이 못 받는 첨부 {len(restricted)}건 — 이 숫자 밖이다 "
                          "(슬랙에 다시 올리거나, fetch_slack_files.py --dry-run --manifest 뒤 "
                          "decide.py --skip --manifest)")

        # **채널 몇 개가 실패해도 total 은 성공한 채널만 센 값이다** — 실패한 채널은 아무것도
        # 안 얹고 `failed` 로만 나온다 (`archive-health.js` 의 `pendingDocuments` 안 채널 순회).
        # 그래서 이 숫자는 「전부」가
        # 아니라 「적어도 이만큼」이고, 화면도 그렇게 적어야 한다. 이름을 함께 적는 이유는
        # `not_in_channel`(봇 미초대)과 속도 제한이 손볼 곳이 서로 다르기 때문이다.
        failed = docs.get("failed") or []
        partial = f"채널 {len(failed)}개를 못 읽었습니다" if failed else None
        fail_detail = ["· 읽기 실패 " + " · ".join(str(f) for f in failed[:5])
                       + (f" 외 {len(failed) - 5}개" if len(failed) > 5 else "")] if failed else []

        out.append(_row(
            "docs", "미변환 첨부", "doc-archive",
            count=int(docs.get("total") or 0),
            detail=detail + fail_detail + why(pre_why, PRE_DIGEST_HOUR),
            deadline_at=next_at(now, pre_h, pre_m), partial=partial,
            note=f"최근 {docs.get('scanDays', '?')}일 범위 · {pre_h:02d}:{pre_m:02d} 전에 push 돼야 그날 요약에 실린다",
        ))
        apps = docs.get("approvals") or []
        out.append(_row(
            "approvals", "승인 미반영", "doc-archive",
            count=len(apps), partial=partial,
            # 승인도 같은 조회에서 온다. 실패 표시를 여기 안 달면 이 줄만 아무 흔적 없이 0건이다.
            detail=(["· " + " · ".join(_approval_name(a) for a in apps[:5])] if apps else []) + fail_detail,
            note="팀이 [공개] 로 승인했는데 봇은 아직 막고 있는 자료",
        ))

    # ── 동기화 밀림 (할 일이 아니라 상태다)
    sp = _probe_problem(sync)
    if sp:
        out.append(_row("sync", "동기화 밀림", "—", unit="일", **sp))
    else:
        last = _parse_dt(sync.get("last_sync"))
        if last is None:
            out.append(_row("sync", "동기화 밀림", "—", unit="일",
                            error="상태 파일이 없거나 last_sync 를 읽지 못했습니다"))
        else:
            # **날짜를 빼지 않고 경과 시간으로 잰다.** `last_sync` 는 UTC 로 적히는데
            # (`slack-archive.js` 의 `toISOString()`) 여기 `now` 는 KST 라, 날짜끼리 빼면
            # 07:00 반영(=UTC 전날 22:00)이 아침 내내 「1일 밀림」으로 보인다. 09:00 점검은
            # 경과 시간 기준(`archive-health.js` 의 `syncLag`)이라 같은 순간에 0일이라고 말한다.
            out.append(_row("sync", "동기화 밀림", "—", unit="일",
                            count=max(0, int((now - last).total_seconds() // 86400)),
                            detail=[f"· 마지막 {last.astimezone(now.tzinfo).date().isoformat()}"],
                            note=f"{warn}일에 경고 · 14일 넘으면 실시간 조회로도 안 메워진다"))
    return out


def _kind_detail(items):
    if not items:
        return []
    counts = {}
    for it in items:
        counts[it.get("kind", "?")] = counts.get(it.get("kind", "?"), 0) + 1
    label = {
        "summary": "요약 불일치", "dropped": "근거 못 찾아 뺀 후보",
        "derive": "파생값", "renamed": "채널 개명", "note": "확인할 것",
        "error": "반영 실패", "undeclared-private": "비공개 선언 누락",
    }
    return ["· " + " · ".join(f"{label.get(k, k)} {v}" for k, v in counts.items())]


def _approval_name(a):
    if isinstance(a, dict):
        return f"#{a.get('channel', '?')} {a.get('name', '?')}"
    return str(a)


def is_actionable(r, warn=None) -> bool:
    """번호를 붙일 줄인가. 못 센 줄(`error`)은 **셌다고 볼 수 없으니 늘 붙인다.**"""
    if r["error"]:
        return True
    if r.get("partial"):
        # 일부만 센 값은 0이어도 0건이 아니다. 못 읽은 채널에 뭐가 있는지 가서 봐야 한다.
        return True
    if r["count"] is None:
        return False
    if r["key"] == "sync":
        # 밀림은 할 일이 아니라 상태다. 경고선을 넘었을 때만 손댈 것이 된다.
        return r["count"] >= (warn if warn is not None else _sync_warn_days())
    return r["count"] > 0


# ─────────────────────────────────────────────────────────── 잠금

def lock_state(path: Path = None, now: datetime = None, stale_hours: int = STALE_HOURS) -> dict:
    """지금 다른 세션이 돌고 있나. **읽기만 한다 — 자동으로 풀지 않는다.**

    자동 해제를 안 하는 이유: 오래됐다는 것과 죽었다는 것은 다르다. 문서 200건을
    변환하는 회차는 실제로 몇 시간이 걸리고, 자동으로 풀면 그 세션을 밟는다.
    `slack-sync` 와 `doc-archive` 가 겹치면 상태 파일이 통째로 덮인다.

    `path` 기본값을 `= LOCK` 으로 박지 않는 이유: 그러면 **정의 시점**에 값이 굳어 시험이
    임시 경로로 갈아끼울 수 없고, 그래서 「이미 잠겨 있으면 종료코드 2」가 한 번도 검증되지
    않았다 (2026-08-12 리뷰).
    """
    path = path or LOCK
    now = now or now_kst()
    if not path.exists():
        return {"held": False}
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # 못 읽는 잠금을 「없음」으로 읽으면 두 세션이 동시에 돈다. 있는 것으로 본다.
        return {"held": True, "stale": True, "unreadable": str(e),
                "pid": None, "since": None, "hours": None, "section": None}
    since = _parse_dt(rec.get("since"))
    hours = (now - since).total_seconds() / 3600 if since else None
    return {
        "held": True,
        "stale": hours is None or hours >= stale_hours,
        "unreadable": None,
        "pid": rec.get("pid"),
        "since": rec.get("since"),
        "hours": hours,
        "section": rec.get("section"),
    }


def take_lock(section: str, path: Path = None) -> dict:
    """잠금을 건다. **걸었는지 여부는 `acquired` 로 돌려준다.**

    부르는 쪽이 pid 를 견줘 판정하면 안 된다 — 앞 잠금의 pid 가 우연히 지금 pid 와 같으면
    (재부팅 후 pid 재사용, 같은 프로세스에서 두 번 부르는 경우) 남의 잠금을 자기 것으로
    읽고 「잠금을 걸었습니다」라고 답한다. 2026-08-12 에 시험을 붙이자 바로 드러났다.

    **읽어 보고 나서 쓰지 않는다.** `"x"` 는 「없을 때만 만든다」를 한 동작으로 하므로
    확인과 쓰기 사이가 없다. 나눠 하면 그 사이에 들어온 세션의 잠금을 덮어쓰고 둘 다
    「걸었다」고 답한다 — 잠금이 막으려던 바로 그 상황이다. 반쯤 쓰인 파일을 다른 세션이
    읽으면 JSON 오류가 나는데, 그건 `lock_state` 가 이미 「잠겨 있음」으로 본다 (덜 위험한 쪽).
    """
    path = path or LOCK
    try:
        with open(path, "x", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "since": now_kst().isoformat(timespec="seconds"),
                "section": section,
            }, f, ensure_ascii=False, indent=1)
    except FileExistsError:
        return dict(lock_state(path), acquired=False)
    return dict(lock_state(path), acquired=True)


def release_lock(path: Path = None) -> bool:
    path = path or LOCK
    if not path.exists():
        return False
    path.unlink()
    return True


# ─────────────────────────────────────────────────────────── 화면

def _w(s: str) -> int:
    """한글은 두 칸으로 센다 — 안 그러면 표가 어긋난다."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _w(s))


def _amount(r) -> str:
    if r["error"]:
        return "조회 실패"
    if r["skipped"]:
        return "안 셈"
    if r["count"] is None:
        return "안 셈"
    if r.get("partial"):
        # 「최소」를 앞에 붙인다. `40건+` 처럼 뒤에 붙이면 0 인 날 화면에 `0건` 이 그대로
        # 보이고, 훑어보는 사람은 그 한 글자를 못 본다. 넷째 상태는 눈에 먼저 들어와야 한다.
        return f"최소 {r['count']:,}{r['unit']}"
    return f"{r['count']:,}{r['unit']}"


def render(now: datetime, rs: list, prep: dict) -> str:
    warn = _sync_warn_days()
    act = [r for r in rs if is_actionable(r, warn)]
    rest = [r for r in rs if not is_actionable(r, warn)]

    # 못 센 줄이 맨 위 — 그 줄이 있는 동안은 「할 일 없음」을 말할 수 없다.
    act.sort(key=lambda r: (
        0 if r["error"] else 1,
        r["deadline_at"].timestamp() if r["deadline_at"] else 9e18,
    ))
    rest.sort(key=lambda r: r["deadline_at"].timestamp() if r["deadline_at"] else 9e18)

    L = []
    L.append(f"[아카이브 한 바퀴 · {now.date().isoformat()} ({DAYS[now.weekday()]}) "
             f"{now.hour:02d}:{now.minute:02d}]")
    L.append("")
    L.extend(_prep_lines(prep))
    L.append("")

    numbered = [r for r in act if not r["error"]]
    order = {id(r): i + 1 for i, r in enumerate(numbered)}

    wl = max([14] + [_w(r["label"]) for r in rs])
    wa = max([8] + [_w(_amount(r)) for r in rs])
    wd = max([10] + [_w(deadline_text(now, r["deadline_at"])) for r in rs])
    L.append(f"{'#':<3}{_pad('할 일', wl)}  {_pad('남은 것', wa)}  {_pad('마감', wd)}  넘길 곳")

    for r in act + rest:
        mark = "!" if r["error"] else (str(order[id(r)]) if id(r) in order else "-")
        L.append(f"{mark:<3}{_pad(r['label'], wl)}  {_pad(_amount(r), wa)}  "
                 f"{_pad(deadline_text(now, r['deadline_at']), wd)}  {r['skill']}")
        if r["error"]:
            L.append(f"   · {r['error']}")
        elif r["skipped"]:
            L.append(f"   · {r['skipped']} — 이 줄은 세지 않았습니다")
        else:
            for d in r["detail"]:
                L.append(f"   {d}")

    L.append("")
    # 「셌다 / 안 셌다 / 못 셌다」를 여기서도 지킨다. 안 센 줄이 하나라도 있으면
    # 「할 일 없음」이라고 닫지 않는다 — 그렇게 닫으면 그날의 첨부가 통째로 묻힌다.
    unseen = [r for r in rs if r["error"] or r["skipped"] or r["partial"]]
    if any(r["error"] for r in rs):
        L.append("못 센 줄이 있습니다 (`!`). 그 줄이 0건이라는 뜻이 아닙니다 — 사유를 먼저 보세요.")
    if any(r["partial"] for r in rs):
        L.append("일부만 센 줄이 있습니다 (「최소」). 못 읽은 채널의 것은 이 숫자에 안 들어 있습니다.")
    if numbered:
        L.append(f"다음은 {order[id(numbered[0])]}번입니다.")
    elif unseen:
        L.append("센 줄에는 할 일이 없습니다. 안 센 줄(" +
                 " · ".join(r["label"] for r in unseen) + ")은 아직 모릅니다.")
    else:
        L.append("지금 할 일이 없습니다.")
    return "\n".join(L)


def _prep_lines(prep: dict) -> list:
    """준비 줄. **여기서도 「모른다」를 말할 수 있어야 한다** — 표만 셋으로 나누고 이 줄이
    「깨끗함 · origin 과 같음」으로 단정하면, 못 본 것을 봤다고 하는 고장이 그대로 남는다."""
    lock = prep.get("lock") or {"held": False}
    clean = prep.get("clean")
    untracked = prep.get("untracked") or []
    bits = []
    if clean is None:
        bits.append("작업 트리 확인 못 함")
    elif clean:
        bits.append("git 깨끗함")
    else:
        n = len(prep.get("dirty") or [])
        bits.append(f"작업 트리에 변경 {n}개 — 정리 먼저")
    if untracked:
        bits.append(f"추적 안 되는 파일 {len(untracked)}개")
    compare = prep.get("compare", "ok")
    behind, ahead = prep.get("behind", 0), prep.get("ahead", 0)
    if compare == "skipped":
        bits.append("origin 대조 안 함 (--fast)")
    elif compare == "failed":
        bits.append("origin 대조 못 함")
    elif behind:
        bits.append(f"origin 이 {behind}개 앞섬 — git pull 먼저")
    elif ahead:
        bits.append(f"안 나간 커밋 {ahead}개")
    else:
        bits.append("origin 과 같음")
    # **켜져 있을 때는 아무 말도 안 한다.** 정상인 것을 매번 적으면 준비 줄이 길어지고,
    # 길어지면 안 읽힌다. 꺼졌거나 모를 때만 나온다.
    hooks = prep.get("hooks") or {}
    if hooks.get("on") is None:
        bits.append("커밋·push 관문 확인 못 함")
    elif not hooks.get("on"):
        bits.append("커밋·push 관문 꺼져 있음")
    if not lock.get("held"):
        bits.append("잠금 없음")
    elif lock.get("stale"):
        h = lock.get("hours")
        bits.append(f"잠금 있음 — 오래됨({h:.0f}시간)" if h is not None else "잠금 있음 — 읽지 못함")
    else:
        bits.append(f"잠금 있음 ({lock.get('section') or '?'})")

    out = [f"준비   {' · '.join(bits)}"]
    if prep.get("status_error"):
        out.append(f"       · {prep['status_error']}")
        out.append("       작업 트리를 못 봤습니다. 「깨끗하다」는 뜻이 아닙니다 — 확인 전에는 시작하지 마세요.")
    if prep.get("compare_error"):
        out.append(f"       · {prep['compare_error']}")
        out.append("       origin 과 대조하지 못했습니다. 화면의 「안 나간 커밋」 수를 믿지 마세요.")
    if prep.get("compare_note"):
        out.append(f"       · {prep['compare_note']}")
    # 관문이 꺼진 것을 알려주는 신호는 「커밋이 그냥 통과하는 것」 하나뿐이라, 정상 동작과
    # 구별되지 않는다. 그동안 스킬 문서 넷은 「점검을 건너뛰면 커밋이 막힌다」고 적고 있었다.
    if hooks.get("on") is False:
        out.append(f"       · pre-commit·pre-push 가 안 돕니다 — 7.5 점검·요약 승인을 안 거친 커밋과,"
                   f" 승인 목록에 없는 커밋이 섞인 push 가 그냥 통과합니다"
                   + (f" (지금 값: {hooks['value']})" if hooks.get("value") else ""))
        # **상대경로를 적어 주면 안 된다.** 이 저장소(자료)에는 `.githooks/` 가 없어서
        # `git config core.hooksPath .githooks` 는 아무 훅도 안 켜고, 이미 제대로 걸려
        # 있던 것을 덮어 꺼 버린다. 걸 값은 코드 저장소의 절대경로다.
        out.append(f"       켜기: git config core.hooksPath \"{HOOKS_TARGET.as_posix()}\"")
    elif hooks.get("on") is None and hooks.get("error"):
        out.append(f"       · 커밋·push 관문 상태를 못 봤습니다 — {hooks['error']}")
    if clean is False:
        for p in (prep.get("dirty") or [])[:5]:
            out.append(f"       · {p}")
        out.append("       다른 창의 작업일 수 있습니다. 커밋 대상을 확인하기 전에는 시작하지 마세요.")
    # `doc-archive` 가 만드는 새 문서 md 는 전부 추적 안 되는 파일이라, 이걸 안 보면
    # 다른 창에서 돌고 있는 문서 회차가 준비 줄에 아무 흔적도 안 남긴다.
    hot = [p for p in untracked
           if p.startswith(("documents/", "slack-export/"))]
    if hot:
        out.append(f"       · 아카이브 폴더에 추적 안 되는 파일 {len(hot)}개 — {hot[0]}")
        out.append("       다른 창이 문서 변환 중일 수 있습니다.")
    if lock.get("held"):
        out.append(f"       다른 세션이 돌고 있습니다 (pid {lock.get('pid')} · {lock.get('since')})")
        if lock.get("stale"):
            out.append("       정말 끝난 것이 맞으면: python .claude/skills/archive-run/scripts/board.py --unlock")
    return out


# ─────────────────────────────────────────────────────────── 조회

def _run(cmd, cwd=None, timeout=180):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def probe_work(path: Path = None) -> dict:
    """아침 보고. **파일이 없으면 0건이다 — 실패가 아니다.**

    쓰는 쪽이 남은 것이 없으면 파일을 지운다 (`pending-work.js` 의 `runPendingWork`,
    "빈 목록을 남겨 두면 볼 것이 있다로 읽힌다"). 이걸 실패로 읽으면 할 일 없는 날마다
    「조회 실패」가 떠서 곧 아무도 안 읽게 되고, 그때 진짜 실패도 같이 묻힌다.
    """
    path = path or PENDING_WORK
    if not path.exists():
        return {"items": []}
    try:
        p = _run([sys.executable, str(REVIEW_WORK), "--json"])
        if p.returncode != 0:
            return {"error": f"review_work.py 실패: {(p.stderr or '').strip()[:200]}"}
        return {"items": json.loads(p.stdout or "[]")}
    except Exception as e:
        return {"error": f"review_work.py 를 부르지 못했습니다: {e}"}


def _active_deferred(state_path: Path = None) -> dict:
    """지금 살아 있는 「나중에」. `src/ingest/util.js` 의 `activeDeferred` 와 같은 규칙.

    `.sync-state.json` 의 `deferred` 는 `{id: {until, ...}}` 객체 맵이고, `until` 이
    `YYYY-MM-DD` 이면서 오늘(KST) 이후인 것만 살아 있다. 만기가 지나면 저절로 돌아온다.
    """
    try:
        state = json.loads((state_path or SYNC_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    today = now_kst().date().isoformat()
    return {k: v for k, v in (state.get("deferred") or {}).items()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str((v or {}).get("until") or ""))
            and str(v["until"]) >= today}


def probe_edits(path: Path = None, state_path: Path = None) -> dict:
    """슬랙 수정·삭제. 없으면 0건 (`slack-archive.js` 의 `ingestConversations` 안 `PENDING_FILE` 쓰기, 위와 같은 규칙).

    **있는데 못 읽는 것은 다르다** — 그건 진짜 실패다.

    **미뤄 둔 건은 뺀다.** `.pending-edits.json` 은 미룬 것을 `items` 에 **그대로 남긴다**
    (`slack-archive.js` 의 `ingestConversations` 안 「나중에」 제외 — "파일에는 그대로 남긴다, 빼는 것은 보고뿐"). 그래서 파일을
    그냥 세면 07:00 보고·09:00 점검이 조용한 건이 상황판에만 되살아난다. 만기까지 조용한
    것이 「나중에」의 약속이다.
    """
    path = path or PENDING_EDITS
    if not path.exists():
        return {"items": [], "deferred": []}
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("items") or []
    except (OSError, ValueError) as e:
        return {"error": f"{path.name} 을 읽지 못했습니다: {e}"}
    held = _active_deferred(state_path)
    return {"items": [it for it in items if it.get("id") not in held],
            "deferred": [it for it in items if it.get("id") in held]}


def probe_sync() -> dict:
    try:
        return {"last_sync": json.loads(SYNC_STATE.read_text(encoding="utf-8")).get("last_sync")}
    except (OSError, ValueError) as e:
        return {"error": f"{SYNC_STATE.name} 을 읽지 못했습니다: {e}"}


def probe_docs(fast=False) -> dict:
    """미변환 첨부·승인 미반영. **슬랙을 훑어서 20초쯤 걸린다.**"""
    if fast:
        return {"skipped": "--fast"}
    if not BOARD_DOCS.exists():
        return {"error": f"{BOARD_DOCS} 이 없습니다"}
    try:
        p = _run(["node", str(BOARD_DOCS)], cwd=str(HERMES), timeout=180)
        if p.returncode != 0:
            return {"error": f"board-docs.js 실패: {(p.stderr or '').strip()[:200]}"}
        return json.loads(p.stdout)
    except subprocess.TimeoutExpired:
        return {"error": "슬랙 조회가 180초 안에 안 끝났습니다"}
    except Exception as e:
        return {"error": f"board-docs.js 를 부르지 못했습니다: {e}"}


def probe_hooks() -> dict:
    """커밋·push 관문이 **켜져 있나**. `git config core.hooksPath` 한 줄이다.

    스위치는 하나다 — 이 설정을 걸면 `.githooks/` 의 훅이 **전부** 돈다. 2026-08-29 에
    `pre-push`(승인받은 목록 그대로만 origin/main 으로 내보낸다)가 붙었고, 그것도 이
    한 줄에 함께 걸린다.

    훅은 2026-08-12 부터 코드 저장소 안(`.githooks/`)에 있지만, git 은 훅을
    복제하지 않으므로 **설정을 걸어야만 돈다.** **거는 것은 사람이다** — 기계마다
    절대경로라 저장소에 안 실리고, 새 클론에서는 다시 건다. 안 걸면 관문이 통째로 꺼진다.
    걸 값은 아래 `켜기:` 줄이 낸다(상대경로가 아니라 코드 저장소의 절대경로다).

    **꺼진 것을 알려주는 신호가 「커밋이 그냥 통과하는 것」 하나뿐이었다** — 그건
    정상 동작과 구별되지 않는다. 그동안 스킬 문서 넷은 「점검을 건너뛰면 커밋이
    막힌다」고 적고 있었다. 여기서 그 문장이 지금 이 기계에서 참인지 본다.

    **고치지는 않는다.** git 설정을 바꾸는 것은 사람의 자리다 — 알리기만 한다.

    **판정은 git 에게 맡긴다** (2026-08-13 리뷰). 전에는 `git config --get` 값을 `".githooks"`
    라는 **글자와 그대로 대봤다.** `./.githooks`·절대경로·역슬래시로 걸어 두면 훅은 실제로
    도는데 화면은 「꺼져 있음」이라 말했다 — 안전한 방향의 오탐이지만, 사람이 이미 걸어 둔
    것을 또 걸게 만든다. `git rev-parse --git-path hooks` 는 설정을 반영한 **실제 경로**를
    주므로(걸려 있으면 `.githooks`, 아니면 `.git/hooks`) 표기 차이를 git 이 흡수한다.
    """
    p = _run(["git", "rev-parse", "--git-path", "hooks"], cwd=str(ROOT), timeout=15)
    if p.returncode != 0:
        return {"on": None, "error": (p.stderr or "").strip()[:120] or f"종료코드 {p.returncode}"}
    where = (p.stdout or "").strip()
    if not where:
        return {"on": None, "error": "git 이 훅 경로를 알려주지 않았습니다"}
    # git 은 저장소 뿌리 기준 상대경로로 주는 것이 보통이고, 절대경로일 때도 있다.
    # `Path / 절대경로` 는 절대경로 쪽을 그대로 쓰므로 둘 다 이 한 줄로 풀린다.
    # **대보는 곳은 코드 저장소다** (2026-08-31 이사). 커밋이 일어나는 곳은 자료
    # 저장소인데 훅 스크립트는 코드 저장소에 있어서, 자료 쪽에는 코드 저장소의
    # **절대경로**를 걸어 둔다. 여기를 `ROOT`(자료) 기준으로 두면 제대로 걸어 둔
    # 상태를 「꺼져 있음」이라 말하고, 안내대로 따르면 **돌던 관문이 실제로 꺼진다.**
    actual = (Path(ROOT) / where).resolve()
    on = actual == HOOKS_TARGET

    # 화면에 보여줄 값은 사람이 설정한 그대로가 낫다 — 「무엇을 고쳐야 하나」가 그 문자열이다.
    # 못 읽어도 판정은 위에서 이미 났으므로 여기서 실패로 뒤집지 않는다.
    # (판정 기준은 위 `HOOKS_TARGET` — 코드 저장소의 .githooks 다.)
    cfg = _run(["git", "config", "--get", "core.hooksPath"], cwd=str(ROOT), timeout=15)
    value = (cfg.stdout or "").strip() if cfg.returncode in (0, 1) else ""
    return {"on": on, "value": value, "path": str(actual)}


def probe_git(fetch=True) -> dict:
    """git 상태. **`core.quotepath=false` 를 빠뜨리면 안 된다.**

    없으면 git 이 한글 경로를 `"\\354\\262\\255..."` 로 감싸 내보내, 준비 줄이 지목하는
    파일 이름을 사람이 읽을 수 없다. 그러면 「내 것인지 남의 것인지 확인하라」는 그 줄이
    제 일을 못 한다 — 이 저장소는 파일명이 거의 다 한글이다
    (`doc-archive` SKILL.md 11단계가 같은 함정을 적어 두었다. 2026-08-12 실측).

    **종료코드를 본다.** 안 보면 `git status` 가 실패했을 때 출력이 비어 「변경 없음」이 되고
    화면은 「git 깨끗함」이라고 단정한다. 표에서는 셋으로 나누면서 준비 줄에서 안 나누면
    같은 고장이 여기로 옮겨 올 뿐이다 (2026-08-12 리뷰). `clean=None` 은 **모른다**는 뜻이다.
    """
    err = lambda p, what: ((p.stderr or "").strip()[:160] or f"{what} 종료코드 {p.returncode}")
    try:
        compare, compare_error = "ok", None
        if fetch:
            f = _run(["git", "fetch", "--quiet"], cwd=str(ROOT), timeout=60)
            if f.returncode != 0:
                # 여기서 멈추지 않고 계속 세면 **낡은 origin/main** 을 기준으로 세게 된다.
                compare, compare_error = "failed", err(f, "git fetch")
        else:
            compare = "skipped"

        st = _run(["git", "-c", "core.quotepath=false", "status", "--porcelain"], cwd=str(ROOT))
        if st.returncode != 0:
            return {"clean": None, "dirty": [], "untracked": [], "ahead": 0, "behind": 0,
                    "compare": compare, "compare_error": compare_error,
                    "status_error": err(st, "git status")}
        lines = [ln for ln in (st.stdout or "").splitlines() if ln]
        # 추적 안 되는 것은 `dirty` 에서 뺀다 — 받은편지함·프로젝트 폴더에 상시로 있어
        # 전부 세면 준비 줄이 영영 안 깨끗해진다. 대신 개수를 따로 적는다 (아래 `_prep_lines`).
        dirty = [ln[3:] for ln in lines if not ln.startswith("??")]
        untracked = [ln[3:] for ln in lines if ln.startswith("??")]

        # **`origin/main` 을 박아 쓰지 않는다** — 다른 브랜치에서 돌리면 엉뚱한 수를
        # 「안 나간 커밋」으로 보여준다. 추적 브랜치가 없으면 `origin/main` 으로 물러서되
        # 그 사실을 화면에 적는다 (말없이 물러서는 것이 이 화면에서 가장 위험한 종류다).
        up = _run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=str(ROOT))
        base, base_note = (up.stdout or "").strip(), None
        if up.returncode != 0 or not base:
            base, base_note = "origin/main", "추적 브랜치가 없어 origin/main 과 견줌"

        ahead = _run(["git", "rev-list", "--count", f"{base}..HEAD"], cwd=str(ROOT))
        behind = _run(["git", "rev-list", "--count", f"HEAD..{base}"], cwd=str(ROOT))
        if ahead.returncode != 0 or behind.returncode != 0:
            bad = ahead if ahead.returncode != 0 else behind
            compare, compare_error = "failed", err(bad, "git rev-list")
        return {
            "clean": not dirty, "dirty": dirty, "untracked": untracked,
            "compare": compare, "compare_error": compare_error, "compare_note": base_note,
            "ahead": int((ahead.stdout or "0").strip() or 0),
            "behind": int((behind.stdout or "0").strip() or 0),
            "hooks": probe_hooks(),
        }
    except Exception as e:
        # git 을 아예 못 불렀으면 관문 상태도 「모른다」다 — 여기서 `on: False` 로 적으면
        # 「꺼져 있다」고 단정하는 것이 된다.
        return {"clean": None, "dirty": [], "untracked": [], "ahead": 0, "behind": 0,
                "compare": "failed", "compare_error": str(e),
                "hooks": {"on": None, "error": str(e)[:120]},
                "status_error": f"git 을 부르지 못했습니다: {e}"}


# ─────────────────────────────────────────────────────────── CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="아카이브 한 바퀴 — 남은 일을 한 표로 보인다")
    ap.add_argument("--fast", action="store_true", help="슬랙을 안 훑는다 (그 두 줄은 「안 셈」)")
    ap.add_argument("--json", action="store_true", help="기계용 출력")
    ap.add_argument("--lock", metavar="구간", help="잠금 걸기 (구간 이름)")
    ap.add_argument("--unlock", action="store_true", help="잠금 풀기")
    args = ap.parse_args(argv)

    # 둘 다 주면 전에는 `--unlock` 만 돌고 `--lock` 을 말없이 버렸다 — 잠금이 안 걸렸는데
    # 오류도 안 나서, 걸린 줄 알고 구간을 시작하게 된다.
    if args.unlock and args.lock:
        print("--lock 과 --unlock 을 함께 줄 수 없습니다. 하나만 주세요.", file=sys.stderr)
        return 2

    # **잠금은 질의가 아니라 명령이다.** `--json` 은 상황판을 기계가 읽으려고 붙이는 것인데,
    # 함께 주면 전에는 「잠금을 걸었습니다 — … (pid …)」라는 **한국어 산문**을 내고 0 으로
    # 끝났다 — 부르는 쪽은 JSON 을 기다리다 파싱에 실패한다. 잠금 결과용 JSON 계약을 새로
    # 만들지 않고 거절한다 (2026-08-13 리뷰). 위 `--lock`+`--unlock` 거절과 같은 모양이다.
    if args.json and (args.lock or args.unlock):
        print("--json 은 상황판을 볼 때만 씁니다. 잠금은 --lock/--unlock 만 주세요.",
              file=sys.stderr)
        return 2

    if args.unlock:
        print("잠금을 풀었습니다." if release_lock() else "걸린 잠금이 없습니다.")
        return 0

    now = now_kst()
    if args.lock:
        st = take_lock(args.lock)
        if not st.get("acquired"):
            print(f"이미 잠겨 있습니다 (pid {st.get('pid')} · {st.get('since')} · {st.get('section')})",
                  file=sys.stderr)
            return 2
        print(f"잠금을 걸었습니다 — {args.lock} (pid {os.getpid()})")
        return 0

    prep = probe_git(fetch=not args.fast)
    prep["lock"] = lock_state(LOCK, now)
    rs = rows(now, work=probe_work(), edits=probe_edits(),
              sync=probe_sync(), docs=probe_docs(fast=args.fast))

    if args.json:
        print(json.dumps({
            "now": now.isoformat(timespec="seconds"),
            "prep": {k: v for k, v in prep.items() if k != "lock"},
            "lock": prep["lock"],
            "rows": [{**r, "deadline_at": r["deadline_at"].isoformat() if r["deadline_at"] else None}
                     for r in rs],
        }, ensure_ascii=False, indent=1))
        return 0

    print()
    print(render(now, rs, prep))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
