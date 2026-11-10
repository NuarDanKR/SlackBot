"""Slack Lists 행 첨부 읽기 전용 실측 (B-90 1단계).

계약: `docs/verification/2026-11-10-slack-lists-attachments.md`

실측 도구의 시험이 필요한 이유는 하나다 — **이것은 운영 채널에서 돌아간다.**
「구조만 보려고」 켠 스크립트가 사람 이름을 터미널에 흘리면 그 터미널 기록이
곧 유출이고, 쓰기 메서드를 한 번 잘못 부르면 운영 List 가 바뀐다.

고정하는 것 넷.

1. **쓰기 메서드 이름이 코드에 없다.** `lists:write` 가 없어도 호출은 런타임에
   거절되고, 그때는 이미 요청이 나간 뒤다
2. **본문·파일명·사용자 정보를 출력하지 않는다.** ID 도 해시로만 남긴다
3. **한 쪽이라도 못 읽으면 그 List 를 미완으로 남긴다** — 반쪽 목록은
   「첨부 없음」 으로 읽힌다
4. **행 텍스트와 URL 에서 파일 좌표를 추측하지 않는다.** `attachment[]` 와
   `link[].file.file_id` 만 본다
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "probe_slack_lists.py"

_spec = importlib.util.spec_from_file_location("probe_slack_lists", SOURCE)
probe = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)

LIST_ID = "F0LIST0001"
CHANNEL = "C0ABCDEF123"
FILE_A = "F0FILEAAAA"
FILE_B = "F0FILEBBBB"

SECRETS = ("홍길동", "900101-1234567", "계약서_최종.xlsx", "U0SOMEUSER")


def _row(*fields) -> dict:
    return {"id": "Row0001", "fields": list(fields)}


def _page(items, cursor: str = "") -> dict:
    return {"items": items, "response_metadata": {"next_cursor": cursor}}


# --- 1. 쓰기에 닿지 않는다 -----------------------------------------------------


@pytest.mark.parametrize("method", probe.FORBIDDEN_METHODS)
def test_no_write_method_name_appears_in_the_source(method):
    """이름이 없으면 오타로도 부를 수 없다.

    `slack_sdk==3.43.0` 의 같은 `WebClient` 에 `slackLists_items_update` 가 함께
    있다. 권한이 없으면 Slack 이 거절하지만 그 거절은 **요청이 나간 뒤**에 온다.
    """
    body = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(body)

    # 금지 목록 **선언 자체**는 그 이름을 담는다. 문자열 위치로 잘라내면 위에
    # 코드를 한 줄 더할 때마다 깨지므로, AST 로 그 대입문만 찾아 비운다.
    lines = body.splitlines()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "FORBIDDEN_METHODS"
            for target in node.targets
        ):
            for index in range(node.lineno - 1, node.end_lineno):
                lines[index] = ""
            break
    else:  # pragma: no cover - 목록이 사라지면 이 시험이 무의미하다
        pytest.fail("FORBIDDEN_METHODS 선언을 찾지 못했다")

    code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert method not in code, f"쓰기 메서드 이름이 코드에 있다: {method}"


def test_the_forbidden_list_names_methods_the_sdk_actually_has():
    """오타 난 금지 목록은 아무것도 막지 않는다. 그런 목록은 있는 것이 더 나쁘다."""
    from slack_sdk import WebClient

    for method in probe.FORBIDDEN_METHODS:
        assert hasattr(WebClient, method), method


def test_the_sdk_has_the_read_method_so_no_string_api_call_is_needed():
    """계약 문서의 전제를 고친 자리(2026-11-10).

    고정된 SDK 에 편의 메서드가 **있다.** 업그레이드도, 문자열
    `api_call("slackLists.items.list")` 어댑터도 필요 없다 — 전자는 기존 Slack
    경로 전체의 회귀 위험을 지불하는 일이고, 후자는 오타가 런타임까지 가는 길이다.
    """
    from slack_sdk import WebClient

    assert hasattr(WebClient, "slackLists_items_list")
    assert "api_call(" not in SOURCE.read_text(encoding="utf-8")


def test_the_script_never_downloads_a_file():
    body = SOURCE.read_text(encoding="utf-8")

    for name in ("url_private", "requests.get", "httpx", "urlopen", "download"):
        assert name not in body, name


# --- 2. 파일 좌표는 명시된 칸에서만 ------------------------------------------


def test_an_attachment_array_is_read():
    got = probe.ListProbe(list_ref="x")
    got.row_fields(_row({"key": "col1", "attachment": [FILE_A, FILE_B]}))

    assert got.attachment_files == {FILE_A, FILE_B}
    assert got.link_files == set()


def test_a_link_field_file_id_is_read():
    got = probe.ListProbe(list_ref="x")
    got.row_fields(_row({"key": "col2", "link": [{"file": {"file_id": FILE_A}}]}))

    assert got.link_files == {FILE_A}
    assert got.attachment_files == set()


def test_row_text_and_urls_are_not_guessed_at():
    """행 텍스트와 일반 URL 에서 좌표를 뽑지 않는다.

    정규식으로 추측한 좌표는 틀렸을 때 조용하다 — 없는 파일을 출처로 붙이고,
    사람이 확인하러 가면 아무것도 없다(원칙 2).
    """
    got = probe.ListProbe(list_ref="x")
    got.row_fields(_row({
        "key": "note",
        "text": f"{FILE_A} 참조, https://files.slack.com/{FILE_B}",
        "rich_text": [{"text": FILE_A}],
    }))

    assert got.attachment_files == set()
    assert got.link_files == set()


def test_a_file_on_two_rows_is_counted_once_and_flagged():
    """메시지·파일 탭·Canvas·List 에 중복 노출된 파일은 revision 한 번만 쓴다.

    실측 단계에서는 그 중복이 **실제로 일어나는지**를 센다.
    """
    got = probe.ListProbe(list_ref="x")
    got.row_fields(_row({"key": "a", "attachment": [FILE_A]}))
    got.row_fields(_row({"key": "b", "attachment": [FILE_A]}))

    assert got.attachment_files == {FILE_A}
    assert got.duplicate_hits == 1


@pytest.mark.parametrize(
    "field_row",
    [
        {"key": "a", "attachment": [""]},
        {"key": "a", "attachment": [None]},
        {"key": "a", "link": [{"file": {}}]},
        {"key": "a", "link": ["not-a-dict"]},
        {"key": "a", "attachment": "F0NOTALIST"},
    ],
)
def test_malformed_fields_are_dropped_not_guessed(field_row):
    got = probe.ListProbe(list_ref="x")
    got.row_fields(_row(field_row))

    assert got.attachment_files == set()
    assert got.link_files == set()


# --- 3. 반쪽 목록은 「없음」 이 아니다 ----------------------------------------


def test_every_page_is_followed_to_the_end():
    client = Mock()
    client.slackLists_items_list.side_effect = [
        _page([_row({"key": "a", "attachment": [FILE_A]})], cursor="p2"),
        _page([_row({"key": "a", "attachment": [FILE_B]})]),
    ]

    got = probe.probe_list(client, LIST_ID)

    assert got.complete
    assert got.pages == 2
    assert got.rows == 2
    assert got.attachment_files == {FILE_A, FILE_B}


def test_a_failed_page_leaves_the_list_incomplete():
    """일부 행만 읽고 완료로 표시하면 그 뒤 첨부가 「없는 것」 이 된다."""
    client = Mock()
    client.slackLists_items_list.side_effect = [
        _page([_row({"key": "a", "attachment": [FILE_A]})], cursor="p2"),
        RuntimeError("ratelimited"),
    ]

    got = probe.probe_list(client, LIST_ID)

    assert not got.complete
    assert got.failure == "RuntimeError"
    assert got.report()["complete"] is False


def test_a_never_ending_cursor_is_a_failure_not_a_result():
    client = Mock()
    client.slackLists_items_list.return_value = _page([], cursor="always-more")

    got = probe.probe_list(client, LIST_ID)

    assert not got.complete
    assert got.failure == "too_many_pages"
    assert client.slackLists_items_list.call_count == probe.MAX_PAGES


def test_an_incomplete_list_is_not_asked_about_visibility():
    """미완인 List 는 가시성도 묻지 않는다.

    일부 파일만 확인하면 「확인했다」 는 기록이 반쪽 사실이 된다. 그 기록을
    보고 다음 단계가 「확인된 파일」 을 내려받는다.
    """
    client = Mock()
    client.files_list.return_value = {
        "files": [{"id": LIST_ID, "filetype": "list"}],
        "response_metadata": {"next_cursor": ""},
    }
    client.slackLists_items_list.side_effect = [
        _page([_row({"key": "a", "attachment": [FILE_A]})], cursor="p2"),
        RuntimeError("ratelimited"),
    ]

    out = probe.probe_channel(client, "tyit", CHANNEL)

    (row,) = out["lists"]
    assert row["complete"] is False
    assert row["visibility"] is None
    assert client.files_info.call_count == 0, "미완인데 가시성을 물었다"


def test_a_complete_list_does_get_its_visibility_checked():
    client = Mock()
    client.files_list.return_value = {
        "files": [{"id": LIST_ID, "filetype": "list"}],
        "response_metadata": {"next_cursor": ""},
    }
    client.slackLists_items_list.return_value = _page(
        [_row({"key": "a", "attachment": [FILE_A]})]
    )
    client.files_info.return_value = {"file": {"channels": [CHANNEL]}}

    out = probe.probe_channel(client, "tyit", CHANNEL)

    (row,) = out["lists"]
    assert row["complete"] is True
    assert row["visibility"] == {
        "same_channel": 1, "other_channel_only": 0, "unverified": 0
    }


def test_a_channel_probe_writes_nothing_and_calls_only_read_methods():
    """실측 한 번이 부르는 Slack 메서드 전부를 센다."""
    client = Mock()
    client.files_list.return_value = {
        "files": [{"id": LIST_ID, "filetype": "list"}],
        "response_metadata": {"next_cursor": ""},
    }
    client.slackLists_items_list.return_value = _page(
        [_row({"key": "a", "attachment": [FILE_A]})]
    )
    client.files_info.return_value = {"file": {"channels": [CHANNEL]}}

    probe.probe_channel(client, "tyit", CHANNEL)

    called = {
        name for name, _, _ in client.mock_calls if name and "." not in name
    }
    assert called <= {"files_list", "slackLists_items_list", "files_info"}, called


# --- 4. 가시성은 추측하지 않는다 ----------------------------------------------


def test_a_file_shared_in_the_same_channel_counts_as_visible():
    client = Mock()
    client.files_info.return_value = {"file": {"channels": [CHANNEL]}}

    got = probe.check_visibility(client, CHANNEL, {FILE_A})

    assert got == {"same_channel": 1, "other_channel_only": 0, "unverified": 0}


def test_a_file_only_in_another_channel_is_not_counted_as_visible():
    client = Mock()
    client.files_info.return_value = {"file": {"channels": ["C0OTHER"]}}

    got = probe.check_visibility(client, CHANNEL, {FILE_A})

    assert got["same_channel"] == 0
    assert got["other_channel_only"] == 1


def test_a_lookup_failure_is_unverified_not_absent():
    """가시성 미확인은 **거절**이지 통과가 아니다. 추측해서 통과시키지 않는다."""
    client = Mock()
    client.files_info.side_effect = RuntimeError("file_not_found")

    got = probe.check_visibility(client, CHANNEL, {FILE_A})

    assert got["unverified"] == 1
    assert got["same_channel"] == 0


def test_a_private_channel_group_membership_also_counts():
    client = Mock()
    client.files_info.return_value = {"file": {"groups": [CHANNEL]}}

    got = probe.check_visibility(client, CHANNEL, {FILE_A})

    assert got["same_channel"] == 1


# --- 5. 기록에 본문이 없다 -----------------------------------------------------


def test_the_report_holds_counts_and_hashes_only():
    got = probe.ListProbe(list_ref=probe.short(LIST_ID))
    got.row_fields(_row({
        "key": "계약자",
        "attachment": [FILE_A],
        "text": "홍길동 900101-1234567",
    }))
    got.complete = True

    blob = json.dumps(got.report(), ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in blob, secret
    assert LIST_ID not in blob
    assert FILE_A not in blob


def test_an_id_hashes_to_the_same_short_value_and_cannot_be_read_back():
    one = probe.short(FILE_A)
    two = probe.short(FILE_A)

    assert one == two
    assert one != probe.short(FILE_B)
    assert FILE_A not in one
    assert len(one) == 8


def test_a_column_name_a_person_wrote_is_hashed():
    """칸 이름은 설정에 따라 사람이 쓴 말이다(예: 「담당자」).

    이름이 그대로 남으면 List 의 구조에서 조직과 업무가 읽힌다.
    """
    got = probe.ListProbe(list_ref="x")
    got.row_fields(_row({"key": "담당자 주민등록번호"}))

    blob = json.dumps(dict(got.column_kinds), ensure_ascii=False)
    assert "담당자" not in blob
    assert "주민등록번호" not in blob


def test_nothing_in_the_script_prints_a_response_body():
    """출력은 `json.dumps(out)` 한 자리뿐이고, `out` 에는 건수와 해시만 들어간다."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    printed = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]

    assert printed, "출력이 아예 없으면 실측이 되지 않는다"
    for call in printed:
        rendered = ast.dump(call)
        for leak in ("items", "fields", "files", "text", "name"):
            assert f"attr='{leak}'" not in rendered


