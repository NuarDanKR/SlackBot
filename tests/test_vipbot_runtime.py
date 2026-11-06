"""VIPBot Socket Mode 런타임 시험 (B-83). **Slack·DB·LLM 호출 0회.**

여기 쓰는 값은 전부 합성이다. 실제 토큰·사번·채널·질문을 넣지 않는다.

붙들고 있는 것은 다섯이다.

1. 설정이 비면 **공용 값으로 메우지 않고** 기동을 멈춘다
2. 신원은 `auth_test` · DB · 이벤트 봉투 **셋이 같을 때만** 처리한다
3. 봉투가 어긋나면 권한 조회·LLM·Slack 호출 **전에** 끝난다
4. 사용자 `--model=` 지정은 엔진에 닿지 않는다
5. 수집·백필·첨부·실시간 조회 경로는 **import 조차 하지 않는다**
"""

from __future__ import annotations

import ast
import logging
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

import pytest

from tybot import vipbot_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "tybot" / "vipbot_runtime.py"
UNIT = ROOT / "deploy" / "tybot-vipbot.service"

WS = "pilot"
TEAM = "T0VIPTEAM"
BOT = "U0BOT0001"
USER = "U0VIP0001"
CH = "C0CHAN001"
DM = "D0DM00001"
QUESTION = "합성 질문 ZZZQUESTIONZZZ"
ANSWER = "합성 상세 답변 ZZZANSWERZZZ"

#: 런타임이 들여올 수 있는 전부. **목록이 곧 경계다.**
ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "contextlib",
    "dataclasses",
    "logging",
    "os",
    "pathlib",
    "tybot.vip_profile",
    "tybot.vipbot_anthropic_credential",
    "tybot.vipbot_engine",
    "tybot.vipbot_event",
    "tybot.vipbot_event_claims",
    "tybot.vipbot_runtime_access",
    "tybot.vipbot_runtime_store",
}

#: 쓰는 길과 실시간 조회. 하나라도 들어오면 계약 위반이다.
FORBIDDEN_IMPORTS = {
    "tybot.archive.writer",
    "tybot.archive.attachment_writer",
    "tybot.archive.backfill",
    "tybot.archive.files",
    "tybot.archive.channel_files",
    "tybot.archive.canvas",
    "tybot.archive.supervisor",
    "tybot.slack.pilot",
}

#: 공용 설정 이름. 런타임이 이 이름을 **보기만 해도** 폴백이 생긴 것이다.
SHARED_ENV = ("ARCHIVE_DIR", "DATABASE_URL", "QA_LOG_DIR", "TYBOT_ENV_FILE")


def env(**changes) -> dict:
    values = {
        "VIPBOT_WORKSPACE": WS,
        "VIPBOT_ARCHIVE_DIR": "",
        "VIPBOT_DAILY_LIMIT_USD": "12.50",
    }
    values.update(changes)
    return {key: value for key, value in values.items() if value is not None}


def settings(tmp_path, **changes):
    return runtime.load_settings(env(VIPBOT_ARCHIVE_DIR=str(tmp_path), **changes))


def code(call) -> str:
    with pytest.raises(runtime.VipbotRuntimeError) as caught:
        call()
    return caught.value.code


# --- 대역 -------------------------------------------------------------------


@dataclass
class Answer:
    text: str = ANSWER
    citations: list = field(default_factory=lambda: ["[전산팀]공지, 📄문서(2026-11-06)"])

    def to_slack(self) -> str:
        return f"{self.text}\n\n출처:\n• {self.citations[0]}"


class Engine:
    def __init__(self, answer: Answer | None = None):
        self.questions: list[str] = []
        self._answer = answer or Answer()

    def answer(self, question, ctx):
        self.questions.append(question)
        return self._answer


class Client:
    def __init__(self, identity=None, raises=None):
        self.identity = identity
        self.raises = raises
        self.calls: list[tuple] = []

    def auth_test(self):
        self.calls.append(("auth_test",))
        if self.raises is not None:
            raise self.raises
        return self.identity

    def conversations_open(self, *, users):
        self.calls.append(("conversations_open", users))
        return {"channel": {"id": DM}}

    def chat_postMessage(self, *, channel, text, thread_ts=None):
        self.calls.append(("chat_postMessage", channel, text))
        return {"ok": True}

    def names(self):
        return [call[0] for call in self.calls]


class Claims:
    def __init__(self, allow=True):
        self.allow = allow
        self.seen: list[str] = []

    def claim(self, event_id, *, now=None):
        self.seen.append(event_id)
        return self.allow


def connection(**changes) -> dict:
    values = {
        "team_id": TEAM,
        "bot_user_id": BOT,
        "bot_token": "synthetic-bot-token",
        "app_token": "synthetic-app-token",
    }
    values.update(changes)
    return values


