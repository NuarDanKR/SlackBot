"""서비스 상태 파일 읽기 — **허용한 칸만 읽는다.**

## 왜 allowlist 인가
상태 파일은 프금팀 Hermes 가 쓴다. 우리가 쓰는 파일이 아니다. 계약(§7.4, 인계서 §8)에는
토큰·질문·답변·문서 본문·비공개 채널 이름을 넣지 않기로 돼 있지만, **계약은 코드가
아니다.** 실수로 하나 들어가면 우리 화면이 그것을 그대로 사내에 띄운다.

그래서 읽는 쪽에서 막는다. 아래 표에 있는 칸만 꺼내고 나머지는 **통째로 버린다.**
모르는 칸이 늘어도 화면은 조용하다. 금지어를 찾아 지우는 방식(denylist)은 쓰지 않는다 —
새로운 이름의 칸이 하나 생길 때마다 뚫리고, 뚫린 것을 알 방법이 없다.

## 없는 것과 틀린 것을 갈라 본다
화면이 답해야 하는 질문은 「지금 괜찮은가」 하나가 아니다.

| 판정 | 뜻 | 사람이 할 일 |
|---|---|---|
| `unavailable` | 상태 파일이 없다 | 아직 안 붙었거나 경로가 다르다 |
| `unreadable` | 있는데 못 읽는다(권한·깨짐) | 권한이나 기록 코드를 본다 |
| `incomplete` | 읽었는데 계약에 있는 칸이 없다 | 프금팀에 그 칸을 요청한다 |
| `stale` | 칸은 다 있는데 기록이 낡았다 | 프로세스가 멈췄는지 본다 |
| `ok` | 최신이고 칸이 다 있다 | 없음 |

「빈 화면」 하나로 뭉뜽그리면 넷이 다 똑같이 보인다. 그러면 아직 안 붙은 것과 어제
죽은 것을 구분할 수 없고, 사람은 둘 다 「원래 그런가 보다」 로 읽는다.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tybot_pf.health")

STATE_FILENAME = "health.json"

UNAVAILABLE = "unavailable"
UNREADABLE = "unreadable"
INCOMPLETE = "incomplete"
STALE = "stale"
OK = "ok"

# ---------------------------------------------------------------------------
# 읽는 칸 — 이 표에 없는 것은 화면에 절대 오르지 않는다
# ---------------------------------------------------------------------------
#
# 값: (화면에 쓰는 이름, 종류). 종류는 값을 어떻게 다듬을지만 정한다.
#   text  짧은 문자열. 길면 자른다(로그·메시지가 통째로 들어오는 것을 막는다)
#   time  ISO 시각 문자열
#   int   정수
#   money 소수
_TEXT, _TIME, _INT, _MONEY = "text", "time", "int", "money"

FIELDS: dict[str, tuple[str, str]] = {
    # 무엇이 돌고 있나
    "source_commit": ("sourceCommit", _TEXT),
    "started_at": ("startedAt", _TIME),
    "generated_at": ("generatedAt", _TIME),
    # Slack
    "last_slack_connect": ("lastSlackConnect", _TIME),
    "last_ingest": ("lastIngest", _TIME),
    # 자료 Git
    "last_archive_pull": ("lastArchivePull", _TIME),
    "last_archive_push": ("lastArchivePush", _TIME),
    "unpushed_commits": ("unpushedCommits", _INT),
    "archive_conflict": ("archiveConflict", _TEXT),
    # 배치
    "last_digest": ("lastDigest", _TIME),
    "last_model_call": ("lastModelCall", _TIME),
    # 비용 (당일)
    "calls_today": ("callsToday", _INT),
    "tokens_today": ("tokensToday", _INT),
    "cost_usd_today": ("costUsdToday", _MONEY),
}

# 계약이 지켜졌는지 판정할 때 **반드시 있어야 하는** 칸. 이게 없으면 화면이 무엇도
# 단정할 수 없다 — 「정상」 이라고 칠할 근거가 없다.
REQUIRED = ("source_commit", "started_at", "generated_at")

# 정규화된 오류 코드(인계서 §8). 이 목록에 없는 코드는 `unknown` 으로 접는다 —
# Hermes 가 예외 메시지를 코드 자리에 넣어도 화면에 본문이 오르지 않는다.
ERROR_CODES = (
    "slack",
    "anthropic",
    "git_pull",
    "git_push",
    "conflict",
    "archive_invalid",
    "config_invalid",
)
UNKNOWN_ERROR = "unknown"

# 한 값의 최대 길이. 커밋 해시·ISO 시각·짧은 코드만 들어오는 자리다. 더 길면
# 무언가 잘못 들어온 것이므로 자른다.
MAX_TEXT = 120
MAX_ERRORS = 20


@dataclass
class ServiceHealth:
    """한 서비스의 상태. **파일에 있던 것 중 허용된 칸만 담긴다.**"""

    service: str
    status: str
    # 판정의 근거. 화면이 「왜 이렇게 보이나」 를 말할 수 있어야 한다.
    reason: str = ""
    path: str = ""
    fields: dict[str, object] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # 파일이 마지막으로 바뀐 시각. `generated_at` 과 다를 수 있다 — 다르면 그것도
    # 정보다(기록은 했는데 값이 안 바뀌는 상태).
    file_modified_at: str = ""
    age_seconds: int | None = None

    def to_json(self) -> dict:
        return {
            "service": self.service,
            "status": self.status,
            "reason": self.reason,
            "path": self.path,
            "fields": self.fields,
            "errors": self.errors,
            "missing": self.missing,
            "fileModifiedAt": self.file_modified_at,
            "ageSeconds": self.age_seconds,
        }


def _text(value: object) -> str:
    return str(value).strip()[:MAX_TEXT]


def _coerce(value: object, kind: str) -> object | None:
    if value is None:
        return None
    if kind == _INT:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if kind == _MONEY:
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return None
    # text·time 둘 다 짧은 문자열로 다룬다. 시각 형식은 여기서 강제하지 않는다 —
    # 형식이 어긋나면 화면이 그대로 보여 주고, 그게 계약 위반의 증거가 된다.
    text = _text(value)
    return text or None


def _errors(raw: object) -> list[dict[str, str]]:
    """오류 목록. **코드는 접고 본문은 버린다.**

    Hermes 가 `{"code": "...", "at": "..."}` 로 준다는 계약이다. 메시지 칸이 와도
    읽지 않는다 — 예외 메시지에는 경로·식별자·때로는 질문 조각이 섞인다.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[:MAX_ERRORS]:
        if not isinstance(item, dict):
            continue
        code = _text(item.get("code")).lower()
        out.append({
            "code": code if code in ERROR_CODES else UNKNOWN_ERROR,
            "at": _text(item.get("at")),
        })
    return out


