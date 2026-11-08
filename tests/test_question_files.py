from __future__ import annotations

import pytest

from tybot import question_files
from tybot.archive.files import BoundedFileLines
from tybot.question_files import (
    MAX_FILE_BYTES,
    MAX_QUESTION_CHARS,
    MAX_QUESTION_FILES,
    MAX_TOTAL_BYTES,
    read_question_files,
    validate_question_files,
)


def _file(index: int = 1, **changes) -> dict:
    row = {
        "id": f"F0123456{index}",
        "name": f"report-{index}.pdf",
        "filetype": "pdf",
        "size": 1024,
        "url_private_download": f"https://files.slack.com/files-pri/T1-F{index}/report.pdf",
        "permalink": f"https://tyit.slack.com/files/U1/F0123456{index}",
    }
    row.update(changes)
    return row


class Loader:
    def __init__(self, *, lines=None, warnings=None, error=None) -> None:
        self.lines = ["[첨부추출:report.pdf] 진행률 현황"] if lines is None else lines
        self.warnings = [] if warnings is None else warnings
        self.error = error
        self.calls = []

    def __call__(self, files, token):
        self.calls.append((files, token))
        if self.error:
            raise self.error
        return self.lines, self.warnings


def test_valid_files_are_read_once_without_an_archive_writer():
    loader = Loader()

    got = read_question_files([_file()], "xoxb-fixture", loader=loader)

    assert got.files_read == 1
    assert got.lines == ("[첨부추출:report.pdf] 진행률 현황",)
    assert got.citations == ("https://tyit.slack.com/files/U1/F01234561",)
    assert got.codes == ()
    assert len(loader.calls) == 1


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"id": "bad"}, "file_identity_invalid"),
        ({"size": 0}, "file_size_exceeded"),
        ({"size": MAX_FILE_BYTES + 1}, "file_size_exceeded"),
        ({"size": "not-a-number"}, "file_size_invalid"),
        ({"url_private_download": "http://files.slack.com/x"}, "file_url_invalid"),
        ({"url_private_download": "https://example.com/x"}, "file_url_invalid"),
        ({"url_private_download": "https://user@files.slack.com/x"}, "file_url_invalid"),
        ({"url_private_download": "https://files.slack.com:443/x"}, "file_url_invalid"),
        ({"permalink": ""}, "file_permalink_invalid"),
        ({"filetype": "zip", "name": "archive.zip"}, "file_type_unsupported"),
    ],
)
def test_invalid_metadata_is_rejected_before_the_token_reaches_a_loader(changes, code):
    loader = Loader()

    got = read_question_files([_file(**changes)], "xoxb-fixture", loader=loader)

    assert got.codes == (code,)
    assert loader.calls == []


def test_file_count_and_total_size_are_bounded_before_download():
    loader = Loader()
    too_many = [_file(i) for i in range(MAX_QUESTION_FILES + 1)]

    assert read_question_files(too_many, "x", loader=loader).codes == ("file_count_exceeded",)
    assert loader.calls == []

    each = MAX_TOTAL_BYTES // 3 + 1
    got = read_question_files([_file(i, size=each) for i in range(3)], "x", loader=loader)
    assert got.codes == ("file_total_exceeded",)
    assert loader.calls == []


@pytest.mark.parametrize("files", [None, "files", b"files", ["not-a-map"]])
def test_non_file_collections_are_rejected(files):
    loader = Loader()

    got = read_question_files(files, "x", loader=loader)

    assert got.codes in {("files_invalid",), ("file_metadata_invalid",)}
    assert loader.calls == []


def test_missing_token_is_rejected_before_file_metadata_is_opened():
    loader = Loader()

    got = read_question_files([_file()], "", loader=loader)

    assert got.codes == ("file_token_missing",)
    assert loader.calls == []


def test_no_files_needs_no_token_or_loader():
    loader = Loader()

    got = read_question_files([], "", loader=loader)

    assert got == type(got)()
    assert loader.calls == []


