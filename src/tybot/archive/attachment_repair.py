"""이미 쓰여 있는 첨부 정본의 **권한 칸을 고친다.** 못 고치면 재색인을 막는다.

결정: 2026-09-26 오너 지시 2번(빈 ACL·잘못된 visibility 집계·복구).

## 왜 고칠 것이 생겼나

`render` 가 ACL 을 대괄호 없이 적던 때가 있었다. 채널명은 `#` 로 시작하므로
프론트매터 파서가 그 자리부터 주석으로 읽었고, **ACL 이 통째로 비었다.** 오류는
안 났다. 빈 ACL 은 읽는 쪽에서 「제한 없음」 으로 읽힐 수 있으니, 이건 권한이
조용히 넓어지는 길이다(절대 원칙 3 — 막는 쪽이 기본값).

reader 는 그런 문서를 이제 거절한다(`attachment_reader.validate`). 거절은 안전하지만
**그 자료가 답변에서 사라진다**, 그리고 사라진 것과 없는 것은 화면에서 같아 보인다.
그래서 세어서 고친다.

## 무엇을 근거로 고치나

첨부는 그 채널에 올라온 것이므로 **채널 문서의 권한을 상속한다**(`AttachmentDoc`
머리말). 고치는 값도 거기서 온다 — 정본 자신이 말하는 것을 믿지 않는다.

## 모르면 안 고친다

채널 문서를 못 찾거나, 채널 문서 자신이 비공개인데 ACL 이 비어 있으면 **고칠 값이
없다.** 그때 추측해서 채우면 권한을 지어내는 것이다. 그런 문서가 하나라도 있으면
재색인을 막는다(`blocking_reason`) — 절반만 고친 상태로 색인하면, 사라진 자료와
아직 안 고친 자료를 구분할 방법이 없어진다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .attachment_reader import VALID_VISIBILITY, load, source_files, validate

log = logging.getLogger("tybot.archive.attachment_repair")


@dataclass(frozen=True)
class Finding:
    """정본 한 장의 진단. `fix` 가 없으면 **사람이 봐야 하는 것**이다."""

    path: Path
    workspace: str
    channel_id: str
    problem: str
    #: 고칠 값 `(visibility, acl)`. 모르면 `None` — 추측해 채우지 않는다.
    fix: tuple[str, frozenset[str]] | None = None
    #: `attachment_reader.Problem.code`. 문구가 아니라 이걸로 분류한다.
    code: str = ""

    @property
    def fixable(self) -> bool:
        return self.fix is not None


def channel_rights(channel_docs) -> dict[tuple[str, str], tuple[str, frozenset[str]]]:
    """`(워크스페이스, 채널 ID)` → `(visibility, acl)`.

    같은 채널에 대해 문서들이 **다른 권한을 말하면 그 채널은 뺀다.** 어느 쪽이
    참인지 고를 근거가 없고, 넓은 쪽을 고르면 권한이 넓어진다.
    """
    seen: dict[tuple[str, str], set[tuple[str, frozenset[str]]]] = {}
    for doc in channel_docs:
        key = (
            str(getattr(doc, "workspace", "") or ""),
            str(getattr(doc, "channel_id", "") or ""),
        )
        if not key[1]:
            continue
        rights = (
            str(getattr(doc, "visibility", "") or ""),
            frozenset(getattr(doc, "acl", ()) or ()),
        )
        seen.setdefault(key, set()).add(rights)
    return {key: next(iter(v)) for key, v in seen.items() if len(v) == 1}


def survey(root: Path | str, channel_docs) -> list[Finding]:
    """근거가 못 되는 정본 전부. **정상 문서는 나오지 않는다.**

    판정은 `attachment_reader.validate` 하나가 한다. 여기서 검사를 다시 쓰면
    reader 가 거절하는데 여기서는 「깨끗하다」 고 말하는 날이 오고, 그날 재색인이
    **사라진 자료를 못 본 채** 지나간다.

    고칠 수 있는 것은 **권한 칸뿐**이다(`Problem.rights`). 필수 칸 누락·경로
    불일치·모르는 상태는 채널 문서로 되돌릴 수 있는 값이 아니다 — 세서 막는다.
    """
    rights = channel_rights(channel_docs)
    out: list[Finding] = []
    for path in source_files(root):
        doc = load(path)
        if doc is None:
            out.append(Finding(path, "", "", "정본으로 읽히지 않는다", code="unreadable"))
            continue
        problem = validate(doc, path, root)
        if problem is None:
            continue
        key = (doc.workspace, doc.channel_id)
        out.append(Finding(
            path, doc.workspace, doc.channel_id, problem.message,
            _fix(rights.get(key)) if problem.rights else None,
            code=problem.code,
        ))
    return out


def _fix(rights: tuple[str, frozenset[str]] | None) -> tuple[str, frozenset[str]] | None:
    """채널 권한을 고칠 값으로 쓸 수 있나.

    채널 자신이 **비공개인데 ACL 이 비면** 그 값으로는 정본도 똑같이 깨진다.
    고친 척만 하고 같은 문제가 남는다.
    """
    if rights is None:
        return None
    visibility, acl = rights
    if visibility not in VALID_VISIBILITY:
        return None
    if visibility == "private" and not acl:
        return None
    return (visibility, acl)


def blocking_reason(findings: list[Finding]) -> str:
    """재색인을 막을 이유. 없으면 빈 문자열.

    **고칠 수 있는 것이 남아 있어도 막는다.** 색인은 지금 읽히는 문서만 넣으므로,
    안 고친 문서는 「없는 자료」 로 색인되고 나중에 고쳐도 그 사실이 안 보인다.

    권한 문제가 아닌 것(필수 칸 누락·경로 불일치·모르는 상태)은 복구 대상이
    아니므로 **사람을 부른다.** 그걸 「고치면 된다」 로 적으면 스크립트를 돌리고
    건수가 안 줄어드는 자리에서 멈춘다.
    """
    if not findings:
        return ""
    blocked = [f for f in findings if not f.fixable]
    head = f"근거로 못 쓰는 정본 {len(findings)}건이 있습니다"
    if blocked:
        kinds = ", ".join(sorted({f.code or "unknown" for f in blocked}))
        return (
            f"{head} — 그중 {len(blocked)}건은 채널 권한으로 되돌릴 수 없습니다"
            f"({kinds}). 사람이 확인해야 합니다."
        )
    return (
        f"{head} — 전부 채널 권한으로 되돌릴 수 있습니다."
        " 복구를 먼저 반영하세요: scripts/repair_attachment_rights.py --apply"
    )


# ---------------------------------------------------------------------------
# 고치기
# ---------------------------------------------------------------------------

def apply_fix(finding: Finding) -> bool:
    """프론트매터의 `visibility`·`acl` **두 줄만** 고친다.

    본문은 손대지 않는다. 정본의 본문은 변환 결과이고, 그걸 고치는 것은 원문
    편집과 같은 무게다(절대 원칙 1).
    """
    if finding.fix is None:
        return False
    visibility, acl = finding.fix
    try:
        text = finding.path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("정본을 읽지 못했다 %s: %s", finding.path, type(exc).__name__)
        return False
    fixed = rewrite_rights(text, visibility, acl)
    if fixed is None or fixed == text:
        return False
    tmp = finding.path.with_suffix(".md.tmp")
    try:
        tmp.write_text(fixed, encoding="utf-8")
        tmp.replace(finding.path)
    except OSError as exc:
        log.error("정본을 고치지 못했다 %s: %s", finding.path, type(exc).__name__)
        return False
    return True


def rewrite_rights(text: str, visibility: str, acl: frozenset[str]) -> str | None:
    """프론트매터만 바꾼 전체 문서. 프론트매터가 없으면 `None`.

    `render` 와 **같은 모양**으로 적는다 — 대괄호. 다르게 적으면 지금 고친 문서가
    다음 파서에서 또 빈 ACL 이 된다.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None

    rendered = "[" + ", ".join(sorted(acl)) + "]" if acl else "[]"
    head = lines[1:end]
    wrote_visibility = wrote_acl = False
    for index, line in enumerate(head):
        if line.startswith("visibility:"):
            head[index] = f"visibility: {visibility}"
            wrote_visibility = True
        elif line.startswith("acl:"):
            head[index] = f"acl: {rendered}"
            wrote_acl = True
    if not wrote_visibility:
        head.append(f"visibility: {visibility}")
    if not wrote_acl:
        head.append(f"acl: {rendered}")
    return "\n".join(["---", *head, "---", *lines[end + 1:]]) + "\n"
