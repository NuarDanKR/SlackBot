from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from tybot.access import RequestContext
from tybot.answer import Answer, AnswerEngine
from tybot.archive.store import ArchiveDoc, ArchiveStore, RawLine, SearchHit
from tybot.evidence_refs import refs_from_hits
from tybot.intent import Intent
from tybot.question_files import QuestionFileEvidence
from tybot.slack import pilot
from tybot.slack.pilot import WorkspaceBot

LINK = "https://tyit.slack.com/files/U1/F01234561/report.pdf"


def _bot(tmp_path) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot.store = ArchiveStore(tmp_path)
    bot.cfg = SimpleNamespace(bot_token="xoxb-fixture")
    return bot


def test_dm_conversion_is_reused_instead_of_downloading_the_file_twice(
    tmp_path, monkeypatch
):
    bot = _bot(tmp_path)
    staged = SimpleNamespace(
        file_id="F01234561",
        body_lines=("변환된 첨부 본문",),
        warnings=(),
    )
    calls = []

    def read(files, token, *, loader):
        calls.append((files, token))
        loaded = loader(files, token)
        return QuestionFileEvidence(
            lines=loaded.lines,
            codes=("file_read_partial",) if loaded.warnings else (),
        )

    monkeypatch.setattr(pilot, "read_question_files", read)
    got = bot._question_file_evidence(
        {"files": [{"id": "F01234561"}], "_tybot_dm_attachments": [staged]}
    )

    assert got.lines == ("변환된 첨부 본문",)
    assert calls == [([{"id": "F01234561"}], "xoxb-fixture")]


def test_transient_question_file_hits_cannot_be_saved_as_reopenable_refs(tmp_path):
    bot = _bot(tmp_path)
    attached = QuestionFileEvidence(lines=("현장 진척 72%",), citations=(LINK,))

    hits = bot._question_file_hits(attached, {"ts": "1793930000.100"})

    assert [hit.line.text for hit in hits] == ["현장 진척 72%"]
    assert hits[0].doc.channel_id is None
    assert refs_from_hits(hits, tmp_path) == []


def test_slack_permalink_replaces_the_synthetic_document_citation():
    answer = Answer(
        "답변",
        ["[질문 첨부], 📄__question_attachment__.txt(2026-11-06)"],
        "model",
        0.0,
        1,
        "answered",
    )
    attached = QuestionFileEvidence(lines=("본문",), citations=(LINK,))

    got = WorkspaceBot._apply_question_file_citations(answer, attached)

    assert got.citations == [LINK]


def test_a_failed_answer_never_claims_the_file_as_a_source():
    answer = Answer("읽지 못함", [], None, 0.0, 0, "no_hits")
    attached = QuestionFileEvidence(lines=("본문",), citations=(LINK,))

    got = WorkspaceBot._apply_question_file_citations(answer, attached)

    assert got.citations == []


def test_access_context_is_built_before_question_files_are_opened():
    route = inspect.getsource(WorkspaceBot.route_message)
    handler = inspect.getsource(WorkspaceBot._handle_request)
    assert route.index("self._request_context(") < route.index("self._handle(")
    assert handler.index('event.get("_tybot_request_context")') < handler.index(
        "self._question_file_evidence(event)"
    )


def test_an_answer_that_did_not_consume_evidence_never_cites_the_file():
    answer = Answer("대기 중입니다.", [], None, 0.0, 0, "smalltalk")
    attached = QuestionFileEvidence(lines=("본문",), citations=(LINK,))

    got = WorkspaceBot._apply_question_file_citations(answer, attached)

    assert got.citations == []



# --- 알려진 결함: 선언 크기만 믿는다 ------------------------------------------
#
# 2026-11-06 실측. `question_files` 의 10MB/20MB 상한은 Slack **metadata 의
# `size`** 만 본다. 실제 다운로드는 `archive.files.file_lines` 가 하고, 그쪽은
#
#     download_bytes(f, bot_token)        # limit=None → resp.read() 전량
#     download_text(f, bot_token)         # MAX_TEXT_BYTES 기본 0 = 무제한
#
# 둘 다 **받은 바이트를 세지 않는다.** 선언 1KB 파일이 40MB 를 끝까지 받는 것을
# 가짜 응답으로 재현했다.
#
# 이 저장소는 이미 같은 모양으로 한 번 넘어졌다 — `deploy/tybot.service` 의
# 2026-11-02 주석: "unbounded attachment search grew the main process past
# 3 GiB and triggered the host-wide OOM killer".
#
# 아카이브 수집 경로의 무제한 기본값은 **의도된 선택**이다(`convert._limit`:
# 변환 손실은 되돌릴 수 없다). 문제는 그 선택을 **질문 첨부**가 물려받은 것이다.
# 질문 첨부는 일회용이고, 크기 주장은 우리가 만든 값이 아니다.
#
# 고치는 자리: `question_files.read_question_files` 가 아카이브의 무제한
# `file_lines` 대신 **바이트 예산을 건 로더**를 쓰게 한다
# (`download_bytes(f, token, limit=MAX_FILE_BYTES)` + 누적 ≤ MAX_TOTAL_BYTES).
# `archive/files.py` 의 기본값은 건드리지 않는다 — 수집 쪽 계약은 그대로다.


