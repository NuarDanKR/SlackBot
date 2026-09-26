"""첨부 정본 문서 — 변환 본문을 raw 에서 떼어 독립 문서로 둔다.

설계: `docs/design/archiving-bot-separation-2026-09-23.md` §2·§3

## 무엇이 문제인가
지금 첨부 변환본은 **두 곳에 있다.**

```
staging/…/attachments/<file-id>/extracted.md   ← 본문 (검색 트리 밖)
archive/…/raw/2026-09-23.md                    ← 같은 본문이 [첨부추출:이름] 줄로 복제
```

검색·답변이 읽는 것은 raw 쪽이다. staging 은 재처리용이라 프론트매터도 ACL 도 없어
문서로 열 수 없다. 그래서 첨부는 **채팅 원문의 일부처럼** 취급된다.

그게 셋을 망친다.

1. **사람 발언과 문서가 한 줄기로 섞인다.** 문서의 8월 금액과 사람이 9월에 정정한
   금액이 같은 목록에 오면 모델이 하나를 골라 합친다(절대 원칙 7 이 막으려던 것)
2. **첨부별 상태를 잃는다.** PII 로 막힌 것, 절반만 읽힌 것, 변환 실패한 것이
   전부 「그 줄이 없음」 으로 같아 보인다
3. **되돌릴 수 없다.** raw 는 편집 금지라 변환기를 고쳐도 옛 본문이 그대로 남는다

## 무엇을 하나
`metadata.json` 과 `extracted.md` 는 **이미 file ID 단위로 있다.** 새로 수집할 것이
없다. 그 둘을 프론트매터 붙은 정본 문서로 옮긴다.

```
archive/workspaces/<ws>/channels/<channel-id>/attachments/<file-id>/<revision>.md
```

`revision` 은 원본 sha256 앞자리다. 같은 파일을 더 나은 변환기로 다시 읽으면 새
revision 이 생기고, **옛 것을 지우지 않는다** — 그때 답이 달라진 이유를 되짚을 수
있어야 한다. 근거로 쓰는 것은 가장 최근 revision 하나다.

## 같은 파일을 두 번 세지 않는다
기존 raw 에 박힌 `[첨부추출:…]` 줄은 **소급 삭제하지 않는다**(설계 §3). 편집 금지
대상이고, 지우면 그 줄을 가리키던 옛 출처가 끊긴다.

그래서 새 reader 는 raw 와 정본 문서를 **둘 다** 보게 되고, 그대로 두면 같은 첨부가
근거 두 개가 된다. 답이 「두 군데서 확인됨」 처럼 보이는데 사실은 한 군데다.

막는 방법은 §3 이 말한 대로 file ID 지만, **옛 raw 줄에는 file ID 가 없다** —
파일명만 있다(`attachment_trace` 가 같은 문제를 겪었다). 그래서 다리를 놓는다.

- 새 문서: file ID 로 센다
- 옛 raw 줄: **채널 + 파일명**으로 맞춰 본다. 같은 채널에 같은 이름이 raw 에 이미
  있으면 그 정본 문서는 근거에서 뺀다

보수적인 쪽으로 틀린다. 이름이 우연히 겹치면 새 문서 하나를 놓치지만, 그 내용은
raw 에 남아 있으므로 답변에서 사라지지는 않는다. 반대로 하면 조용히 두 번 센다.

이 다리는 **스스로 걷힌다.** 새 writer 가 raw 에 본문을 안 쓰기 시작하면 새 파일은
raw 에 없고, 그러면 아무것도 안 걸러진다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tybot.archive.attachment_doc")

SCHEMA_VERSION = 1

# raw 에 박힌 첨부 줄. `document_evidence.EXTRACT_PREFIXES` 와 같은 표식이다.
LEGACY_LINE = re.compile(r"^\[(첨부추출|첨부본문):(?P<name>[^\]]+)\]")

# 변환 상태. `files.py` 의 `conversion_state` 와 같은 값이어야 한다 — 다르면
# 여기서 조용히 `unknown` 이 되고, 화면은 「변환 안 됨」 으로 읽는다.
CONVERTED = "succeeded"
PARTIAL = "partial"
BLOCKED = "blocked"
FAILED = "failed"
UNSUPPORTED = "unsupported"
PENDING = "pending"

# 근거로 쓸 수 있는 상태. 나머지는 **문서로는 남기되 본문이 없다** — 그 사실
# 자체가 정보다(「PII 로 막혔다」 와 「그런 파일이 없다」 는 다르다).
USABLE = (CONVERTED, PARTIAL)

REVISION_LENGTH = 12

#: 본문이 없을 때 대신 적는 줄. **읽는 쪽이 이걸 본문으로 오해하면 안 된다** —
#: 「PII 로 막혔다」 가 검색 결과로 나오면 그 자체가 근거처럼 보인다.
NO_BODY_PREFIX = "> 본문이 없습니다:"

#: 부분 변환본에 붙는 안내. 본문이 아니라 메타다.
PARTIAL_PREFIX = "> 부분 변환본입니다"


class AttachmentDocError(RuntimeError):
    """정본 문서를 만들 수 없다."""


@dataclass(frozen=True)
class AttachmentDoc:
    """첨부 하나의 정본. **채널 문서의 ACL 을 상속한다.**

    첨부는 그 채널에 올라온 것이므로 채널보다 넓게 열릴 수 없다. 상속하지 않고
    따로 적으면 채널 권한이 바뀐 날 둘이 갈라지고, 그때 더 넓은 쪽이 이긴다.
    """

    workspace: str
    channel_id: str
    channel: str
    file_id: str
    name: str
    revision: str
    visibility: str
    acl: frozenset[str]
    # 변환 본문. 상태에 따라 비어 있을 수 있고, **빈 것과 없는 것은 다르다.**
    text: str = ""
    conversion_state: str = PENDING
    # 어느 메시지에 붙어 있었나. 없으면 채널 링크로 내려간다.
    message_ts: str = ""
    permalink: str = ""
    filetype: str = ""
    sha256: str = ""
    staged_at: str = ""
    # 변환이 **끝난** 시각. `staged_at` 은 metadata 를 쓴 시각이라 재변환에서 둘이
    # 갈린다. 최신 판정은 이쪽으로 한다 — 없으면 `staged_at` 으로 내려간다.
    converted_at: str = ""
    # 어떤 변환기가 읽었나. revision 의 재료이자, 나중에 그 판을 재현하는 근거다.
    converter_name: str = ""
    converter_version: str = ""
    # 얼마나 읽었나. 모르면 None — 0 으로 만들면 모르는 것을 안다고 적는 셈이다.
    coverage_read: int | None = None
    coverage_total: int | None = None
    # PII·변환 실패 사유. **코드만** 남긴다(본문·번호 일부를 복제하지 않는다).
    error_code: str = ""

    @property
    def usable(self) -> bool:
        """근거로 쓸 수 있나."""
        return self.conversion_state in USABLE and bool(self.text.strip())

    @property
    def partial(self) -> bool:
        return self.conversion_state == PARTIAL

    def relative_path(self) -> Path:
        return (
            Path("workspaces") / _safe(self.workspace)
            / "channels" / _safe(self.channel_id)
            / "attachments" / _safe(self.file_id)
            / f"{self.revision}.md"
        )


def _safe(value: str) -> str:
    """경로 한 칸. `files._safe_component` 와 **같은 규칙**이어야 한다.

    다르면 같은 파일이 두 경로에 생기고, 그때 중복 방지 키가 둘이 된다.

    **`.` 과 `..` 은 따로 막는다.** 허용 문자에 `.` 가 있으므로 걸러도 그대로
    남는데, 그 둘은 파일 이름이 아니라 **경로 지시자**다. `file_id` 가 `..` 면
    `attachments/../…` 가 되어 채널 밖으로 나간다. Slack file ID 가 그럴 리
    없다고 믿지 않는다 — 이 값은 우리가 만든 것이 아니다.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", (value or "").strip())
    if cleaned.strip(".") == "":
        return "_"
    return cleaned[:120] or "_"


