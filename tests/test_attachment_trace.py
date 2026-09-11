"""첨부 단계 추적 — `converted` 는 답변 가능을 뜻하지 않는다.

설계: `docs/design/document-pipeline-trace-and-report-summary.md` §4·§8·§17

## 재현하는 사례
주간업무보고 HWP/HWPX 가 올라온 채널에서 「여태까지 수집된 주간 보고 회의 관련 내용을
종합해줘」 라고 물었더니 봇이 「회의록이나 논의가 없다」 고 답했다. 파일은 실제로 있었다.

이 화면만으로는 원인을 고를 수 없었다 — 수집 안 됨 / 변환 실패 / 원문 미반영 / 색인
지연 / 권한 / 랭킹 탈락이 모두 같은 답으로 보인다. 그래서 **최초 실패 단계 하나**를
가리키게 한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tybot import attachment_trace as at

# 실제 채널에 있던 파일들. 이름만 쓰고 본문은 쓰지 않는다.
REPORTS = (
    "[주간업무보고] 2026.09.10_방글라데시 차토그람 하수도.hwp",
    "공사팀 업무보고(2026.06월말 기준)_호남고철2-5.hwp",
    "[주간보고]광명자원회수시설 (26년09월2주차).hwpx",
    "공사팀 업무보고(2026.08월말 기준)_천안성환-평택.hwp",
)

WS = "tyit"
CH = "C0BQUGRHV2A"


def _stage(
    tmp_path: Path,
    *,
    name=REPORTS[0],
    file_id="F1",
    status="converted",
    extracted_lines=12,
    object_exists=True,
    sha="a" * 64,
    message_ts="1789094137.625899",
    extra=None,
) -> tuple[Path, dict]:
    """`stage_files()` 가 만드는 것과 같은 모양으로 만든다."""
    suffix = Path("workspaces") / WS / "channels" / CH / "attachments" / file_id
    staged = tmp_path / "staging" / suffix
    objects = tmp_path / "objects" / suffix
    staged.mkdir(parents=True, exist_ok=True)
    objects.mkdir(parents=True, exist_ok=True)

    obj = objects / name
    if object_exists:
        obj.write_bytes(b"%HWP fake")

    meta = {
        "schema_version": 1,
        "status": status,
        "slack_file_id": file_id,
        "name": name,
        "filetype": name.rsplit(".", 1)[-1],
        "mimetype": "",
        "permalink": f"https://slack.example/files/{file_id}",
        "declared_size": 240 * 1024,
        "sha256": sha,
        "object_path": str(obj),
        "extracted": extracted_lines > 0,
        "error": None,
        "staged_at": "2026-09-11T02:00:00+00:00",
    }
    if message_ts:
        meta["origin_message_ts"] = message_ts
    meta.update(extra or {})
    (staged / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if extracted_lines > 0:
        body = "\n".join(f"본문 {i}" for i in range(extracted_lines))
        (staged / "extracted.md").write_text(
            f"<!-- 로컬 변환본 -->\n# {name}\n\n{body}\n", encoding="utf-8"
        )
    return staged / "metadata.json", meta


def _archive_lines(*names, extracted=True, listed=True) -> list[str]:
    """원문 MD 의 첨부 줄. `archive/files.py` 와 같은 모양이어야 한다."""
    lines: list[str] = []
    for name in names:
        if listed:
            lines.append(f"[첨부:자동변환] {name} (hwp, 240KB) · <https://x|원본 파일>")
        if extracted:
            lines += [f"[첨부추출:{name}] 본문 {i}" for i in range(3)]
    return lines


def _trace(meta_path, meta, *, doc_lines=(), **kw) -> at.AttachmentTrace:
    return at.trace(
        meta, meta_path, workspace=WS, channel_id=CH, doc_lines=doc_lines, **kw
    )


def _status(got: at.AttachmentTrace, stage: str) -> str:
    return next(s.status for s in got.stages if s.stage == stage)


# --- 정상 경로 ----------------------------------------------------------------
def test_a_fully_processed_attachment_has_no_failure(tmp_path):
    meta_path, meta = _stage(tmp_path)
    got = _trace(meta_path, meta, doc_lines=_archive_lines(REPORTS[0]))
    assert _status(got, at.ARCHIVED) == at.OK
    # 색인·검색은 DB 와 RequestContext 가 있어야 한다. 안 주면 N/A 다.
    assert _status(got, at.INDEXED) == at.NOT_APPLICABLE
    assert got.first_failure is None or got.first_failure.stage == at.ARCHIVED


# --- 이 설계의 핵심: converted != 답변 가능 -----------------------------------
def test_converted_but_not_archived_is_a_failure(tmp_path):
    """metadata 는 converted, extracted.md 도 있는데 원문에 줄이 없다.

    콘솔에는 정상으로 보이고 답변은 「자료가 없다」 고 한다. 그 둘이 구별되지 않으면
    사람은 봇이 틀렸다고 결론 낸다.
    """
    meta_path, meta = _stage(tmp_path)
    got = _trace(meta_path, meta, doc_lines=[])
    assert _status(got, at.CONVERTED) == at.OK
    stuck = got.first_failure
    assert stuck and stuck.stage == at.ARCHIVED
    assert "archive-mark-missing" in stuck.detail


def test_listed_but_extraction_missing_says_so(tmp_path):
    """목록 표시는 있고 추출 줄만 없다 — 수집은 됐고 반영이 끊긴 것이다."""
    meta_path, meta = _stage(tmp_path)
    got = _trace(meta_path, meta, doc_lines=_archive_lines(REPORTS[0], extracted=False))
    stuck = got.first_failure
    assert stuck and stuck.stage == at.ARCHIVED
    assert "archive-lines-missing" in stuck.detail


def test_idempotent_write_still_counts_as_archived(tmp_path):
    """`writer.ingest().written == 0` 은 실패가 아니다. 줄이 있으면 성공이다."""
    meta_path, meta = _stage(tmp_path)
    got = _trace(meta_path, meta, doc_lines=_archive_lines(REPORTS[0]))
    assert _status(got, at.ARCHIVED) == at.OK


# --- 앞 단계가 막히면 뒤는 판정하지 않는다 -------------------------------------
def test_download_failure_stops_at_stored(tmp_path):
    meta_path, meta = _stage(
        tmp_path, status="download_or_extract_failed",
        extracted_lines=0, object_exists=False,
    )
    stuck = got_first = _trace(meta_path, meta).first_failure
    assert got_first and stuck.stage == at.STORED
    assert "object-missing" in stuck.detail


def test_convert_failure_does_not_report_archived_as_ok(tmp_path):
    """변환이 막혔는데 「색인 실패」 라고 하면 담당자는 재색인을 돌린다."""
    meta_path, meta = _stage(
        tmp_path, status="download_or_extract_failed", extracted_lines=0,
        extra={"error": "kordoc 변환 실패"},
    )
    got = _trace(meta_path, meta, doc_lines=_archive_lines(REPORTS[0]))
    stuck = got.first_failure
    assert stuck and stuck.stage == at.CONVERTED
    for later in (at.ARCHIVED, at.INDEXED, at.RETRIEVABLE):
        assert _status(got, later) == at.NOT_APPLICABLE


def test_empty_extraction_is_a_convert_failure(tmp_path):
    """머리글만 남은 변환본은 내용이 없는 것이다."""
    meta_path, meta = _stage(tmp_path, extracted_lines=0)
    (meta_path.parent / "extracted.md").write_text(
        "<!-- 로컬 변환본 -->\n# 제목\n", encoding="utf-8"
    )
    meta["extracted"] = True
    stuck = _trace(meta_path, meta).first_failure
    assert stuck and stuck.stage == at.CONVERTED
    assert "extracted-empty" in stuck.detail


def test_pii_refusal_is_marked_as_intended(tmp_path):
    """실패로만 보이면 되살리려 한다. 원칙 5 위반이 된다."""
    meta_path, meta = _stage(tmp_path, status="pii_refused", extracted_lines=0)
    got = _trace(meta_path, meta)
    stuck = got.first_failure
    assert stuck and stuck.stage == at.SCREENED
    assert "의도된 차단" in stuck.detail
    assert "되살리지 않는다" in got.action()


def test_unsupported_format_is_skipped_not_failed(tmp_path):
    meta_path, meta = _stage(tmp_path, name="그림.psd", status="unsupported",
                             extracted_lines=0)
    got = _trace(meta_path, meta)
    assert _status(got, at.CONVERTED) == at.SKIPPED
    # SKIP 은 뒤 단계를 막지 않는다 — 원문 목록 표시까지는 갈 수 있다.
    assert got.first_failure is None or got.first_failure.stage != at.CONVERTED


# --- 모르는 것은 모른다고 한다 -------------------------------------------------
def test_same_name_different_file_ids_are_not_merged(tmp_path):
    """원문 줄에는 파일명만 남는다. 둘이면 어느 것이 반영됐는지 알 수 없다."""
    meta_path, meta = _stage(tmp_path, file_id="F1")
    _stage(tmp_path, file_id="F2")
    got = _trace(meta_path, meta, doc_lines=_archive_lines(REPORTS[0]), same_name=2)
    stuck = got.first_failure
    assert stuck and stuck.stage == at.ARCHIVED
    assert stuck.status == at.UNKNOWN
    assert "ambiguous-name" in stuck.detail


def test_missing_sha256_is_unknown_not_ok(tmp_path):
    meta_path, meta = _stage(tmp_path, sha="")
    stuck = _trace(meta_path, meta).first_failure
    assert stuck and stuck.stage == at.STORED
    assert stuck.status == at.UNKNOWN


# --- 구형 metadata ------------------------------------------------------------
def test_old_metadata_without_message_ts_still_reads(tmp_path):
    """필드를 추가하기 전에 만들어진 첨부도 읽혀야 한다."""
    meta_path, meta = _stage(tmp_path, message_ts="")
    got = _trace(meta_path, meta, doc_lines=_archive_lines(REPORTS[0]))
    assert _status(got, at.OBSERVED) == at.OK
    assert _status(got, at.ARCHIVED) == at.OK


def test_metadata_without_a_file_id_fails_first_stage(tmp_path):
    meta_path, meta = _stage(tmp_path)
    meta["slack_file_id"] = ""
    stuck = _trace(meta_path, meta).first_failure
    assert stuck and stuck.stage == at.OBSERVED


# --- 원문 줄 파싱 -------------------------------------------------------------
@pytest.mark.parametrize("name", REPORTS)
def test_report_names_survive_the_mark_parsing(name):
    """대괄호·괄호·마이너스가 든 실제 파일명이다. 정규식이 이걸 놓치면 전부 실패로 보인다."""
    lines = _archive_lines(name)
    assert at.listed_in_archive(lines, name)
    assert at.extracted_line_count(lines, name) == 3


def test_other_files_lines_are_not_counted():
    lines = _archive_lines(REPORTS[0]) + _archive_lines(REPORTS[1])
    assert at.extracted_line_count(lines, REPORTS[0]) == 3
    assert at.extracted_line_count(lines, REPORTS[1]) == 3


def test_text_body_mark_counts_too():
    lines = ["[첨부본문:메모.txt] 한 줄"]
    assert at.extracted_line_count(lines, "메모.txt") == 1


def test_plain_conversation_lines_are_ignored():
    lines = ["> [2026-09-11 09:00] 홍길동: 주간보고 올렸습니다"]
    assert at.extracted_line_count(lines, REPORTS[0]) == 0
    assert not at.listed_in_archive(lines, REPORTS[0])


def test_line_hash_ignores_surrounding_space():
    assert at.line_hash(" a ") == at.line_hash("a")
    assert at.line_hash("a") != at.line_hash("b")


# --- 검수 폴더 훑기 -----------------------------------------------------------
def test_staged_attachments_finds_all_reports(tmp_path):
    for i, name in enumerate(REPORTS):
        _stage(tmp_path, name=name, file_id=f"F{i}")
    found = at.staged_attachments(tmp_path / "archive")
    assert len(found) == len(REPORTS)
    assert {m["name"] for _, m in found} == set(REPORTS)


def test_staged_attachments_filters_by_channel(tmp_path):
    _stage(tmp_path, file_id="F1")
    assert at.staged_attachments(tmp_path / "archive", channel_id="OTHER") == []
    assert at.staged_attachments(tmp_path / "archive", channel_id=CH)


def test_missing_staging_dir_is_not_an_error(tmp_path):
    assert at.staged_attachments(tmp_path / "없음" / "archive") == []


def test_staging_root_matches_the_writer_layout(tmp_path):
    """어긋나면 조용히 0건이 되고, 그건 「첨부가 없다」 와 구별되지 않는다."""
    from tybot.archive.files import attachment_storage

    archive = tmp_path / "archive"
    storage = attachment_storage(archive, WS, CH)
    root = at.staging_root(archive)
    assert str(storage.staging_dir).startswith(str(root))


# --- 여러 건 요약 -------------------------------------------------------------
def test_summary_counts_the_first_failure_of_each(tmp_path):
    """네 보고서가 서로 다른 단계에서 막혔을 때, 무엇부터 고칠지 보여야 한다."""
    traces = []
    # 1: 정상 · 2: 원문 미반영 · 3: 변환 실패 · 4: PII 차단
    m1 = _stage(tmp_path, name=REPORTS[0], file_id="F1")
    traces.append(_trace(*m1, doc_lines=_archive_lines(REPORTS[0])))
    m2 = _stage(tmp_path, name=REPORTS[1], file_id="F2")
    traces.append(_trace(*m2, doc_lines=[]))
    m3 = _stage(tmp_path, name=REPORTS[2], file_id="F3",
                status="download_or_extract_failed", extracted_lines=0)
    traces.append(_trace(*m3))
    m4 = _stage(tmp_path, name=REPORTS[3], file_id="F4",
                status="pii_refused", extracted_lines=0)
    traces.append(_trace(*m4))

    counts = at.summarize(traces)
    assert counts.get(at.ARCHIVED) == 1
    assert counts.get(at.CONVERTED) == 1
    assert counts.get(at.SCREENED) == 1
    text = at.summary_line(counts)
    assert "원문 반영" in text and "변환" in text


def test_summary_of_nothing_says_so():
    assert "첨부가 없습니다" in at.summary_line({})


# --- 보고서 문구 --------------------------------------------------------------
def test_report_names_the_first_failure_and_the_action(tmp_path):
    meta_path, meta = _stage(tmp_path)
    text = _trace(meta_path, meta, doc_lines=[]).report()
    assert "최초 실패" in text
    assert "convert_staged_attachments.py" in text  # 조치가 명령으로 적힌다


def test_report_does_not_leak_extracted_text(tmp_path):
    """파일명과 상태는 보여도 추출 본문은 보이지 않는다(설계 §8)."""
    meta_path, meta = _stage(tmp_path)
    text = _trace(meta_path, meta, doc_lines=_archive_lines(REPORTS[0])).report()
    assert "본문 0" not in text
    assert REPORTS[0] in text


def test_every_stage_has_a_label_and_an_action():
    """단계를 늘리고 문구를 안 쓰면 화면에 영문 코드가 그대로 나간다."""
    for stage in at.STAGES:
        assert at.STAGE_LABEL.get(stage)
        assert at.STAGE_ACTION.get(stage)
