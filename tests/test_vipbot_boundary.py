"""VIPBot 경계 계약 (B-83) — **쓰는 길에 닿지 않는다.**

## 2026-11-09 로 규칙이 한 축 바뀌었다

전에는 VIPBot 이 별도 답변 제품이었다. 자기 전달 코드(`vipbot_delivery`)로 DM
한 통만 보냈고, 그래서 이 파일은 「Canvas 금지 · Slack 호출 두 개」 를 고정했다.

지금은 **같은 `WorkspaceBot` 의 VIP 구성 분기**다. 다른 것은 **넷**뿐이다 —
모델·프롬프트·비용 상한, 사용자 허용 판정, 근거 채널, 그리고 **상세 답변은
요청자 DM 으로만** 간다(`private_answers`).

넷째가 「DM 한 통」 과 같아 보이지만 다르다. 예전에는 **만드는 것 자체**가 작아서
DM 하나로 끝났고, 그래서 Canvas·첨부·근거 보기가 아예 없었다. 지금은 TYBot 과
똑같이 만들고 **보내는 곳만** 바꾼다. 그래서 Canvas·첨부 금지는 **계약이 아니라
결함**이 됐다 — 같은 질문에 TYBot 은 Canvas, VIP 는 긴 메시지가 나갔다.

전달이 DM 이라고 스코프가 줄지 않는다는 점이 중요하다. Canvas 를 **만드는**
호출은 똑같이 일어나고, 채널에는 고정 안내 한 줄이 나가므로 `chat:write` 도
그대로 필요하다. 「상세는 DM 으로만」 은 매니페스트가 아니라 코드가 지키고,
그 동작은 `test_vip_common_answer_path.py` 가 따로 고정한다.

바뀌지 않은 축은 그대로다. **VIP 는 쓰지 않는다.** 수집·백필·자동 참여·채널
관리·아카이브 쓰기는 여전히 TYBot 하나의 몫이고, 이 파일이 그것을 고정한다.

1. VIP **구성 모듈**의 import 목록을 잠근다. 쓰는 모듈을 들여오면 깨진다
2. 매니페스트의 스코프·이벤트를 잠근다. 스코프는 설치된 뒤에 좁히기 어렵다
3. 매니페스트가 **공통 답변 경로가 실제로 부르는 것**을 덮는지 대조한다.
   덜 주면 VIP 에서만 조용히 다른 답이 나가고, 더 주면 안 쓰는 권한이 남는다

VIP 가 공통 경로를 **실제로 지나가는지**는 `test_vip_common_answer_path.py` 가
동작으로 본다. 이 파일은 선언을 본다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "pilot" / "vipbot-slack-app-manifest.yaml"
TYBOT_MANIFEST = ROOT / "docs" / "pilot" / "slack-app-manifest.yaml"

#: VIP 구성 모듈. **VIP 만의 선택**이 사는 곳이고, 그 선택은 모델·프롬프트·
#: 비용 상한·사용자 판정·근거 좌표뿐이다. 답변을 만드는 코드는 여기 없다 —
#: 생기는 순간 TYBot 과 VIP 의 답이 갈라지기 시작한다.
VIP_CONFIG_SOURCES = (
    "src/tybot/vip_workspace.py",
    "src/tybot/vip_profile.py",
    "src/tybot/vip_access.py",
)

#: 전환 전까지 아직 떠 있는 **독립 런타임**. 새 분기로 절체할 때까지 운영에서
#: 돌고 있으므로 보호를 걷지 않는다. 다만 **동결**이다 — 여기에 기능을 더하면
#: 두 전달 경로가 동시에 자라고, 같은 질문에 두 형식이 나간다.
LEGACY_RUNTIME_SOURCES = ("src/tybot/vipbot_delivery.py",)
LEGACY_EVENT_SOURCES = ("src/tybot/vipbot_event.py",)
LEGACY_BOOT_SOURCES = ("src/tybot/vipbot_runtime.py",)

#: 공통 답변 경로. 매니페스트가 덮어야 할 Slack 호출이 여기서 나온다.
COMMON_ANSWER_SOURCES = (
    "src/tybot/slack/pilot.py",
    "src/tybot/slack/progress.py",
    "src/tybot/canvas_answer.py",
)

#: VIP 구성 모듈이 들여올 수 있는 전부. **목록이 곧 경계다.**
CONFIG_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "logging",
    "math",
    "os",
    "re",
    "tybot.access",
    "tybot.answer",
    "tybot.archive.store",
    "tybot.bot_access",
    "tybot.db",
    "tybot.gateway.router",
    "tybot.identity",
    "tybot.slack.pilot",
    "tybot.vip_access",
    "tybot.vip_profile",
}

LEGACY_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "logging",
    "tybot.bot_access",
}

LEGACY_EVENT_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "re",
    "typing",
    "tybot.access",
    "tybot.bot_access",
    "tybot.vip_access",
    "tybot.vipbot_delivery",
}

#: 쓰는 길. 하나라도 import 되면 계약 위반이다.
#:
#: `archive.store` 는 여기 없다 — **읽는** 쪽이고, 공통 답변 엔진이 그것으로
#: 근거를 연다. 읽기와 쓰기를 한 목록에 섞으면 둘 중 하나는 반드시 틀린다.
FORBIDDEN_IMPORTS = {
    "tybot.archive.writer",
    "tybot.archive.attachment_writer",
    "tybot.archive.revision_store",
    "tybot.archive.write_owner",
    "tybot.archive.backfill",
    "tybot.archive.backfill_runner",
    "tybot.archive.backfill_adapter",
    "tybot.archive.files",
    "tybot.archive.channel_files",
    "tybot.archive.attachment_reader",
    "tybot.archive.attachment_ack",
    "tybot.archive.ingest_ack",
    "tybot.archive.canvas",
    "tybot.archive.supervisor",
    "tybot.archive.dm_inbox",
    "tybot.archive.dm_consumer",
    "tybot.archive.convert",
    "tybot.archive.conversion_worker",
}

#: 수집·참여·채널 관리 쪽 Slack API. VIP 구성·legacy 런타임에서는 이름만 나와도
#: 안 된다. (공통 경로에는 있다 — 거기서는 TYBot 이 그 일을 한다.)
FORBIDDEN_SLACK_CALLS = (
    "conversations_join",
    "conversations_history",
    "conversations_replies",
    "conversations_invite",
    "conversations_create",
    "files_upload",
    "files_getUploadURLExternal",
    "admin_conversations",
)

#: 공통 답변 경로가 부르는 Slack 메서드 → 그 호출에 필요한 봇 스코프.
#:
#: 이 표는 **양쪽으로** 검사한다. 매니페스트에 스코프가 있어야 하고, 그 메서드가
#: 공통 경로 소스에 실제로 있어야 한다. 한쪽만 보면 표가 소설이 된다 —
#: 코드에서 사라진 호출 때문에 안 쓰는 권한이 남거나, 새로 생긴 호출이 표에
#: 없어서 VIP 에서만 조용히 실패한다.
ANSWER_PATH_SCOPES = {
    "chat_postMessage": "chat:write",
    "chat_update": "chat:write",
    "chat_postEphemeral": "chat:write",
    "reactions_add": "reactions:write",
    "canvases_create": "canvases:write",
    "canvases_access_set": "canvases:write",
    "files_info": "files:read",
    "users_info": "users:read",
    "conversations_info": "channels:read",
    "conversations_open": "im:write",
}

#: 스코프를 쓰지 않는 호출. 상호작용 설정이 켜져 있어야 동작한다.
TRIGGER_ONLY_CALLS = ("views_open",)

#: 봇 스코프가 필요 없는 호출.
NO_SCOPE_CALLS = {"views_open", "views_update", "auth_test"}

#: 공통 봇 안에 있지만 **TYBot 만 지나가는** 길. VIP 는 수집·채널 관리·멤버십
#: 조회를 하지 않으므로 여기 스코프를 받지 않는다.
#:
#: - 수집: `_ingest_live` · `_ingest_dm` · `_ingest_channel`
#: - 채널 관리: `/채널` 명령과 전역 바로가기
#: - 멤버십 범위: `_context` 의 기본 갈래. VIP 는 `request_context_resolver` 로
#:   관리형 좌표를 받으므로 이 갈래에 들어가지 않는다
TYBOT_ONLY_CALLS = {
    "conversations_join",
    "conversations_history",
    "conversations_replies",
    "conversations_invite",
    "conversations_create",
    "conversations_rename",
    "conversations_list",
    "users_conversations",
}


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def imported(relative: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source(relative))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.add(node.module or "")
        elif isinstance(node, ast.ImportFrom):
            # `from .answer import ...` 는 `tybot.answer` 와 같은 경계다.
            # `from . import bot_access, identity` 는 **두 모듈**이다 — 하나로
            # 뭉뚱그리면 그 줄로 무엇이든 들여올 수 있게 된다.
            if node.module:
                found.add(f"tybot.{node.module}")
            else:
                found.update(f"tybot.{alias.name}" for alias in node.names)
    return found


def client_calls(relative: str) -> set[str]:
    """`client.<something>(...)` 로 부른 이름 전부."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source(relative))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "client"
        ):
            found.add(node.func.attr)
    return found


