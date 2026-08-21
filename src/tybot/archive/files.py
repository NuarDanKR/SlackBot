"""Slack 첨부 파일 처리.

원칙:
- 텍스트 파일(txt/md/csv/json)은 그대로 넣는다.
- **업무 문서(xlsx/docx/pptx/pdf/hwpx)는 변환해서 넣는다** — 실사용자가 올리는 건 이쪽이고,
  이걸 놓치면 맥락의 대부분이 빠진다. 변환 규칙은 `convert.py` 참조.
- 변환본은 `[첨부추출:파일명]` 으로 표시한다. 사람이 자동 변환본임을 알 수 있어야 한다.
- 스캔 PDF·구형 hwp·이미지·도면은 **여전히 미변환**이다. OCR 은 오류가 사실처럼 굳는 경로다.
- 다운로드에는 `files:read` 스코프와 봇 토큰 Bearer 헤더가 **둘 다** 필요하다.
  헤더가 없으면 파일 대신 로그인 HTML 이 200 으로 내려온다(조용한 고장) - 그래서 검증한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.request import Request, urlopen

from .convert import ConvertError, can_convert, convert

logger = logging.getLogger("tybot.files")

# 본문을 원문에 넣어도 되는 형식
TEXT_EXTS = {"txt", "md", "markdown", "csv", "tsv", "json", "yaml", "yml", "log", "ini", "conf"}
TEXT_MIMES = {"text/plain", "text/markdown", "text/csv", "application/json"}
# 변환도 안 되는 형식 - 목록만 남긴다
UNCONVERTED_EXTS = {
    "xls", "doc", "ppt", "hwp",  # 구형 바이너리 포맷
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff",  # 이미지(OCR 미도입)
    "dwg", "dxf",  # 도면
    "zip", "7z", "rar",  # 압축
}

MAX_TEXT_BYTES = 256 * 1024  # 원문에 넣는 텍스트 상한
MAX_DOC_BYTES = 20 * 1024 * 1024  # 변환 시도 상한(20MB)
MAX_TEXT_LINES = 200
DOWNLOAD_TIMEOUT = 20


class DownloadError(RuntimeError):
    """파일을 받지 못했거나 받은 것이 파일이 아니다."""


@dataclass(frozen=True)
class SlackFile:
    id: str
    name: str
    filetype: str
    size: int
    url_private_download: str | None
    mimetype: str = ""

    @classmethod
    def from_event(cls, f: dict) -> "SlackFile":
        return cls(
            id=str(f.get("id", "")),
            name=str(f.get("name") or f.get("title") or f.get("id") or "unnamed"),
            filetype=str(f.get("filetype") or "").lower(),
            size=int(f.get("size") or 0),
            url_private_download=f.get("url_private_download") or f.get("url_private"),
            mimetype=str(f.get("mimetype") or ""),
        )

    @property
    def is_text(self) -> bool:
        return self.filetype in TEXT_EXTS or self.mimetype in TEXT_MIMES

    @property
    def is_convertible(self) -> bool:
        return can_convert(self.filetype) and self.size <= MAX_DOC_BYTES

    def describe(self, state: str | None = None) -> str:
        """원문에 남기는 한 줄 설명. 본문을 못 넣는 경우에도 흔적은 남는다."""
        kb = max(1, self.size // 1024)
        if state is None:
            state = "본문 수집" if self.is_text else ("변환" if self.is_convertible else "미변환")
        return f"[첨부:{state}] {self.name} ({self.filetype or self.mimetype or '?'}, {kb}KB)"


def download_bytes(f: SlackFile, bot_token: str, limit: int) -> bytes:
    """원본 바이트를 가져온다. 로그인 HTML 이 오면 실패로 처리한다."""
    if not f.url_private_download:
        raise DownloadError(f"{f.name}: 다운로드 URL 없음")
    req = Request(f.url_private_download, headers={"Authorization": f"Bearer {bot_token}"})
    with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:  # noqa: S310 - Slack 고정 도메인
        raw = resp.read(limit + 1)
    if raw[:15].lstrip().lower().startswith(b"<!doctype html"):
        raise DownloadError(f"{f.name}: 로그인 페이지가 내려왔다 - files:read 스코프 확인")
    return raw


def download_text(f: SlackFile, bot_token: str) -> str:
    """텍스트 파일 본문을 가져온다. 실패는 예외로 올린다(조용히 넘기지 않는다)."""
    if not f.url_private_download:
        raise DownloadError(f"{f.name}: 다운로드 URL 없음")
    req = Request(f.url_private_download, headers={"Authorization": f"Bearer {bot_token}"})
    with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:  # noqa: S310 - Slack 고정 도메인
        ctype = (resp.headers.get("Content-Type") or "").lower()
        raw = resp.read(MAX_TEXT_BYTES + 1)
    # files:read 누락 시 Slack 은 로그인 페이지를 200 으로 돌려준다.
    if "text/html" in ctype and not f.is_text:
        raise DownloadError(f"{f.name}: HTML 응답 - files:read 스코프 또는 토큰 확인")
    if raw[:15].lstrip().lower().startswith(b"<!doctype html"):
        raise DownloadError(f"{f.name}: 로그인 페이지가 내려왔다 - files:read 스코프 확인")
    truncated = len(raw) > MAX_TEXT_BYTES
    text = raw[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > MAX_TEXT_LINES:
        lines = lines[:MAX_TEXT_LINES]
        truncated = True
    out = "\n".join(lines)
    if truncated:
        out += f"\n…(이하 생략, 원본 {max(1, f.size // 1024)}KB)"
    return out


def file_lines(files: list[dict], bot_token: str | None) -> tuple[list[str], list[str]]:
    """첨부 목록 → (원문에 넣을 줄들, 경고 메시지들).

    반환되는 줄은 수집기가 그대로 원문 라인 본문으로 쓴다.
    """
    lines: list[str] = []
    warnings: list[str] = []
    for raw in files or []:
        f = SlackFile.from_event(raw)

        if not (f.is_text or f.is_convertible):
            lines.append(f.describe("미변환"))
            continue
        if not bot_token:
            lines.append(f.describe("미변환"))
            warnings.append(f"{f.name}: 토큰이 없어 본문을 가져오지 못했습니다")
            continue

        try:
            if f.is_text:
                body = download_text(f, bot_token).splitlines()
                tag = "첨부본문"
            else:
                data = download_bytes(f, bot_token, MAX_DOC_BYTES)
                body = convert(f.filetype, data)
                tag = "첨부추출"
        except (DownloadError, ConvertError) as e:
            # 변환 못 한 것은 목록에 남긴다 - 색인의 「변환하지 못한 것」이 된다.
            lines.append(f.describe("미변환"))
            warnings.append(f"{f.name}: {e}")
            logger.warning("첨부 처리 실패 %s: %s", f.name, e)
            continue
        except Exception as e:  # noqa: BLE001 - 첨부 하나가 수집 전체를 막지 않는다
            lines.append(f.describe("미변환"))
            warnings.append(f"{f.name}: 예상치 못한 오류 {e.__class__.__name__}: {e}")
            logger.exception("첨부 처리 중 예외 %s", f.name)
            continue

        lines.append(f.describe("본문 수집" if f.is_text else "변환"))
        for ln in body:
            if ln.strip():
                lines.append(f"[{tag}:{f.name}] {ln.strip()}")
    return lines, warnings
