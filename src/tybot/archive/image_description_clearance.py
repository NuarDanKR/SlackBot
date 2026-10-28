"""외부 전송 **직전**의 안전 판정. 두 가지만 답한다.

```
선별 → 원본 bytes → [이 모듈] → 예약 → dispatch → 모델 1회
                     access_verified
                     pii_blocked
```

`preflight_image_description` 과 `run_approved_image` 는 이 둘을 **받기만 한다**
(`specialist` 쪽 `CALLER_CHECKS` 와 같은 이름이다). 지금까지는 그 둘을 아무도
만들지 않아, 호출자가 손으로 적어 넣는 자리였다. 손으로 적는 자리는 언젠가
`True` 로 적힌다.

## 왜 순수한가

모델·DB·파일·네트워크를 부르지 않는다. 이 판정이 I/O 를 하면 **판정하는 동안
사실이 바뀐다** — ACL 을 읽어 와 통과시킨 뒤 전송 전에 채널이 닫히면, 우리는
닫힌 자료를 밖으로 보낸 것이다. 그래서 「지금 이 사실들로 보내도 되나」 만
답하고, 그 사실을 **언제 읽었는지는 호출자가 책임진다.**

## 승인은 좌표에 결속된다

`SafetyAssessment` 는 「이 사진」 에 대한 승인이지 「이 사람의 판단」 이 아니다.
그래서 워크스페이스·채널·파일·원본 해시·provider·model·정책 판에 **전부**
묶는다. 하나라도 어긋나면 그 승인은 **다른 것에 대한 승인**이므로 쓰지 않는다.

묶지 않으면 「한 번 승인」 이 「그 사람이 승인한 모든 것」 으로 번진다. 모델이
바뀌어도, 정책이 완화돼도, 심지어 같은 채널의 다른 사진에도 같은 도장이 찍힌다.

## `text_screened` 는 여기서 읽지 않는다

선별기가 주는 `text_screened` 는 **글자 검사가 돌았다**는 사실일 뿐이다.
신분증 사진이 OCR 에 실패하면 검사는 아무것도 못 본 채 지나가고, 그 결과도
`text_screened=True` 다. 그것으로 `pii_blocked=False` 를 만들면 **못 읽은 것이
안전으로 바뀐다.**

이 모듈은 그 칸을 **아예 보지 않는다.** 읽고 무시하는 것보다 안 읽는 쪽이
낫다 — 읽는 코드가 있으면 다음 사람이 거기에 조건을 하나 더 붙인다.

## 모르면 막는다

빈 `visibility`·빈 `acl` 은 「공개」 가 아니라 **모름**이다(원칙 3). 승인 당시와
달라진 것도 모름이다 — 넓어졌는지 좁아졌는지 여기서 알 수 없고, 넓어진 쪽을
맞혀 통과시키면 권한 없는 사람이 설명으로 내용을 안다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

#: 원본 해시 표기. 선별기(`image_description_source`)가 내려 보내는 꼴과 같다.
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

#: 거절 코드. **전부 ASCII 고정값**이다 — 본문·파일명·경로·사람 이름을 담지
#: 않는다. 코드가 아니라 값을 담으면 로그가 곧 유출 경로가 된다.
INVALID_CLOCK = "invalid_clock"
ASSESSMENT_MISSING = "assessment_missing"
ASSESSMENT_INVALID = "assessment_invalid"
ASSESSMENT_REVOKED = "assessment_revoked"
ASSESSMENT_NOT_YET_VALID = "assessment_not_yet_valid"
ASSESSMENT_EXPIRED = "assessment_expired"
WORKSPACE_MISMATCH = "workspace_mismatch"
CHANNEL_MISMATCH = "channel_mismatch"
FILE_MISMATCH = "file_mismatch"
ORIGINAL_SHA256_MISMATCH = "original_sha256_mismatch"
PROVIDER_MISMATCH = "provider_mismatch"
MODEL_MISMATCH = "model_mismatch"
POLICY_REVISION_MISMATCH = "policy_revision_mismatch"
DOC_MISSING = "doc_missing"
DOC_COORDINATE_MISMATCH = "doc_coordinate_mismatch"
VISIBILITY_UNKNOWN = "visibility_unknown"
ACL_UNKNOWN = "acl_unknown"
VISIBILITY_CHANGED = "visibility_changed"
ACL_CHANGED = "acl_changed"
PII_NOT_CLEARED = "pii_not_cleared"

CODES = (
    INVALID_CLOCK, ASSESSMENT_MISSING, ASSESSMENT_INVALID, ASSESSMENT_REVOKED,
    ASSESSMENT_NOT_YET_VALID, ASSESSMENT_EXPIRED, WORKSPACE_MISMATCH,
    CHANNEL_MISMATCH, FILE_MISMATCH, ORIGINAL_SHA256_MISMATCH, PROVIDER_MISMATCH,
    MODEL_MISMATCH, POLICY_REVISION_MISMATCH, DOC_MISSING, DOC_COORDINATE_MISMATCH,
    VISIBILITY_UNKNOWN, ACL_UNKNOWN, VISIBILITY_CHANGED, ACL_CHANGED,
    PII_NOT_CLEARED,
)


@dataclass(frozen=True)
class SafetyAssessment:
    """**사람이 이 사진 하나를 보고 내린** 판단. 도장이지 규칙이 아니다.

    `assessed_by` 는 결속을 위해 받되 **밖으로 나가지 않는다**(규칙 7). 누가
    승인했는지는 원장이 들고, 이 판정의 결과에는 좌표와 코드만 남는다.
    """

    workspace: str
    channel_id: str
    file_id: str
    original_sha256: str
    provider: str
    model: str
    policy_revision: str
    #: 승인 **당시**의 공개 범위. 지금 값과 다르면 그 승인은 다른 상태의 것이다.
    visibility: str
    acl: frozenset[str]
    #: 누가 승인했나. 비어 있으면 「사람의 명시적 승인」 이 아니다(규칙 5).
    assessed_by: str
    assessed_at: datetime
    expires_at: datetime
    #: 사람이 **사진을 보고** 개인정보 없음을 명시한 경우만 참. 기본은 거짓이다.
    pii_cleared: bool = False
    #: 승인 뒤 철회됐나. 철회는 만료를 기다리지 않는다.
    revoked: bool = False


@dataclass(frozen=True)
class Clearance:
    """전송 직전의 답. **기본값이 닫혀 있다.**

    `access_verified=False` · `pii_blocked=True` 가 기본이라, 이 객체를 어떻게
    만들어도 아무 말 안 한 상태는 「보내지 마라」 다.

    담는 것은 좌표와 코드뿐이다 — 본문·파일명·경로·사람 이름이 없으므로
    그대로 로그에 찍어도 된다. 로그는 호출자가 찍는다.
    """

    workspace: str
    channel_id: str
    file_id: str
    access_verified: bool = False
    pii_blocked: bool = True
    code: str = ""

    @property
    def cleared(self) -> bool:
        """보내도 되나."""
        return self.access_verified and not self.pii_blocked

    def as_checks(self) -> dict:
        """`run_approved_image(checks=...)` 에 그대로 넘기는 꼴."""
        return {
            "access_verified": self.access_verified,
            "pii_blocked": self.pii_blocked,
        }


def clear_for_dispatch(
    candidate,
    *,
    doc,
    assessment: SafetyAssessment | None,
    provider: str,
    model: str,
    policy_revision: str,
    now: datetime,
) -> Clearance:
    """지금 이 사실들로 사진을 밖에 보내도 되나.

    `provider`·`model`·`policy_revision` 은 **지금 쓰려는 값**이다. 승인에 적힌
    것과 다르면 그 승인은 다른 경로에 대한 것이므로 쓰지 않는다.

    예외를 내지 않는다. 배치가 건당 사유를 세야 하고, 예외는 메시지에 값을
    끌고 다니기 쉽다.
    """
    workspace = _text(getattr(candidate, "workspace", ""))
    channel_id = _text(getattr(candidate, "channel_id", ""))
    file_id = _text(getattr(candidate, "file_id", ""))

    def refuse(code: str) -> Clearance:
        # 좌표가 어긋난 판정은 **두 축을 함께 닫는다.** 승인 자체를 믿을 수
        # 없는데 그 승인이 말한 「개인정보 없음」 만 살려 둘 이유가 없다.
        return Clearance(workspace, channel_id, file_id, False, True, code)

    if not isinstance(now, datetime) or now.utcoffset() is None:
        return refuse(INVALID_CLOCK)
    if assessment is None:
        return refuse(ASSESSMENT_MISSING)
    if not _well_formed(assessment):
        return refuse(ASSESSMENT_INVALID)

    # --- 승인이 아직 살아 있나 -------------------------------------------
    if assessment.revoked is not False:
        return refuse(ASSESSMENT_REVOKED)
    if assessment.assessed_at > now:
        # 미래에 찍힌 도장. 시계가 틀렸거나 누가 적어 넣은 것이다.
        return refuse(ASSESSMENT_NOT_YET_VALID)
    if assessment.expires_at <= now:
        return refuse(ASSESSMENT_EXPIRED)

    # --- 승인이 이 사진 · 이 경로에 대한 것인가 ---------------------------
    if assessment.workspace != workspace:
        return refuse(WORKSPACE_MISMATCH)
    if assessment.channel_id != channel_id:
        return refuse(CHANNEL_MISMATCH)
    if assessment.file_id != file_id:
        return refuse(FILE_MISMATCH)
    if assessment.original_sha256 != _digest(getattr(candidate, "original_sha256", "")):
        return refuse(ORIGINAL_SHA256_MISMATCH)
    if assessment.provider != _text(provider):
        return refuse(PROVIDER_MISMATCH)
    if assessment.model != _text(model):
        return refuse(MODEL_MISMATCH)
    if assessment.policy_revision != _text(policy_revision):
        return refuse(POLICY_REVISION_MISMATCH)

    # --- 정본이 지금도 같은 것을 가리키나 ---------------------------------
    if doc is None:
        return refuse(DOC_MISSING)
    if (
        _text(getattr(doc, "workspace", "")) != workspace
        or _text(getattr(doc, "channel_id", "")) != channel_id
        or _text(getattr(doc, "file_id", "")) != file_id
    ):
        return refuse(DOC_COORDINATE_MISMATCH)
    if _digest(getattr(doc, "sha256", "")) != assessment.original_sha256:
        # 같은 자리에 다른 원본이 들어왔다. 사람이 본 사진이 아니다.
        return refuse(ORIGINAL_SHA256_MISMATCH)

    # --- 지금 권한을 아는가, 그리고 그때와 같은가 -------------------------
    visibility = _text(getattr(doc, "visibility", ""))
    acl = _acl(getattr(doc, "acl", None))
    if not visibility:
        # 빈 값은 「공개」 가 아니라 **모름**이다(원칙 3).
        return refuse(VISIBILITY_UNKNOWN)
    if not acl:
        return refuse(ACL_UNKNOWN)
    if visibility != assessment.visibility:
        return refuse(VISIBILITY_CHANGED)
    if acl != _acl(assessment.acl):
        # 넓어졌는지 좁아졌는지 여기서 알 수 없다. 맞혀서 통과시키지 않는다.
        return refuse(ACL_CHANGED)

    # --- 개인정보는 사람이 명시해야 열린다 --------------------------------
    #
    # `candidate.text_screened` 는 **읽지 않는다.** 글자 검사가 돌았다는 사실은
    # 사진에 개인정보가 없다는 뜻이 아니다(모듈 설명 참고).
    if assessment.pii_cleared is not True or not _text(assessment.assessed_by):
        # 권한은 확인됐다. 사람이 아직 안 봤을 뿐이고, 그 둘은 다른 상태다.
        return Clearance(workspace, channel_id, file_id, True, True, PII_NOT_CLEARED)

    return Clearance(workspace, channel_id, file_id, True, False, "")


def _text(value) -> str:
    return str(value or "").strip()


def _digest(value) -> str:
    """선별기와 **같은 규칙**으로 고른다 — `strip().lower()`."""
    return str(value or "").strip().lower()


def _acl(value) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes)):
        # 문자열은 집합처럼 순회되어 **글자 하나하나가 멤버**가 된다. 그렇게
        # 만들어진 집합은 어떤 ACL 과도 같지 않아 조용히 거절로 흐른다 —
        # 조용한 거절은 틀린 통과보다 낫지만, 여기서 끊는 편이 읽기 쉽다.
        return frozenset()
    try:
        return frozenset(_text(item) for item in value)
    except TypeError:
        return frozenset()


def _well_formed(assessment: SafetyAssessment) -> bool:
    """승인이 **판정에 쓸 수 있는 꼴**인가. 내용이 맞는지는 보지 않는다."""
    required = (
        assessment.workspace, assessment.channel_id, assessment.file_id,
        assessment.provider, assessment.model, assessment.policy_revision,
        assessment.visibility,
    )
    if any(not isinstance(item, str) or not item.strip() for item in required):
        return False
    if any(item != item.strip() for item in required):
        # 앞뒤 공백이 붙은 좌표는 결속을 무르게 만든다 — ` C123` 과 `C123` 이
        # 다르게 묶이면 승인 하나가 두 사진을 가리킬 수 있다.
        return False
    if not isinstance(assessment.original_sha256, str):
        return False
    if not _SHA256.match(assessment.original_sha256):
        return False
    if not isinstance(assessment.acl, (set, frozenset)):
        return False
    if type(assessment.pii_cleared) is not bool or type(assessment.revoked) is not bool:
        # `1`·`"true"` 는 참처럼 보이지만 사람이 승인했다는 뜻이 아니다.
        return False
    for moment in (assessment.assessed_at, assessment.expires_at):
        if not isinstance(moment, datetime) or moment.utcoffset() is None:
            return False
    return assessment.expires_at > assessment.assessed_at


__all__ = [
    "CODES",
    "Clearance",
    "SafetyAssessment",
    "clear_for_dispatch",
]
