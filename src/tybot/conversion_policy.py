"""변환 런타임 정책 — rhwp 앱 ID 와 야간 재점검 숫자의 **DB 정본** (B-89 후속).

## 왜 DB 로 옮기나

rhwp 위임은 「TYBot 이 HWP 를 직접 변환하는가」 를 정한다. 전에는 그 값이
`console-managed.env` 와 프로세스 환경변수 두 곳에 있었고, 수집기·콘솔·변환 잡이
서로 다른 파일을 보고 있을 수 있었다. 누가 언제 바꿨는지도 남지 않았다.

이제 한 행(`conversion_runtime_policy`)이 정본이다. 바꿀 때마다 감사 행이 하나씩
붙는다(`conversion_runtime_policy_audit`). 시크릿은 없다 — 앱 ID 와 숫자뿐이다.

## 모르면 「꺼짐」 이 아니다

예전 코드는 값이 비어 있으면 「위임 꺼짐」 으로 읽었다. 그러면 DB 가 잠깐 안 보이는
순간 TYBot 이 HWP 를 직접 변환하기 시작하고, 같은 문서가 rhwp PDF 와 두 벌의
근거가 된다. 그래서 상태는 셋이다.

- ``db`` — DB 행을 읽었고 값이 바르다. 위임 켜짐/꺼짐을 **말할 수 있는** 유일한 상태
- ``legacy`` — DB 행이 없거나 DB 가 안 보여, 전환 기간 동안 옛 설정(콘솔 파일·
  환경변수)을 읽었다. **앱 ID 가 하나 이상일 때만** 이 상태가 된다 — 옛 설정의 빈 값은
  「꺼짐」 이 아니라 「모른다」 다
- ``unknown`` — 그 밖 전부. DB 미접속·표 없음·행 없음·잘못된 값

`unknown` 과 `legacy` 에서는 HWP/HWPX 를 직접 변환하지 않는다. 야간 재점검의
`--apply` 는 **`db` 일 때만** 돈다 — 소급은 비용이 드는 쓰기라 출처가 확실해야 한다.

DB 에 행이 있는데 값이 틀리면 옛 설정으로 내려가지 **않는다.** DB 가 정본이 된 뒤에
틀린 값을 옛 값으로 덮으면, 누가 무엇을 보고 있는지 다시 알 수 없게 된다.

설계 메모: BACKLOG B-89 「변환 정책 DB 이전」.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

TABLE = "conversion_runtime_policy"
AUDIT_TABLE = "conversion_runtime_policy_audit"

DEFAULT_RECHECK_LIMIT = 50
DEFAULT_RHWP_GRACE_HOURS = 6

#: 한 밤 소급 상한. 0 은 「소급하지 않음」 이다.
RECHECK_LIMIT_RANGE = (0, 1000)
#: rhwp PDF 를 기다리는 시간(시). 0 은 「바로 미완료」 라 뜻이 없다.
RHWP_GRACE_RANGE = (1, 168)
#: 지정할 수 있는 rhwp 앱 수. 예외를 넓히는 값이라 상한을 둔다.
MAX_APP_IDS = 10

#: 옛 설정 키. 전환 기간에만 읽는다.
LEGACY_APPS_ENV = "TYBOT_CONVERTER_APP_IDS"
LEGACY_LIMIT_ENV = "TYBOT_CONVERT_RECHECK_LIMIT"
LEGACY_GRACE_ENV = "TYBOT_CONVERT_RHWP_GRACE_HOURS"
#: `0` 이면 옛 설정을 읽지 않는다 — 전환이 끝난 뒤 끈다.
LEGACY_SWITCH_ENV = "TYBOT_CONVERT_POLICY_LEGACY"

STATE_DB = "db"
STATE_LEGACY = "legacy"
STATE_UNKNOWN = "unknown"

#: 왜 `db` 가 아닌가. 고정 낱말이다 — 화면·로그·시험이 같은 값을 본다.
DB_UNCONFIGURED = "db_unconfigured"
DB_UNAVAILABLE = "db_unavailable"
TABLE_MISSING = "table_missing"
ROW_MISSING = "row_missing"
INVALID_VALUE = "invalid_value"

#: 옛 설정으로 내려가도 되는 사유. **틀린 DB 값은 여기 없다.**
_FALLBACK_REASONS = frozenset({DB_UNCONFIGURED, DB_UNAVAILABLE, TABLE_MISSING, ROW_MISSING})

SOURCE_DB = "db"
SOURCE_LEGACY_FILE = "legacy-console-file"
SOURCE_LEGACY_ENV = "legacy-env"
SOURCE_DEFAULT = "default"
SOURCE_UNKNOWN = "unknown"

DELEGATION_ON = "on"
DELEGATION_OFF = "off"
DELEGATION_UNKNOWN = "unknown"

#: 위임 대상 원본 형식. `converter_bots.DELEGATED_TYPES` 와 같다(순환 import 를 피해 둘).
DELEGATED_TYPES = frozenset({"hwp", "hwpx"})

_APP_ID = re.compile(r"^A[A-Z0-9]{6,}$")

#: 다시 읽는 간격. 콘솔에서 바꾸면 재시작 없이 이 안에 반영된다.
CACHE_SECONDS = 60.0
#: DB 가 수집 경로에서 오래 매달리지 않게 한다.
CONNECT_TIMEOUT_SECONDS = 3


class PolicyUnavailable(RuntimeError):
    """DB 를 읽지 못했다. `reason` 은 위의 고정 낱말 중 하나다."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


