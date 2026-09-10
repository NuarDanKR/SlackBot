"""표준 Slack 업무 채널 생성·이름 변경 지원.

채널 메시지 원문과 섞이지 않도록 생성 요청자 정보는 STATE_DIR 아래에 둔다. Slack의
Channel Manager를 대체하는 권한이 아니라, 봇의 이름 변경 API를 누가 사용할 수 있는지
제한하는 최소 권한 기록이다.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .channels import COLLECT_PREFIXES, PREFIX_ALIASES, ChannelSpec, parse
from .orgsearch import OrgHit, decode_value, option

_OWNER_LOCK = threading.Lock()
_SPACE_RE = re.compile(r"\s+")
_CODE_RE = re.compile(r"^[0-9A-Za-z]+$")
_CHANNEL_PREFIX = {"본사팀": "팀"}

# 한 번에 만들 수 있는 채널 수.
# 상한을 두는 이유는 두 가지다. 오타로 줄바꿈이 잔뜩 들어간 입력이 그대로 실행되는 것을
# 막고, Slack 의 채널 생성 호출이 분당 제한에 걸려 절반만 만들어지는 상태를 피한다.
MAX_BATCH = 10
_ORG_BLOCK_IDS = {
    "본부": "org_hq",
    "실": "org_div",
    "본사팀": "org_team",
    "현장": "org_site",
    "업무": "org_task",
}


# 검토자·발송 시각 입력 칸. **생성 모달과 수정 모달이 같은 이름을 쓴다** -
# 갈리면 한쪽 제출이 조용히 「검토자 없음」 으로 읽힌다.
REVIEWER_BLOCK = "reviewers"
SEND_AT_BLOCK = "send_at"


class ChannelNameError(ValueError):
    """모달의 특정 입력 블록에 표시할 채널명 오류."""

    def __init__(self, message: str, block_id: str) -> None:
        super().__init__(message)
        self.block_id = block_id


@dataclass(frozen=True)
class ChannelRequest:
    prefix: str
    org_name: str
    org_code: str
    task: str
    visibility: str = "private"
    members: tuple[str, ...] = ()
    # 요약 검토자와 보낼 시각. **생성 시점에 정한다** — 나중에 정하게 두면
    # 안 정한 채널이 쌓이고, 그 채널들은 요약이 반영되지 않고 읽지 못한 첨부도
    # 아무에게도 가지 않는다(조용히).
    reviewers: tuple[str, ...] = ()
    send_at: str = "08:00"

    @property
    def name(self) -> str:
        return build_channel_name(self.prefix, self.org_name, self.org_code, self.task)


def _piece(value: str) -> str:
    return _SPACE_RE.sub("-", (value or "").strip())


def build_channel_name(prefix: str, org_name: str, org_code: str, task: str) -> str:
    """사람이 입력한 네 항목을 수집 규칙에 맞는 Slack 채널명으로 조립한다."""
    prefix = (prefix or "").strip()
    org_name = _piece(org_name)
    org_code = (org_code or "").strip().upper()
    task = _piece(task)

    if prefix not in COLLECT_PREFIXES:
        raise ChannelNameError("조직 구분을 선택해 주세요.", "prefix")
    if not org_name:
        raise ChannelNameError("조직명을 입력해 주세요.", "org_name")
    if "_" in org_name or "#" in org_name:
        raise ChannelNameError("조직명에는 밑줄(_)이나 #을 사용할 수 없습니다.", "org_name")
    if not _CODE_RE.fullmatch(org_code):
        raise ChannelNameError("조직코드는 영문과 숫자만 입력해 주세요.", "org_code")
    if not task:
        raise ChannelNameError("업무명을 입력해 주세요.", "task")
    if "#" in task:
        raise ChannelNameError("업무명에는 #을 사용할 수 없습니다.", "task")

    channel_prefix = _CHANNEL_PREFIX.get(prefix, prefix)
    name = f"{channel_prefix}-{org_name}_{org_code}-{task}"
    if len(name) > 80:
        raise ChannelNameError("채널명이 80자를 넘습니다. 조직명이나 업무명을 줄여 주세요.", "task")
    if parse(name) is None:
        raise ChannelNameError("표준 채널명으로 만들 수 없는 입력입니다.", "task")
    return name


def parse_tasks(raw: str) -> list[str]:
    """업무명 입력을 여러 개로 나눈다. 줄바꿈과 쉼표를 모두 구분자로 본다.

    초기 구축 때는 조직은 같고 업무만 다른 채널을 한꺼번에 만드는 일이 잦다.
    그때마다 모달을 다시 여는 것은 같은 값을 여러 번 고르게 만든다.

    **중복은 조용히 지우지 않고 막는다.** 같은 이름이 두 번 들어오면 두 번째는
    Slack 이 `name_taken` 으로 거절하는데, 그러면 "일부만 만들어졌다" 는 결과가
    나오고 사람이 무엇이 빠졌는지 되짚어야 한다. 만들기 전에 알려주는 편이 낫다.
    """
    parts = [chunk.strip() for line in (raw or "").splitlines()
             for chunk in line.split(",")]
    tasks = [p for p in parts if p]
    if not tasks:
        raise ChannelNameError("업무명을 입력해 주세요.", "task")

    seen: set[str] = set()
    for task in tasks:
        key = _piece(task).lower()
        if key in seen:
            raise ChannelNameError(
                f"업무명이 겹칩니다: {task}. 같은 채널을 두 번 만들 수 없습니다.", "task"
            )
        seen.add(key)

    if len(tasks) > MAX_BATCH:
        raise ChannelNameError(
            f"한 번에 {MAX_BATCH}개까지 만들 수 있습니다(지금 {len(tasks)}개). "
            "나눠서 만들어 주세요.",
            "task",
        )
    return tasks


def _selected(state: dict, block_id: str, action_id: str) -> dict:
    return state.get(block_id, {}).get(action_id, {})


def _selected_by_action(state: dict, action_id: str) -> tuple[str, dict]:
    for block_id, actions in state.items():
        if action_id in actions:
            return block_id, actions[action_id]
    return "", {}


def request_from_view(view: dict, *, include_channel_options: bool) -> ChannelRequest:
    """Slack view_submission을 채널 요청으로 바꾼다.

    조직코드는 사용자가 입력하지 않는다. 조직 검색 선택값에서 코드와 이름을 함께 읽는다.
    """
    state = (view.get("state") or {}).get("values") or {}
    prefix = (_selected(state, "prefix", "prefix").get("selected_option") or {}).get(
        "value", ""
    )
    org_block_id, org_action = _selected_by_action(state, "org")
    selected_org = org_action.get("selected_option") or {}
    org_code, _derived_prefix, org_name = decode_value(selected_org.get("value", ""))
    if not org_code or not org_name:
        raise ChannelNameError(
            "조직명을 검색해 목록에서 선택해 주세요.", org_block_id or "org_team"
        )
    task = _selected(state, "task", "task").get("value", "")
    visibility = "private"
    members: tuple[str, ...] = ()
    # 생성 화면에만 있는 값들. 기본값을 **여기서** 정해 둔다 - `if` 안에서만
    # 만들면 다른 화면의 제출에서 이름 조립이 UnboundLocalError 로 터진다.
    reviewers: tuple[str, ...] = ()
    send_at = "08:00"
    if include_channel_options:
        visibility = (
            _selected(state, "visibility", "visibility").get("selected_option") or {}
        ).get("value", "private")
        members = tuple(
            _selected(state, "members", "members").get("selected_users") or ()
        )
        reviewers = tuple(
            _selected(state, REVIEWER_BLOCK, "reviewers").get("selected_users") or ()
        )
        if not reviewers:
            raise ChannelNameError(
                "요약 검토자를 한 명 이상 골라 주세요. 검토자가 없으면 요약이 "
                "반영되지 않고, 봇이 읽지 못한 첨부도 아무에게도 가지 않습니다.",
                REVIEWER_BLOCK,
            )
        send_at = (
            _selected(state, SEND_AT_BLOCK, "send_at").get("selected_time") or "08:00"
        )
    request = ChannelRequest(
        prefix, org_name, org_code, task, visibility, members, reviewers, send_at
    )
    _ = request.name
    return request


def requests_from_view(view: dict) -> list[ChannelRequest]:
    """채널 생성 제출을 요청 목록으로 바꾼다. 업무명 줄 수만큼 나온다.

    구분·조직·공개범위·참여자는 모두 같고 업무명만 다르다. 이름 조립은
    `ChannelRequest.name` 이 하므로 여기서 규칙을 다시 쓰지 않는다.
    """
    base = request_from_view(view, include_channel_options=True)
    state = (view.get("state") or {}).get("values") or {}
    tasks = parse_tasks(_selected(state, "task", "task").get("value", ""))

    out = [
        ChannelRequest(base.prefix, base.org_name, base.org_code, task,
                       base.visibility, base.members,
                       base.reviewers, base.send_at)
        for task in tasks
    ]
    # 이름까지 조립해 봐야 규칙 위반이 여기서 드러난다. 만들다가 중간에 실패하면
    # 절반만 만들어진 상태로 끝난다.
    names = [r.name for r in out]
    if len(set(names)) != len(names):
        raise ChannelNameError("같은 채널명이 두 번 나옵니다.", "task")
    return out


def selected_prefix(view: dict, *, default: str = "본사팀") -> str:
    """지금 고른 구분. 구분이 바뀔 때 화면을 다시 그리는 데 쓴다."""
    state = (view.get("state") or {}).get("values") or {}
    picked = (_selected(state, "prefix", "prefix").get("selected_option") or {}).get("value")
    return picked or default


def action_prefix(body: dict, *, default: str = "본사팀") -> str:
    """block_actions의 방금 선택한 구분을 읽는다.

    Slack은 action payload와 view.state를 함께 보내지만 view.state에는 변경 전 값이
    남을 수 있다. 화면을 다시 그릴 때는 actions의 새 값을 우선해야 한다.
    """
    actions = body.get("actions") or []
    selected = (actions[0].get("selected_option") or {}) if actions else {}
    picked = str(selected.get("value") or "")
    if picked in COLLECT_PREFIXES:
        return picked
    return selected_prefix(body.get("view") or {}, default=default)


def typed_task(view: dict) -> str:
    """다시 그릴 때 이미 입력한 업무명을 잃지 않게 한다."""
    state = (view.get("state") or {}).get("values") or {}
    return _selected(state, "task", "task").get("value", "") or ""


def _name_inputs(
    spec: ChannelSpec | None = None,
    *,
    prefix: str = "본사팀",
    defaults: dict | None = None,
    dispatch_prefix: bool = True,
    multi_task: bool = False,
) -> list[dict]:
    """구분·조직 검색·업무명을 받는다. 조직코드는 검색 선택값 안에 숨긴다."""
    options = [
        {"text": {"type": "plain_text", "text": p}, "value": p}
        for p in ("본부", "실", "본사팀", "현장", "업무")
    ]
    # 예전 이름으로 만들어진 채널을 이름 변경할 때 목록에 없는 값이 오지 않게 옮긴다.
    picked = PREFIX_ALIASES.get(prefix, prefix)
    if picked not in {o["value"] for o in options}:
        picked = "본사팀"

    hit = (defaults or {}).get(picked)
    selected_org = OrgHit(spec.org_code, spec.org_name) if spec else hit
    org_block_id = _ORG_BLOCK_IDS[picked]

    return [
        {
            "type": "input",
            "block_id": "prefix",
            # 고르는 즉시 아래 두 칸을 다시 채운다. 이 값이 없으면 Slack 이
            # 선택 사실을 서버에 보내지 않아 자동 채움이 동작하지 않는다.
            "dispatch_action": dispatch_prefix,
            "label": {"type": "plain_text", "text": "조직 구분"},
            "element": {
                "type": "static_select",
                "action_id": "prefix",
                "options": options,
                "initial_option": next(o for o in options if o["value"] == picked),
            },
            "hint": {
                "type": "plain_text",
                "text": "본부 > 본사팀 > 현장, 또는 실 > 본사팀. "
                        "업무는 조직이 아니라 다른 팀과 협업할 때 쓰는 채널이며, "
                        "주관 팀의 조직코드를 그대로 씁니다.",
            },
        },
        {
            "type": "input",
            # views.update는 block_id/action_id가 같으면 기존 선택을 보존한다.
            # 구분마다 block_id를 바꿔야 전산팀 → 경영본부 초기값이 실제로 반영된다.
            "block_id": org_block_id,
            "label": {"type": "plain_text", "text": "조직명"},
            "element": {
                "type": "external_select",
                "action_id": "org",
                "min_query_length": 1,
                "placeholder": {"type": "plain_text", "text": "조직명 또는 코드로 검색"},
                **({"initial_option": option(selected_org)} if selected_org else {}),
            },
            "hint": {
                "type": "plain_text",
                "text": "조직코드는 선택한 조직에서 자동으로 적용됩니다.",
            },
        },
        {
            "type": "input",
            "block_id": "task",
            "label": {
                "type": "plain_text",
                "text": "업무명" + (f" (한 줄에 하나, 최대 {MAX_BATCH}개)"
                                   if multi_task else ""),
            },
            "element": {
                "type": "plain_text_input",
                "action_id": "task",
                # 생성에서만 여러 줄을 받는다. 이름 변경은 채널 하나를 고치는 것이라
                # 여러 줄을 받으면 무엇이 되는지 알 수 없다.
                **({"multiline": True} if multi_task else {}),
                "placeholder": {
                    "type": "plain_text",
                    "text": ("예: 주간회의\n안전점검\n기성청구"
                             if multi_task else "예: 주간회의"),
                },
                **({"initial_value": spec.task} if spec else {}),
            },
            **({
                "hint": {
                    "type": "plain_text",
                    "text": "여러 개를 적으면 조직·공개범위·참여자는 같고 "
                            "업무명만 다른 채널이 한꺼번에 만들어집니다. "
                            "쉼표로 나눠도 됩니다.",
                },
            } if multi_task else {}),
        },
    ]


def create_modal(
    private_metadata: str,
    *,
    prefix: str = "본사팀",
    defaults: dict | None = None,
    task: str = "",
    default_reviewer: str = "",
) -> dict:
    """전역 바로가기와 `/채널 생성`이 공유하는 생성 모달.

    구분이 바뀔 때 이 함수로 다시 만들어 `views_update` 한다. 그래서 이미 입력한
    업무명(`task`)을 인자로 받아 되살린다 — 다시 그렸다고 입력이 사라지면 안 된다.
    """
    blocks = _name_inputs(prefix=prefix, defaults=defaults, multi_task=True)
    if task:
        blocks[-1]["element"]["initial_value"] = task
    blocks.extend(
        [
            {
                "type": "input",
                "block_id": "visibility",
                "label": {"type": "plain_text", "text": "공개 범위"},
                "element": {
                    "type": "radio_buttons",
                    "action_id": "visibility",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "비공개"},
                            "value": "private",
                        },
                        {
                            "text": {"type": "plain_text", "text": "공개"},
                            "value": "public",
                        },
                    ],
                    "initial_option": {
                        "text": {"type": "plain_text", "text": "비공개"},
                        "value": "private",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "members",
                "optional": True,
                "label": {"type": "plain_text", "text": "추가 참여자"},
                "element": {
                    "type": "multi_users_select",
                    "action_id": "members",
                    "placeholder": {"type": "plain_text", "text": "나중에 초대해도 됩니다"},
                },
            },
            # 검토자는 **필수**다. 선택으로 두면 안 정한 채널이 쌓이고, 그 채널은
            # 요약이 반영되지 않고 읽지 못한 첨부도 아무에게도 가지 않는다 —
            # 둘 다 오류 없이 조용하다. 기본값은 만든 사람 자신이다.
            {
                "type": "input",
                "block_id": REVIEWER_BLOCK,
                "label": {"type": "plain_text", "text": "요약 검토자"},
                "element": {
                    "type": "multi_users_select",
                    "action_id": "reviewers",
                    "placeholder": {"type": "plain_text", "text": "검토자를 고르세요"},
                    **(
                        {"initial_users": [default_reviewer]}
                        if default_reviewer
                        else {}
                    ),
                },
                "hint": {
                    "type": "plain_text",
                    "text": "봇이 만든 요약 후보와 읽지 못한 첨부를 매일 이 사람에게 "
                            "DM 으로 보냅니다. 확인한 것만 반영됩니다.",
                },
            },
            {
                "type": "input",
                "block_id": SEND_AT_BLOCK,
                "optional": True,
                "label": {"type": "plain_text", "text": "검토 DM 보낼 시각 (KST)"},
                "element": {
                    "type": "timepicker",
                    "action_id": "send_at",
                    "initial_time": "08:00",
                },
            },
        ]
    )
    return {
        "type": "modal",
        "callback_id": "tybot_create_channel",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "업무 채널 만들기"},
        "submit": {"type": "plain_text", "text": "만들기"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": blocks,
    }


def rename_modal(private_metadata: str, spec: ChannelSpec) -> dict:
    return {
        "type": "modal",
        "callback_id": "tybot_rename_channel",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "채널 이름 변경"},
        "submit": {"type": "plain_text", "text": "변경"},
        "close": {"type": "plain_text", "text": "취소"},
        # 이름변경은 제출 시 선택값을 읽으면 된다. 여기서 prefix 액션을 보내면
        # 생성 모달 전용 views_update 핸들러가 화면을 생성 모달로 바꿔 버린다.
        "blocks": _name_inputs(spec, dispatch_prefix=False),
    }


# --- `/채널 수정` — 한 화면에서 이름·검토자·시각 ------------------------------
#
# 전에는 `/채널 이름변경` 과 `/채널 검토자` 가 따로였고, 이름 변경은 **현재 이름이
# 표준 형식일 때만** 열렸다. 그래서 정작 규칙 밖 이름을 고치려는 사람이 막혔다.
# 여기서는 이름 칸을 **선택 입력**으로 두고, 채운 것만 바꾼다.
EDIT_CALLBACK = "tybot_edit_channel"

@dataclass(frozen=True)
class ChannelEdit:
    """`/채널 수정` 제출 결과. 비어 있는 항목은 **바꾸지 않는다는 뜻**이다."""

    name: str = ""
    reviewers: tuple[str, ...] = ()
    send_at: str = ""
    # 검토자 칸을 비워서 제출했는가. 「그대로 두기」 와 「전부 해제」 를 가른다 —
    # 이 둘을 섞으면 실수로 검토가 멈추고 아무 표시도 안 난다.
    clear_reviewers: bool = False

    @property
    def renames(self) -> bool:
        return bool(self.name)


def edit_modal(
    private_metadata: str,
    *,
    spec: ChannelSpec | None,
    current_name: str = "",
    reviewers: tuple[str, ...] = (),
    send_at: str = "08:00",
    defaults: dict | None = None,
) -> dict:
    """이름·검토자·시각을 한 화면에서 고친다.

    이름 칸은 **선택 입력**이다. 검토자만 바꾸려는 사람에게 조직 검색을 강요하면,
    그 사람은 검토자 지정을 포기한다.
    """
    name_blocks = _name_inputs(
        spec,
        prefix=spec.prefix if spec else "본사팀",
        defaults=defaults,
        dispatch_prefix=False,
    )
    # 이름은 안 건드려도 된다. `prefix` 는 항상 초기값이 있어 비울 수 없으므로
    # 조직·업무만 선택으로 둔다 — 이름 변경은 그 둘이 다 채워졌을 때만 한다.
    for block in name_blocks:
        if block["block_id"] != "prefix":
            block["optional"] = True

    where = current_name or (spec.raw if spec else "")
    head = (
        f"*{where}*" if where else "*채널 수정*"
    ) + "\n이름은 바꿀 때만 채우세요. 검토자만 고쳐도 됩니다."
    if spec is None and where:
        head += (
            "\n\n🔴 지금 이름이 표준 형식이 아니어서 **이 채널은 수집되지 않습니다.** "
            "조직과 업무명을 고르면 표준 이름으로 바꿉니다."
        )

    return {
        "type": "modal",
        "callback_id": EDIT_CALLBACK,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "채널 수정"},
        "submit": {"type": "plain_text", "text": "저장"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": head}},
            {"type": "divider"},
            *name_blocks,
            {"type": "divider"},
            {
                "type": "input",
                "block_id": REVIEWER_BLOCK,
                "optional": True,
                "label": {"type": "plain_text", "text": "요약 검토자"},
                "element": {
                    "type": "multi_users_select",
                    "action_id": "reviewers",
                    "placeholder": {"type": "plain_text", "text": "검토자를 고르세요"},
                    **({"initial_users": list(reviewers)} if reviewers else {}),
                },
                "hint": {
                    "type": "plain_text",
                    "text": "검토자가 없으면 요약을 반영하지 않고, 읽지 못한 첨부도 "
                            "아무에게도 가지 않습니다. 비우고 저장하면 전부 해제됩니다.",
                },
            },
            {
                "type": "input",
                "block_id": SEND_AT_BLOCK,
                "optional": True,
                "label": {"type": "plain_text", "text": "검토 DM 보낼 시각 (KST)"},
                "element": {
                    "type": "timepicker",
                    "action_id": "send_at",
                    "initial_time": send_at or "08:00",
                },
                "hint": {
                    "type": "plain_text",
                    "text": "그날 요약 후보와 확인이 필요한 첨부를 이 시각에 DM 으로 보냅니다.",
                },
            },
        ],
    }


def edit_from_view(view: dict) -> ChannelEdit:
    """`/채널 수정` 제출을 읽는다.

    이름은 **조직과 업무명이 둘 다 채워졌을 때만** 조립한다. 하나만 채운 것은
    실수이므로 그 칸에 오류를 붙여 되돌려 준다 — 조용히 무시하면 사람은 바꿨다고
    믿고 나간다.
    """
    state = (view.get("state") or {}).get("values") or {}
    prefix = (_selected(state, "prefix", "prefix").get("selected_option") or {}).get(
        "value", ""
    )
    org_block_id, org_action = _selected_by_action(state, "org")
    selected_org = org_action.get("selected_option") or {}
    org_code, _derived, org_name = decode_value(selected_org.get("value", ""))
    task = (_selected(state, "task", "task").get("value") or "").strip()

    picked_org = bool(org_code and org_name)
    name = ""
    if picked_org and task:
        name = build_channel_name(prefix, org_name, org_code, task)
    elif picked_org and not task:
        raise ChannelNameError("업무명을 적어 주세요.", "task")
    elif task and not picked_org:
        raise ChannelNameError(
            "조직명을 검색해 목록에서 선택해 주세요.", org_block_id or "org_team"
        )

    reviewer_action = _selected(state, REVIEWER_BLOCK, "reviewers")
    picked = tuple(reviewer_action.get("selected_users") or ())
    # 검토자 칸이 화면에 있었는가. 없던 화면(옛 모달)의 제출을 「전부 해제」 로
    # 읽으면 검토가 조용히 멈춘다.
    had_block = REVIEWER_BLOCK in state
    send_at = (
        _selected(state, SEND_AT_BLOCK, "send_at").get("selected_time") or ""
    )

    return ChannelEdit(
        name=name,
        reviewers=picked,
        send_at=send_at,
        clear_reviewers=had_block and not picked,
    )


class ChannelOwnerStore:
    """TYBot 생성 채널의 최초 요청자와 위임된 수정 담당자를 기록한다."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "channels": {}}
        if not isinstance(data.get("channels"), dict):
            return {"version": 1, "channels": {}}
        return data

    def record(self, workspace: str, channel_id: str, owner_user_id: str, name: str) -> None:
        with _OWNER_LOCK:
            data = self._read()
            key = f"{workspace}:{channel_id}"
            existing = data["channels"].get(key) or {}
            data["channels"][key] = {
                "owner_user_id": owner_user_id,
                "name": name,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "manager_user_ids": list(existing.get("manager_user_ids") or []),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def owner_of(self, workspace: str, channel_id: str) -> str:
        """이 채널을 만든 사람. 없으면 빈 문자열.

        검토자를 안 정한 채널의 **대체 수신자**다. 여기서 빈 값을 받은 쪽은
        조용히 넘어가지 말고 그 사실을 남겨야 한다 — 그 채널의 첨부는 아무도
        확인하지 않는 상태가 된다.
        """
        with _OWNER_LOCK:
            row = self._read()["channels"].get(f"{workspace}:{channel_id}") or {}
        return str(row.get("owner_user_id") or "")

    def managers_of(self, workspace: str, channel_id: str) -> tuple[str, ...]:
        """개설자 외에 `/채널 수정`을 위임받은 사용자들."""
        with _OWNER_LOCK:
            row = self._read()["channels"].get(f"{workspace}:{channel_id}") or {}
        raw = row.get("manager_user_ids") or []
        if not isinstance(raw, list):
            return ()
        return tuple(dict.fromkeys(str(user).strip() for user in raw if str(user).strip()))

    def set_managers(
        self,
        workspace: str,
        channel_id: str,
        manager_user_ids: list[str] | tuple[str, ...],
        *,
        set_by: str,
    ) -> tuple[str, ...]:
        """위임 목록을 교체한다. 개설자 기록은 그대로 보존한다."""
        managers = tuple(
            dict.fromkeys(str(user).strip() for user in manager_user_ids if str(user).strip())
        )
        with _OWNER_LOCK:
            data = self._read()
            key = f"{workspace}:{channel_id}"
            row = data["channels"].get(key)
            if not isinstance(row, dict):
                row = {}
                data["channels"][key] = row
            row["manager_user_ids"] = list(managers)
            row["managers_updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            row["managers_updated_by"] = set_by
            data["version"] = 2
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        return managers

    def owners(self) -> dict[tuple[str, str], str]:
        """`{(워크스페이스, 채널ID): 개설자}` — 한 번 읽어 여러 채널에 쓴다."""
        with _OWNER_LOCK:
            rows = self._read()["channels"]
        out: dict[tuple[str, str], str] = {}
        for key, row in rows.items():
            workspace, _, channel_id = str(key).partition(":")
            owner = str((row or {}).get("owner_user_id") or "")
            if channel_id and owner:
                out[(workspace, channel_id)] = owner
        return out

    def is_owner(self, workspace: str, channel_id: str, user_id: str) -> bool:
        with _OWNER_LOCK:
            row = self._read()["channels"].get(f"{workspace}:{channel_id}") or {}
        return bool(user_id) and row.get("owner_user_id") == user_id

    def is_manager(self, workspace: str, channel_id: str, user_id: str) -> bool:
        """개설자 또는 위임된 TYBot 수정 담당자인가."""
        if not user_id:
            return False
        return self.is_owner(workspace, channel_id, user_id) or user_id in self.managers_of(
            workspace, channel_id
        )
