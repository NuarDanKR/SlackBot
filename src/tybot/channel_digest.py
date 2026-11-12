"""채널×일 다이제스트 — 입력 수집과 지문 (B-93 2단계 §7-1).

설계: `docs/design/channel-day-digest.md`.

다이제스트는 **검색 길잡이**다. 그 문장은 답변 근거·출처로 나가지 않고, 질문이
다이제스트와 맞으면 그 (채널, 날짜)의 **원문**을 현재 권한으로 다시 연다(원칙 1·3).

이 모듈은 아직 아무것도 쓰지 않는다. 하는 일은 셋이다.

1. 채널 디렉터리에서 **하루치 입력**을 모은다 — 그날 일자 MD 의 사람 발언 줄과,
   그날 메시지에 붙은 첨부 정본(최신 revision, 쓸 수 있는 것만)
2. 입력의 **지문**(`revision`)을 만든다. 일자 파일 해시·첨부 revision·프롬프트 판·모델을
   합친 값이라, 같은 입력이면 다시 만들지 않고 원문이 바뀌면 다른 값이 된다
3. LLM 에 줄 입력 본문을 만든다(상한 안에서, 자르면 자른다고 적는다)

DM(`workspaces/<ws>/dm/…`)은 대상이 아니다. 그 사람 한 명의 기록이고 본인 DM 에서만
근거가 된다(dm-workspace 설계) — 채널 길잡이에 섞으면 그 경계가 흐려진다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from .archive import attachment_reader
from .archive.store import load_doc

#: 하루의 경계는 한국 시간이다. 일자 MD 파일명도 KST 날짜다.
KST = timezone(timedelta(hours=9))

#: 프롬프트·입력 규칙의 판. 바꾸면 모든 지문이 바뀌어 다시 만든다.
PROMPT_REVISION = "2026-11-12.1"

#: 첨부 한 개에서 입력으로 넣는 앞부분 상한(글자).
ATTACHMENT_EXCERPT_CHARS = 6_000
#: 하루 입력 전체 상한(글자). 넘으면 뒤를 자르고 「잘림」 을 적는다.
DAY_INPUT_MAX_CHARS = 60_000

_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


@dataclass(frozen=True)
class SourceFile:
    """일자 원문 MD 한 개의 좌표. `path` 는 아카이브 루트 기준 POSIX 경로."""

    path: str
    sha256: str
    lines: int

    def to_json(self) -> dict:
        return {"path": self.path, "sha256": self.sha256, "lines": self.lines}


@dataclass(frozen=True)
class SourceAttachment:
    """그날 메시지에 붙은 첨부 정본 한 개의 좌표."""

    path: str
    file_id: str
    revision: str
    sha256: str

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "file_id": self.file_id,
            "revision": self.revision,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class DayInput:
    """한 채널의 하루치 입력. 다이제스트 하나가 묶는 범위가 정확히 이것이다."""

    workspace: str
    channel_id: str
    channel: str
    day: date
    lines: tuple[str, ...] = ()
    attachments: tuple[tuple[str, str], ...] = ()  # (파일명, 본문 앞부분)
    source_files: tuple[SourceFile, ...] = ()
    source_attachments: tuple[SourceAttachment, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.lines and not any(text.strip() for _, text in self.attachments)


def day_of_message_ts(value: str) -> date | None:
    """Slack 메시지 ts(epoch 초) → KST 날짜. 모양이 틀리면 `None`."""
    try:
        seconds = float(str(value or "").strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC).astimezone(KST).date()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _raw_files(root: Path, workspace: str, channel_id: str) -> list[Path]:
    """그 채널의 일자 원문 MD. 아카이브 배치가 두 가지라 둘 다 본다(`ArchiveStore._files`).

    - `workspaces/<ws>/channels/<채널ID>__<이름>/raw/YYYY-MM-DD.md` (수집기 v2)
    - `<ws>/<채널ID>__<이름>/archive/raw/YYYY-MM-DD.md` (Archiver)

    DM(`workspaces/<ws>/dm/…`)은 어느 쪽 모양에도 걸리지 않는다 — 그 사람 한 명의 기록이고
    본인 DM 에서만 근거가 된다(dm-workspace 설계). 채널 길잡이에 섞지 않는다.
    """
    found = list(root.glob(f"workspaces/{workspace}/channels/{channel_id}__*/raw/*.md"))
    found += list(root.glob(f"{workspace}/{channel_id}__*/archive/raw/*.md"))
    return sorted(p for p in found if _DAY_FILE.match(p.name))


def _attachment_files(root: Path, workspace: str, channel_id: str) -> list[Path]:
    found: list[Path] = []
    for pattern in (
        f"workspaces/{workspace}/channels/{channel_id}/attachments/**/*.md",
        f"workspaces/{workspace}/channels/{channel_id}__*/attachments/**/*.md",
        f"{workspace}/{channel_id}__*/archive/attachments/**/*.md",
    ):
        found.extend(root.glob(pattern))
    return sorted(set(found))


def collect_channel_days(root: Path | str, workspace: str, channel_id: str) -> list[DayInput]:
    """채널 하나의 날짜별 입력. 날짜 오름차순.

    - 일자 원문: 사람 발언 줄만 있다(봇 발언은 아카이브에 쌓지 않는다 — 원칙 1)
    - 첨부: 파일마다 최신 revision 하나, 쓸 수 있는 것만. 날짜는 첨부가 붙은 **메시지
      ts** 의 KST 날짜다. ts 가 없는 첨부는 날짜를 정할 수 없어 넣지 않는다 — 엉뚱한 날에
      묶이면 그날 원문에 없는 내용이 된다
    - 파일 머리의 workspace·channel_id 가 요청과 다르면 넣지 않는다. 경로만 믿으면 잘못
      놓인 파일 하나가 다른 채널 길잡이에 섞인다(원칙 3)
    """
    root = Path(root)
    by_day: dict[date, dict] = {}

    def slot(day: date) -> dict:
        return by_day.setdefault(day, {"lines": [], "files": [], "atts": [], "texts": []})

    channel = ""
    for path in _raw_files(root, workspace, channel_id):
        doc = load_doc(path)
        if doc.workspace != workspace or (doc.channel_id or "") != channel_id:
            continue
        day = date.fromisoformat(_DAY_FILE.match(path.name).group(1))
        channel = channel or doc.channel
        entry = slot(day)
        for line in doc.raw_lines:
            entry["lines"].append(f"{line.ts} {line.speaker}: {line.text}")
        entry["files"].append(
            SourceFile(path=_rel(path, root), sha256=_sha256(path), lines=len(doc.raw_lines))
        )

    loaded = []
    for path in _attachment_files(root, workspace, channel_id):
        doc = attachment_reader.load(path)
        if doc is not None and doc.workspace == workspace and doc.channel_id == channel_id:
            loaded.append(doc)
    for doc in attachment_reader.current_by_file(loaded):
        if not doc.usable or doc.source_path is None:
            continue
        day = day_of_message_ts(doc.message_ts)
        if day is None:
            continue
        channel = channel or doc.channel
        entry = slot(day)
        entry["texts"].append(
            (doc.name or doc.file_id, (doc.text or "")[:ATTACHMENT_EXCERPT_CHARS])
        )
        entry["atts"].append(
            SourceAttachment(
                path=_rel(doc.source_path, root),
                file_id=doc.file_id,
                revision=doc.revision,
                sha256=doc.sha256 or _sha256(doc.source_path),
            )
        )

    return [
        DayInput(
            workspace=workspace,
            channel_id=channel_id,
            channel=channel,
            day=day,
            lines=tuple(entry["lines"]),
            attachments=tuple(entry["texts"]),
            source_files=tuple(entry["files"]),
            source_attachments=tuple(entry["atts"]),
        )
        for day, entry in sorted(by_day.items())
    ]


def revision(day_input: DayInput, *, model: str, prompt_revision: str = PROMPT_REVISION) -> str:
    """입력 지문. 원문 좌표·해시·프롬프트 판·모델이 같으면 같은 값이다.

    본문을 직접 해시하지 않고 **파일 해시**를 쓴다 — 지문만 보고도 「어느 원문이 바뀌었나」
    를 `source_files` 와 대조해 설명할 수 있어야 한다.
    """
    payload = {
        "workspace": day_input.workspace,
        "channel_id": day_input.channel_id,
        "day": day_input.day.isoformat(),
        "files": sorted((f.path, f.sha256) for f in day_input.source_files),
        "attachments": sorted(
            (a.file_id, a.revision, a.sha256) for a in day_input.source_attachments
        ),
        "prompt": prompt_revision,
        "model": model,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def digest_id(day_input: DayInput, rev: str) -> str:
    """행 식별자. 같은 (채널, 날짜, 지문)이면 같은 값 — 재실행이 중복 행을 만들지 않는다."""
    key = f"{day_input.workspace}|{day_input.channel_id}|{day_input.day.isoformat()}|{rev}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class RenderedInput:
    text: str
    truncated: bool = False
    chars: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


def render_input(day_input: DayInput, *, max_chars: int = DAY_INPUT_MAX_CHARS) -> RenderedInput:
    """LLM 에 줄 하루치 본문. 상한을 넘으면 **자른다고 적고** 자른다."""
    parts: list[str] = [f"[대화 {len(day_input.lines)}줄]"]
    parts.extend(day_input.lines)
    for name, text in day_input.attachments:
        if text.strip():
            parts.append(f"\n[첨부: {name}]")
            parts.append(text.strip())
    body = "\n".join(parts)
    if len(body) <= max_chars:
        return RenderedInput(text=body, chars=len(body))
    cut = body[:max_chars].rstrip()
    note = f"(입력이 {len(body)}자라 앞 {max_chars}자만 넣었습니다)"
    return RenderedInput(text=f"{cut}\n{note}", truncated=True, chars=len(body), notes=(note,))


__all__ = [
    "ATTACHMENT_EXCERPT_CHARS",
    "DAY_INPUT_MAX_CHARS",
    "KST",
    "PROMPT_REVISION",
    "DayInput",
    "RenderedInput",
    "SourceAttachment",
    "SourceFile",
    "collect_channel_days",
    "day_of_message_ts",
    "digest_id",
    "render_input",
    "revision",
]
