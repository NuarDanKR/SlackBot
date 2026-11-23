from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from tybot.archive import attachment_doc, attachment_reader, legacy_promote, writer
from tybot.archive.store import load_doc

WS = "civil"
CID = "C12345678"
CHANNEL = "원가팀vs원가팀"
ACL = "공사팀vs원가팀"


def _legacy_doc(
    source: Path,
    *,
    workspace: str = WS,
    channel_id: str = CID,
    channel: str = CHANNEL,
    acl: str = ACL,
    visibility: str = "private",
    dm_user: str = "",
    day: str = "2026-11-18",
    lines: tuple[str, ...] = (
        "> [2026-11-18 10:00|1794973200.000100] 홍길동: 지급 조건을 확인했습니다.",
    ),
) -> Path:
    if dm_user:
        path = source / workspace / "dm" / dm_user / "raw" / f"{day}.md"
    else:
        path = (
            source / workspace / "channels" / f"{channel_id}__{channel}"
            / "raw" / f"{day}.md"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = [
        "---",
        f"workspace: {workspace}",
        f'channel: "{channel}"',
        f"channel_id: {channel_id}",
        f"visibility: {visibility}",
        "schema_version: 2",
        f"source_date: {day}",
        "last_ingested: 2026-11-18T10:00+09:00",
        f"acl: [{acl}]" if acl else "acl: []",
        "share_with: []",
    ]
    if dm_user:
        frontmatter.append(f"dm_user: {dm_user}")
    text = "\n".join([*frontmatter, "---", "", "## 원문", "", *lines, ""])
    path.write_text(text, encoding="utf-8")
    return path


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "quarantine" / "workspaces"
    live = tmp_path / "live"
    source.mkdir(parents=True)
    live.mkdir()
    return source, live


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _live_message(live: Path, *, acl: str = ACL) -> None:
    target = live / WS / f"{CID}__{CHANNEL}" / "archive"
    writer.ingest(
        live,
        workspace=WS,
        channel=CHANNEL,
        channel_id=CID,
        visibility="private",
        acl=[acl],
        messages=[
            writer.IncomingMessage(
                ts=datetime.fromisoformat("2026-11-18T10:00:00+09:00"),
                speaker="홍길동",
                text="지급 조건을 확인했습니다.",
                source_ts="1794973200.000100",
            )
        ],
        channel_directory=target,
    )


def _legacy_attachment(path: Path, text: str, *, acl: str = ACL) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        attachment_doc.render(
            attachment_doc.AttachmentDoc(
                workspace=WS,
                channel_id=CID,
                channel=CHANNEL,
                file_id="F12345678",
                name="현황보고.txt",
                revision="rev",
                visibility="private",
                acl=frozenset({acl}),
                text=text,
                conversion_state=attachment_doc.CONVERTED,
            )
        ),
        encoding="utf-8",
    )


