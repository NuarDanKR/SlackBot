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
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar

from .files import DownloadError, SlackFile, download_bytes

logger = logging.getLogger("tybot.canvas")


def _canvas_limit() -> int:
    """캔버스 줄 상한. **`0` 은 무제한.**"""
    from .convert import _limit

    return _limit("TYBOT_CANVAS_MAX_LINES")

# 예전 고정 제목. 이미 만들어진 Canvas 가 남아 있으므로 계속 본다.
GENERATED_TITLE = "TYBot 정식 답변"


def is_generated_canvas(title: str, canvas_id: str = "", body: str = "") -> bool:
    """봇이 만든 답변 Canvas 인가. **세 겹 중 하나만 맞아도 제외한다.**

    제목이 질문마다 달라지면서(설계 §4) 예전 `title.startswith(...)` 한 겹으로는
    우리 문서를 못 알아보게 됐다. 못 알아보는 순간 봇 답변이 다시 근거로 수집된다
    — 원칙 1(요약 재귀 금지)이 조용히 깨지는 자리다.

    1. 제목 접미사 ` · TYBot` — 사람이 제목을 고치면 사라질 수 있다
    2. 기록한 canvas_id — 디스크 유실·재설치로 사라질 수 있다
    3. 본문 첫 블록의 Disclaimer — 사람이 지울 수 있다

    한 겹씩은 다 뚫린다. 그래서 셋을 함께 본다.
    """
    from ..canvas_answer import DISCLAIMER_MARK, TITLE_SUFFIX, is_generated

    name = (title or "").strip()
    if name.startswith(GENERATED_TITLE) or name.endswith(TITLE_SUFFIX.strip()):
        return True
    if canvas_id and is_generated(canvas_id):
        return True
    # 본문은 **앞쪽만** 본다. Disclaimer 는 첫 블록이고, 뒤까지 뒤지면 우리 답변을
    # 인용한 사람 문서까지 제외된다.
    return DISCLAIMER_MARK in (body or "")[:2000]

MAX_CANVAS_BYTES = 1024 * 1024  # 캔버스 마크다운 상한(Slack 문서상 1 MiB)
# **자르지 않는다**(2026-09-14). 300 이던 것을 없앴다 — 캔버스에 정리해 둔 표와
# 목록이 300줄을 넘는 일은 흔하고, 잘린 뒤쪽은 아카이브에 영영 없어진다.
# `0` 은 무제한이고, 비상용으로만 환경변수를 남긴다(`convert._limit` 와 같은 규칙).
MAX_LINES = _canvas_limit()
TEXT_MIMES = frozenset({"text/markdown", "text/plain"})
HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})