def manifest() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def declarations() -> str:
    """주석을 걷어낸 매니페스트. 주석은 「무엇을 안 넣었는지」 를 설명하므로
    금지어 검사를 그대로 돌리면 설명문에 걸린다. 설치되는 것은 선언뿐이다."""
    return "\n".join(raw.split("#", 1)[0] for raw in manifest().splitlines())


def declared(section: str) -> list[str]:
    """`bot:` 또는 `bot_events:` 아래 적힌 항목. 주석은 세지 않는다."""
    text = manifest()
    start = text.index(f"{section}:") + len(section) + 1
    items: list[str] = []
    for raw in text[start:].splitlines():
        if not raw.strip():
            continue
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.lstrip().startswith("- "):
            break
        items.append(line.strip()[2:].strip())
    return items


# --- 1. VIP 구성은 쓰는 길에 닿지 않는다 ---------------------------------------


@pytest.mark.parametrize("relative", VIP_CONFIG_SOURCES)
def test_vip_configuration_imports_nothing_outside_its_whitelist(relative):
    extra = imported(relative) - CONFIG_ALLOWED_IMPORTS
    assert not extra, f"VIP 구성이 새 모듈을 들여왔다: {sorted(extra)}"


@pytest.mark.parametrize(
    "relative", VIP_CONFIG_SOURCES + LEGACY_RUNTIME_SOURCES + LEGACY_EVENT_SOURCES
)
def test_no_vip_module_imports_a_writer_collector_or_backfill_path(relative):
    assert not imported(relative) & FORBIDDEN_IMPORTS


