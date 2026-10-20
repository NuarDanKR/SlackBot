# Companion app rollout and HWP/Vision split (2026-10-20)

## Current state

- No Slack invitations or historical conversions were run from this development workspace.
- Server connection inventory reported enabled, verified Master and Archiver for
  `archi`, `civil`, `invest`, `mgmt`, and `tyit`. `pilot` has a verified enabled
  Archiver but disabled Master, so its Archiver-joined channels remain in scope.
  `tec` remains excluded. This is connection state, not proof of channel counts
  or installation of either companion app.
- `scripts/inventory_companion_channels.py` is read-only. It inventories the union of channels currently joined by enabled, identity-verified Master and Archiver connections. `tec` is excluded by default. An unreadable source aborts the inventory.
- Run on the server with the console DB/secret environment available:
  `sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env /opt/tybot/.venv/bin/python /opt/tybot/scripts/inventory_companion_channels.py`
- Review the resulting workspace/channel IDs before inviting either independent Hermes or rhwp. A channel invisible to both source bots is outside this inventory.

## External invitation gates

1. Confirm the independent Hermes and rhwp apps are installed in each selected workspace, and record each app's bot user ID there. Do not assume user IDs are shared across workspaces.
2. Approve the external document-processing boundary before inviting rhwp to sensitive channels. The public beta temporarily downloads HWP/HWPX files to its conversion server. Newly invited channels default to automatic detection; choose the intended channel mode explicitly. See [rhwp-slack README](https://github.com/postmelee/rhwp-slack#readme).
3. The inviter must be a member of each private channel and have the relevant Slack invite scope. An inventory does not confer invite rights. Do not treat `not_in_channel` or `no_permission` as a successful invite.
4. Start with one non-sensitive channel, verify both apps' identity and behavior, then expand by workspace with per-channel receipts. Do not use `tec` or inactive/disabled connections.

## Claude assignment: historical HWP recovery

Own a separate, explicit local recovery route for retained human-uploaded HWP/HWPX originals using `rhwp` CLI or another validated local converter. Do **not** rely on rhwp-slack automatically scanning old messages. Do not enable bot-message ingestion globally.

- First run a read-only inventory of retained originals with file ID, workspace, channel, size, conversion state, and existing revision identity. Exclude `tec` and files without verified original bytes or human uploader provenance.
- Dry-run by default. Require workspace/channel/file scope and operator identity for writes. Bound batches and record per-file outcomes; make reruns idempotent.
- Preserve original bytes and failed stubs. Store the derived PDF as a separate generation linked to the original Slack file ID and SHA-256, with converter/version/config identity, PDF hash, page basis, coverage, and quality state. Never label PDF page numbers as original HWP print pages.
- Reuse existing revision, duplicate and authorization checks. Do not publish a partial or unverified conversion as complete searchable evidence. Test failure, rerun, permission loss, and converter-version changes.
- Pilot with `F0C9LHFTHLM` and `F0C9VH39NTW` only after a human checks representative tables and numbers against the HWP originals. The prior LibreOffice-rendered result had partial page coverage.
- Work in HWP converter/recovery and focused tests. Leave `scripts/inventory_companion_channels.py`, Vision policy/ledger, and Slack invitation operations to Codex/operator.

## Codex assignment: Vision path

Keep local text extraction/OCR as a first pass, but route failed, uncertain, or image-heavy PDF pages through the existing B-80 Vision approval and budget ledger. Tables embedded as images need page-level structured extraction, not just OCR text. The model output is a derived candidate, not original evidence. Preserve page coordinates, uncertainty, and source separation; never fill unreadable numbers by inference. No blanket model call for every image and no live external transfer until provider, model, page, budget, and PII approvals are enforced by the worker.

## Acceptance checks

- Inventory includes `Master union Archiver`, excluding `tec`, with no partial result accepted after an API failure.
- App invitation is independently verified per workspace/channel; rhwp historical conversion is an explicit request or local batch, not an automatic consequence of invite.
- Bot-generated PDF never becomes an independent human source. It may be used only as a validated derivative of a retained human original.
- Vision coverage, table cells, amounts, units, and uncertain regions are checked against representative originals before enabling publication.