# --- 6. 범위 -------------------------------------------------------------------


def test_only_one_registered_channel_can_be_probed():
    """워크스페이스 전체 List 스캔은 선택지로 두지 않는다(계약 §범위 밖)."""
    body = SOURCE.read_text(encoding="utf-8")

    assert "--channel" in body
    assert "--all" not in body
    assert "files_list(" in body
    assert 'types="list"' in body


def test_a_channel_list_lookup_failure_is_not_reported_as_no_lists():
    client = Mock()
    client.files_list.side_effect = RuntimeError("channel_not_found")

    found, failure, seen = probe.find_lists(client, CHANNEL)

    assert found == []
    assert failure == "RuntimeError"
    assert sum(seen.values()) == 0


def test_only_list_filetypes_are_taken_from_the_channel():
    client = Mock()
    client.files_list.return_value = {
        "files": [
            {"id": LIST_ID, "filetype": "list"},
            {"id": "F0PDF00001", "filetype": "pdf"},
        ],
        "response_metadata": {"next_cursor": ""},
    }

    found, failure, seen = probe.find_lists(client, CHANNEL)

    assert failure == ""
    assert [item["id"] for item in found] == [LIST_ID]
    assert dict(seen) == {"list": 1, "pdf": 1}


def test_no_undocumented_types_filter_is_sent_to_slack():
    """`files.list` 의 `types` 는 `all·spaces·snippets·images·gdocs·zips·pdfs·canvas`
    만 받는다. **`list` 는 그 목록에 없다.**

    1차 실측에서 `types="list"` 를 보냈고 0건이 돌아왔다. 실패 칸은 비어 있어서
    보고서는 그것을 「List 가 없다」 로 적었다 — 이 도구가 막으려던 바로 그
    잘못이다. 거르는 일은 우리가 한다.
    """
    client = Mock()
    client.files_list.return_value = {
        "files": [], "response_metadata": {"next_cursor": ""}
    }

    probe.find_lists(client, CHANNEL)

    (call,) = client.files_list.call_args_list
    assert "types" not in call.kwargs, "문서에 없는 types 값을 보냈다"


