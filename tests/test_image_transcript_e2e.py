"""B-81 후속: 설명·OCR 전사 분리와 사진 검색 E2E.

모델 응답(가짜 router) → 코어 파싱 → 저장 가드 → 파생 revision·검색 투영 →
사진 길잡이 → 현재 ACL 로 원문 재개봉까지 한 줄로 잇는다. DB·네트워크·API 키 없이
돈다. 원장 DB 는 순서를 기록하는 가짜 손잡이, 아카이브는 임시 디렉터리의 실제 파일이다.

거는 것:
1. 전사는 설명과 **다른 칸·다른 revision·다른 라벨**이다. 설명에 섞이면 설명을 보류한다
2. 전사는 마스킹 뒤 저장되고, 문서 자체가 민감물이면 저장하지 않는다
3. 검색이 돌려주는 것은 **원문 줄**이다. 설명·전사 문장은 근거로 나가지 않는다
4. 직전 재검증 — 권한 없는 사람·원본이 사라진/바뀐 사진·PII 가 남은 투영은 빠진다
5. 모델 실패·timeout·무료 한도 소진이어도 로컬 OCR 검색과 원본 수집은 그대로다
6. provenance — provider/model/prompt 판·원본 좌표·불확실성이 두 revision 에 다 남는다
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot import image_guide
from tybot.access import RequestContext
from tybot.archive import attachment_doc, writer
from tybot.archive import image_description as idesc
from tybot.archive import image_transcript as itx
from tybot.archive.files import attachment_storage
from tybot.archive.image_description_output_guard import (
    CODE_TRANSCRIPT_LEAK,
    VERDICT_ALLOW,
    VERDICT_WITHHOLD,
    guard_index_write,
)
from tybot.archive.image_description_store import ImageDescriptionStore, dispatch_identity
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import ModelSpec, ProviderTimeout, Sensitivity

WS, CH, CHANNEL = "tyit", "C0SITE0001", "#현장_김해외동(180182)_채팅방"
FID = "F0IMG00001"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-site-photo" * 8
SHA = hashlib.sha256(PNG).hexdigest()
MODEL, PROVIDER = "gemini-free-vision", "google"
LOCAL_OCR = "안전제일 출입금지 구역"

DESCRIPTION = "건설현장 실외에 타워크레인과 철골 구조물이 보인다."
TRANSCRIPT_LINES = ["안전제일 출입금지 구역", "현장소장 연락처 010-1234-5678"]


# --- 가짜 모델 ---------------------------------------------------------------


class _Resp:
    def __init__(self, text, *, cost_usd=0.0, stop_reason="end_turn"):
        self.text = text
        self.model = MODEL
        self.provider = PROVIDER
        self.stop_reason = stop_reason
        self.input_tokens = 900
        self.output_tokens = 140
        self.cost_usd = cost_usd


class _Router:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def resolve(self, model, sensitivity):
        return ModelSpec(model, PROVIDER, 0.0, 0.0, Sensitivity.CONFIDENTIAL)

    def restricted_to(self, model, *, sensitivity):
        return self

    def complete(self, messages, **kw):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _model_json(**over) -> str:
    body = {
        "describable": True,
        "description": DESCRIPTION,
        "tags": ["건설현장", "실외", "크레인", "철골", "안내문"],
        "uncertainty": [],
        "transcript": {"present": True, "lines": TRANSCRIPT_LINES, "uncertainty": []},
    }
    body.update(over)
    return json.dumps(body, ensure_ascii=False)


def _image() -> idesc.ImageInput:
    return idesc.ImageInput(
        workspace=WS, channel=CH, file_id=FID, original_sha256=SHA, image=PNG, mime="image/png"
    )


def _describe(response) -> idesc.ImageDescription:
    return idesc.describe_image(
        _image(),
        router=_Router(response),
        model=MODEL,
        provider=PROVIDER,
        max_output_tokens=1024,
        timeout_seconds=20,
    )


# --- 1. 코어: 설명과 전사는 다른 칸 ------------------------------------------


def test_prompt_revision_is_bumped_and_the_prompt_separates_the_two_fields():
    assert idesc.PROMPT_REVISION != "2026-10-27.1"
    assert '"transcript"' in idesc.SYSTEM_PROMPT
    assert "description` 에는 사진 속 글자를 **한 구절도 옮기지 않습니다" in idesc.SYSTEM_PROMPT
    assert "#T" not in idesc.SYSTEM_PROMPT


def test_one_call_returns_a_description_and_a_separate_transcript():
    router = _Router(_Resp(_model_json()))
    got = idesc.describe_image(
        _image(),
        router=router,
        model=MODEL,
        provider=PROVIDER,
        max_output_tokens=1024,
        timeout_seconds=20,
    )
    assert router.calls == 1
    assert got.status == idesc.STATUS_DESCRIBED
    assert got.description == DESCRIPTION
    assert got.transcript.status == itx.STATUS_TRANSCRIBED
    assert got.transcript.lines == tuple(TRANSCRIPT_LINES)
    assert "출입금지" not in got.description


def test_a_broken_transcript_does_not_take_the_description_down():
    got = _describe(_Resp(_model_json(transcript={"present": True, "lines": "글자"})))
    assert got.status == idesc.STATUS_DESCRIBED
    assert got.transcript.status == itx.STATUS_FAILED
    assert got.transcript.error_code == itx.ERROR_INVALID


def test_an_old_response_without_a_transcript_is_absent_not_failed():
    raw = json.loads(_model_json())
    raw.pop("transcript")
    got = _describe(_Resp(json.dumps(raw, ensure_ascii=False)))
    assert got.transcript.status == itx.STATUS_ABSENT
    assert not got.transcript.present


@pytest.mark.parametrize(
    ("value", "status", "code"),
    [
        (
            {"present": True, "lines": ["가" * (itx.MAX_LINE_CHARS + 1)]},
            itx.STATUS_FAILED,
            itx.ERROR_TOO_LONG,
        ),
        (
            {"present": True, "lines": ["한 줄"] * (itx.MAX_LINES + 1)},
            itx.STATUS_FAILED,
            itx.ERROR_TOO_LONG,
        ),
        ({"present": True, "lines": ["  ", ""]}, itx.STATUS_FAILED, itx.ERROR_EMPTY),
        ({"present": True, "lines": [1, 2]}, itx.STATUS_FAILED, itx.ERROR_INVALID),
        ({"present": "yes", "lines": []}, itx.STATUS_FAILED, itx.ERROR_INVALID),
        ({"present": False, "lines": ["무시"]}, itx.STATUS_ABSENT, ""),
        (
            {"present": True, "lines": ["흐린 글자"], "uncertainty": ["partial_text", "made_up"]},
            itx.STATUS_UNCERTAIN,
            "",
        ),
    ],
)
def test_transcript_parsing_never_truncates_and_keeps_only_known_codes(value, status, code):
    got = itx.parse_transcript(value)
    assert (got.status, got.error_code) == (status, code)
    assert "made_up" not in got.uncertainty


# --- 2. 저장 가드 -------------------------------------------------------------


def test_transcript_is_masked_before_storage():
    decision = itx.guard_transcript(
        itx.parse_transcript({"present": True, "lines": TRANSCRIPT_LINES})
    )
    assert decision.storable
    assert "010-1234-5678" not in decision.body
    assert decision.masked_count == 1
    assert itx.CODE_MASKED in decision.codes


def test_a_direct_identifier_is_masked_not_stored_in_clear():
    decision = itx.guard_transcript(
        itx.parse_transcript({"present": True, "lines": ["주민등록번호 900101-1234567"]})
    )
    assert decision.storable
    assert "900101-1234567" not in decision.body
    assert itx.CODE_MASKED in decision.codes


REGISTRY = [
    "등기사항전부증명서", "갑구 소유권에 관한 사항", "을구 소유권 이외의 권리에 관한 사항",
    "부동산등기법", "등기원인 매매", "고유번호", "접수 제12345호", "등기목적 소유권이전",
]


def test_a_sensitive_document_transcript_is_not_stored():
    """등기부처럼 **문서 자체가 민감물**이면 마스킹으로 끝내지 않고 저장하지 않는다."""
    decision = itx.guard_transcript(itx.parse_transcript({"present": True, "lines": REGISTRY}))
    assert not decision.storable
    assert decision.codes == (itx.CODE_PII_BLOCKED,)
    assert decision.body == ""


def test_a_description_that_quotes_the_transcript_is_withheld():
    result = _describe(
        _Resp(
            _model_json(
                description="안내문에 안전제일 출입금지 구역 이라고 적혀 있다.",
            )
        )
    )
    decision = guard_index_write(result)
    assert decision.verdict == VERDICT_WITHHOLD
    assert decision.description == ""
    assert CODE_TRANSCRIPT_LEAK in decision.codes
    assert decision.tags  # 태그는 남는다


def test_short_common_overlaps_are_not_a_leak():
    transcript = itx.parse_transcript({"present": True, "lines": ["안전 제일"]})
    assert not itx.leaks_into("안전 표지가 있는 건설현장", transcript)
    assert guard_index_write(_describe(_Resp(_model_json()))).verdict == VERDICT_ALLOW


# --- 3. 원장: 두 revision + 검색 투영 ----------------------------------------

SCOPE = {
    "workspace": WS,
    "channel": CH,
    "file_id": FID,
    "original_sha256": SHA,
    "policy_revision": "global:local",
    "prompt_revision": idesc.PROMPT_REVISION,
    "model": MODEL,
    "provider": PROVIDER,
    "image_sha256": SHA,
}
JOB = {
    "id": "job-1",
    "identity_sha256": dispatch_identity(SCOPE),
    "approval_id": "approval-1",
    "workspace": WS,
    "policy_revision": "global:local",
    "prompt_revision": idesc.PROMPT_REVISION,
    "scope": SCOPE,
    "catalog_snapshot": {"model": MODEL, "provider": PROVIDER},
    "max_output_tokens": 1024,
    "state": "dispatching",
    "expires_at": datetime(2026, 11, 12, tzinfo=UTC) + timedelta(minutes=10),
}
GLOBAL = {"workspace": "", "revision": "global", "enabled": True, "config": {}}


class _Ledger:
    """원장 손잡이 대역. 실행한 SQL 과 값만 기록한다."""

    autocommit = True
    info = SimpleNamespace(transaction_status=0)

    def __init__(self):
        self.replies = deque([GLOBAL, JOB])
        self.events: list = []

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield
        except Exception:
            self.events.append("rollback")
            raise
        self.events.append("commit")

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, args=None):
        self.events.append((" ".join(sql.split()), args))

    def fetchone(self):
        return self.replies.popleft()

    def inserts(self, table: str) -> list[tuple]:
        return [
            args
            for event in self.events
            if isinstance(event, tuple) and event[0].startswith(f"INSERT INTO {table} ")
            for args in [event[1]]
        ]


def _publish(result) -> tuple[str, _Ledger]:
    ledger = _Ledger()
    revision_id = ImageDescriptionStore(ledger).finish_and_publish("job-1", result)
    return revision_id, ledger


def test_publish_writes_two_revisions_with_full_provenance():
    revision_id, ledger = _publish(_describe(_Resp(_model_json())))
    assert revision_id and ledger.events[-1] == "commit"
    (description,) = ledger.inserts("archiver_image_description_revision")
    (transcript,) = ledger.inserts("archiver_image_transcript_revision")
    # 설명 revision 에는 전사 글자가 없다.
    assert "출입금지" not in description[14]
    # 전사 revision: 같은 job·설명 revision 에 묶이고 provenance 를 그대로 문다.
    assert transcript[2] == "job-1" and transcript[3] == revision_id
    assert transcript[4:8] == (WS, CH, FID, SHA)
    assert transcript[9:12] == (idesc.PROMPT_REVISION, MODEL, PROVIDER)
    assert transcript[12] == itx.STATUS_TRANSCRIBED
    assert "010-1234-5678" not in transcript[13]
    assert transcript[1] != description[1]  # identity 가 다르다


def test_the_search_projection_has_one_row_per_kind_with_its_own_label():
    _, ledger = _publish(_describe(_Resp(_model_json())))
    rows = ledger.inserts("image_search_entry")
    kinds = {row[1]: row for row in rows}
    assert set(kinds) == {"description", "transcript"}
    assert kinds["description"][11] == "[AI 이미지 설명·검색용]"
    assert kinds["transcript"][11] == itx.LABEL
    assert "출입금지" not in kinds["description"][12]
    assert "출입금지" in kinds["transcript"][12]
    assert "010-1234-5678" not in kinds["transcript"][12]
    for row in rows:
        assert row[3:7] == (WS, CH, FID, SHA)
        assert row[7:10] == (PROVIDER, MODEL, idesc.PROMPT_REVISION)


def test_uncertainty_is_kept_on_both_revisions():
    result = _describe(
        _Resp(
            _model_json(
                uncertainty=["blurred"],
                transcript={
                    "present": True,
                    "lines": ["흐린 표지"],
                    "uncertainty": ["partial_text"],
                },
            )
        )
    )
    _, ledger = _publish(result)
    (description,) = ledger.inserts("archiver_image_description_revision")
    (transcript,) = ledger.inserts("archiver_image_transcript_revision")
    assert description[16] == ["blurred"]
    assert transcript[12] == itx.STATUS_UNCERTAIN and transcript[16] == ["partial_text"]


def test_a_blocked_description_takes_its_transcript_with_it():
    """규칙을 어긴 응답(설명에 숫자)의 전사는 믿을 근거가 없다."""
    _, ledger = _publish(_describe(_Resp(_model_json(description="크레인 2대가 보인다."))))
    assert ledger.inserts("archiver_image_transcript_revision") == []
    assert ledger.inserts("image_search_entry") == []


def test_an_unverified_cost_publishes_nothing():
    _, ledger = _publish(_describe(_Resp(_model_json(), cost_usd=None)))
    assert ledger.inserts("archiver_image_description_revision") == []
    assert ledger.inserts("archiver_image_transcript_revision") == []


def test_a_pii_blocked_transcript_keeps_the_description():
    _, ledger = _publish(_describe(_Resp(_model_json(
        transcript={"present": True, "lines": REGISTRY},
    ))))
    assert len(ledger.inserts("archiver_image_description_revision")) == 1
    assert ledger.inserts("archiver_image_transcript_revision") == []
    assert {row[1] for row in ledger.inserts("image_search_entry")} == {"description"}


def test_a_withheld_description_projects_tags_only():
    result = _describe(
        _Resp(
            _model_json(
                uncertainty=["person_visible"],
                transcript={"present": False, "lines": []},
            )
        )
    )
    _, ledger = _publish(result)
    (row,) = ledger.inserts("image_search_entry")
    assert row[1] == "description"
    assert DESCRIPTION not in row[12]
    assert "크레인" in row[12]


# --- 4. 검색 → 현재 ACL 로 원문 재개봉 ---------------------------------------


def _archive(root: Path, *, human: bool = True, sha: str = SHA, with_object: bool = True) -> None:
    """채널 원문(첨부 줄) + 첨부 정본(로컬 OCR 본문) + 원본 바이트."""
    writer.ingest(
        root,
        workspace=WS,
        channel=CHANNEL,
        channel_id=CH,
        acl=[CHANNEL],
        messages=[
            writer.IncomingMessage(
                datetime(2026, 11, 12, 1, 0, tzinfo=UTC),
                "김현장",
                f"[첨부:자동변환] 현장사진.png (png, 1KB) · id:{FID}",
                source_ts="1794000000.000100",
            )
        ],
    )
    doc = attachment_doc.AttachmentDoc(
        workspace=WS,
        channel_id=CH,
        channel=CHANNEL,
        file_id=FID,
        name="현장사진.png",
        revision="rev0000000001",
        visibility="private",
        acl=frozenset({CHANNEL}),
        text=LOCAL_OCR,
        conversion_state=attachment_doc.CONVERTED,
        message_ts="1794000000.000100",
        filetype="png",
        sha256=sha,
        staged_at="2026-11-12T01:00:00+00:00",
        human_upload_confirmed=human,
    )
    path = root / doc.relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(attachment_doc.render(doc), encoding="utf-8")
    if with_object:
        objects = attachment_storage(root, WS, CH).objects_dir / FID
        objects.mkdir(parents=True, exist_ok=True)
        (objects / "현장사진.png").write_bytes(PNG)


class _EntryConn:
    """`image_search_entry` 조회 대역. 낱말 LIKE 와 workspace 범위를 흉내 낸다."""

    def __init__(self, rows):
        self.rows = rows
        self.sql: list = []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params):
                conn.sql.append((sql, params))
                words = [
                    value[1:-1].replace("\\%", "%").replace("\\_", "_").replace("\\\\", "\\")
                    for key, value in sorted(params.items())
                    if key.startswith("kw")
                ]
                self.rows = [
                    row
                    for row in conn.rows
                    if row["workspace"] in params["workspaces"]
                    and any(w in row["body"].replace(" ", "").lower() for w in words)
                ]

            def fetchall(self):
                return self.rows

        return _Cur()


def _entries_from(ledger: _Ledger) -> list[dict]:
    keys = (
        "id",
        "kind",
        "revision_id",
        "workspace",
        "channel_id",
        "file_id",
        "original_sha256",
        "provider",
        "model",
        "prompt_revision",
        "status",
        "label",
        "body",
        "uncertainty",
    )
    return [dict(zip(keys, row, strict=True)) for row in ledger.inserts("image_search_entry")]


def _ctx(channels=(CHANNEL,)):
    return RequestContext(workspace=WS, channels=frozenset(channels))


def _end_to_end(tmp_path, **archive_kw):
    root = tmp_path / "archive"
    _archive(root, **archive_kw)
    _, ledger = _publish(_describe(_Resp(_model_json())))
    return ArchiveStore(root), _EntryConn(_entries_from(ledger))


def test_end_to_end_a_photo_found_by_its_ai_description_reopens_original_lines(tmp_path):
    """「타워크레인」 은 로컬 OCR 에 없다. AI 설명으로 찾고, 돌려주는 것은 원문 줄이다."""
    store, conn = _end_to_end(tmp_path)
    assert not store.search("타워크레인", _ctx(), limit=20)  # 로컬 OCR 만으로는 못 찾는다

    hits = image_guide.expand(store, _ctx(), "타워크레인 사진", conn=conn)

    texts = [hit.line.text for hit in hits]
    assert any(LOCAL_OCR in text for text in texts)  # 첨부 정본의 로컬 OCR 줄
    assert any(f"id:{FID}" in text for text in texts)  # 채널의 첨부 줄(사진이 올라온 자리)
    joined = "\n".join(texts)
    assert DESCRIPTION not in joined and "[AI " not in joined  # 설명 문장은 근거가 아니다


def test_end_to_end_the_ai_transcript_finds_text_and_the_answer_cites_the_original(tmp_path):
    store, conn = _end_to_end(tmp_path)
    hits = image_guide.expand(store, _ctx(), "현장소장 연락처", conn=conn)
    assert hits
    assert all("010-1234-5678" not in hit.line.text for hit in hits)
    assert all("[AI " not in hit.line.text for hit in hits)
    assert all(hit.citation() for hit in hits)


def test_a_requester_without_channel_access_gets_nothing(tmp_path):
    store, conn = _end_to_end(tmp_path)
    assert image_guide.expand(store, _ctx(channels=("#다른채널",)), "타워크레인", conn=conn) == []


def test_another_workspace_entry_is_never_queried(tmp_path):
    store, conn = _end_to_end(tmp_path)
    image_guide.expand(
        store, RequestContext(workspace="mgmt", channels=frozenset()), "타워크레인", conn=conn
    )
    for _sql, params in conn.sql:
        assert WS not in params["workspaces"]


@pytest.mark.parametrize(
    "archive_kw",
    [
        {"with_object": False},  # 원본이 사라졌다
        {"human": False},  # 사람 업로드가 확인되지 않는다
        {"sha": "c" * 64},  # 정본의 해시가 투영과 다르다(재수집으로 바뀜)
    ],
)
def test_a_photo_whose_original_cannot_be_reverified_is_dropped(tmp_path, archive_kw):
    store, conn = _end_to_end(tmp_path, **archive_kw)
    assert image_guide.expand(store, _ctx(), "타워크레인", conn=conn) == []


def test_a_changed_original_byte_is_dropped(tmp_path):
    store, conn = _end_to_end(tmp_path)
    objects = attachment_storage(store.root, WS, CH).objects_dir / FID
    (objects / "현장사진.png").write_bytes(PNG + b"tampered")
    assert image_guide.expand(store, _ctx(), "타워크레인", conn=conn) == []


def test_a_projection_that_still_carries_pii_is_dropped(tmp_path):
    store, conn = _end_to_end(tmp_path)
    for row in conn.rows:
        row["body"] += " 주민등록번호 900101-1234567 타워크레인"
    assert image_guide.expand(store, _ctx(), "타워크레인", conn=conn) == []


def test_no_database_means_no_guide_and_no_document_reads(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    class _Store:
        def visible_docs(self, ctx):
            raise AssertionError("DB 가 없으면 원문을 읽기 전에 끝난다")

    assert image_guide.expand(_Store(), _ctx(), "타워크레인") == []


def test_the_answer_pipeline_adds_guide_hits_after_search(tmp_path, monkeypatch):
    from tybot import answer as answer_module

    store, conn = _end_to_end(tmp_path)
    monkeypatch.setattr(image_guide, "_connect", lambda: conn)
    engine = SimpleNamespace(_store=store)
    merged = answer_module.AnswerEngine._with_image_guide(engine, "타워크레인", [], _ctx())
    assert any(LOCAL_OCR in hit.line.text for hit in merged)


# --- 5. 모델 실패·무료 한도 소진 → 로컬 OCR 로 복귀 -------------------------


@pytest.mark.parametrize(
    "response",
    [
        ProviderTimeout("timeout"),
        RuntimeError("quota exhausted 429"),
        _Resp("not json"),
        _Resp(_model_json(), stop_reason="max_tokens"),
    ],
)
def test_model_failure_publishes_nothing_and_local_ocr_still_answers(tmp_path, response):
    root = tmp_path / "archive"
    _archive(root)
    result = _describe(response)
    assert result.status == idesc.STATUS_FAILED
    assert not result.transcript.present

    from tybot.archive.image_description_store import ImageDescriptionDenied

    ledger = _Ledger()
    try:
        ImageDescriptionStore(ledger).finish_and_publish("job-1", result)
    except ImageDescriptionDenied:
        # 호출 예외(timeout·429)는 provider 영수증이 없어 원장이 거절하고 롤백한다.
        # worker 는 그 job 을 unknown 으로 닫고 재전송하지 않는다(dispatch_reserved_image).
        assert ledger.events[-1] == "rollback"
    assert ledger.inserts("archiver_image_description_revision") == []
    assert ledger.inserts("archiver_image_transcript_revision") == []
    assert ledger.inserts("image_search_entry") == []

    store = ArchiveStore(root)
    conn = _EntryConn(_entries_from(ledger))
    assert image_guide.expand(store, _ctx(), "출입금지", conn=conn) == []
    # 길잡이가 없어도 로컬 OCR 이 원래대로 찾는다. 수집된 원본·줄은 그대로다.
    assert any(LOCAL_OCR in hit.line.text for hit in store.search("출입금지", _ctx(), limit=20))
    assert (attachment_storage(root, WS, CH).objects_dir / FID / "현장사진.png").is_file()


def test_ingest_never_waits_for_or_imports_the_vision_path():
    """수집 경로는 모델을 부르지 않는다. 로컬 OCR 만 돌고 끝난다."""
    root = Path(__file__).resolve().parents[1] / "src/tybot/archive"
    for name in ("files.py", "convert.py", "writer.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "image_description" not in source, name
        assert "image_transcript" not in source, name
        assert "google_provider" not in source, name


def test_a_failed_local_ocr_still_keeps_the_original(tmp_path, monkeypatch):
    """로컬 OCR 이 실패해도 원본 보관과 첨부 줄은 남는다(원본 수집 성공)."""
    from tybot.archive import files
    from tybot.archive.convert import ConvertError

    monkeypatch.setattr(files, "download_bytes", lambda *a, **k: PNG)

    def broken(*_a, **_k):
        raise ConvertError("ocr down")

    monkeypatch.setattr(files, "convert", broken)
    storage = files.attachment_storage(tmp_path / "archive", WS, CH)
    (item,) = files.stage_attachments(
        [{"id": FID, "name": "현장사진.png", "filetype": "png", "size": len(PNG)}],
        "test-token",
        storage,
    )
    assert item.extracted is False
    assert any(f"id:{FID}" in line for line in item.lines)
    meta = json.loads(item.metadata_path.read_text(encoding="utf-8"))
    assert meta["original_state"] == "retained"
    assert meta["sha256"] == SHA


# --- 6. 스키마 -----------------------------------------------------------------

SCHEMA = Path(__file__).resolve().parents[1] / "deploy/sql/archiver_image_transcript_schema.sql"


def test_schema_index_expression_matches_the_guide_query():
    sql = SCHEMA.read_text(encoding="utf-8")
    expression = image_guide.ENTRY_COMPACT_BODY.replace("e.", "")
    assert sql.count(f"(({expression}) gin_") == 2


def test_master_reads_only_the_projection_never_the_vision_ledger():
    sql = "\n".join(
        line.split("--", 1)[0] for line in SCHEMA.read_text(encoding="utf-8").splitlines()
    )
    master = [line for line in sql.splitlines() if "TO tyslackai" in line]
    assert master == ["        GRANT SELECT ON TABLE image_search_entry TO tyslackai;"]
    import sys
    from importlib import import_module

    sys.path.insert(0, str(SCHEMA.parents[2] / "scripts"))
    drift = import_module("check_schema_drift")
    assert drift.VISION_TABLES["archiver_image_transcript_revision"] == SCHEMA.name
    assert "image_search_entry" not in drift.VISION_TABLES
    assert drift.MASTER_READ_ONLY["image_search_entry"] == SCHEMA.name


def test_schema_labels_and_limits_match_the_code():
    sql = SCHEMA.read_text(encoding="utf-8")
    assert f"'{itx.LABEL}'" in sql
    assert f"line_count BETWEEN 1 AND {itx.MAX_LINES}" in sql
    assert "vector_state text NOT NULL DEFAULT 'parked'" in sql
    assert "INSERT INTO" not in sql.upper()


def test_a_worker_timeout_closes_the_job_without_a_revision_or_a_resend():
    """timeout 은 결과 미확정으로 닫힌다. 같은 사진을 다시 보내지 않는다."""
    from tybot.archive import image_description_worker as worker

    calls: list = []

    class _Store:
        def begin_dispatch(self, job_id, image, *, checks):
            calls.append("begin")
            return SimpleNamespace(
                model=MODEL, provider=PROVIDER, max_output_tokens=1024, timeout_seconds=20.0,
            )

        def finish_and_publish(self, job_id, result):
            calls.append("publish")
            return ImageDescriptionStore(_Ledger()).finish_and_publish(job_id, result)

        def mark_unknown(self, job_id, *, code):
            calls.append(("unknown", code))

    router = _Router(ProviderTimeout("timeout"))
    with pytest.raises(worker.ImageDescriptionWorkerError):
        worker.dispatch_reserved_image(_Store(), "job-1", _image(), router=router, checks={})
    assert router.calls == 1
    assert calls == ["begin", "publish", ("unknown", "publish_result_unknown")]


def test_the_answer_path_actually_calls_the_image_guide():
    import inspect

    from tybot import answer as answer_module

    source = inspect.getsource(answer_module.AnswerEngine.answer)
    assert "self._with_approved_guide(query, hits, ctx)" in source
    assert "self._with_image_guide(query, hits, ctx)" in source


def _write_attachment(root: Path, **over) -> None:
    base = {
        "workspace": WS, "channel_id": CH, "channel": CHANNEL, "file_id": FID,
        "name": "현장사진.png", "revision": "rev0000000001", "visibility": "private",
        "acl": frozenset({CHANNEL}), "text": LOCAL_OCR,
        "conversion_state": attachment_doc.CONVERTED, "message_ts": "1794000000.000100",
        "filetype": "png", "sha256": SHA, "staged_at": "2026-11-12T01:00:00+00:00",
        "human_upload_confirmed": True,
    }
    doc = attachment_doc.AttachmentDoc(**(base | over))
    path = root / doc.relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(attachment_doc.render(doc), encoding="utf-8")


def test_only_the_matched_photo_lines_come_back_not_its_neighbours(tmp_path):
    """같은 채널의 **다른 사진** OCR 줄이 섞이면 출처가 다른 사진을 가리킨다."""
    store, conn = _end_to_end(tmp_path)
    _write_attachment(
        store.root, file_id="F0IMG00002", name="다른사진.png", revision="rev0000000009",
        text="옆 사진의 글자 타워크레인", sha256="d" * 64,
        staged_at="2026-11-12T02:00:00+00:00",
    )
    hits = image_guide.expand(ArchiveStore(store.root), _ctx(), "타워크레인", conn=conn)
    assert hits
    assert all("옆 사진" not in hit.line.text for hit in hits)


def test_a_newer_canonical_revision_with_other_bytes_retires_the_old_projection(tmp_path):
    """재수집으로 정본 해시가 바뀌면 옛 투영으로 그 사진을 찾지 않는다."""
    store, conn = _end_to_end(tmp_path)
    _write_attachment(
        store.root, revision="rev0000000002", sha256="e" * 64,
        staged_at="2026-11-12T05:00:00+00:00",
    )
    assert image_guide.expand(ArchiveStore(store.root), _ctx(), "타워크레인", conn=conn) == []
