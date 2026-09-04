"""헬스 체크 — 봇이 "돌고는 있는데 제 일을 못 하는" 상태를 드러낸다.

프로세스가 살아 있는지는 이미 `/api/health` 가 답한다. 여기서 보는 것은 그 다음이다.
**조용한 고장**이 이 시스템에서 가장 위험한 실패 방식이기 때문이다.

- 봇이 붙어 있지만 채널에 초대되지 않아 수집이 비어 간다
- 수집은 되는데 스키마가 깨져 검색이 그 문서를 건너뛴다
- 답은 나오는데 근거를 못 찾아 "없다" 만 반복한다
- 명령을 코드에 넣었는데 매니페스트에 없어 Slack 이 그 명령을 모른다
- 사용자가 👎 를 계속 누르는데 아무도 보지 않는다

셋 다 오류 로그를 남기지 않는다. 그래서 숫자로 드러내는 것 말고는 방법이 없다.

## 판정 값
`ok` 정상 · `warn` 살펴볼 것 · `bad` 지금 조치 · `unknown` 판단할 자료가 없음

**자료가 없으면 `ok` 가 아니라 `unknown` 이다.** 아무도 질문하지 않은 날을
"품질 좋음" 으로 칠하면 그 지표는 아무 말도 하지 않는 것이 된다.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path

from . import reader

logger = logging.getLogger("tybot.console.health")

# 이 값들은 파일럿 규모(하루 수십 건) 기준이다. 운영 규모가 커지면 다시 재야 한다.
MIN_SAMPLE = 10          # 이보다 적으면 비율을 신뢰하지 않는다
GROUNDED_WARN = 0.70     # 근거를 찾은 답변 비율
GROUNDED_BAD = 0.50
ERROR_WARN = 0.02        # 오류로 끝난 질문 비율
ERROR_BAD = 0.10
SATISFACTION_WARN = 0.70
SATISFACTION_BAD = 0.50
SLOW_MS = 15_000         # 이보다 느린 답변이 잦으면 사람이 기다리지 못한다

WORST = {"unknown": 0, "ok": 1, "warn": 2, "bad": 3}


def _worst(*levels: str) -> str:
    """가장 나쁜 판정을 고른다. `unknown` 은 나쁨으로 치지 않는다."""
    real = [x for x in levels if x != "unknown"]
    if not real:
        return "unknown"
    return max(real, key=lambda x: WORST[x])


def _ratio_level(value: float, warn: float, bad: float) -> str:
    """값이 클수록 좋은 지표."""
    if value < bad:
        return "bad"
    if value < warn:
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# 1. 봇 연결
# ---------------------------------------------------------------------------


def bot_section(rows: list[dict]) -> dict:
    """`reader.workspace_status()` 결과를 그대로 받는다."""
    items = []
    for row in rows:
        problems = []
        level = "ok"
        connected = row.get("connected")
        if connected is None:
            # 상태 파일이 낡았다. 봇이 죽었는지 파일만 안 쓰는지 알 수 없다.
            level = "warn"
            problems.append("상태 파일이 오래됐습니다(봇이 멈췄을 수 있습니다)")
        elif connected is False:
            level = "bad"
            problems.append("Slack 에 연결되어 있지 않습니다")

        if row.get("writeProblem"):
            level = "bad"
            problems.append(f"저장 실패: {row['writeProblem']}")

        uninvited = int(row.get("uninvitedChannels") or 0)
        if uninvited:
            # 오류가 아니다. 그래서 아무도 모르는 채로 수집이 비어 간다.
            level = _worst(level, "warn")
            problems.append(f"초대되지 않은 채널 {uninvited}개 — 그 채널은 수집되지 않습니다")

        items.append({
            "workspace": row.get("key"),
            "label": row.get("label") or row.get("key"),
            "level": level,
            "connected": connected,
            "problems": problems,
        })

    return {
        "level": _worst(*[i["level"] for i in items]) if items else "unknown",
        "workspaces": items,
    }


# ---------------------------------------------------------------------------
# 2. 아카이브
# ---------------------------------------------------------------------------


def archive_section(rows: list[dict], docs: list[dict]) -> dict:
    """수집이 밀리거나 문서가 깨졌는지 본다."""
    broken = sum(int(r.get("broken") or 0) for r in rows)
    stale = [r for r in rows if r.get("health") == "stale"]
    total_docs = len(docs)

    level = "ok"
    problems = []
    if broken:
        # 깨진 문서는 검색에서 조용히 빠진다. 답변이 "없다" 로 바뀌는 원인이다.
        level = "bad"
        problems.append(f"스키마가 깨진 문서 {broken}건 — 검색에서 제외됩니다")
    if stale:
        level = _worst(level, "warn")
        names = ", ".join(str(r.get("label") or r.get("key")) for r in stale[:3])
        problems.append(f"수집이 밀린 워크스페이스 {len(stale)}개: {names}")
    if not total_docs:
        level = "unknown"
        problems.append("수집된 문서가 없습니다")

    return {
        "level": level,
        "documents": total_docs,
        "brokenDocuments": broken,
        "staleWorkspaces": len(stale),
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# 3. 답변 품질
# ---------------------------------------------------------------------------

def answer_section(records: list[dict]) -> dict:
    """감사기록(qa-log)으로 답변이 실제로 쓸모 있었는지 본다."""
    total = len(records)
    if total < MIN_SAMPLE:
        return {
            "level": "unknown",
            "questions": total,
            "note": f"표본이 적어({total}건) 비율을 판단하지 않습니다",
            "problems": [],
        }

    errors = sum(1 for r in records if str(r.get("error") or "").strip())
    grounded = sum(1 for r in records if int(r.get("hits") or 0) > 0)
    no_hits = total - grounded
    slow = sum(1 for r in records if int(r.get("elapsed_ms") or 0) > SLOW_MS)

    grounded_rate = grounded / total
    error_rate = errors / total

    level = _ratio_level(grounded_rate, GROUNDED_WARN, GROUNDED_BAD)
    problems = []
    if grounded_rate < GROUNDED_WARN:
        problems.append(
            f"근거를 찾은 답변이 {grounded_rate:.0%} 입니다 — "
            "아카이브에 자료가 없거나 검색이 못 찾고 있습니다"
        )
    # 어떤 예외였는지 함께 낸다. 비율만 있으면 고칠 수 없다 —
    # "22% 가 실패" 는 볼 때마다 같은 조사를 처음부터 다시 하게 만든다.
    # qa-log 의 `error` 는 예외 클래스명뿐이라(업무 내용이 없다) 그대로 보여도 된다.
    error_kinds = Counter(
        str(r.get("error") or "").strip()
        for r in records
        if str(r.get("error") or "").strip()
    )
    top_error = error_kinds.most_common(1)
    detail = f" — 가장 많은 것은 {top_error[0][0]} {top_error[0][1]}건" if top_error else ""
    if error_rate >= ERROR_BAD:
        level = "bad"
        problems.append(f"오류로 끝난 질문이 {error_rate:.0%} 입니다{detail}")
    elif error_rate >= ERROR_WARN:
        level = _worst(level, "warn")
        problems.append(f"오류로 끝난 질문이 {errors}건 있습니다{detail}")
    if slow:
        level = _worst(level, "warn")
        problems.append(f"{SLOW_MS // 1000}초를 넘긴 답변 {slow}건")

    reasons = Counter(str(r.get("reason") or "-") for r in records)
    return {
        "level": level,
        "questions": total,
        "grounded": grounded,
        "noHits": no_hits,
        "groundedRate": round(grounded_rate, 3),
        "errors": errors,
        "errorRate": round(error_rate, 3),
        "slowAnswers": slow,
        "topReasons": [{"reason": k, "count": v} for k, v in reasons.most_common(5)],
        "topErrors": [{"kind": k, "count": v} for k, v in error_kinds.most_common(5)],
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# 4. 슬래시 명령 — 매니페스트와 코드가 어긋났는가
# ---------------------------------------------------------------------------

_COMMAND_IN_CODE = re.compile(r'@\w+\.app\.command\(\s*["\'](/[^"\']+)["\']')
_COMMAND_IN_MANIFEST = re.compile(r'^\s*-?\s*command:\s*["\']?(/[^\s"\']+)', re.MULTILINE)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("명령 점검용 파일을 읽지 못했습니다 (%s): %s", path, e)
        return ""


def command_section(manifest_text: str, code_text: str) -> dict:
    """코드에 등록된 명령과 매니페스트에 선언된 명령을 맞춰 본다.

    **이 어긋남은 오류를 내지 않는다.** 매니페스트에만 없으면 Slack 이 그 명령을 모르고
    사용자에게 "명령을 찾을 수 없다" 가 뜬다. 코드에만 없으면 Slack 은 명령을 보내는데
    봇이 아무 응답도 하지 않는다. 둘 다 서버 로그에는 아무것도 남지 않는다.
    """
    in_code = set(_COMMAND_IN_CODE.findall(code_text))
    in_manifest = set(_COMMAND_IN_MANIFEST.findall(manifest_text))

    if not in_code and not in_manifest:
        return {
            "level": "unknown",
            "note": "명령 목록을 읽지 못했습니다",
            "commands": [], "problems": [],
        }

    missing_in_manifest = sorted(in_code - in_manifest)
    missing_in_code = sorted(in_manifest - in_code)

    problems = []
    level = "ok"
    if missing_in_manifest:
        level = "bad"
        problems.append(
            "매니페스트에 없는 명령: " + ", ".join(missing_in_manifest)
            + " — Slack 이 이 명령을 모릅니다. 매니페스트를 갱신하고 앱을 재설치하세요"
        )
    if missing_in_code:
        level = _worst(level, "warn")
        problems.append(
            "코드에 없는 명령: " + ", ".join(missing_in_code)
            + " — Slack 이 보내도 봇이 응답하지 않습니다"
        )

    commands = sorted(in_code | in_manifest)
    return {
        "level": level,
        "commands": [
            {
                "name": c,
                "inCode": c in in_code,
                "inManifest": c in in_manifest,
            }
            for c in commands
        ],
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# 5. 피드백 만족도
# ---------------------------------------------------------------------------


def _read_feedback(days: int, allowed: frozenset[str] | set[str] | None) -> list[dict]:
    now = reader._now()
    months = {(now - timedelta(days=i)).strftime("%Y-%m") for i in range(days + 1)}
    cutoff = (now - timedelta(days=days)).isoformat()
    rows: list[dict] = []
    for month in sorted(months):
        path = reader.qa_log_dir() / f"feedback-{month}.jsonl"
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(rec.get("at", "")) < cutoff:
                        continue
                    if allowed is not None and str(rec.get("workspace", "")) not in allowed:
                        continue
                    rows.append(rec)
        except OSError as e:
            logger.warning("피드백 기록을 읽지 못했습니다 (%s): %s", path, e)
    return rows


def _display_names(records: list[dict]) -> dict[str, str]:
    """Slack 사용자 ID → 표시 이름. 감사기록에 이미 들어 있는 값을 쓴다."""
    names: dict[str, str] = {}
    for rec in records:
        uid = str(rec.get("user") or "")
        name = str(rec.get("user_name") or "").strip()
        if uid and name:
            names.setdefault(uid, name)
    return names


def _departments(pairs: set[tuple[str, str]]) -> dict[str, str]:
    """(워크스페이스, Slack 사용자) → 부서명.

    이름만으로는 누가 어느 팀 사람인지 몰라, 정정이 어느 조직 자료에 관한
    것인지 판단할 수 없었다. user_identity 로 사번을 찾고 조직을 붙인다.

    매핑이 없으면 빈 값이다 — 없는 것을 추측해 채우지 않는다.
    """
    if not pairs:
        return {}
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return {}
    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(
                """
                select i.workspace, i.slack_user, o.name
                  from user_identity i
                  join employee e on e.emp_no = i.emp_no
                  left join org_unit o on o.code = e.org_code
                 where (i.workspace, i.slack_user) = any(%(pairs)s)
                """,
                {"pairs": list(pairs)},
            )
            return {
                f"{row[0]}:{row[1]}": str(row[2] or "")
                for row in cur.fetchall()
            }
    except Exception as exc:  # noqa: BLE001 - 부서를 못 붙이는 것으로 점검이 멈추면 안 된다
        logger.debug("부서 정보를 붙이지 못했습니다: %s", exc)
        return {}


def contributors(
    events: list[dict],
    names: dict[str, str] | None = None,
    depts: dict[str, str] | None = None,
    *,
    include_text: bool = False,
) -> list[dict]:
    """정정 사항을 많이 보낸 사람 순으로.

    **글을 쓴 것만 기여로 센다.** 👍 는 한 번 누르면 끝이라 개수를 세면 순위가
    "많이 누른 사람" 이 되고, 실제로 아카이브를 고칠 거리를 준 사람이 묻힌다.
    그래서 정정 사항이 담긴 신고(`negative`·`missing`·`correction`)를 기준으로 센다.

    부서·워크스페이스를 함께 낸다. 이름만으로는 정정이 어느 조직 자료에 관한
    것인지 알 수 없어, 순위만 있고 판단할 거리가 없었다.

    마지막 정정 내용도 담는다(`include_text` 일 때만). 관리자만 보는 화면이다 —
    신고 본문에는 업무 내용이 들어 있으므로 개발자 권한에는 건수만 나간다.
    """
    names = names or {}
    depts = depts or {}
    tally: dict[str, Counter] = {}
    ws_by_actor: dict[str, set[str]] = {}
    last_text: dict[str, tuple[str, str]] = {}
    for e in events:
        if e.get("action") in {"removed", "resolved"}:
            continue
        actor = str(e.get("actor") or "")
        if not actor:
            continue
        kind = str(e.get("kind") or "")
        text = str(e.get("text") or "").strip()
        counter = tally.setdefault(actor, Counter())
        counter[kind] += 1
        ws_by_actor.setdefault(actor, set()).add(str(e.get("workspace") or ""))
        if text:
            counter["with_text"] += 1
            at = str(e.get("at") or "")
            if at >= last_text.get(actor, ("", ""))[0]:
                last_text[actor] = (at, text)

    rows = []
    for actor, c in tally.items():
        # 정정 사항이 실제로 담긴 건수. 이게 순위의 기준이다.
        useful = c["with_text"]
        workspaces = sorted(w for w in ws_by_actor.get(actor, set()) if w)
        dept = ""
        for ws in workspaces:
            dept = depts.get(f"{ws}:{actor}") or dept
        rows.append({
            "actor": actor,
            "name": names.get(actor) or actor,
            "dept": dept,
            "workspaces": workspaces,
            "corrections": useful,
            "reports": c["negative"] + c["missing"] + c["correction"],
            "praise": c["positive"],
            "total": sum(v for k, v in c.items() if k != "with_text"),
            "lastCorrection": last_text.get(actor, ("", ""))[1] if include_text else "",
        })
    rows.sort(key=lambda r: (-r["corrections"], -r["reports"], r["name"]))
    return rows[:20]


def feedback_items(
    events: list[dict],
    names: dict[str, str] | None = None,
    depts: dict[str, str] | None = None,
    *,
    include_text: bool = False,
    limit: int = 50,
) -> list[dict]:
    """신고 하나하나. 무엇이 들어왔고 처리됐는지 볼 수 있어야 한다.

    건수만 보이던 것이 문제였다. "정정 제보 1건" 을 보고도 내용을 알 수 없어
    아무 조치도 할 수 없었고, 봇에 반영됐는지도 알 수 없었다.

    처리 표시는 같은 로그의 `resolved` 줄이다 — 신고를 지우거나 고치지 않는다.
    """
    from ..feedback import event_id

    names = names or {}
    depts = depts or {}
    resolved: dict[str, dict] = {}
    for e in events:
        if e.get("action") != "resolved":
            continue
        target = str(e.get("target") or "")
        if target:
            resolved[target] = e

    # 취소된 신고는 목록에 두지 않는다. 사용자가 스스로 물린 것이다.
    cancelled = {
        event_id(e) for e in events if e.get("action") == "removed"
    }

    out: list[dict] = []
    for e in events:
        if e.get("action") in {"removed", "resolved"}:
            continue
        eid = event_id(e)
        if eid in cancelled:
            continue
        done = resolved.get(eid)
        workspace = str(e.get("workspace") or "")
        actor = str(e.get("actor") or "")
        text = str(e.get("text") or "").strip()
        out.append({
            "id": eid,
            "at": str(e.get("at") or ""),
            "workspace": workspace,
            "kind": str(e.get("kind") or ""),
            "actor": actor,
            "name": names.get(actor) or actor,
            "dept": depts.get(f"{workspace}:{actor}", ""),
            "qaRecordId": str(e.get("qa_record_id") or ""),
            # 본문은 관리자에게만 나간다. 신고에는 업무 내용이 들어 있다.
            "text": text if include_text else "",
            "hasText": bool(text),
            "handled": done is not None,
            "handledBy": str((done or {}).get("actor") or ""),
            "handledAt": str((done or {}).get("at") or ""),
            "handledNote": str((done or {}).get("text") or "") if include_text else "",
        })
    out.sort(key=lambda r: (r["handled"], r["at"]), reverse=False)
    # 처리 안 된 것을 위로. 처리된 것은 최근 것만 남긴다.
    open_rows = [r for r in out if not r["handled"]]
    done_rows = [r for r in out if r["handled"]]
    open_rows.sort(key=lambda r: r["at"], reverse=True)
    done_rows.sort(key=lambda r: r["handledAt"], reverse=True)
    return (open_rows + done_rows)[:limit]


def feedback_section(events: list[dict]) -> dict:
    """👍/👎 와 `/피드백` 을 합쳐 만족도를 낸다.

    **취소(`removed`)를 빼고 센다.** 눌렀다 취소한 것을 그대로 두면 만족도가 실제보다
    좋게 나오고, 그러면 이 숫자를 보고 아무 조치도 하지 않게 된다.
    """
    from ..feedback import event_id

    active = [
        e for e in events
        if e.get("action") not in {"removed", "resolved"}
    ]
    counts = Counter(str(e.get("kind") or "-") for e in active)
    positive = counts.get("positive", 0)
    negative = counts.get("negative", 0)
    missing = counts.get("missing", 0)
    corrections = counts.get("correction", 0)
    # 처리 표시가 붙은 신고는 남은 일감이 아니다. 처리해도 숫자가 줄지 않으면
    # 화면은 영원히 빨간색이고, 그러면 아무도 보지 않는다.
    handled = {
        str(e.get("target") or "")
        for e in events
        if e.get("action") == "resolved" and e.get("target")
    }

    rated = positive + negative + missing
    if rated < MIN_SAMPLE:
        level, rate = "unknown", None
        note = f"표본이 적어({rated}건) 만족도를 판단하지 않습니다"
    else:
        rate = positive / rated
        level = _ratio_level(rate, SATISFACTION_WARN, SATISFACTION_BAD)
        note = ""

    problems = []
    if rate is not None and rate < SATISFACTION_WARN:
        problems.append(f"만족도가 {rate:.0%} 입니다 — 부정 {negative}건, 근거 없음 {missing}건")
    # 정정은 나쁜 신호가 아니라 **처리해야 할 일감**이다.
    # 본문 유무로 세지 않는다 — 본문이 없는 신고도 누군가 올린 것이고,
    # 그걸 조용히 빼면 아무도 처리하지 않은 채로 사라진다.
    open_written = sum(
        1 for e in active
        if str(e.get("kind") or "") in {"negative", "missing", "correction"}
        and event_id(e) not in handled
    )
    if open_written:
        problems.append(f"처리하지 않은 신고 {open_written}건")

    return {
        "level": level,
        "positive": positive,
        "negative": negative,
        "missing": missing,
        "corrections": corrections,
        "openCorrections": open_written,
        "rated": rated,
        "satisfaction": None if rate is None else round(rate, 3),
        "note": note,
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# 전체
# ---------------------------------------------------------------------------


def manifest_text() -> str:
    return _read(reader.manifest_path())


def code_text() -> str:
    return _read(Path(__file__).resolve().parents[1] / "slack" / "pilot.py")


def report(
    days: int = 7,
    allowed: frozenset[str] | set[str] | None = None,
    store=None,
    *,
    include_text: bool = False,
) -> dict:
    """대시보드가 그대로 그릴 수 있는 모양으로 낸다.

    `include_text` 는 신고 본문을 담을지다. 기본은 담지 않는다 — 신고에는 업무
    내용이 들어 있고, 권한은 막는 쪽이 기본값이다. 관리자에게만 켠다.
    """
    rows = reader.workspace_status(store)
    docs = reader.collected_docs(store)
    if allowed is not None:
        rows = [r for r in rows if str(r.get("key", "")) in allowed]
        docs = [d for d in docs if str(d.get("workspace", "")) in allowed]

    records = reader._read_qa_records(days)
    if allowed is not None:
        records = [r for r in records if str(r.get("workspace", "")) in allowed]

    events = _read_feedback(days, allowed)
    feedback = feedback_section(events)
    names = _display_names(records)
    pairs = {
        (str(e.get("workspace") or ""), str(e.get("actor") or ""))
        for e in events
        if e.get("workspace") and e.get("actor")
    }
    depts = _departments(pairs)
    feedback["contributors"] = contributors(
        events, names, depts, include_text=include_text
    )
    feedback["items"] = feedback_items(
        events, names, depts, include_text=include_text
    )

    sections = {
        "bot": bot_section(rows),
        "archive": archive_section(rows, docs),
        "answers": answer_section(records),
        "commands": command_section(manifest_text(), code_text()),
        "feedback": feedback,
    }
    overall = _worst(*[s["level"] for s in sections.values()])
    problems = [
        {"section": name, "message": msg}
        for name, s in sections.items()
        for msg in s.get("problems", [])
    ]
    return {
        "level": overall,
        "days": days,
        "checkedAt": reader._now().isoformat(timespec="seconds"),
        "sections": sections,
        "problems": problems,
    }
