"""TYBot 연동의 **역할 분리와 검토 대조** — 중복 발송·미발송 양쪽을 본다.

설계: `docs/design/hermes-write-entrypoints.md` §3·§6

여기서 보는 것은 셋이다.

1. 연동 모드에서 Hermes 가 **요약·위생 점검을 보내지 않는다**(중복 발송)
2. PF 기본 모드에서는 **그대로 보낸다**(미발송 — 차단이 운영을 끄면 실패)
3. 이미 끝낸 검토를 생략할 때 **좌표와 해시로만** 판정한다

2번이 1번만큼 중요하다. 중복을 막는다고 PF 운영을 조용히 멈추면, 자료가 낡기
시작하는데 오류는 한 줄도 안 난다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tybot import summary_review_reconcile as rec

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "subbots" / "hermes"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None or not HERMES.is_dir(), reason="node 또는 subbots/hermes 가 없다"
)
# CLI 는 `@slack/bolt` 등을 import 한다. 없으면 ESM 이 **링크 단계에서** 실패해
# 어떤 최상위 코드도 돌지 못한다 — 관문이 설 자리가 없다. 환경 문제이지 결함이
# 아니므로, 그때는 정적 시험이 같은 것을 본다.
needs_deps = pytest.mark.skipif(
    not (HERMES / "node_modules").is_dir(),
    reason="subbots/hermes/node_modules 가 없어 실행 시험은 건너뛴다",
)


def _roles(mode: str | None) -> list[str]:
    # **환경을 통째로 물려준다.** 최소 env 로 띄우면 윈도우에서 node 가 뜨지 않아
    # 「역할이 비었다」 가 아니라 「프로세스가 죽었다」 로 실패한다.
    env = dict(os.environ)
    env.pop("HERMES_MODE", None)
    if mode is not None:
        env["HERMES_MODE"] = mode
    got = subprocess.run(
        [NODE, "--input-type=module", "-e",
         "import {rolesFor} from './src/mode.js';"
         "console.log(JSON.stringify([...rolesFor()].sort()));"],
        cwd=HERMES, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert got.returncode == 0, got.stderr
    return json.loads(got.stdout.strip())


# --- 1·2. 역할 분리 ------------------------------------------------------------
@needs_node
def test_pf_mode_keeps_every_role():
    """PF 직접 실행은 **지금까지와 같다.** 질문·DM·요약·수집 전부 Hermes 몫이다."""
    assert _roles(None) == ["answer", "digest-publish", "health", "ingest"]


@needs_node
def test_pf_archiver_mode_keeps_pf_features_but_drops_collection():
    """PF 질문·DM·요약은 유지하고 원문 writer만 Archiving Bot에 넘긴다."""
    assert _roles("pf-archiver") == ["answer", "digest-publish", "health"]


@needs_node
def test_tybot_mode_leaves_only_answering():
    """연동 모드에서는 독립 Node 런타임을 띄우지 않는다.

    답변 규칙의 주인은 Hermes 지만 실행은 TYBot 의 계약·권한 도구 경로다.
    """
    assert _roles("tybot") == []


@needs_node
@needs_deps
@pytest.mark.parametrize(
    ("script", "role"), [("run-digest.js", "digest-publish"), ("run-health.js", "health")]
)
def test_a_send_cli_refuses_in_tybot_mode(script, role):
    """스케줄만 막으면 손으로 돌리는 길이 남는다 — 그게 중복 발송의 실제 경로다."""
    env = dict(os.environ)
    env["HERMES_MODE"] = "tybot"
    got = subprocess.run(
        [NODE, f"scripts/{script}", "daily"],
        cwd=HERMES, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )

    assert got.returncode == 2, f"{script} 종료 코드 {got.returncode}"
    assert "역할이 아닙니다" in got.stderr
    assert "두 번" in got.stderr


def test_the_scheduler_registers_jobs_by_role():
    """역할이 없으면 **예약하지 않는다.** 예약 후 실패는 「고장」 으로 보인다."""
    text = (HERMES / "src" / "scheduler.js").read_text(encoding="utf-8")
    start = text.split("export function startScheduler", 1)[1]

    assert "ownsRole" in start
    for role in ("ROLES.DIGEST_PUBLISH", "ROLES.HEALTH", "ROLES.INGEST"):
        assert role in start, f"{role} 이 역할 기준으로 걸리지 않았다"


@pytest.mark.parametrize(
    ("module", "fn", "role"),
    [("src/digest.js", "runDigest", "DIGEST_PUBLISH"),
     ("src/archive-health.js", "runHealth", "HEALTH")],
)
def test_every_send_function_guards_in_its_first_lines(module, fn, role):
    """관문이 뒤에 있으면 모델을 부른 **뒤에** 막혀 비용이 나간다."""
    text = (HERMES / module).read_text(encoding="utf-8")
    body = text.split(f"function {fn}(", 1)[1]
    head = "\n".join(body.splitlines()[:8])

    assert "assertOwnsRole(" in head, f"{module}:{fn} 앞머리에 역할 관문이 없다"
    assert f"ROLES.{role}" in head


# --- 3. 검토 대조 --------------------------------------------------------------
LOCATOR = "2026-10-02.md:42"
DIGEST = "a" * 64


def _candidate(**kw) -> dict:
    base = {
        "workspace": "tyit",
        "channel_id": "C1",
        "evidence_locator": LOCATOR,
        "evidence_hash": DIGEST,
    }
    base.update(kw)
    return base


def _decision(**kw) -> rec.ExternalDecision:
    base = {
        "workspace": "tyit",
        "channel_id": "C1",
        "evidence_locator": LOCATOR,
        "evidence_hash": DIGEST,
        "state": "approved",
    }
    base.update(kw)
    return rec.ExternalDecision(**base)


def test_a_finished_decision_on_the_same_line_is_skipped():
    """좌표·출처·해시가 전부 같고 **확정**됐을 때만 생략한다."""
    assert rec.decide(_candidate(), [_decision()]) == (True, rec.SKIP)
    assert rec.decide(_candidate(), [_decision(state="rejected")]) == (True, rec.SKIP)


@pytest.mark.parametrize(
    ("name", "decisions", "reason"),
    [
        ("기록을 못 읽음", None, rec.RECORDS_UNREADABLE),
        ("끝낸 것이 없음", [], rec.NO_MATCH),
        ("보류", [_decision(state="deferred")], rec.NOT_FINAL),
        ("다른 채널", [_decision(channel_id="C9")], rec.SOURCE_MISMATCH),
        ("다른 워크스페이스", [_decision(workspace="mgmt")], rec.SOURCE_MISMATCH),
        ("결정 이후 원문 변경", [_decision(evidence_hash="b" * 64)], rec.EVIDENCE_CHANGED),
        ("다른 줄", [_decision(evidence_locator="2026-10-02.md:7")], rec.NO_MATCH),
    ],
)
def test_it_never_skips_when_anything_is_uncertain(name, decisions, reason):
    """묻는 쪽으로 틀리면 사람이 한 번 더 볼 뿐이다. 생략 쪽으로 틀리면 아무도 모른다."""
    skip, got = rec.decide(_candidate(), decisions)

    assert skip is False, name
    assert got == reason, name


def test_a_candidate_without_coordinates_is_never_skipped():
    """우리 후보에 좌표가 없으면 대조 자체가 불가능하다."""
    assert rec.decide(_candidate(evidence_hash=""), [_decision()]) == (
        False, rec.NO_COORDINATE,
    )
    assert rec.decide(_candidate(evidence_locator=""), [_decision()]) == (
        False, rec.NO_COORDINATE,
    )


def test_running_the_local_skill_is_not_a_reason_to_skip():
    """「스킬이 돌았다」 는 **어떤 항목을 어떤 원문으로** 끝냈는지 말해 주지 않는다.

    Hermes 의 현재 기록(`applied`·`dismissed`)은 항목 id 와 요약 섹션 해시만 담는다 —
    원문 줄 좌표도 그 줄의 해시도 없다. 그래서 그 기록만으로는 **아무것도 생략되지
    않아야 한다.**
    """
    hermes_shaped = rec.parse_decisions({
        "decisions": [
            {"state": "applied", "channel": "#팀-전산", "at": "2026-10-02T09:00:00+09:00"},
            {"state": "dismissed", "summaryHash": "deadbeef", "reason": "불필요"},
        ]
    })

    skip, reason = rec.decide(_candidate(), hermes_shaped)

    assert skip is False
    assert reason in (rec.NO_MATCH, rec.SOURCE_MISMATCH)


def test_unreadable_records_are_not_the_same_as_empty(tmp_path):
    """파일이 사라진 날 **모든 후보가 조용히 생략되는** 것이 가장 나쁜 실패다."""
    missing = tmp_path / "none.json"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    assert rec.load_decisions(missing) == []      # 없는 것은 읽은 것이다
    assert rec.load_decisions(broken) is None     # 못 읽은 것은 다르다


@pytest.mark.parametrize("payload", [[], {"decisions": []}, {"schema": "v2", "decisions": []}])
def test_an_unknown_decision_schema_is_unreadable(tmp_path, payload):
    """미래 형식을 구형 코드가 짐작해 읽으면 검토를 잘못 생략할 수 있다."""
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert rec.load_decisions(path) is None


def test_the_filter_reports_why_each_candidate_stayed():
    """「오늘 후보가 없다」 와 「전부 생략됐다」 는 사람이 할 일이 완전히 다르다."""
    rows = [_candidate(), _candidate(evidence_locator="2026-10-02.md:7")]

    kept, reasons = rec.filter_candidates(rows, [_decision()])

    assert len(kept) == 1
    assert reasons[rec.SKIP] == 1
    assert reasons[rec.NO_MATCH] == 1


# --- 공통 결정 기록 왕복 (설계 summary-approval-ports.md §3) -------------------
#
# 읽는 쪽만 있으면 대조는 늘 「끝낸 것이 없다」 로 끝난다. 쓰는 쪽이 있어야 승인
# 인터페이스를 갈아 끼워도(TYBot DM ↔ 로컬 스킬) 서로의 결정을 본다.
def _candidate_row(**kw) -> dict:
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "workspace": "tyit",
        "channel_id": "C1",
        "evidence_locator": LOCATOR,
        "evidence_hash": DIGEST,
        "evidence_message_ts": "1759000000.000100",
        "kind": "number_or_schedule",
        "state": "approved",
        "decided_at": "2026-10-06T09:00:00+09:00",
        "decided_by": "U1",
        "proposed_text": "공정률은 62.5%입니다",
        "evidence_quote": "공정률은 62.5%입니다",
    }
    base.update(kw)
    return base


def test_an_exported_decision_reads_back_and_matches(tmp_path):
    """내보낸 것을 그대로 읽어 **같은 후보에 대해 생략 판정**이 서야 한다."""
    path = tmp_path / "decisions.json"

    written = rec.write_export(path, [_candidate_row()])
    loaded = rec.load_decisions(path)

    assert written == 1
    assert rec.decide(_candidate(), loaded) == (True, rec.SKIP)


def test_the_export_never_carries_text(tmp_path):
    """이 파일은 **다른 쪽이 읽는 것**이다. 사내 문장이 PF 로, PF 문장이 사내로
    건너가면 크로스 워크스페이스 노출이다(원칙 4). 대조에 필요한 것은 좌표뿐이다.
    """
    path = tmp_path / "decisions.json"
    rec.write_export(path, [_candidate_row()])

    body = path.read_text(encoding="utf-8")

    assert "공정률" not in body, "요약 본문이 기록에 실렸다"
    assert "proposed_text" not in body
    assert "evidence_quote" not in body
    assert DIGEST in body and LOCATOR in body


@pytest.mark.parametrize("state", ["pending", "expired", "superseded"])
def test_states_that_are_not_a_human_decision_are_not_exported(tmp_path, state):
    """미응답 폐기·대체됨은 **사람이 내린 판단이 아니다.**

    보내면 받는 쪽이 「끝났다」 로 읽을 수 있다. 읽는 쪽이 조심하는 것보다 애초에
    안 보내는 쪽이 안전하다.
    """
    path = tmp_path / "decisions.json"

    assert rec.write_export(path, [_candidate_row(state=state)]) == 0
    assert rec.load_decisions(path) == []


def test_a_deferred_decision_is_exported_but_never_skips(tmp_path):
    """보류는 보낸다 — 받는 쪽이 「아직 안 끝났다」 를 알아야 한다. 생략은 안 한다."""
    path = tmp_path / "decisions.json"
    rec.write_export(path, [_candidate_row(state="deferred")])

    loaded = rec.load_decisions(path)

    assert len(loaded) == 1
    assert rec.decide(_candidate(), loaded) == (False, rec.NOT_FINAL)


def test_a_candidate_without_coordinates_is_not_exported(tmp_path):
    """좌표 없는 기록은 받는 쪽에서 버려질 뿐이라 보내지 않는다."""
    path = tmp_path / "decisions.json"

    assert rec.write_export(path, [_candidate_row(evidence_hash="")]) == 0


def test_the_export_is_written_atomically(tmp_path):
    """읽는 쪽이 반쯤 쓰인 파일을 보면 파싱이 깨지고, 깨짐은 「못 읽음」 이라
    대조가 통째로 멈춘다."""
    path = tmp_path / "decisions.json"
    rec.write_export(path, [_candidate_row()])
    rec.write_export(path, [_candidate_row(), _candidate_row(state="deferred")])

    assert not list(tmp_path.glob("*.tmp")), "임시 파일이 남았다"
    assert len(rec.load_decisions(path)) == 2


# --- 경계 보강 1. 모르는 모드 값은 기동 실패 --------------------------------
#
# 처음에는 「모르는 값은 PF 로 본다 — 오타가 운영을 멈추게 하지 않는다」 였다.
# **틀렸다.** `HERMES_MODE=tybo` 가 조용히 PF 가 되면 연동으로 띄운 줄 알았던
# 프로세스가 **쓰기가 열린 채로** 돈다. 그건 운영이 멈추는 것보다 나쁘다 —
# 멈추면 바로 알지만, 열린 채로 도는 것은 원문이 늘어난 뒤에야 안다.
@needs_node
@pytest.mark.parametrize("value", [None, "", "  ", "pf", "PF", " pf "])
def test_unset_or_exact_pf_is_pf(value):
    assert _roles(value) == ["answer", "digest-publish", "health", "ingest"]


@needs_node
@pytest.mark.parametrize("value", ["tybot", "TYBOT", " tybot "])
def test_exact_tybot_is_tybot(value):
    assert _roles(value) == []


@needs_node
@pytest.mark.parametrize("value", ["pf-archiver", "PF-ARCHIVER", " pf-archiver "])
def test_exact_pf_archiver_keeps_non_collection_roles(value):
    assert _roles(value) == ["answer", "digest-publish", "health"]


def test_tybot_mode_blocks_the_standalone_slack_and_local_ask_entrypoints():
    """TYBot 연동은 Node Hermes를 같이 띄우는 구성이 아니다."""
    index = (HERMES / "src" / "index.js").read_text(encoding="utf-8")
    launcher = (HERMES / "scripts" / "run-server.js").read_text(encoding="utf-8")
    package = json.loads((HERMES / "package.json").read_text(encoding="utf-8"))
    ask = (HERMES / "scripts" / "run-ask.js").read_text(encoding="utf-8")
    claude = (HERMES / "src" / "claude.js").read_text(encoding="utf-8")

    assert index.index("assertOwnsRole(ROLES.ANSWER") < index.index("const env = requireEnv(")
    assert package["scripts"]["start"] == "node scripts/run-server.js"
    assert launcher.index("assertOwnsRole(ROLES.ANSWER") < launcher.index("import('../src/index.js')")
    assert ask.split("async function main()", 1)[1].lstrip().startswith("{")
    assert "assertOwnsRole(ROLES.ANSWER" in ask.split("async function main()", 1)[1][:500]
    assert "assertOwnsRole(ROLES.ANSWER" in claude.split("function answerQuestion", 1)[1][:300]


@needs_node
def test_tybot_mode_refuses_npm_start_before_loading_config_or_slack():
    """연동 호스트에 PF 설정·node_modules가 없어도 정확한 사유와 코드 2로 막힌다."""
    got = subprocess.run(
        [NODE, "scripts/run-server.js"],
        cwd=HERMES,
        env={**os.environ, "HERMES_MODE": "tybot"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    assert got.returncode == 2
    assert "독립 Node 런타임을 실행하지 않습니다" in got.stderr
    assert "config.json" not in got.stderr


@needs_node
@pytest.mark.parametrize("value", ["tybo", "tybot2", "ty bot", "master", "0", "false"])
def test_any_other_value_fails_to_start(value):
    """설정 오류는 **조용히 넘어가지 않는다.**"""
    env = dict(os.environ)
    env["HERMES_MODE"] = value
    got = subprocess.run(
        [NODE, "--input-type=module", "-e",
         "import {mode} from './src/mode.js'; console.log(mode());"],
        cwd=HERMES, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )

    assert got.returncode != 0, f"{value!r} 가 조용히 통과했다: {got.stdout}"
    assert "HERMES_MODE" in got.stderr
    assert value in got.stderr


@pytest.mark.parametrize("value", ["tybo", "master", "0"])
def test_the_skill_side_fails_on_the_same_values(value):
    """두 곳의 판정이 갈리면 봇은 막혔는데 스킬은 쓰는 상태가 된다."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_hermes_mode", HERMES / ".claude" / "skills" / "_shared" / "mode.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    os.environ["HERMES_MODE"] = value
    try:
        with pytest.raises(Exception) as caught:
            module.mode()
        assert "HERMES_MODE" in str(caught.value)
    finally:
        os.environ.pop("HERMES_MODE", None)


