"""VIPBot 경계 계약 (B-83) — **쓰는 길에 닿지 않는다.**

VIPBot 은 묻는 사람 한 명에게 답하는 봇이다. 수집·백필·첨부 보관·Canvas 쓰기에
닿을 이유가 없고, 닿을 **수단**도 없어야 한다.

「부르지 않겠다」 는 약속은 다음 수정에서 죽는다. 그래서 두 가지를 파일로 고정한다.

1. **import 목록 전체**를 화이트리스트로 잠근다. 쓰는 모듈을 들여오는 순간 깨진다
2. **Slack 호출 이름**을 둘로 잠근다. 매니페스트에 없는 API 를 부르면 깨진다

그리고 매니페스트 자체 — 스코프는 설치된 뒤에 좁히기 어렵다. 한 번 승인되면
워크스페이스마다 다시 받아야 하고, 그 사이 토큰은 넓은 권한을 들고 돌아다닌다.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "pilot" / "vipbot-slack-app-manifest.yaml"
TYBOT_MANIFEST = ROOT / "docs" / "pilot" / "slack-app-manifest.yaml"

#: VIPBot 이 소유한 파일. 늘어나면 여기 적고, 적는 순간 아래 계약이 전부 적용된다.
VIPBOT_SOURCES = ("src/tybot/vipbot_delivery.py",)

#: VIPBot 이 들여올 수 있는 전부. **목록이 곧 경계다.**
ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "logging",
    "tybot.bot_access",
}

#: 쓰는 길. 하나라도 import 되면 계약 위반이다.
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
    "tybot.archive.store",
    "tybot.archive.convert",
    "tybot.archive.conversion_worker",
}

#: 부를 수 있는 Slack 메서드. 매니페스트의 스코프와 **같은 크기**여야 한다.
ALLOWED_SLACK_CALLS = {"conversations_open", "chat_postMessage"}

#: 수집·참여·파일·Canvas 쪽 Slack API. 이름만 나와도 안 된다.
FORBIDDEN_SLACK_CALLS = (
    "conversations_join",
    "conversations_history",
    "conversations_replies",
    "conversations_invite",
    "conversations_create",
    "files_upload",
    "files_info",
    "files_getUploadURLExternal",
    "canvases_create",
    "canvases_edit",
    "admin_conversations",
)


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def imported(relative: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source(relative))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.add(node.module or "")
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


# --- 1. 쓰는 길에 닿지 않는다 -------------------------------------------------


@pytest.mark.parametrize("relative", VIPBOT_SOURCES)
def test_vipbot_imports_nothing_outside_its_whitelist(relative):
    extra = imported(relative) - ALLOWED_IMPORTS
    assert not extra, f"VIPBot 이 새 모듈을 들여왔다: {sorted(extra)}"


@pytest.mark.parametrize("relative", VIPBOT_SOURCES)
def test_vipbot_imports_no_writer_collector_backfill_or_attachment_path(relative):
    assert not imported(relative) & FORBIDDEN_IMPORTS


def test_the_forbidden_list_names_modules_that_actually_exist():
    # 오타 난 금지 목록은 아무것도 막지 않는다. 그런 목록은 있는 것이 더 나쁘다.
    for name in FORBIDDEN_IMPORTS:
        path = ROOT / "src" / Path(name.replace(".", "/") + ".py")
        assert path.is_file(), f"금지 목록의 모듈이 없다: {name}"


@pytest.mark.parametrize("relative", VIPBOT_SOURCES)
def test_vipbot_calls_only_the_two_slack_methods_it_declared(relative):
    assert client_calls(relative) == ALLOWED_SLACK_CALLS


@pytest.mark.parametrize("relative", VIPBOT_SOURCES)
def test_no_collection_or_canvas_api_is_even_named(relative):
    text = source(relative)
    for name in FORBIDDEN_SLACK_CALLS:
        assert name not in text


def test_no_tybot_module_reaches_into_the_vip_path_yet():
    # VIPBot 은 **별도 앱**이다. 기존 봇 코드가 이것을 부르기 시작하면 두 앱의
    # 토큰과 권한이 한 프로세스에서 섞인다.
    callers = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if path.relative_to(ROOT).as_posix() not in VIPBOT_SOURCES
        and "vipbot_delivery" in path.read_text(encoding="utf-8")
    ]
    assert callers == [], f"기존 코드가 VIP 경로를 물었다: {callers}"


# --- 2. 매니페스트: 따로 설치되는 별도 앱 --------------------------------------


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
    assert "request_url" not in text
    assert "is_enabled: false" in text


def test_only_the_scopes_an_answer_and_a_dm_need_are_declared():
    assert set(declared("bot")) == {
        "app_mentions:read",
        "chat:write",
        "im:history",
        "im:write",
        "users:read",
        "users:read.email",
    }


def test_only_the_two_events_it_answers_are_subscribed():
    assert set(declared("bot_events")) == {"app_mention", "message.im"}


@pytest.mark.parametrize(
    "scope",
    [
        "channels:history",
        "groups:history",
        "channels:join",
        "channels:manage",
        "channels:read",
        "groups:read",
        "files:read",
        "files:write",
        "canvases:write",
        "canvases:read",
        "commands",
        "reactions:read",
        "reactions:write",
    ],
)
def test_no_collection_joining_file_or_canvas_scope_is_granted(scope):
    assert scope not in declared("bot")


@pytest.mark.parametrize(
    "event",
    [
        "message.channels",
        "message.groups",
        "channel_created",
        "channel_rename",
        "member_joined_channel",
        "file_shared",
        "reaction_added",
    ],
)
def test_no_collection_event_is_subscribed(event):
    assert event not in declared("bot_events")


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
