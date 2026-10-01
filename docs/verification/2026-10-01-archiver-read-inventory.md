# Archiver read-root inventory

`scripts/inspect_archiver_read_coverage.py` compares the existing TYBot archive
and the new Archiver root without changing either. It reads channel raw files
only; private DM paths are not opened. Output contains counts, never message
bodies, file names, channel names, or tokens.

Run only after both roots are present and readable by the service account:

```bash
sudo -u tybot env -u DATABASE_URL \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/inspect_archiver_read_coverage.py \
  --legacy-root /var/lib/tybot/archive \
  --archiver-root /var/lib/tybot/archiver-shadow/v2
```

`shared_coordinates` means the same `(workspace, channel ID, Slack message ts)`
appears under both roots. `legacy_only_coordinates` and
`archiver_only_coordinates` are counts, not proof of complete history.
`uncoordinated_lines`, `unidentified_channel_documents`, and
`broken_documents` must be reviewed separately. A zero overlap can also mean
missing identifiers or parser failures. Derived attachment lines can share a
message coordinate, so these counts are message coordinates, not raw lines.

This is a preflight inventory, not a reader switch or authorization to stop
Master collection. The runtime still opens one archive root, and its search
index uses paths relative to that root. A second root needs an explicit index
namespace and ACL-preserving read contract before TYBot or Hermes can search
both safely.