class PolicyConflict(RuntimeError):
    """다른 사람이 먼저 바꿨다. 화면을 새로 읽고 다시 저장해야 한다."""


class PolicyInvalid(ValueError):
    """저장하려는 값이 틀렸다. 메시지는 사람이 읽는 한국어다."""


@dataclass(frozen=True)
class ConversionPolicy:
    state: str
    reason: str = ""
    app_ids: frozenset[str] = frozenset()
    recheck_limit: int = DEFAULT_RECHECK_LIMIT
    rhwp_grace_hours: int = DEFAULT_RHWP_GRACE_HOURS
    sources: Mapping[str, str] = field(default_factory=dict)
    revision: int | None = None
    updated_by: str = ""
    updated_at: str = ""

    @property
    def known(self) -> bool:
        return self.state != STATE_UNKNOWN

    @property
    def delegation(self) -> str:
        """`on` · `off` · `unknown`. **`off` 는 DB 에서만 나온다.**"""
        if self.state == STATE_UNKNOWN:
            return DELEGATION_UNKNOWN
        if self.app_ids:
            return DELEGATION_ON
        return DELEGATION_OFF if self.state == STATE_DB else DELEGATION_UNKNOWN

    @property
    def nightly_apply_allowed(self) -> bool:
        return self.state == STATE_DB

    def direct_conversion_allowed(self, filetype: str) -> bool:
        """TYBot 이 이 형식을 직접 변환해도 되나. 위임 대상은 `off` 일 때만."""
        if str(filetype or "").lower() not in DELEGATED_TYPES:
            return True
        return self.delegation == DELEGATION_OFF

    def signature(self) -> tuple:
        return (
            self.state,
            self.reason,
            tuple(sorted(self.app_ids)),
            self.recheck_limit,
            self.rhwp_grace_hours,
            tuple(sorted(self.sources.items())),
            self.revision,
        )

    def to_json(self, *, include_actor: bool = False) -> dict:
        """화면·지표용. 시크릿은 없다. 실행자는 콘솔(관리자)에만 싣는다."""
        payload = {
            "state": self.state,
            "reason": self.reason,
            "delegation": self.delegation,
            "appIds": sorted(self.app_ids),
            "recheckLimit": self.recheck_limit if self.known else None,
            "rhwpGraceHours": self.rhwp_grace_hours if self.known else None,
            "sources": dict(self.sources),
            "revision": self.revision,
            "nightlyApplyAllowed": self.nightly_apply_allowed,
            "hwpDirectConversion": self.direct_conversion_allowed("hwp"),
        }
        if include_actor:
            payload["updatedBy"] = self.updated_by
            payload["updatedAt"] = self.updated_at
        return payload


def unknown(reason: str) -> ConversionPolicy:
    return ConversionPolicy(
        state=STATE_UNKNOWN,
        reason=reason,
        sources={
            "appIds": SOURCE_UNKNOWN,
            "recheckLimit": SOURCE_UNKNOWN,
            "rhwpGraceHours": SOURCE_UNKNOWN,
        },
    )


