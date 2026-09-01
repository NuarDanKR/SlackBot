"""화면이 쓸 데이터를 파일에서 만든다.

## 왜 DB 없이 시작하나
아카이브 원문의 진실은 MD 파일이고, DB 는 "매 질문마다 수천 개 MD 를 읽지 않으려는 캐시"다
(`docs/design/db-and-acl.md`). 파일럿 규모(문서 수십 개)에서는 파일을 그대로 읽어도 충분하고,
`ArchiveStore` 는 이미 mtime 기준 캐시를 갖고 있다. DB 는 문서가 늘어난 뒤 같은 함수 뒤에
끼워 넣으면 된다 — 그래서 여기서 반환하는 모양을 화면 타입(`console-web/src/mock/types.ts`)과
1:1로 맞춘다.

## 여기서 만들지 않는 것
- **Slack 만 아는 것**(연결 상태, 채널 수, 초대 누락 채널): 봇이 상태 파일에 적고 여기서 읽는다.
  콘솔이 Slack 을 직접 호출하면 콘솔에도 토큰이 필요해지고 rate limit 을 갉아먹는다.
- **질문·답변 본문**: 감사기록에 있지만 화면으로 내려보내지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import heartbeat
from ..archive.store import ArchiveStore, workspace_from_path
from ..config import cost_state_path

logger = logging.getLogger("tybot.console.reader")

KST = timezone(timedelta(hours=9))
COURSE_DAYS = 30
BASELINE_DAYS = 14
# 마지막 수집이 이 시간을 넘기면 '수집 멈춤'으로 본다(업무일 기준 하루).
STALLED_HOURS = 24
WATCH_HOURS = 8


def manifest_path() -> Path:
    """Slack 앱 매니페스트 원본 위치. 배포본에서도 같은 상대 위치에 있다.

    **여기 두는 이유**: 헬스 체크가 매니페스트와 코드의 명령 목록을 맞춰 보는데,
    이 함수가 `app.py` 에 있으면 경로 하나 때문에 FastAPI 를 import 하게 된다.
    콘솔을 설치하지 않은(봇만 도는) 서버에서는 그것만으로 배포 테스트가 실패한다.
    """
    override = os.getenv("MANIFEST_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "docs" / "pilot" / "slack-app-manifest.yaml"


def archive_dir() -> Path:
    return Path(os.getenv("ARCHIVE_DIR", "./archive"))


def qa_log_dir() -> Path:
    return Path(os.getenv("QA_LOG_DIR", "./qa-log"))


def harness_dir() -> Path:
    return Path(os.getenv("HARNESS_DIR", str(archive_dir().parent / "harness")))


def _now() -> datetime:
    return datetime.now(KST)


# ---------------------------------------------------------------------------
# 워크스페이스 현황
# ---------------------------------------------------------------------------

def _workspace_labels() -> dict[str, str]:
    """환경변수에서 표시 이름을 모은다. 토큰이 없어도 읽을 수 있어야 한다.

    `load_workspaces()` 는 토큰이 없으면 예외를 던진다(봇이 반쪽으로 뜨는 걸 막으려고).
    콘솔은 토큰 없이도 목록을 보여 줘야 하므로 라벨만 따로 읽는다.
    """
    import re

    labels: dict[str, str] = {}
    keys = [k.strip() for k in (os.getenv("WORKSPACES") or "").split(",") if k.strip()]
    if not keys:
        key = os.getenv("PILOT_WORKSPACE", "pilot")
        return {key: os.getenv("WORKSPACE_LABEL", key)}
    for key in keys:
        sfx = re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")
        labels[key] = os.getenv(f"WORKSPACE_LABEL_{sfx}", key)
    return labels


def _readable_map(known: set[str]) -> dict[str, list[str]]:
    from ..workspaces import parse_cross_read

    try:
        parsed = parse_cross_read(os.getenv("CROSS_WS_READ"), known)
    except Exception as e:  # noqa: BLE001 - 설정 오류가 화면 전체를 막지 않는다
        logger.warning("CROSS_WS_READ 파싱 실패, 크로스 열람을 비워 둡니다: %s", e)
        return {}
    return {k: sorted(v) for k, v in parsed.items()}


def _root_keys() -> set[str]:
    return {k.strip() for k in (os.getenv("ROOT_WORKSPACES") or "").split(",") if k.strip()}


def _ts_to_date(ts: str) -> str | None:
    """원문 라인의 `2026-08-12 09:15` 에서 날짜만."""
    if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
        return ts[:10]
    return None


def _courses(dates: dict[str, int], today: date) -> list[dict]:
    """최근 30일을 하루 한 칸으로. 수집이 없던 날은 0 으로 남긴다(화면의 '수집 없음')."""
    out = []
    for i in range(COURSE_DAYS - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        out.append({"date": day, "lines": dates.get(day, 0)})
    return out


def _health(last_ingested: str | None, *, has_problem: bool, broken: int) -> str:
    if has_problem or last_ingested is None:
        return "stalled"
    try:
        age = (_now() - datetime.fromisoformat(last_ingested)).total_seconds() / 3600
    except ValueError:
        return "stalled"
    if age > STALLED_HOURS:
        return "stalled"
    if age > WATCH_HOURS or broken:
        return "watch"
    return "ok"


def workspace_status(store: ArchiveStore | None = None) -> list[dict]:
    """`데이터 현황` 화면이 쓰는 목록."""
    store = store or ArchiveStore(archive_dir())
    labels = _workspace_labels()
    docs = store.docs()
    broken = store.broken()
    readable = _readable_map(set(labels))
    roots = _root_keys()
    today = _now().date()
    spend = _spend_by_workspace_today()

    by_ws: dict[str, list] = defaultdict(list)
    for d in docs:
        by_ws[d.workspace].append(d)
    broken_by_ws: dict[str, int] = defaultdict(int)
    for path, _reason in broken:
        broken_by_ws[workspace_from_path(path, store.root)] += 1

    # 설정에 없지만 아카이브에는 있는 워크스페이스도 보여 준다(설정에서 빠진 것을 알 수 있게)
    keys = sorted(set(labels) | set(by_ws) | set(broken_by_ws))

    out: list[dict] = []
    for key in keys:
        ws_docs = by_ws.get(key, [])
        lines_by_day: dict[str, int] = defaultdict(int)
        raw_lines = 0
        last_ts: str | None = None
        for d in ws_docs:
            raw_lines += len(d.raw_lines)
            for line in d.raw_lines:
                day = _ts_to_date(line.ts)
                if day:
                    lines_by_day[day] += 1
            if d.last_ingested and (last_ts is None or d.last_ingested > last_ts):
                last_ts = d.last_ingested

        beat = heartbeat.read(key)
        stale = beat is None or heartbeat.is_stale(beat)
        write_problem = (beat or {}).get("write_problem")

        out.append(
            {
                "key": key,
                "label": labels.get(key, key),
                "role": "root" if key in roots else "member",
                "readable": readable.get(key, []),
                # 봇 상태 파일이 없거나 낡았으면 '모름'이다. 함부로 false 로 두면
                # 멀쩡한 봇을 '연결 끊김'으로 표시하게 된다.
                "connected": None if stale else bool(beat.get("connected")),
                "realtime": bool((beat or {}).get("realtime", True)),
                "channels": int((beat or {}).get("channels", len(ws_docs))),
                "uninvitedChannels": int((beat or {}).get("uninvited_channels", 0)),
                "docs": len(ws_docs),
                "rawLines": raw_lines,
                "brokenDocs": broken_by_ws.get(key, 0),
                "lastIngestedAt": last_ts,
                "writeProblem": write_problem,
                "courses": _courses(lines_by_day, today),
                "spendTodayUsd": spend.get(key, 0.0),
                "limitUsd": float((beat or {}).get("limit_usd", 0.0)),
                "health": _health(
                    last_ts, has_problem=bool(write_problem), broken=broken_by_ws.get(key, 0)
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 사용량 — 감사기록(JSONL)에서 만든다
# ---------------------------------------------------------------------------

def _read_qa_records(days: int) -> list[dict]:
    """최근 N일 감사기록. 월별 파일이라 필요한 달만 연다."""
    now = _now()
    wanted_months = {
        (now - timedelta(days=i)).strftime("%Y-%m") for i in range(days + 1)
    }
    cutoff = (now - timedelta(days=days)).isoformat()
    rows: list[dict] = []
    for month in sorted(wanted_months):
        path = qa_log_dir() / f"qa-{month}.jsonl"
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
                    if str(rec.get("ts", "")) >= cutoff:
                        rows.append(rec)
        except OSError as e:
            logger.warning("감사기록을 읽지 못했습니다 (%s): %s", path, e)
    return rows


def _spend_by_workspace_today() -> dict[str, float]:
    today = _now().date().isoformat()
    out: dict[str, float] = defaultdict(float)
    for rec in _read_qa_records(1):
        if str(rec.get("ts", ""))[:10] == today:
            out[str(rec.get("workspace", ""))] += float(rec.get("cost_usd") or 0)
    return dict(out)


def _spent_today_from_state() -> float | None:
    """봇이 남긴 당일 누적. 감사기록 합계보다 이쪽이 정확하다(분류 호출까지 포함)."""
    try:
        data = json.loads(Path(cost_state_path()).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if str(data.get("day")) != _now().date().isoformat():
        return 0.0
    try:
        return float(data.get("spent_usd") or 0)
    except (TypeError, ValueError):
        return None


def usage_snapshot(allowed: frozenset[str] | set[str] | None = None) -> dict:
    """`API 사용량` 화면이 쓰는 값. 질문 본문은 담지 않는다.

    `allowed` 를 주면 **그 워크스페이스의 기록만으로 모든 집계를 만든다.**
    호출한 쪽에서 결과를 나중에 걸러내는 방식은 쓰지 않는다 — 목록만 거르고 시간대별·모델별·
    기준선 같은 합계를 그대로 두면, 담당자가 다른 워크스페이스의 사용 패턴을 읽을 수 있다.
    범위를 한 곳(행을 고르는 자리)에서만 정하면 그런 누락이 생기지 않는다.
    """
    now = _now()
    today = now.date().isoformat()
    recent = _read_qa_records(BASELINE_DAYS)
    if allowed is not None:
        recent = [r for r in recent if str(r.get("workspace", "")) in allowed]
    today_rows = [r for r in recent if str(r.get("ts", ""))[:10] == today]

    by_hour_calls: dict[str, int] = defaultdict(int)
    by_hour_cost: dict[str, float] = defaultdict(float)
    by_model: dict[str, dict] = {}
    by_workspace: dict[str, dict] = defaultdict(lambda: {"calls": 0, "costUsd": 0.0})

    for r in today_rows:
        hour = str(r.get("ts", ""))[11:13] + ":00"
        by_hour_calls[hour] += 1
        by_hour_cost[hour] += float(r.get("cost_usd") or 0)
        model = r.get("model") or "-"
        m = by_model.setdefault(
            model, {"model": model, "calls": 0, "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}
        )
        m["calls"] += 1
        m["costUsd"] += float(r.get("cost_usd") or 0)
        w = by_workspace[str(r.get("workspace", ""))]
        w["calls"] += 1
        w["costUsd"] += float(r.get("cost_usd") or 0)

    # 기준선: 최근 14일 중 오늘을 뺀 날들의 '같은 시각까지 누적'의 중위값
    cutoff_hm = str(now.strftime("%H:%M"))
    per_day: dict[str, float] = defaultdict(float)
    for r in recent:
        ts = str(r.get("ts", ""))
        day = ts[:10]
        if day == today:
            continue
        if ts[11:16] <= cutoff_hm:
            per_day[day] += float(r.get("cost_usd") or 0)
    values = sorted(per_day.values())
    baseline = values[len(values) // 2] if values else 0.0

    # 당일 누적은 봇이 남긴 상태 파일이 더 정확하다(분류 호출까지 포함). 다만 그 값은
    # **전 워크스페이스 합산**이라, 범위가 좁혀진 요청에는 쓸 수 없다.
    spent = _spent_today_from_state() if allowed is None else None
    if spent is None:
        spent = sum(float(r.get("cost_usd") or 0) for r in today_rows)

    # 자정 예상: 지금까지의 속도를 남은 시간에 그대로 적용
    elapsed_h = now.hour + now.minute / 60
    projected = spent * (24 / elapsed_h) if elapsed_h > 0.5 else spent

    labels = _workspace_labels()
    keys = set(labels) if allowed is None else set(labels) & set(allowed)
    limits = {k: float((heartbeat.read(k) or {}).get("limit_usd", 0.0)) for k in keys}

    # 상한도 범위를 따른다. 전체 요청이면 합산 상한, 좁혀진 요청이면 그 워크스페이스 상한의 합.
    limit_usd = (
        float(os.getenv("DAILY_COST_LIMIT_USD", "50"))
        if allowed is None
        else round(sum(limits.values()), 6)
    )

    return {
        "asOf": now.isoformat(timespec="seconds"),
        "limitUsd": limit_usd,
        "spentUsd": round(spent, 6),
        "projectedUsd": round(projected, 6),
        "baselineUsd": round(baseline, 6),
        "callsToday": len(today_rows),
        "byHour": [
            {"hour": h, "calls": by_hour_calls[h], "costUsd": round(by_hour_cost[h], 6)}
            for h in sorted(by_hour_calls)
        ],
        "byModel": sorted(by_model.values(), key=lambda m: -m["costUsd"]),
        "byWorkspace": [
            {
                "key": k,
                "label": labels.get(k, k),
                "calls": v["calls"],
                "costUsd": round(v["costUsd"], 6),
                "limitUsd": limits.get(k, 0.0),
            }
            for k, v in sorted(by_workspace.items())
        ],
        "recent": [
            {
                "at": str(r.get("ts", ""))[11:16],
                "workspace": r.get("workspace", ""),
                "intent": r.get("intent_kind", ""),
                "source": r.get("intent_source", "llm"),
                "reason": r.get("reason", ""),
                "hits": int(r.get("hits") or 0),
                "model": r.get("model") or "-",
                "costUsd": float(r.get("cost_usd") or 0),
                "ms": int(r.get("elapsed_ms") or 0),
            }
            for r in sorted(today_rows, key=lambda r: str(r.get("ts", "")), reverse=True)[:30]
        ],
    }


# ---------------------------------------------------------------------------
# 수집 문서
# ---------------------------------------------------------------------------

def collected_docs(store: ArchiveStore | None = None) -> list[dict]:
    """문서 목록. **본문은 담지 않는다** — 열람은 별도 함수로, 기록을 남기며 한다."""
    store = store or ArchiveStore(archive_dir())
    labels = _workspace_labels()
    root = archive_dir()
    out: list[dict] = []

    for d in store.source_docs():
        try:
            size = d.path.stat().st_size
        except OSError:
            size = 0
        attachment = sum(1 for ln in d.raw_lines if ln.text.startswith("[첨부"))
        out.append(
            {
                "workspace": d.workspace,
                "workspaceLabel": labels.get(d.workspace, d.workspace),
                "channel": d.channel,
                "path": d.path.relative_to(root).as_posix(),
                "lines": len(d.raw_lines),
                "bytes": size,
                "attachmentLines": attachment,
                "lastIngestedAt": d.last_ingested,
                "visibility": d.visibility,
                "acl": sorted(d.acl),
                "shareWith": sorted(d.share_with),
                "schemaError": None,
                "content": None,
            }
        )

    for path, reason in store.broken():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        workspace = workspace_from_path(path, root)
        out.append(
            {
                "workspace": workspace,
                "workspaceLabel": labels.get(workspace, workspace),
                "channel": path.stem,
                "path": path.relative_to(root).as_posix(),
                "lines": 0,
                "bytes": size,
                "attachmentLines": 0,
                "lastIngestedAt": None,
                "visibility": "private",
                "acl": [],
                "shareWith": [],
                "schemaError": reason,
                "content": None,
            }
        )
    return sorted(out, key=lambda d: (d["workspace"], d["channel"]))


class NotFound(LookupError):
    """요청한 문서가 아카이브 안에 없다."""


def read_document(rel_path: str) -> str:
    """문서 본문을 읽는다. 경로 탈출을 막는다.

    `rel_path` 는 사용자가 보내는 값이다. `../../etc/passwd` 같은 값이 그대로 열리면
    아카이브 밖 파일이 노출된다. 그래서 **해석한 절대경로가 아카이브 안인지** 확인한다.
    """
    root = archive_dir().resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root) or target.suffix != ".md":
        raise NotFound(f"아카이브 안의 문서가 아닙니다: {rel_path}")
    try:
        return target.read_text(encoding="utf-8")
    except OSError as e:
        raise NotFound(f"문서를 읽지 못했습니다: {rel_path} ({e})") from e


# ---------------------------------------------------------------------------
# 봇 규칙(하네싱) 문서
# ---------------------------------------------------------------------------

KIND_BY_STEM = {
    "rules": ("rules", "답변 규칙"),
    "workflow": ("workflow", "업무 흐름"),
    "glossary": ("glossary", "용어 사전"),
    "prompt": ("prompt", "프롬프트"),
}


def harness_files() -> list[dict]:
    """`봇 규칙 편집` 화면이 쓰는 목록. 경로는 `<HARNESS_DIR>/<워크스페이스>/<이름>.md`."""
    base = harness_dir()
    labels = _workspace_labels()
    if not base.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(base.rglob("*.md")):
        ws = path.parent.name
        kind, title = KIND_BY_STEM.get(path.stem, ("rules", path.stem))
        try:
            content = path.read_text(encoding="utf-8")
            stat = path.stat()
        except OSError as e:
            logger.warning("규칙 문서를 읽지 못했습니다 (%s): %s", path, e)
            continue
        out.append(
            {
                "workspace": ws,
                "workspaceLabel": labels.get(ws, ws),
                "path": path.relative_to(base.parent).as_posix(),
                "title": title,
                "kind": kind,
                "updatedAt": datetime.fromtimestamp(stat.st_mtime, KST).isoformat(
                    timespec="seconds"
                ),
                "updatedBy": "-",
                "pendingRequestId": None,
                "content": content,
            }
        )
    return out
