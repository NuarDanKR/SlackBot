"""사진 설명을 **저장하기 직전에** 다시 본다. 프롬프트 준수를 믿지 않는다.

## 왜 한 번 더 보나

`image_description` 의 프롬프트는 사람 식별·번호판 전사·수량 추정을 금지한다.
그러나 프롬프트는 **부탁이지 보장이 아니다.** 모델이 안 지킨 날은 그 설명이
그대로 파생 색인에 들어가고, 색인은 원문보다 **넓게 읽힌다** — 원본 사진은
채널 ACL 안에 있지만 설명은 검색 결과로 먼저 보인다.

그래서 저장 직전에 **결정론적으로** 본다. 모델이 무엇을 적었든 여기서 같은
규칙이 돈다.

## 세 가지 결말

| 판정 | 뜻 | 저장 |
|---|---|---|
| `allow` | 설명과 태그를 색인한다 | 설명 + 태그 |
| `withhold_description` | 설명은 빼고 태그·코드만 남긴다 | 태그만 |
| `block` | 아무것도 저장하지 않는다 | 없음 |

가운데가 따로 있는 이유가 있다. 사람이 찍힌 사진은 **색인에서 지우면 안 되고**
(그 사진이 있다는 사실은 PII 검토에 필요하다) **설명을 남겨도 안 된다**. 둘 중
하나만 두면 그 사진은 사라지거나 설명이 돌아다닌다.

## 「차단」 과 「설명 제외」 를 가른 기준

지시의 3번(이메일·전화·차량번호·숫자)은 **차단**, 4번(민감 uncertainty)은
**설명 본문을 색인하지 않는다** 로 말이 다르다. 그대로 따랐다.

까닭도 그 쪽이 맞다. 4번은 모델이 **규칙을 지켜서** 「여기 사람이 보입니다」 라고
알려 준 경우다 — 그 신호는 쓸모가 있고 태그는 남길 만하다. 3번은 모델이
**규칙을 어긴** 경우다. 같은 응답에서 나온 태그를 믿을 근거가 없다.

## 숫자를 통째로 막는다

이미지 설명은 공정률·수량·치수·번호 전사를 **전부** 금지한다. 그러니 설명에
숫자가 들어갈 이유가 없다. 「굴착기 2대」 같은 문장이 허용되기 시작하면 어디까지가
세는 것이고 어디부터가 추정인지 **코드가 가를 수 없다.**

## 로그

차단 사유는 **코드만** 남긴다. 설명 원문도, 무엇이 걸렸는지의 문자열도 넣지
않는다 — 차단 로그에 탐지 문자열을 적으면 **막으려던 값이 로그로 샌다.**
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from . import image_description as core

logger = logging.getLogger(__name__)

VERDICT_ALLOW = "allow"
VERDICT_WITHHOLD = "withhold_description"
VERDICT_BLOCK = "block"

# --- 사유 코드 ---------------------------------------------------------------
#
# **비민감 코드만.** 운영 목록 API 와 로그에 그대로 나간다.
CODE_UNKNOWN_STATUS = "unknown_status"
CODE_STATUS_FAILED = "status_failed"
CODE_RRN = "rrn_shape"
CODE_EMAIL = "email_shape"
CODE_PHONE = "phone_shape"
CODE_PLATE = "plate_shape"
CODE_DIGITS = "digits_present"
CODE_SENSITIVE = "sensitive_uncertainty"
CODE_UNKNOWN_UNCERTAINTY = "unknown_uncertainty"
CODE_TAG_DROPPED = "tag_not_in_allow_list"
CODE_NO_DESCRIPTION = "no_description"

CODES = (
    CODE_UNKNOWN_STATUS,
    CODE_STATUS_FAILED,
    CODE_RRN,
    CODE_EMAIL,
    CODE_PHONE,
    CODE_PLATE,
    CODE_DIGITS,
    CODE_SENSITIVE,
    CODE_UNKNOWN_UNCERTAINTY,
    CODE_TAG_DROPPED,
    CODE_NO_DESCRIPTION,
)

#: 이 코드가 붙은 설명은 **본문을 색인하지 않는다.**
#:
#: 전부 「우리가 판단을 금지한 것이 사진에 보인다」 는 신호다. 모델은 규칙을
#: 지켰고, 그래서 신호 자체는 남길 값이 있다 — 설명만 뺀다.
SENSITIVE_UNCERTAINTY = frozenset({
    "person_visible",
    "plate_visible",
    "text_heavy",
    "measurement_visible",
    "damage_like_subject",
})

#: 화질 문제. 설명이 안전하면 `uncertain` 파생 색인으로 **저장할 수 있다.**
QUALITY_UNCERTAINTY = frozenset({
    "low_resolution",
    "blurred",
    "too_dark",
    "overexposed",
    "occluded",
})

if core.UNCERTAINTY_CODES != SENSITIVE_UNCERTAINTY | QUALITY_UNCERTAINTY:
    # 코어에 코드가 늘었는데 여기서 분류를 안 했다는 뜻이다. **조용히 통과시키면
    # 새 민감 코드가 설명과 함께 색인된다.** `assert` 로 두면 `-O` 에서 사라지므로
    # import 시점에 확실히 막는다.
    raise RuntimeError(
        "image_description.UNCERTAINTY_CODES 와 분류가 어긋났습니다: "
        + ",".join(sorted(core.UNCERTAINTY_CODES
                          ^ (SENSITIVE_UNCERTAINTY | QUALITY_UNCERTAINTY)))
    )

# --- 형태 검사 ---------------------------------------------------------------
#
# 주민등록번호는 **기존 검사기를 그대로 쓴다**(`pii_screen.screen_line`). 여기에
# 또 정규식을 두면 두 규칙이 갈라지고, 갈라진 쪽은 아무도 안 본다.

_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
# 한국 전화번호. 숫자 검사가 대개 먼저 잡지만, **사유 코드가 달라야**
# 운영에서 「모델이 무엇을 적었나」 를 가릴 수 있다.
_PHONE_RE = re.compile(r"(?<!\d)0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
# 12가3456 · 서울12가3456
_PLATE_RE = re.compile(r"(?<!\d)\d{2,3}\s*[가-힣]\s*\d{4}(?!\d)")
# 아라비아 숫자만이 아니라 **유니코드 숫자 전부**. 전각 숫자로 적어도 숫자다.
_DIGIT_RE = re.compile(r"\d")


@dataclass(frozen=True)
class ImageIndexDecision:
    """저장 판정. **입력을 고치지 않고 따로 만든다.**

    `ImageDescription` 은 모델이 무엇을 돌려줬는지의 기록이다. 거기서 설명을
    지우면 **무슨 일이 있었는지가 사라진다** — 원장은 원본을 그대로 들고 있어야
    나중에 「왜 색인이 비었나」 를 답할 수 있다.
    """

    verdict: str
    workspace: str
    channel: str
    file_id: str
    original_sha256: str
    prompt_revision: str
    #: 색인에 넣을 상태. 저장 불가면 빈 문자열이다.
    index_status: str = ""
    #: 색인에 넣을 설명. `allow` 가 아니면 **빈 문자열**이다.
    description: str = ""
    tags: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    #: 비민감 사유 코드. 탐지된 문자열은 들어가지 않는다.
    codes: tuple[str, ...] = ()

    @property
    def storable(self) -> bool:
        """파생 색인에 줄을 만들 수 있나."""
        return self.verdict in (VERDICT_ALLOW, VERDICT_WITHHOLD)


def _shape_code(text: str) -> str | None:
    """설명에서 **막아야 할 형태**를 찾는다. 찾은 문자열은 돌려주지 않는다.

    돌려주는 것은 코드 하나뿐이다. 무엇이 걸렸는지를 밖으로 내보내면 그 값이
    로그와 운영 API 를 타고 **막으려던 자리 밖으로** 나간다.
    """
    from ..pii_screen import screen_line

    said = text or ""
    if screen_line(said):
        return CODE_RRN
    if _EMAIL_RE.search(said):
        return CODE_EMAIL
    if _PHONE_RE.search(said):
        return CODE_PHONE
    if _PLATE_RE.search(said):
        return CODE_PLATE
    if _DIGIT_RE.search(said):
        return CODE_DIGITS
    return None


def _log(result, decision: ImageIndexDecision) -> None:
    """**좌표와 코드만.** 설명도 태그도 탐지 문자열도 넣지 않는다."""
    logger.info(
        "image_index_guard ws=%s ch=%s file=%s src=%s verdict=%s codes=%s "
        "chars=%d tags=%d unc=%d rev=%s",
        result.workspace, result.channel, result.file_id, result.status,
        decision.verdict, ",".join(decision.codes) or "-",
        len(decision.description), len(decision.tags),
        len(decision.uncertainty), decision.prompt_revision,
    )


def _decide(result, verdict: str, codes: tuple[str, ...], *,
            description: str = "", tags: tuple[str, ...] = (),
            uncertainty: tuple[str, ...] = ()) -> ImageIndexDecision:
    """**좌표는 언제나 결과에서 가져온다.**"""
    return ImageIndexDecision(
        verdict=verdict,
        workspace=result.workspace,
        channel=result.channel,
        file_id=result.file_id,
        original_sha256=result.original_sha256,
        prompt_revision=result.prompt_revision,
        index_status=(
            "" if verdict == VERDICT_BLOCK
            else core.STATUS_UNCERTAIN if verdict == VERDICT_WITHHOLD
            else core.STATUS_UNCERTAIN if uncertainty
            else core.STATUS_DESCRIBED
        ),
        description=description,
        tags=tags,
        uncertainty=uncertainty,
        codes=codes,
    )


def guard_index_write(result) -> ImageIndexDecision:
    """`ImageDescription` 을 파생 색인에 넣기 직전에 검사한다.

    입력을 **고치지 않는다.** 판정은 별도 객체로 돌려주고, 원본 결과는 원장이
    그대로 들고 있는다.

    예외를 내지 않는다 — 저장 경로에서 터지면 그 사진은 재시도 큐에 남고,
    같은 입력으로 또 터진다. 모르는 것은 **막는 쪽**으로 닫는다(원칙 3).
    """
    codes: list[str] = []

    if result.status not in (core.STATUS_DESCRIBED, core.STATUS_UNCERTAIN,
                             core.STATUS_FAILED):
        decision = _decide(result, VERDICT_BLOCK, (CODE_UNKNOWN_STATUS,))
        _log(result, decision)
        return decision

    # 1. 실패한 결과는 저장하지 않는다. 설명이 비어 있더라도 「봤는데 없었다」 가
    #    색인에 남으면 그 사진은 다시 안 본다.
    if result.status == core.STATUS_FAILED:
        decision = _decide(result, VERDICT_BLOCK, (CODE_STATUS_FAILED,))
        _log(result, decision)
        return decision

    # 2~3. 설명의 형태 검사. **모델이 규칙을 어긴 경우라 통째로 막는다** —
    #      같은 응답에서 나온 태그를 믿을 근거가 없다.
    shape = _shape_code(result.description)
    if shape:
        decision = _decide(result, VERDICT_BLOCK, (shape,))
        _log(result, decision)
        return decision

    # 6. 태그는 **코어 허용 목록만.** 코어가 이미 거르지만 여기서 안 믿는다 —
    #    믿으면 코어를 고칠 때 이 문이 조용히 열린다.
    tags = tuple(t for t in result.tags if t in core.TAGS)
    if len(tags) != len(result.tags):
        codes.append(CODE_TAG_DROPPED)

    known = tuple(u for u in result.uncertainty if u in core.UNCERTAINTY_CODES)
    unknown = len(known) != len(result.uncertainty)
    if unknown:
        # 모르는 코드가 민감한 것인지 **알 수 없다.** 막는 쪽이 기본값이다.
        codes.append(CODE_UNKNOWN_UNCERTAINTY)

    # 4. 민감 uncertainty 가 하나라도 있으면 **설명 본문을 색인하지 않는다.**
    if unknown or any(u in SENSITIVE_UNCERTAINTY for u in known):
        if not unknown:
            codes.append(CODE_SENSITIVE)
        decision = _decide(result, VERDICT_WITHHOLD, tuple(codes),
                           tags=tags, uncertainty=known)
        _log(result, decision)
        return decision

    # 5. 화질 코드만 남았다. 설명이 안전하면 `uncertain` 으로 저장한다.
    description = (result.description or "").strip()
    if not description:
        # 넣을 본문이 없다. 막을 일은 아니지만 **설명이 있는 줄로 세지 않는다.**
        codes.append(CODE_NO_DESCRIPTION)
        decision = _decide(result, VERDICT_WITHHOLD, tuple(codes),
                           tags=tags, uncertainty=known)
        _log(result, decision)
        return decision

    decision = _decide(result, VERDICT_ALLOW, tuple(codes),
                       description=description, tags=tags, uncertainty=known)
    _log(result, decision)
    return decision


__all__ = [
    "CODES",
    "CODE_DIGITS",
    "CODE_EMAIL",
    "CODE_NO_DESCRIPTION",
    "CODE_PHONE",
    "CODE_PLATE",
    "CODE_RRN",
    "CODE_SENSITIVE",
    "CODE_STATUS_FAILED",
    "CODE_TAG_DROPPED",
    "CODE_UNKNOWN_STATUS",
    "CODE_UNKNOWN_UNCERTAINTY",
    "QUALITY_UNCERTAINTY",
    "SENSITIVE_UNCERTAINTY",
    "VERDICT_ALLOW",
    "VERDICT_BLOCK",
    "VERDICT_WITHHOLD",
    "ImageIndexDecision",
    "guard_index_write",
]