def test_an_empty_channel_is_told_apart_from_a_channel_without_lists():
    """「0건」 두 가지를 가른다.

    파일이 아예 없는 채널과, 파일은 많은데 List 가 없는 채널은 다음에 할 일이
    다르다 — 전자는 채널을 잘못 고른 것일 수 있고, 후자는 실제 결론이다.
    """
    empty = Mock()
    empty.files_list.return_value = {
        "files": [], "response_metadata": {"next_cursor": ""}
    }
    busy = Mock()
    busy.files_list.return_value = {
        "files": [{"id": "F1", "filetype": "pdf"}, {"id": "F2", "filetype": "png"}],
        "response_metadata": {"next_cursor": ""},
    }

    out_empty = probe.probe_channel(empty, "tyit", CHANNEL)
    out_busy = probe.probe_channel(busy, "tyit", CHANNEL)

    assert out_empty["list_count"] == out_busy["list_count"] == 0
    assert out_empty["files_seen"] == 0
    assert out_busy["files_seen"] == 2
    assert out_busy["filetypes"] == {"pdf": 1, "png": 1}


def test_a_half_read_file_listing_is_a_failure_not_an_empty_result():
    client = Mock()
    client.files_list.return_value = {
        "files": [{"id": LIST_ID, "filetype": "list"}],
        "response_metadata": {"next_cursor": "always-more"},
    }

    found, failure, seen = probe.find_lists(client, CHANNEL)

    assert failure == "too_many_pages"
    assert found == []
    assert sum(seen.values()) == probe.MAX_PAGES

    out = probe.probe_channel(client, "tyit", CHANNEL)
    assert out["list_lookup_failure"] == "too_many_pages"
    assert out["list_count"] == 0
    assert out["lists"] == []