@pytest.mark.parametrize("relative", VIP_CONFIG_SOURCES)
def test_vip_configuration_never_calls_slack_itself(relative):
    # VIP 구성은 **판정과 설정**이다. Slack 을 직접 부르기 시작하면 공통 경로
    # 밖에 두 번째 전달 코드가 생긴다 — 2026-11-09 에 걷어낸 그 구조다.
    assert client_calls(relative) == set()


def test_the_forbidden_list_names_modules_that_actually_exist():
    # 오타 난 금지 목록은 아무것도 막지 않는다. 그런 목록은 있는 것이 더 나쁘다.
    for name in FORBIDDEN_IMPORTS:
        path = ROOT / "src" / Path(name.replace(".", "/") + ".py")
        assert path.is_file(), f"금지 목록의 모듈이 없다: {name}"


def test_vip_configuration_owns_only_model_access_and_evidence_choices():
    """VIP 구성이 고르는 것은 **설정값**이다 — 모델·사용자 판정·근거 좌표.

    넷째 차이인 전달 방식은 공통 봇의 `private_answers` 플래그이고, 그것도
    여기서 Slack 을 부르지 않는다.

    답변을 **만드는** 코드가 여기 생기면 그때부터 TYBot 과 VIP 의 답이 갈라진다.
    갈라진 것은 조용하다: 두 봇이 같은 질문에 다른 형식으로 답해도 아무 시험도
    깨지지 않고, 사람만 "VIP 는 왜 이래?" 하고 묻는다.
    """
    config = "\n".join(source(relative) for relative in VIP_CONFIG_SOURCES)
    for own_delivery in (
        "def answer(",
        "def compose(",
        "def deliver(",
        "chat_postMessage",
        "canvases_create",
    ):
        assert own_delivery not in config, f"VIP 구성이 전달 코드를 가졌다: {own_delivery}"


# --- 2. 전환 전까지 독립 런타임은 동결 -----------------------------------------


@pytest.mark.parametrize("relative", LEGACY_RUNTIME_SOURCES)
def test_the_legacy_runtime_imports_nothing_outside_its_whitelist(relative):
    extra = imported(relative) - LEGACY_ALLOWED_IMPORTS
    assert not extra, f"동결된 legacy 런타임이 새 모듈을 들여왔다: {sorted(extra)}"


@pytest.mark.parametrize("relative", LEGACY_RUNTIME_SOURCES)
def test_the_legacy_delivery_grows_no_new_slack_calls(relative):
    """예전 규칙은 「VIP 는 DM 한 통만 보낸다」 였다. 지금 규칙은 **동결**이다.

    이 모듈은 절체될 때까지만 산다. 여기에 Canvas·첨부·근거 보기를 더하면 공통
    경로와 나란히 자라고, 그러면 절체할 것이 두 배가 된다. 더할 곳은 공통
    경로다 — 그래야 TYBot 과 VIP 가 **같이** 바뀐다.
    """
    assert client_calls(relative) == {"conversations_open", "chat_postMessage"}