# --- 경계 보강 2. 임시 파일은 고유해야 한다 ----------------------------------
def test_concurrent_writers_are_refused_not_silently_merged(tmp_path):
    """**서로 다른 스냅샷**을 동시에 쓰면 마지막 쓰기가 앞 결정을 통째로 덮는다.

    고유 임시 파일과 원자적 `replace` 는 **깨진 파일**만 막는다. 둘 다 온전한
    파일이고 다만 하나만 남는다 — 그게 이 시험이 보는 유실이다.

    같은 스냅샷을 여러 번 쓰면 이 문제가 **안 보인다.** 전에 그렇게 써 두어서
    시험이 통과하고 있었다.
    """
    import threading

    path = tmp_path / "decisions.json"
    # 스냅샷마다 좌표가 다르다. 섞이거나 덮이면 어느 쪽인지 바로 드러난다.
    snapshots = {
        f"only-{n}": [_candidate_row(evidence_locator=f"snap-{n}.md:{n}")]
        for n in range(4)
    }
    refused: list[Exception] = []
    other: list[Exception] = []

    def run(rows):
        for _ in range(8):
            try:
                rec.write_export(path, rows)
            except rec.ConcurrentExportRefused as exc:
                refused.append(exc)
            except Exception as exc:
                other.append(exc)

    threads = [threading.Thread(target=run, args=(rows,)) for rows in snapshots.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not other, f"예상 못 한 실패: {other}"

    # 남은 파일은 **어느 한 스냅샷의 온전한 한 벌**이어야 한다 — 섞이면 안 된다.
    loaded = rec.load_decisions(path)
    assert loaded is not None, "읽을 수 없는 파일이 남았다"
    locators = {d.evidence_locator for d in loaded}
    assert len(locators) == 1, f"두 스냅샷이 섞였다: {sorted(locators)}"
    assert not list(tmp_path.glob("*.tmp*")), "임시 파일이 남았다"


def test_a_second_writer_is_told_why_it_was_refused(tmp_path):
    """거부는 **사람이 읽을 수 있어야** 한다 — 「출처가 둘이면 파일을 따로」."""
    from tybot.lock import FileLock

    path = tmp_path / "decisions.json"
    held = FileLock(path.with_name(f".{path.name}.writer.lock"), label="시험")
    held.acquire()
    try:
        with pytest.raises(rec.ConcurrentExportRefused) as caught:
            rec.write_export(path, [_candidate_row()], timeout=0.1)
    finally:
        held.release()

    assert "한 출처의 스냅샷" in str(caught.value)
    assert caught.value.code == "export_writer_conflict"


def test_a_single_writer_still_writes_normally(tmp_path):
    """계약을 좁혔다고 평범한 한 번 쓰기가 막히면 안 된다."""
    path = tmp_path / "decisions.json"

    assert rec.write_export(path, [_candidate_row()]) == 1
    assert rec.write_export(path, [_candidate_row(state="deferred")]) == 1
    assert len(rec.load_decisions(path)) == 1


def test_a_failed_write_leaves_no_leftover(tmp_path, monkeypatch):
    """실패해도 찌꺼기를 남기지 않는다. 남으면 다음 사람이 그것을 기록으로 읽는다."""
    path = tmp_path / "decisions.json"

    def boom(*a, **kw):
        raise OSError("디스크 가득 참")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        rec.write_export(path, [_candidate_row()])

    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp*")), "실패 뒤 임시 파일이 남았다"


# --- 경계 보강 3. 승인과 거절이 함께 있으면 생략하지 않는다 ------------------
def test_approved_and_rejected_on_the_same_evidence_is_a_conflict():
    """둘이 함께 있으면 **무엇이 확정인지 아무도 모른다.**

    먼저 온 것을 쓰거나 나중 것을 쓰면 「생략했는데 왜 그렇게 정해졌는지」 를
    사람이 설명할 수 없다. 묻는 쪽으로 틀린다.
    """
    decisions = [_decision(state="approved"), _decision(state="rejected")]

    skip, reason = rec.decide(_candidate(), decisions)

    assert skip is False
    assert reason == rec.CONFLICT


def test_the_same_state_twice_is_not_a_conflict():
    """같은 결정이 두 번 기록된 것은 모순이 아니다 — 두 인터페이스가 같은 답을 냈다."""
    decisions = [_decision(state="approved"), _decision(state="approved", decided_by="U2")]

    assert rec.decide(_candidate(), decisions) == (True, rec.SKIP)


def test_a_conflict_elsewhere_does_not_block_this_candidate():
    """다른 좌표의 모순이 이 후보를 막으면, 한 건의 모순이 그날 전체를 멈춘다."""
    decisions = [
        _decision(state="approved"),
        _decision(state="approved", evidence_locator="other.md:9"),
        _decision(state="rejected", evidence_locator="other.md:9"),
    ]

    assert rec.decide(_candidate(), decisions) == (True, rec.SKIP)