# --- 7. 토큰은 콘솔 DB 에서 온다 -----------------------------------------------


def test_the_token_comes_from_the_console_database_not_the_environment():
    """이 저장소에서 Slack 토큰의 정본은 **DB** 다(`bot_connection`).

    env 로 따로 읽으면 콘솔에서 토큰을 갈아 끼운 날 이 스크립트만 옛 토큰으로
    돌고, 그 결과를 「그 봇이 못 본다」 로 읽게 된다 — 실측이 틀린 결론을 낸다.
    """
    body = SOURCE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )

    assert "runtime_tokens" in code
    assert "SLACK_BOT_TOKEN" not in code, "환경변수에서 토큰을 읽는다"
    assert "os.getenv" not in code


def test_only_a_verified_enabled_connection_is_used(monkeypatch):
    """`runtime_tokens` 는 enabled + 신원 확인 통과만 돌려준다.

    꺼 둔 연결의 토큰으로 실측하면 운영에서 실제로 쓰이지 않을 권한을 본다.
    """
    called: list = []
    import tybot.console.bot_identity as identity

    monkeypatch.setattr(
        identity, "runtime_tokens",
        lambda ws, key: called.append((ws, key)) or ("xoxb-test", "xapp-test"),
    )

    assert probe.bot_token("tyit", "archiver") == "xoxb-test"
    assert called == [("tyit", "archiver")]


def test_the_default_bot_is_the_archiver():
    """Lists 수집은 Archiver 의 일이다(B-90 권한 계약).

    실측도 같은 토큰으로 해야 「이 봇이 볼 수 있나」 가 실제 답이 된다. TYBot
    토큰으로 재 보면 Archiver 에 없는 권한을 있는 것으로 읽는다.
    """
    assert probe.ARCHIVER_BOT == "archiver"
    assert 'default=ARCHIVER_BOT' in SOURCE.read_text(encoding="utf-8")


def test_a_token_failure_prints_a_reason_and_never_the_token():
    body = SOURCE.read_text(encoding="utf-8")

    # 예외를 그대로 찍으면 드라이버 메시지에 DSN 이 섞여 나올 수 있다.
    assert "type(exc).__name__" in body
    assert "print(f\"봇 토큰을 가져오지 못했습니다: {exc}\")" not in body
