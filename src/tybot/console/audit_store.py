"""Unified, append-only console audit view."""
from __future__ import annotations

import json
import os
from pathlib import Path

SENSITIVE_KEYS = {"password", "secret", "token", "key", "content", "question", "feedback", "prompt"}


class AuditStoreError(RuntimeError):
    pass


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise AuditStoreError("DATABASE_URL이 없습니다.")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise AuditStoreError(f"감사 DB 연결 실패: {exc}") from exc


def _safe_metadata(metadata: dict | None) -> dict:
    clean: dict = {}
    for key, value in (metadata or {}).items():
        lowered = str(key).lower()
        if any(word in lowered for word in SENSITIVE_KEYS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[str(key)] = value
        elif isinstance(value, list):
            clean[str(key)] = [str(item)[:100] for item in value[:20]]
    return clean


def record(*, actor: str, category: str, action: str, target_type: str, target_id: str,
           workspace: str | None = None, outcome: str = "succeeded", metadata: dict | None = None) -> None:
    if outcome not in {"requested", "succeeded", "failed"}:
        raise AuditStoreError("지원하지 않는 감사 결과입니다.")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO console_audit_event
                    (actor, category, action, target_type, target_id, workspace, outcome, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (actor, category[:40], action[:80], target_type[:40], target_id[:200],
                 workspace, outcome, json.dumps(_safe_metadata(metadata), ensure_ascii=False)),
            )
    except Exception as exc:
        raise AuditStoreError(f"감사 기록 저장 실패: {exc}") from exc


def _legacy_jsonl(path: Path, category: str, action: str) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-500:]:
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        out.append({
            "id": f"legacy:{path.name}:{len(out)}",
            "at": raw.get("at") or raw.get("ts") or "",
            "actor": raw.get("email") or raw.get("actor") or "-",
            "category": category,
            "action": raw.get("action") or action,
            "targetType": "legacy",
            "targetId": raw.get("unit") or raw.get("path") or path.name,
            "workspace": raw.get("workspace"),
            "outcome": raw.get("result") or raw.get("outcome") or "succeeded",
            "source": "legacy",
            "metadata": {},
        })
    return out


def list_events(*, qa_log_dir: Path, category: str = "", workspace: str = "", actor: str = "",
                limit: int = 200) -> list[dict]:
    rows: list[dict] = []
    try:
        with _connect() as conn, conn.cursor() as cur:
            clauses: list[str] = []
            params: list[object] = []
            if category:
                clauses.append("category = %s")
                params.append(category)
            if workspace:
                clauses.append("workspace = %s")
                params.append(workspace)
            if actor:
                clauses.append("actor ILIKE %s")
                params.append(f"%{actor}%")
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            cur.execute(
                f"SELECT * FROM console_audit_event {where} ORDER BY at DESC LIMIT %s",
                params,
            )
            for raw in cur.fetchall():
                row = dict(raw)
                rows.append({
                    "id": str(row["id"]), "at": row["at"], "actor": row["actor"],
                    "category": row["category"], "action": row["action"],
                    "targetType": row["target_type"], "targetId": row["target_id"],
                    "workspace": row["workspace"], "outcome": row["outcome"],
                    "source": "database", "metadata": row.get("metadata") or {},
                })

            if not category or category == "deployment":
                deploy_clauses: list[str] = []
                deploy_params: list[object] = []
                if workspace:
                    deploy_clauses.append("workspace = %s")
                    deploy_params.append(workspace)
                if actor:
                    deploy_clauses.append("actor ILIKE %s")
                    deploy_params.append(f"%{actor}%")
                deploy_where = f"WHERE {' AND '.join(deploy_clauses)}" if deploy_clauses else ""
                deploy_params.append(limit)
                cur.execute(
                    f"SELECT * FROM deploy_event {deploy_where} ORDER BY at DESC LIMIT %s",
                    deploy_params,
                )
                for raw in cur.fetchall():
                    row = dict(raw)
                    rows.append({
                        "id": f"deploy:{row['id']}", "at": row["at"], "actor": row["actor"],
                        "category": "deployment", "action": row["action"],
                        "targetType": "commit", "targetId": row.get("commit_sha") or "-",
                        "workspace": row["workspace"], "outcome": "succeeded",
                        "source": "deploy_event", "metadata": {},
                    })
    except Exception:  # noqa: BLE001 - legacy audit remains available during DB rollout
        # Legacy records remain useful while the new schema is being rolled out.
        rows = []

    rows.extend(_legacy_jsonl(qa_log_dir / "archive-read.jsonl", "archive", "read"))
    rows.extend(_legacy_jsonl(qa_log_dir / "env-settings.jsonl", "environment", "change"))
    rows.extend(_legacy_jsonl(qa_log_dir / "timer-actions.jsonl", "timer", "action"))
    if category:
        rows = [row for row in rows if row["category"] == category]
    if workspace:
        rows = [row for row in rows if row.get("workspace") == workspace]
    if actor:
        rows = [row for row in rows if actor.lower() in str(row["actor"]).lower()]
    rows.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
    return rows[:limit]
