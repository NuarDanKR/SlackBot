"""Supervisor 운영 콘솔의 **판단**.

결정: 2026-09-29 작업지시서 §3.2·§3.3·§4 · §7-6.

가짜 저장소 하나로 규칙을 전부 본다. 커서를 흉내 내지 않는다 — 커서 흉내는 진짜
DB 와 달라지고, 그러면 「SQL 이 나갔나」 를 보느라 「규칙이 맞나」 를 안 보게 된다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tybot.console import supervisor_admin
from tybot.console.archiving_admin import AdminRefused
from tybot.console.supervisor_admin import Actor

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
WHO = Actor("dan@taeyoung.com", "파일럿 소급 검증")


class FakeRepo:
    """표 세 개를 dict 로 든다. 판단은 하나도 하지 않는다."""

    def __init__(self, runtime: dict | None = None, jobs: list[dict] | None = None,
                 cursors: list[dict] | None = None) -> None:
        self._runtime = runtime
        self._jobs = list(jobs or [])
        self._cursors = list(cursors or [])
        self.audit: list[dict] = []
        self.next_id = 100

    # -- SupervisorRepo -------------------------------------------------
    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def _tx():
            yield self

        return _tx()

    def runtime(self, workspace: str):
        return dict(self._runtime) if self._runtime else None

    def save_desired(self, workspace, mode, generation, actor):
        self._runtime = {
            **(self._runtime or {"observed_state": "stopped", "heartbeat_at": None}),
            "workspace": workspace, "desired_mode": mode,
            "generation": generation, "updated_by": actor,
        }

    def cursors(self, workspace):
        return list(self._cursors)

    def jobs(self, workspace, limit=20):
        return [job for job in self._jobs if job["workspace"] == workspace][:limit]

    def job(self, job_id):
        return next((job for job in self._jobs if job["id"] == job_id), None)

    def create_job(self, row):
        self.next_id += 1
        job = {
            "id": self.next_id, "state": "queued", "found_count": 0,
            "written_count": 0, "duplicate_count": 0, "refused_count": 0,
            "failed_count": 0, "error_code": "", "started_at": None,
            "finished_at": None, "created_at": NOW, **row,
        }
        self._jobs.insert(0, job)
        return job

    def cancel_job(self, job_id):
        job = self.job(job_id)
        if job is None or job["state"] != "queued":
            return 0
        job["state"] = "cancelled"
        return 1

    # -- 감사 저장소 -----------------------------------------------------
    def add_audit(self, row):
        self.audit.append(row)


def _runtime(**over) -> dict:
    row = {
        "workspace": "tyit", "desired_mode": "shadow", "observed_state": "running",
        "generation": 3, "heartbeat_at": NOW - timedelta(seconds=10),
        "last_event_at": NOW - timedelta(seconds=30),
        "last_write_at": NOW - timedelta(seconds=31),
        "error_code": "", "error_note": "", "updated_at": NOW, "updated_by": "dan",
    }
    row.update(over)
    return row


def _job(**over) -> dict:
    job = {
        "id": 1, "workspace": "tyit", "channel_id": "C1",
        "from_ts": "1000.000100", "to_ts": "2000.000100", "dry_run": True,
        "state": "succeeded", "requested_by": "dan", "reason": "검증",
        "found_count": 5, "written_count": 0, "duplicate_count": 0,
        "refused_count": 0, "failed_count": 0, "error_code": "",
        "started_at": NOW, "finished_at": NOW, "created_at": NOW,
    }
    job.update(over)
    return job


# --- 무엇이 고장났나 -------------------------------------------------------------

def test_a_lost_heartbeat_is_not_healthy():
    """프로세스가 죽어도 마지막 관측 상태는 running 으로 남는다. 그 값만 보면 정상이다."""
    state = supervisor_admin.health(
        _runtime(heartbeat_at=NOW - timedelta(hours=1)), now=NOW,
    )

    assert state["state"] == "degraded"
    assert state["heartbeatStale"] is True


def test_a_worker_that_never_reported_is_not_healthy():
    state = supervisor_admin.health(_runtime(heartbeat_at=None), now=NOW)

    assert state["state"] == "degraded"


def test_a_fresh_heartbeat_is_healthy():
    assert supervisor_admin.health(_runtime(), now=NOW)["state"] == "ok"


def test_stopping_is_not_an_error():
    """껐는데 아직 도는 중은 과도 상태다. 오류로 만들면 사람이 고칠 것을 찾는다."""
    state = supervisor_admin.health(
        _runtime(desired_mode="off", observed_state="running"), now=NOW,
    )

    assert state["state"] == "stopping"


def test_off_and_stopped_is_off_not_error():
    state = supervisor_admin.health(
        _runtime(desired_mode="off", observed_state="stopped", heartbeat_at=None),
        now=NOW,
    )

    assert state["state"] == "off"
    assert state["heartbeatStale"] is False


def test_an_error_state_stays_an_error():
    state = supervisor_admin.health(
        _runtime(observed_state="error", error_code="SlackApiError"), now=NOW,
    )

    assert state["state"] == "error"


def test_a_workspace_never_seen_is_unknown():
    assert supervisor_admin.health(None, now=NOW)["state"] == "unknown"


# --- 읽기 모델 -------------------------------------------------------------------

def test_the_detail_says_saving_is_not_applying():
    """「적용됐다」 로 보이면 사람은 확인하지 않고 떠난다."""
    detail = supervisor_admin.runtime_detail("tyit", FakeRepo(_runtime()), now=NOW)

    assert "supervisor" in detail["runtimeEffect"]
    assert detail["desiredMode"] == "shadow"
    assert detail["health"]["state"] == "ok"


def test_live_is_not_selectable_in_the_pilot():
    """화면이 버튼을 회색으로 만들 근거. 눌러 보고 거절당하는 것보다 낫다."""
    detail = supervisor_admin.runtime_detail("tyit", FakeRepo(_runtime()), now=NOW)

    assert detail["selectableModes"] == ["off", "shadow"]
    assert detail["gatedModes"] == ["live"]


def test_the_detail_names_the_workspace_place_under_the_common_root():
    """공통 root 로 모은 뒤 이 워크스페이스 자료가 어디 있나(§3.3).

    root 앞에 워크스페이스를 다시 붙이지 않는다. 그 중복이 파일럿에서 실제로 났다.
    """
    detail = supervisor_admin.runtime_detail("tyit", FakeRepo(_runtime()), now=NOW)

    assert detail["archiveRelativePath"] == "workspaces/tyit"


def test_a_workspace_without_a_row_reads_as_off():
    """행이 없다고 화면이 비면, 사람은 무엇을 눌러야 할지 모른다."""
    detail = supervisor_admin.runtime_detail("tyit", FakeRepo(), now=NOW)

    assert detail["desiredMode"] == "off"
    assert detail["generation"] == 0
    assert detail["health"]["state"] == "unknown"


def test_the_detail_carries_cursors_and_jobs():
    repo = FakeRepo(_runtime(), jobs=[_job()], cursors=[{"channel_id": "C1"}])

    detail = supervisor_admin.runtime_detail("tyit", repo, now=NOW)

    assert [row["channel_id"] for row in detail["cursors"]] == ["C1"]
    assert [row["id"] for row in detail["jobs"]] == [1]


# --- 희망 상태 -------------------------------------------------------------------

def test_changing_the_mode_bumps_the_generation():
    """세대가 안 오르면 supervisor 는 「바뀐 것이 없다」 고 보고 옛 설정으로 돈다."""
    repo = FakeRepo(_runtime(desired_mode="off", generation=3))

    detail = supervisor_admin.set_desired_mode("tyit", "shadow", WHO, repo,
                                               audit_repo=repo)

    assert detail["desiredMode"] == "shadow"
    assert detail["generation"] == 4


def test_pressing_the_same_mode_does_not_restart_the_worker():
    repo = FakeRepo(_runtime(desired_mode="shadow", generation=7))

    detail = supervisor_admin.set_desired_mode("tyit", "shadow", WHO, repo,
                                               audit_repo=repo)

    assert detail["generation"] == 7


def test_live_is_refused():
    """파일럿 범위 밖이다. 여는 것은 별도 결정이고 기본값이 되면 안 된다."""
    repo = FakeRepo(_runtime())

    with pytest.raises(AdminRefused, match="파일럿 범위 밖"):
        supervisor_admin.set_desired_mode("tyit", "live", WHO, repo, audit_repo=repo)


def test_a_mode_change_is_audited():
    repo = FakeRepo(_runtime(desired_mode="off"))

    supervisor_admin.set_desired_mode("tyit", "shadow", WHO, repo, audit_repo=repo)

    assert repo.audit[0]["old_value"] == "off"
    assert repo.audit[0]["new_value"] == "shadow"
    assert repo.audit[0]["reason"] == "파일럿 소급 검증"


def test_a_change_without_a_reason_is_refused():
    with pytest.raises(AdminRefused, match="사유"):
        Actor("dan@taeyoung.com", "  ")


# --- 소급 요청 -------------------------------------------------------------------

def test_a_dry_run_is_queued_not_run():
    """콘솔은 Slack 을 읽지 않는다. 만드는 것은 요청이다."""
    repo = FakeRepo(_runtime())

    result = supervisor_admin.request_backfill(
        "tyit", WHO, repo, channel_id="C1", from_ts="1000.000100",
        to_ts="2000.000100", audit_repo=repo,
    )

    assert result["job"]["state"] == "queued"
    assert result["job"]["dry_run"] is True
    assert "집어가면" in result["runtimeEffect"]


def test_a_real_run_needs_a_preview_of_the_same_range():
    """세어 보지 않고 걸면 중간에 멈춘 것과 다 된 것을 구분할 수 없다."""
    repo = FakeRepo(_runtime())

    with pytest.raises(AdminRefused, match="미리보기가 먼저"):
        supervisor_admin.request_backfill(
            "tyit", WHO, repo, channel_id="C1", from_ts="1000.000100",
            to_ts="2000.000100", dry_run=False, audit_repo=repo,
        )


def test_a_preview_of_another_range_does_not_authorise_this_one():
    """이 시험이 없으면 아무 미리보기 하나가 모든 범위를 열어 준다."""
    repo = FakeRepo(_runtime(), jobs=[_job(to_ts="1500.000100")])

    with pytest.raises(AdminRefused, match="미리보기가 먼저"):
        supervisor_admin.request_backfill(
            "tyit", WHO, repo, channel_id="C1", from_ts="1000.000100",
            to_ts="2000.000100", dry_run=False, audit_repo=repo,
        )


def test_a_real_run_after_its_preview_is_queued():
    repo = FakeRepo(_runtime(), jobs=[_job()])

    result = supervisor_admin.request_backfill(
        "tyit", WHO, repo, channel_id="C1", from_ts="1000.000100",
        to_ts="2000.000100", dry_run=False, audit_repo=repo,
    )

    assert result["job"]["dry_run"] is False
    assert result["job"]["state"] == "queued"


def test_a_failed_preview_does_not_authorise_a_real_run():
    repo = FakeRepo(_runtime(), jobs=[_job(state="failed")])

    with pytest.raises(AdminRefused, match="미리보기가 먼저"):
        supervisor_admin.request_backfill(
            "tyit", WHO, repo, channel_id="C1", from_ts="1000.000100",
            to_ts="2000.000100", dry_run=False, audit_repo=repo,
        )


def test_an_unfinished_job_blocks_a_new_one():
    """두 작업이 같은 구간을 읽으면 cursor 가 서로를 덮는다."""
    repo = FakeRepo(_runtime(), jobs=[_job(state="running")])

    with pytest.raises(AdminRefused, match="끝나지 않은"):
        supervisor_admin.request_backfill(
            "tyit", WHO, repo, channel_id="C1", audit_repo=repo,
        )


def test_a_job_on_another_channel_does_not_block():
    repo = FakeRepo(_runtime(), jobs=[_job(state="running", channel_id="C9")])

    result = supervisor_admin.request_backfill(
        "tyit", WHO, repo, channel_id="C1", audit_repo=repo,
    )

    assert result["job"]["state"] == "queued"


def test_a_whole_workspace_job_blocks_every_channel():
    """채널을 비운 작업은 전부를 훑는다. 그 위에 채널 하나를 겹쳐 걸 수 없다."""
    repo = FakeRepo(_runtime(), jobs=[_job(state="queued", channel_id="")])

    with pytest.raises(AdminRefused, match="끝나지 않은"):
        supervisor_admin.request_backfill(
            "tyit", WHO, repo, channel_id="C1", audit_repo=repo,
        )


@pytest.mark.parametrize("bad", ["2026-09-01", "1000", "어제", "1000.0001000000"])
def test_a_range_that_is_not_a_slack_ts_is_refused(bad):
    """사람이 적은 날짜를 그대로 넘기면 Slack 이 0 으로 읽고 채널 전체를 긁는다."""
    with pytest.raises(AdminRefused, match="Slack ts"):
        supervisor_admin.request_backfill(
            "tyit", WHO, FakeRepo(_runtime()), from_ts=bad, audit_repo=None,
        )


def test_a_backwards_range_is_refused():
    with pytest.raises(AdminRefused, match="뒤여야"):
        supervisor_admin.request_backfill(
            "tyit", WHO, FakeRepo(_runtime()), from_ts="2000.000100",
            to_ts="1000.000100",
        )


def test_an_open_range_is_allowed():
    """채널 전체를 처음부터 긁는 것은 유효한 요청이다. 범위를 비운 것이 곧 그 뜻이다."""
    repo = FakeRepo(_runtime())

    result = supervisor_admin.request_backfill("tyit", WHO, repo, audit_repo=repo)

    assert result["job"]["from_ts"] == ""


def test_a_backfill_request_is_audited():
    repo = FakeRepo(_runtime())

    supervisor_admin.request_backfill(
        "tyit", WHO, repo, channel_id="C1", from_ts="1000.000100",
        to_ts="2000.000100", audit_repo=repo,
    )

    assert repo.audit[0]["field"] == "backfill_dry_run"
    assert repo.audit[0]["channel_id"] == "C1"


# --- 취소 -----------------------------------------------------------------------

def test_a_queued_job_can_be_taken_back():
    repo = FakeRepo(_runtime(), jobs=[_job(state="queued")])

    supervisor_admin.cancel_backfill("tyit", 1, WHO, repo, audit_repo=repo)

    assert repo.job(1)["state"] == "cancelled"


def test_a_running_job_is_not_cancelled_by_the_console():
    """실제로는 계속 쓰고 있는데 화면만 멈춘 것으로 보이면, 사람은 한 번 더 건다."""
    repo = FakeRepo(_runtime(), jobs=[_job(state="running")])

    with pytest.raises(AdminRefused, match="이미 시작했거나"):
        supervisor_admin.cancel_backfill("tyit", 1, WHO, repo, audit_repo=repo)
    assert repo.job(1)["state"] == "running"


def test_another_workspaces_job_is_not_cancellable():
    """워크스페이스 경계를 넘는 조작은 콘솔에서도 막는다(절대 원칙 4)."""
    repo = FakeRepo(_runtime(), jobs=[_job(workspace="mgmt", state="queued")])

    with pytest.raises(AdminRefused, match="이 워크스페이스의 작업이 아닙니다"):
        supervisor_admin.cancel_backfill("tyit", 1, WHO, repo, audit_repo=repo)
    assert repo.job(1)["state"] == "queued"