def test_loader_errors_and_warnings_return_fixed_codes_without_exception_text():
    secret = "question body must not escape"
    failed = read_question_files([_file()], "x", loader=Loader(error=RuntimeError(secret)))
    partial = read_question_files([_file()], "x", loader=Loader(warnings=[secret]))

    assert failed.codes == ("file_read_failed",)
    assert secret not in repr(failed)
    assert partial.codes == ("file_read_partial",)
    assert secret not in repr(partial)


def test_empty_loader_output_is_not_presented_as_evidence():
    got = read_question_files([_file()], "x", loader=Loader(lines=[]))

    assert got.lines == ()
    assert got.codes == ("file_read_empty",)


def test_oversized_converted_text_is_rejected_instead_of_partially_answered():
    got = read_question_files(
        [_file()],
        "x",
        loader=Loader(lines=["x" * (MAX_QUESTION_CHARS + 1)]),
    )

    assert got.lines == ()
    assert got.files_read == 0
    assert got.codes == ("file_text_exceeded",)


@pytest.mark.parametrize(
    "permalink",
    [
        "http://tyit.slack.com/files/U/F",
        "https://example.com/files/U/F",
        "https://user@tyit.slack.com/files/U/F",
        "https://tyit.slack.com:443/files/U/F",
    ],
)
def test_invalid_permalink_blocks_download_because_the_answer_could_not_cite_it(permalink):
    loader = Loader()

    got = read_question_files([_file(permalink=permalink)], "x", loader=loader)

    assert got.files_read == 0
    assert got.codes == ("file_permalink_invalid",)
    assert loader.calls == []


def test_duplicate_safe_permalinks_are_returned_once():
    link = "https://tyit.slack.com/files/U1/F1"
    got = read_question_files(
        [_file(1, permalink=link), _file(2, permalink=link)],
        "x",
        loader=Loader(),
    )

    assert got.citations == (link,)


def test_duplicate_file_identity_is_rejected_before_download():
    loader = Loader()

    got = read_question_files([_file(1), _file(1)], "x", loader=loader)

    assert got.codes == ("file_identity_duplicate",)
    assert loader.calls == []


def test_production_reader_cites_only_files_whose_bodies_were_read(monkeypatch):
    def bounded(files, token, **limits):
        assert token == "x"
        assert limits == {
            "max_file_bytes": MAX_FILE_BYTES,
            "max_total_bytes": MAX_TOTAL_BYTES,
        }
        return BoundedFileLines(
            lines=("[첨부본문:report-2.pdf] 확인됨",),
            warnings=("F01234561",),
            file_ids=("F01234562",),
            bytes_read=12,
        )

    monkeypatch.setattr(question_files, "bounded_file_lines", bounded)

    got = read_question_files([_file(1), _file(2)], "x")

    assert got.files_read == 1
    assert got.codes == ("file_read_partial",)
    assert got.citations == ("https://tyit.slack.com/files/U1/F01234562",)


def test_actual_download_limit_failure_discards_all_evidence(monkeypatch):
    monkeypatch.setattr(
        question_files,
        "bounded_file_lines",
        lambda *args, **kwargs: BoundedFileLines(
            lines=("must not survive",),
            file_ids=("F01234561",),
            limit_code="file_size_exceeded",
        ),
    )

    got = read_question_files([_file()], "x")

    assert got.lines == ()
    assert got.citations == ()
    assert got.files_read == 0
    assert got.codes == ("file_size_exceeded",)


def test_preflight_validates_urls_without_downloading(monkeypatch):
    monkeypatch.setattr(
        question_files,
        "bounded_file_lines",
        lambda *args, **kwargs: pytest.fail("preflight downloaded file bytes"),
    )

    assert validate_question_files([_file()], "x") == ()
    assert validate_question_files(
        [_file(url_private_download="https://example.com/file")], "x"
    ) == ("file_url_invalid",)
