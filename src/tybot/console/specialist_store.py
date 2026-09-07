"""PostgreSQL registry for code-defined specialist bots.

The console stores lifecycle metadata only.  It never accepts executable paths,
prompts, or arbitrary endpoints; those remain reviewed application code.
"""
from __future__ import annotations

import json
import os
import re

from .workspace_store import WorkspaceStoreError

KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
# `available` 은 여기 적지 않는다. **프롬프트 파일이 있으면 배포된 것**이다
# (`specialist_adapters.available_keys()`). 손으로 적으면 프롬프트를 지워도 화면은
# 「배포됨」 이라고 말하고, 그 어긋남은 눌러 봐야 드러난다.
ALLOWED_ADAPTERS = {
    "hermes": {"name": "Hermes", "domain": "내부 문서", "contracts": ("v1",)},
    "legal": {"name": "법률 전문 봇", "domain": "법률", "contracts": ("v1",)},
    "tax": {"name": "세무 전문 봇", "domain": "세무", "contracts": ("v1",)},
    "construction": {"name": "건설 전문 봇", "domain": "건설", "contracts": ("v1",)},
}


def _deployed() -> set[str]:
    """런타임이 실제로 있는 어댑터. 읽지 못하면 빈 집합 — 없다고 본다."""
    try:
        from ..specialist_adapters import available_keys

        return available_keys()
    except Exception:  # noqa: BLE001 - 배포 여부를 못 읽는 것이 화면을 막지 않는다
        return set()
VALID_STATES = {"draft", "enabled", "disabled"}


class SpecialistStoreError(RuntimeError):
    """A specialist registry operation could not be completed."""


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SpecialistStoreError("DATABASE_URL이 없습니다.")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise SpecialistStoreError(f"전문 봇 DB 연결 실패: {exc}") from exc


def adapters() -> list[dict]:
    deployed = _deployed()
    return [
        {"key": key, **value, "available": key in deployed}
        for key, value in ALLOWED_ADAPTERS.items()
    ]


def is_ready() -> bool:
    """Return whether the specialist lifecycle schema is available."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.specialist_bot') IS NOT NULL AS ready")
            return bool(cur.fetchone()["ready"])
    except Exception:  # noqa: BLE001 - capability discovery must fail closed
        return False


def _row(row: dict) -> dict:
    item = dict(row)
    item["workspaces"] = list(item.get("workspaces") or [])
    item["adapterAvailable"] = str(item["adapter"]) in _deployed()
    return item


def list_specialists() -> list[dict]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.*, coalesce(array_agg(sw.workspace ORDER BY sw.workspace)
                    FILTER (WHERE sw.workspace IS NOT NULL), '{}') AS workspaces
                  FROM specialist_bot s
                  LEFT JOIN specialist_workspace sw ON sw.specialist = s.key
                 GROUP BY s.key
                 ORDER BY s.name, s.key
                """
            )
            return [_row(row) for row in cur.fetchall()]
    except SpecialistStoreError:
        raise
    except Exception as exc:
        raise SpecialistStoreError(f"전문 봇 조회 실패: {exc}") from exc


def get_specialist(key: str) -> dict | None:
    key = key.strip().lower()
    return next((row for row in list_specialists() if row["key"] == key), None)


def list_requests() -> list[dict]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM specialist_change_request ORDER BY requested_at DESC LIMIT 200")
            return [dict(row) for row in cur.fetchall()]
    except SpecialistStoreError:
        raise
    except Exception as exc:
        raise SpecialistStoreError(f"전문 봇 변경 요청 조회 실패: {exc}") from exc


