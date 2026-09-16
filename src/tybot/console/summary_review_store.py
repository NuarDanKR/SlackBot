"""콘솔이 읽는 요약 검토 회차 상태(B-50).

설계: [`docs/design/summary-review-canvas.md`](../../../docs/design/summary-review-canvas.md) §9

## 무엇을 보여 주고 무엇을 안 보여 주나

| 보여 준다 | 안 보여 준다 |
|---|---|
| 회차 상태·실패 코드 | Canvas 본문 |
| 후보 건수와 결정 건수 | 후보 문장·근거 원문 |
| 발송·결정 수치 | 사람이 적은 정정사항 |
| Canvas 링크 | 내부 파일 경로 |

정정사항은 그 검토자가 쓴 판단이다. 콘솔에 띄우면 다음 검토자가 그 문장에 끌리고,
관리자 화면이 **근거의 사본**이 된다(설계 §11).

`ambiguous` 를 따로 세는 이유: 그 회차는 **사람이 확인해야** 다음으로 간다.
자동 재생성은 하지 않는다 — Slack 이 이미 Canvas 를 만들었을 수 있다.
"""
from __future__ import annotations

import os

# 한 화면에 실을 회차 수. 더 필요하면 기간으로 좁힌다.
MAX_ROWS = 100


class SummaryReviewStoreError(RuntimeError):
    """요약 검토 회차를 읽지 못했다."""


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SummaryReviewStoreError("DATABASE_URL이 없습니다.")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise SummaryReviewStoreError(f"요약 검토 DB 연결 실패: {exc}") from exc


def is_ready() -> bool:
    """회차 스키마가 올라가 있는가. **못 읽으면 False**(막는 쪽이 기본값)."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.summary_review_artifact') IS NOT NULL AS ready"
            )
            row = cur.fetchone() or {}
            return bool(row.get("ready"))
    except Exception:  # noqa: BLE001 - 기능 탐지는 막는 쪽으로 실패한다
        return False


def rounds(workspaces: list[str] | None = None, *, limit: int = MAX_ROWS) -> list[dict]:
    """최근 회차 목록. **본문 없이** 좌표와 수치만."""
    where = ""
    params: list = []
    if workspaces is not None:
        if not workspaces:
            return []
        where = "WHERE a.workspace = ANY(%s)"
        params.append(list(workspaces))
    params.append(max(1, min(int(limit or MAX_ROWS), MAX_ROWS)))
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT a.id, a.workspace, a.channel_id, a.channel_name, a.review_date,
                   a.state, a.error_code, a.canvas_permalink, a.created_at, a.ready_at,
                   (SELECT count(*) FROM summary_review_artifact_candidate m
                     WHERE m.artifact_id = a.id) AS candidates,
                   (SELECT count(*) FROM summary_review_artifact_candidate m
                      JOIN summary_review_candidate c ON c.id = m.candidate_id
                     WHERE m.artifact_id = a.id
                       AND c.state IN ('approved','rejected')) AS decided,
                   (SELECT count(*) FROM summary_review_delivery d
                     WHERE d.artifact_id = a.id AND d.state = 'sent') AS sent,
                   (SELECT count(*) FROM summary_review_delivery d
                     WHERE d.artifact_id = a.id AND d.state = 'failed') AS failed
              FROM summary_review_artifact a
              {where}
             ORDER BY a.created_at DESC
             LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    return [_row(row) for row in rows]


def _row(row: dict) -> dict:
    return {
        "id": str(row.get("id") or ""),
        "workspace": str(row.get("workspace") or ""),
        "channelId": str(row.get("channel_id") or ""),
        "channelName": str(row.get("channel_name") or ""),
        "reviewDate": str(row.get("review_date") or ""),
        "state": str(row.get("state") or ""),
        # 비민감 코드만. 예외 메시지 원문을 그대로 싣지 않는다.
        "errorCode": str(row.get("error_code") or ""),
        "canvasUrl": str(row.get("canvas_permalink") or ""),
        "candidates": int(row.get("candidates") or 0),
        "decided": int(row.get("decided") or 0),
        "sent": int(row.get("sent") or 0),
        "failed": int(row.get("failed") or 0),
        "createdAt": str(row.get("created_at") or ""),
    }


def health(workspaces: list[str] | None = None) -> dict:
    """진단 한 줄에 쓸 집계. 읽지 못하면 **모른다고** 말한다."""
    try:
        items = rounds(workspaces)
    except SummaryReviewStoreError:
        return {"available": False, "ambiguous": 0, "failed": 0, "open": 0}
    return {
        "available": True,
        # 사람이 손대야 하는 것. 자동으로 다시 만들지 않는다.
        "ambiguous": sum(1 for r in items if r["state"] == "ambiguous"),
        "failed": sum(1 for r in items if r["state"] == "failed"),
        "open": sum(1 for r in items if r["state"] in ("ready", "partial")),
    }


__all__ = [
    "MAX_ROWS",
    "SummaryReviewStoreError",
    "health",
    "is_ready",
    "rounds",
]
