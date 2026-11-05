"""B-12 질의 임베딩 실행 경계 시험 — 섀도 전용.

**여기 쓰는 글은 전부 합성이다.** 실제 사내 원문·채널명·사람 이름·질문을 넣지 않는다.

붙들고 있는 것은 다섯이다.

1. 정책이 통과하기 전에는 **질문이 이 프로세스를 못 떠난다**
2. 호출은 **정확히 한 번.** 실패해도 다시 부르지 않는다
3. 질문 본문·벡터·프로바이더 오류 본문은 결과·로그·`repr` 어디에도 없다
4. 승인된 `SearchScope` 그대로 넘기고, 현재 권한·revision 으로 **다시** 검증한다
5. 결과는 섀도다 — 점수도 `SearchHit` 도 들지 않는다
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import fields
from pathlib import Path

import pytest

from tybot.access import RequestContext
from tybot.archive.embedding_source import VisibleSources
from tybot.archive.store import ArchiveStore
from tybot.embedding_index_job import EmbeddingFailure
from tybot.embedding_query_run import (
    DETAIL_WORDS,
    QUERY_OUTCOMES,
    ShadowQueryResult,
    _Once,
    run_shadow_query,
)
from tybot.embedding_search import SearchScope
from tybot.evidence_refs import EvidenceRef

WS = "pilot"
CTX = RequestContext(workspace=WS)
OTHER = RequestContext(workspace="elsewhere")
CHANNEL_NAME = "#팀_합성000(ZZZ000)_생성"
CHANNEL_ID = "C00000000"
RAW_REL = f"workspaces/{WS}/channels/{CHANNEL_ID}/raw/2026-01-01.md"
MODEL = "synth-model-1"
CHUNKER = "source-v1:chunk-v1:max=1200:ov=120"

#: 합성 질문. 어떤 출력에도 나오면 안 된다.
QUESTION = "합성 질문 ZZZQUESTIONZZZ 무엇인가"
#: 합성 벡터. 역시 어떤 출력에도 나오면 안 된다.
VECTOR = (0.1234567, -0.7654321, 0.5)
#: 프로바이더가 4xx 본문에 질문을 되돌려 주는 상황.
PROVIDER_BODY = f"400 invalid_request: input was {QUESTION!r}"


# --- 합성 아카이브 -----------------------------------------------------------


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = tmp_path / "workspaces" / WS / "channels" / CHANNEL_ID / "raw"
    out.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(
        f"> [2026-01-01 09:{i:02d} |1767000{i:04d}.0001] 화자{i % 3}: 합성 본문 {i}"
        for i in range(6)
    )
    (out / "2026-01-01.md").write_text(
        f'---\nworkspace: {WS}\nchannel: "{CHANNEL_NAME}"\n'
        f"channel_id: {CHANNEL_ID}\nvisibility: public\nacl: []\n"
        f"last_ingested: 2026-01-01T17:00+09:00\n---\n\n"
        f"## 원문 (자동 취합, 편집 금지)\n{rows}\n",
        encoding="utf-8",
    )
    return tmp_path


def window(root, ctx=CTX) -> VisibleSources:
    return VisibleSources(ArchiveStore(root), ctx)


def canonical(root):
    entry = window(root).entry(WS, CHANNEL_ID, RAW_REL)
    assert entry is not None
    return entry


def candidate_row(root, *, source_revision=None, number=0):
    entry = canonical(root)
    span = entry.spans[number]
    text = entry.source.text[span.start : span.end]
    return {
        "workspace": WS,
        "channel_id": CHANNEL_ID,
        "doc_path": RAW_REL,
        "source_revision": source_revision or entry.source.source_revision,
        "chunker_revision": CHUNKER,
        "model_revision": MODEL,
        "chunk_no": number,
        "start_offset": span.start,
        "end_offset": span.end,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "distance": 0.1,
    }


# --- 가짜 DB·정책·임베더 -----------------------------------------------------


class Cursor:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.payload

    def fetchall(self):
        return self.payload


class Database:
    def __init__(self, payload):
        self.cur = Cursor(payload)
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self.cur


class Exploding:
    def cursor(self):
        raise RuntimeError(f"policy lookup blew up near {QUESTION!r}")


def policy(**changes):
    values = {
        "allow_query": True,
        "model": MODEL,
        "transport": "local",
        "revision": 3,
        "query_external": False,
        "state": "active",
        "model_external": False,
    }
    values.update(changes)
    return Database(tuple(values.values()))


class Batch:
    def __init__(self, vectors):
        self.vectors = vectors
        self.model_revision = MODEL
        self.cost_usd = None


class Embedder:
    """주입되는 임베더. 호출 수와 받은 인자를 센다."""

    def __init__(self, *, vectors=(VECTOR,), raises=None):
        self.vectors = vectors
        self.raises = raises
        self.calls = []

    def embed(self, inputs, *, model_revision, policy_revision):
        self.calls.append((inputs, model_revision, policy_revision))
        if self.raises is not None:
            raise self.raises
        return Batch(self.vectors)


class Connector:
    """`candidates()` 가 쓰는 연결 공장. 몇 번 불렸는지 센다."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
        self.db = None

    def __call__(self):
        self.calls += 1
        if self.rows is None:
            return None
        self.db = Database(self.rows)
        return self.db


