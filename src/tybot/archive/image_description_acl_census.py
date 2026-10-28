"""이미지 후보의 **권한 분포만** 센다. 읽기 전용 진단기다.

## 왜 필요한가

전송 직전 판정(`image_description_clearance`)은 빈 `acl` 을 거절한다 — 빈 값은
「공개」 가 아니라 **모름**이기 때문이다(원칙 3). 옳은 선택이지만 **대가를 재지
않았다.** 정말로 ACL 이 빈 문서가 아카이브에 많다면 그 사진들은 영원히 후보로만
남고, 운영자는 「왜 하나도 안 나오나」 만 보게 된다.

이 모듈은 그 분포를 센다. 고치지 않고, 판정하지 않고, **숫자만** 돌려준다.

## 담지 않는 것

본문·파일명·경로·사용자·채널 이름을 **결과에 담지 않는다.** 돌려주는 것은
칸 이름과 개수뿐이다 — 좌표조차 없다.

좌표를 담지 않는 이유는 편의가 아니다. 「어느 채널이 비어 있나」 를 알려면
채널 멤버십을 아는 사람이 봐야 하는데, 집계는 그 권한을 거치지 않고 만들어진다.
집계에 좌표를 붙이는 순간 **권한 없는 사람이 집계로 채널 구성을 안다.**
고칠 자리를 찾는 것은 복구 도구(`attachment_repair`)의 몫이고, 그쪽은 권한을
지나서 돈다.

## 원본을 열지 않는다

`AttachmentDoc` 에 적힌 것만 본다. 바이트를 읽지 않고 네트워크를 열지 않는다.
진단이 원본을 열면 「세어 보기만 했다」 가 아니게 된다.

## 「이미지인가」 는 선별기와 같은 규칙으로 센다

`image_description_source.FILETYPE_MIME` 을 그대로 쓴다. 규칙을 여기 베껴 적으면
선별 쪽이 바뀐 날 **둘이 갈라지고**, 갈라진 집계는 없는 문제를 가리킨다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .image_description_source import FILETYPE_MIME

#: 세는 칸. **하나의 문서는 정확히 한 칸**에 들어간다.
NOT_AN_IMAGE = "not_an_image"
UNKNOWN_VISIBILITY = "unknown_visibility"
PUBLIC_EMPTY_ACL = "public_empty_acl"
PUBLIC_WITH_ACL = "public_with_acl"
PRIVATE_EMPTY_ACL = "private_empty_acl"
PRIVATE_WITH_ACL = "private_with_acl"
#: 최소 집계를 넘는 칸 하나. `acl` 이 집합이 아닌 경우다 — 빈 것과 **섞으면
#: 안 된다.** 섞으면 「고치면 열리는 수」 를 실제보다 크게 보고한다.
UNREADABLE_ACL = "unreadable_acl"

BUCKETS = (
    NOT_AN_IMAGE,
    UNKNOWN_VISIBILITY,
    PUBLIC_EMPTY_ACL,
    PUBLIC_WITH_ACL,
    PRIVATE_EMPTY_ACL,
    PRIVATE_WITH_ACL,
    UNREADABLE_ACL,
)

#: 우리가 아는 공개 범위. 그 밖은 **모름**이다(`attachment_reader` 와 같은 값).
PUBLIC = "public"
PRIVATE = "private"


@dataclass(frozen=True)
class AclCensus:
    """칸별 개수. **좌표도 이름도 담지 않는다.**"""

    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def image_total(self) -> int:
        """이미지로 센 것 전부. 「후보가 될 수 있었던 수」 의 모수다."""
        return self.total - self.counts.get(NOT_AN_IMAGE, 0)

    @property
    def blocked_by_empty_acl(self) -> int:
        """**ACL 만 채우면 열리는 수.** 이 숫자가 이 모듈을 만든 이유다."""
        return (
            self.counts.get(PUBLIC_EMPTY_ACL, 0)
            + self.counts.get(PRIVATE_EMPTY_ACL, 0)
        )

    @property
    def rows(self) -> tuple[tuple[str, int], ...]:
        """화면에 찍는 순서. `BUCKETS` 순서를 쓴다 — 개수 순으로 정렬하면
        실행마다 줄이 움직여 사람이 변화를 못 읽는다."""
        return tuple((name, self.counts.get(name, 0)) for name in BUCKETS)


def take_census(docs) -> AclCensus:
    """`AttachmentDoc` 목록을 한 번 훑어 칸별로 센다.

    예외를 내지 않는다. 진단이 한 줄 때문에 멈추면 **나머지 분포를 못 본다** —
    읽을 수 없는 값은 그 자체로 칸이 있다(`unreadable_acl`).
    """
    counts = dict.fromkeys(BUCKETS, 0)
    for doc in docs or ():
        counts[_bucket(doc)] += 1
    return AclCensus(counts=counts)


def _bucket(doc) -> str:
    if not _is_image(doc):
        return NOT_AN_IMAGE
    visibility = str(getattr(doc, "visibility", "") or "").strip().lower()
    if visibility not in (PUBLIC, PRIVATE):
        # 빈 값도, 모르는 값도 여기다. 「공개」 로 내려 읽지 않는다.
        return UNKNOWN_VISIBILITY
    acl = getattr(doc, "acl", None)
    if not _readable(acl):
        return UNREADABLE_ACL
    if acl:
        return PUBLIC_WITH_ACL if visibility == PUBLIC else PRIVATE_WITH_ACL
    return PUBLIC_EMPTY_ACL if visibility == PUBLIC else PRIVATE_EMPTY_ACL


def _is_image(doc) -> bool:
    """선별기와 **같은 규칙**이다. 확장자 표기만 다듬는다."""
    filetype = str(getattr(doc, "filetype", "") or "").strip().lower()
    return filetype in FILETYPE_MIME


def _readable(acl) -> bool:
    """`acl` 이 **집합으로 적힌** 값인가.

    문자열은 순회하면 글자 하나하나가 멤버가 된다. 비어 있지 않은 문자열을
    「ACL 이 있다」 로 세면 **없는 권한을 있다고 보고**하게 된다.
    """
    return isinstance(acl, (set, frozenset))


__all__ = [
    "BUCKETS",
    "NOT_AN_IMAGE",
    "PRIVATE_EMPTY_ACL",
    "PRIVATE_WITH_ACL",
    "PUBLIC_EMPTY_ACL",
    "PUBLIC_WITH_ACL",
    "UNKNOWN_VISIBILITY",
    "UNREADABLE_ACL",
    "AclCensus",
    "take_census",
]