def _validate_proposal(proposal: dict) -> dict:
    key = str(proposal.get("key") or "").strip().lower()
    name = str(proposal.get("name") or "").strip()
    domain = str(proposal.get("domain") or "").strip()
    adapter = str(proposal.get("adapter") or "").strip().lower()
    state = str(proposal.get("state") or "draft").strip().lower()
    version = str(proposal.get("version") or "").strip()
    contract = str(proposal.get("contractVersion") or "v1").strip()
    workspaces = sorted({str(value).strip().lower() for value in proposal.get("workspaces", []) if str(value).strip()})
    if not KEY_RE.fullmatch(key):
        raise SpecialistStoreError("전문 봇 키는 영문 소문자로 시작하는 2~32자의 소문자·숫자·하이픈이어야 합니다.")
    if not name or len(name) > 80 or not domain or len(domain) > 80:
        raise SpecialistStoreError("이름과 담당 분야는 1~80자여야 합니다.")
    if adapter not in ALLOWED_ADAPTERS:
        raise SpecialistStoreError("코드에 등록되지 않은 전문 봇 어댑터입니다.")
    if state not in VALID_STATES:
        raise SpecialistStoreError("요청 가능한 상태가 아닙니다.")
    if len(version) > 80 or len(contract) > 40:
        raise SpecialistStoreError("버전 값이 너무 깁니다.")
    if contract not in ALLOWED_ADAPTERS[adapter]["contracts"]:
        raise SpecialistStoreError("코드에서 계약 검사를 통과한 계약 버전이 아닙니다.")

    model = str(proposal.get("model") or "").strip()
    if model:
        # **레지스트리에 있는 모델만.** 없는 값을 저장하면 저장할 때가 아니라
        # 질문할 때 실패하고(UnknownModel → 마스터 폴백), 그 실패는 조용하다.
        from ..gateway.router import DEFAULT_REGISTRY

        if model not in DEFAULT_REGISTRY:
            raise SpecialistStoreError(f"게이트웨이에 등록되지 않은 모델입니다: {model}")
    hint = str(proposal.get("routingHint") or "").strip()
    if len(hint) > 300:
        raise SpecialistStoreError("라우팅 설명은 300자를 넘을 수 없습니다.")
    rules = str(proposal.get("rules") or "").strip()
    if len(rules) > 8000:
        raise SpecialistStoreError("답변 규칙은 8000자를 넘을 수 없습니다.")
    try:
        min_confidence = float(proposal.get("minConfidence", 0.6))
    except (TypeError, ValueError) as exc:
        raise SpecialistStoreError("최소 신뢰도를 읽지 못했습니다.") from exc
    if not 0.0 <= min_confidence <= 1.0:
        raise SpecialistStoreError("최소 신뢰도는 0과 1 사이여야 합니다.")

    return {
        "key": key,
        "name": name,
        "domain": domain,
        "adapter": adapter,
        "state": state,
        "version": version,
        "contractVersion": contract,
        "workspaces": workspaces,
        "model": model,
        "routingHint": hint,
        "minConfidence": round(min_confidence, 2),
        "rules": rules,
    }


def create_request(*, actor: str, proposal: dict) -> int:
    clean = _validate_proposal(proposal)
    checks = [
        {"id": "adapter", "state": "pass", "detail": "코드 등록 어댑터"},
        {"id": "contract", "state": "pass", "detail": "계약 버전 지정"},
    ]
    try:
        with _connect() as conn, conn.cursor() as cur:
            if clean["workspaces"]:
                cur.execute("SELECT key FROM workspace WHERE key = any(%s)", (clean["workspaces"],))
                known = {str(row["key"]) for row in cur.fetchall()}
                unknown = sorted(set(clean["workspaces"]) - known)
                if unknown:
                    raise SpecialistStoreError(f"등록되지 않은 워크스페이스입니다: {', '.join(unknown)}")
            cur.execute(
                """
                INSERT INTO specialist_change_request (specialist, proposal, checks, requester)
                VALUES (%s, %s, %s, %s) RETURNING id
                """,
                (clean["key"], json.dumps(clean, ensure_ascii=False),
                 json.dumps(checks, ensure_ascii=False), actor),
            )
            return int(cur.fetchone()["id"])
    except (SpecialistStoreError, WorkspaceStoreError):
        raise
    except Exception as exc:
        raise SpecialistStoreError(f"전문 봇 변경 요청 저장 실패: {exc}") from exc


