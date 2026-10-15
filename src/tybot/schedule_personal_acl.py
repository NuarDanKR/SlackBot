"""Direct-user calendar rights, separate from organization and archive ACLs."""
from __future__ import annotations

import os
from datetime import datetime, timedelta


def enabled() -> bool:
    return os.getenv("SCHEDULE_PERSONAL_DM_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# Shared by planner, cancellation, settings and the last check before sending.
# A known direct deny blocks both grant paths. Parent organizations are not inferred.
ACCESS_SQL = """
    NOT EXISTS (
        SELECT 1 FROM schedule_folder_person denied
        WHERE denied.source_folder_id=f.source_folder_id
          AND denied.emp_no=r.emp_no AND denied.is_present AND NOT denied.read_allowed
    )
    AND (
        (NOT f.dm_only AND EXISTS (
            SELECT 1 FROM schedule_folder_org fo
            WHERE fo.source_folder_id=f.source_folder_id
              AND fo.org_code=r.org_code AND fo.enabled
        ))
        OR EXISTS (
            SELECT 1 FROM schedule_folder_person personal
            JOIN schedule_personal_acl_state fresh ON fresh.source='oracle-ur-v1'
            WHERE personal.source_folder_id=f.source_folder_id
              AND personal.emp_no=r.emp_no AND personal.is_present AND personal.read_allowed
              AND fresh.generated_at >= now()-interval '15 minutes'
              AND fresh.generated_at <= now()+interval '1 minute'
              AND personal.source_generated_at=fresh.generated_at
        )
    )
"""


def validate(manifest: dict) -> list[str]:
    if "personal_acl_complete" not in manifest:
        if any("emp_no" in row for row in manifest.get("folders", []) if isinstance(row, dict)):
            return ["personal ACL rows require a complete versioned snapshot"]
        return []
    if (manifest.get("personal_acl_complete") is not True
            or type(manifest.get("personal_acl_version")) is not int
            or manifest.get("personal_acl_version") != 1):
        return ["personal ACL snapshot must be complete and version 1"]
    rows = manifest.get("folders")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return ["personal ACL folder rows are malformed"]
    seen = set()
    known = set(manifest.get("source_folders") or [])
    for row in rows:
        if "emp_no" not in row:
            continue
        folder = row.get("folder_id")
        emp = row.get("emp_no")
        if (type(folder) is not int or folder <= 0 or folder not in known
                or not isinstance(emp, str) or not emp.strip() or len(emp) > 64
                or row.get("org_code") or type(row.get("read_allowed")) is not bool):
            return ["personal ACL identity, folder or permission is invalid"]
        if (folder, emp) in seen:
            return ["personal ACL has duplicate user/folder entries"]
        seen.add((folder, emp))
    return []


def sync(cur, manifest: dict, generated: datetime, *, now: datetime) -> bool:
    """Apply only a complete, fresh, monotonically newer ACL snapshot in the caller txn."""
    if "personal_acl_complete" not in manifest:
        return True
    problems = validate(manifest)
    if problems:
        raise ValueError(problems[0])
    if not isinstance(generated, datetime) or generated.tzinfo is None:
        raise ValueError("personal ACL snapshot timestamp is missing or naive")
    if generated > now + timedelta(minutes=1):
        raise ValueError("personal ACL snapshot is future dated")
    if generated < now - timedelta(minutes=15):
        return False
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('schedule-personal-acl'))")
    cur.execute("SELECT generated_at FROM schedule_personal_acl_state WHERE source='oracle-ur-v1' FOR UPDATE")
    row = cur.fetchone()
    previous = (row.get("generated_at") if isinstance(row, dict) else row[0]) if row else None
    if previous is not None and generated <= previous:
        return False
    cur.execute("UPDATE schedule_folder_person SET is_present=false WHERE is_present")
    rows = manifest["folders"]
    department_ids = {row["folder_id"] for row in rows if row.get("org_code")}
    personal_ids = {row["folder_id"] for row in rows if "emp_no" in row}
    for row in rows:
        if "emp_no" not in row:
            continue
        cur.execute(
            """INSERT INTO schedule_folder_person
                (source_folder_id,emp_no,read_allowed,is_present,source_generated_at)
            SELECT %s,%s,%s,true,%s WHERE EXISTS (SELECT 1 FROM employee WHERE emp_no=%s)
            ON CONFLICT (source_folder_id,emp_no) DO UPDATE
            SET read_allowed=excluded.read_allowed,is_present=true,
                source_generated_at=excluded.source_generated_at""",
            (row["folder_id"], row["emp_no"], row["read_allowed"], generated, row["emp_no"]),
        )
    cur.execute(
        """UPDATE schedule_folder f SET dm_only=true
        WHERE (f.source_folder_id=ANY(%s) OR EXISTS (
            SELECT 1 FROM schedule_folder_person old WHERE old.source_folder_id=f.source_folder_id
        )) AND NOT f.source_folder_id=ANY(%s)""",
        (sorted(personal_ids), sorted(department_ids)),
    )
    cur.execute(
        "UPDATE schedule_folder SET dm_only=false WHERE source_folder_id=ANY(%s)",
        (sorted(department_ids),),
    )
    cur.execute(
        """INSERT INTO schedule_personal_acl_state (source,generated_at)
        VALUES ('oracle-ur-v1',%s) ON CONFLICT (source) DO UPDATE
        SET generated_at=excluded.generated_at,applied_at=now()""", (generated,),
    )
    return True