def from_db(
    app_ids: Iterable[str] = (),
    *,
    recheck_limit: int = DEFAULT_RECHECK_LIMIT,
    rhwp_grace_hours: int = DEFAULT_RHWP_GRACE_HOURS,
    revision: int = 1,
    updated_by: str = "",
    updated_at: str = "",
) -> ConversionPolicy:
    """DB 에서 읽은(또는 시험이 DB 에서 읽은 것처럼 만든) 정책."""
    return ConversionPolicy(
        state=STATE_DB,
        app_ids=frozenset(app_ids),
        recheck_limit=recheck_limit,
        rhwp_grace_hours=rhwp_grace_hours,
        sources={"appIds": SOURCE_DB, "recheckLimit": SOURCE_DB, "rhwpGraceHours": SOURCE_DB},
        revision=revision,
        updated_by=updated_by,
        updated_at=updated_at,
    )


# --- 값 검증 ----------------------------------------------------------------


def _strict_int(value, *, name: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool):
        raise PolicyInvalid(f"{name}은(는) 정수여야 합니다.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        number = int(value)
    else:
        raise PolicyInvalid(f"{name}은(는) 정수여야 합니다.")
    low, high = bounds
    if not low <= number <= high:
        raise PolicyInvalid(f"{name}은(는) {low}~{high} 사이여야 합니다.")
    return number


def validate_app_ids(values) -> list[str]:
    """앱 ID 목록. 하나라도 틀리면 전체를 거절한다 — 틀린 값이 넓어지지 않게."""
    if values is None:
        raise PolicyInvalid("rhwp 앱 ID 목록이 없습니다. 위임을 끄려면 빈 목록을 보냅니다.")
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, list | tuple | frozenset | set):
        raise PolicyInvalid("rhwp 앱 ID 는 목록이어야 합니다.")
    cleaned = []
    for item in values:
        if not isinstance(item, str):
            raise PolicyInvalid("rhwp 앱 ID 는 문자열이어야 합니다.")
        text = item.strip().upper()
        if not text:
            continue
        if not _APP_ID.fullmatch(text):
            raise PolicyInvalid(f"Slack 앱 ID 형식이 아닙니다: {item.strip()} (예: A0123456789)")
        cleaned.append(text)
    unique = list(dict.fromkeys(cleaned))
    if len(unique) > MAX_APP_IDS:
        raise PolicyInvalid(f"rhwp 앱 ID 는 {MAX_APP_IDS}개까지 지정할 수 있습니다.")
    return unique


def validate(app_ids, recheck_limit, rhwp_grace_hours) -> tuple[list[str], int, int]:
    return (
        validate_app_ids(app_ids),
        _strict_int(recheck_limit, name="한 밤 소급 상한", bounds=RECHECK_LIMIT_RANGE),
        _strict_int(rhwp_grace_hours, name="rhwp 대기 시간", bounds=RHWP_GRACE_RANGE),
    )


def policy_from_row(row: Mapping) -> ConversionPolicy:
    """DB 행을 정책으로. 값이 틀리면 `unknown(invalid_value)` — 옛 설정으로 안 내려간다."""
    try:
        raw_ids = row.get("rhwp_app_ids")
        if not isinstance(raw_ids, list | tuple):
            raise PolicyInvalid("rhwp_app_ids 가 배열이 아닙니다.")
        ids = validate_app_ids(list(raw_ids))
        # DB 는 정규화된 값만 가져야 한다. 소문자·중복·빈 칸이 섞였으면 사람이 SQL 로
        # 손댄 것이다 — 고쳐 읽지 않고 모른다고 한다.
        if ids != list(raw_ids):
            raise PolicyInvalid("rhwp_app_ids 가 정규화돼 있지 않습니다.")
        # DB 열은 integer 다. 문자열·실수가 왔다면 행이 아니라 다른 무엇을 읽은 것이다.
        for key in ("recheck_limit", "rhwp_grace_hours"):
            if not isinstance(row.get(key), int):
                raise PolicyInvalid(f"{key} 가 정수가 아닙니다.")
        limit = _strict_int(
            row.get("recheck_limit"), name="recheck_limit", bounds=RECHECK_LIMIT_RANGE
        )
        grace = _strict_int(
            row.get("rhwp_grace_hours"), name="rhwp_grace_hours", bounds=RHWP_GRACE_RANGE
        )
        revision = row.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise PolicyInvalid("revision 이 틀렸습니다.")
    except PolicyInvalid as exc:
        logger.error("변환 정책 DB 값이 틀렸습니다 — 위임 상태를 모르는 것으로 둡니다: %s", exc)
        return unknown(INVALID_VALUE)
    updated_at = row.get("updated_at")
    return from_db(
        ids,
        recheck_limit=limit,
        rhwp_grace_hours=grace,
        revision=revision,
        updated_by=str(row.get("updated_by") or ""),
        updated_at=updated_at.isoformat()
        if isinstance(updated_at, datetime)
        else str(updated_at or ""),
    )


