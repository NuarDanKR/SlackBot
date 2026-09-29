"""Slack 라이선스 현황 — 할당(사람이 입력)과 활성(Slack API)을 한 표로 만든다.

콘솔 > 운영 > 라이선스 현황이 쓴다. 스키마: `deploy/sql/slack_license_schema.sql`

## 무엇이 어디서 오나
| 값 | 출처 |
|---|---|
| 워크스페이스 목록 | 워크스페이스 레지스트리 + 환경변수 토큰(= Slack 연동) · 사람이 직접 추가한 행 |
| 할당 | 사람이 입력 → `slack_license` / `slack_license_manual` (Slack 에서 가져올 수 없는 값) |
| 활성 | 연동 워크스페이스는 Slack `users.list` · 직접 추가한 곳은 사람이 입력 |

직접 추가한 워크스페이스만 삭제할 수 있다. 연동 워크스페이스는 레지스트리에서 오므로
여기서 지워도 다음 조회에 다시 나타난다 — 지울 수 있는 것처럼 보이면 안 된다.

## 활성을 어떻게 세나
비활성화(deleted)되지 않은 **사람 계정**이다. 봇·앱 계정·Slackbot 은 뺀다.
단일 채널 게스트는 요금이 붙지 않으므로 빼고, 다중 채널 게스트는 넣는다.

Slack 의 「공정 청구(최근 14일 미접속 자동 제외)」 는 `team.billableInfo` 로만 알 수 있고
그 API 는 관리자 **사용자** 토큰이 필요하다. 봇 토큰으로는 부를 수 없어서 쓰지 않는다 —
그래서 여기 숫자는 청구서 숫자보다 클 수 있다. 화면에도 그렇게 적는다.

## 토큰은 이 모듈 밖으로 나가지 않는다
복호화한 봇 토큰은 Slack 호출에만 쓰고 응답·로그에 싣지 않는다.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .workspace_store import WorkspaceStoreError, _connect

logger = logging.getLogger("tybot.console.license_store")
KST = timezone(timedelta(hours=9))

#: Slack 조회 결과를 이만큼 재사용한다. 화면을 열 때마다 워크스페이스 수만큼
#: `users.list` 를 부르면 Tier 2 한도(분당 20회)에 금방 닿는다.
CACHE_SECONDS = 600
MAX_ALLOCATED = 1_000_000
MAX_LABEL = 80

#: 라이선스 현황에 싣지 않는 연동 워크스페이스(표시 이름 기준).
#: 「Slack확산TFT」 는 도입 작업용 워크스페이스라 조직 라이선스 비교에서 뺀다(2026-09-28 요청).
#: 이름은 DB 레지스트리에서 오므로 띄어쓰기·대소문자는 무시하고 비교한다.
EXCLUDED_LABELS = frozenset({"slack확산tft"})


def _label_key(label: str) -> str:
    return "".join(str(label).split()).casefold()


def without_excluded(linked: dict[str, tuple[str, str | None]]) -> dict[str, tuple[str, str | None]]:
    return {key: value for key, value in linked.items() if _label_key(value[0]) not in EXCLUDED_LABELS}


class LicenseStoreError(RuntimeError):
    """라이선스 설정을 안전하게 읽거나 쓰지 못했다."""


@dataclass(frozen=True)
class ActiveCount:
    active: int
    guests: int
    fetched_at: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Slack 활성 수
# ---------------------------------------------------------------------------

def count_active_members(client) -> tuple[int, int]:
    """(활성 수, 그중 다중 채널 게스트 수). 페이지를 끝까지 넘긴다."""
    active = 0
    guests = 0
    cursor = None
    while True:
        response = client.users_list(limit=200, cursor=cursor) if cursor else client.users_list(limit=200)
        for member in response.get("members") or []:
            if member.get("deleted") or member.get("is_bot") or member.get("is_app_user"):
                continue
            if member.get("id") == "USLACKBOT":
                continue
            if member.get("is_ultra_restricted"):
                continue  # 단일 채널 게스트 — 요금이 붙지 않는다
            active += 1
            if member.get("is_restricted"):
                guests += 1
        cursor = ((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            return active, guests


_cache: dict[str, tuple[float, ActiveCount]] = {}
_cache_lock = threading.Lock()


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _fetch_one(key: str, token: str, client_factory) -> ActiveCount:
    try:
        active, guests = count_active_members(client_factory(token=token, timeout=15))
        return ActiveCount(active=active, guests=guests, fetched_at=_now_iso())
    except Exception as exc:  # noqa: BLE001 - Slack 오류는 그 워크스페이스 칸에만 적는다
        response = getattr(exc, "response", None)
        detail = str(response.get("error") or "") if isinstance(response, dict) or hasattr(response, "get") else ""
        logger.warning("라이선스 활성 수 조회 실패 — workspace=%s error=%s", key, detail or type(exc).__name__)
        return ActiveCount(
            active=0, guests=0, fetched_at=_now_iso(),
            error=f"Slack 조회 실패: {detail or type(exc).__name__}",
        )


def active_counts(
    tokens: dict[str, str], *, refresh: bool = False, client_factory=None
) -> dict[str, ActiveCount]:
    """연동 워크스페이스별 활성 수. 실패한 곳은 `error` 를 채워 돌려준다."""
    if client_factory is None:
        from slack_sdk import WebClient

        client_factory = WebClient
    now = time.monotonic()
    result: dict[str, ActiveCount] = {}
    todo: dict[str, str] = {}
    with _cache_lock:
        for key, token in tokens.items():
            cached = _cache.get(key)
            if not refresh and cached and now - cached[0] < CACHE_SECONDS and cached[1].error is None:
                result[key] = cached[1]
            else:
                todo[key] = token
    if todo:
        with ThreadPoolExecutor(max_workers=min(6, len(todo))) as pool:
            futures = {key: pool.submit(_fetch_one, key, token, client_factory) for key, token in todo.items()}
            fetched = {key: future.result() for key, future in futures.items()}
        with _cache_lock:
            for key, count in fetched.items():
                _cache[key] = (now, count)
        result.update(fetched)
    return result


def linked_workspaces() -> dict[str, tuple[str, str | None]]:
    """Slack 연동 워크스페이스 — {키: (표시 이름, 봇 토큰 또는 None)}.

    레지스트리(DB 암호화 토큰)가 우선이고, 토큰이 없으면 환경변수 토큰을 쓴다.
    사용 중지된 워크스페이스도 라이선스는 차지하므로 목록에 둔다.
    """
    from ..workspaces import env_suffix
    from .workspace_store import runtime_workspaces

    out: dict[str, tuple[str, str | None]] = {}
    try:
        for row in runtime_workspaces():
            key = str(row["key"]).lower()
            out[key] = (str(row.get("label") or key), row.get("bot_token") or None)
    except WorkspaceStoreError as exc:
        logger.warning("워크스페이스 레지스트리를 읽지 못해 환경변수만 봅니다: %s", exc)

    configured = [key.strip().lower() for key in (os.getenv("WORKSPACES") or "").split(",") if key.strip()]
    for key in configured:
        token = os.getenv(f"SLACK_BOT_TOKEN_{env_suffix(key)}", "").strip() or None
        label, current = out.get(key, (os.getenv(f"WORKSPACE_LABEL_{env_suffix(key)}", "") or key, None))
        if token or key not in out:
            out[key] = (label, current or token)
    single = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if single and not configured:
        key = os.getenv("PILOT_WORKSPACE", "pilot").lower()
        label, current = out.get(key, (os.getenv("WORKSPACE_LABEL", "") or key, None))
        out[key] = (label, current or single)
    return without_excluded(out)


# ---------------------------------------------------------------------------
# 저장된 할당
# ---------------------------------------------------------------------------

def list_stored() -> dict[str, int]:
    """연동 워크스페이스의 할당 — {워크스페이스 키: 할당 수}."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT workspace, allocated FROM slack_license")
            return {str(row["workspace"]).lower(): int(row["allocated"]) for row in cur.fetchall()}
    except WorkspaceStoreError as exc:
        raise LicenseStoreError(str(exc)) from exc
    except Exception as exc:
        raise LicenseStoreError(f"라이선스 설정 조회 실패: {exc}") from exc


