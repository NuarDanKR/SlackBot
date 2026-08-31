"""Slack 사람 메시지를 아카이브 v2의 일자별 원문 MD에 저장한다.

신규 경로는 ``workspaces/<workspace>/channels/<channel-id>__<name>/raw/YYYY-MM-DD.md``다.
봇 출력은 저장하지 않고, 원문은 기존 라인을 보존한 채 뒤에만 추가한다.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..channels import parse as parse_channel
from ..lock import archive_write_lock
from .store import RAW_HEADING_RE, SchemaError, validate

KST = timezone(timedelta(hours=9))

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
    dedupe_key: str | None = None


def screen(text: str) -> str | None:
    """PII/제외 대상이면 사유 반환, 아니면 None."""
    for pat, name in PII_PATTERNS:
        if pat.search(text):
            return name
    return None


def _slugify(value: str) -> str:
    s = value.lstrip("#").strip()
    return re.sub(r"[^0-9A-Za-z가-힣()_\-]+", "_", s).strip("_") or "unnamed"


def _stable_channel_id(channel: str, channel_id: str | None) -> str:
    if channel_id:
        cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", channel_id).strip("_")
        if cleaned:
            return cleaned
    digest = hashlib.sha256(channel.encode("utf-8")).hexdigest()[:12]
    return f"legacy-{digest}"


def channel_dir(
    root: Path | str,
    workspace: str,
    channel: str,
    channel_id: str | None = None,
) -> Path:
    """채널 ID가 같은 기존 디렉터리를 재사용해 이름 변경에도 경로를 유지한다."""
    base = Path(root) / "workspaces" / _slugify(workspace) / "channels"
    stable_id = _stable_channel_id(channel, channel_id)
    existing = sorted(base.glob(f"{stable_id}__*")) if base.is_dir() else []
    if existing:
        return existing[0]
    return base / f"{stable_id}__{_slugify(channel)}"


def doc_path(
    root: Path | str,
    workspace: str,
    channel: str,
    *,
    channel_id: str | None = None,
    day: date | None = None,
) -> Path:
    day = day or datetime.now(KST).date()
    return channel_dir(root, workspace, channel, channel_id) / "raw" / f"{day.isoformat()}.md"


def _new_doc(
    workspace: str,
    channel: str,
    channel_id: str,
    source_date: date,
    visibility: str,
    acl: list[str],
    share_with: list[str],
    imported_from: str | None,
) -> str:
    acl_s = "[" + ", ".join(acl) + "]"
    share_s = "[" + ", ".join(share_with) + "]"
    spec = parse_channel(channel)
    org_lines = ""
    if spec:
        org_lines = f"org_kind: {spec.kind}\n"
        if spec.org_code:
            org_lines += f"org_code: {spec.org_code}\n"
        org_lines += f"org_name: {spec.org_name}\n"
    import_line = f'imported_from: "{imported_from}"\n' if imported_from else ""
    return (
        "---\n"
        "schema_version: 2\n"
        f"workspace: {workspace}\n"
        f'channel: "{channel}"\n'
        f"channel_id: {channel_id}\n"
        f"source_date: {source_date.isoformat()}\n"
        f"visibility: {visibility}\n"
        f"acl: {acl_s}\n"
        f"share_with: {share_s}\n"
        f"{import_line}"
        f"{org_lines}"
        "doc_count: 0\n"
        "last_ingested: \n"
        "---\n\n"
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
    refused: list[tuple[str, str]]
    paths: tuple[Path, ...] = field(default_factory=tuple)


def ingest(
    root: Path | str,
    *,
    workspace: str,
    channel: str,
    messages: list[IncomingMessage],
    channel_id: str | None = None,
    visibility: str = "private",
    acl: list[str] | None = None,
    share_with: list[str] | None = None,
    imported_from: str | None = None,
) -> IngestResult:
    """원문을 KST 날짜별 파일에 추가한다. 검증 실패 시 어떤 파일도 쓰지 않는다."""
    with archive_write_lock(root):
        return _ingest_locked(
            root,
            workspace=workspace,
            channel=channel,
            channel_id=channel_id,
            messages=messages,
            visibility=visibility,
            acl=acl,
            share_with=share_with,
            imported_from=imported_from,
        )


def _ingest_locked(
    root: Path | str,
    *,
    workspace: str,
    channel: str,
    channel_id: str | None,
    messages: list[IncomingMessage],
    visibility: str,
    acl: list[str] | None,
    share_with: list[str] | None,
    imported_from: str | None,
) -> IngestResult:
    stable_id = _stable_channel_id(channel, channel_id)
    directory = channel_dir(root, workspace, channel, stable_id)
    raw_dir = directory / "raw"
    existing_texts: dict[Path, str] = {
        path: path.read_text(encoding="utf-8") for path in sorted(raw_dir.glob("*.md"))
    }
    all_existing = "\n".join(existing_texts.values())
    seen = set(all_existing.splitlines())
    existing_dedupe_keys = {
        m.dedupe_key
        for m in messages
        if m.dedupe_key and f"[수집키:{m.dedupe_key}]" in all_existing
    }

    grouped: dict[date, list[str]] = {}
    refused: list[tuple[str, str]] = []
    skipped_bot = 0
    for message in messages:
        if message.is_bot:
            skipped_bot += 1
            continue
        if message.dedupe_key in existing_dedupe_keys:
            continue
        reason = screen(message.text)
        if reason:
            refused.append((message.speaker, reason))
            continue
        line = format_line(message)
        if line in seen:
            continue
        seen.add(line)
        grouped.setdefault(message.ts.astimezone(KST).date(), []).append(line)

    fallback_day = messages[0].ts.astimezone(KST).date() if messages else datetime.now(KST).date()
    fallback_path = doc_path(root, workspace, channel, channel_id=stable_id, day=fallback_day)
    if not grouped:
        return IngestResult(fallback_path, 0, skipped_bot, refused)

    stamp = datetime.now(KST).strftime("%Y-%m-%dT%H:%M+09:00")
    pending: dict[Path, str] = {}
    for day, lines in sorted(grouped.items()):
        path = doc_path(root, workspace, channel, channel_id=stable_id, day=day)
        text = existing_texts.get(path) or _new_doc(
            workspace,
            channel,
            stable_id,
            day,
            visibility,
            acl or [],
            share_with or [],
            imported_from,
        )
        body = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
        body = re.sub(
            r"^last_ingested:.*$", f"last_ingested: {stamp}", body, count=1, flags=re.MULTILINE
        )
        # 콜백 대신 직접 자른다. 루프 변수를 참조하는 람다는 늦게 불릴 때
        # 값이 어긋날 수 있어(ruff B023) 애초에 만들지 않는다.
        counted = re.search(r"^doc_count:\s*(\d+)\s*$", body, flags=re.MULTILINE)
        if counted:
            body = (
                body[: counted.start()]
                + f"doc_count: {int(counted.group(1)) + len(lines)}"
                + body[counted.end() :]
            )
        validate(body, path=str(path))
        if not RAW_HEADING_RE.search(body):
            raise SchemaError(f"{path}: '## 원문' 섹션 유실")
        pending[path] = body

    for path, body in pending.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    paths = tuple(pending)
    return IngestResult(paths[-1], sum(map(len, grouped.values())), skipped_bot, refused, paths)