# --- 옛 설정 ----------------------------------------------------------------


def legacy_values(env: Mapping[str, str] | None = None) -> dict:
    """옛 설정의 **원문 값**과 출처. 콘솔 화면이 전환 전 값을 보여 줄 때 쓴다."""
    from .archive.converter_bots import _from_managed_files

    env = os.environ if env is None else env
    file_value = _from_managed_files()
    apps_raw, apps_source = (
        (file_value, SOURCE_LEGACY_FILE)
        if file_value
        else (env.get(LEGACY_APPS_ENV, ""), SOURCE_LEGACY_ENV if env.get(LEGACY_APPS_ENV) else "")
    )
    return {
        "appIds": apps_raw.strip(),
        "appIdsSource": apps_source,
        "recheckLimit": str(env.get(LEGACY_LIMIT_ENV, "")).strip(),
        "rhwpGraceHours": str(env.get(LEGACY_GRACE_ENV, "")).strip(),
    }


def _legacy_number(raw: str, *, name: str, bounds, default: int) -> tuple[int, str]:
    if not raw:
        return default, SOURCE_DEFAULT
    # 옛 grace 는 실수(`6.0`)로 적혀 있을 수 있다. 정수로 떨어질 때만 받는다.
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".", 1)[0]
    return _strict_int(raw, name=name, bounds=bounds), SOURCE_LEGACY_ENV


def legacy_policy(db_reason: str, env: Mapping[str, str] | None = None) -> ConversionPolicy:
    """DB 가 답하지 못할 때 옛 설정으로 만든 정책. 앱 ID 가 있어야만 `legacy` 다."""
    env = os.environ if env is None else env
    if str(env.get(LEGACY_SWITCH_ENV, "1")).strip().lower() in {"0", "false", "no", "off"}:
        return unknown(db_reason)
    values = legacy_values(env)
    try:
        ids = validate_app_ids(values["appIds"])
        limit, limit_source = _legacy_number(
            values["recheckLimit"],
            name=LEGACY_LIMIT_ENV,
            bounds=RECHECK_LIMIT_RANGE,
            default=DEFAULT_RECHECK_LIMIT,
        )
        grace, grace_source = _legacy_number(
            values["rhwpGraceHours"],
            name=LEGACY_GRACE_ENV,
            bounds=RHWP_GRACE_RANGE,
            default=DEFAULT_RHWP_GRACE_HOURS,
        )
    except PolicyInvalid as exc:
        logger.error("옛 변환 설정이 틀렸습니다 — 위임 상태를 모르는 것으로 둡니다: %s", exc)
        return unknown(db_reason)
    if not ids:
        # 옛 설정의 빈 값은 「꺼짐」 이었다. 이제는 그렇게 읽지 않는다.
        return unknown(db_reason)
    return ConversionPolicy(
        state=STATE_LEGACY,
        reason=db_reason,
        app_ids=frozenset(ids),
        recheck_limit=limit,
        rhwp_grace_hours=grace,
        sources={
            "appIds": values["appIdsSource"],
            "recheckLimit": limit_source,
            "rhwpGraceHours": grace_source,
        },
    )


