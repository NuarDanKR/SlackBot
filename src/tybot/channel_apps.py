"""채널에 **꼭 있어야 하는 앱 넷**이 실제로 있는가 (B-87).

## 왜 따로 보나

`/채널 상태` 는 TYBot 하나의 참여만 봤다. 그래서 HWP 변환 앱이 빠져 있거나
수집기가 초대되지 않은 채널도 「모두 정상」 으로 보였다. 네 역할은 고장나는
이유도, 사람이 할 일도 서로 다르다.

| 역할 | 하는 일 | 빠지면 |
|---|---|---|
| Archiver | 원문 수집 | 대화가 아카이브에 안 쌓인다 — 조용하다 |
| TYBot | 답변 | 멘션해도 아무 반응이 없다 |
| rhwp | HWP → PDF | 한글 문서 내용이 근거에서 통째로 빠진다 |
| Hermes | 내부 기록 전문봇 | 전문가 답변이 마스터 답변으로 떨어진다 |

## 이름으로 추측하지 않는다

봇 이름·표시 이름은 누구나 비슷하게 만들 수 있다. 「아카이버」 라는 이름의 다른
앱이 들어와 있으면 이름 대조는 **있다고 말하고**, 그 순간 이 화면은 거짓말이
된다. 그래서 정체성은 두 가지만 쓴다.

- `bot_connection` 의 **검증된** `bot_user_id` — `auth.test` 결과이고,
  `state='enabled'` 는 스키마상 `identity_ok` 없이 존재할 수 없다
- 변환 앱의 **Slack 앱 ID** — 콘솔이 관리하는 값(`TYBOT_CONVERTER_APP_IDS`)

## 모르는 것을 「없음」 이라 하지 않는다

가장 중요한 규칙이다. Slack API 가 실패했을 때 「미초대」 라고 쓰면, 사람은 이미
들어와 있는 앱을 초대하러 가서 아무 일도 못 한다. 더 나쁘게는 **실제로 빠진 앱이
있을 때도 같은 화면**이라, 다음부터 이 화면을 안 믿는다.

비공개 채널에서 TYBot 이 멤버가 아니면 `conversations.members` 자체가 실패한다.
그것은 「다른 앱이 없다」 가 아니라 「우리가 볼 수 없다」 다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# --- 역할 ---------------------------------------------------------------------

ARCHIVER = "archiver"
TYBOT = "tybot"
RHWP = "rhwp"
HERMES = "hermes"

#: 화면에 나오는 순서. 수집 → 답변 → 변환 → 전문가 순으로, 고장이 뒤에 미치는
#: 순서와 같다. Archiver 가 없으면 나머지가 정상이어도 답할 자료가 안 쌓인다.
ROLES = (ARCHIVER, TYBOT, RHWP, HERMES)

LABELS = {
    ARCHIVER: "Archiver",
    TYBOT: "TYBot",
    RHWP: "rhwp",
    HERMES: "Hermes",
}

#: 역할 → `bot_catalog.key`. VIPBot 은 **여기 없다.** 필수 앱이 아니고, 섞으면
#: 「VIP 가 없어서 빨강」 인 채널이 생긴다(B-87 오너 결정).
CATALOG_KEYS = {ARCHIVER: "archiver", TYBOT: "master", HERMES: "hermes"}

# --- 판정 결과 ----------------------------------------------------------------

JOINED = "joined"
NOT_INVITED = "not_invited"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"

STATES = frozenset({JOINED, NOT_INVITED, UNKNOWN, NOT_APPLICABLE})

#: 왜 그렇게 판정했나. **고정 낱말**이다 — 화면 문구가 바뀌어도 시험과 로그는
#: 같은 값을 본다.
BINDING_MISSING = "binding_missing"
DB_UNREADABLE = "db_unreadable"
BINDING_DISABLED = "binding_disabled"
IDENTITY_UNVERIFIED = "identity_unverified"
MEMBERS_UNREADABLE = "members_unreadable"
APP_ID_UNRESOLVED = "app_id_unresolved"
EXTERNAL_RPC = "external_rpc"
DELEGATION_OFF = "delegation_off"
MEMBER = "member"
ABSENT = "absent"

CODES = frozenset({
    BINDING_MISSING, DB_UNREADABLE, BINDING_DISABLED, IDENTITY_UNVERIFIED, MEMBERS_UNREADABLE,
    APP_ID_UNRESOLVED, EXTERNAL_RPC, DELEGATION_OFF, MEMBER, ABSENT,
})

MARKS = {JOINED: "✅", NOT_INVITED: "❌", UNKNOWN: "❓", NOT_APPLICABLE: "➖"}

STATE_WORDS = {
    JOINED: "참여",
    NOT_INVITED: "미초대",
    UNKNOWN: "확인 불가",
    NOT_APPLICABLE: "해당 없음",
}


@dataclass(frozen=True)
class AppBinding:
    """한 역할의 **검증된** 정체성. 이름은 들어오지 않는다.

    `bot_user_ids` 와 `app_ids` 가 둘 다 비어 있으면 대조할 것이 없다는 뜻이고,
    그때는 `missing_code` 가 왜인지 말한다.
    """

    role: str
    bot_user_ids: frozenset[str] = frozenset()
    app_ids: frozenset[str] = frozenset()
    #: 이 채널에서 필수인가. 거짓이면 `해당 없음` 으로 표시하고 판정을 흐리지 않는다.
    required: bool = True
    #: 필수가 아닌 이유 또는 정체성을 모르는 이유.
    missing_code: str = ""
    #: 사람이 지금 할 수 있는 행동. 비어 있으면 안내를 붙이지 않는다.
    invite_hint: str = ""
    #: 화면에 적을 보조 설명(모델명·앱 ID 같은 값은 넣지 않는다).
    note: str = ""

    @property
    def identified(self) -> bool:
        return bool(self.bot_user_ids or self.app_ids)


@dataclass(frozen=True)
class ChannelAppFacts:
    """모은 사실. **판정은 `evaluate` 가 한다** — Slack 없이 전부 시험한다."""

    bindings: tuple[AppBinding, ...] = ()
    #: 채널 멤버의 Slack 사용자 ID. `None` 은 **못 읽었다**(미초대가 아니다).
    member_ids: frozenset[str] | None = None
    #: 봇 멤버가 속한 Slack 앱 ID. `None` 은 못 풀었다.
    member_app_ids: frozenset[str] | None = None
    is_private: bool = False
    #: 멤버 조회가 실패한 이유(고정 낱말). 화면에는 쓰지 않고 사유 구분에만 쓴다.
    probe_error: str = ""


@dataclass(frozen=True)
class AppStatus:
    role: str
    label: str
    state: str
    code: str
    detail: str = ""
    fix: str = ""

    @property
    def mark(self) -> str:
        return MARKS[self.state]


# --- 판정 ---------------------------------------------------------------------


def _private_note(facts: ChannelAppFacts) -> str:
    if facts.is_private:
        return "비공개 채널이라 TYBot 이 멤버가 아니면 목록을 볼 수 없습니다."
    return "Slack 조회가 실패했습니다. 자료가 없다는 뜻이 아닙니다."


def _unknown(binding: AppBinding, code: str, detail: str, fix: str = "") -> AppStatus:
    return AppStatus(
        binding.role, LABELS[binding.role], UNKNOWN, code, detail,
        fix or binding.invite_hint,
    )


def evaluate_one(binding: AppBinding, facts: ChannelAppFacts) -> AppStatus:
    """한 역할의 상태. **순서가 계약이다.**

    1. 필수가 아니면 거기서 끝 — 쓰지 않는 앱의 부재를 고장으로 세지 않는다
    2. 정체성을 모르면 `확인 불가` — 대조할 ID 가 없는데 「없다」 고 말할 수 없다
    3. 멤버 목록을 못 읽으면 `확인 불가` — **이 줄이 이 모듈의 존재 이유다**
    4. 그제서야 참여/미초대
    """
    label = LABELS[binding.role]
    if not binding.required:
        return AppStatus(
            binding.role, label, NOT_APPLICABLE,
            binding.missing_code or DELEGATION_OFF, binding.note,
        )
    if not binding.identified:
        code = binding.missing_code or BINDING_MISSING
        detail = binding.note or (
            "콘솔에 이 워크스페이스의 연결이 없습니다."
            if code == BINDING_MISSING
            else "연결은 있으나 Slack 신원이 확인되지 않았습니다."
        )
        return _unknown(
            binding, code, detail,
            "콘솔 > 봇 연결에서 토큰을 넣고 신원 확인을 하세요.",
        )
    needs_app_ids = bool(binding.app_ids) and not binding.bot_user_ids
    if facts.member_ids is None:
        return _unknown(binding, MEMBERS_UNREADABLE, _private_note(facts))
    if needs_app_ids and facts.member_app_ids is None:
        return _unknown(
            binding, APP_ID_UNRESOLVED,
            "채널 멤버는 읽었지만 어느 앱인지 확인하지 못했습니다.",
        )
    if binding.bot_user_ids & facts.member_ids:
        return AppStatus(binding.role, label, JOINED, MEMBER, binding.note)
    if binding.app_ids and (facts.member_app_ids or frozenset()) & binding.app_ids:
        return AppStatus(binding.role, label, JOINED, MEMBER, binding.note)
    return AppStatus(
        binding.role, label, NOT_INVITED, ABSENT,
        binding.note or "이 채널에 없습니다.",
        binding.invite_hint,
    )


def evaluate(facts: ChannelAppFacts) -> tuple[AppStatus, ...]:
    """역할 순서대로 판정한다. 등록되지 않은 역할은 목록에서 **빠지지 않는다** —
    빠지면 사람이 「검사했는데 괜찮다」 로 읽는다."""
    known = {binding.role: binding for binding in facts.bindings}
    out = []
    for role in ROLES:
        binding = known.get(role) or AppBinding(role=role, missing_code=BINDING_MISSING)
        out.append(evaluate_one(binding, facts))
    return tuple(out)


def missing(statuses: Iterable[AppStatus]) -> tuple[AppStatus, ...]:
    return tuple(s for s in statuses if s.state == NOT_INVITED)


def unresolved(statuses: Iterable[AppStatus]) -> tuple[AppStatus, ...]:
    return tuple(s for s in statuses if s.state == UNKNOWN)


# --- 화면 ---------------------------------------------------------------------


def summary(statuses: Iterable[AppStatus]) -> str:
    """한 줄 결론. 사람이 목록을 세지 않아도 되게 한다."""
    rows = tuple(statuses)
    gone = missing(rows)
    unsure = unresolved(rows)
    if gone:
        names = ", ".join(s.label for s in gone)
        return f"{names} 이(가) 이 채널에 없습니다."
    if unsure:
        names = ", ".join(s.label for s in unsure)
        return f"{names} 의 참여 여부를 확인하지 못했습니다."
    return "필수 앱이 모두 참여 중입니다."


def lines(statuses: Iterable[AppStatus]) -> list[str]:
    """역할마다 한 줄. 고칠 것이 있으면 그 아래 한 줄 더."""
    out: list[str] = []
    for status in statuses:
        word = STATE_WORDS[status.state]
        detail = f" — {status.detail}" if status.detail else ""
        out.append(f"{status.mark} {status.label}: {word}{detail}")
        if status.fix:
            out.append(f"　└ {status.fix}")
    return out


# --- 정체성 수집 --------------------------------------------------------------
#
# 아래는 DB·설정을 읽는다. Slack 은 부르지 않는다 — 멤버 조회는 호출자가 하고,
# 그래야 이 판정 전체를 Slack 없이 시험할 수 있다.


#: 초대 안내. **봇 핸들을 지어내지 않는다.**
#:
#: 전에는 `/invite @Archiver` 라고 적었다. `Archiver` 는 우리가 화면에 쓰는
#: 역할 이름이지 Slack 핸들이 아니다 — 실제 핸들은 워크스페이스마다 다르고,
#: 앱을 설치한 사람이 바꿀 수도 있다. 그 명령을 그대로 친 사람은
#: `user_not_found` 를 보고, 고칠 방법은 못 얻는다.
#:
#: 우리가 아는 것은 `bot_user_id` 뿐이다. Slack 에서 `<@U...>` 는 메시지
#: 본문에서만 이름으로 풀리고 슬래시 명령 인자로는 안 된다. 그래서 **명령 대신
#: 어디서 이름을 확인하는지**를 적는다.
INVITE_HINT = (
    "채널 > 통합 > 앱 추가에서 이 앱을 추가하세요. "
    "앱 이름은 콘솔 > 봇 연결에서 확인할 수 있습니다."
)


def _invite_hint(label: str) -> str:
    return INVITE_HINT


def binding_from_connection(role: str, row: Mapping | None) -> AppBinding:
    """`bot_connection` 한 행을 정체성으로 바꾼다.

    **`enabled` 만 인정한다.** `draft`·`disabled` 연결의 `bot_user_id` 는 아직
    운영에서 쓰이지 않는 값이고, 그것으로 「참여 중」 이라고 쓰면 꺼 둔 앱이
    살아 있는 것처럼 보인다.
    """
    label = LABELS[role]
    if row is None:
        return AppBinding(
            role=role, missing_code=BINDING_MISSING,
            invite_hint=_invite_hint(label),
            note="콘솔에 이 워크스페이스의 연결이 없습니다.",
        )
    state = str(row.get("state") or "")
    user_id = str(row.get("bot_user_id") or "").strip()
    if state != "enabled":
        return AppBinding(
            role=role, missing_code=BINDING_DISABLED,
            invite_hint="콘솔 > 봇 연결에서 이 봇을 켜세요.",
            note=f"콘솔 연결 상태가 `{state or '없음'}` 입니다.",
        )
    if not user_id or row.get("identity_ok") is not True:
        return AppBinding(
            role=role, missing_code=IDENTITY_UNVERIFIED,
            invite_hint="콘솔 > 봇 연결에서 신원 확인을 다시 하세요.",
            note="연결은 켜져 있으나 Slack 신원이 확인되지 않았습니다.",
        )
    return AppBinding(
        role=role,
        bot_user_ids=frozenset({user_id}),
        invite_hint=_invite_hint(label),
    )


def converter_binding(app_ids: Iterable[str], *, delegated: bool) -> AppBinding:
    """변환 앱. **HWP 위임이 켜져 있을 때만 필수다.**

    끄고 쓰는 워크스페이스에서 이 줄이 빨강이면, 고칠 것이 없는 경고가 매번
    보인다. 그런 경고는 몇 번 지나면 옆의 진짜 경고까지 같이 무시된다.
    """
    ids = frozenset(str(value).strip().upper() for value in app_ids if str(value).strip())
    if not delegated:
        return AppBinding(
            role=RHWP, required=False, missing_code=DELEGATION_OFF,
            note="HWP 변환을 이 앱에 넘기지 않는 설정입니다.",
        )
    if not ids:
        # **위임은 켰는데 앱 ID 가 없다.** 「해당 없음」 이라고 쓰면 안 된다 —
        # 이 상태에서 TYBot 은 HWP 를 직접 변환하지 않고(`conversion_delegated`),
        # 받을 앱도 지정돼 있지 않다. 한글 문서가 통째로 근거에서 빠지는데
        # 화면은 조용하다. 설정이 반쯤 된 것은 **모르는 것**으로 적는다.
        return AppBinding(
            role=RHWP, missing_code=BINDING_MISSING,
            invite_hint="콘솔 > 환경 설정에서 변환 앱의 Slack 앱 ID 를 넣으세요.",
            note="HWP 변환을 넘기도록 켜져 있으나 변환 앱 ID 가 비어 있습니다.",
        )
    return AppBinding(
        role=RHWP, app_ids=ids,
        invite_hint=INVITE_HINT,
        note="",
    )


def hermes_binding(row: Mapping | None, *, route_mode: str) -> AppBinding:
    """Hermes. **외부 RPC 구성이면 Slack 앱이 필요 없다.**

    그때 「미초대」 라고 쓰면 사람은 있지도 않은 앱을 찾으러 간다. 사실대로
    `해당 없음(외부 전문봇)` 이라고 적는다(B-87).

    판단 순서가 중요하다 — Slack 연결이 **켜져 있으면** 그쪽이 먼저다. 둘 다
    구성된 공존 기간에는 채널 참여가 실제로 필요하기 때문이다.
    """
    binding = binding_from_connection(HERMES, row)
    if binding.identified:
        return binding
    if str(route_mode or "disabled") != "disabled":
        return AppBinding(
            role=HERMES, required=False, missing_code=EXTERNAL_RPC,
            note="외부 전문봇으로 호출합니다(Slack 앱 없음).",
        )
    return binding


__all__ = [
    "ABSENT",
    "APP_ID_UNRESOLVED",
    "ARCHIVER",
    "BINDING_DISABLED",
    "BINDING_MISSING",
    "CATALOG_KEYS",
    "CODES",
    "DB_UNREADABLE",
    "DELEGATION_OFF",
    "EXTERNAL_RPC",
    "HERMES",
    "IDENTITY_UNVERIFIED",
    "INVITE_HINT",
    "JOINED",
    "LABELS",
    "MARKS",
    "MEMBER",
    "MEMBERS_UNREADABLE",
    "NOT_APPLICABLE",
    "NOT_INVITED",
    "RHWP",
    "ROLES",
    "STATES",
    "STATE_WORDS",
    "TYBOT",
    "UNKNOWN",
    "AppBinding",
    "AppStatus",
    "ChannelAppFacts",
    "binding_from_connection",
    "converter_binding",
    "evaluate",
    "evaluate_one",
    "hermes_binding",
    "lines",
    "missing",
    "summary",
    "unresolved",
]