@pytest.mark.parametrize(
    "relative", LEGACY_RUNTIME_SOURCES + LEGACY_EVENT_SOURCES + VIP_CONFIG_SOURCES
)
def test_no_collection_or_channel_management_api_is_even_named(relative):
    text = source(relative)
    for name in FORBIDDEN_SLACK_CALLS:
        assert name not in text


@pytest.mark.parametrize("relative", LEGACY_EVENT_SOURCES)
def test_the_legacy_event_coordinator_imports_no_writer_or_collector(relative):
    assert imported(relative) == LEGACY_EVENT_ALLOWED_IMPORTS


@pytest.mark.parametrize("relative", LEGACY_EVENT_SOURCES)
def test_the_legacy_event_coordinator_never_calls_slack_directly(relative):
    assert client_calls(relative) == set()


def test_the_shared_branch_does_not_call_the_legacy_delivery():
    """공통 분기는 `vipbot_delivery` 를 부르지 않는다.

    예전 이 자리의 시험은 「기존 코드가 VIP 경로를 물면 안 된다」 였다. 그 전제는
    VIPBot 이 **별도 프로세스**라는 것이었고, 지금은 아니다 — 공통 런타임이 VIP
    토큰으로 `WorkspaceBot` 을 하나 더 만든다. 토큰이 섞이는 것은
    `pilot._require_distinct_runtime_tokens` 가 따로 막는다.

    남은 금지는 **전달 경로가 둘이 되는 것**이다. 공통 분기가 legacy delivery 를
    부르기 시작하면, 같은 질문이 어느 날은 공통 형식으로 어느 날은 DM 한 통으로
    나간다. 그 차이는 로그만 봐서는 보이지 않는다.
    """
    owned = set(LEGACY_RUNTIME_SOURCES + LEGACY_EVENT_SOURCES + LEGACY_BOOT_SOURCES)
    callers = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if path.relative_to(ROOT).as_posix() not in owned
        and "vipbot_delivery" in path.read_text(encoding="utf-8")
    ]
    assert callers == [], f"공통 코드가 legacy 전달 경로를 물었다: {callers}"


# --- 3. 매니페스트: 따로 설치되는 별도 앱 --------------------------------------


def test_it_is_its_own_app_with_its_own_name():
    text = manifest()
    assert "name: VIPBot" in text
    assert "display_name: vipbot" in text
    assert MANIFEST != TYBOT_MANIFEST
    assert "VIPBot" not in TYBOT_MANIFEST.read_text(encoding="utf-8")


def test_socket_mode_only_and_no_new_inbound_port():
    text = declarations()
    assert "socket_mode_enabled: true" in text
    # request_url 이 하나라도 있으면 그 주소로 들어오는 길이 생긴다.
    # 상호작용을 켜도 이것은 변하지 않는다 — 페이로드는 Socket Mode 로 온다.
    assert "request_url" not in text


def test_interactivity_is_on_so_the_evidence_button_actually_opens():
    """「근거 보기」 는 공통 답변이 근거로 답할 때마다 붙는다.

    상호작용이 꺼져 있으면 버튼은 그려지는데 눌러도 아무 일이 없다. 코드 주석이
    말하는 「버튼이 있는데 눌러도 아무것도 안 나오면 없는 것만 못하다」 가
    VIP 에서만 재현되는 상태다.
    """
    assert "is_enabled: true" in declarations()
    assert "ACTION_SHOW" in source("src/tybot/evidence_view.py")


def test_only_the_scopes_the_common_answer_path_needs_are_declared():
    assert set(declared("bot")) == {
        "app_mentions:read",
        "chat:write",
        # 질문 첨부(`question_files`)와 Canvas permalink(`files.info`).
        "files:read",
        # Canvas 답변. 없으면 VIP 만 조용히 메시지로 되돌아간다.
        "canvases:write",
        # 👀 수신 표시. 공통 경로가 요청마다 무조건 부른다.
        "reactions:write",
        # 채널 이름 한 줄. 범위 판정은 channel_id 로 한다.
        "channels:read",
        "groups:read",
        "im:history",
        "im:write",
        "users:read",
        "users:read.email",
    }


def test_only_the_two_events_it_answers_are_subscribed():
    assert set(declared("bot_events")) == {"app_mention", "message.im"}


