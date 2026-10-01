"""DM 첨부 **원본 바이트**를 Archiver 에 넘기는 금고 (B-68).

모듈: `tybot.archive.dm_file_handoff`

## 이 파일이 지키는 여섯

1. **Archiver 가 다시 받지 않는다.** 그 토큰으로 Master DM 을 열면 개인 대화의
   읽는 사람이 하나 더 생긴다. 바이트는 인계와 함께 오거나 오지 않는다
2. **좌표가 보존된다** — 파일 ID·메시지 ts·해시
3. **암호화한 뒤에 디스크에 쓴다.** 평문이 잠깐이라도 파일로 남지 않는다
4. **같은 것을 다시 넣어도 한 벌이다**
5. **다른 내용이면 거부한다.** 덮어쓰지 않는다 — Archiver 가 이미 읽고 인용했을
   수 있다
6. **평문이 로그·임시 파일에 안 남는다**
"""

from __future__ import annotations

import ast
import logging
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from tybot.archive.dm_file_handoff import (
    MAX_FILE_BYTES,
    DmFile,
    DmFileVault,
    digest_of,
)

WS = "tyit"
DM = "D0AB12345"
ME = "U0BR12345"
TS = "1759100000.000100"
FILE_ID = "F0FILE123"

#: 본문. 이 글자가 디스크나 로그에 그대로 보이면 안 된다.
SECRET = "착공계 초안 — 계약금 3억 2천만원".encode()


def _meta(data: bytes = SECRET, **over) -> DmFile:
    base = {
        "workspace": WS, "channel_id": DM, "user_id": ME, "message_ts": TS,
        "file_id": FILE_ID, "name": "착공계.pdf", "mimetype": "application/pdf",
        "size": len(data), "sha256": digest_of(data),
    }
    base.update(over)
    return DmFile(**base)


@pytest.fixture
def vault(tmp_path) -> DmFileVault:
    return DmFileVault(tmp_path / "vault", Fernet(Fernet.generate_key()))


# --- 1. Archiver 가 다시 받지 않는다 -----------------------------------------------

def test_the_module_never_reaches_slack():
    """**이 시험이 이 모듈의 이유다.**

    Archiver 토큰으로 Master DM 파일을 다시 받으면, 그 토큰이 개인 대화를 여는
    두 번째 열쇠가 된다. 바이트는 인계와 함께 오거나 오지 않는다.
    """
    source = (Path(__file__).resolve().parent.parent / "src" / "tybot" / "archive"
              / "dm_file_handoff.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    for forbidden in ("slack_sdk", "slack_bolt", "requests", "httpx",
                      "urllib", "http", "socket"):
        assert forbidden not in imported, forbidden

    # 이름만 본다. 산문에서 「re-downloads 하지 않는다」 라고 쓴 것까지 잡으면
    # 설명을 지우게 되고, 그건 이 시험이 지키려던 것과 반대다.
    used = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    for forbidden in ("files_info", "WebClient", "bot_token", "urlopen",
                      "get", "post", "download"):
        assert forbidden not in used, forbidden


# --- 2. 좌표와 해시가 보존된다 -----------------------------------------------------

def test_the_coordinates_survive_the_round_trip(vault):
    key = vault.put(_meta(), SECRET)

    meta, data = vault.open(WS, key)

    assert (meta.file_id, meta.message_ts, meta.channel_id) == (FILE_ID, TS, DM)
    assert meta.sha256 == digest_of(SECRET)
    assert data == SECRET


def test_the_name_and_type_survive(vault):
    """출처 표기가 파일 이름을 쓴다. 잃으면 사람이 어떤 파일인지 못 찾는다."""
    meta, _ = vault.open(WS, vault.put(_meta(), SECRET))

    assert (meta.name, meta.mimetype) == ("착공계.pdf", "application/pdf")


def test_the_key_changes_when_the_content_changes(vault):
    """같은 파일 ID 로 다시 올린 파일은 **다른 내용**이다. 재시도가 아니다."""
    other = "바뀐 내용".encode()

    assert _meta().key != _meta(other).key


def test_the_key_is_stable_for_the_same_entry():
    assert _meta().key == _meta().key


@pytest.mark.parametrize("field,value", [
    ("workspace", "TYIT"),
    ("channel_id", "C0CHAN123"),   # 채널은 DM 이 아니다
    ("channel_id", "G0GROUP12"),   # 다자 DM
    ("user_id", "B0BOT1234"),
    ("message_ts", "어제"),
    ("file_id", "C0FILE123"),
    ("sha256", "짧은해시"),
    ("name", "../남의자리.pdf"),
    ("name", ""),
    ("mimetype", "x" * 256),
])
def test_a_broken_coordinate_is_refused(field, value):
    with pytest.raises(ValueError):
        _meta(**{field: value}).validate()


def test_a_size_that_is_not_an_integer_is_refused():
    """`True` 는 `int` 의 하위형이다. 크기로 받으면 1 바이트가 된다."""
    for bad in (True, "12", 1.5, -1):
        with pytest.raises(ValueError, match="file size"):
            _meta(size=bad).validate()


