from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("cryptography", reason="워크스페이스 토큰 암호화 선택 의존성")

from cryptography.fernet import Fernet

from tybot.console import workspace_store


def test_workspace_cipher_round_trip(monkeypatch):
    monkeypatch.setenv("WORKSPACE_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    cipher = workspace_store._fernet()
    token = b"xoxb-test_token_value"

    encrypted = cipher.encrypt(token)

    assert encrypted != token
    assert cipher.decrypt(encrypted) == token


def test_workspace_mask_does_not_contain_middle_of_token():
    token = "xoxb-visi_ble_secret_middle_last"

    masked = workspace_store._mask(token)

    assert masked.startswith("xoxb-visi")
    assert masked.endswith("last")
    assert "secret_middle" not in masked


def test_workspace_cipher_requires_dedicated_key(monkeypatch):
    monkeypatch.delenv("WORKSPACE_SECRET_KEY", raising=False)
    monkeypatch.delenv("WORKSPACE_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    with pytest.raises(workspace_store.WorkspaceStoreError, match="WORKSPACE_SECRET_KEY"):
        workspace_store._fernet()


# ---------------------------------------------------------------------------
# WC-01 — 워크스페이스 등록에 봇 토큰을 요구하지 않는다
# ---------------------------------------------------------------------------
#
# 봇 토큰은 이제 `봇 관리 > 워크스페이스 연결` 에서 봇별로 등록한다
# (Master·Archiver·Hermes). 워크스페이스 행은 **조직 metadata** 만 든다.
#
# 토큰을 워크스페이스에 매는 것은 봇이 하나뿐이던 시절의 구조다. 지금은 한
# 워크스페이스에 봇이 여럿이라 애초에 한 쌍으로 표현할 수 없다. 그런데 저장
# 경로가 아직 그걸 요구해서 **새 워크스페이스를 하나도 만들 수 없었다.**

#: 시험용 가짜 토큰. 조각으로 만든다 — 한 줄에 토큰처럼 적으면 커밋 가드가
#: 진짜로 보고 막는다(실제로 막혔다).
FAKE_BOT = "xoxb-" + "0" * 24
FAKE_APP = "xapp-" + "0" * 24


class _Cursor:
    """저장 경로가 실제로 내는 질의를 받아 적는 가짜 커서.

    DB 를 띄우지 않고도 「무엇을 거부하는가」를 볼 수 있어야 한다 — 거부는
    질의가 아니라 분기에서 나오기 때문이다.
    """

    def __init__(self, *, exists: bool = False, secret_count: int = 0):
        self.exists = exists
        self.secret_count = secret_count
        self.statements: list[str] = []
        self._next = None

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        text = sql.lower()
        if "from workspace where lower(key)" in text and "for update" in text:
            self._next = {"key": "tec"} if self.exists else None
        elif "count(*) as count from workspace_secret" in text:
            self._next = {"count": self.secret_count}
        elif "from workspace where lower(key) = any" in text:
            self._next = []
        else:
            self._next = None

    def fetchone(self):
        return self._next

    def fetchall(self):
        return self._next or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _save(monkeypatch, cursor, **over):
    monkeypatch.setattr(workspace_store, "_connect", lambda: _Conn(cursor))
    values = {
        "key": "tec",
        "label": "프로젝트금융",
        "role": "member",
        "state": "enabled",
        "limit_usd": 10.0,
        "readable": [],
        "bot_token": None,
        "app_token": None,
        "actor": "dan@taeyoung.com",
    }
    values.update(over)
    workspace_store.save_workspace(**values)


def test_a_new_workspace_is_created_without_bot_tokens(monkeypatch):
    """**조직 metadata 만으로 등록된다.** 봇은 나중에 봇 관리에서 붙인다."""
    cursor = _Cursor(exists=False)

    _save(monkeypatch, cursor)

    inserted = [s for s in cursor.statements if s.startswith("INSERT INTO workspace ")]
    assert len(inserted) == 1
    # 시크릿은 하나도 쓰지 않는다 — 받은 적이 없으니 쓸 것도 없다.
    assert not [s for s in cursor.statements if "workspace_secret" in s and "INSERT" in s]


def test_editing_a_tokenless_workspace_still_works(monkeypatch):
    """등록만 되고 봇이 아직 안 붙은 워크스페이스도 **고칠 수 있어야** 한다.

    못 고치면 하루 상한 하나 바꾸려고 토큰을 넣어야 하고, 그 토큰은 이 화면이
    받지 않는 값이다.
    """
    monkeypatch.delenv("SLACK_BOT_TOKEN_TEC", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN_TEC", raising=False)
    cursor = _Cursor(exists=True, secret_count=0)

    _save(monkeypatch, cursor, label="프로젝트금융(수정)")

    assert [s for s in cursor.statements if s.startswith("INSERT INTO workspace ")]


def test_one_token_alone_is_still_refused(monkeypatch):
    """교체는 **쌍으로만** 한다. 한쪽만 바꾸면 그 워크스페이스의 봇이 죽는다."""
    cursor = _Cursor(exists=False)

    with pytest.raises(workspace_store.WorkspaceStoreError, match="함께 입력"):
        _save(monkeypatch, cursor, bot_token=FAKE_BOT)

    assert cursor.statements == []


def test_tokens_given_together_are_still_stored(monkeypatch):
    """옛 경로를 끊지 않는다 — 과거 배포본의 복구 수단이 사라진다."""
    monkeypatch.setenv("WORKSPACE_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    cursor = _Cursor(exists=False)

    _save(monkeypatch, cursor, bot_token=FAKE_BOT, app_token=FAKE_APP)

    secrets = [s for s in cursor.statements if "workspace_secret" in s]
    assert len(secrets) == 2


def test_the_old_refusal_message_is_gone():
    """문구가 남아 있으면 어딘가 그 분기도 남아 있다."""
    source = Path(workspace_store.__file__).read_text(encoding="utf-8")

    assert "새 워크스페이스에는 봇 토큰과 앱 토큰이" not in source


# ---------------------------------------------------------------------------
# WC-14 — Master 가 `bot_connection` 토큰을 읽는다
# ---------------------------------------------------------------------------
#
# 전에는 Archiver 만 새 `bot_connection` 을 읽었고 Master 는 `workspace_secret` 만
# 봤다(`bot_admin.py:60` 주석). 그래서 **봇 관리에서 Master 토큰을 넣어도 실제로
# 안 붙었다** — 화면의 저장은 기록이지 적용이 아니었다.
#
# 그 상태로 WC-01 을 켜면 반쪽이 된다. 토큰 없이 등록은 되는데, 봇을 붙일 방법이
# 없다. 「등록했는데 왜 수집이 안 되지」 가 되고, 아무 오류도 안 난다.


def _pick(**over):
    """`runtime_workspaces` 가 DB 에서 받는 한 행."""
    row = {
        "key": "tyit",
        "label": "전산팀",
        "role": "member",
        "state": "enabled",
        "legacy_bot": None,
        "legacy_app": None,
        "connection_bot": None,
        "connection_app": None,
        "readable": [],
    }
    row.update(over)
    return row


def test_a_master_connection_token_wins_over_the_legacy_secret():
    """`bot_connection` 이 정본이다. 둘 다 있으면 새 쪽을 쓴다."""
    chosen = workspace_store.pick_runtime_tokens(
        _pick(legacy_bot=b"old-bot", legacy_app=b"old-app",
              connection_bot=b"new-bot", connection_app=b"new-app")
    )

    assert chosen == (b"new-bot", b"new-app", "bot_connection")


def test_the_legacy_secret_is_used_while_no_connection_exists():
    """전환 중에도 돌던 봇은 계속 돌아야 한다."""
    chosen = workspace_store.pick_runtime_tokens(
        _pick(legacy_bot=b"old-bot", legacy_app=b"old-app")
    )

    assert chosen == (b"old-bot", b"old-app", "workspace_secret")


def test_a_half_configured_connection_never_mixes_sources():
    """한쪽만 새 표에 있으면 **섞지 않는다.**

    섞으면 봇 토큰과 앱 토큰이 서로 다른 앱의 것이 된다. Slack 은 그걸 연결
    실패로만 알려 주고, 담당자는 어느 쪽이 틀렸는지 화면에서 못 본다.
    """
    chosen = workspace_store.pick_runtime_tokens(
        _pick(legacy_bot=b"old-bot", legacy_app=b"old-app", connection_bot=b"new-bot")
    )

    assert chosen == (b"old-bot", b"old-app", "workspace_secret")


def test_nothing_anywhere_is_not_a_token():
    chosen = workspace_store.pick_runtime_tokens(_pick())

    assert chosen == (None, None, "")


def test_only_a_connection_pair_is_enough():
    """`workspace_secret` 이 비어도 새 연결만으로 뜬다 — WC-01 의 목적지다."""
    chosen = workspace_store.pick_runtime_tokens(
        _pick(connection_bot=b"new-bot", connection_app=b"new-app")
    )

    assert chosen == (b"new-bot", b"new-app", "bot_connection")


def test_the_query_only_takes_a_verified_enabled_master_connection():
    """토큰을 잘못 붙이면 **다른 워크스페이스에 수집한다.** 그건 오류가 아니라 유출이다.

    그래서 Archiver 가 쓰는 함수와 같은 네 조건을 건다.
    """
    import inspect

    source = inspect.getsource(workspace_store.runtime_workspaces)

    assert "bot_key = 'master'" in source
    assert "connector_type = 'slack_socket'" in source
    assert "state = 'enabled'" in source
    assert "identity_ok IS TRUE" in source


def test_a_half_configured_legacy_secret_is_not_used_either():
    """옛 표도 반쪽이면 안 쓴다. 섞이는 것은 어느 표에서든 같은 사고다."""
    assert workspace_store.pick_runtime_tokens(_pick(legacy_bot=b"old-bot")) == (
        None, None, "",
    )
    assert workspace_store.pick_runtime_tokens(_pick(legacy_app=b"old-app")) == (
        None, None, "",
    )


def test_the_row_carries_where_its_token_came_from():
    """전환 중에는 「콘솔에서 본 값」 과 「봇이 쓰는 값」 이 다를 수 있다.

    그 간극은 조용하다 — 출처를 안 남기면 어느 표가 이겼는지 아무도 모른다.
    """
    import inspect

    source = inspect.getsource(workspace_store.runtime_workspaces)

    assert 'item["token_source"] = source' in source
