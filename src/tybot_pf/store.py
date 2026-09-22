"""PF 표 조회와 감사 기록. **PF 자기 표만 만진다.**

TYBot 표(`workspace` · `usage_call` · `archive_doc` · `specialist_*` · `raw_line`)를
여기서 부르지 않는다. 부를 수도 없다 — PF DB role 에 권한이 없다. 코드 규칙과 DB 권한이
같은 말을 하게 두는 이유는, 둘 중 하나가 틀렸을 때 나머지가 막아 주기 때문이다.
"""
from __future__ import annotations

import logging

from .auth import _connect

logger = logging.getLogger("tybot_pf.store")


class PFStoreError(RuntimeError):
    """PF 표를 읽지 못했다."""


def services(keys: list[str] | None = None) -> list[dict]:
    """서비스 목록. `keys` 를 주면 그 안에서만 고른다.

    **`keys` 는 요청이 아니라 권한에서 온다.** 요청 body 로 service key 를 받아 범위를
    바꾸지 않는다(§8.3) — 받으면 권한 없는 키를 넣어 보는 것이 곧 탐색 도구가 된다.
    """
    sql = """
        SELECT key, label, state, business_owner, infra_owner, state_dir
          FROM managed_service
         WHERE state <> 'disabled'
    """
    params: tuple = ()
    if keys is not None:
        if not keys:
            return []
        sql += " AND key = ANY(%s)"
        params = ([str(k).strip().lower() for k in keys],)
    sql += " ORDER BY label, key"
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        raise PFStoreError(f"서비스 목록을 읽지 못했습니다: {exc}") from exc


def service(key: str, *, allowed: list[str]) -> dict | None:
    """권한 있는 서비스 하나. 권한 밖이면 **없는 것처럼** 돌려준다.

    「권한이 없습니다」 와 「그런 서비스가 없습니다」 를 가르면, 키를 바꿔 넣어 보는
    것만으로 어떤 서비스가 존재하는지 알 수 있다.
    """
    wanted = key.strip().lower()
    if wanted not in {str(value).strip().lower() for value in allowed}:
        return None
    found = services([wanted])
    return found[0] if found else None


def record(
    *,
    actor: str,
    action: str,
    service_key: str = "",
    outcome: str = "succeeded",
    detail: str = "",
) -> None:
    """감사 한 줄. **실패해도 화면을 막지 않는다.**

    조회만 여는 오늘도 기록한다. action 을 열 때 감사를 같이 만들면 그 사이의 조작은
    아무 데도 안 남는다.

    본문·시크릿은 담지 않는다. `detail` 은 사람이 읽을 짧은 문장이다.
    """
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pf_audit_event (actor, service, action, outcome, detail)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (actor, service_key, action, outcome, detail[:500]),
            )
    except Exception as exc:  # noqa: BLE001 - 기록 실패가 조회를 막지 않는다
        logger.warning("PF 감사 기록 실패 action=%s: %s", action, exc)


def audit(*, actor: str, allowed: list[str], limit: int = 100) -> list[dict]:
    """감사 목록.

    두 가지를 보여 준다.

    - 권한 있는 **서비스**의 기록. 그 서비스를 보는 사람들이 무엇을 했나
    - 서비스와 무관한 내 기록(로그인처럼). **본인 것만** — 남의 로그인 시각은
      그 사람의 근무 시간표가 된다

    권한이 하나도 없어도 자기 로그인 기록은 보인다. 그게 「나는 들어왔는데 왜
    아무것도 없나」 에 답하는 유일한 줄이다.
    """
    keys = [str(value).strip().lower() for value in allowed]
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT at, actor, service, action, outcome, detail
                  FROM pf_audit_event
                 WHERE (service <> '' AND service = ANY(%s))
                    OR (service = '' AND lower(actor) = %s)
                 ORDER BY at DESC
                 LIMIT %s
                """,
                (keys, actor.strip().lower(), max(1, min(limit, 500))),
            )
            return [
                {
                    "at": row["at"].isoformat(timespec="seconds") if row["at"] else "",
                    "actor": row["actor"],
                    "service": row["service"],
                    "action": row["action"],
                    "outcome": row["outcome"],
                    "detail": row["detail"],
                }
                for row in cur.fetchall()
            ]
    except Exception as exc:
        raise PFStoreError(f"감사 기록을 읽지 못했습니다: {exc}") from exc