def body(**changes) -> dict:
    event = {
        "type": "app_mention",
        "channel_type": "channel",
        "user": USER,
        "channel": CH,
        "ts": "1762400000.000100",
        "text": f"<@{BOT}> {QUESTION}",
    }
    event.update(changes.pop("event", {}))
    values = {"team_id": TEAM, "event_id": "Ev0000000001", "event": event}
    values.update(changes)
    return values


def deliver(payload, *, engine=None, claims=None, config=None, tmp_path, authorize=None):
    client = Client()
    engine = engine or Engine()
    monkey = authorize or (lambda **_kwargs: _authorized())
    original = runtime.authorize_runtime
    runtime.authorize_runtime = lambda _connect, **kwargs: monkey(**kwargs)
    try:
        result = runtime.handle_body(
            payload,
            client,
            settings=settings(tmp_path),
            config=config or connection(),
            claims=claims or Claims(),
            engine=engine,
        )
    finally:
        runtime.authorize_runtime = original
    return result, client, engine


def _authorized():
    from tybot.vip_access import Authorization

    return Authorization(
        True,
        "allowed",
        WS,
        USER,
        emp_no="E1",
        grant_id=7,
        sources=frozenset({(WS, "C0SRC0001")}),
    )


def _denied(code_word="no_grant"):
    from tybot.vip_access import Authorization

    return Authorization(False, code_word, WS, USER)


# --- 1. 설정은 공용 값으로 메우지 않는다 ---------------------------------------


def test_every_vipbot_setting_is_required(tmp_path):
    got = runtime.load_settings(env(VIPBOT_ARCHIVE_DIR=str(tmp_path)))
    assert got.workspace == WS
    assert got.archive_dir == tmp_path
    assert got.profile.daily_limit_usd == 12.5
    assert got.profile.cost_state_path == "/var/lib/tybot-vipbot/cost-state.json"
    assert got.profile.model == "claude-opus-5-5"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"VIPBOT_WORKSPACE": None}, runtime.WORKSPACE_UNCONFIGURED),
        ({"VIPBOT_WORKSPACE": ""}, runtime.WORKSPACE_UNCONFIGURED),
        ({"VIPBOT_WORKSPACE": "   "}, runtime.WORKSPACE_UNCONFIGURED),
        ({"VIPBOT_WORKSPACE": "질문처럼 생긴 값"}, runtime.WORKSPACE_UNCONFIGURED),
        ({"VIPBOT_WORKSPACE": "9starts-with-digit"}, runtime.WORKSPACE_UNCONFIGURED),
        ({"VIPBOT_DAILY_LIMIT_USD": None}, "vip_cost_limit_unconfigured"),
        ({"VIPBOT_DAILY_LIMIT_USD": "0"}, "vip_cost_limit_invalid"),
        ({"VIPBOT_DAILY_LIMIT_USD": "-1"}, "vip_cost_limit_invalid"),
        ({"VIPBOT_MODEL": "claude-haiku-5-5"}, "vip_model_not_approved"),
    ],
)
def test_a_missing_or_unsafe_setting_stops_the_boot(tmp_path, changes, expected):
    assert code(lambda: settings(tmp_path, **changes)) == expected


@pytest.mark.parametrize(
    ("archive", "expected"),
    [
        ("", "vipbot_archive_unconfigured"),
        ("   ", "vipbot_archive_unconfigured"),
        ("relative/archive", "vipbot_archive_unconfigured"),
    ],
)
def test_a_relative_or_missing_archive_is_refused(archive, expected):
    assert code(lambda: runtime.load_settings(env(VIPBOT_ARCHIVE_DIR=archive))) == expected


def test_an_archive_path_that_is_not_a_directory_is_refused(tmp_path):
    missing = tmp_path / "nope"
    assert (
        code(lambda: runtime.load_settings(env(VIPBOT_ARCHIVE_DIR=str(missing))))
        == "vipbot_archive_unreadable"
    )


@pytest.mark.parametrize("shared", SHARED_ENV)
def test_a_shared_setting_never_fills_a_missing_vipbot_one(tmp_path, shared):
    """공용 값이 있어도 **VIPBot 값이 비면 기동하지 않는다.**"""
    values = env(VIPBOT_ARCHIVE_DIR=None, VIPBOT_WORKSPACE=None)
    values[shared] = str(tmp_path)
    assert code(lambda: runtime.load_settings(values)) == runtime.WORKSPACE_UNCONFIGURED


def test_the_runtime_source_never_names_a_shared_setting():
    body_text = SOURCE.read_text(encoding="utf-8")
    code_only = "\n".join(
        line for line in body_text.splitlines() if not line.lstrip().startswith("#")
    )
    for name in SHARED_ENV:
        assert f'"{name}"' not in code_only
    assert "getenv" not in code_only


