"""HWP 변환을 변환 앱(rhwp)에 넘기는 경계 (2026-11-09 오너 결정).

원칙 1 의 예외라서 **좁게** 받는지를 건다 — 지정된 앱 ID 만, PDF 파일만, 글은
버리고. 그리고 앱이 지정되지 않으면 아무것도 바뀌지 않는지를 건다.
"""
from __future__ import annotations

import pytest

from tybot import attachment_review, question_files
from tybot.archive import backfill, converter_bots, files
from tybot.archive import convert as convert_module
from tybot.console import env_settings

APP = "A0CONVERTER1"
OTHER = "A0SOMEOTHER9"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """운영 설정 파일과 캐시가 시험 사이로 새지 않게 막는다."""
    monkeypatch.setenv("ENV_SETTINGS_PATH", str(tmp_path / "managed.env"))
    monkeypatch.setattr(converter_bots, "PRODUCTION_MANAGED_ENV", tmp_path / "absent.env")
    monkeypatch.delenv(converter_bots.CONVERTER_APPS_ENV, raising=False)
    converter_bots.clear_cache()
    yield
    converter_bots.clear_cache()


def _enable(monkeypatch, value: str = APP) -> None:
    monkeypatch.setenv(converter_bots.CONVERTER_APPS_ENV, value)
    converter_bots.clear_cache()


def _app_message(**over) -> dict:
    message = {
        "ts": "1790000000.000100",
        "user": "U0RHWPBOT",
        "bot_id": "B0RHWP",
        "app_id": APP,
        "subtype": "bot_message",
        "text": "변환했습니다 — 이 안내문은 봇 출력이다",
        "files": [
            {"id": "F1", "name": "주간보고.pdf", "filetype": "pdf"},
            {"id": "F2", "name": "주간보고.hwp", "filetype": "hwp"},
        ],
    }
    message.update(over)
    return message


# --- 앱 ID 목록 --------------------------------------------------------------


def test_nothing_is_delegated_until_an_app_is_named():
    assert converter_bots.converter_app_ids() == frozenset()
    assert converter_bots.conversion_delegated() is False
    assert converter_bots.converted_document_message(_app_message()) is None


def test_malformed_ids_are_dropped_not_widened(monkeypatch):
    # 틀린 값이 「아무 앱이나」 로 넓어지면 원칙 1 의 예외가 열린다.
    _enable(monkeypatch, f" {APP.lower()} , bot-name, *, ")
    assert converter_bots.converter_app_ids() == frozenset({APP})


def test_console_file_wins_over_the_process_environment(monkeypatch, tmp_path):
    """수집기는 콘솔 저장 뒤 재시작되지 않는다. 파일을 직접 읽어야 바뀐 값을 안다."""
    (tmp_path / "managed.env").write_text(
        f"{converter_bots.CONVERTER_APPS_ENV}={OTHER}\n", encoding="utf-8"
    )
    _enable(monkeypatch, APP)
    assert converter_bots.converter_app_ids() == frozenset({OTHER})


def test_the_value_is_cached_and_cleared_on_save(monkeypatch):
    _enable(monkeypatch, APP)
    assert converter_bots.converter_app_ids(now=0.0) == frozenset({APP})
    monkeypatch.setenv(converter_bots.CONVERTER_APPS_ENV, OTHER)
    assert converter_bots.converter_app_ids(now=1.0) == frozenset({APP})
    converter_bots.clear_cache()
    assert converter_bots.converter_app_ids(now=2.0) == frozenset({OTHER})


def test_console_input_is_validated_as_a_whole():
    assert converter_bots.parse_app_ids(f"{APP}, {APP.lower()}, {OTHER}") == [APP, OTHER]
    with pytest.raises(ValueError):
        converter_bots.parse_app_ids(f"{APP}, rhwp")


# --- 받는 메시지 -------------------------------------------------------------


def test_only_the_pdf_survives_and_the_bot_text_is_dropped(monkeypatch):
    _enable(monkeypatch)
    kept = converter_bots.converted_document_message(_app_message())
    assert kept is not None
    assert kept["text"] == ""
    assert [item["name"] for item in kept["files"]] == ["주간보고.pdf"]
    assert "bot_id" not in kept and "app_id" not in kept
    # 사람이 올린 파일로 확인됐다고 적히지 않게 하는 표식.
    assert kept[converter_bots.CONVERTER_MARK] == APP


def test_app_id_from_bot_profile_is_recognised(monkeypatch):
    _enable(monkeypatch)
    message = _app_message(app_id=None, bot_profile={"app_id": APP})
    assert converter_bots.converted_document_message(message) is not None