@pytest.mark.parametrize("method,scope", sorted(ANSWER_PATH_SCOPES.items()))
def test_every_slack_call_in_the_common_answer_path_has_its_scope(method, scope):
    """매니페스트가 **코드가 부르는 것**을 덮는지 본다.

    덜 주면 VIP 에서만 조용히 다른 답이 나간다 — 공통 경로는 Canvas 실패도
    반응 실패도 삼키고 메시지로 내려가게 돼 있다. 사람은 "VIP 는 왜 캔버스가
    안 와요?" 라고 묻고, 로그에는 경고 한 줄만 남는다.
    """
    common = "\n".join(source(relative) for relative in COMMON_ANSWER_SOURCES)
    assert f"{method}(" in common, f"표에만 있고 코드에 없는 호출: {method}"
    assert scope in declared("bot"), f"{method} 를 부르는데 {scope} 가 없다"


def test_every_slack_call_in_the_shared_bot_is_classified():
    """공통 봇에 **새 Slack 호출**이 생기면 여기서 분류하게 만든다.

    표를 한 방향으로만 보면 구멍이 남는다 — 표에 없는 호출이 공통 경로에
    생겨도 아무도 모르고, VIP 토큰은 그 스코프가 없으니 **VIP 에서만** 실패한다.
    그런 실패는 공통 경로가 전부 삼키게 돼 있어(예외를 잡고 메시지로 내려간다)
    로그 경고 한 줄로 끝난다.

    분류지 금지가 아니다. 새 호출이 VIP 답변 경로면 스코프를 주고, TYBot 만
    지나가면 `TYBOT_ONLY_CALLS` 에 적는다. 적는 순간 **왜** 그런지가 남는다.
    """
    seen: set[str] = set()
    for relative in COMMON_ANSWER_SOURCES:
        seen |= client_calls(relative)
    known = set(ANSWER_PATH_SCOPES) | NO_SCOPE_CALLS | TYBOT_ONLY_CALLS
    assert seen <= known, f"분류되지 않은 Slack 호출: {sorted(seen - known)}"


def test_the_tybot_only_calls_really_have_no_vip_scope():
    # 목록이 「VIP 는 안 쓴다」 고 말하는데 스코프가 있으면 둘 중 하나가 거짓말이다.
    granted = set(declared("bot"))
    assert "channels:history" not in granted
    assert "groups:history" not in granted
    assert "channels:join" not in granted
    assert "channels:manage" not in granted


@pytest.mark.parametrize("method", TRIGGER_ONLY_CALLS)
def test_trigger_based_calls_need_interactivity_not_a_scope(method):
    common = "\n".join(source(relative) for relative in COMMON_ANSWER_SOURCES)
    assert f"{method}(" in common
    assert "is_enabled: true" in declarations()


@pytest.mark.parametrize(
    "scope",
    [
        "channels:history",
        "groups:history",
        "mpim:history",
        "channels:join",
        "channels:manage",
        "groups:write",
        "files:write",
        "canvases:read",
        "commands",
        "reactions:read",
    ],
)
def test_no_collection_joining_writing_or_command_scope_is_granted(scope):
    assert scope not in declared("bot")


@pytest.mark.parametrize(
    "event",
    [
        "message.channels",
        "message.groups",
        "channel_created",
        "channel_rename",
        "channel_archive",
        "channel_deleted",
        "group_archive",
        "group_deleted",
        "member_joined_channel",
        "file_shared",
        "reaction_added",
        "reaction_removed",
        "app_home_opened",
    ],
)
def test_no_collection_or_lifecycle_event_is_subscribed(event):
    assert event not in declared("bot_events")


def test_no_reaction_event_is_subscribed_because_nothing_handles_one():
    """받기만 하고 안 쓰는 이벤트는 로그에만 남는다.

    👀 를 **붙이는** 것(`reactions:write`)과 남의 반응을 **읽는** 것은 다른
    스코프다. 공통 봇에 반응 처리기가 생기면 그때 같이 연다.
    """
    pilot = source("src/tybot/slack/pilot.py")
    assert 'event("reaction_added")' not in pilot
    assert 'event("reaction_removed")' not in pilot


def test_no_user_token_scopes_are_requested():
    # 사용자 토큰은 사람을 대신해 행동한다. VIPBot 은 그럴 일이 없다.
    text = declarations()
    assert "user:" not in text.split("scopes:", 1)[1].split("settings:", 1)[0]


def test_the_archiving_app_keeps_the_collection_scopes_and_vipbot_does_not():
    # 수집은 TY Archiving 의 일이다. 두 앱이 같은 원문을 모으면 어느 쪽이 쓴
    # 줄인지 알 수 없고, 되돌릴 때 한쪽만 되돌아간다.
    archiving = (ROOT / "docs" / "pilot" / "archiving-app-manifest.yaml").read_text(
        encoding="utf-8"
    )
    assert "channels:history" in archiving
    assert "channels:history" not in declared("bot")