# --- 2. DB 연결과 신원 --------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"team_id": ""},
        {"team_id": "not-a-team"},
        {"bot_user_id": ""},
        {"bot_token": ""},
        {"app_token": ""},
    ],
)
def test_a_half_filled_connection_row_stops_the_boot(monkeypatch, changes):
    monkeypatch.setattr(
        runtime, "load_runtime_config", lambda _ws: connection(**changes)
    )
    assert code(lambda: runtime.load_connection(WS)) == runtime.CONNECTION_INCOMPLETE


def test_a_complete_connection_row_is_normalized(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "load_runtime_config",
        lambda _ws: connection(team_id=f"  {TEAM.lower()}  ", bot_user_id=BOT.lower()),
    )
    got = runtime.load_connection(WS)
    assert got["team_id"] == TEAM and got["bot_user_id"] == BOT


def test_identity_must_match_the_database_row():
    client = Client({"team_id": TEAM, "user_id": BOT})
    assert runtime.verify_identity(client, connection()) == {
        "team_id": TEAM,
        "bot_user_id": BOT,
    }


@pytest.mark.parametrize(
    "identity",
    [
        {"team_id": "T0OTHERTEAM", "user_id": BOT},
        {"team_id": TEAM, "user_id": "U0OTHER01"},
        {"team_id": "", "user_id": BOT},
        {"team_id": TEAM, "user_id": ""},
        {},
    ],
)
def test_a_token_pointing_at_another_workspace_stops_the_boot(identity):
    client = Client(identity)
    assert code(lambda: runtime.verify_identity(client, connection())) in {
        runtime.IDENTITY_MISMATCH,
        runtime.IDENTITY_UNVERIFIED,
    }


def test_an_unreachable_auth_test_is_not_an_identity(caplog):
    client = Client(raises=RuntimeError(f"slack said {ANSWER}"))
    with caplog.at_level(logging.DEBUG):
        got = code(lambda: runtime.verify_identity(client, connection()))
    assert got == runtime.IDENTITY_UNVERIFIED
    assert ANSWER not in caplog.text


def test_the_identity_log_never_carries_a_token(caplog):
    client = Client({"team_id": "T0OTHERTEAM", "user_id": BOT})
    with caplog.at_level(logging.DEBUG):
        code(lambda: runtime.verify_identity(client, connection()))
    assert "synthetic-bot-token" not in caplog.text
    assert "synthetic-app-token" not in caplog.text


# --- 3. 봉투가 어긋나면 아무것도 안 부른다 --------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"team_id": "T0OTHERTEAM"},
        {"team_id": ""},
        {"team_id": None},
        {"event": None},
        {"event": "텍스트"},
    ],
)
def test_a_foreign_or_malformed_envelope_closes_before_any_lookup(tmp_path, changes):
    payload = body()
    payload.update(changes)
    claims = Claims()
    engine = Engine()

    def forbidden(**_kwargs):
        raise AssertionError("권한 조회까지 갔다")

    result, client, engine = deliver(
        payload, engine=engine, claims=claims, tmp_path=tmp_path, authorize=forbidden
    )
    assert result.outcome == "ignored" and result.reason == "invalid_event"
    assert engine.questions == []
    assert claims.seen == []
    assert client.names() == []


def test_a_body_that_is_not_a_mapping_is_ignored(tmp_path):
    result, client, engine = deliver(["not", "a", "mapping"], tmp_path=tmp_path)
    assert result.outcome == "ignored"
    assert engine.questions == [] and client.names() == []


def test_a_duplicate_event_never_reaches_the_model(tmp_path):
    claims = Claims(allow=False)
    result, client, engine = deliver(body(), claims=claims, tmp_path=tmp_path)
    assert (result.outcome, result.reason) == ("ignored", "duplicate_event")
    assert claims.seen == ["Ev0000000001"]
    assert engine.questions == [] and client.names() == []


def test_the_envelope_event_id_is_what_gets_claimed(tmp_path):
    claims = Claims()
    deliver(body(event_id="Ev-unique-0009"), claims=claims, tmp_path=tmp_path)
    assert claims.seen == ["Ev-unique-0009"]


def test_a_denied_caller_never_reaches_the_model(tmp_path):
    result, client, engine = deliver(
        body(), tmp_path=tmp_path, authorize=lambda **_kwargs: _denied()
    )
    assert result.delivery is not None
    assert result.delivery.outcome == "permission_denied"
    assert engine.questions == []
    assert all(call[1] != CH for call in client.calls if call[0] == "chat_postMessage")


# --- 4. 모델 지정은 엔진에 닿지 않는다 -----------------------------------------