def revision_for(
    *,
    sha256: str,
    converter_name: str = "",
    converter_version: str = "",
    config: dict | None = None,
    staged_at: str = "",
) -> str:
    """이 변환본의 판 번호. **원본만으로 정하지 않는다.**

    전에는 원본 sha256 앞자리였다. 그러면 같은 파일을 **더 나은 변환기로 다시
    읽어도 같은 경로**가 되어 덮어쓴다. 그 변환본을 인용한 답변이 이미 나가
    있으면, 사람이 출처를 눌렀을 때 인용된 문장이 없다.

    네 가지에서 나온다 — 원본 해시, 변환기 이름, 변환기 판, 출력에 영향을 주는
    설정. 하나라도 다르면 다른 판이고, 다른 판은 **덮지 않고 나란히 쌓인다**.

    `archiving_state.attachment_revision` 과 같은 규칙을 쓴다. 두 곳이 갈리면
    DB 가 아는 revision 과 파일 경로가 어긋나고, 그때 근거를 못 찾는다.

    변환기를 모르는 옛 metadata 는 **원본 해시만으로** 계산한다 — 그래야 이미
    쓰여 있는 정본의 경로가 바뀌지 않는다.
    """
    digest = (sha256 or "").strip()
    if not digest:
        stamp = re.sub(r"[^0-9]", "", staged_at or "")[:14]
        return f"t{stamp}" if stamp else "unknown"
    if not converter_name:
        # 옛 metadata. 경로를 바꾸면 이미 있는 정본이 고아가 된다.
        return digest[:REVISION_LENGTH]

    from .archiving_state import attachment_revision

    return attachment_revision(
        source_sha256=digest,
        converter_name=converter_name,
        converter_version=converter_version,
        config=config or {},
    )[:REVISION_LENGTH]


