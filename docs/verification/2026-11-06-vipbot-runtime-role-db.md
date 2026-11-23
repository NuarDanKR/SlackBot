# VIPBot runtime role DB verification

> **Retired standalone-runtime record (2026-11-12).** VIPBot now runs only as
> a branch of `tybot.service`; the steps below are historical, not deployment instructions.

Date: 2026-11-06

## Result

The VIPBot runtime-role contract passed against a disposable PostgreSQL 18
cluster:

```text
65 passed in 0.57s
```

The test covered the executable-only SECURITY DEFINER boundary, direct table
denials, PUBLIC denial, token isolation, disabled and unverified connections,
and privilege stability after schema reapplication.

## Isolation

- The test cluster listened only on a temporary loopback port.
- The database name contained the required `schema_test` marker.
- No operational database was used.
- The production DSN and credentials were not recorded in this document.

After pytest completed, the test cluster was queried for `tybot_vipbot`,
`tybot_vipbot_runtime`, and `tybot_vipbot_definer`. The query returned no
rows.

The empty result confirms fixture cleanup. The temporary PostgreSQL server was
then stopped, its stopped state was verified with `pg_ctl status`, and its data
directory was removed.

## Fixes found by the real database

The first runs exposed stale test fixtures rather than product SQL failures:

1. `workspace.name` had been replaced by the current `label`, `archive_path`,
   and `created_by` columns.
2. `bot_connection_secret` now requires `mask` and `updated_by`.
3. PUBLIC is an ACL grantee, not a row in `pg_roles`; the test now inspects ACL
   grantee `0` directly.
4. `enabled` connections cannot have `identity_ok=false`; the unverified test
   now uses the valid `draft` state instead of weakening the database check.

These corrections are in commits `5223d2e`, `6fbf890`, and `e6d8a97`.