def decide_request(
    *, request_id: int, actor: str, decision: str, note: str = "", allow_self: bool = False
) -> None:
    """변경 요청을 승인·반려한다.

    `allow_self` — **관리자 자기 승인.** 서버에 root 로 들어가 SQL 을 칠 수 있는
    사람에게 두 명을 강제하면, 콘솔을 놔두고 그쪽으로 도는 길만 열린다.
    그쪽은 기록이 남지 않는다. 막는 대신 남긴다(감사 기록에 실행자가 남는다).
    개발자는 그대로 다른 사람의 승인을 받는다.
    """
    if decision not in {"approve", "reject"}:
        raise SpecialistStoreError("승인 또는 반려만 선택할 수 있습니다.")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM specialist_change_request WHERE id = %s FOR UPDATE", (request_id,))
            row = cur.fetchone()
            if row is None or row["state"] != "awaiting_approval":
                raise SpecialistStoreError("처리할 수 있는 전문 봇 변경 요청이 아닙니다.")
            if not allow_self and str(row["requester"]).lower() == actor.lower():
                raise SpecialistStoreError("자신이 만든 요청은 직접 승인할 수 없습니다.")
            state = "approved" if decision == "approve" else "rejected"
            if decision == "approve":
                p = dict(row["proposal"])
                adapter = str(p["adapter"])
                requested_state = str(p["state"])
                if requested_state == "enabled" and adapter not in _deployed():
                    raise SpecialistStoreError("런타임 어댑터가 아직 배포되지 않아 활성화할 수 없습니다.")
                if p["contractVersion"] not in ALLOWED_ADAPTERS[adapter]["contracts"]:
                    raise SpecialistStoreError("승인된 계약 검사 버전이 아닙니다.")
                cur.execute(
                    """
                    INSERT INTO specialist_bot
                        (key, name, domain, adapter, state, version, contract_version,
                         model, routing_hint, min_confidence, rules, rules_version,
                         created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        name = excluded.name, domain = excluded.domain,
                        adapter = excluded.adapter, state = excluded.state,
                        version = excluded.version,
                        contract_version = excluded.contract_version,
                        model = excluded.model,
                        routing_hint = excluded.routing_hint,
                        min_confidence = excluded.min_confidence,
                        rules = excluded.rules,
                        -- 규칙이 실제로 바뀔 때만 올린다. 이름만 고친 승인에도
                        -- 버전이 오르면 「무엇이 돌고 있나」 의 뜻이 사라진다.
                        rules_version = specialist_bot.rules_version
                            + CASE WHEN specialist_bot.rules = excluded.rules
                                   THEN 0 ELSE 1 END,
                        updated_at = now(), updated_by = excluded.updated_by
                    """,
                    (p["key"], p["name"], p["domain"], adapter, requested_state,
                     p["version"], p["contractVersion"], p.get("model", ""),
                     p.get("routingHint", ""), p.get("minConfidence", 0.6),
                     p.get("rules", ""), actor, actor),
                )
                cur.execute("DELETE FROM specialist_workspace WHERE specialist = %s", (p["key"],))
                for workspace in p["workspaces"]:
                    cur.execute(
                        "INSERT INTO specialist_workspace (specialist, workspace) VALUES (%s, %s)",
                        (p["key"], workspace),
                    )
            cur.execute(
                """
                UPDATE specialist_change_request
                   SET state = %s, approver = %s, decided_at = now(), note = %s
                 WHERE id = %s
                """,
                (state, actor, note.strip()[:500], request_id),
            )
    except SpecialistStoreError:
        raise
    except Exception as exc:
        raise SpecialistStoreError(f"전문 봇 변경 요청 처리 실패: {exc}") from exc


def list_calls(*, allowed: set[str] | frozenset[str] | None, specialist: str = "", result: str = "", limit: int = 200) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if allowed is not None:
        clauses.append("workspace = any(%s)")
        params.append(sorted(allowed))
    if specialist:
        clauses.append("specialist = %s")
        params.append(specialist)
    if result:
        clauses.append("result = %s")
        params.append(result)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, at, workspace, specialist, routing_reason, confidence,
                           result, elapsed_ms, cost_usd, error_code
                      FROM specialist_call {where} ORDER BY at DESC LIMIT %s""",
                params,
            )
            return [dict(row) for row in cur.fetchall()]
    except SpecialistStoreError:
        raise
    except Exception as exc:
        raise SpecialistStoreError(f"전문 봇 호출 기록 조회 실패: {exc}") from exc


def record_call(*, workspace: str, specialist: str, routing_reason: str, confidence: float | None,
                result: str, elapsed_ms: int, cost_usd: float, error_code: str = "") -> None:
    """Record non-sensitive routing metadata for a specialist adapter."""
    if result not in {"success", "fallback", "error", "contract_violation"}:
        raise SpecialistStoreError("지원하지 않는 전문 봇 호출 결과입니다.")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO specialist_call
                    (workspace, specialist, routing_reason, confidence, result,
                     elapsed_ms, cost_usd, error_code)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (workspace, specialist, routing_reason[:200], confidence, result,
                 max(0, elapsed_ms), max(0, cost_usd), error_code[:100]),
            )
    except Exception as exc:
        raise SpecialistStoreError(f"전문 봇 호출 기록 저장 실패: {exc}") from exc