def revision_of(sha256: str, *, staged_at: str = "") -> str:
    """옛 이름. 변환기를 모르는 호출부가 아직 쓴다."""
    return revision_for(sha256=sha256, staged_at=staged_at)


# ---------------------------------------------------------------------------
# staging 에서 읽어 정본으로
# ---------------------------------------------------------------------------

def from_staged(
    staged_dir: Path | str,
    *,
    workspace: str,
    channel_id: str,
    channel: str,
    visibility: str,
    acl: frozenset[str],
) -> AttachmentDoc | None:
    """`staging/…/<file-id>/` 하나를 정본 문서로 읽는다.

    읽을 수 없으면 **예외 대신 `None`** 이다. 첨부 하나가 깨졌다고 나머지 수백 개의
    정본화가 멈추면 안 된다. 대신 로그를 남긴다.
    """
    root = Path(staged_dir)
    try:
        meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("첨부 metadata 를 읽지 못했다 %s: %s", root, type(exc).__name__)
        return None
    if not isinstance(meta, dict):
        return None

    file_id = str(meta.get("slack_file_id") or "").strip()
    if not file_id:
        # 좌표가 없으면 **임의 채널에 붙이지 않는다**(설계 §3). 어느 파일인지
        # 모르는 문서는 중복 방지도 출처 표시도 할 수 없다.
        logger.warning("첨부에 file ID 가 없어 정본을 만들지 않는다: %s", root)
        return None

    text = ""
    extracted = root / "extracted.md"
    if extracted.exists():
        try:
            text = _strip_local_header(extracted.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("첨부 본문을 읽지 못했다 %s: %s", extracted, type(exc).__name__)

    return AttachmentDoc(
        workspace=workspace,
        channel_id=channel_id,
        channel=channel,
        file_id=file_id,
        name=str(meta.get("name") or ""),
        revision=revision_for(
            sha256=str(meta.get("sha256") or ""),
            converter_name=str(meta.get("converter_name") or ""),
            converter_version=str(meta.get("converter_version") or ""),
            config=meta.get("converter_config") or {},
            staged_at=str(meta.get("staged_at") or ""),
        ),
        visibility=visibility,
        acl=frozenset(acl),
        text=text,
        conversion_state=str(meta.get("conversion_state") or PENDING),
        message_ts=str(meta.get("origin_message_ts") or ""),
        permalink=str(meta.get("permalink") or ""),
        filetype=str(meta.get("filetype") or ""),
        sha256=str(meta.get("sha256") or ""),
        staged_at=str(meta.get("staged_at") or ""),
        converted_at=str(meta.get("converted_at") or ""),
        converter_name=str(meta.get("converter_name") or ""),
        converter_version=str(meta.get("converter_version") or ""),
        coverage_read=_int_or_none(meta.get("coverage_read")),
        coverage_total=_int_or_none(meta.get("coverage_total")),
        error_code=str(meta.get("error_code") or ""),
    )


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _strip_local_header(text: str) -> str:
    """`extracted.md` 의 로컬 머리말과 제목 줄을 뗀다.

    그 둘은 사람이 staging 을 열어 볼 때를 위한 것이고, 정본에는 프론트매터가
    같은 정보를 더 정확히 담는다. 남겨 두면 검색에 제목이 본문으로 잡힌다.
    """
    lines = text.splitlines()
    while lines and (
        lines[0].startswith("<!--") or lines[0].startswith("# ") or not lines[0].strip()
    ):
        lines.pop(0)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# 렌더
# ---------------------------------------------------------------------------

def render(doc: AttachmentDoc) -> str:
    """정본 문서 한 장.

    프론트매터는 `ArchiveStore` 가 요구하는 네 칸(`workspace`·`channel`·
    `visibility`·`acl`)을 반드시 담는다 — 없으면 스키마 오류로 문서 전체가
    검색에서 빠지고, 그 사실은 오류 없이 「자료가 없음」 으로 보인다.
    """
    # **대괄호로 싼다.** 채널명은 `#` 로 시작하는데, 맨 값으로 적으면
    # 프론트매터 파서가 그 자리부터 주석으로 읽어 **ACL 이 통째로 빈다**
    # (`store._strip_comment`). 빈 ACL 은 「제한 없음」 으로 읽힐 수 있으므로
    # 그건 권한이 조용히 넓어지는 길이다. 채널 문서 writer 가 이미 이 모양이다.
    acl = "[" + ", ".join(sorted(doc.acl)) + "]" if doc.acl else "[]"
    head = [
        "---",
        f"workspace: {doc.workspace}",
        # 채널명은 `#` 로 시작할 수 있다. 따옴표를 안 씌우면 파서가 주석으로 읽어
        # 표시명이 빈 문자열이 되고, 그러면 관계없는 문서가 한 채널로 합쳐진다(B-61).
        f'channel: "{doc.channel}"',
        f"channel_id: {doc.channel_id}",
        f"visibility: {doc.visibility}",
        f"acl: {acl}",
        f"schema_version: {SCHEMA_VERSION}",
        "kind: attachment",
        f"file_id: {doc.file_id}",
        f'file_name: "{doc.name}"',
        f"revision: {doc.revision}",
        f"conversion_state: {doc.conversion_state}",
    ]
    if doc.filetype:
        head.append(f"filetype: {doc.filetype}")
    if doc.message_ts:
        head.append(f"message_ts: {doc.message_ts}")
    if doc.permalink:
        head.append(f"permalink: {doc.permalink}")
    if doc.sha256:
        head.append(f"sha256: {doc.sha256}")
    if doc.staged_at:
        head.append(f"staged_at: {doc.staged_at}")
    if doc.converted_at:
        head.append(f"converted_at: {doc.converted_at}")
    if doc.converter_name:
        head.append(f"converter_name: {doc.converter_name}")
    if doc.converter_version:
        head.append(f"converter_version: {doc.converter_version}")
    if doc.coverage_total is not None:
        head.append(f"coverage_read: {doc.coverage_read}")
        head.append(f"coverage_total: {doc.coverage_total}")
    if doc.error_code:
        head.append(f"error_code: {doc.error_code}")
    head.append("---")

    body = [f"# {doc.name or doc.file_id}", ""]
    if doc.usable:
        if doc.partial:
            # **「본문이 나왔다」 와 「다 읽었다」 는 다르다.** 부분 변환본을 전체인
            # 것처럼 두면 없는 내용을 「없다」 고 답하게 된다.
            body.append(
                f"> 부분 변환본입니다"
                f"{_coverage_note(doc)}. 원본에 더 있을 수 있습니다."
            )
            body.append("")
        body.append(doc.text.rstrip())
    else:
        # 본문이 없어도 **문서는 남긴다.** 「PII 로 막혔다」 와 「그런 파일이 없다」
        # 는 사람이 할 일이 다르다.
        body.append(f"{NO_BODY_PREFIX} {_state_note(doc)}")
    return "\n".join([*head, "", *body]) + "\n"


def _coverage_note(doc: AttachmentDoc) -> str:
    if doc.coverage_total:
        return f"({doc.coverage_read}/{doc.coverage_total})"
    return ""


def _state_note(doc: AttachmentDoc) -> str:
    return {
        BLOCKED: "개인정보 판정으로 수집에서 제외됐습니다",
        FAILED: "변환에 실패했습니다",
        UNSUPPORTED: "변환할 수 없는 형식입니다",
        PENDING: "아직 변환되지 않았습니다",
    }.get(doc.conversion_state, f"상태 {doc.conversion_state}")


# ---------------------------------------------------------------------------
# 중복 방지
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LegacyIndex:
    """옛 raw 에 이미 본문이 박혀 있는 첨부들.

    채널마다 파일명 집합. file ID 로 맞추고 싶지만 **옛 줄에는 없다** — 그래서
    이름으로 다리를 놓는다(모듈 머리말).
    """

    #: 키는 `(workspace, channel_id)` 다. 채널 ID 만 쓰면 다른 워크스페이스의
    #: 같은 채널 ID 가 이 채널 정본을 지운다 — 회사 경계를 넘는 실수다(원칙 4).
    by_channel: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict)

    def has(self, workspace: str, channel_id: str, name: str) -> bool:
        if not name:
            return False
        return name in self.by_channel.get(
            (workspace or "", channel_id or ""), frozenset()
        )