def list_manual() -> list[dict]:
    """직접 추가한 워크스페이스."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, label, allocated, active, created_by FROM slack_license_manual ORDER BY id"
            )
            return [dict(row) for row in cur.fetchall()]
    except WorkspaceStoreError as exc:
        raise LicenseStoreError(str(exc)) from exc
    except Exception as exc:
        raise LicenseStoreError(f"직접 추가한 워크스페이스 조회 실패: {exc}") from exc


def _count(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_ALLOCATED:
        raise LicenseStoreError(f"{name}은(는) 0~{MAX_ALLOCATED:,} 사이의 정수여야 합니다.")
    return value


def validate(rows: list[dict], linked: set[str]) -> list[tuple[str, int]]:
    """연동 워크스페이스 할당을 검사한다. 규칙에 걸리면 422 로 돌려줄 수 있게 이유를 적는다."""
    clean: list[tuple[str, int]] = []
    seen: set[str] = set()
    for row in rows:
        workspace = str(row.get("workspace") or "").strip().lower()
        if workspace not in linked:
            raise LicenseStoreError(f"Slack 에 연동되지 않은 워크스페이스입니다: {workspace or '(빈 값)'}")
        if workspace in seen:
            raise LicenseStoreError(f"같은 워크스페이스가 두 번 들어왔습니다: {workspace}")
        seen.add(workspace)
        clean.append((workspace, _count(row.get("allocated"), "할당 라이선스")))
    return clean


def validate_manual(rows: list[dict], linked_labels: set[str]) -> list[dict]:
    """직접 추가한 워크스페이스를 검사한다.

    이름은 연동 워크스페이스와도 겹치면 안 된다 — 같은 이름이 두 줄이면 합계가 두 번
    잡히고, 어느 쪽이 실제 값인지 사람이 알 수 없다.
    """
    clean: list[dict] = []
    seen: set[str] = set()
    taken = {_label_key(label) for label in linked_labels}
    for row in rows:
        label = str(row.get("label") or "").strip()
        if not label or len(label) > MAX_LABEL or "\n" in label or "\r" in label:
            raise LicenseStoreError(f"워크스페이스 이름은 1~{MAX_LABEL}자의 한 줄이어야 합니다.")
        key = _label_key(label)
        if key in EXCLUDED_LABELS:
            raise LicenseStoreError(f"라이선스 현황에서 제외한 워크스페이스입니다: {label}")
        if key in taken:
            raise LicenseStoreError(f"Slack 에 연동된 워크스페이스와 이름이 같습니다: {label}")
        if key in seen:
            raise LicenseStoreError(f"같은 이름의 워크스페이스가 두 번 들어왔습니다: {label}")
        seen.add(key)
        row_id = row.get("id")
        clean.append({
            "id": int(row_id) if row_id is not None else None,
            "label": label,
            "allocated": _count(row.get("allocated"), "할당 라이선스"),
            "active": _count(row.get("active"), "활성 라이선스"),
        })
    return clean


def save(
    *,
    actor: str,
    rows: list[dict],
    linked: dict[str, tuple[str, str | None]],
    manual: list[dict] | None = None,
    removed: list[int] | None = None,
) -> dict:
    """한 트랜잭션으로 저장한다. 반쯤 저장된 표는 합계가 틀린 표다."""
    clean = validate(rows, set(linked))
    manual_rows = validate_manual(manual or [], {label for label, _token in linked.values()})
    removed_ids = sorted({int(value) for value in (removed or [])})
    if {row["id"] for row in manual_rows if row["id"] is not None} & set(removed_ids):
        raise LicenseStoreError("삭제할 워크스페이스를 동시에 수정할 수 없습니다.")
    result = {"saved": len(clean), "added": 0, "updated": 0, "deleted": 0}
    try:
        with _connect() as conn, conn.cursor() as cur:
            for workspace, allocated in clean:
                cur.execute(
                    """
                    INSERT INTO slack_license (workspace, allocated, updated_by)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (workspace) DO UPDATE SET
                        allocated = excluded.allocated,
                        updated_at = now(),
                        updated_by = excluded.updated_by
                    """,
                    (workspace, allocated, actor),
                )
            # 지우기를 먼저 한다. 지운 이름으로 다시 추가할 때 UNIQUE 에 걸리지 않게.
            for row_id in removed_ids:
                cur.execute("DELETE FROM slack_license_manual WHERE id = %s", (row_id,))
                result["deleted"] += cur.rowcount
            for row in manual_rows:
                if row["id"] is None:
                    cur.execute(
                        """
                        INSERT INTO slack_license_manual
                            (label, allocated, active, created_by, updated_by)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (row["label"], row["allocated"], row["active"], actor, actor),
                    )
                    result["added"] += 1
                    continue
                cur.execute(
                    """
                    UPDATE slack_license_manual
                       SET label = %s, allocated = %s, active = %s,
                           updated_at = now(), updated_by = %s
                     WHERE id = %s
                    """,
                    (row["label"], row["allocated"], row["active"], actor, row["id"]),
                )
                if cur.rowcount != 1:
                    raise LicenseStoreError("다른 곳에서 먼저 삭제된 워크스페이스입니다. 화면을 새로 고쳐 주세요.")
                result["updated"] += 1
    except LicenseStoreError:
        raise
    except WorkspaceStoreError as exc:
        raise LicenseStoreError(str(exc)) from exc
    except Exception as exc:
        if "slack_license_manual_label_key" in str(exc):
            raise LicenseStoreError("같은 이름의 워크스페이스가 이미 있습니다.") from exc
        raise LicenseStoreError(f"라이선스 설정 저장 실패: {exc}") from exc
    return result