def test_dry_run_is_read_only_and_counts_only_new_coordinates(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(
        source,
        lines=(
            "> [2026-11-18 10:00|1794973200.000100] 홍길동: 지급 조건을 확인했습니다.",
            "> [2026-11-18 10:01|1794973260.000200] 홍길동: 새 메시지입니다.",
        ),
    )
    _live_message(live)
    before_source = _hashes(source)
    before_live = _hashes(live)

    report = legacy_promote.promote(source, live)

    assert report.ok
    assert report.source_messages == 2
    assert report.pending_messages == 1
    assert report.written_messages == 0
    assert _hashes(source) == before_source
    assert _hashes(live) == before_live


def test_apply_merges_new_lines_without_changing_the_source(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(
        source,
        lines=(
            "> [2026-11-18 10:00|1794973200.000100] 홍길동: 지급 조건을 확인했습니다.",
            "> [2026-11-18 10:01|1794973260.000200] 홍길동: 새 메시지입니다.",
        ),
    )
    _live_message(live)
    before_source = _hashes(source)

    report = legacy_promote.promote(source, live, apply=True)

    assert report.ok
    assert report.written_messages == 1
    assert _hashes(source) == before_source
    target, = (live / WS).glob(f"{CID}__*/archive/raw/*.md")
    doc = load_doc(target)
    assert [line.message_ts for line in doc.raw_lines] == [
        "1794973200.000100", "1794973260.000200",
    ]


def test_coordinate_free_lines_are_deduplicated_by_the_exact_legacy_line(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(
        source,
        lines=(
            "> [2026-11-18 10:00] 홍길동: 좌표 없는 기록",
            "> [2026-11-18 10:00] 홍길동: 좌표 없는 기록",
        ),
    )

    report = legacy_promote.promote(source, live, apply=True)

    assert report.ok
    assert report.written_messages == 1


def test_an_access_policy_mismatch_blocks_every_write(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(source)
    _live_message(live, acl="다른채널")
    before = _hashes(live)

    report = legacy_promote.promote(source, live, apply=True)

    assert not report.ok
    assert "access policies differ" in report.blocked[0]
    assert _hashes(live) == before


def test_a_private_legacy_channel_without_acl_is_not_promoted(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(source, acl="")

    report = legacy_promote.promote(source, live, apply=True)

    assert not report.ok
    assert "empty ACL" in report.blocked[0]
    assert _hashes(live) == {}


def test_attachment_extracts_copy_idempotently(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(source)
    attachment = (
        source / WS / "channels" / CID / "attachments" / "F12345678" / "rev.md"
    )
    _legacy_attachment(attachment, "extracted body")

    first = legacy_promote.promote(source, live, apply=True)
    second = legacy_promote.promote(source, live, apply=True)

    assert first.ok and first.copied_attachment_documents == 1
    assert second.ok and second.identical_attachment_documents == 1
    copied, = (live / WS).glob(
        f"{CID}__*/archive/attachments/F12345678/rev.md"
    )
    assert "extracted body" in copied.read_text(encoding="utf-8")


def test_a_different_attachment_with_the_same_revision_blocks_all_writes(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(
        source,
        lines=(
            "> [2026-11-18 10:01|1794973260.000200] 홍길동: 쓰이면 안 됩니다.",
        ),
    )
    source_attachment = (
        source / WS / "channels" / CID / "attachments" / "F12345678" / "rev.md"
    )
    _legacy_attachment(source_attachment, "legacy")
    live_attachment = (
        live / WS / f"{CID}__{CHANNEL}" / "archive" / "attachments"
        / "F12345678" / "rev.md"
    )
    live_attachment.parent.mkdir(parents=True)
    live_attachment.write_text("live", encoding="utf-8")
    before = _hashes(live)

    report = legacy_promote.promote(source, live, apply=True)

    assert not report.ok
    assert "revision differs" in report.blocked[0]
    assert _hashes(live) == before


def test_attachment_values_are_masked_before_they_enter_the_live_archive(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(source)
    attachment = (
        source / WS / "channels" / CID / "attachments" / "F12345678" / "rev.md"
    )
    _legacy_attachment(attachment, "연락처 010-1234-5678 입니다")

    report = legacy_promote.promote(source, live, apply=True)

    assert report.ok
    copied, = (live / WS).glob(
        f"{CID}__*/archive/attachments/F12345678/rev.md"
    )
    loaded = attachment_reader.load(copied)
    assert loaded is not None
    assert "010-1234-5678" not in loaded.text
    assert "[전화번호1]" in loaded.text


def test_an_attachment_cannot_widen_its_channel_acl(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(source)
    attachment = (
        source / WS / "channels" / CID / "attachments" / "F12345678" / "rev.md"
    )
    _legacy_attachment(attachment, "본문", acl="다른채널")

    report = legacy_promote.promote(source, live, apply=True)

    assert not report.ok
    assert "access policies differ" in report.blocked[0]
    assert _hashes(live) == {}


def test_a_sensitive_document_is_preserved_as_blocked_without_its_body(tmp_path):
    source, live = _roots(tmp_path)
    _legacy_doc(source)
    attachment = (
        source / WS / "channels" / CID / "attachments" / "F12345678" / "rev.md"
    )
    _legacy_attachment(attachment, "주민등록번호 900101-1234567 입니다")

    report = legacy_promote.promote(source, live, apply=True)

    assert report.ok
    copied, = (live / WS).glob(
        f"{CID}__*/archive/attachments/F12345678/rev.md"
    )
    loaded = attachment_reader.load(copied)
    assert loaded is not None
    assert loaded.conversion_state == attachment_doc.BLOCKED
    assert loaded.text == ""
    assert "900101" not in copied.read_text(encoding="utf-8")


def test_dm_documents_stay_in_the_private_dm_namespace(tmp_path):
    source, live = _roots(tmp_path)
    user_id = "U12345678"
    _legacy_doc(
        source,
        channel_id="D12345678",
        channel="DM:U12345678",
        acl="",
        dm_user=user_id,
    )

    report = legacy_promote.promote(source, live, apply=True)

    assert report.ok
    assert report.written_messages == 1
    assert list((live / WS / "dm" / user_id / "archive" / "raw").glob("*.md"))
    assert not list((live / WS).glob("D12345678__*/archive/raw/*.md"))


def test_an_unrelated_workspaces_underscore_tree_is_never_scanned(tmp_path):
    source, live = _roots(tmp_path)
    ignored = source.parent / "workspaces_" / "civil" / "secret.md"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("ignore me", encoding="utf-8")

    report = legacy_promote.promote(source, live, apply=True)

    assert report.ok
    assert report.source_documents == 0
    assert _hashes(live) == {}


@pytest.mark.parametrize("nested", ["source-in-live", "live-in-source"])
def test_source_and_destination_must_be_disjoint(tmp_path, nested):
    if nested == "source-in-live":
        live = tmp_path / "live"
        source = live / "quarantine"
    else:
        source = tmp_path / "quarantine"
        live = source / "live"
    source.mkdir(parents=True)
    live.mkdir(parents=True, exist_ok=True)

    with pytest.raises(legacy_promote.LegacyPromotionError, match="disjoint"):
        legacy_promote.promote(source, live)


def test_an_attachment_without_a_determinable_channel_policy_is_not_copied(tmp_path):
    """운영 채널은 있는데 원문이 없으면 대조할 정책이 없다 — 복사하지 않는다(fail-closed)."""
    source, live = _roots(tmp_path)
    (live / WS / f"{CID}__{CHANNEL}" / "archive" / "raw").mkdir(parents=True)
    attachment = (
        source / WS / "channels" / CID / "attachments" / "F12345678" / "rev.md"
    )
    _legacy_attachment(attachment, "본문", acl="다른채널")

    report = legacy_promote.promote(source, live, apply=True)

    assert not report.ok
    assert "no access policy for target channel" in report.blocked[0]
    assert not list(live.rglob("attachments/*/*.md"))


def test_a_malformed_channel_directory_is_not_globbed(tmp_path):
    source, live = _roots(tmp_path)
    attachment = (
        source / WS / "channels" / "C*" / "attachments" / "F12345678" / "rev.md"
    )
    try:
        _legacy_attachment(attachment, "본문")
    except OSError:
        return  # 파일 시스템이 `*` 를 허용하지 않으면 이 경로는 생길 수 없다

    report = legacy_promote.promote(source, live, apply=True)

    assert not report.ok
    assert _hashes(live) == {}


def _dm_attachment(source: Path, *, user_id: str, conversation: str, acl: str) -> Path:
    path = source / WS / "dm" / user_id / "attachments" / "F12345678" / "rev.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        attachment_doc.render(
            attachment_doc.AttachmentDoc(
                workspace=WS, channel_id=conversation, channel=f"DM:{user_id}",
                file_id="F12345678", name="개인메모.txt", revision="rev",
                visibility="private", acl=frozenset({acl}) if acl else frozenset(),
                text="개인 첨부", conversion_state=attachment_doc.CONVERTED,
            )
        ),
        encoding="utf-8",
    )
    return path


def test_a_dm_attachment_without_its_dm_document_is_not_copied(tmp_path):
    source, live = _roots(tmp_path)
    _dm_attachment(source, user_id="U12345678", conversation="D12345678", acl="")

    report = legacy_promote.promote(source, live, apply=True)

    assert not report.ok
    assert "no DM document plan" in report.blocked[0]
    assert _hashes(live) == {}


def test_a_dm_attachment_cannot_carry_a_different_policy(tmp_path):
    source, live = _roots(tmp_path)
    user_id = "U12345678"
    _legacy_doc(source, channel_id="D12345678", channel=f"DM:{user_id}", acl="",
                dm_user=user_id)
    _dm_attachment(source, user_id=user_id, conversation="D12345678", acl="공사팀")

    report = legacy_promote.promote(source, live, apply=True)

    assert not report.ok
    assert any("DM access policies differ" in item for item in report.blocked)
    assert _hashes(live) == {}


def test_a_dm_attachment_matching_its_dm_document_is_copied(tmp_path):
    source, live = _roots(tmp_path)
    user_id = "U12345678"
    _legacy_doc(source, channel_id="D12345678", channel=f"DM:{user_id}", acl="",
                dm_user=user_id)
    _dm_attachment(source, user_id=user_id, conversation="D12345678", acl="")

    report = legacy_promote.promote(source, live, apply=True)

    assert report.ok, report.blocked
    assert list((live / WS / "dm" / user_id).rglob("attachments/F12345678/rev.md"))