def test_the_actual_downloaded_bytes_are_bounded(monkeypatch):
    """선언이 1KB 여도 **받은 바이트**가 상한을 넘으면 거절한다.

    Slack metadata 의 `size` 는 우리가 만든 값이 아니다. 그것만 믿으면 1KB 라고
    적힌 파일이 수십 MB 를 끌고 들어온다 — 이 저장소가 2026-11-02 에 겪은
    모양이다(`deploy/tybot.service`: "unbounded attachment search grew the main
    process past 3 GiB and triggered the host-wide OOM killer").

    아카이브 수집 경로의 무제한 기본값은 **의도된 선택**이다(`convert._limit`:
    변환 손실은 되돌릴 수 없다). 질문 첨부는 일회용이라 그 선택을 물려받지
    않는다.
    """
    from tybot import question_files
    from tybot.archive import files as archive_files

    oversized = b"%PDF-1.4\n" + b"A" * (question_files.MAX_FILE_BYTES + 1)
    reads: list = []

    class Response:
        headers: ClassVar[dict] = {"Content-Type": "application/pdf"}

        def read(self, size=None):
            reads.append(size)
            return oversized if size is None else oversized[:size]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(archive_files, "urlopen", lambda *a, **k: Response())

    refused = question_files.read_question_files(
        [
            {
                "id": "F0OVERSIZE",
                "size": 1024,  # 거짓말
                "name": "small.pdf",
                "filetype": "pdf",
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/x/small.pdf",
                "permalink": LINK,
            }
        ],
        "xoxb-fixture",
    )

    assert refused.lines == ()
    # **결정적 단언은 이것 하나다.** 변환이 뒤에서 실패해 거절 코드가 나오는
    # 것은 우연이고, 그때도 파일은 이미 전량 메모리에 들어와 있다. 봐야 하는
    # 것은 `read()` 에 상한이 걸렸는가다.
    assert reads, "다운로드가 일어나지 않았다 — 시험이 가리키는 자리가 아니다"
    assert all(size is not None for size in reads), (
        f"limit 없이 전량을 읽었다: read({reads}) · 선언 size 는 1024 였다"
    )
    assert all(size <= question_files.MAX_FILE_BYTES + 1 for size in reads)


def test_a_rejected_attachment_is_never_replaced_by_an_archive_search(
    tmp_path, monkeypatch
):
    """첨부를 못 읽었는데 **검색 결과로 조용히 메우지 않는다.**

    사람은 그 파일을 보고 물었다. 못 읽었으면 못 읽었다고 말해야, 자기가 받은
    답이 그 파일의 답이 아니라는 것을 안다.
    """
    bot = _bot(tmp_path)
    monkeypatch.setattr(
        pilot,
        "read_question_files",
        lambda *a, **k: QuestionFileEvidence(codes=("file_size_exceeded",)),
    )
    event = {"files": [{"id": "F01234561"}], "ts": "1762400000.000100"}

    got = bot._question_file_evidence(event)

    assert got.lines == ()
    assert got.codes == ("file_size_exceeded",)
    assert event["_tybot_question_file_used"] is False
    # 호출부는 이 상태에서 `engine.respond` 로 내려가지 않는다 — 거절 답변을
    # 만들고 `continue` 한다. 그 분기가 사라지면 검색 결과가 답으로 나간다.
    source = inspect.getsource(pilot.WorkspaceBot._handle_request_tasks) if hasattr(
        pilot.WorkspaceBot, "_handle_request_tasks"
    ) else inspect.getsource(pilot)
    guard = 'if event.get("files") and (attached is None or not attached.lines):'
    assert guard in source
    branch = source[source.index(guard) : source.index(guard) + 900]
    assert '"no_hits"' in branch and "continue" in branch
    assert "engine.respond" not in branch


def test_an_advice_question_keeps_the_new_attachment_in_the_model_input():
    """판단 경로도 첨부를 **모델 입력에** 넣는다.

    요약·일반 답변만 챙기고 판단을 빠뜨리면 「이 파일 보고 어떻게 할까」 가
    파일 없이 답해진다 — 그 답은 틀렸다는 표시조차 없다. 소스를 훑지 않고
    실제로 프롬프트에 들어갔는지 본다.
    """
    seed_text = "합성 첨부 본문 ZZZSEEDZZZ"
    prompts: list[str] = []

    class Router:
        def complete(self, messages, **_kwargs):
            prompts.append(str(messages[-1].content))
            return SimpleNamespace(text="답", model="m", cost_usd=0.0, raw=None)

        def pick(self, *_a, **_k):
            return "m"

    class Store:
        root = Path("/x")

        def search(self, *_a, **_k):
            return []

        def source_docs(self):
            return []

        def visible_docs(self, _ctx):
            return []

        def resolve_refs(self, _refs, _ctx):
            return [], []

    doc = ArchiveDoc(
        path=Path("/x/__question_attachment__.txt"),
        workspace="tyit",
        channel="질문 첨부",
        visibility="private",
        acl=frozenset(),
        share_with=frozenset(),
        last_ingested=None,
        channel_id=None,
        raw_lines=[],
    )
    line = RawLine(
        ts="2026-11-06 09:00",
        speaker="질문 첨부",
        text=seed_text,
        lineno=1,
        source_path=doc.path,
        message_ts="1.1",
    )
    seed = [SearchHit(doc=doc, line=line, score=1)]
    engine = AnswerEngine(Store(), Router(), allow_master_business_answers=True)
    ctx = RequestContext(workspace="tyit", channels=frozenset({"#c"}))

    engine.respond(
        "이 파일 보고 어떻게 할까",
        ctx,
        Intent(kind="advice", question="이 파일 보고 어떻게 할까"),
        seed_hits=seed,
    )

    assert prompts, "모델이 불리지 않았다"
    assert seed_text in "".join(prompts), "판단 경로가 첨부를 모델에 안 넣었다"
