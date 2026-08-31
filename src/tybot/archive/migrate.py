"""아카이브 v1 평면 파일을 v2 일자별 구조로 비파괴 복사한다."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import writer
from .store import ArchiveDoc, SchemaError, load_doc

KST = timezone(timedelta(hours=9))


@dataclass
class MigrationReport:
    dry_run: bool
    legacy_files: int = 0
    eligible_files: int = 0
    migrated_messages: int = 0
    unresolved_channels: list[str] = field(default_factory=list)
    blocked_files: list[str] = field(default_factory=list)
    broken_files: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def load_channel_map(path: Path | str) -> dict[str, dict[str, str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("채널 매핑 최상위는 워크스페이스 객체여야 합니다")
    out: dict[str, dict[str, str]] = {}
    for workspace, channels in data.items():
        if not isinstance(channels, dict):
            raise ValueError(f"{workspace}: 채널 매핑은 객체여야 합니다")
        out[str(workspace)] = {str(name): str(cid) for name, cid in channels.items()}
    return out


def migrate_archive(
    root: Path | str,
    channel_map: dict[str, dict[str, str]],
    *,
    apply: bool = False,
) -> MigrationReport:
    """v1을 v2로 복사한다. ``apply=False``가 기본이며 원본은 항상 보존한다."""
    archive = Path(root)
    legacy = sorted((archive / "channels").glob("*/*.md"))
    report = MigrationReport(dry_run=not apply, legacy_files=len(legacy))

    planned: list[tuple[Path, ArchiveDoc, str, list[writer.IncomingMessage]]] = []
    for path in legacy:
        rel = path.relative_to(archive).as_posix()
        try:
            doc = load_doc(path)
        except (OSError, SchemaError, ValueError) as exc:
            report.broken_files.append(f"{rel}: {exc}")
            continue

        channel_id = channel_map.get(doc.workspace, {}).get(doc.channel)
        if not channel_id:
            report.unresolved_channels.append(f"{doc.workspace}:{doc.channel}")
            continue

        blocked = [line for line in doc.raw_lines if writer.screen(line.text)]
        if blocked:
            report.blocked_files.append(f"{rel}: PII/제외 대상 {len(blocked)}줄")
            continue

        messages = [
            writer.IncomingMessage(
                ts=datetime.strptime(line.ts, "%Y-%m-%d %H:%M").replace(tzinfo=KST),
                speaker=line.speaker,
                text=line.text,
            )
            for line in doc.raw_lines
        ]
        report.eligible_files += 1
        planned.append((path, doc, channel_id, messages))

    if not apply:
        report.migrated_messages = sum(len(item[3]) for item in planned)
        return report
    if report.unresolved_channels or report.blocked_files or report.broken_files:
        return report

    for path, doc, channel_id, messages in planned:
        result = writer.ingest(
            archive,
            workspace=doc.workspace,
            channel=doc.channel,
            channel_id=channel_id,
            messages=messages,
            visibility=doc.visibility,
            acl=sorted(doc.acl),
            share_with=sorted(doc.share_with),
            imported_from=path.relative_to(archive).as_posix(),
        )
        report.migrated_messages += result.written
    return report