def scope(channels=(CHANNEL_ID,), workspace=WS) -> SearchScope:
    return SearchScope.checked(workspace, channels)


def run(root, **overrides):
    kwargs = {
        "workspace": WS,
        "model_revision": MODEL,
        "transport": "local",
        "policy_connection": policy(),
        "embedder": Embedder(),
        "scope": scope(),
        "sources": window(root),
        "root": root,
        "connect": Connector([candidate_row(root)]),
    }
    kwargs.update(overrides)
    return run_shadow_query(QUESTION, **kwargs), kwargs


# --- 1. 정책이 통과하기 전에는 아무것도 안 나간다 -----------------------------


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"allow_query": False}, "query_disabled"),
        ({"model": "other-v1"}, "model_mismatch"),
        ({"state": "draft"}, "model_inactive"),
        ({"transport": "external"}, "transport_mismatch"),
    ],
)
def test_a_closed_policy_stops_the_question_inside_this_process(
    archive, changes, detail
):
    got, used = run(archive, policy_connection=policy(**changes))
    assert got.outcome == "policy_denied"
    assert got.detail == detail
    assert got.embed_calls == 0
    assert used["embedder"].calls == []
    assert used["connect"].calls == 0


def test_an_unapproved_external_transport_never_sends_the_question(archive):
    got, used = run(
        archive,
        transport="external",
        policy_connection=policy(transport="external", query_external=False),
    )
    assert (got.outcome, got.detail) == (
        "policy_denied",
        "query_external_not_approved",
    )
    assert used["embedder"].calls == []


def test_an_unreadable_policy_is_not_an_approval(archive):
    got, used = run(archive, policy_connection=Exploding())
    assert (got.outcome, got.detail) == ("db_unavailable", "policy_unreadable")
    assert got.embed_calls == 0
    assert used["embedder"].calls == []


def test_the_provider_error_text_from_the_policy_read_is_not_kept(archive, caplog):
    with caplog.at_level(logging.DEBUG):
        got, _ = run(archive, policy_connection=Exploding())
    assert QUESTION not in caplog.text
    assert QUESTION not in repr(got)


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"workspace": "질문처럼 생긴 값"}, "request_invalid"),
        ({"model_revision": ""}, "request_invalid"),
        ({"transport": "carrier-pigeon"}, "request_invalid"),
        ({"scope": SearchScope.checked("elsewhere", (CHANNEL_ID,))}, "scope_mismatch"),
    ],
)
def test_a_request_that_does_not_hold_together_is_refused_before_the_call(
    archive, overrides, detail
):
    got, used = run(archive, **overrides)
    assert (got.outcome, got.detail) == ("policy_denied", detail)
    assert used["embedder"].calls == []
    assert used["connect"].calls == 0


# --- 2. 호출은 정확히 한 번 --------------------------------------------------


def test_the_happy_path_embeds_the_question_exactly_once(archive):
    got, used = run(archive)
    assert got.outcome == "ok"
    assert got.embed_calls == 1
    assert len(used["embedder"].calls) == 1


