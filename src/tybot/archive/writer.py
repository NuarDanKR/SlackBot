"""원문 수집기 — Slack 메시지를 MD 아카이브 '## 원문' 섹션에 append.

절대 원칙:
- 1겹: 봇 자신의 답변/요약은 저장하지 않는다(요약 재귀 금지).
- 원문 블록은 편집하지 않는다. append only. 정정은 `[정정]` 라인 추가로만.
- PII/제외 대상 키워드가 있으면 그 메시지는 아카이브하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..channels import parse as parse_channel
from ..lock import archive_write_lock
from .store import RAW_HEADING_RE, SchemaError, validate

KST = timezone(timedelta(hours=9))

# CLAUDE.md 원칙 5 — 아카이브 금지 대상
PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\d{6}\s*[-–]\s*[1-4]\d{6}"), "주민등록번호 형식"),
    (re.compile(r"등기부\s*등본"), "등기부등본"),
    (re.compile(r"계약자\s*명단"), "계약자 명단"),
    (re.compile(r"주민(등록)?번호"), "주민번호 언급"),
]


class ArchiveRefused(ValueError):
    """아카이브 금지 대상."""


@dataclass(frozen=True)
class IncomingMessage:
    """Slack 원문 1건."""

    ts: datetime
    speaker: str
    text: str
    is_bot: bool = False
    # 같은 논리 원문 묶음을 재수집할 때 쓰는 영속 키. 키 자체도 메시지 본문에
    # `[수집키:<key>]` 로 기록되어야 프로세스 재시작 뒤에도 중복을 판정할 수 있다.
    dedupe_key: str | None = None


def screen(text: str) -> str | None:
    """PII/제외 대상이면 사유 반환, 아니면 None."""
    for pat, name in PII_PATTERNS:
        if pat.search(text):
            return name
    return None


def _slugify(channel: str) -> str:
    """채널명 → 파일명. 원 채널명은 프론트매터에 보존한다."""
    s = channel.lstrip("#").strip()
    return re.sub(r"[^0-9A-Za-z가-힣()_\-]+", "_", s).strip("_") or "unnamed"


def doc_path(root: Path | str, workspace: str, channel: str) -> Path:
    return Path(root) / "channels" / _slugify(workspace) / f"{_slugify(channel)}.md"


def _new_doc(workspace: str, channel: str, visibility: str, acl: list[str]) -> str:
    acl_s = "[" + ", ".join(acl) + "]"
    # 채널명에서 조직 정보를 뽑아 남긴다. 조직명은 바뀌어도 코드는 유지되므로,
    # 조직 개편·워크스페이스 통합 뒤에도 이 문서가 어느 조직 것인지 추적할 수 있다.
    spec = parse_channel(channel)
    org_lines = ""
    if spec:
        org_lines = f"org_kind: {spec.kind}\n"
        if spec.org_code:
            org_lines += f"org_code: {spec.org_code}\n"
        org_lines += f"org_name: {spec.org_name}\n"
    return (
        "---\n"
        f"workspace: {workspace}\n"
        f'channel: "{channel}"\n'
        f"visibility: {visibility}\n"
        f"acl: {acl_s}\n"
        f"{org_lines}"
        "doc_count: 0\n"
        "last_ingested: \n"
        "---\n\n"
        "## 요약 (사람이 관리, 봇은 수정 금지)\n"
        "- \n\n"
        "## 원문 (자동 취합, 편집 금지)\n"
    )


def format_line(msg: IncomingMessage) -> str:
    ts = msg.ts.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    text = msg.text.replace("\n", " ").strip()
    return f"> [{ts}] {msg.speaker}: {text}"


@dataclass
class IngestResult:
    path: Path
    written: int
    skipped_bot: int
    refused: list[tuple[str, str]]  # (speaker, 사유)


def ingest(
    root: Path | str,
    *,
    workspace: str,
    channel: str,
    messages: list[IncomingMessage],
    visibility: str = "private",
    acl: list[str] | None = None,
) -> IngestResult:
    """원문 append. 형식 검사 실패 시 SchemaError 를 올리고 **아무것도 쓰지 않는다**(롤백).

    읽기-수정-쓰기 전체를 아카이브 쓰기 락 안에서 한다. 실시간 수집(`tybot.service`)과
    정기 백필(`tybot-collect.timer`)은 별도 프로세스라, 락이 없으면 같은 파일에 동시에 append 해
    라인이 섞이거나 `doc_count` 갱신이 유실될 수 있다.
    """
    with archive_write_lock(root):
        return _ingest_locked(
            root,
            workspace=workspace,
            channel=channel,
            messages=messages,
            visibility=visibility,
            acl=acl,
        )


def _ingest_locked(
    root: Path | str,
    *,
    workspace: str,
    channel: str,
    messages: list[IncomingMessage],
    visibility: str,
    acl: list[str] | None,
) -> IngestResult:
    path = doc_path(root, workspace, channel)
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    text = existing if existing is not None else _new_doc(workspace, channel, visibility, acl or [])

    seen = set(text.splitlines())
    existing_dedupe_keys = {
        m.dedupe_key
        for m in messages
        if m.dedupe_key and f"[수집키:{m.dedupe_key}]" in text
    }
    lines: list[str] = []
    refused: list[tuple[str, str]] = []
    skipped_bot = 0
    for m in messages:
        if m.is_bot:
            skipped_bot += 1  # 1겹: 봇 출력은 근거가 될 수 없다
            continue
        if m.dedupe_key in existing_dedupe_keys:
            continue
        reason = screen(m.text)
        if reason:
            refused.append((m.speaker, reason))
            continue
        line = format_line(m)
        if line in seen:
            continue  # 재수집 멱등
        seen.add(line)
        lines.append(line)

    if not lines:
        if existing is None:
            validate(text, path=str(path))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return IngestResult(path=path, written=0, skipped_bot=skipped_bot, refused=refused)

    body = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    stamp = datetime.now(KST).strftime("%Y-%m-%dT%H:%M+09:00")
    body = re.sub(r"^last_ingested:.*$", f"last_ingested: {stamp}", body, count=1, flags=re.MULTILINE)
    body = re.sub(
        r"^doc_count:\s*(\d+)\s*$",
        lambda m: f"doc_count: {int(m.group(1)) + len(lines)}",
        body,
        count=1,
        flags=re.MULTILINE,
    )

    # 게시 전 형식 검사 — 실패하면 디스크에 아무것도 남기지 않는다.
    validate(body, path=str(path))
    if not RAW_HEADING_RE.search(body):
        raise SchemaError(f"{path}: '## 원문' 섹션 유실")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return IngestResult(path=path, written=len(lines), skipped_bot=skipped_bot, refused=refused)