# --- 3. 암호화한 뒤에 디스크에 쓴다 ------------------------------------------------

def test_nothing_on_disk_holds_the_plaintext(vault, tmp_path):
    vault.put(_meta(), SECRET)

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_bytes(), path


def test_the_metadata_is_not_readable_on_disk(vault, tmp_path):
    """이름만 평문으로 남아도 「누가 무엇을 올렸나」 가 샌다."""
    vault.put(_meta(), SECRET)

    for path in tmp_path.rglob("*.bin"):
        blob = path.read_bytes()
        assert "착공계.pdf".encode() not in blob
        assert FILE_ID.encode() not in blob
        assert ME.encode() not in blob


def test_a_tampered_entry_is_refused(vault, tmp_path):
    """봉인이 메타와 바이트를 함께 묶는다. 한쪽만 바꿔치기할 수 없다."""
    key = vault.put(_meta(), SECRET)
    entry = next(tmp_path.rglob("*.bin"))
    entry.write_bytes(entry.read_bytes()[:-5] + b"AAAAA")

    with pytest.raises(ValueError, match="cannot be read"):
        vault.open(WS, key)


def test_another_key_cannot_open_it(tmp_path):
    first = DmFileVault(tmp_path / "vault", Fernet(Fernet.generate_key()))
    key = first.put(_meta(), SECRET)
    stranger = DmFileVault(tmp_path / "vault", Fernet(Fernet.generate_key()))

    with pytest.raises(ValueError, match="cannot be read"):
        stranger.open(WS, key)


def test_the_vault_refuses_a_symlinked_path(tmp_path):
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    try:
        (root / WS).symlink_to(tmp_path / "밖", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다")

    with pytest.raises(ValueError, match="symlink"):
        DmFileVault(root, Fernet(Fernet.generate_key())).put(_meta(), SECRET)


# --- 4. 중복 재시도 ----------------------------------------------------------------

def test_the_same_entry_twice_stores_one(vault):
    """인계는 ACK 전에 죽을 수 있다. 재전달이 두 벌이 되면 근거가 두 번 세어진다."""
    first = vault.put(_meta(), SECRET)
    second = vault.put(_meta(), SECRET)

    assert first == second
    assert vault.keys(WS) == [first]


def test_a_retry_after_a_crash_between_write_and_ack(vault, tmp_path):
    """임시 파일이 남아 있어도 다음 시도가 막히지 않아야 한다."""
    directory = tmp_path / "vault" / WS / "files"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".dmfile-남은것").write_bytes("쓰다 만 것".encode())

    key = vault.put(_meta(), SECRET)

    assert vault.keys(WS) == [key]


def test_acknowledging_removes_only_that_entry(vault):
    mine = vault.put(_meta(), SECRET)
    other = vault.put(_meta(b"\xff\xfe", file_id="F0OTHER12"), b"\xff\xfe")

    vault.acknowledge(WS, mine)

    assert vault.keys(WS) == [other]


def test_acknowledging_twice_is_an_error(vault):
    key = vault.put(_meta(), SECRET)
    vault.acknowledge(WS, key)

    with pytest.raises(ValueError, match="cannot be read"):
        vault.acknowledge(WS, key)


# --- 5. 불일치 거부 ----------------------------------------------------------------

def test_a_digest_that_does_not_match_the_bytes_is_refused(vault):
    """생산자가 제대로 못 읽은 파일이다. 받으면 출처 사슬에 검증 불가한 바이트가 든다."""
    with pytest.raises(ValueError, match="does not match its digest"):
        vault.put(_meta(sha256=digest_of("다른 내용".encode())), SECRET)


def test_a_size_that_does_not_match_the_bytes_is_refused(vault):
    with pytest.raises(ValueError, match="size does not match"):
        vault.put(_meta(size=len(SECRET) + 1), SECRET)


def test_the_same_identity_with_other_bytes_is_refused(vault, tmp_path):
    """덮어쓰면 Archiver 가 이미 읽고 인용한 파일이 바뀐다."""
    key = vault.put(_meta(), SECRET)
    # 같은 키를 주장하도록 메타의 해시만 유지한 채 다른 바이트를 민다.
    forged = replace(_meta(), size=5)

    with pytest.raises(ValueError):
        vault.put(forged, "가짜".encode())
    assert vault.open(WS, key)[1] == SECRET


def test_a_corrupted_entry_blocks_the_retry_instead_of_overwriting(vault, tmp_path):
    key = vault.put(_meta(), SECRET)
    next(tmp_path.rglob("*.bin")).write_bytes("깨진 것".encode())

    with pytest.raises(ValueError, match="cannot be verified"):
        vault.put(_meta(), SECRET)
    assert vault.keys(WS) == [key]