def test_the_embedder_is_told_which_approval_the_call_is_made_under(archive):
    _, used = run(archive)
    inputs, model_revision, policy_revision = used["embedder"].calls[0]
    assert model_revision == MODEL
    assert policy_revision == "query:pilot:r3"
    assert len(inputs) == 1
    assert inputs[0].chunk_no == 0
    assert (
        inputs[0].text_sha256
        == hashlib.sha256(QUESTION.encode("utf-8")).hexdigest()
    )


@pytest.mark.parametrize(
    ("raises", "detail"),
    [
        (EmbeddingFailure("embedding_call_timeout"), "embedding_call_timeout"),
        (EmbeddingFailure("embedding_identity_mismatch"), "embedding_identity_mismatch"),
        (RuntimeError(PROVIDER_BODY), "embedding_call_failed"),
    ],
)
def test_a_failed_call_is_not_tried_again(archive, raises, detail):
    got, used = run(archive, embedder=Embedder(raises=raises))
    assert (got.outcome, got.detail) == ("embedding_failed", detail)
    assert got.embed_calls == 1
    assert len(used["embedder"].calls) == 1
    assert used["connect"].calls == 0


def test_a_second_call_is_refused_by_the_wrapper_not_by_a_promise():
    once = _Once(Embedder())
    once.embed((), model_revision=MODEL, policy_revision="query:pilot:r3")
    with pytest.raises(RuntimeError, match="query_embedder_called_twice"):
        once.embed((), model_revision=MODEL, policy_revision="query:pilot:r3")


@pytest.mark.parametrize("vectors", [(), (VECTOR, VECTOR)])
def test_anything_other_than_one_vector_for_one_question_is_a_failure(
    archive, vectors
):
    got, used = run(archive, embedder=Embedder(vectors=vectors))
    assert (got.outcome, got.detail) == (
        "embedding_failed",
        "embedding_result_incomplete",
    )
    assert used["connect"].calls == 0


def test_a_blank_question_is_refused_without_calling_the_provider(archive):
    embedder = Embedder()
    got = run_shadow_query(
        "   ",
        workspace=WS,
        model_revision=MODEL,
        transport="local",
        policy_connection=policy(),
        embedder=embedder,
        scope=scope(),
        sources=window(archive),
        root=archive,
        connect=Connector([]),
    )
    assert (got.outcome, got.detail) == (
        "embedding_failed",
        "embedding_request_invalid",
    )
    assert embedder.calls == []


# --- 3. 본문·벡터·오류 본문은 어디에도 안 남는다 ------------------------------


def test_no_result_field_carries_the_question_the_vector_or_an_error_body(archive):
    got, _ = run(archive, embedder=Embedder(raises=RuntimeError(PROVIDER_BODY)))
    blob = repr(got) + "".join(str(getattr(got, f.name)) for f in fields(got))
    assert QUESTION not in blob
    assert "invalid_request" not in blob
    for value in VECTOR:
        assert str(value) not in blob


def test_the_log_line_carries_counts_and_fixed_words_only(archive, caplog):
    with caplog.at_level(logging.DEBUG):
        got, _ = run(archive)
    assert QUESTION not in caplog.text
    for value in VECTOR:
        assert str(value) not in caplog.text
    assert got.outcome in caplog.text


def test_a_provider_body_never_reaches_the_log_either(archive, caplog):
    with caplog.at_level(logging.DEBUG):
        run(archive, embedder=Embedder(raises=RuntimeError(PROVIDER_BODY)))
    assert QUESTION not in caplog.text
    assert "invalid_request" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_every_word_a_result_can_say_is_from_a_fixed_set():
    assert "ok" in QUERY_OUTCOMES
    assert all(re.fullmatch(r"[a-z_]*", word) for word in DETAIL_WORDS)
    with pytest.raises(ValueError, match="unknown query outcome"):
        ShadowQueryResult(outcome="그럭저럭")
    with pytest.raises(ValueError, match="unknown query detail"):
        ShadowQueryResult(outcome="policy_denied", detail="400 from provider")


# --- 4. 승인된 범위 그대로, 그리고 다시 검증 ---------------------------------


def test_only_the_approved_scope_reaches_the_index(archive):
    _, used = run(archive, scope=scope(("C00000000", "C11111111")))
    _, params = used["connect"].db.cur.calls[0]
    assert params["workspace"] == WS
    assert params["channel_ids"] == ["C00000000", "C11111111"]
    assert params["model_revision"] == MODEL