@pytest.mark.parametrize(
    "over",
    [
        {"app_id": OTHER},  # 지정되지 않은 앱
        {"files": [{"id": "F2", "name": "주간보고.hwp", "filetype": "hwp"}]},  # PDF 없음
        {"subtype": "message_changed"},  # 수정 이벤트는 revision 경로다
        {"ts": ""},
        {"user": "", "bot_id": ""},
    ],
)
def test_everything_else_from_bots_is_still_refused(monkeypatch, over):
    _enable(monkeypatch)
    assert converter_bots.converted_document_message(_app_message(**over)) is None


def test_backfill_keeps_humans_and_strips_the_converter_message(monkeypatch):
    _enable(monkeypatch)
    human = {"ts": "1790000000.000200", "user": "U0PERSON", "text": "확인 부탁드립니다"}
    assert backfill._collectable(human) is human
    assert backfill._collectable(_app_message(app_id=OTHER)) is None
    kept = backfill._collectable(_app_message())
    assert kept is not None and kept["text"] == ""


# --- TYBot 은 HWP 를 직접 변환하지 않는다 -------------------------------------


def test_hwp_conversion_is_switched_off_only_while_an_app_is_named(monkeypatch):
    assert convert_module.can_convert("hwp") is True
    assert convert_module.can_convert("hwpx") is True
    _enable(monkeypatch)
    assert convert_module.can_convert("hwp") is False
    assert convert_module.can_convert("HWPX") is False
    assert convert_module.can_convert("pdf") is True


def test_staged_hwp_is_kept_as_an_original_and_marked_delegated(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(files, "download_bytes", lambda *a: b"HWP original bytes")

    def must_not_convert(*args):
        raise AssertionError("delegated HWP must not reach the converter")

    monkeypatch.setattr(files, "convert", must_not_convert)
    storage = files.attachment_storage(tmp_path / "archive", "tyit", "C0TEST")
    (item,) = files.stage_attachments(
        [{"id": "F9", "name": "주간보고.hwp", "filetype": "binary", "size": 18}],
        "test-token", storage,
    )
    assert item.extracted is False
    assert any(line.startswith("[첨부:변환앱위임]") for line in item.lines)

    (attachment,) = attachment_review.scan(tmp_path / "archive")
    assert attachment.delegated is True
    assert attachment.conversion_failed is False
    assert attachment_review.status_label(attachment) == attachment_review.DELEGATED_LABEL
    assert attachment.object_path is not None and attachment.object_path.is_file()


def test_a_download_failure_before_conversion_is_still_reported(tmp_path, monkeypatch):
    """위임 코드는 다운로드 뒤에 정해진다. 그 전에 실패하면 평소 실패로 남아야 한다."""
    _enable(monkeypatch)
    storage = files.attachment_storage(tmp_path / "archive", "tyit", "C0TEST")
    (item,) = files.stage_attachments(
        [{"id": "F9", "name": "주간보고.hwp", "filetype": "hwp", "size": 18}],
        None, storage,  # 토큰 없음 — 다운로드 실패
    )
    assert any(line.startswith("[첨부:처리실패]") for line in item.lines)


def test_question_hwp_asks_for_the_converted_pdf(monkeypatch):
    _enable(monkeypatch)

    def loader(*args):
        raise AssertionError("delegated HWP must not be downloaded for a question")

    got = question_files.read_question_files(
        [{
            "id": "F01234561",
            "name": "주간보고.hwp",
            "filetype": "hwp",
            "size": 1024,
            "url_private_download": "https://files.slack.com/files-pri/T1-F1/a.hwp",
            "permalink": "https://tyit.slack.com/files/U1/F01234561",
        }],
        "xoxb-fixture",
        loader=loader,
    )
    assert got.codes == (question_files.HWP_USE_PDF_CODE,)
    assert "PDF" in question_files.refusal_message(question_files.HWP_USE_PDF_CODE)


# --- 콘솔 설정 ---------------------------------------------------------------


def test_console_validates_and_preserves_the_app_list(monkeypatch):
    base = {"realtimeIngest": True, "autojoinChannels": True, "replyInThread": True}
    monkeypatch.setenv(converter_bots.CONVERTER_APPS_ENV, APP)
    # 예전 화면은 칸을 보내지 않는다 — 그때 값을 지우면 안 된다.
    assert env_settings._validate(base)[converter_bots.CONVERTER_APPS_ENV] == APP
    assert env_settings._validate({**base, "converterAppIds": []})[
        converter_bots.CONVERTER_APPS_ENV
    ] == ""
    assert env_settings._validate({**base, "converterAppIds": [OTHER.lower(), APP]})[
        converter_bots.CONVERTER_APPS_ENV
    ] == f"{OTHER},{APP}"
    with pytest.raises(ValueError):
        env_settings._validate({**base, "converterAppIds": ["rhwp"]})
