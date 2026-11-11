"""필수 앱 넷의 **정체성과 채널 멤버십**을 모은다 (B-87).

판정은 `channel_apps.evaluate` 가 한다. 여기서는 사실만 모으고, 못 모은 것은
`None` 으로 남긴다 — 「없다」 와 「못 읽었다」 는 다른 사실이고, 그 구분이
이 기능의 전부다.

## 실패를 삼키되 숨기지 않는다

DB 를 못 읽어도, Slack 이 거절해도 상태 화면은 떠야 한다. 한 조각 때문에 화면
전체가 사라지면 사람은 **다른 멀쩡한 항목도** 못 본다. 그래서 전부 `try` 로
감싸되, 실패한 자리는 `None` 으로 남겨 화면이 「확인 불가」 라고 말하게 한다.
"""

from __future__ import annotations

import logging
import os

from .channel_apps import (
    CATALOG_KEYS,
    DB_UNREADABLE,
    HERMES,
    AppBinding,
    ChannelAppFacts,
    binding_from_connection,
    converter_binding,
    hermes_binding,
)

log = logging.getLogger(__name__)

#: 한 번에 읽을 멤버 수. Slack 상한은 1000 이고, 큰 채널에서 한 페이지만 보면
#: 뒤쪽에 있는 앱을 「미초대」 로 적게 된다.
PAGE_SIZE = 200

#: 몇 쪽까지 따라갈까. 끝까지 못 읽었으면 **멤버 목록을 통째로 버린다** —
#: 반쪽 목록으로 「없다」 고 말하는 것이 이 모듈이 막으려는 바로 그 고장이다.
MAX_PAGES = 20

#: `users.list` 한 쪽의 크기. Slack 권장 상한이 200 이다.
USER_PAGE_SIZE = 200

#: 봇 사용자 지도를 다시 읽는 간격(초).
CACHE_SECONDS = 300.0

#: workspace -> (읽은 시각, {봇 사용자 ID: 앱 ID}). 실패는 담지 않는다.
_app_map_cache: dict[str, tuple[float, dict]] = {}


def _connections(workspace: str) -> dict[str, dict]:
    """`bot_connection` 에서 이 워크스페이스의 Slack 연결을 읽는다.

    읽지 못하면 **빈 dict 가 아니라 예외**를 올린다 — 빈 dict 는 「연결이
    없다」 와 구분되지 않고, 그러면 DB 장애가 「콘솔에 등록하세요」 안내로
    둔갑한다.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL not configured")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT bot_key, state, bot_user_id, identity_ok
              FROM bot_connection
             WHERE workspace = %s AND connector_type = 'slack_socket'
            """,
            (workspace,),
        )
        return {str(row["bot_key"]): dict(row) for row in cur.fetchall()}


def _hermes_route_mode(workspace: str) -> str:
    """Hermes 를 내부 호출로 쓰고 있나. 못 읽으면 `disabled` 로 본다.

    모르는 상태에서 「외부 RPC 라 해당 없음」 이라고 적으면, 정말로 빠진 Slack
    연결이 그 문장 뒤에 숨는다. 막는 쪽이 기본값이다.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return "disabled"
    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT route_mode
                  FROM specialist_route
                 WHERE workspace = %s AND specialist = %s
                """,
                (workspace, HERMES),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - 상태 화면은 떠야 한다
        log.warning("Hermes 라우트 조회 실패 ws=%s: %s", workspace, type(exc).__name__)
        return "disabled"
    return str(row[0]) if row else "disabled"


def bindings(workspace: str, *, converter_ids=None, delegated=None) -> tuple:
    """네 역할의 정체성. Slack 은 부르지 않는다."""
    from .archive import converter_bots

    try:
        rows = _connections(workspace)
        known = True
    except Exception as exc:  # noqa: BLE001 - DB 없이도 화면은 떠야 한다
        log.warning("봇 연결 조회 실패 ws=%s: %s", workspace, type(exc).__name__)
        rows, known = {}, False

    out = []
    for role in ("archiver", "tybot"):
        key = CATALOG_KEYS[role]
        if not known:
            # DB 를 못 읽은 것을 「등록 안 됨」 으로 적지 않는다. 그렇게 적으면
            # DB 장애가 「콘솔에 등록하세요」 안내로 둔갑하고, 사람은 이미
            # 등록된 것을 또 등록하러 간다.
            out.append(AppBinding(
                role=role,
                missing_code=DB_UNREADABLE,
                invite_hint="콘솔 DB 연결을 확인하세요.",
                note="콘솔 DB 를 읽지 못해 연결을 확인할 수 없습니다.",
            ))
            continue
        out.append(binding_from_connection(role, rows.get(key)))

    ids = converter_bots.converter_app_ids() if converter_ids is None else converter_ids
    if delegated is None:
        # 「모름」 을 「꺼짐」 으로 접지 않는다(B-89 후속). 꺼짐으로 적으면 화면이
        # 「해당 없음」 을 보여 주는데, 그동안 TYBot 은 HWP 를 변환하지 않는다.
        state = converter_bots.delegation_state()
        on = True if state == "on" else False if state == "off" else None
    else:
        on = delegated
    out.append(converter_binding(ids, delegated=on))

    out.append(
        hermes_binding(
            rows.get(CATALOG_KEYS[HERMES]) if known else None,
            route_mode=_hermes_route_mode(workspace) if known else "disabled",
        )
    )
    return tuple(out)