def test_a_requester_with_no_channels_never_opens_the_index(archive):
    got, used = run(archive, scope=scope(()))
    assert (got.outcome, got.detail) == ("no_candidates", "")
    assert got.embed_calls == 1
    assert used["connect"].calls == 0


def test_a_candidate_whose_source_moved_is_a_snapshot_change_not_an_answer(archive):
    row = candidate_row(archive, source_revision="b" * 64)
    got, _ = run(archive, connect=Connector([row]))
    assert got.outcome == "snapshot_changed"
    assert got.detail == "source_revision_changed"
    assert got.candidate_count == 1
    assert got.resolved_count == 0
    assert got.refs == ()


def test_a_candidate_the_requester_may_not_see_is_indistinguishable_from_none(archive):
    # 같은 후보, 권한만 다른 창구. 「바뀌었다」 와 구분되면 벡터 캐시가
    # 문서 존재 여부를 묻는 창구가 된다.
    got, _ = run(archive, sources=window(archive, OTHER))
    assert (got.outcome, got.detail) == ("no_candidates", "")
    assert got.candidate_count == 1
    assert got.resolved_count == 0
    assert "source_not_visible" in got.dropped


def test_a_surviving_candidate_comes_back_as_reopenable_coordinates(archive):
    got, _ = run(archive)
    assert got.outcome == "ok"
    assert got.candidate_count == 1
    assert got.resolved_count == 1
    assert got.refs
    for ref in got.refs:
        assert isinstance(ref, EvidenceRef)
        assert ref.workspace == WS
        assert ref.channel_id == CHANNEL_ID
        assert ref.document_path == RAW_REL
        assert ref.line_no > 0
        assert (Path(archive) / ref.document_path).exists()


def test_an_unreadable_index_is_told_apart_from_an_empty_one(archive):
    unreadable, used = run(archive, connect=Connector(None))
    assert (unreadable.outcome, unreadable.detail) == (
        "db_unavailable",
        "vector_index_unreadable",
    )
    assert unreadable.embed_calls == 1
    assert used["connect"].calls == 1
    empty, _ = run(archive, connect=Connector([]))
    assert (empty.outcome, empty.detail) == ("no_candidates", "")


# --- 5. 섀도다 ---------------------------------------------------------------


def test_the_result_has_nowhere_to_put_a_score_or_a_hit():
    names = {f.name for f in fields(ShadowQueryResult)}
    assert names == {
        "outcome",
        "detail",
        "model_revision",
        "policy_revision",
        "transport",
        "embed_calls",
        "candidate_count",
        "resolved_count",
        "refs",
        "dropped",
    }
    for forbidden in ("score", "hit", "text", "line", "answer", "rank"):
        assert not any(forbidden in name for name in names)


def test_an_ok_result_cannot_be_built_without_coordinates():
    with pytest.raises(ValueError, match="ok requires evidence coordinates"):
        ShadowQueryResult(outcome="ok")
    with pytest.raises(ValueError, match="ok requires evidence coordinates"):
        ShadowQueryResult(
            outcome="no_candidates",
            refs=(EvidenceRef(kind="archive_line", workspace=WS, channel_id=CHANNEL_ID),),
        )


def test_a_question_is_never_counted_as_embedded_twice():
    with pytest.raises(ValueError, match="at most once"):
        ShadowQueryResult(outcome="no_candidates", embed_calls=2)


def test_nothing_in_the_product_calls_the_shadow_run_yet():
    root = Path(__file__).resolve().parents[1] / "src"
    callers = [
        path
        for path in root.rglob("*.py")
        if path.name != "embedding_query_run.py"
        and "embedding_query_run" in path.read_text(encoding="utf-8")
    ]
    assert callers == [], f"섀도 경로가 답변에 물렸다: {callers}"


# --- 6. 잘못 엮인 호출은 조용히 지나가지 않는다 -------------------------------


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"scope": (CHANNEL_ID,)}, TypeError),
        ({"embedder": object()}, TypeError),
        ({"sources": object()}, TypeError),
        ({"limit": 0}, ValueError),
        ({"limit": True}, ValueError),
    ],
)
def test_a_miswired_boundary_fails_loudly_at_wiring_time(archive, overrides, error):
    with pytest.raises(error):
        run(archive, **overrides)