# --- DB ---------------------------------------------------------------------


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise PolicyUnavailable(DB_UNCONFIGURED, "DATABASE_URL 이 없습니다")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 배포 환경에는 있다
        raise PolicyUnavailable(DB_UNAVAILABLE, "psycopg 가 없습니다") from exc
    try:
        return psycopg.connect(
            url,
            row_factory=psycopg.rows.dict_row,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise PolicyUnavailable(DB_UNAVAILABLE, type(exc).__name__) from exc


_SELECT = (
    f"SELECT rhwp_app_ids, recheck_limit, rhwp_grace_hours, revision, updated_by, updated_at "
    f"FROM {TABLE} WHERE id = 1"
)


def _is_undefined_table(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "42P01" or type(exc).__name__ == "UndefinedTable"


def fetch_row(conn=None) -> dict | None:
    """정본 행. 없으면 None. DB 문제는 `PolicyUnavailable`."""
    own = conn is None
    if own:
        conn = _connect()
    try:
        row = conn.execute(_SELECT).fetchone()
    except Exception as exc:
        if _is_undefined_table(exc):
            raise PolicyUnavailable(TABLE_MISSING, "표가 없습니다") from exc
        raise PolicyUnavailable(DB_UNAVAILABLE, type(exc).__name__) from exc
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                logger.debug("변환 정책 DB 연결을 닫지 못했습니다", exc_info=True)
    return dict(row) if row else None


def resolve(*, fetch=None, env: Mapping[str, str] | None = None) -> ConversionPolicy:
    """캐시 없이 지금 정책을 판정한다. 콘솔·readiness·야간 배치가 쓴다."""
    fetch = fetch or fetch_row
    try:
        row = fetch()
    except PolicyUnavailable as exc:
        reason = exc.reason if exc.reason in _FALLBACK_REASONS else DB_UNAVAILABLE
        return legacy_policy(reason, env)
    if row is None:
        return legacy_policy(ROW_MISSING, env)
    return policy_from_row(row)


# --- 캐시 -------------------------------------------------------------------

_cache: tuple[float, ConversionPolicy] | None = None
_override: ConversionPolicy | None = None
_last_logged: tuple | None = None


def _log(policy: ConversionPolicy) -> None:
    """상태가 바뀐 순간에만 남긴다. 파일마다 찍으면 로그가 그것으로 덮인다."""
    global _last_logged
    signature = policy.signature()
    if signature == _last_logged:
        return
    _last_logged = signature
    sources = ",".join(f"{key}={value}" for key, value in sorted(policy.sources.items()))
    message = "변환 정책 state=%s reason=%s delegation=%s revision=%s sources=%s"
    args = (policy.state, policy.reason or "-", policy.delegation, policy.revision, sources)
    if policy.state == STATE_DB:
        logger.info(message, *args)
    else:
        logger.warning(message + " — HWP 직접 변환과 야간 apply 를 막습니다", *args)


def current(*, now: float | None = None) -> ConversionPolicy:
    """수집·변환 경로가 쓰는 정책. 60초 캐시."""
    global _cache
    if _override is not None:
        return _override
    clock = time.monotonic() if now is None else now
    if _cache is not None and clock - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    policy = resolve()
    _log(policy)
    _cache = (clock, policy)
    return policy


def clear_cache() -> None:
    global _cache
    _cache = None


def set_override(policy: ConversionPolicy | None) -> None:
    """시험 전용. 운영 코드는 부르지 않는다."""
    global _override
    _override = policy
    clear_cache()


# --- 저장·이력 --------------------------------------------------------------


def save(
    *,
    app_ids,
    recheck_limit,
    rhwp_grace_hours,
    actor: str,
    expected_revision: int | None,
    reason: str = "",
    conn=None,
) -> ConversionPolicy:
    """한 트랜잭션에서 정본을 바꾸고 감사 행을 붙인다.

    `expected_revision` 은 화면이 읽었던 판이다(행이 없었으면 0). 다르면 다른 사람이
    먼저 바꾼 것이다 — 덮어쓰지 않고 `PolicyConflict`.
    """
    ids, limit, grace = validate(app_ids, recheck_limit, rhwp_grace_hours)
    actor = str(actor or "").strip()
    if not actor:
        raise PolicyInvalid("실행자가 없습니다.")
    note = str(reason or "").strip()[:500]
    expected = int(expected_revision or 0)
    own = conn is None
    conn = _connect() if own else conn
    try:
        with conn.transaction():
            row = conn.execute(
                f"SELECT rhwp_app_ids, recheck_limit, rhwp_grace_hours, revision "
                f"FROM {TABLE} WHERE id = 1 FOR UPDATE"
            ).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if current_revision != expected:
                raise PolicyConflict(
                    f"다른 변경이 먼저 저장됐습니다(현재 판 {current_revision}). 새로 읽고 다시 저장하세요."
                )
            previous = None
            if row:
                previous = {
                    "rhwpAppIds": list(row["rhwp_app_ids"] or []),
                    "recheckLimit": row["recheck_limit"],
                    "rhwpGraceHours": row["rhwp_grace_hours"],
                }
                saved = conn.execute(
                    f"UPDATE {TABLE} SET rhwp_app_ids = %(ids)s, recheck_limit = %(limit)s,"
                    f" rhwp_grace_hours = %(grace)s, revision = revision + 1,"
                    f" updated_by = %(actor)s, updated_at = now()"
                    f" WHERE id = 1 AND revision = %(expected)s"
                    f" RETURNING rhwp_app_ids, recheck_limit, rhwp_grace_hours, revision,"
                    f" updated_by, updated_at",
                    {
                        "ids": ids,
                        "limit": limit,
                        "grace": grace,
                        "actor": actor,
                        "expected": expected,
                    },
                ).fetchone()
            else:
                saved = conn.execute(
                    f"INSERT INTO {TABLE} (id, rhwp_app_ids, recheck_limit, rhwp_grace_hours,"
                    f" revision, updated_by) VALUES (1, %(ids)s, %(limit)s, %(grace)s, 1, %(actor)s)"
                    f" ON CONFLICT (id) DO NOTHING"
                    f" RETURNING rhwp_app_ids, recheck_limit, rhwp_grace_hours, revision,"
                    f" updated_by, updated_at",
                    {"ids": ids, "limit": limit, "grace": grace, "actor": actor},
                ).fetchone()
            if not saved:
                raise PolicyConflict("다른 변경이 먼저 저장됐습니다. 새로 읽고 다시 저장하세요.")
            from psycopg.types.json import Jsonb

            conn.execute(
                f"INSERT INTO {AUDIT_TABLE} (revision, rhwp_app_ids, recheck_limit,"
                f" rhwp_grace_hours, previous, updated_by, reason)"
                f" VALUES (%(revision)s, %(ids)s, %(limit)s, %(grace)s, %(previous)s,"
                f" %(actor)s, %(reason)s)",
                {
                    "revision": saved["revision"],
                    "ids": ids,
                    "limit": limit,
                    "grace": grace,
                    "previous": Jsonb(previous) if previous is not None else None,
                    "actor": actor,
                    "reason": note,
                },
            )
    finally:
        if own:
            conn.close()
    clear_cache()
    policy = policy_from_row(dict(saved))
    _log(policy)
    return policy


def history(limit: int = 20, conn=None) -> list[dict]:
    """감사 이력. 최신이 먼저. 값과 실행자·사유뿐이다."""
    own = conn is None
    conn = _connect() if own else conn
    try:
        rows = conn.execute(
            f"SELECT revision, rhwp_app_ids, recheck_limit, rhwp_grace_hours, previous,"
            f" updated_by, reason, recorded_at FROM {AUDIT_TABLE}"
            f" ORDER BY revision DESC LIMIT %(limit)s",
            {"limit": max(1, min(int(limit), 200))},
        ).fetchall()
    finally:
        if own:
            conn.close()
    return [
        {
            "revision": row["revision"],
            "appIds": list(row["rhwp_app_ids"] or []),
            "recheckLimit": row["recheck_limit"],
            "rhwpGraceHours": row["rhwp_grace_hours"],
            "previous": row["previous"],
            "updatedBy": row["updated_by"],
            "reason": row["reason"],
            "recordedAt": row["recorded_at"].isoformat()
            if isinstance(row["recorded_at"], datetime)
            else str(row["recorded_at"] or ""),
        }
        for row in rows
    ]


__all__ = [
    "ConversionPolicy",
    "PolicyConflict",
    "PolicyInvalid",
    "PolicyUnavailable",
    "clear_cache",
    "current",
    "from_db",
    "history",
    "legacy_policy",
    "legacy_values",
    "policy_from_row",
    "resolve",
    "save",
    "set_override",
    "unknown",
    "validate",
]
