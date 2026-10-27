"""Archiver 원본 메타데이터에서 **이미지 설명 후보**를 고른다.

## 하는 일과 안 하는 일

| 한다 | 하지 않는다 |
|---|---|
| `AttachmentDoc` 을 읽어 후보와 **탈락 사유**를 낸다 | 파일·DB·네트워크를 건드린다 |
| 좌표와 원본 해시를 그대로 옮긴다 | 권한을 판정한다 |
| 아는 사실만 적는다 | 모르는 것을 안전으로 바꾼다 |

순수 함수다. 바이트를 읽지 않고 모델을 부르지 않는다. 여기서 나온 후보는
`image_description_policy.preflight_image_description` 과 작업 원장을 **다시**
지나야 전송된다 — 이 모듈은 「볼 만한 것이 어디 있나」 까지만 말한다.

## 왜 「안전」 을 말하지 않나

이 선별기는 **PII 가 없다고 말할 수 없다.** 아카이브의 PII 검사는 변환된
**글자**를 본다. 사진은 OCR 로 글자를 뽑고 그 글자를 검사하는데, 신분증을 찍은
사진이 OCR 에 실패하면 검사는 **아무것도 못 본 채로 통과**한다.

그래서 이 모듈이 내보내는 것은 판정이 아니라 사실 하나다 — `text_screened`,
즉 **글자 검사가 실제로 돌았는가.** `True` 라도 그것은 「그 사진에 개인정보가
없다」 가 아니다. 판정은 전송 직전에 사람·정책이 한다(원칙 3·5).

반대로 **이미 `blocked` 로 판정된 첨부는 여기서 끊는다.** 아는 위험을 통과시킬
이유는 없다.

## 봇이 만든 이미지

`AttachmentDoc` 에는 업로더가 없다. 수집 단계에서 `bot_id`·`app_id` 로 가리고
그 사실은 메타데이터에 남지 않는다. 그래서 **여기서는 알 수 없다.**

알 수 없는 것을 사람 원본으로 취급하면 봇이 만든 그림을 봇이 다시 설명하고,
그 설명이 색인에 들어가 다음 답변의 검색 결과가 된다 — 원칙 1이 막으려는 고리가
그대로 생긴다. 그래서 **호출자가 「사람이 올린 것」 이라고 짚어 준 파일만** 후보다.
안 짚으면 후보가 아니다.

## 같은 사진이 두 채널에 있으면 둘 다 후보다

채널마다 멤버가 다르다. 한 채널에만 색인하고 다른 채널 검색이 그것을 집으면
**권한 없는 사람이 설명으로 내용을 안다**(원칙 3). 중복 제거는 채널 안에서만
한다 — 모델을 두 번 부르는 비용은 그 대가다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .attachment_doc import BLOCKED, CONVERTED, PARTIAL, PENDING, AttachmentDoc
from .image_description import ALLOWED_MIME

#: Slack `filetype` → 보낼 MIME. **코어가 받는 것만** 적는다.
FILETYPE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

if set(FILETYPE_MIME.values()) - set(ALLOWED_MIME):
    # 코어가 받지 않는 형식을 후보로 올리면 승인·예약을 다 지나고 **호출 직전에**
    # `ImageInputError` 로 떨어진다. 그 자리는 이미 작업 원장에 줄이 생긴 뒤다.
    raise RuntimeError(
        "image_description.ALLOWED_MIME 과 어긋났습니다: "
        + ",".join(sorted(set(FILETYPE_MIME.values()) - set(ALLOWED_MIME)))
    )

#: 한 번에 고르는 후보 수의 기본 상한. 배치가 커지면 사람이 못 본다.
DEFAULT_LIMIT = 200

_SHA256 = re.compile(r"^[a-f0-9]{64}$")

# --- 탈락 사유 ---------------------------------------------------------------
#
# **비민감 코드만.** 운영 화면과 로그에 그대로 나간다.
SKIP_NOT_AN_IMAGE = "not_an_image"
SKIP_NO_COORDINATES = "missing_coordinates"
SKIP_NO_SHA256 = "missing_original_sha256"
SKIP_BAD_SHA256 = "invalid_original_sha256"
SKIP_NO_ACL = "missing_acl"
SKIP_PII_BLOCKED = "pii_blocked"
SKIP_PENDING = "conversion_pending"
SKIP_NOT_HUMAN = "not_known_human_upload"
SKIP_ALREADY_INDEXED = "already_indexed"
SKIP_DUPLICATE = "duplicate_in_batch"
SKIP_OVER_LIMIT = "over_batch_limit"

SKIP_CODES = (
    SKIP_NOT_AN_IMAGE,
    SKIP_NO_COORDINATES,
    SKIP_NO_SHA256,
    SKIP_BAD_SHA256,
    SKIP_NO_ACL,
    SKIP_PII_BLOCKED,
    SKIP_PENDING,
    SKIP_NOT_HUMAN,
    SKIP_ALREADY_INDEXED,
    SKIP_DUPLICATE,
    SKIP_OVER_LIMIT,
)


@dataclass(frozen=True)
class ImageCandidate:
    """설명해 볼 만한 원본 하나. **메타데이터에 적힌 것뿐이다.**

    바이트를 들고 있지 않다. 원본을 읽어 `ImageInput` 을 만드는 것은 승인·권한을
    다시 확인한 뒤 worker 가 할 일이다 — 여기서 바이트까지 들면 선별이 곧
    적재가 되고, 승인 전에 원본이 메모리로 올라온다.
    """

    workspace: str
    channel_id: str
    file_id: str
    original_sha256: str
    filetype: str
    mime: str
    revision: str = ""
    message_ts: str = ""
    permalink: str = ""
    visibility: str = ""
    acl: frozenset[str] = frozenset()
    #: 변환 본문이 PII 글자 검사를 **실제로 거쳤나.**
    #:
    #: **「개인정보가 없다」 가 아니다.** 사진의 글자가 OCR 에 안 잡히면 검사는
    #: 아무것도 못 본 채 지나간다. 전송 전 판정의 입력일 뿐이다.
    text_screened: bool = False
    conversion_state: str = ""

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        """채널 **안에서만** 같은 원본을 한 번 센다."""
        return (self.workspace, self.channel_id, self.original_sha256)


@dataclass(frozen=True)
class SkippedSource:
    """왜 안 골랐나. **파일 이름·본문은 담지 않는다.**"""

    workspace: str
    channel_id: str
    file_id: str
    reason: str


@dataclass(frozen=True)
class Selection:
    """한 배치의 결과. **고른 것과 버린 것을 함께** 돌려준다.

    버린 쪽을 안 돌려주면 「후보 0건」 과 「전부 걸러짐」 이 같아 보인다. 둘은
    사람이 할 일이 완전히 다르다 — 앞은 기다리는 것이고 뒤는 고치는 것이다.
    """

    candidates: tuple[ImageCandidate, ...] = ()
    skipped: tuple[SkippedSource, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        """사유별 건수. 운영 화면이 읽는 값이다."""
        out: dict[str, int] = {}
        for item in self.skipped:
            out[item.reason] = out.get(item.reason, 0) + 1
        return out


def _sort_key(doc: AttachmentDoc) -> tuple:
    """**입력 순서에 기대지 않는다.** 같은 입력이면 같은 배치가 나와야 한다."""
    return (
        doc.workspace or "",
        doc.channel_id or "",
        doc.converted_at or doc.staged_at or "",
        doc.file_id or "",
        doc.revision or "",
    )


def _skip(doc: AttachmentDoc, reason: str) -> SkippedSource:
    return SkippedSource(
        workspace=doc.workspace or "",
        channel_id=doc.channel_id or "",
        file_id=doc.file_id or "",
        reason=reason,
    )


def _reason(doc: AttachmentDoc, *, human: bool, indexed: bool) -> str:
    """이 첨부가 후보가 못 되는 첫 번째 이유. 없으면 빈 문자열.

    순서가 곧 우선순위다. **아는 위험(PII)을 먼저** 적는다 — 「이미지가 아님」 이
    먼저 나오면 차단된 첨부가 운영 화면에서 평범한 비이미지로 보인다.
    """
    if (doc.conversion_state or "") == BLOCKED:
        return SKIP_PII_BLOCKED
    filetype = (doc.filetype or "").strip().lower()
    if filetype not in FILETYPE_MIME:
        return SKIP_NOT_AN_IMAGE
    if not all((doc.workspace, doc.channel_id, doc.file_id)):
        return SKIP_NO_COORDINATES
    digest = (doc.sha256 or "").strip().lower()
    if not digest:
        return SKIP_NO_SHA256
    if not _SHA256.match(digest):
        # 길이·글자가 안 맞으면 **어느 바이트의 승인인지 못 적는다.**
        return SKIP_BAD_SHA256
    if not (doc.visibility or "").strip():
        # 첨부는 채널 ACL 을 상속한다. 그것이 비면 묶을 자리가 없다(원칙 3).
        return SKIP_NO_ACL
    if (doc.conversion_state or "") == PENDING:
        # 아직 아무것도 안 돈 상태다. 글자 검사도 안 돌았고 원본이 자리 잡았는지도
        # 모른다 — **기다리는 쪽**이 맞다.
        return SKIP_PENDING
    if not human:
        return SKIP_NOT_HUMAN
    if indexed:
        return SKIP_ALREADY_INDEXED
    return ""


def select_image_candidates(
    docs,
    *,
    human_file_ids,
    already_indexed=(),
    limit: int = DEFAULT_LIMIT,
) -> Selection:
    """원본 메타데이터 → 설명 후보.

    `human_file_ids` 는 **사람이 올린 것이 확인된** 파일 ID 다. 여기에 없는 파일은
    봇이 만들었을 수도, 그냥 모르는 것일 수도 있다 — 둘 다 후보가 아니다.
    `AttachmentDoc` 에는 업로더가 없어 이 모듈이 스스로 알 길이 없다.

    `already_indexed` 는 `(workspace, channel_id, original_sha256)` 집합이다.
    작업 원장이 중복을 막지만, 이미 끝난 것을 후보로 올리면 **배치 상한을 먹고**
    아직 안 본 사진이 밀린다.

    예외를 내지 않는다. 못 고른 것은 전부 `skipped` 로 **이름을 달아** 돌려준다.
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")

    human = frozenset(human_file_ids or ())
    indexed = frozenset(already_indexed or ())

    candidates: list[ImageCandidate] = []
    skipped: list[SkippedSource] = []
    seen: set[tuple[str, str, str]] = set()
    overflow: list[AttachmentDoc] = []

    for doc in sorted(docs, key=_sort_key):
        digest = (doc.sha256 or "").strip().lower()
        key = (doc.workspace or "", doc.channel_id or "", digest)
        reason = _reason(
            doc,
            human=doc.file_id in human,
            indexed=key in indexed,
        )
        if reason:
            skipped.append(_skip(doc, reason))
            continue
        if key in seen:
            # 같은 채널의 같은 원본이다. 채널이 다르면 여기 안 걸린다 — 채널마다
            # 멤버가 다르고, 한 줄만 색인하면 다른 채널이 그것을 집는다.
            skipped.append(_skip(doc, SKIP_DUPLICATE))
            continue
        if len(candidates) >= limit:
            # **버리지 않고 다음 배치로 남긴다.** 조용히 사라지면 그 사진은
            # 아무도 다시 안 본다.
            overflow.append(doc)
            continue
        seen.add(key)
        state = doc.conversion_state or ""
        filetype = (doc.filetype or "").strip().lower()
        candidates.append(
            ImageCandidate(
                workspace=doc.workspace,
                channel_id=doc.channel_id,
                file_id=doc.file_id,
                original_sha256=digest,
                filetype=filetype,
                mime=FILETYPE_MIME[filetype],
                revision=doc.revision or "",
                message_ts=doc.message_ts or "",
                permalink=doc.permalink or "",
                visibility=doc.visibility or "",
                acl=frozenset(doc.acl or ()),
                text_screened=state in (CONVERTED, PARTIAL),
                conversion_state=state,
            )
        )
    skipped.extend(_skip(doc, SKIP_OVER_LIMIT) for doc in overflow)
    return Selection(candidates=tuple(candidates), skipped=tuple(skipped))


def confirmed_human_file_ids(docs) -> frozenset[str]:
    """정본 provenance가 확인된 파일 ID만 돌려준다. 없는 옛 값은 허용하지 않는다."""
    return frozenset(
        doc.file_id
        for doc in docs
        if doc.human_upload_confirmed is True and doc.file_id
    )


__all__ = [
    "DEFAULT_LIMIT",
    "FILETYPE_MIME",
    "SKIP_ALREADY_INDEXED",
    "SKIP_BAD_SHA256",
    "SKIP_CODES",
    "SKIP_DUPLICATE",
    "SKIP_NOT_AN_IMAGE",
    "SKIP_NOT_HUMAN",
    "SKIP_NO_ACL",
    "SKIP_NO_COORDINATES",
    "SKIP_NO_SHA256",
    "SKIP_OVER_LIMIT",
    "SKIP_PENDING",
    "SKIP_PII_BLOCKED",
    "ImageCandidate",
    "Selection",
    "SkippedSource",
    "confirmed_human_file_ids",
    "select_image_candidates",
]