def _age_seconds(stamp: str, *, now: _dt.datetime) -> int | None:
    try:
        when = _dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=now.tzinfo)
    return int((now - when).total_seconds())


def read(
    service: str,
    state_dir: Path | str,
    *,
    stale_after: int = 900,
    now: _dt.datetime | None = None,
) -> ServiceHealth:
    """상태 파일 한 장을 읽는다. **예외를 던지지 않는다.**

    이 함수가 던지면 화면 전체가 오류가 된다. 그런데 「상태를 못 읽는다」 는 것 자체가
    화면이 보여 줘야 할 상태다 — 못 읽는다는 사실을 보여 주는 편이 항상 낫다.
    """
    now = now or _dt.datetime.now(_dt.UTC)
    path = Path(state_dir) / STATE_FILENAME
    result = ServiceHealth(service=service, status=UNAVAILABLE, path=str(path))

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        result.reason = "상태 파일이 아직 없습니다."
        return result
    except OSError as exc:
        result.status = UNREADABLE
        # 예외 문자열을 그대로 싣지 않는다 — 경로·계정이 섞여 나온다.
        result.reason = f"상태 파일을 읽지 못했습니다({type(exc).__name__})."
        logger.warning("PF 상태 파일 읽기 실패 service=%s path=%s: %s", service, path, exc)
        return result

    with contextlib.suppress(OSError):
        result.file_modified_at = (
            _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.UTC)
            .isoformat(timespec="seconds")
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        result.status = UNREADABLE
        result.reason = "상태 파일이 JSON 형식이 아닙니다."
        return result
    if not isinstance(data, dict):
        result.status = UNREADABLE
        result.reason = "상태 파일의 최상위가 객체가 아닙니다."
        return result

    # --- 여기서부터가 격리선이다. 아래 줄 밖의 값은 어디로도 나가지 않는다. ---
    for key, (name, kind) in FIELDS.items():
        value = _coerce(data.get(key), kind)
        if value is not None:
            result.fields[name] = value
    result.errors = _errors(data.get("errors"))
    result.missing = [key for key in REQUIRED if key not in data]

    if result.missing:
        result.status = INCOMPLETE
        result.reason = (
            "상태 파일에 계약 항목이 없습니다: " + ", ".join(result.missing)
        )
        return result

    generated = str(data.get("generated_at") or "")
    result.age_seconds = _age_seconds(generated, now=now)
    if result.age_seconds is None:
        result.status = INCOMPLETE
        result.reason = "generated_at 을 시각으로 읽지 못했습니다."
        return result
    if result.age_seconds > stale_after:
        result.status = STALE
        result.reason = (
            f"마지막 기록이 {result.age_seconds // 60}분 전입니다"
            f"(기준 {stale_after // 60}분)."
        )
        return result

    result.status = OK
    return result