@dataclass(frozen=True)
class CanvasCapture:
    """캔버스 조회 결과. dedupe_key 는 lines 전체에 같은 값을 적용한다."""

    lines: list[str]
    warnings: list[str]
    dedupe_key: str | None = None
    permalink: str = ""
    # 캔버스 본문이 **확실히** 가리키는 Slack 파일 ID(B-46).
    #
    # 캔버스에 정리해 둔 표·정산서가 근거에 안 들어가던 것을 고치기 위한 값이다.
    # 본문에는 파일 **이름**만 남아서 검색은 걸리고 내용은 없었다 — 가장 헷갈리는
    # 모양이다.
    #
    # **추측해서 넣지 않는다.** Slack 파일 URL 형태로 확실히 잡히는 것만 담는다.
    file_ids: tuple[str, ...] = ()


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
        self._pending_href = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in self.BLOCK_TAGS:
            self._flush()
        if tag == "a":
            # **링크 대상을 버리지 않는다**(B-46).
            #
            # 예전에는 속성을 통째로 무시해서 `<a href="…">기성금 정산표</a>` 가
            # `기성금 정산표` 로만 남았다. **라벨은 남고 대상은 사라진다** —
            # 검색에는 걸리는데 열 수가 없고, 사람은 봇이 자료를 못 읽는다고 읽는다.
            #
            # 대상을 **따라가지는 않는다.** 여기 남기는 것은 출처 표시와 사람
            # 확인용이다(설계: 외부 URL 본문 크롤링은 하지 않는다).
            self._pending_href = _safe_href(dict(attrs).get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "a":
            # 라벨 바로 뒤에 대상을 붙인다. 라벨이 없으면 대상만 남긴다 —
            # 빈 링크도 사람이 눌러 볼 수 있는 단서다.
            href = self._pending_href
            self._pending_href = ""
            if href:
                self._buf.append(f"<{href}>")
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


# 원문에 남겨도 되는 링크 구성표.
#
# `javascript:`·`data:` 는 남기지 않는다. 원문은 사람이 읽고 **눌러 보는** 자리라,
# 실행 가능한 구성표를 그대로 두면 아카이브가 그것을 나르는 통로가 된다.
SAFE_LINK_SCHEMES = ("http://", "https://", "mailto:", "slack://")
MAX_HREF_CHARS = 500


def _safe_href(value: object) -> str:
    """원문에 남길 링크. 남길 수 없으면 빈 문자열.

    **고쳐서 남기지 않는다** — 이상한 값을 다듬어 넣으면 그 줄이 원문인지 우리가
    만든 것인지 구별할 수 없게 된다. 통과하거나 버리거나 둘 중 하나다.
    """
    href = str(value or "").strip()
    if not href or len(href) > MAX_HREF_CHARS:
        return ""
    if any(ch in href for ch in ("<", ">", "\n", "\r", "|")):
        # 원문 줄 형식(`> [ts] 사람: 내용`)과 인용 문법을 깨뜨린다.
        return ""
    lowered = href.lower()
    return href if lowered.startswith(SAFE_LINK_SCHEMES) else ""


# 캔버스 본문 안의 Slack 파일 링크. **두 형태만 받는다.**
#
# 맨몸 `F0ABCDE` 토큰은 받지 않는다 — 엑셀 셀 주소나 문서 번호일 수 있고, 그것을
# 파일로 읽으면 없는 파일을 찾느라 실패가 쌓인다. 확실한 것만 받고 나머지는
# 「미확인」 으로 둔다.
SLACK_FILE_REFS = (
    # https://<team>.slack.com/files/<user>/<FID>/<name>
    re.compile(r"slack\.com/files/[^/\s]+/(F[A-Z0-9]{6,})", re.IGNORECASE),
    # https://files.slack.com/files-pri/<T...>-<FID>/<name>
    re.compile(r"files\.slack\.com/files-[a-z]+/T[A-Z0-9]+-(F[A-Z0-9]{6,})", re.IGNORECASE),
)


def file_refs(lines: list[str], *, exclude: str = "") -> tuple[str, ...]:
    """캔버스 줄에서 Slack 파일 ID 를 뽑는다. 순서는 처음 나온 차례.

    `exclude` 는 캔버스 자신의 파일 ID 다. 캔버스도 파일이라 본문에 자기 링크가
    있으면 **자기를 첨부로 수집**하게 된다 — 그러면 같은 내용이 두 벌 들어간다.
    """
    found: list[str] = []
    for line in lines:
        for pattern in SLACK_FILE_REFS:
            for match in pattern.finditer(line):
                file_id = match.group(1).upper()
                if file_id != (exclude or "").upper() and file_id not in found:
                    found.append(file_id)
    return tuple(found)


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
    if MAX_LINES and len(lines) > MAX_LINES:
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
    if title.startswith(GENERATED_TITLE):
        logger.info("봇이 만든 답변 Canvas 수집 제외 %s", file_id)
        return CanvasCapture([], [])
    try:
        raw = download_bytes(f, bot_token, MAX_CANVAS_BYTES)
        lines = _to_lines(raw, f.mimetype)
    except Exception as e:  # noqa: BLE001 - 추측해서 넣느니 미변환으로 남긴다
        logger.warning("캔버스 수집 실패 %s: %s", title, e)
        return _unconverted(
            channel_id, title, f"{file_id}:download", f"캔버스 {title}: {e}"
        )

    # 본문을 받고 나서 **한 번 더** 본다. 제목을 사람이 고쳤거나 기록이 유실된
    # 경우, Disclaimer 가 마지막 방어선이다(원칙 1).
    if is_generated_canvas(title, file_id, "\n".join(lines[:20])):
        logger.info("봇이 만든 답변 Canvas 수집 제외(본문 표식) %s", file_id)
        return CanvasCapture([], [])

    payload = "\n".join(lines).encode()
    key = _key(channel_id, file_id, payload)
    out = [f"[캔버스:수집] {title} [수집키:{key}]"]
    out += [f"[캔버스본문:{title}] {ln}" for ln in lines]
    return CanvasCapture(out, [], key, f.permalink or "", file_refs(lines, exclude=file_id))