def legacy_index(docs) -> LegacyIndex:
    """채널 원문에서 `[첨부추출:…]`·`[첨부본문:…]` 이름을 걷는다."""
    found: dict[tuple[str, str], set[str]] = {}
    for doc in docs:
        key = (
            str(getattr(doc, "workspace", "") or ""),
            str(getattr(doc, "channel_id", "") or ""),
        )
        for line in getattr(doc, "raw_lines", []):
            match = LEGACY_LINE.match(str(getattr(line, "text", "")))
            if match:
                found.setdefault(key, set()).add(match.group("name").strip())
    return LegacyIndex({key: frozenset(value) for key, value in found.items()})


def pick_current(docs: list[AttachmentDoc]) -> list[AttachmentDoc]:
    """file ID 마다 **가장 최근 revision 하나**만 남긴다.

    옛 판을 지우지 않는 이유는 되짚기 위해서지 근거로 쓰기 위해서가 아니다.
    둘 다 근거로 세면 같은 첨부가 두 번 나오고, 그중 하나는 낡았다.
    """
    newest: dict[str, AttachmentDoc] = {}
    for doc in docs:
        current = newest.get(doc.file_id)
        if current is None or _newer(doc, current):
            newest[doc.file_id] = doc
    return sorted(newest.values(), key=lambda d: (d.channel_id, d.file_id))


def _newer(candidate: AttachmentDoc, current: AttachmentDoc) -> bool:
    """`staged_at` 이 앞서면 최신. 없으면 바꾸지 않는다 — 모르는 것으로 아는 것을
    덮으면 최신이 옛것으로 밀린다."""
    if not candidate.staged_at:
        return False
    if not current.staged_at:
        return True
    return candidate.staged_at > current.staged_at


def usable_evidence(
    attachments: list[AttachmentDoc], legacy: LegacyIndex
) -> list[AttachmentDoc]:
    """근거로 쓸 첨부. **같은 첨부를 두 번 세지 않는다.**

    - 본문이 없는 것은 뺀다(상태는 문서에 남아 있고, 그건 화면이 본다)
    - raw 에 이미 같은 이름이 박혀 있으면 뺀다 — 그쪽이 이미 근거로 잡힌다
    - file ID 당 최신 revision 하나만
    """
    current = pick_current([doc for doc in attachments if doc.usable])
    return [
        doc for doc in current
        if not legacy.has(doc.workspace, doc.channel_id, doc.name)
    ]