def members(client, channel_id: str) -> tuple[frozenset[str] | None, str]:
    """채널 멤버의 Slack 사용자 ID 전부. 실패하면 `(None, 사유)`.

    **끝까지 읽지 못하면 버린다.** 반쪽 목록은 뒤 페이지에 있는 앱을
    「미초대」 로 만든다.
    """
    found: set[str] = set()
    cursor = ""
    for _ in range(MAX_PAGES):
        try:
            res = client.conversations_members(
                channel=channel_id, limit=PAGE_SIZE, cursor=cursor
            ) or {}
        except Exception as exc:  # noqa: BLE001 - 사유만 남기고 판정은 위에서
            code = getattr(getattr(exc, "response", None), "data", {}) or {}
            reason = str(code.get("error") or type(exc).__name__)
            log.info("채널 멤버 조회 실패 ch=%s: %s", channel_id, reason)
            return None, reason
        found.update(str(value) for value in (res.get("members") or []))
        cursor = str(((res.get("response_metadata") or {}).get("next_cursor")) or "")
        if not cursor:
            return frozenset(found), ""
    log.warning("채널 멤버가 너무 많아 끝까지 읽지 못했습니다 ch=%s", channel_id)
    return None, "too_many_members"


def bot_app_map(client, workspace: str, *, now: float | None = None) -> dict | None:
    """워크스페이스의 **봇 사용자 → Slack 앱 ID** 지도. 못 읽으면 `None`.

    ## 왜 `users.list` 인가

    처음에는 멤버마다 `users.info` 를 불렀다. 멤버가 N 명이면 호출이 N 번이고,
    `/채널 상태` 한 번에 수십~수백 번이 된다. Slack 의 `users.info` 는 Tier 4
    (분당 100+)지만 그 한도는 **워크스페이스 전체가 나눠 쓴다** — 상태 화면
    하나가 수집기와 답변 경로의 호출까지 같이 굶긴다.

    `users.list` 는 한 쪽에 200 명씩 온다. 같은 정보를 **1/200 의 호출**로 얻고,
    아래 캐시가 그 결과를 재사용한다.

    ## 캐시가 짧은 이유

    앱을 새로 설치하면 새 봇 사용자가 생긴다. 그 직후 `/채널 상태` 를 보는
    사람은 방금 넣은 앱을 확인하러 온 사람이다. TTL 이 길면 그 사람에게
    「없음」 이라고 말하게 된다. 5분은 호출을 줄이면서 그 상황을 몇 분 안에
    푸는 값이다.

    못 읽으면 캐시하지 않는다 — 실패를 캐시하면 한 번 꼬인 조회가 TTL 동안
    「확인 불가」 를 고정한다.
    """
    import time

    clock = time.monotonic() if now is None else now
    hit = _app_map_cache.get(workspace)
    if hit is not None and clock - hit[0] < CACHE_SECONDS:
        return hit[1]

    found: dict[str, str] = {}
    cursor = ""
    for _ in range(MAX_PAGES):
        try:
            res = client.users_list(limit=USER_PAGE_SIZE, cursor=cursor) or {}
        except Exception as exc:  # noqa: BLE001 - 판정은 위에서 한다
            log.info("사용자 목록 조회 실패 ws=%s: %s", workspace, type(exc).__name__)
            return None
        for member in res.get("members") or []:
            if not isinstance(member, dict) or not member.get("is_bot"):
                continue
            app_id = str(
                (member.get("profile") or {}).get("api_app_id") or ""
            ).strip().upper()
            if app_id:
                found[str(member.get("id") or "")] = app_id
        cursor = str(((res.get("response_metadata") or {}).get("next_cursor")) or "")
        if not cursor:
            _app_map_cache[workspace] = (clock, found)
            return found
    # 끝까지 못 읽었다. 반쪽 지도로 「그 앱이 없다」 고 말하지 않는다.
    log.warning("사용자가 너무 많아 목록을 끝까지 읽지 못했습니다 ws=%s", workspace)
    return None


def app_ids(client, workspace: str, member_ids) -> frozenset[str] | None:
    """채널 멤버 중 **봇이 속한 앱 ID**. 지도를 못 읽으면 `None`.

    변환 앱은 앱 ID 로 관리하므로(`TYBOT_CONVERTER_APP_IDS`) 사용자 ID 만으로는
    대조할 수 없다 — 이름으로 맞히지 않기 위해 치르는 비용이다.
    """
    mapping = bot_app_map(client, workspace)
    if mapping is None:
        return None
    return frozenset(
        mapping[user_id] for user_id in (member_ids or ()) if user_id in mapping
    )


def clear_cache() -> None:
    """시험과 앱 설치 직후에 쓴다."""
    _app_map_cache.clear()


def facts(
    client,
    workspace: str,
    channel_id: str,
    *,
    is_private: bool = False,
    need_app_ids: bool = True,
) -> ChannelAppFacts:
    """상태 화면이 쓸 사실 한 묶음."""
    found = bindings(workspace)
    member_ids, error = members(client, channel_id)
    resolved = None
    if member_ids is not None and need_app_ids:
        resolved = app_ids(client, workspace, member_ids)
    return ChannelAppFacts(
        bindings=found,
        member_ids=member_ids,
        member_app_ids=resolved,
        is_private=is_private,
        probe_error=error,
    )


__all__ = [
    "CACHE_SECONDS",
    "MAX_PAGES",
    "PAGE_SIZE",
    "USER_PAGE_SIZE",
    "app_ids",
    "bindings",
    "bot_app_map",
    "clear_cache",
    "facts",
    "members",
]
