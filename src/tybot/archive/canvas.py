"""Slack 채널 캔버스 수집.

## 확인된 것 (Slack 공식 문서)
- 채널 캔버스의 ID 는 `conversations.info` 의 채널 `properties` 에서 얻는다.
  (`conversations.canvases.create` 문서: "You can retrieve the ID of an existing channel
  canvas by checking the channel properties via the conversations.info method.")
- 캔버스는 **파일**(`F...`)로 존재한다.

## 확인되지 않은 것 — 그래서 방어적으로 짠다
캔버스 **본문을 그대로 돌려주는 전용 조회 메서드**는 공식 문서에서 확인하지 못했다
(`canvases.sections.lookup` 은 섹션 id 만 준다). 그래서 파일 다운로드 경로로 읽되,
응답이 예상과 다르면 **추측하지 않고 '미변환'으로 기록하고 경고를 남긴다**.

이 판단을 코드에 박아두는 이유: 캔버스 본문을 잘못 파싱해 원문에 넣으면 되돌릴 수 없다.
안 넣는 쪽이 항상 안전하다.

## 갱신 처리
캔버스는 계속 편집된다. 원문은 append only 이므로 **수집 시점의 스냅샷**을 남긴다.
내용이 그대로면 같은 줄이 되어 멱등 처리로 걸러지고, 바뀌면 새 스냅샷이 덧붙는다.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar

from .files import DownloadError, SlackFile, download_bytes

logger = logging.getLogger("tybot.canvas")

MAX_CANVAS_BYTES = 1024 * 1024  # 캔버스 마크다운 상한(Slack 문서상 1 MiB)
MAX_LINES = 300
TEXT_MIMES = frozenset({"text/markdown", "text/plain"})
HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})


@dataclass(frozen=True)
class CanvasCapture:
    """캔버스 조회 결과. dedupe_key 는 lines 전체에 같은 값을 적용한다."""

    lines: list[str]
    warnings: list[str]
    dedupe_key: str | None = None


def _key(channel_id: str, stage: str, payload: bytes = b"") -> str:
    digest = hashlib.sha256(b"\0".join((channel_id.encode(), stage.encode(), payload))).hexdigest()
    return f"canvas:{digest[:24]}"


def _unconverted(channel_id: str, label: str, stage: str, warning: str) -> CanvasCapture:
    key = _key(channel_id, stage)
    return CanvasCapture(
        lines=[f"[캔버스:미변환] {label} [수집키:{key}]"],
        warnings=[warning],
        dedupe_key=key,
    )


class _TextExtractor(HTMLParser):
    """HTML 로 내려오는 경우를 대비한 최소 텍스트 추출기.

    태그를 지우고 텍스트만 줄 단위로 모은다. 구조 해석·추론은 하지 않는다.
    """

    BLOCK_TAGS: ClassVar[frozenset[str]] = frozenset(
        {"p", "div", "li", "h1", "h2", "h3", "h4", "br", "tr"}
    )

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._buf: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in self.BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag in self.BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self._buf.append(data.strip())

    def _flush(self) -> None:
        if self._buf:
            self.parts.append(" ".join(self._buf))
            self._buf = []

    def close(self) -> None:
        super().close()
        self._flush()


def canvas_file_id(client, channel_id: str) -> str | None:
    """채널에 붙은 캔버스의 파일 ID. 없으면 None."""
    info = client.conversations_info(channel=channel_id)
    props = (info.get("channel") or {}).get("properties") or {}
    canvas = props.get("canvas") or {}
    return canvas.get("file_id") or canvas.get("document_id") or None


def _to_lines(raw: bytes, mimetype: str) -> list[str]:
    """받은 바이트를 줄 목록으로. 형식을 못 알아보면 예외."""
    if len(raw) > MAX_CANVAS_BYTES:
        raise DownloadError(f"캔버스 본문이 {MAX_CANVAS_BYTES}바이트 상한을 넘었다")
    mime = (mimetype or "").split(";", 1)[0].strip().lower()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise DownloadError("캔버스 본문이 UTF-8 텍스트가 아니다") from e
    if any(ord(ch) < 32 and ch not in "\t\r\n" for ch in text):
        raise DownloadError("캔버스 본문에 바이너리 제어문자가 있다")
    head = text.lstrip()[:200].lower()
    if mime in HTML_MIMES or head.startswith(("<!doctype html", "<html")):
        p = _TextExtractor()
        p.feed(text)
        p.close()
        lines = p.parts
    elif mime in TEXT_MIMES:
        lines = [ln.strip() for ln in text.splitlines()]
    else:
        raise DownloadError(f"지원하지 않는 캔버스 형식: {mime or '미상'}")
    lines = [ln for ln in lines if ln]
    if not lines:
        raise DownloadError("캔버스 본문이 비어 있거나 형식을 알아보지 못했다")
    if len(lines) > MAX_LINES:
        lines = [*lines[:MAX_LINES], f"…(이하 생략, 총 {len(lines)}줄)"]
    return lines


def canvas_lines(client, channel_id: str, bot_token: str | None) -> CanvasCapture:
    """채널 캔버스 → 원문 줄·경고·영속 중복 키.

    캔버스가 없으면 둘 다 빈 목록이다(정상).
    """
    try:
        file_id = canvas_file_id(client, channel_id)
    except Exception as e:  # noqa: BLE001 - 조회 실패를 캔버스 없음으로 숨기지 않는다
        logger.warning("캔버스 ID 조회 실패 %s: %s", channel_id, e)
        return _unconverted(
            channel_id,
            channel_id,
            "lookup",
            f"캔버스 {channel_id}: conversations.info 실패 - {e}",
        )
    if not file_id:
        return CanvasCapture([], [])
    if not bot_token:
        return _unconverted(
            channel_id, file_id, f"{file_id}:token", "캔버스: 토큰이 없어 본문을 가져오지 못했습니다"
        )

    try:
        info = client.files_info(file=file_id)
    except Exception as e:  # noqa: BLE001
        return _unconverted(
            channel_id,
            file_id,
            f"{file_id}:files-info",
            f"캔버스 {file_id}: files.info 실패 - {e}",
        )

    f = SlackFile.from_event(info.get("file") or {})
    title = f.name or file_id
    try:
        raw = download_bytes(f, bot_token, MAX_CANVAS_BYTES)
        lines = _to_lines(raw, f.mimetype)
    except Exception as e:  # noqa: BLE001 - 추측해서 넣느니 미변환으로 남긴다
        logger.warning("캔버스 수집 실패 %s: %s", title, e)
        return _unconverted(
            channel_id, title, f"{file_id}:download", f"캔버스 {title}: {e}"
        )

    payload = "\n".join(lines).encode()
    key = _key(channel_id, file_id, payload)
    out = [f"[캔버스:수집] {title} [수집키:{key}]"]
    out += [f"[캔버스본문:{title}] {ln}" for ln in lines]
    return CanvasCapture(out, [], key)
