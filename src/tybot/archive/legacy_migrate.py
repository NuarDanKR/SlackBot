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

#: 첨부 **참조** 줄. 파일 하나당 한 줄이고 「이 파일이 있었다」 를 말한다.
ATTACHMENT_REFERENCE = re.compile(r"\[첨부:")

#: 첨부에서 **뽑아낸 본문** 줄. `files.py` 가 추출 텍스트를 한 줄씩 이 표시와 함께
#: raw 에 넣는다. 그래서 첨부 하나가 raw 에 수백 줄을 만든다.
#:
#: 이 줄들은 **파생 자료**다. 원본 바이트가 `objects/` 에 있으면 다시 만들 수 있다.
ATTACHMENT_BODY = re.compile(r"\[첨부(?:추출|본문):")

#: 수집기가 남긴 edit/delete 이력 줄. 사람이 그 자리에서 한 말과 같은 것으로
#: 세지 않는다 — 같은 내용이 여러 판으로 남아 있는 것이다.
REVISION_LINE = re.compile(r"\[(?:수정 전|수정 후|삭제 전|삭제됨)\]")

#: 사람이 아닌 화자. `writer.IncomingMessage(speaker=...)` 가 쓰는 값이다.
SYNTHETIC_SPEAKERS = frozenset({"캔버스", "캔버스 첨부"})

#: 이 줄과 첨부 정본을 잇는 유일한 좌표. 옛 줄에는 없다.
ATTACHMENT_ID = re.compile(r"·\s*id:([A-Za-z0-9_-]+)")


def line_kind(line) -> str:
    """줄 하나의 종류. **「나머지」 를 사람 대화라고 단정하지 않는다.**

    전에는 「알려진 첨부 표시가 아니면 사람 대화」 였다. 그러면 모르는 형식이
    사람 대화 수에 섞이고, 섞인 줄은 아무 표시도 남기지 않는다. 알아보는 종류를
    먼저 빼고 남는 것을 `residual` 로 부른다 — 이름이 「모른다」 를 담는다.
    """
    text = line.text
    if ATTACHMENT_REFERENCE.search(text):
        return "attachment_reference"
    if ATTACHMENT_BODY.search(text):
        return "attachment_body"
    if REVISION_LINE.search(text):
        return "revision"
    if line.speaker in SYNTHETIC_SPEAKERS:
        return "canvas"
    return "residual"


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
    #: 알아보는 종류를 뺀 **나머지** 줄. 사람 대화로 보이지만 단정하지 않는다.
    residual_lines: int
    #: 그중 좌표가 있는 줄.
    residual_coordinated: int
    #: 첨부 참조 줄(파일 하나당 하나).
    attachment_reference_lines: int
    #: 첨부에서 뽑아낸 본문 줄. **파생 자료다.**
    attachment_body_lines: int
    #: edit/delete 이력 줄.
    revision_lines: int
    #: 캔버스 스냅샷 줄.
    canvas_lines: int
    attachment_ids: frozenset[str]


