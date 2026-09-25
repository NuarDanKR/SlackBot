"""워크스페이스에 붙는 서비스 — master · archiver · hermes_direct.

결정: 2026-09-25 오너 확정 §2·§3. 스키마: `deploy/sql/workspace_service_schema.sql`.

## 워크스페이스를 봇마다 만들지 않는다

Archiving Bot 은 별도 Slack 앱이라 토큰이 따로 있다. 그렇다고 워크스페이스를 하나
더 만들면 같은 채널이 두 워크스페이스에 존재하고, ACL·한도·아카이브 경로가 전부
갈라진다. 권한이 두 곳에 있으면 한 곳만 고치는 날이 오고, 그날 **한쪽에서만**
새어 나간다(절대 원칙 3).

## 토큰은 나가지 않는다

`workspace_secret` 과 같은 규칙이다 — 복호화 조회 함수를 **만들지 않는다.**
여기서 나가는 것은 `mask` 뿐이고, 봇이 쓸 평문은 기동 시 자기가 복호화한다.

만들지 않는 이유는 쓰는 사람을 못 믿어서가 아니다. 있으면 언젠가 로그에 찍히고,
로그는 감사보다 오래 남는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .workspace_store import (
    WorkspaceStoreError,
    _connect,
    _fernet,
    _mask,
    _validate_token,
)


class Service(StrEnum):
    MASTER = "master"
    ARCHIVER = "archiver"
    # PF 직접 호출. **공존 기간에만** 쓴다(오너 결정 §1).
    HERMES_DIRECT = "hermes_direct"


#: Slack 토큰을 갖는 서비스. 전부다 — **이 표에 없는 것이 요점**이다.
#:
#: 최종 Hermes specialist 는 여기 없다. 전문 봇이 Slack 에 직접 붙으면 우리가
#: 권한을 판정할 자리가 사라진다 — 계약이 「우리가 필터한 텍스트만 준다」 인데
#: 자기 토큰이 있으면 그 계약이 무의미해진다(CLAUDE.md 봇 체계).
#: 전문 봇의 접근 범위는 `specialist_workspace` 가 정한다.
TOKEN_BEARING: frozenset[Service] = frozenset(Service)


class ServiceStoreError(WorkspaceStoreError):
    """서비스 등록·변경이 거절됐다."""


@dataclass(frozen=True)
class Identity:
    """Slack `auth.test` 가 말해 준 신원. **사람이 적는 값이 아니다.**"""

    team_id: str
    bot_user_id: str


def check_identity(expected: Identity, actual: Identity) -> str:
    """토큰이 **이 워크스페이스의 이 서비스** 것인가. 아니면 사유를 돌려준다.

    토큰을 잘못 붙이면 다른 워크스페이스에 수집한다. 그건 오류가 아니라 유출이고,
    붙일 때 확인하지 않으면 한참 뒤에 발견된다 — 그때는 이미 남의 대화가 우리
    아카이브에 들어와 있다.
    """
    if not actual.team_id or not actual.bot_user_id:
        return "Slack 이 team_id 또는 bot_user_id 를 주지 않았습니다"
    if expected.team_id and actual.team_id != expected.team_id:
        return (
            f"다른 워크스페이스의 토큰입니다: 기대 {expected.team_id}, "
            f"실제 {actual.team_id}"
        )
    if expected.bot_user_id and actual.bot_user_id == expected.bot_user_id:
        # 같은 봇 사용자면 별도 앱이 아니라 **같은 앱을 두 번 등록**한 것이다.
        # 그 상태로 Socket Mode 를 두 곳에서 열면 이벤트를 양쪽이 받는다
        # (중복 답변·비용 2배, CLAUDE.md 금지사항).
        return f"이미 다른 서비스가 쓰는 봇 사용자입니다: {actual.bot_user_id}"
    return ""


def list_services(workspace: str) -> list[dict]:
    """이 워크스페이스에 붙은 서비스. **mask 만 나간다.**"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.service, s.state, s.error, s.team_id, s.bot_user_id,
                   s.identity_ok, s.identity_error, s.identity_checked_at,
                   s.note, s.updated_at, s.updated_by,
                   max(sec.mask) FILTER (WHERE sec.kind = 'bot') AS bot_mask,
                   max(sec.mask) FILTER (WHERE sec.kind = 'app') AS app_mask,
                   count(sec.kind) AS token_count
              FROM workspace_service s
              LEFT JOIN workspace_service_secret sec
                     ON sec.workspace = s.workspace AND sec.service = s.service
             WHERE s.workspace = %s
             GROUP BY s.service, s.state, s.error, s.team_id, s.bot_user_id,
                      s.identity_ok, s.identity_error, s.identity_checked_at,
                      s.note, s.updated_at, s.updated_by
             ORDER BY s.service
            """,
            (workspace,),
        )
        return [dict(row) for row in cur.fetchall()]


def save_service(
    workspace: str,
    service: Service | str,
    *,
    actor: str,
    bot_token: str | None = None,
    app_token: str | None = None,
    note: str = "",
) -> None:
    """서비스를 등록하거나 토큰을 교체한다.

    **상태는 여기서 `enabled` 로 만들지 않는다.** 신원 검사를 통과해야 켜진다
    (`record_identity`). 등록과 동시에 켤 수 있으면 토큰을 잘못 붙인 채로 수집이
    시작되고, 그건 붙인 사람이 자리를 뜬 뒤에 드러난다.
    """
    name = Service(service)
    if (bot_token is None) != (app_token is None):
        raise ServiceStoreError("토큰을 교체할 때는 봇 토큰과 앱 토큰을 함께 입력하세요.")

    encrypted: dict[str, tuple[bytes, str]] = {}
    if bot_token is not None and app_token is not None:
        bot = _validate_token(bot_token, "xoxb-", "봇 토큰")
        app = _validate_token(app_token, "xapp-", "앱 토큰")
        cipher = _fernet()
        encrypted = {
            "bot": (cipher.encrypt(bot.encode("utf-8")), _mask(bot)),
            "app": (cipher.encrypt(app.encode("utf-8")), _mask(app)),
        }

    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"ws-service:{workspace}",)
            )
            cur.execute("SELECT 1 FROM workspace WHERE key = %s", (workspace,))
            if not cur.fetchone():
                raise ServiceStoreError(f"없는 워크스페이스입니다: {workspace}")
            cur.execute(
                """
                INSERT INTO workspace_service (workspace, service, state, note, updated_by)
                VALUES (%s, %s, 'disabled', %s, %s)
                ON CONFLICT (workspace, service) DO UPDATE SET
                    note = excluded.note,
                    updated_at = now(),
                    updated_by = excluded.updated_by
                """,
                (workspace, str(name), note, actor),
            )
            for kind, (ciphertext, mask) in encrypted.items():
                cur.execute(
                    """
                    INSERT INTO workspace_service_secret
                        (workspace, service, kind, ciphertext, mask, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (workspace, service, kind) DO UPDATE SET
                        ciphertext = excluded.ciphertext,
                        mask = excluded.mask,
                        updated_at = now(),
                        updated_by = excluded.updated_by
                    """,
                    (workspace, str(name), kind, ciphertext, mask, actor),
                )
            if encrypted:
                # 토큰이 바뀌면 이전 신원 검사 결과는 **더 이상 이 토큰의 것이
                # 아니다.** 지우지 않으면 새 토큰이 옛 검사 결과를 물려받아
                # 검사 없이 켜진 채로 남는다.
                cur.execute(
                    """
                    UPDATE workspace_service
                       SET identity_ok = NULL, identity_error = '',
                           identity_checked_at = NULL,
                           state = CASE WHEN state = 'enabled' THEN 'disabled' ELSE state END
                     WHERE workspace = %s AND service = %s
                    """,
                    (workspace, str(name)),
                )
                _audit(
                    cur, actor, workspace,
                    field=f"service.{name}.token",
                    old_value="", new_value=mask_summary(encrypted),
                    reason=note or "토큰 교체",
                )
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise ServiceStoreError(f"서비스 저장 실패: {exc}") from exc


def mask_summary(encrypted: dict[str, tuple[bytes, str]]) -> str:
    """감사에 남길 값. **가린 것만** 남는다.

    평문은 화면·로그·감사 어디에도 안 나간다(오너 결정 §3).
    """
    return " ".join(f"{kind}={mask}" for kind, (_, mask) in sorted(encrypted.items()))


def record_identity(
    workspace: str, service: Service | str, actual: Identity, *, actor: str
) -> str:
    """신원 검사 결과를 적고, 통과했으면 켠다. 사유가 있으면 안 켜고 돌려준다."""
    name = Service(service)
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT team_id, bot_user_id FROM workspace_service
                 WHERE workspace = %s AND service <> %s AND bot_user_id <> ''
                """,
                (workspace, str(name)),
            )
            others = [dict(row) for row in cur.fetchall()]
            expected = Identity(
                team_id=others[0]["team_id"] if others else "",
                bot_user_id="",
            )
            problem = check_identity(expected, actual)
            if not problem and any(
                row["bot_user_id"] == actual.bot_user_id for row in others
            ):
                problem = f"이미 다른 서비스가 쓰는 봇 사용자입니다: {actual.bot_user_id}"

            cur.execute(
                """
                UPDATE workspace_service
                   SET team_id = %s, bot_user_id = %s,
                       identity_ok = %s, identity_error = %s,
                       identity_checked_at = now(),
                       state = CASE WHEN %s THEN 'enabled' ELSE 'disabled' END,
                       error = NULL,
                       updated_at = now(), updated_by = %s
                 WHERE workspace = %s AND service = %s
                """,
                (
                    actual.team_id, actual.bot_user_id,
                    not problem, problem,
                    not problem, actor, workspace, str(name),
                ),
            )
            if int(cur.rowcount or 0) != 1:
                raise ServiceStoreError(
                    f"등록되지 않은 서비스라 신원을 기록할 수 없습니다: {workspace}/{name}"
                )
            _audit(
                cur, actor, workspace,
                field=f"service.{name}.identity",
                old_value="", new_value="ok" if not problem else "refused",
                reason=problem or "신원 검사 통과",
            )
            return problem
    except Exception as exc:
        raise ServiceStoreError(f"신원 기록 실패: {exc}") from exc


def _audit(cur, actor: str, workspace: str, *, field: str,
           old_value: str, new_value: str, reason: str) -> None:
    """설정 변경은 **지워지지 않는 기록**으로 남는다(오너 결정 §5).

    누가 언제 무엇을 바꿨는지 없으면, 사고가 났을 때 범위를 정할 수 없다.
    """
    cur.execute(
        """
        INSERT INTO archive_config_audit
            (actor, subject, workspace, channel_id, field, old_value, new_value, reason)
        VALUES (%s, 'workspace_service', %s, '', %s, %s, %s, %s)
        """,
        (actor, workspace, field, old_value, new_value, reason),
    )