def test_an_entry_filed_under_another_workspace_is_refused(vault, tmp_path):
    """워크스페이스를 떼면 남의 회사 자료가 이 금고에서 열린다(절대 원칙 4)."""
    key = vault.put(_meta(), SECRET)
    other = tmp_path / "vault" / "mgmt" / "files"
    other.mkdir(parents=True)
    (other / f"{key}.bin").write_bytes(
        next((tmp_path / "vault" / WS / "files").glob("*.bin")).read_bytes()
    )

    with pytest.raises(ValueError, match="identity mismatch"):
        vault.open("mgmt", key)


def test_a_file_over_the_limit_is_refused(vault):
    oversized = _meta(size=MAX_FILE_BYTES + 1)

    with pytest.raises(ValueError, match="file size"):
        oversized.validate()


def test_an_oversized_payload_is_refused_before_sealing(vault, monkeypatch, tmp_path):
    """봉인은 평문 크기만큼 메모리를 쓴다. 한도 없이 받으면 저장 문제가 메모리 문제가 된다.

    한도 판정이 `validate()` 와 `put()` 두 곳에 있다. 둘 중 어느 쪽이 먼저 걸리든
    **쓰기 전에** 막혀야 한다 — 이 시험이 보는 것은 그것이다.
    """
    monkeypatch.setattr("tybot.archive.dm_file_handoff.MAX_FILE_BYTES", 8)
    data = b"123456789"

    with pytest.raises(ValueError, match=r"size|too large"):
        vault.put(_meta(data, size=9, sha256=digest_of(data)), data)
    assert list(tmp_path.rglob("*.bin")) == []


# --- 6. 평문이 로그·임시 파일에 안 남는다 ------------------------------------------

def test_no_temporary_file_survives(vault, tmp_path):
    vault.put(_meta(), SECRET)

    assert [p.name for p in tmp_path.rglob(".dmfile-*")] == []


def test_a_failed_put_leaves_no_temporary_file(vault, tmp_path):
    with pytest.raises(ValueError):
        vault.put(_meta(sha256=digest_of("틀린 해시".encode())), SECRET)

    assert list(tmp_path.rglob(".dmfile-*")) == []
    assert list(tmp_path.rglob("*.bin")) == []


def test_nothing_sensitive_reaches_the_log(vault, caplog):
    with caplog.at_level(logging.DEBUG):
        key = vault.put(_meta(), SECRET)
        vault.open(WS, key)
        with pytest.raises(ValueError):
            vault.put(_meta(sha256=digest_of("틀림".encode())), SECRET)

    blob = caplog.text
    assert SECRET.decode() not in blob
    assert "착공계.pdf" not in blob
    assert FILE_ID not in blob


def test_the_module_never_logs_a_value(vault):
    """로그 호출 자체가 없어야 한다. 서식 문자열 하나가 본문을 흘린다."""
    source = (Path(__file__).resolve().parent.parent / "src" / "tybot" / "archive"
              / "dm_file_handoff.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "log"
    ]

    assert calls == []


def test_an_exception_message_carries_no_content(vault):
    """예외 문구도 로그로 간다. 거기 본문이 들어가면 같은 사고다."""
    for broken, data in (
        (_meta(sha256=digest_of("틀림".encode())), SECRET),
        (_meta(size=1), SECRET),
    ):
        with pytest.raises(ValueError) as caught:
            vault.put(broken, data)

        assert SECRET.decode() not in str(caught.value)
        assert "착공계.pdf" not in str(caught.value)


# --- 7. 봉인은 멀쩡한데 내용이 어긋난 항목 -----------------------------------------
#
# 암호문이 유효해도 안에 든 메타와 바이트가 서로 안 맞을 수 있다. 같은 키를 가진
# 사람이 만들면 가능하다. 그래서 봉인 검사 **뒤에도** 해시를 다시 본다.

def _plant(vault: DmFileVault, root: Path, meta: DmFile, data: bytes, *, key: str) -> None:
    """정상 봉인이지만 내용이 어긋난 항목을 심는다."""
    directory = root / WS / "files"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{key}.bin").write_bytes(vault._seal(meta, data))


def test_a_retry_refuses_when_the_stored_bytes_differ(vault, tmp_path):
    """메타는 같고 바이트만 다른 항목. 봉인 검사만으로는 통과한다."""
    real = _meta()
    _plant(vault, tmp_path / "vault", real, b"\x00" * len(SECRET), key=real.key)

    with pytest.raises(ValueError, match="conflicting content"):
        vault.put(real, SECRET)


def test_open_rejects_bytes_that_do_not_match_the_stored_digest(vault, tmp_path):
    """복호화 뒤에도 해시를 다시 본다. 안 보면 검증 안 된 바이트가 근거로 나간다."""
    real = _meta()
    _plant(vault, tmp_path / "vault", real, b"\x00" * len(SECRET), key=real.key)

    with pytest.raises(ValueError, match="does not match its digest"):
        vault.open(WS, real.key)


def test_open_rejects_a_size_that_disagrees_with_the_bytes(vault, tmp_path):
    real = _meta()
    short = SECRET[:-1]
    _plant(vault, tmp_path / "vault", replace(real, size=len(short)), short, key=real.key)

    with pytest.raises(ValueError, match=r"identity mismatch|does not match"):
        vault.open(WS, real.key)
