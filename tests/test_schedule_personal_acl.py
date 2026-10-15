from datetime import UTC, datetime, timedelta

import pytest

from tybot import schedule_dm as dm
from tybot import schedule_personal_acl as acl

NOW = datetime(2026, 10, 15, 10, tzinfo=UTC)


def manifest(rows=None):
    return {
        "personal_acl_complete": True, "personal_acl_version": 1,
        "source_folders": [594, 651, 2423],
        "folders": rows if rows is not None else [
            {"folder_id": 594, "folder_name": "department", "org_code": "ORG1"},
            {"folder_id": 651, "folder_name": "shared", "org_code": "ORG2"},
            {"folder_id": 651, "emp_no": "E1", "read_allowed": True},
            {"folder_id": 2423, "emp_no": "E1", "read_allowed": True},
        ],
    }


class Cursor:
    def __init__(self, previous=None):
        self.previous = previous
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return {"generated_at": self.previous} if self.previous else None


def test_default_off(monkeypatch):
    monkeypatch.delenv("SCHEDULE_PERSONAL_DM_ENABLED", raising=False)
    assert not acl.enabled()


def test_complete_personal_snapshot_validates():
    assert acl.validate(manifest()) == []
    assert acl.validate({"folders": []}) == []


@pytest.mark.parametrize("bad", [
    {"personal_acl_complete": False}, {"personal_acl_version": 2},
    {"personal_acl_version": True}, {"folders": "not-a-list"},
    {"folders": [{"folder_id": 2423, "emp_no": "E1", "read_allowed": "true"}]},
    {"folders": [{"folder_id": 2423, "emp_no": "E1", "org_code": "ORG1", "read_allowed": True}]},
    {"folders": [{"folder_id": 9999, "emp_no": "E1", "read_allowed": True}]},
])
def test_ambiguous_or_partial_acl_is_refused(bad):
    data = manifest()
    data.update(bad)
    assert acl.validate(data)


def test_unversioned_personal_rows_are_not_treated_as_legacy():
    assert acl.validate({"folders": [{"emp_no": "E1"}]})


def test_duplicate_grant_deny_must_be_resolved_at_source():
    data = manifest()
    data["folders"].append({"folder_id": 651, "emp_no": "E1", "read_allowed": False})
    assert acl.validate(data)


def test_full_sync_revokes_absent_rights_and_keeps_personal_folders_dm_only():
    cur = Cursor()
    assert acl.sync(cur, manifest(), NOW, now=NOW)
    sql = " ".join(q for q, _ in cur.calls)
    assert "is_present=false" in sql
    assert "pg_advisory_xact_lock" in sql
    only = next(params for q, params in cur.calls if "dm_only=true" in q)
    assert only == ([651, 2423], [594, 651])
    assert "insert into schedule_folder_org" not in sql.lower()
    assert "update employee" not in sql.lower()


def test_empty_complete_personal_list_revokes_without_inventing_department_grants():
    cur = Cursor()
    assert acl.sync(cur, manifest([]), NOW, now=NOW)
    assert any("is_present=false" in q for q, _ in cur.calls)
    assert not any("INSERT INTO schedule_folder_person" in q for q, _ in cur.calls)


def test_legacy_snapshot_does_not_revoke_or_refresh_personal_rights():
    cur = Cursor()
    assert acl.sync(cur, {}, NOW, now=NOW)
    assert not cur.calls


def test_old_and_out_of_order_snapshots_do_not_revive_rights():
    for previous, generated in [(NOW, NOW), (NOW, NOW-timedelta(minutes=1))]:
        cur = Cursor(previous)
        assert not acl.sync(cur, manifest(), generated, now=NOW)
        assert not any("UPDATE schedule_folder_person" in q for q, _ in cur.calls)
    cur = Cursor()
    assert not acl.sync(cur, manifest(), NOW-timedelta(hours=1), now=NOW)
    assert not cur.calls


def test_future_timestamp_is_not_trusted():
    with pytest.raises(ValueError, match="future"):
        acl.sync(Cursor(), manifest(), NOW+timedelta(hours=1), now=NOW)


def test_one_access_rule_is_used_for_planning_and_cancellation():
    assert acl.ACCESS_SQL in dm.PLAN_PERSONAL_SQL
    assert acl.ACCESS_SQL in dm.CANCEL_PERSONAL_SQL
    assert "denied" in acl.ACCESS_SQL
    assert "15 minutes" in acl.ACCESS_SQL
    assert "personal.emp_no=r.emp_no" in acl.ACCESS_SQL
    assert "parent" not in acl.ACCESS_SQL
    assert "on conflict (source_folder_id, date_id, emp_no, reminder_minutes)" in dm.PLAN_PERSONAL_SQL


def test_dm_only_is_blocked_when_feature_is_off():
    assert "NOT f.dm_only" in dm.PLAN_SQL
    assert "NOT f.dm_only" in dm.CANCEL_INELIGIBLE_SQL
    assert "not f.dm_only" in dm.OCCURRENCE_SQL


def test_personal_access_does_not_expand_manual_public_queries():
    from tybot import schedule

    for sql in (schedule.CHANNEL_SQL, schedule.ORG_SQL, schedule.ORG_LEGACY_SQL):
        assert "NOT f.dm_only" in sql
        assert "schedule_folder_person" not in sql
