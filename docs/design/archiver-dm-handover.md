# Archiver DM handover

Decision: 2026-10-01. Scope is a one-to-one DM between a person and TYBot Master.
Human-to-human DMs, group DMs (`mpim`), bot replies, and system messages are not
archive input. This extends the personal-workspace rules in `dm-workspace.md`;
it does not widen who may read a DM.

## Ingress boundary

The Archiver is a separate Slack app. Its `message.im` subscription would receive
DMs addressed to the Archiver app, not DMs addressed to TYBot Master. Adding
`im:history` or `message.im` to `archiving-app-manifest.yaml` does not migrate
Master DMs.

The Archiver app manifest disables its own Messages tab and requests neither
`message.im` nor `im:history`. Apply that manifest setting to each installed
Slack app separately; changing this repository file alone does not update an
existing Slack app. Keep Master app DM delivery enabled for replies.

Master must remain the Slack event ingress and answer bot for its own DMs. After
identifying a human one-to-one DM, Master hands the event to an Archiver-owned,
durable internal inbox. Master must not write that DM to the old archive after
the handover is enabled. The inbox must preserve the event until the Archiver
confirms a durable raw write or a recorded policy refusal. Delivery and retries
are idempotent by workspace, DM ID, message timestamp, and revision identity.
Do not log message bodies, attachment bytes, or tokens in the inbox audit trail.
Protect any queued payload as private DM data; a coordinate-only queue is not
enough if a message is edited or deleted before the worker fetches it.

`archive/dm_inbox.py` provides an encrypted, idempotent event inbox. When
mirroring an attachment event, Master downloads each original with its own
token and seals the bytes in `DmFileVault` before queueing a reference in the
event envelope. A failed download prevents that event from entering the inbox;
it does not interrupt the existing Master archive write or reply. The consumer
verifies every vault entry against the event coordinate and file ID, copies the
original into its private `objects/` as ciphertext, writes a raw reference
marked "extraction pending", and ACKs only after those writes are synced.
The original is retained, but its content is not yet converted or searchable.
Orphaned vault
entries after a failed envelope write need a reviewed retention policy; do not
remove them solely because no current envelope points at them.
The intended private
state root is `/var/lib/tybot/state/dm-handoff`; the Archiver unit allows only
that additional path to become writable once it exists.

The Master producer is now connected as an **opt-in mirror only** using
`ARCHIVER_DM_MIRROR_ENABLED=1` and an absolute `ARCHIVER_DM_HANDOFF_DIR`.
The default is off. It screens message text before queueing and preserves the
existing Master DM archive write. It runs after the existing reply so a file
download does not delay that reply, but the event is not durably queued if
Master exits between reply and mirror. This ordering is suitable for a mirror,
not the final cutover. An independent Archiver consumer is also
opt-in (`ARCHIVER_DM_CONSUME_ENABLED=1`) and requires `per-channel-v1`, the same
absolute inbox directory, and the workspace secret key. It writes DM text and
attachment references to `<root>/<workspace>/dm/<user-id>/archive/raw/`.
File-share envelopes without original bytes remain pending. Do not enable either
switch on a server until retention, historical replay, reply latency, and
operational review are complete.

The current consumer checks the encrypted envelope's workspace, Master bot ID,
`channel_type == "im"`, and sender identity before writing. Before queueing,
Master also calls `conversations.info` with its own token and verifies the DM
ID, type, and other user. An explicit nonmember or non-two-member response is
rejected. This requires `im:read` on the **installed Master app**: the manifest
change alone does not grant it. Missing scope, API failure, or incomplete
identity fails only the mirror; Master continues its existing DM archive write
and reply. Bot/app/system messages and `mpim` fail closed. The
Archiver uses the shared raw writer for text, including PII screening. It does
not yet convert DM attachments, expose their content to the reader, or write
message revisions. No Archiver app token reads Master DMs.

## Storage and reading

New DM data belongs under a private per-user namespace in the new archive,
separate from channel enumeration and cross-workspace search. The text path is
`<root>/<workspace>/dm/<user-id>/archive/raw/`; encrypted originals are under
`<root>/<workspace>/dm/<user-id>/objects/<workspace>/files/`. Preserve `dm_user`, DM ID,
and source message timestamp. A DM is visible only to that same Slack user in
that workspace and only in a DM request; admin/root role alone grants no access.
Hermes and channel answers never receive private DM evidence by default.

Historical backfill must use the Master app's authorized DM history, with the
Master side feeding the same inbox. It can recover only history still visible
to that app, not deleted messages or earlier edit versions. Existing Master DM
files remain an immutable recovery copy until a separate retention decision;
they are not silently merged into the new canonical archive.

## Cutover gate

Do not disable `pilot._ingest_dm` yet. First provide the durable inbox,
Archiver DM writer, per-user reader/ACL, historical backfill, and an ACK-based
handover. Confirm that a DM still gets a timely Master reply when the Archiver
is unavailable, that retries do not duplicate raw lines, and that no other
user's DM can be read. Only then replace Master's archive write with the
inbox handoff. Channel handover and DM handover are independent switches.