def test_a_user_model_flag_is_stripped_before_the_engine_sees_it(tmp_path):
    payload = body(
        event={
            "type": "app_mention",
            "channel_type": "channel",
            "user": USER,
            "channel": CH,
            "ts": "1.1",
            "text": f"<@{BOT}> {QUESTION} --model=claude-haiku-5-5",
        }
    )
    result, _client, engine = deliver(payload, tmp_path=tmp_path)
    assert result.outcome == "handled"
    assert engine.questions
    from tybot.answer import MODEL_FLAG_RE

    assert MODEL_FLAG_RE.findall(engine.questions[0]) == []
    assert "claude-haiku-5-5" not in engine.questions[0]


def test_the_allowed_path_delivers_the_rendered_answer_to_the_dm_only(tmp_path):
    result, client, _engine = deliver(body(), tmp_path=tmp_path)
    assert result.outcome == "handled" and result.delivery.outcome == "delivered"
    dm_texts = [call[2] for call in client.calls if call[0] == "chat_postMessage" and call[1] == DM]
    channel_texts = [
        call[2] for call in client.calls if call[0] == "chat_postMessage" and call[1] == CH
    ]
    assert any(ANSWER in text and "출처:" in text for text in dm_texts)
    assert all(ANSWER not in text for text in channel_texts)


def test_a_dm_event_is_answered_in_place(tmp_path):
    payload = body(
        event={
            "type": "message",
            "channel_type": "im",
            "user": USER,
            "channel": DM,
            "ts": "1.1",
            "text": QUESTION,
        }
    )
    result, client, _engine = deliver(payload, tmp_path=tmp_path)
    assert result.delivery.outcome == "delivered"
    assert "conversations_open" not in client.names()


# --- 5. 경계: import 와 Slack 지연 로딩 ----------------------------------------


def imported() -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            prefix = "tybot." if node.level else ""
            found.add(f"{prefix}{node.module}" if node.level else node.module)
    return found


def module_level_imports() -> set[str]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(("tybot." if node.level else "") + node.module)
    return found


def test_the_runtime_imports_nothing_outside_its_whitelist():
    extra = module_level_imports() - ALLOWED_IMPORTS
    assert not extra, f"런타임이 새 모듈을 들여왔다: {sorted(extra)}"


def test_the_runtime_imports_no_writer_collector_or_live_path():
    assert not imported() & FORBIDDEN_IMPORTS


def test_no_collection_or_files_api_is_even_named():
    body_text = SOURCE.read_text(encoding="utf-8")
    for name in (
        "files_upload",
        "files_info",
        "conversations_history",
        "conversations_join",
        "canvases_create",
        "live_fetch",
    ):
        assert name not in body_text


def test_slack_is_imported_lazily_so_the_unit_precheck_can_run():
    """`ExecStartPre=python -c "import tybot.vipbot_runtime"` 가 전제다."""
    assert not {"slack_bolt", "slack_sdk"} & module_level_imports()
    assert "slack_bolt" in imported()
    # 이미 적재된 모듈을 지우고 다시 들여와도 Slack 없이 성공해야 한다.
    sys.modules.pop("tybot.vipbot_runtime", None)
    import importlib

    importlib.import_module("tybot.vipbot_runtime")


def test_importing_the_runtime_starts_nothing():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    assert calls == []


def test_the_result_carries_no_question_or_answer(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        result, _client, _engine = deliver(body(), tmp_path=tmp_path)
    blob = repr(result) + "".join(
        str(getattr(result.delivery, item.name)) for item in fields(result.delivery)
    )
    assert QUESTION not in blob and ANSWER not in blob
    assert QUESTION not in caplog.text and ANSWER not in caplog.text


# --- 6. 아직 켜지 않는다 -------------------------------------------------------


def test_the_unit_still_cannot_be_enabled_even_though_the_module_exists():
    """모듈이 생겼지만 **활성화는 별도 결정**이다(B-83 진행 목록).

    `[Install]` 이 없으면 `systemctl enable` 이 거부된다. 켜는 날 이 시험을
    고치는 것이 그 결정을 남기는 방법이다.
    """
    directives = [
        line.strip()
        for raw in UNIT.read_text(encoding="utf-8").splitlines()
        if (line := raw.split("#", 1)[0]).strip()
    ]
    assert "[Install]" not in directives
    assert not any(item.startswith("WantedBy=") for item in directives)
    assert (
        'ExecStart=/opt/tybot/.venv/bin/python -m tybot.vipbot_runtime' in directives
    )


def test_the_unit_precheck_names_the_module_that_now_exists():
    assert SOURCE.is_file()
    assert (
        'ExecStartPre=/opt/tybot/.venv/bin/python -c "import tybot.vipbot_runtime"'
        in UNIT.read_text(encoding="utf-8")
    )