# ---------------------------------------------------------------------------
# 화면 응답
# ---------------------------------------------------------------------------

def build_report(
    linked: dict[str, tuple[str, str | None]],
    stored: dict[str, int],
    counts: dict[str, ActiveCount],
    manual: list[dict] | None = None,
) -> dict:
    """연동 목록 · 저장된 할당 · Slack 조회 결과 · 직접 추가한 행을 한 표로 합친다.

    DB·Slack 없이 시험한다. 연동이 끊긴 워크스페이스의 할당은 보이지 않는다 —
    활성 수를 알 수 없는 줄은 비교가 안 된다.
    """
    rows: list[dict] = []
    for key, (label, token) in without_excluded(linked).items():
        count = counts.get(key)
        error = None if token else "봇 토큰이 없어 Slack 에서 조회하지 못했습니다."
        if count and count.error:
            error = count.error
        ok = count is not None and count.error is None
        rows.append({
            "kind": "slack",
            "id": None,
            "workspace": key,
            "label": label,
            "allocated": int(stored.get(key, 0)),
            "active": count.active if ok else None,
            "guests": count.guests if ok else 0,
            "fetchedAt": count.fetched_at if ok else None,
            "error": error,
        })
    for row in manual or []:
        rows.append({
            "kind": "manual",
            "id": int(row["id"]),
            "workspace": None,
            "label": str(row["label"]),
            "allocated": int(row.get("allocated") or 0),
            "active": int(row.get("active") or 0),
            "guests": 0,
            "fetchedAt": None,
            "error": None,
        })
    rows.sort(key=lambda row: row["label"])
    synced = [row for row in rows if row["kind"] == "slack" and row["active"] is not None]
    return {
        "rows": rows,
        "linkedCount": sum(1 for row in rows if row["kind"] == "slack"),
        "syncedCount": len(synced),
        "syncedAt": max((row["fetchedAt"] for row in synced if row["fetchedAt"]), default=None),
        "cacheSeconds": CACHE_SECONDS,
    }
