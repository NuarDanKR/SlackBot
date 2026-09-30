"""기존 TYBot 아카이브를 **새 채널별 구조로 옮기는 계획.**

설계: `docs/design/legacy-archive-migration.md`
목적지 규칙: `shadow_paths` · 원문 형식: `store.validate`

## 이 모듈이 하는 일과 안 하는 일

**한다** — 원본을 읽고, 어디로 갈지 정하고, 무엇이 막히는지 세고, 보고서를 만든다.

**안 한다** — 원본을 고치지 않는다. 목적지에 쓰지도 않는다. 이 모듈에는 파일을
만드는 코드가 없고, 시험이 그것을 고정한다(`test_legacy_migrate.py`).

옮기는 실행은 계획과 보고서를 사람이 본 뒤의 별도 단계다. 계획을 세우는 코드와
옮기는 코드를 같은 함수에 두면, 「보기만 하려던」 실행이 옮겨 버리는 날이 온다.

## 좌표가 없는 것은 합치지 않는다

소급 수집이 이미 가져온 메시지와 옛 아카이브의 같은 메시지를 합쳐야 한다. 대조는
**`(workspace, channel_id, message_ts)` 세 값**으로만 한다.

옛 줄에는 `message_ts` 가 없는 것이 많다. 그런 줄은 **어느 것과도 짝짓지 않는다** —
시각과 화자가 비슷하다고 같은 메시지로 보는 순간, 다른 메시지를 지우거나 같은
메시지를 둘로 남긴다. 둘 다 오류를 내지 않는다. 그래서 「모른다」 를 그대로 들고
간다(`uncoordinated`).

## 채널 ID 가 없는 문서는 막는다

새 구조의 디렉터리 이름은 Slack 채널 ID 다(`shadow_paths.channel_root`). v1 문서에는
`channel_id` 가 없고, `writer._stable_channel_id()` 가 채널명 해시로 `legacy-<12자리>`
를 만든다. 그 값은 새 구조의 정규식(`legacy-[a-f0-9]{16}`)을 **통과하지 못한다.**

맞춰 주지 않는다. 채널 ID 를 지어내면 그 채널의 과거 자료가 실제 채널과 다른
자리에 쌓이고, 나중에 누구도 둘이 같은 채널이었다는 것을 모른다. 대신 막고,
보고서가 그 목록을 든다 — 사람이 실제 ID 를 찾아 주면 그때 옮긴다.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .shadow_paths import ShadowPathError, archive_dir
from .store import SchemaError, load_doc, parse_frontmatter, validate

log = logging.getLogger("tybot.archive.legacy_migrate")

#: 새 구조가 받아들이는 채널 ID. `shadow_paths.channel_root` 와 **같은 규칙**이어야
#: 한다. 다르면 여기서 통과한 문서가 목적지에서 거절된다.
CHANNEL_ID = re.compile(r"(?:[CG][A-Z0-9]{8,}|legacy-[a-f0-9]{16})")

#: 원문 줄에 남은 첨부 표시와 그 파일 ID.
ATTACHMENT_LINE = re.compile(r"\[첨부(?:추출)?:")
ATTACHMENT_ID = re.compile(r"·\s*id:([A-Za-z0-9_-]+)")


class MigrationRefused(RuntimeError):
    """이 계획으로는 옮기지 않는다. **사유를 사람 말로 들고 있다.**"""


@dataclass(frozen=True)
class SourceDoc:
    """옛 아카이브의 원문 문서 하나. **본문은 들지 않는다.**

    보고서에 본문이 들어가면 그 보고서가 또 하나의 아카이브가 되고, 권한이 붙지
    않은 사본이 생긴다(절대 원칙 3).
    """

    path: Path
    workspace: str
    channel: str
    channel_id: str
    visibility: str
    acl: frozenset[str]
    share_with: frozenset[str]
    schema_version: int
    raw_sha256: str
    line_count: int
    #: `message_ts` 가 있는 줄 수. 소급 결과와 대조할 수 있는 것들이다.
    coordinated: int
    #: 좌표가 없는 줄 수. **어느 것과도 짝짓지 않는다.**
    uncoordinated: int
    attachment_lines: int
    attachment_ids: frozenset[str]


@dataclass(frozen=True)
class Placement:
    """문서 하나가 어디로 가나. 못 가면 왜 못 가나."""

    doc: SourceDoc
    destination: Path | None
    verdict: str
    reason: str = ""
    #: 소급이 이미 가져온 줄 수(옮길 때 빼야 하는 것). 좌표가 있는 줄만 센다.
    duplicate_lines: int = 0

    @property
    def blocked(self) -> bool:
        return self.verdict == "blocked"


@dataclass
class Report:
    """계획과 검증 결과. **보고서가 결론이고, 옮기는 것은 다음 단계다.**"""

    source: Path
    destination: Path
    placements: list[Placement] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    #: DM 문서 수. 새 구조는 채널 단위라 여기서 옮기지 않는다. **열지도 않는다.**
    dm_documents: int = 0
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    @property
    def planned(self) -> list[Placement]:
        return [item for item in self.placements if item.verdict == "plan"]

    @property
    def blocked(self) -> list[Placement]:
        return [item for item in self.placements if item.blocked]

    def content(self) -> dict:
        """**내용** — 무엇이 몇 줄이나 옮겨지나."""
        lines = sum(item.doc.line_count for item in self.planned)
        duplicate = sum(item.duplicate_lines for item in self.planned)
        return {
            "documents": len(self.planned),
            "blockedDocuments": len(self.blocked),
            "rawLines": lines,
            "duplicateLines": duplicate,
            "newLines": lines - duplicate,
            "unreadable": len(self.unreadable),
        }

    def permissions(self) -> dict:
        """**권한** — 옮기면서 넓어지는 것이 없나."""
        public = sum(1 for item in self.planned if item.doc.visibility == "public")
        empty_acl = sum(1 for item in self.planned if not item.doc.acl)
        shared = sum(1 for item in self.planned if item.doc.share_with)
        return {
            "public": public,
            "private": len(self.planned) - public,
            "emptyAcl": empty_acl,
            "crossWorkspaceShared": shared,
            "dmDocumentsLeftBehind": self.dm_documents,
        }

    def provenance(self) -> dict:
        """**출처** — 좌표가 있나. 없는 것은 합치지 않는다."""
        coordinated = sum(item.doc.coordinated for item in self.planned)
        uncoordinated = sum(item.doc.uncoordinated for item in self.planned)
        no_id = sum(1 for item in self.blocked if "채널 ID" in item.reason)
        return {
            "coordinatedLines": coordinated,
            "uncoordinatedLines": uncoordinated,
            "documentsWithoutChannelId": no_id,
            "schemaV1": sum(1 for item in self.planned if item.doc.schema_version == 1),
            "schemaV2": sum(1 for item in self.planned if item.doc.schema_version == 2),
            "note": (
                "좌표가 없는 줄은 소급 결과와 대조하지 않습니다. 같은 메시지가 두 벌"
                " 남을 수 있고, 그것을 추정으로 합치지 않습니다."
            ),
        }

    def attachments(self) -> dict:
        """**첨부** — 원문이 가리키는 파일이 실제로 있나."""
        referenced = sum(item.doc.attachment_lines for item in self.planned)
        ids: set[str] = set()
        for item in self.planned:
            ids |= item.doc.attachment_ids
        return {
            "referenceLines": referenced,
            "identifiedFiles": len(ids),
            # 좌표(`id:`)가 없는 첨부 줄은 이름으로만 이어져 있다. 옮긴 뒤 이름이
            # 겹치면 잘못 이어지므로, 이 수가 0 이 아니면 사람이 한 번 봐야 한다.
            "linesWithoutFileId": max(0, referenced - len(ids)),
        }

    def as_json(self) -> dict:
        return {
            "source": str(self.source),
            "destination": str(self.destination),
            "blocked": bool(self.refusals),
            "refusals": list(self.refusals),
            "content": self.content(),
            "permissions": self.permissions(),
            "provenance": self.provenance(),
            "attachments": self.attachments(),
            "blockedDocuments": [
                {"path": str(item.doc.path), "reason": item.reason}
                for item in self.blocked
            ],
            "unreadable": [
                {"path": path, "reason": reason} for path, reason in self.unreadable
            ],
            "note": (
                "이 보고서는 계획입니다. 원본을 고치지 않았고 목적지에 쓰지도"
                " 않았습니다."
            ),
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_documents(root: Path | str) -> list[Path]:
    """옛 아카이브의 **채널 원문** 파일. DM 은 목록에 넣지 않는다.

    DM 은 그 사람 한 명의 기록이고 새 구조는 채널 단위다. 목록에 넣으면 이 도구가
    그 파일을 열게 되고, 그 순간 보고서가 개인 기록을 읽은 셈이 된다.
    """
    base = Path(root)
    found = sorted((base / "workspaces").glob("*/channels/*/raw/*.md"))
    found += sorted((base / "channels").glob("*/*.md"))
    return found


def dm_document_count(root: Path | str) -> int:
    """DM 문서 수. **파일을 열지 않고 센다.**"""
    return len(sorted((Path(root) / "workspaces").glob("*/dm/*/raw/*.md")))


def read_source(path: Path) -> SourceDoc:
    """문서 하나를 읽어 계획에 필요한 값만 남긴다. 본문은 버린다."""
    text = path.read_text(encoding="utf-8")
    validate(text, path=str(path))
    fm = parse_frontmatter(text)
    doc = load_doc(path)
    ids = {
        match.group(1)
        for line in doc.raw_lines
        for match in [ATTACHMENT_ID.search(line.text)]
        if match
    }
    coordinated = sum(1 for line in doc.raw_lines if line.message_ts)
    return SourceDoc(
        path=path,
        workspace=doc.workspace,
        channel=doc.channel,
        channel_id=str(fm.get("channel_id") or ""),
        visibility=doc.visibility,
        acl=doc.acl,
        share_with=doc.share_with,
        schema_version=doc.schema_version,
        raw_sha256=_sha256("\n".join(line.text for line in doc.raw_lines)),
        line_count=len(doc.raw_lines),
        coordinated=coordinated,
        uncoordinated=len(doc.raw_lines) - coordinated,
        attachment_lines=sum(
            1 for line in doc.raw_lines if ATTACHMENT_LINE.search(line.text)
        ),
        attachment_ids=frozenset(ids),
    )


def collected_coordinates(root: Path | str) -> set[tuple[str, str, str]]:
    """소급·그림자가 **이미 가져온** 메시지 좌표.

    `(workspace, channel_id, message_ts)` 세 값만 본다. 셋 다 있어야 한 건으로
    센다 — 둘로 맞추면 다른 채널의 같은 시각이 같은 메시지가 된다.
    """
    seen: set[tuple[str, str, str]] = set()
    base = Path(root)
    candidates = sorted(base.glob("*/*/archive/raw/*.md"))
    candidates += sorted((base / "workspaces").glob("*/channels/*/raw/*.md"))
    for path in candidates:
        try:
            doc = load_doc(path)
        except (SchemaError, OSError) as exc:
            log.warning("이미 수집된 문서를 읽지 못해 좌표에서 뺀다 %s: %s", path, exc)
            continue
        if not doc.channel_id:
            continue
        for line in doc.raw_lines:
            if line.message_ts:
                seen.add((doc.workspace, doc.channel_id, line.message_ts))
    return seen


def check_roots(source: Path, destination: Path, live_archive: Path) -> list[str]:
    """옮겨도 되는 자리인가. **운영 원본과 섞이는 자리는 거부한다.**"""
    refusals: list[str] = []
    src, dest, live = Path(source), Path(destination), Path(live_archive)
    for name, value in (("원본", src), ("목적지", dest), ("운영 archive", live)):
        if not value.is_absolute():
            refusals.append(f"{name} 경로는 절대경로여야 합니다: {value}")
    if refusals:
        return refusals
    if dest == src or dest.is_relative_to(src) or src.is_relative_to(dest):
        refusals.append(
            f"목적지 {dest} 가 원본 {src} 와 겹칩니다. 옛 자료 위에 새 구조를 쓰면"
            " 어느 쪽이 원본인지 알 수 없게 됩니다."
        )
    if dest == live or dest.is_relative_to(live) or live.is_relative_to(dest):
        refusals.append(
            f"목적지 {dest} 가 운영 archive {live} 와 겹칩니다. 이 도구는 운영"
            " 아카이브에 쓰지 않습니다."
        )
    return refusals


def _place(doc: SourceDoc, destination: Path) -> tuple[Path | None, str, str]:
    if not doc.channel_id:
        return None, "blocked", (
            "채널 ID 가 없습니다(v1 문서). 새 구조의 디렉터리 이름이 채널 ID 라"
            " 자리를 정할 수 없습니다. 실제 Slack 채널 ID 를 찾아 주세요."
        )
    if not CHANNEL_ID.fullmatch(doc.channel_id):
        return None, "blocked", (
            f"채널 ID 가 새 구조의 규칙과 다릅니다: {doc.channel_id}."
            " 지어내지 않습니다."
        )
    if not doc.acl and doc.visibility != "public":
        return None, "blocked", (
            "비공개 문서인데 ACL 이 비어 있습니다. 그대로 옮기면 권한이 조용히"
            " 넓어집니다(절대 원칙 3)."
        )
    try:
        target = archive_dir(destination, doc.workspace, doc.channel_id, doc.channel)
    except ShadowPathError as exc:
        return None, "blocked", f"목적지 경로를 만들 수 없습니다: {exc}"
    return target / "raw" / doc.path.name, "plan", ""


def plan(
    source: Path | str,
    destination: Path | str,
    *,
    live_archive: Path | str,
    collected: set[tuple[str, str, str]] | None = None,
) -> Report:
    """무엇을 어디로 옮길지 세고 **아무것도 바꾸지 않는다.**

    `collected` 는 소급·그림자가 이미 가져온 좌표다. 주지 않으면 중복을 0 으로
    세는 것이 아니라 **대조하지 않았다**는 뜻이고, 보고서의 `duplicateLines` 가
    0 으로 나온다. 실제로 옮기기 전에는 반드시 주어야 한다.
    """
    src, dest = Path(source), Path(destination)
    report = Report(source=src, destination=dest)
    report.refusals.extend(check_roots(src, dest, Path(live_archive)))
    report.dm_documents = dm_document_count(src)
    index = collected or set()

    for path in source_documents(src):
        try:
            doc = read_source(path)
        except (SchemaError, OSError, ValueError) as exc:
            # 읽지 못한 문서를 **건너뛰고 끝내지 않는다.** 목록에 남겨야 사람이
            # 「몇 건이 빠졌나」 를 안다. 조용히 빠지면 옮긴 뒤에도 모른다.
            report.unreadable.append((str(path), f"{type(exc).__name__}: {exc}"))
            continue
        target, verdict, reason = _place(doc, dest)
        duplicates = 0
        if verdict == "plan" and index:
            duplicates = _count_duplicates(path, doc, index)
        report.placements.append(Placement(
            doc=doc, destination=target, verdict=verdict, reason=reason,
            duplicate_lines=duplicates,
        ))
    return report


def _count_duplicates(
    path: Path, doc: SourceDoc, index: set[tuple[str, str, str]],
) -> int:
    """소급이 이미 가져온 줄 수. **좌표가 있는 줄만 센다.**

    좌표가 없는 줄은 후보에도 올리지 않는다. 비슷해 보인다고 합치면 다른 메시지를
    지우거나 같은 메시지를 둘로 남기고, 둘 다 오류를 내지 않는다.
    """
    loaded = load_doc(path)
    return sum(
        1
        for line in loaded.raw_lines
        if line.message_ts
        and (doc.workspace, doc.channel_id, line.message_ts) in index
    )
