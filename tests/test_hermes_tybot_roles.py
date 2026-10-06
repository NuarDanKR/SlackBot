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
    assert _roles(None) == ["answer", "digest", "health", "ingest"]


@needs_node
def test_tybot_mode_leaves_only_answering():
    """연동 모드에서 **발송은 하나도 없다.** 요약도 위생 점검도 TYBot 이 맡는다."""
    assert _roles("tybot") == ["answer"]


@needs_node
@needs_deps
@pytest.mark.parametrize(
    ("script", "role"), [("run-digest.js", "digest"), ("run-health.js", "health")]
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
    for role in ("ROLES.DIGEST", "ROLES.HEALTH", "ROLES.INGEST"):
        assert role in start, f"{role} 이 역할 기준으로 걸리지 않았다"


@pytest.mark.parametrize(
    ("module", "fn", "role"),
    [("src/digest.js", "runDigest", "DIGEST"),
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


def test_the_filter_reports_why_each_candidate_stayed():
    """「오늘 후보가 없다」 와 「전부 생략됐다」 는 사람이 할 일이 완전히 다르다."""
    rows = [_candidate(), _candidate(evidence_locator="2026-10-02.md:7")]

    kept, reasons = rec.filter_candidates(rows, [_decision()])

    assert len(kept) == 1
    assert reasons[rec.SKIP] == 1
    assert reasons[rec.NO_MATCH] == 1
