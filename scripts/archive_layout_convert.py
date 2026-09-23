#!/usr/bin/env python3
"""아카이브를 구조 1·2 두 배치로 변환한다 — **입력은 둘, 중간표현은 하나.**

    # 우리 아카이브에서
    python scripts/archive_layout_convert.py --from ty --source <스냅샷> --to a --out <경로>
    python scripts/archive_layout_convert.py --from ty --source <스냅샷> --to b --out <경로>

    # 프금팀 아카이브에서
    python scripts/archive_layout_convert.py --from pf --source <pf경로> --to a --out <경로>

    # 두 배치가 같은 내용인지
    python scripts/archive_layout_convert.py --verify <a경로> <b경로>

설계: `docs/design/archive-layout-benchmark.md` §3

## 구조

```
TY 스냅샷 ─┐
           ├─▶ 중간표현(Channel·Message) ─┬─▶ a 배치 (채널당 파일 하나)
PF 스냅샷 ─┘                              └─▶ b 배치 (채널당 폴더·날짜별)
```

입력 파서는 **둘**이어야 한다. 두 팀의 저장 모양이 다르고, 실측은 두 팀 자료를
각각 봐야 하기 때문이다. 출력기는 **하나의 중간표현**에서 나온다 — 따로 만들면
파싱이 갈리고, 그때 재는 것은 구조 차이가 아니라 두 스크립트의 차이가 된다.

## 없는 것을 지어내지 않는다

PF 자료에는 Slack `message_ts` 가 없다. **만들지 않는다.** 만들면 그 좌표로
permalink 를 만들게 되고, 사람이 눌러도 아무 데도 안 간다. 대신 `legacy_source_ref`
로 **어디서 왔는지**를 남긴다 — 스냅샷 해시, 파일, 줄 번호.

## 알려진 손실 — PF 본문의 줄바꿈

PF 는 메시지 본문의 줄바꿈을 보존한다. 우리 원문 형식은 한 줄이다
(`writer.format_line` 이 `\\n` 을 공백으로 바꾼다). 그래서 PF → 우리 형식 변환은
**문단 구조를 잃는다.**

실측에는 지장이 없다 — 두 배치에 **똑같이** 눌러 쓰므로 비교는 공정하다.
다만 실제 이관에서는 이 손실을 따로 다뤄야 한다. 변환 요약이 몇 건에서 눌러
썼는지 보고한다.

## 운영을 건드리지 않는다
`--source` 는 읽기만 하고 `--out` 은 비어 있어야 한다. 운영 경로를 주면 거절한다.
DB 는 쓰지 않는다 — `ArchiveStore.docs()` 는 파일만 읽는다.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.archive.store import ArchiveStore, load_doc  # noqa: E402
from tybot.archive.writer import _slugify  # noqa: E402

LAYOUTS = ("a", "b")
SOURCES = ("ty", "pf")
RAW_HEADING = "## 원문 (자동 취합, 편집 금지)"
# 출처 사이드카 스키마. 칸이 바뀌면 올린다 — 읽는 쪽이 옛 파일을 만났을 때
# 「빠진 칸」 과 「원래 없던 칸」 을 구분할 수 있어야 한다.
PROVENANCE_SCHEMA = 1

# 운영 경로. 실수로 여기에 쓰지 않는다.
PROTECTED = ("/var/lib/tybot/archive", "/var/lib/tybot/objects", "/var/lib/tybot/staging")


# ---------------------------------------------------------------------------
# 중간표현
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceRef:
    """이 메시지가 원본 어디서 왔나.

    **절대 경로를 담지 않는다.** 담으면 같은 사본을 다른 자리에서 변환했을 때
    값이 달라지고, 두 배치의 사이드카가 바이트로 같지 않게 된다. 그러면 「구조
    차이」 로 보이지만 실은 변환한 사람의 홈 디렉터리 차이다.
    """

    kind: str = ""               # "pf_git_snapshot" | "ty_archive"
    snapshot_sha256: str = ""    # full 64 hex. 화면 표시만 앞 12자를 쓴다
    source_path: str = ""        # source 루트 기준 POSIX 상대경로
    source_line: int = 0

    def as_json(self) -> dict | None:
        if not self.kind:
            return None
        return {
            "kind": self.kind,
            "snapshot_sha256": self.snapshot_sha256,
            "source_path": self.source_path,
            "source_line": self.source_line,
        }


@dataclass(frozen=True)
class Message:
    """메시지 하나. **두 배치가 이것에서 나온다.**

    `message_ts` 는 없으면 빈 문자열이다 — 지어내지 않는다(머리말).
    """

    ts: str                      # "YYYY-MM-DD HH:MM"
    speaker: str
    text: str
    message_ts: str = ""
    thread_ts: str = ""
    attachments: tuple[str, ...] = ()
    # 어디서 왔나. PF 자료처럼 좌표가 없는 것을 되짚는 유일한 길이다.
    # **원문 줄에는 넣지 않는다** — 원문이 오염되고, 배치 사이 바이트 동등성도
    # 깨진다. 별도 사이드카(`provenance/<id>.jsonl`)로 나간다.
    source_ref: SourceRef = SourceRef()

    @property
    def day(self) -> date | None:
        try:
            return date.fromisoformat(self.ts[:10])
        except ValueError:
            return None

    def key(self) -> str:
        """내용 동일성 키.

        **줄 번호와 파일 경로는 넣지 않는다** — 그건 배치마다 다르다. 대신 작성자·
        시각·본문·첨부·thread 좌표를 전부 넣는다. 하나라도 빼면 그 칸이 배치 사이에
        달라져도 검증이 통과한다.
        """
        raw = "|".join([
            self.ts, self.speaker, self.text, self.message_ts, self.thread_ts,
            ",".join(self.attachments),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Channel:
    workspace: str
    channel: str
    channel_id: str
    visibility: str
    acl: frozenset[str]
    share_with: frozenset[str]
    messages: list[Message] = field(default_factory=list)

    @property
    def stable_id(self) -> str:
        """경로에 쓸 식별자. **있는 값을 쓰기만 한다** — 지어내면 원본과 대조가 안 된다."""
        return self.channel_id or f"legacy-{_slugify(self.channel)}"

    def key(self) -> str:
        return f"{self.workspace}/{self.stable_id}"


# ---------------------------------------------------------------------------
# 입력 1 — 우리 아카이브
# ---------------------------------------------------------------------------

def read_ty(source: Path) -> tuple[list[Channel], list[str]]:
    """우리 v2 아카이브를 읽는다.

    `ArchiveStore.docs()` 를 쓰는 이유: 새 파서를 만들면 **reader 가 보는 것과 다른
    것**을 옮기게 된다. 그러면 변환은 맞는데 답변에서 줄이 빠지고, 그건 오류 없이
    「그 자료가 없다」 로만 보인다.
    """
    notes: list[str] = []
    store = ArchiveStore(source)
    files = store.source_files()
    snapshot = snapshot_digest(files, source) if files else ""
    out: list[Channel] = []
    for doc in store.docs():
        if getattr(doc, "dm_user", None):
            continue  # DM 은 다루지 않는다(머리말)
        if not doc.channel:
            notes.append(f"표시명이 빈 문서를 건너뛴다: {doc.path}")
            continue
        out.append(Channel(
            workspace=doc.workspace,
            channel=doc.channel,
            channel_id=doc.channel_id or "",
            visibility=doc.visibility,
            acl=frozenset(doc.acl),
            share_with=frozenset(doc.share_with),
            messages=[
                Message(
                    ts=line.ts,
                    speaker=line.speaker,
                    text=line.text,
                    message_ts=line.message_ts,
                    source_ref=_ty_ref(line, source, snapshot),
                )
                for line in doc.raw_lines
            ],
        ))
    return out, notes


def _ty_ref(line, source: Path, snapshot: str) -> SourceRef:
    """우리 아카이브 줄의 출처. 경로는 **source 기준 상대**다."""
    if not line.source_path:
        return SourceRef()
    try:
        rel = Path(line.source_path).relative_to(source).as_posix()
    except ValueError:
        # source 밖 파일. 절대 경로를 남기느니 경로를 비운다 — 절대 경로는
        # 변환한 사람의 자리를 사이드카에 새겨 넣는다.
        return SourceRef()
    return SourceRef(
        kind="ty_archive",
        snapshot_sha256=snapshot,
        source_path=rel,
        source_line=line.lineno,
    )


# ---------------------------------------------------------------------------
# 입력 2 — 프금팀 아카이브
# ---------------------------------------------------------------------------

PF_TITLE = re.compile(r"^#\s+#?(?P<name>.+?)\s*$")
PF_MESSAGE = re.compile(
    r"^\*\*(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*·\s*(?P<speaker>.+?)\*\*\s*$"
)
PF_ATTACHMENT = re.compile(r"^📎\s*첨부:\s*`(?P<name>.+?)`\s*$")
# 원문이 아닌 절. 여기 아래는 사람·봇의 **해석**이라 raw 로 옮기지 않는다.
PF_DERIVED_SECTION = re.compile(r"^##\s*(참여 기록|요약|정리|메모)")
PF_MONTH = re.compile(r"^##\s*\d{4}-\d{2}\s*$")


def read_pf(source: Path, *, workspace: str = "pf") -> tuple[list[Channel], list[str]]:
    """프금팀 채널별 MD 를 읽는다.

    ## 무엇을 옮기고 무엇을 안 옮기나
    머리말(`> **개설**` · `> **기간**`)과 「참여 기록 (요약)」 은 **해석**이다.
    사람이 썼든 봇이 썼든 Slack 원문이 아니고, 요약은 Hermes 몫이다(분리 결정 §1).
    raw 로 옮기면 검색이 그것을 근거로 잡고 출처는 「원문」 이라고 표시한다.

    그래서 옮기는 것은 `**시각 · 화자**` 로 시작하는 메시지뿐이다.
    """
    notes: list[str] = []
    channels: list[Channel] = []
    root = source / "slack-export" / "channels"
    if not root.is_dir():
        root = source  # 채널 파일만 모아 둔 경로도 받는다
    files = sorted(root.glob("*.md"))
    if not files:
        return [], [f"PF 채널 파일을 찾지 못했다: {root}"]

    # 지문은 **source 기준**으로 잰다. `root` 기준으로 재면 `slack-export/channels/`
    # 가 경로에서 빠져, 다른 폴더의 같은 이름 파일과 구분되지 않는다.
    snapshot = snapshot_digest(files, source)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            notes.append(f"{path.name}: 읽지 못함 ({type(exc).__name__})")
            continue
        rel = path.relative_to(source).as_posix()
        name, messages, dropped = _parse_pf_channel(text, rel, snapshot)
        if not name:
            notes.append(f"{path.name}: 채널 이름을 찾지 못해 건너뛴다")
            continue
        if dropped:
            notes.append(f"{path.name}: 해석 절 {dropped}줄을 옮기지 않았다")
        channels.append(Channel(
            workspace=workspace,
            channel=name,
            # PF 자료에는 Slack 채널 ID 가 없다. **지어내지 않는다** —
            # `stable_id` 가 이름에서 `legacy-…` 를 만들고, 그건 신원이 아니라
            # 자리표시자임이 이름으로 드러난다.
            channel_id="",
            # PF 자료는 이 채널이 공개였는지 말해 주지 않는다. **모르면 막는다**
            # (절대 원칙 3). 공개로 찍었다가 틀리면 사내 대화가 권한 없는 사람에게
            # 열리고, 반대로 비공개로 찍었다가 틀리면 사람이 못 볼 뿐이다.
            visibility="private",
            acl=frozenset({name}),
            share_with=frozenset(),
            messages=messages,
        ))
    return channels, notes


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_digest(files: list[Path], root: Path) -> str:
    """이 스냅샷의 지문. **내용으로 잰다.**

    전에는 파일 이름과 크기만 해싱했다. 그러면 내용이 바뀌어도 같은 값이 나오고,
    `legacy_source_ref` 가 「어느 사본이었나」 를 말하지 못한다. 되짚으러 간
    사람이 **다른 내용을 보고도 맞는 줄 안다** — 지문이 없느니만 못하다.

    세 성질을 지킨다.

    | | |
    |---|---|
    | 내용이 한 글자 바뀌면 | 지문이 바뀐다 |
    | 내용이 같고 상대경로만 바뀌면 | 지문이 바뀐다 (어느 채널 파일인지가 뜻이다) |
    | 절대 경로만 다르면(같은 사본, 다른 자리) | 지문이 **같다** |

    경로와 내용 사이에 길이를 끼우는 이유: 길이가 없으면 `ab`+`c` 와 `a`+`bc` 가
    같은 바이트열이 되어, 이름을 갈라 붙인 다른 스냅샷이 같은 지문을 낼 수 있다.

    내부 기록에는 full 64 hex 를 쓴다. 앞자리만 남기면 언젠가 부딪히고, 부딪힌
    날 그 사실을 알아챌 방법이 없다. 사람에게 보일 때만 앞 12자를 쓴다.
    """
    entries = sorted(
        (path.relative_to(root).as_posix(), path) for path in files
    )
    h = hashlib.sha256()
    for rel, path in entries:
        rel_bytes = rel.encode("utf-8")
        h.update(f"{len(rel_bytes)}:".encode("ascii"))
        h.update(rel_bytes)
        h.update(_file_sha256(path).encode("ascii"))
    return h.hexdigest()


def short(digest: str) -> str:
    """사람에게 보여 줄 때만 줄인다. 파일에 적는 값은 줄이지 않는다."""
    return digest[:12]


def _parse_pf_channel(
    text: str, rel_path: str, snapshot: str
) -> tuple[str, list[Message], int]:
    name = ""
    messages: list[Message] = []
    body: list[str] = []
    current: dict | None = None
    attachments: list[str] = []
    dropped = 0
    in_derived = False

    def flush() -> None:
        nonlocal body, attachments, current
        if current is not None:
            messages.append(Message(
                ts=current["ts"],
                speaker=current["speaker"],
                # 우리 원문 형식은 한 줄이다. 눌러 쓰되 **버리지는 않는다**(머리말).
                text=" ".join(line.strip() for line in body if line.strip()),
                message_ts="",          # PF 자료에는 없다. 지어내지 않는다
                attachments=tuple(attachments),
                source_ref=SourceRef(
                    kind="pf_git_snapshot",
                    snapshot_sha256=snapshot,
                    source_path=rel_path,
                    source_line=current["line"],
                ),
            ))
        body, attachments, current = [], [], None

    for lineno, line in enumerate(text.splitlines(), start=1):
        if not name:
            title = PF_TITLE.match(line)
            if title:
                name = "#" + title.group("name").lstrip("#")
                continue
        if PF_DERIVED_SECTION.match(line):
            flush()
            in_derived = True
            continue
        if PF_MONTH.match(line):
            # 월 구분은 원문이 아니라 목차다. 메시지 경계로만 쓴다.
            flush()
            in_derived = False
            continue
        if in_derived:
            dropped += 1 if line.strip() else 0
            continue

        message = PF_MESSAGE.match(line)
        if message:
            flush()
            current = {
                "ts": message.group("ts"),
                "speaker": message.group("speaker").strip(),
                "line": lineno,
            }
            continue
        if current is None:
            continue
        attach = PF_ATTACHMENT.match(line.strip())
        if attach:
            attachments.append(attach.group("name"))
            continue
        if line.strip() in {"---", ""}:
            if line.strip() == "---":
                flush()
            continue
        body.append(line)
    flush()
    return name, messages, dropped


READERS = {"ty": read_ty, "pf": read_pf}


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

def render_line(msg: Message) -> str:
    """원문 한 줄. `store.RAW_LINE_RE` 가 읽는 모양이어야 한다.

    다르면 변환본을 다시 읽을 때 그 줄이 원문으로 안 잡히고, 파일은 멀쩡해 보이는데
    근거가 0건이 된다.

    좌표가 없으면 **시각만** 적는다. 빈 `|` 를 붙이면 파서가 빈 `message_ts` 를
    읽어 「좌표가 있다」 고 판단한다.
    """
    stamp = f"{msg.ts}|{msg.message_ts}" if msg.message_ts else msg.ts
    text = msg.text
    if msg.attachments:
        # 첨부는 **참조만** 남긴다. 변환 본문은 별도 경로에 두기로 했다(분리 결정 §3).
        text += "".join(f" [첨부:{name}]" for name in msg.attachments)
    return f"> [{stamp}] {msg.speaker}: {text}"


def header(ch: Channel, *, source_date: date | None, count: int) -> str:
    """프론트매터. **`writer._new_doc` 과 같은 칸을 담아야 한다.**

    빠지면 `load_doc` 이 거절하고 그 문서는 오류 없이 검색에서 사라진다. 그래서
    변환 끝에 `load_doc` 으로 한 장씩 다시 읽어 확인한다.

    채널명에 따옴표를 씌우는 이유: `#` 로 시작하면 파서가 주석으로 읽어 표시명이
    빈 문자열이 되고, 관계없는 문서가 한 채널로 합쳐진다(B-61).
    """
    acl = "[" + ", ".join(sorted(ch.acl)) + "]"
    share = "[" + ", ".join(sorted(ch.share_with)) + "]"
    date_line = f"source_date: {source_date.isoformat()}\n" if source_date else ""
    return (
        "---\n"
        "schema_version: 2\n"
        f"workspace: {ch.workspace}\n"
        f'channel: "{ch.channel}"\n'
        # 빈 값을 쓰면 `load_doc` 이 필수 필드 누락으로 거절하고, 그 문서는
        # **오류 없이 검색에서 사라진다.** PF 자료에는 Slack ID 가 없으므로
        # `legacy-<slug>` 를 쓴다 — 접두어가 「이건 진짜 ID 가 아니다」 를 말해
        # 주고, 진짜 좌표는 줄마다 `legacy_source_ref` 에 남아 있다.
        f"channel_id: {ch.stable_id}\n"
        f"{date_line}"
        f"visibility: {ch.visibility}\n"
        f"acl: {acl}\n"
        f"share_with: {share}\n"
        f"doc_count: {count}\n"
        "last_ingested: \n"
        "---\n\n"
        f"{RAW_HEADING}\n"
    )


def _channels_base(out: Path, ch: Channel) -> Path:
    return out / "workspaces" / _slugify(ch.workspace) / "channels"


def _workspace_base(out: Path, workspace: str) -> Path:
    return out / "workspaces" / _slugify(workspace)


# ---------------------------------------------------------------------------
# 출처 사이드카 — **원문 밖에** 둔다
# ---------------------------------------------------------------------------

def _dump(row: dict) -> bytes:
    """결정적 직렬화. 같은 입력이면 **언제나 같은 바이트**여야 한다.

    `sort_keys` 가 없으면 파이썬 판이나 dict 삽입 순서가 바뀔 때 파일이 달라지고,
    그 차이는 두 배치 비교에서 「구조 차이」 로 보인다. `ensure_ascii=False` 로
    한글을 그대로 쓴다 — `\\uXXXX` 로 부풀리면 사람이 못 읽고 용량만 는다.
    """
    text = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")


def write_provenance(
    channels: list[Channel],
    out: Path,
    *,
    source_kind: str,
    source_commit: str = "",
) -> list[Path]:
    """`workspaces/<ws>/provenance/<stable_id>.jsonl` 을 쓴다.

    ## 왜 원문이 아니라 사이드카인가
    출처를 원문 줄에 끼우면 세 가지가 한꺼번에 깨진다 — 원문이 편집되고(절대 원칙 1),
    두 배치의 원문 바이트가 달라져 동등성 증명이 못 서고, 검색이 그 문자열을 근거로
    잡는다.

    ## 왜 두 배치에서 같은 바이트여야 하나
    구조 1 은 채널당 파일 하나, 구조 2 는 날짜별로 갈린다. **출처는 그와 무관한
    사실**이다. 사이드카가 배치마다 다르면 「어느 배치로 만든 아카이브냐」 가
    되짚기에 영향을 주게 되고, 그건 구조를 고르는 일과 아무 상관이 없어야 한다.
    그래서 경로도 내용도 배치를 타지 않는다.

    ## 왜 `channels/` 밖인가
    `ArchiveStore._files()` 가 `*/channels/…` 를 훑는다. 그 아래 두면 언젠가
    글롭에 걸려 **답변 근거로 나간다.** 확장자가 `.md` 가 아니라 안전하다고 볼
    수도 있지만, 그건 지금 글롭의 성질이지 구조의 성질이 아니다.

    ## 키
    `ordinal`(채널 안 0-based 순번)과 `record_sha256`(본문 동일성)을 **함께** 쓴다.
    해시만으로는 같은 문장이 두 번 나온 채널에서 어느 쪽인지 못 가리고, 순번만
    쓰면 줄이 하나 끼어들었을 때 그 뒤가 전부 어긋난 채 조용히 맞아 보인다.
    """
    written: list[Path] = []
    per_ws: dict[str, list[Channel]] = defaultdict(list)
    for ch in channels:
        per_ws[ch.workspace].append(ch)

    for workspace, group in sorted(per_ws.items()):
        base = _workspace_base(out, workspace) / "provenance"
        base.mkdir(parents=True, exist_ok=True)
        for ch in sorted(group, key=lambda c: c.stable_id):
            rows = [
                _dump({
                    "schema_version": PROVENANCE_SCHEMA,
                    "ordinal": ordinal,
                    "source_timestamp_text": msg.ts,
                    # Slack 좌표는 없으면 **null** 이다. 빈 문자열로 두면 「좌표가
                    # 있는데 비었다」 와 구분되지 않는다.
                    "message_ts": msg.message_ts or None,
                    "legacy_source_ref": msg.source_ref.as_json(),
                    "record_sha256": msg.key(),
                })
                for ordinal, msg in enumerate(ch.messages)
            ]
            path = base / f"{ch.stable_id}.jsonl"
            # **LF 로 쓴다.** 텍스트 모드로 쓰면 Windows 에서 CRLF 가 되어 같은
            # 입력이 플랫폼마다 다른 바이트를 낸다.
            path.write_bytes(b"".join(row + b"\n" for row in rows))
            written.append(path)

        manifest = base / "manifest.json"
        manifest.write_bytes(_dump({
            "schema_version": PROVENANCE_SCHEMA,
            "workspace": workspace,
            "source_kind": source_kind,
            # **git 커밋과 스냅샷 해시는 다른 값이다.** 커밋은 「저장소의 어느
            # 지점」 이고 스냅샷 해시는 「변환에 실제로 넣은 내용」 이다. 작업
            # 디렉터리가 더러우면 둘이 갈라지고, 그때 믿을 것은 스냅샷 해시다.
            "source_commit": source_commit or None,
            "source_snapshot_sha256": _snapshot_of(group),
            "channel_count": len(group),
            "message_count": sum(len(ch.messages) for ch in group),
        }) + b"\n")
        written.append(manifest)
    return written


def _snapshot_of(channels: list[Channel]) -> str | None:
    """이 배치가 물고 있는 스냅샷 해시. 하나여야 한다."""
    seen = {
        msg.source_ref.snapshot_sha256
        for ch in channels for msg in ch.messages
        if msg.source_ref.snapshot_sha256
    }
    if len(seen) == 1:
        return seen.pop()
    # 둘 이상이면 섞인 입력이다. 아무거나 고르면 되짚기가 틀린 사본을 가리킨다.
    return None


def emit_a(channels: list[Channel], out: Path) -> list[Path]:
    """구조 1 — 채널당 파일 하나. `<slug>__<id>.md`

    이름을 앞에 두어 정렬과 눈으로 찾기가 되고, identity 는 뒤의 ID 가 갖는다
    (2026-09-23 오너 결정). 구조 2 의 디렉터리가 `<id>__<slug>` 인 것과 다른데,
    **그쪽은 이미 운영에 있는 이름**이라 바꾸면 전부 옮겨야 한다. 새로 만드는
    쪽만 읽기 좋은 순서로 둔다.
    """
    written: list[Path] = []
    for ch in channels:
        path = _channels_base(out, ch) / f"{_slugify(ch.channel)}__{ch.stable_id}.md"
        days = [d for d in (m.day for m in ch.messages) if d]
        body = header(ch, source_date=min(days) if days else None, count=len(ch.messages))
        body += "\n".join(render_line(m) for m in ch.messages) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        written.append(path)
    return written


def emit_b(channels: list[Channel], out: Path) -> list[Path]:
    """구조 2 — 채널당 폴더, 날짜별 파일. **지금 운영 구조와 같다.**

    디렉터리 이름도 지금과 같게 둔다. 배치 b 가 곧 운영이어야 비교의 한쪽이
    「우리가 실제로 쓰는 것」 이 된다.
    """
    written: list[Path] = []
    for ch in channels:
        base = (
            _channels_base(out, ch) / f"{ch.stable_id}__{_slugify(ch.channel)}" / "raw"
        )
        by_day: dict[date, list[Message]] = defaultdict(list)
        undated: list[Message] = []
        for msg in ch.messages:
            day = msg.day
            (undated if day is None else by_day[day]).append(msg)
        # 날짜를 못 읽은 줄은 **버리지 않는다.** 가장 이른 날에 붙인다 — 버리면
        # 두 배치의 메시지 수가 달라지고, 검증이 그것을 버그로 잡는다.
        if undated:
            target = min(by_day) if by_day else date(1970, 1, 1)
            by_day[target] = undated + by_day.get(target, [])
        for day, group in sorted(by_day.items()):
            path = base / f"{day.isoformat()}.md"
            body = header(ch, source_date=day, count=len(group))
            body += "\n".join(render_line(m) for m in group) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            written.append(path)
    return written


EMIT = {"a": emit_a, "b": emit_b}


def build(
    channels: list[Channel],
    out: Path,
    layout: str,
    *,
    source_kind: str = "",
    source_commit: str = "",
) -> tuple[list[Path], list[Path]]:
    """배치 하나를 통째로 만든다 — 원문과 출처 사이드카를 **함께**.

    출처 쓰기를 호출부에 맡기지 않는 이유: 한 군데서 빠지면 그 배치만 사이드카가
    없고, 비교는 「경로 집합이 다르다」 로만 말한다. 원인을 변환에서 찾게 된다.
    """
    raw = EMIT[layout](channels, out)
    prov = write_provenance(
        channels, out, source_kind=source_kind, source_commit=source_commit
    )
    return raw, prov


# ---------------------------------------------------------------------------
# 검증 — 여섯 가지를 **전부** 본다
# ---------------------------------------------------------------------------

@dataclass
class Tally:
    channels: int = 0
    messages: int = 0
    # **multiset 이다.** 집합으로 세면 같은 줄이 한쪽에 두 번 있어도 통과한다.
    keys: Counter = field(default_factory=Counter)
    per_channel: dict[str, int] = field(default_factory=dict)
    # 채널별 메시지 **순서**. 대화는 순서가 곧 뜻이다.
    order: dict[str, list[str]] = field(default_factory=dict)
    # 권한. 내용은 같은데 보이는 범위가 달라지는 것이 가장 나쁜 실패다.
    acl: dict[str, tuple[str, frozenset[str], frozenset[str]]] = field(default_factory=dict)
    attachments: Counter = field(default_factory=Counter)
    schema_errors: list[str] = field(default_factory=list)


ATTACH_IN_LINE = re.compile(r"\[첨부:(?P<name>[^\]]+)\]")


def tally(root: Path) -> Tally:
    """배치 하나를 **실제 reader 로** 다시 읽어 센다.

    파일을 직접 세지 않는 이유: 우리가 쓴 것이 아니라 **reader 가 보는 것**이 같아야
    한다. 프론트매터가 어긋나 문서가 통째로 빠지는 경우를 파일 수로는 못 잡는다.
    """
    result = Tally()
    store = ArchiveStore(root)
    for path in store.source_files():
        try:
            load_doc(path)
        except Exception as exc:  # noqa: BLE001 - 위반을 모아 보고한다
            result.schema_errors.append(f"{path}: {exc}")

    for doc in store.docs():
        if getattr(doc, "dm_user", None) or not doc.channel:
            continue
        key = f"{doc.workspace}/{doc.channel_id or doc.channel}"
        result.channels += 1
        result.per_channel[key] = len(doc.raw_lines)
        result.messages += len(doc.raw_lines)
        result.acl[key] = (doc.visibility, frozenset(doc.acl), frozenset(doc.share_with))
        order: list[str] = []
        for line in doc.raw_lines:
            raw = "|".join([line.ts, line.speaker, line.text, line.message_ts])
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            result.keys[digest] += 1
            order.append(digest)
            for match in ATTACH_IN_LINE.finditer(line.text):
                result.attachments[match.group("name")] += 1
        result.order[key] = order
    return result


def verify(a: Path, b: Path) -> list[str]:
    """두 배치가 같은 내용인가. 문제를 **모아서** 돌려준다.

    하나 찾고 멈추면 고치고 다시 돌리기를 반복하게 된다. 한 번에 다 보여 준다.
    """
    problems: list[str] = []
    ta, tb = tally(a), tally(b)

    for name, t in (("a", ta), ("b", tb)):
        for err in t.schema_errors[:10]:
            problems.append(f"[{name}] 스키마 위반: {err}")

    if ta.channels != tb.channels:
        problems.append(f"채널 수가 다르다: a={ta.channels} b={tb.channels}")
    if ta.messages != tb.messages:
        problems.append(f"메시지 수가 다르다: a={ta.messages} b={tb.messages}")

    # 1. 내용 multiset — 중복까지 같아야 한다
    if ta.keys != tb.keys:
        only_a = sum((ta.keys - tb.keys).values())
        only_b = sum((tb.keys - ta.keys).values())
        problems.append(f"메시지 내용이 다르다: a 에만 {only_a}건, b 에만 {only_b}건")

    # 2. 채널별 수와 3. 순서
    for key in sorted(set(ta.per_channel) | set(tb.per_channel)):
        na, nb = ta.per_channel.get(key), tb.per_channel.get(key)
        if na != nb:
            problems.append(f"채널 {key} 메시지 수: a={na} b={nb}")
        elif ta.order.get(key) != tb.order.get(key):
            # 대화는 순서가 곧 뜻이다. 수가 같아도 순서가 다르면 다른 기록이다.
            problems.append(f"채널 {key} 메시지 순서가 다르다")

    # 4. 권한
    for key in sorted(set(ta.acl) | set(tb.acl)):
        if ta.acl.get(key) != tb.acl.get(key):
            problems.append(
                f"채널 {key} 권한이 다르다: a={ta.acl.get(key)} b={tb.acl.get(key)}"
            )

    # 5. 첨부 참조
    if ta.attachments != tb.attachments:
        problems.append(
            f"첨부 참조가 다르다: a={sum(ta.attachments.values())}건"
            f" b={sum(tb.attachments.values())}건"
        )

    # 6. 출처 사이드카 — **바이트로** 같아야 한다
    problems += _verify_provenance(a, b, ta)
    return problems


def provenance_files(root: Path) -> dict[str, bytes]:
    """배치의 출처 사이드카. 키는 `workspaces/` 기준 상대 POSIX 경로."""
    base = root / "workspaces"
    if not base.is_dir():
        return {}
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in sorted(base.glob("*/provenance/*"))
        if path.is_file()
    }


def _verify_provenance(a: Path, b: Path, ta: Tally) -> list[str]:
    """사이드카가 두 배치에서 같은가, 그리고 **메시지마다 정확히 한 줄인가.**

    바이트 비교만 하면 「둘 다 비었다」 도 통과한다. 그래서 원문 줄 수와 맞춰
    본다 — 사이드카가 통째로 안 써진 경우가 바로 그 모양이다.
    """
    problems: list[str] = []
    fa, fb = provenance_files(a), provenance_files(b)
    if set(fa) != set(fb):
        only_a = sorted(set(fa) - set(fb))[:5]
        only_b = sorted(set(fb) - set(fa))[:5]
        problems.append(f"출처 사이드카 경로가 다르다: a 에만 {only_a}, b 에만 {only_b}")
    for name in sorted(set(fa) & set(fb)):
        if fa[name] != fb[name]:
            problems.append(f"출처 사이드카 내용이 다르다: {name}")

    # 채널마다 원문 줄 수와 사이드카 줄 수가 같아야 한다.
    for key, count in sorted(ta.per_channel.items()):
        workspace, _, channel_id = key.partition("/")
        name = f"{_slugify(workspace)}/provenance/{channel_id}.jsonl"
        blob = fa.get(name)
        if blob is None:
            problems.append(f"출처 사이드카가 없다: {name}")
            continue
        rows = blob.count(b"\n")
        if rows != count:
            problems.append(f"출처 줄 수가 원문과 다르다: {name} 원문={count} 출처={rows}")
    return problems


# ---------------------------------------------------------------------------

def _refuse_operational(path: Path, what: str) -> str | None:
    # 준 그대로와 풀어 놓은 것을 **둘 다** 본다. `resolve()` 는 심링크를 따라가
    # 우회를 막아 주지만, 개발 PC(Windows)에서는 `/var/...` 에 드라이브를 붙여
    # `C:/var/...` 로 만들어 버려 오히려 통과시킨다.
    candidates = {str(path).replace("\\", "/")}
    with contextlib.suppress(OSError):
        candidates.add(str(path.resolve()).replace("\\", "/"))
    for guarded in PROTECTED:
        for cand in candidates:
            if cand == guarded or cand.startswith(guarded + "/"):
                return f"{what}이 운영 경로입니다: {path}"
    return None


def _git_commit(source: Path) -> str:
    """입력 저장소의 커밋. **없어도 변환은 돈다.**

    스냅샷 해시가 진짜 근거이고 커밋은 편의다. 여기서 실패하면 조용히 비워 둔다 —
    git 이 없다는 이유로 변환을 못 하게 만들 이유가 없다.
    """
    import subprocess

    for candidate in (source, *source.parents):
        if not (candidate / ".git").exists():
            continue
        with contextlib.suppress(Exception):
            out = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            return out.stdout.strip()
        return ""
    return ""


def _utf8_console() -> None:
    """콘솔이 cp949 여도 죽지 않게 한다.

    개발 PC 의 기본 콘솔 코드페이지가 cp949 라 `—` 같은 글자에서
    `UnicodeEncodeError` 로 **스크립트가 끝난다.** 변환은 다 끝났는데 보고를
    찍다가 죽는 것이라, 사람은 변환이 실패한 줄 안다.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="아카이브를 구조 1·2 두 배치로 변환한다. 자세한 설명은 이 파일 머리말.",
    )
    parser.add_argument("--from", dest="source_kind", choices=SOURCES, help="입력 형식")
    parser.add_argument("--source", help="스냅샷 경로(읽기만 한다)")
    parser.add_argument("--workspace", default="pf", help="PF 입력에 붙일 워크스페이스 키")
    parser.add_argument("--to", choices=LAYOUTS, help="만들 배치")
    parser.add_argument("--out", help="배치를 쓸 경로")
    parser.add_argument(
        "--verify", nargs=2, metavar=("A", "B"), help="두 배치가 같은 내용인지 본다"
    )
    parser.add_argument(
        "--source-commit", default="",
        help="입력 저장소의 git 커밋 SHA. 주지 않으면 source 에서 읽어 본다",
    )
    _utf8_console()
    args = parser.parse_args()

    if args.verify:
        problems = verify(Path(args.verify[0]), Path(args.verify[1]))
        if problems:
            print("■ 두 배치가 같지 않습니다")
            for item in problems:
                print(f"  ✗ {item}")
            print()
            print("측정하지 마세요. 이 숫자는 구조가 아니라 버그를 잽니다.")
            return 1
        print("두 배치가 같은 내용입니다 (수·내용·순서·권한·첨부).")
        return 0

    if not (args.source_kind and args.source and args.to and args.out):
        parser.error("--from --source --to --out 이 함께 필요합니다 (또는 --verify)")

    source, out = Path(args.source).expanduser(), Path(args.out).expanduser()
    if not source.is_dir():
        print(f"스냅샷이 없습니다: {source}")
        return 2
    for path, what in ((source, "입력"), (out, "출력")):
        refusal = _refuse_operational(path, what)
        if refusal:
            print(refusal)
            print("  운영 아카이브는 읽기만 하고, 사본에서 작업하세요(설계 §2).")
            return 2
    if out.exists() and any(out.iterdir()):
        # 남은 파일 위에 쓰면 옛 배치가 섞인다. 그 섞임은 검증에서 「a 에만 있는
        # 메시지」 로 나타나고, 원인을 변환에서 찾게 된다.
        print(f"출력 경로가 비어 있지 않습니다: {out}")
        print("  이전 결과와 섞이면 검증이 엉뚱한 곳을 가리킵니다. 비우고 다시 실행하세요.")
        return 2

    reader = READERS[args.source_kind]
    channels, notes = (
        reader(source, workspace=args.workspace) if args.source_kind == "pf"
        else reader(source)
    )
    if not channels:
        print(f"채널을 하나도 읽지 못했습니다: {source}")
        for note in notes[:10]:
            print(f"  · {note}")
        return 1

    commit = args.source_commit or _git_commit(source)
    written, prov = build(
        channels, out, args.to,
        source_kind="pf_git_snapshot" if args.source_kind == "pf" else "ty_archive",
        source_commit=commit,
    )
    messages = sum(len(ch.messages) for ch in channels)
    no_coord = sum(1 for ch in channels for m in ch.messages if not m.message_ts)
    print(f"배치 {args.to}: 채널 {len(channels)}개, 메시지 {messages}개, 파일 {len(written)}개")
    print(f"  → {out}")
    digest = _snapshot_of(channels)
    if digest:
        # git 커밋과 **다른 값**이다. 커밋은 저장소의 지점, 이것은 실제로 넣은 내용.
        print(f"  · 스냅샷 {short(digest)}… · 커밋 {short(commit) + '…' if commit else '(없음)'}")
    print(f"  · 출처 사이드카 {len(prov)}개 (원문 밖, 두 배치에서 같은 바이트)")
    if no_coord:
        # 지어내지 않았다는 사실을 **숫자로** 말한다. 조용히 비워 두면 나중에
        # 「왜 permalink 가 없나」 를 코드에서 찾게 된다.
        print(f"  · Slack 좌표가 없는 메시지 {no_coord}개 — 지어내지 않고 비워 두었습니다")
    for note in notes[:10]:
        print(f"  · {note}")
    if len(notes) > 10:
        print(f"  · … 외 {len(notes) - 10}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
