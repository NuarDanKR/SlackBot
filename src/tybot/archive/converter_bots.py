"""지정된 변환 앱이 올린 PDF 만 원문 첨부로 받는다 (2026-11-09 오너 결정).

## 왜 예외를 두나

HWP 는 rhwp 앱이 채널에서 PDF 로 바꿔 올린다. TYBot 은 원칙 1 에 따라 봇·앱이
올린 메시지를 모든 수집 경로에서 버려 왔고, 그래서 그 PDF 도 버려졌다. 그 상태로
TYBot 의 HWP 변환을 끄면 HWP 내용이 아카이브에도 답변에도 들어오지 않는다.

원칙 1 이 막으려는 것은 **봇이 자기 출력을 되먹여** 편향되는 것이다. rhwp PDF 는
사람이 올린 원본 문서를 렌더링한 사본이지, 봇이 쓴 문장이 아니다. 그래서 오너
결정으로 **지정된 변환 앱의 PDF 파일만** 받는다.

## 좁게 받는다

- **앱 ID 로만 가린다.** Slack 에 배포된 앱은 워크스페이스가 달라도 앱 ID 가 같다.
  봇 이름·사용자 이름은 누구나 비슷하게 만들 수 있으므로 보지 않는다
- **파일만 받는다.** 그 메시지의 글(「변환했습니다」 같은 안내문)은 버린다 — 글은
  봇 출력이다
- **PDF 만 받는다.** 같은 앱이 다른 형식을 올려도 받지 않는다
- 받은 PDF 는 사람 메시지와 **같은 수집 경로**를 탄다. PII 검사·ACL·중복 방지가
  그대로 걸린다

목록이 비어 있으면 아무것도 바뀌지 않는다 — 예외는 켜야 생긴다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

#: 콘솔 환경 설정에서 관리하는 키. 쉼표로 여러 앱 ID.
CONVERTER_APPS_ENV = "TYBOT_CONVERTER_APP_IDS"

#: 변환 앱이 올린 파일 중 받는 형식.
CONVERTED_TYPES = frozenset({"pdf"})

#: 변환 앱이 맡는 원본 형식. 앱이 켜져 있으면 TYBot 은 이 형식을 직접 변환하지 않는다.
DELEGATED_TYPES = frozenset({"hwp", "hwpx"})

#: 변환을 넘긴 원본의 사유 코드. 「변환 실패」 와 구별해야 사람이 할 일이 다르다.
DELEGATED_CODE = "delegated_to_converter_app"

#: 정리한 메시지에 남기는 표식. `bot_id` 를 지운 뒤에도 「사람이 올린 파일」 로
#: 확인됐다고 적히지 않게 한다(`AttachmentOrigin.human_upload_confirmed`).
CONVERTER_MARK = "_tybot_converter_app"

_APP_ID = re.compile(r"^A[A-Z0-9]{6,}$")


#: 콘솔 설정 파일의 운영 위치. 수집기(Archiver)는 다른 env 파일로 뜨므로 자기 상태
#: 폴더만 보면 콘솔이 쓴 파일을 못 찾을 수 있다.
PRODUCTION_MANAGED_ENV = Path("/var/lib/tybot/config/console-managed.env")

#: 설정 파일을 다시 읽는 간격. 콘솔에서 바꾸면 재시작 없이 이 안에 반영된다.
CACHE_SECONDS = 60.0

_cache: tuple[float, frozenset[str]] | None = None


def _from_managed_files() -> str:
    """콘솔이 쓴 설정 파일에서 값을 읽는다. 없으면 빈 문자열."""
    from ..managed_env import managed_env_path

    for path in (managed_env_path(), PRODUCTION_MANAGED_ENV):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == CONVERTER_APPS_ENV:
                return value.strip().strip('"').strip("'")
    return ""


def converter_app_ids(*, now: float | None = None) -> frozenset[str]:
    """지정된 변환 앱 ID. 형식이 틀린 값은 버린다 — 틀린 값이 아무 앱이나 받게 두지 않는다.

    **콘솔 설정 파일을 직접 읽는다.** 콘솔 저장은 TYBot 본체에만 재시작을 요청하고,
    수집기(Archiver)는 재시작되지 않는다. 프로세스 환경변수만 보면 수집기는 콘솔에서
    바꾼 값을 영영 모른다. 파일 값이 있으면 그것이, 없으면 환경변수가 쓰인다.
    """
    import time

    global _cache
    clock = time.monotonic() if now is None else now
    if _cache is not None and clock - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    raw = _from_managed_files() or os.getenv(CONVERTER_APPS_ENV, "")
    ids = frozenset(
        value for value in (part.strip().upper() for part in raw.split(","))
        if _APP_ID.fullmatch(value)
    )
    _cache = (clock, ids)
    return ids


def clear_cache() -> None:
    """시험과 콘솔 저장 직후에 쓴다."""
    global _cache
    _cache = None


def parse_app_ids(raw: str) -> list[str]:
    """콘솔 입력 검증. 틀린 값이 하나라도 있으면 `ValueError`."""
    values = [part.strip().upper() for part in (raw or "").split(",") if part.strip()]
    bad = [value for value in values if not _APP_ID.fullmatch(value)]
    if bad:
        raise ValueError(f"Slack 앱 ID 형식이 아닙니다: {', '.join(bad)} (예: A0123456789)")
    return list(dict.fromkeys(values))


def conversion_delegated() -> bool:
    """HWP 변환을 변환 앱에 넘겼는가. 앱이 지정돼 있을 때만 참이다."""
    return bool(converter_app_ids())


def app_id_of(message: dict) -> str:
    profile = message.get("bot_profile") or {}
    return str(message.get("app_id") or profile.get("app_id") or "").strip().upper()


def _filetype(item: dict) -> str:
    name = str(item.get("name") or item.get("title") or "")
    suffix = Path(name).suffix.lstrip(".").lower()
    return suffix or str(item.get("filetype") or "").lower()


def converted_document_message(message: dict) -> dict | None:
    """지정된 변환 앱이 올린 PDF 라면 **파일만 남긴** 메시지를, 아니면 None.

    돌려주는 메시지에는 본문(`text`)이 없고 `bot_id`·`app_id` 도 없다 — 뒤의 수집
    경로가 사람 메시지와 같은 규칙으로 다루게 하기 위해서다. 대신 발화자는 그 앱의
    사용자 ID 로 남아, 원문 줄에는 앱 이름이 발화자로 찍힌다.
    """
    allowed = converter_app_ids()
    if not allowed or app_id_of(message) not in allowed:
        return None
    subtype = str(message.get("subtype") or "")
    if subtype not in ("", "file_share", "bot_message"):
        return None
    files = [
        item for item in message.get("files") or []
        if isinstance(item, dict) and _filetype(item) in CONVERTED_TYPES
    ]
    if not files or not message.get("ts"):
        return None
    user = str(message.get("user") or message.get("bot_id") or "").strip()
    if not user:
        return None
    kept = {
        "ts": message["ts"],
        "user": user,
        "text": "",
        "files": files,
        CONVERTER_MARK: app_id_of(message),
    }
    for key in ("thread_ts", "channel", "channel_type", "event_ts"):
        if message.get(key):
            kept[key] = message[key]
    return kept


__all__ = [
    "CONVERTED_TYPES",
    "CONVERTER_APPS_ENV",
    "CONVERTER_MARK",
    "DELEGATED_CODE",
    "DELEGATED_TYPES",
    "app_id_of",
    "clear_cache",
    "conversion_delegated",
    "converted_document_message",
    "converter_app_ids",
    "parse_app_ids",
]