@dataclass(frozen=True)
class Placement:
    """문서 하나가 어디로 가나. 못 가면 왜 못 가나."""

    doc: SourceDoc
    destination: Path | None
    verdict: str
    reason: str = ""
    #: 새 자료에 **같은 좌표의 메시지가 있는** 옛 줄 수. 좌표는 메시지 단위라
    #: 어느 줄이 실제로 넘어왔는지는 말해 주지 않는다.
    duplicate_lines: int = 0
    #: 그중 나머지(사람 대화로 보이는) 줄.
    residual_duplicate_lines: int = 0
    #: 좌표가 맞은 **메시지들**. 수가 아니라 집합인 이유가 있다 — 한 메시지가
    #: 여러 날짜 파일에 걸친다. 수집기는 **수정 시각**으로 날짜 파일을 고르면서
    #: **원문 메시지의 좌표**를 그대로 남기므로(`_ingest_revision` 의
    #: `source_ts=message_ts`), 9월 1일 메시지를 9월 5일에 고치면 같은 좌표가 두
    #: 파일에 있다. 문서별 수를 더하면 한 메시지가 두 건이 된다.
    matched_keys: frozenset[tuple[str, str, str]] = frozenset()

    @property
    def matched_messages(self) -> int:
        """이 문서 하나가 본 메시지 수. 보고서 합계는 집합을 합쳐 센다."""
        return len(self.matched_keys)

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
        """**내용** — 무엇이 몇 줄이나 있나. 갈래로 나눠 센다.

        합쳐서 「원문 N줄」 로 말하면 오해를 부른다. 실측(2026-09-30 운영 아카이브)
        에서 raw 23만 줄 중 첨부에서 뽑아낸 본문이 23만 줄이었다. 그 수를 「옮길
        원문」 으로 읽으면 규모도 위험도 잘못 잡는다.
        """
        lines = sum(item.doc.line_count for item in self.planned)
        residual = sum(item.doc.residual_lines for item in self.planned)
        reference = sum(item.doc.attachment_reference_lines for item in self.planned)
        body = sum(item.doc.attachment_body_lines for item in self.planned)
        revision = sum(item.doc.revision_lines for item in self.planned)
        canvas = sum(item.doc.canvas_lines for item in self.planned)
        duplicate = sum(item.duplicate_lines for item in self.planned)
        residual_duplicate = sum(
            item.residual_duplicate_lines for item in self.planned
        )
        # 문서별 수를 더하지 않는다. 한 메시지가 여러 날짜 파일에 걸치기 때문이다.
        matched_keys: set[tuple[str, str, str]] = set()
        for item in self.planned:
            matched_keys |= item.matched_keys
        return {
            # **경로를 배정할 수 있는** 문서 수다. 안전하게 옮길 수 있다는 뜻이
            # 아니다 — 첨부 본문이 든 옛 raw 도 경로는 배정된다.
            "pathAssignableDocuments": len(self.planned),
            "documents": len(self.planned),
            "blockedDocuments": len(self.blocked),
            "unreadable": len(self.unreadable),

            # --- raw 전체. **이관 가능량이 아니다** -------------------------
            "rawLines": lines,

            # --- 갈래별. 알아보는 것을 빼고 남는 것이 residual --------------
            "residualLines": residual,
            "attachmentReferenceLines": reference,
            "attachmentBodyLines": body,
            "revisionLines": revision,
            "canvasLines": canvas,

            # --- 좌표 대조. **메시지 단위다** -------------------------------
            "matchedMessages": len(matched_keys),
            "rawLinesInMatchedMessages": duplicate,
            "residualLinesInMatchedMessages": residual_duplicate,

            # --- 추정치 -----------------------------------------------------
            "estimate": True,
            "residualNewLinesEstimate": residual - residual_duplicate,
            "estimateCaveat": (
                "좌표(`message_ts`)는 **메시지 단위**입니다. writer 가 한 메시지의"
                " 사람 발언과 첨부 줄에 같은 좌표를 붙이므로, 새 자료에 그 메시지의"
                " 첨부 줄만 들어와 있어도 좌표는 맞습니다. 그래서"
                " residualNewLinesEstimate 는 **줄 종류까지 대조한 값이 아니라"
                " 좌표 기준 추정치**이고, 실제보다 작게 나올 수 있습니다."
            ),

            "note": (
                "residualLines 는 알아보는 종류(첨부 참조·첨부 본문·수정 이력·"
                "캔버스)를 뺀 **나머지**입니다. 사람 대화로 보이지만 모르는 형식이"
                " 섞여 있을 수 있어 그렇게 단정하지 않습니다."
                " 새 채널별 구조(per-channel-v1)는 raw 파일의 내용을 검사하지"
                " 않습니다 — 대신 shadow 루트에 workspaces/ 가 있으면 기동을"
                " 거부하고(`requires a new shadow root without legacy workspaces`),"
                " 첨부 분리를 강제해 **앞으로 쓰는** raw 에 본문이 들어가지 않게"
                " 합니다. 즉 옛 raw 를 그 루트에 넣어도 실행 중에 걸리지 않으므로,"
                " 넣지 않는 것은 사람이 지켜야 하는 약속입니다."
            ),
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
        residual = sum(item.doc.residual_lines for item in self.planned)
        residual_coordinated = sum(
            item.doc.residual_coordinated for item in self.planned
        )
        no_id = sum(1 for item in self.blocked if "채널 ID" in item.reason)
        return {
            "coordinatedLines": coordinated,
            "uncoordinatedLines": uncoordinated,
            # 나머지 줄 기준. 첨부 본문을 빼고 센다 — 합쳐 세면 대조 가능성이
            # 실제보다 높아 보인다.
            "residualLines": residual,
            "residualCoordinatedLines": residual_coordinated,
            "residualUncoordinatedLines": residual - residual_coordinated,
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
        referenced = sum(
            item.doc.attachment_reference_lines for item in self.planned
        )
        body = sum(item.doc.attachment_body_lines for item in self.planned)
        ids: set[str] = set()
        for item in self.planned:
            ids |= item.doc.attachment_ids
        return {
            "referenceLines": referenced,
            "bodyLines": body,
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
    kinds = [line_kind(line) for line in doc.raw_lines]
    residual = [
        line for line, kind in zip(doc.raw_lines, kinds, strict=True)
        if kind == "residual"
    ]
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
        residual_lines=len(residual),
        residual_coordinated=sum(1 for line in residual if line.message_ts),
        attachment_reference_lines=kinds.count("attachment_reference"),
        attachment_body_lines=kinds.count("attachment_body"),
        revision_lines=kinds.count("revision"),
        canvas_lines=kinds.count("canvas"),
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
    세는 것이 아니라 **대조하지 않았다**는 뜻이고, 보고서의 `rawDuplicateLines`·`humanDuplicateLines` 가
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
        matched = Matched()
        if verdict == "plan" and index:
            matched = _count_matched(path, doc, index)
        report.placements.append(Placement(
            doc=doc, destination=target, verdict=verdict, reason=reason,
            duplicate_lines=matched.raw_lines,
            residual_duplicate_lines=matched.residual_lines,
            matched_keys=matched.keys,
        ))
    return report


@dataclass(frozen=True)
class Matched:
    """좌표가 맞은 메시지와 그 메시지에 속한 줄 수.

    **줄이 넘어왔다는 뜻이 아니다.** 좌표는 메시지 단위라, 새 자료에 그 메시지의
    첨부 줄만 있어도 맞는다. 그래서 이름이 `duplicate` 가 아니라 `matched` 다.
    """

    keys: frozenset[tuple[str, str, str]] = frozenset()
    raw_lines: int = 0
    residual_lines: int = 0


def _count_matched(
    path: Path, doc: SourceDoc, index: set[tuple[str, str, str]],
) -> Matched:
    """새 자료에 **같은 좌표의 메시지가 있는** 옛 줄을 센다.

    좌표가 없는 줄은 후보에도 올리지 않는다. 비슷해 보인다고 합치면 다른 메시지를
    지우거나 같은 메시지를 둘로 남기고, 둘 다 오류를 내지 않는다.

    메시지 수와 줄 수를 **따로** 돌려준다. 한 메시지가 사람 발언 한 줄과 첨부 수십
    줄을 만들기 때문에, 줄 수만 보면 「대부분 이미 있다」 로 읽힌다.
    """
    loaded = load_doc(path)
    keys: set[tuple[str, str, str]] = set()
    raw_lines = 0
    residual_lines = 0
    for line in loaded.raw_lines:
        if not line.message_ts:
            continue
        key = (doc.workspace, doc.channel_id, line.message_ts)
        if key not in index:
            continue
        # 좌표를 **그대로** 든다. 워크스페이스·채널을 떼면 다른 채널의 같은 시각이
        # 같은 메시지로 합쳐진다(절대 원칙 4).
        keys.add(key)
        raw_lines += 1
        if line_kind(line) == "residual":
            residual_lines += 1
    return Matched(
        keys=frozenset(keys), raw_lines=raw_lines, residual_lines=residual_lines,
    )
