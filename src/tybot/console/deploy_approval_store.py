"""Database queue for separating deployment requesters from approvers."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta


class DeployApprovalError(RuntimeError):
    """A deployment approval transition was rejected."""


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise DeployApprovalError("DATABASE_URL이 없습니다.")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise DeployApprovalError(f"배포 승인 DB 연결 실패: {exc}") from exc


def _git(*args: str) -> str:
    source = os.getenv("TYBOT_SRC", "/var/lib/tybot/src")
    try:
        result = subprocess.run(
            ["git", "-C", source, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeployApprovalError(f"배포 기준 커밋을 확인하지 못했습니다: {exc}") from exc
    return result.stdout.strip()


def list_requests(allowed: set[str] | None) -> list[dict]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            params: list[object] = []
            scope = ""
            if allowed is not None:
                scope = "WHERE lower(d.workspace) = any(%s)"
                params.append(sorted(allowed))
            cur.execute(
                f"""
                SELECT d.id, d.workspace, w.label AS workspace_label, d.requester,
                       d.requested_at, d.repo, d.branch, d.commit_sha, d.commit_title,
                       d.author, d.fast_forward, d.files, d.checks, d.state,
                       d.approval_expires_at, d.approver, d.decided_at
                  FROM deploy_request d
                  JOIN workspace w ON w.key = d.workspace
                  {scope}
                 ORDER BY
                       (d.state IN ('awaiting_checks', 'awaiting_approval', 'blocked',
                                    'approved', 'applying')) DESC,
                       d.requested_at DESC
                 LIMIT 100
                """,
                params,
            )
            rows = []
            for row in cur.fetchall():
                item = dict(row)
                item["workspace"] = str(item["workspace"]).lower()
                rows.append(item)
            return rows
    except DeployApprovalError:
        raise
    except Exception as exc:
        raise DeployApprovalError(f"배포 요청 조회 실패: {exc}") from exc


def create_request(*, workspace: str, requester: str, reason: str) -> int:
    workspace = workspace.strip().lower()
    reason = reason.strip()
    if len(reason) < 5 or len(reason) > 500:
        raise DeployApprovalError("변경 이유를 5~500자로 입력하세요.")
    commit = _git("rev-parse", "HEAD")
    title = _git("log", "-1", "--format=%s")
    author = _git("log", "-1", "--format=%an")
    branch = os.getenv("TYBOT_BRANCH", "master")
    checks = [
        {
            "id": "server-gate",
            "label": "서버 배포 게이트",
            "state": "pending",
            "detail": "승인 후 update.sh가 전체 테스트와 fast-forward 여부를 검사합니다.",
        }
    ]
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('deploy_approval_queue'))")
            cur.execute(
                "SELECT key FROM workspace WHERE lower(key) = lower(%s) AND state <> 'disabled'",
                (workspace,),
            )
            workspace_row = cur.fetchone()
            if workspace_row is None:
                raise DeployApprovalError("사용 중인 워크스페이스가 아닙니다.")
            db_workspace = str(workspace_row["key"])
            cur.execute(
                """
                SELECT id FROM deploy_request
                 WHERE workspace = %s
                   AND state IN ('awaiting_checks', 'awaiting_approval', 'approved', 'applying')
                 LIMIT 1
                """,
                (db_workspace,),
            )
            if cur.fetchone() is not None:
                raise DeployApprovalError("이 워크스페이스에는 이미 처리 중인 배포 요청이 있습니다.")
            cur.execute(
                """
                INSERT INTO deploy_request
                    (workspace, requester, repo, branch, commit_sha, commit_title,
                     author, fast_forward, files, checks, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, true, '[]'::jsonb, %s, 'awaiting_approval')
                RETURNING id
                """,
                (
                    db_workspace,
                    requester,
                    os.getenv("TYBOT_REPO_LABEL", "SlackBot"),
                    branch,
                    commit,
                    f"{title} — {reason}",
                    author,
                    json.dumps(checks, ensure_ascii=False),
                ),
            )
            request_id = int(cur.fetchone()["id"])
            cur.execute(
                """
                INSERT INTO deploy_event (workspace, commit_sha, actor, action, note)
                VALUES (%s, %s, %s, '요청', %s)
                """,
                (db_workspace, commit, requester, reason),
            )
            return request_id
    except DeployApprovalError:
        raise
    except Exception as exc:
        raise DeployApprovalError(f"배포 요청 저장 실패: {exc}") from exc


def decide_request(*, request_id: int, approver: str, decision: str, note: str) -> dict:
    if decision not in {"approve", "reject"}:
        raise DeployApprovalError("승인 또는 반려만 선택할 수 있습니다.")
    note = note.strip()[:500]
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('deploy_approval_queue'))")
            cur.execute("SELECT * FROM deploy_request WHERE id = %s FOR UPDATE", (request_id,))
            row = cur.fetchone()
            if row is None:
                raise DeployApprovalError("배포 요청을 찾을 수 없습니다.")
            if row["state"] != "awaiting_approval":
                raise DeployApprovalError("이미 처리됐거나 승인할 수 없는 요청입니다.")
            if str(row["requester"]).lower() == approver.lower():
                raise DeployApprovalError("자신이 만든 배포 요청은 직접 승인할 수 없습니다.")
            if decision == "reject":
                cur.execute(
                    """
                    UPDATE deploy_request
                       SET state = 'rejected', approver = %s, decided_at = now()
                     WHERE id = %s
                    """,
                    (approver, request_id),
                )
                cur.execute(
                    """
                    INSERT INTO deploy_event (workspace, commit_sha, actor, action, note)
                    VALUES (%s, %s, %s, '반려', %s)
                    """,
                    (row["workspace"], row["commit_sha"], approver, note),
                )
                return {"approved": False, "workspace": row["workspace"]}
            expires = datetime.now(UTC) + timedelta(minutes=10)
            cur.execute(
                """
                UPDATE deploy_request
                   SET state = 'approved', approver = %s, decided_at = now(),
                       approval_expires_at = %s
                 WHERE id = %s
                """,
                (approver, expires, request_id),
            )
            cur.execute(
                """
                INSERT INTO deploy_event (workspace, commit_sha, actor, action, note)
                VALUES (%s, %s, %s, '승인', %s)
                """,
                (row["workspace"], row["commit_sha"], approver, note),
            )
            return {"approved": True, "workspace": row["workspace"]}
    except DeployApprovalError:
        raise
    except Exception as exc:
        raise DeployApprovalError(f"배포 승인 저장 실패: {exc}") from exc


def restore_awaiting(request_id: int) -> None:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE deploy_request
                   SET state = 'awaiting_approval', approver = NULL, decided_at = NULL,
                       approval_expires_at = NULL
                 WHERE id = %s AND state = 'approved'
                """,
                (request_id,),
            )
    except Exception as exc:
        raise DeployApprovalError(f"승인 상태 복구 실패: {exc}") from exc


def mark_result(request_id: int, state: str, actor: str, note: str = "") -> None:
    mapped = {"running": "applying", "ok": "live", "failed": "blocked", "skipped": "live"}
    target = mapped.get(state)
    if target is None:
        return
    action = "적용" if target == "live" else None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE deploy_request SET state = %s WHERE id = %s",
                (target, request_id),
            )
            if action:
                cur.execute(
                    """
                    INSERT INTO deploy_event (workspace, commit_sha, actor, action, note)
                    SELECT workspace, commit_sha, %s, %s, %s FROM deploy_request WHERE id = %s
                    """,
                    (actor, action, note[:500], request_id),
                )
    except Exception as exc:
        raise DeployApprovalError(f"배포 결과 기록 실패: {exc}") from exc
