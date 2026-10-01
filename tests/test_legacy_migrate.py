"""옛 아카이브를 새 채널별 구조로 옮기는 **계획.**

결정: 2026-09-30 오너 — 운영 원본은 읽기 전용, 계획·dry-run·검증 보고서 먼저.

여기서 보는 것은 넷이다.

1. **원본을 고치지 않는다** — 목적지에도 안 쓴다. 이 도구는 세기만 한다
2. **좌표가 없으면 합치지 않는다** — `(workspace, channel_id, message_ts)` 셋이
   다 있어야 같은 메시지로 센다
3. **자리를 지어내지 않는다** — 채널 ID 가 없는 v1 문서는 막고 목록에 남긴다
4. **권한이 넓어지지 않는다** — ACL 이 빈 비공개 문서는 그대로 안 옮긴다
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tybot.archive import legacy_migrate

WS = "tyit"
CID = "C0FUND123"
CHANNEL = "#팀-전산_ABB110-회의"


def _doc(
    *,
    channel_id: str = CID,
    visibility: str = "private",
    acl: str = "팀-전산_ABB110-회의",
    lines: str = "> [2026-09-01 10:00|1759100000.000100] 김현장: 착공계 제출했습니다.",
    schema: str = "2",
) -> str:
    head = [
        "---",
        f"workspace: {WS}",
        # 채널명은 반드시 따옴표로 감싼다. `#` 뒤를 주석으로 읽는 파서라
        # 감싸지 않으면 channel 이 빈 값이 된다.
        f'channel: "{CHANNEL}"',
        f"visibility: {visibility}",
        f"schema_version: {schema}",
        "last_ingested: 2026-09-01T10:00+09:00",
    ]
    if channel_id:
        head.append(f"channel_id: {channel_id}")
    if schema == "2":
        head.append("source_date: 2026-09-01")
    # 프론트매터 파서는 인라인 목록(`key: [a, b]`)만 읽는다.
    head.append(f"acl: [{acl}]" if acl else "acl: []")
    head += ["---", "", "## 원문", "", lines, ""]
    return "\n".join(head)


def _write(root: Path, name: str = "2026-09-01.md", **kwargs) -> Path:
    channel_id = kwargs.get("channel_id", CID)
    folder = f"{channel_id or 'legacy'}__팀-전산_ABB110-회의"
    path = root / "workspaces" / WS / "channels" / folder / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_doc(**kwargs), encoding="utf-8")
    return path


def _roots(tmp_path) -> tuple[Path, Path, Path]:
    source = tmp_path / "var" / "archive"
    destination = tmp_path / "var" / "archiver-shadow" / "archive"
    live = tmp_path / "var" / "archive"
    source.mkdir(parents=True, exist_ok=True)
    return source, destination, live


def _plan(tmp_path, *, collected=None):
    source, destination, live = _roots(tmp_path)
    return legacy_migrate.plan(
        source, destination, live_archive=live, collected=collected,
    )


# --- 아무것도 바꾸지 않는다 -------------------------------------------------------

def test_the_module_never_writes():
    """이 시험이 이 도구의 전제다. 계획과 실행이 같은 코드에 있으면 언젠가 옮긴다."""
    source = (Path(__file__).resolve().parent.parent / "src" / "tybot" / "archive"
              / "legacy_migrate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    for forbidden in ("write_text", "write_bytes", "mkdir", "copy2", "copy",
                      "replace", "unlink", "rename", "rmtree", "open"):
        assert forbidden not in called, forbidden


def test_planning_leaves_the_source_untouched(tmp_path):
    source, _, _ = _roots(tmp_path)
    path = _write(source)
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    _plan(tmp_path)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_planning_creates_nothing_at_the_destination(tmp_path):
    source, destination, _ = _roots(tmp_path)
    _write(source)

    report = _plan(tmp_path)

    assert report.planned
    assert not destination.exists()


# --- 자리를 지어내지 않는다 -------------------------------------------------------

def test_a_document_with_a_channel_id_gets_a_place(tmp_path):
    source, destination, _ = _roots(tmp_path)
    _write(source)

    (item,) = _plan(tmp_path).placements

    assert item.verdict == "plan"
    assert item.destination == (
        destination / WS / f"{CID}__팀-전산_ABB110-회의" / "archive" / "raw"
        / "2026-09-01.md"
    )


def test_a_v1_document_without_a_channel_id_is_blocked(tmp_path):
    """새 구조의 디렉터리 이름이 채널 ID 다. 지어내면 실제 채널과 다른 자리에 쌓인다."""
    source, _, _ = _roots(tmp_path)
    _write(source, channel_id="", schema="1")

    (item,) = _plan(tmp_path).placements

    assert item.blocked
    assert "채널 ID 가 없습니다" in item.reason
    assert item.destination is None


def test_a_channel_id_the_new_layout_refuses_is_blocked_here_too(tmp_path):
    """여기서 통과시키면 목적지에서 거절당하고, 그때는 절반만 옮겨진 뒤다."""
    source, _, _ = _roots(tmp_path)
    _write(source, channel_id="legacy-25f4047faf16")

    (item,) = _plan(tmp_path).placements

    assert item.blocked
    assert "지어내지 않습니다" in item.reason


def test_the_id_rule_matches_the_destination_rule():
    """두 규칙이 갈리면 계획이 통과시킨 문서가 목적지에서 거절된다."""
    from tybot.archive import shadow_paths

    source = Path(shadow_paths.__file__).read_text(encoding="utf-8")

    assert legacy_migrate.CHANNEL_ID.pattern in source


# --- 권한이 넓어지지 않는다 -------------------------------------------------------

def test_a_private_document_without_an_acl_is_blocked(tmp_path):
    """빈 ACL 을 그대로 옮기면 그 문서가 누구에게나 열린다(절대 원칙 3)."""
    source, _, _ = _roots(tmp_path)
    _write(source, acl="")

    (item,) = _plan(tmp_path).placements

    assert item.blocked
    assert "권한이 조용히 넓어집니다" in item.reason


def test_a_public_document_without_an_acl_is_fine(tmp_path):
    """공개 문서는 원래 ACL 로 막지 않는다. 여기서 막으면 옮길 수 있는 것이 준다."""
    source, _, _ = _roots(tmp_path)
    _write(source, acl="", visibility="public")

    (item,) = _plan(tmp_path).placements

    assert item.verdict == "plan"


def test_dm_documents_are_counted_without_being_opened(tmp_path):
    """DM 은 그 사람 한 명의 기록이다. 목록에 넣으면 이 도구가 그 파일을 연다."""
    source, _, _ = _roots(tmp_path)
    dm = source / "workspaces" / WS / "dm" / "u0br" / "raw" / "2026-09-01.md"
    dm.parent.mkdir(parents=True, exist_ok=True)
    # **읽을 수 있는** 문서로 둔다. 깨진 파일로 두면 「목록에 없다」 가 아니라
    # 「못 읽었다」 로 빠지고, 그러면 이 시험이 다른 이유로 통과한다.
    dm.write_text(_doc(), encoding="utf-8")
    _write(source)

    report = _plan(tmp_path)

    assert report.dm_documents == 1
    assert report.permissions()["dmDocumentsLeftBehind"] == 1
    assert dm not in [item.doc.path for item in report.placements]
    assert dm not in [Path(path) for path, _ in report.unreadable]
    assert len(report.placements) == 1


def test_the_report_carries_no_message_text(tmp_path):
    """보고서에 본문이 들어가면 권한이 안 붙은 사본이 하나 더 생긴다."""
    source, _, _ = _roots(tmp_path)
    _write(source)

    blob = repr(_plan(tmp_path).as_json())

    assert "착공계" not in blob


# --- 좌표가 없으면 합치지 않는다 ---------------------------------------------------

def test_a_coordinated_line_matches_the_backfill(tmp_path):
    source, _, _ = _roots(tmp_path)
    _write(source)
    index = {(WS, CID, "1759100000.000100")}

    (item,) = _plan(tmp_path, collected=index).placements

    assert item.duplicate_lines == 1
    assert item.residual_duplicate_lines == 1
    assert item.matched_messages == 1
    content = _plan(tmp_path, collected=index).content()
    assert content["residualNewLinesEstimate"] == 0


def test_a_line_without_a_coordinate_is_never_matched(tmp_path):
    """비슷해 보인다고 합치면 다른 메시지를 지우거나 같은 것을 둘로 남긴다."""
    source, _, _ = _roots(tmp_path)
    _write(source, lines="> [2026-09-01 10:00] 김현장: 착공계 제출했습니다.")
    index = {(WS, CID, "1759100000.000100")}

    report = _plan(tmp_path, collected=index)
    (item,) = report.placements

    assert item.duplicate_lines == 0
    assert report.provenance()["uncoordinatedLines"] == 1
    assert report.provenance()["coordinatedLines"] == 0


def test_an_empty_coordinate_in_the_index_matches_nothing(tmp_path):
    """좌표가 없는 줄끼리 「둘 다 비었으니 같다」 가 되면 안 된다.

    이 시험이 없으면 `if line.message_ts` 를 지워도 아무도 모른다 — 빈 값으로
    짝지어지는 순간 옛 줄 전부가 한 덩어리로 묶인다.
    """
    source, _, _ = _roots(tmp_path)
    _write(source, lines="> [2026-09-01 10:00] 김현장: 착공계 제출했습니다.")
    index = {(WS, CID, "")}

    (item,) = _plan(tmp_path, collected=index).placements

    assert item.duplicate_lines == 0


def test_the_same_ts_in_another_channel_is_not_a_duplicate(tmp_path):
    """둘로 맞추면 다른 채널의 같은 시각이 같은 메시지가 된다."""
    source, _, _ = _roots(tmp_path)
    _write(source)
    index = {(WS, "C0OTHER99", "1759100000.000100")}

    (item,) = _plan(tmp_path, collected=index).placements

    assert item.duplicate_lines == 0


def test_the_same_ts_in_another_workspace_is_not_a_duplicate(tmp_path):
    """워크스페이스를 안 보면 남의 자료가 우리 중복으로 세어진다(절대 원칙 4)."""
    source, _, _ = _roots(tmp_path)
    _write(source)
    index = {("mgmt", CID, "1759100000.000100")}

    (item,) = _plan(tmp_path, collected=index).placements

    assert item.duplicate_lines == 0


def test_without_an_index_nothing_is_called_a_duplicate(tmp_path):
    """대조하지 않은 것과 겹치는 것이 없는 것은 다르다. 보고서 경고가 그 차이다."""
    source, _, _ = _roots(tmp_path)
    _write(source)

    (item,) = _plan(tmp_path).placements

    assert item.duplicate_lines == 0


def test_the_coordinate_index_needs_all_three_values(tmp_path):
    """채널 ID 가 없는 문서는 좌표를 만들 수 없다. 워크스페이스만으로 맞추면 안 된다."""
    collected = tmp_path / "collected"
    path = collected / "workspaces" / WS / "channels" / "x" / "raw" / "2026-09-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_doc(channel_id="", schema="1"), encoding="utf-8")

    assert legacy_migrate.collected_coordinates(collected) == set()


def test_the_coordinate_index_reads_the_new_layout(tmp_path):
    """이미 옮긴 채널의 좌표도 읽어야 두 번 옮기지 않는다."""
    collected = tmp_path / "collected"
    path = collected / WS / f"{CID}__팀" / "archive" / "raw" / "2026-09-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_doc(), encoding="utf-8")

    assert legacy_migrate.collected_coordinates(collected) == {
        (WS, CID, "1759100000.000100"),
    }


# --- 자리 자체를 거부하는 경우 -----------------------------------------------------

def test_a_destination_inside_the_source_is_refused(tmp_path):
    """옛 자료 위에 새 구조를 쓰면 어느 쪽이 원본인지 알 수 없게 된다.

    운영 archive 를 원본과 다른 자리로 둔다. 같게 두면 이 시험이 **운영 겹침**
    사유로 통과하고, 원본 겹침 판정이 사라져도 모른다.
    """
    source, _, _ = _roots(tmp_path)
    live = tmp_path / "var" / "live-archive"

    refusals = legacy_migrate.check_roots(source, source / "새구조", live)

    assert any("어느 쪽이 원본인지" in line for line in refusals)
    assert not any("운영 archive" in line for line in refusals)


def test_the_live_archive_as_a_destination_is_refused(tmp_path):
    """이 도구는 운영 아카이브에 쓰지 않는다."""
    source, _, live = _roots(tmp_path)

    refusals = legacy_migrate.check_roots(source, live / "안쪽", live)

    assert any("운영 archive" in line for line in refusals)


def test_a_relative_root_is_refused(tmp_path):
    source, _, live = _roots(tmp_path)

    assert any(
        "절대경로" in line
        for line in legacy_migrate.check_roots(Path("archive"), source, live)
    )


def test_a_refused_plan_says_so_in_the_report(tmp_path):
    source, _, live = _roots(tmp_path)
    _write(source)

    report = legacy_migrate.plan(source, source / "안쪽", live_archive=live)

    assert report.as_json()["blocked"] is True


# --- 못 읽은 것을 조용히 빠뜨리지 않는다 -------------------------------------------

def test_an_unreadable_document_is_listed_not_skipped(tmp_path):
    """조용히 빠지면 옮긴 뒤에도 몇 건이 빠졌는지 모른다."""
    source, _, _ = _roots(tmp_path)
    broken = source / "workspaces" / WS / "channels" / "C1__x" / "raw" / "2026-09-01.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("프론트매터가 없다", encoding="utf-8")
    _write(source)

    report = _plan(tmp_path)

    assert len(report.unreadable) == 1
    assert report.content()["unreadable"] == 1
    assert len(report.planned) == 1


# --- 첨부 ----------------------------------------------------------------------

def test_attachment_lines_are_counted(tmp_path):
    source, _, _ = _roots(tmp_path)
    _write(source, lines=(
        "> [2026-09-01 10:00|1759100000.000100] 김현장:"
        " [첨부:변환] 착공계.pdf (pdf, 120KB) · id:F123"
    ))

    files = _plan(tmp_path).attachments()

    assert files["referenceLines"] == 1
    assert files["identifiedFiles"] == 1
    assert files["linesWithoutFileId"] == 0


def test_an_attachment_line_without_a_file_id_is_flagged(tmp_path):
    """옛 줄에는 ID 가 없다. 이름으로만 이으면 같은 이름이 두 번 오른 채널에서 틀린다."""
    source, _, _ = _roots(tmp_path)
    _write(source, lines=(
        "> [2026-09-01 10:00|1759100000.000100] 김현장:"
        " [첨부:변환] 착공계.pdf (pdf, 120KB)"
    ))

    files = _plan(tmp_path).attachments()

    assert files["referenceLines"] == 1
    assert files["linesWithoutFileId"] == 1


# --- 보고서 -------------------------------------------------------------------

def test_the_report_has_all_four_sections(tmp_path):
    """오너가 요구한 네 가지다. 하나라도 빠지면 검토가 반쪽이 된다."""
    source, _, _ = _roots(tmp_path)
    _write(source)

    data = _plan(tmp_path).as_json()

    assert {"content", "permissions", "provenance", "attachments"} <= set(data)


def test_the_report_says_nothing_was_changed(tmp_path):
    source, _, _ = _roots(tmp_path)
    _write(source)

    assert "고치지 않았" in _plan(tmp_path).as_json()["note"]


@pytest.mark.parametrize("schema,expected", [("1", "schemaV1"), ("2", "schemaV2")])
def test_the_report_counts_schema_versions(tmp_path, schema, expected):
    source, _, _ = _roots(tmp_path)
    _write(source, schema=schema)

    assert _plan(tmp_path).provenance()[expected] == 1


# --- 사람 대화와 첨부를 갈라 센다 --------------------------------------------------
#
# 2026-09-30 운영 아카이브 실측: raw 236,046 줄 중 사람 대화는 491 줄이었고 나머지는
# 첨부에서 뽑아낸 본문이었다. 합쳐서 「원문 23만 줄」 로 보고하면 규모도 위험도
# 잘못 잡는다 — 그 23만 줄은 새 구조의 raw 에 **들어갈 수 없는** 파생 자료다.

BODY_LINE = (
    "> [2026-09-01 10:01|1759100001.000100] 김현장:"
    " [첨부추출:착공계.pdf] 공사기간은 2026년 3월까지로 한다"
)
REFERENCE_LINE = (
    "> [2026-09-01 10:00|1759100000.000100] 김현장:"
    " [첨부:변환] 착공계.pdf (pdf, 120KB) · id:F123"
)
HUMAN_LINE = "> [2026-09-01 09:59|1759099999.000100] 이감리: 착공계 확인했습니다."


def _mixed(tmp_path):
    source, _, _ = _roots(tmp_path)
    _write(source, lines="\n".join([HUMAN_LINE, REFERENCE_LINE, BODY_LINE]))
    return _plan(tmp_path)


def test_attachment_body_is_not_counted_as_conversation(tmp_path):
    """이 시험이 이 분리의 이유다. 합쳐 세면 첨부 본문이 사람 대화로 보고된다."""
    content = _mixed(tmp_path).content()

    assert content["rawLines"] == 3
    assert content["residualLines"] == 1
    assert content["attachmentReferenceLines"] == 1
    assert content["attachmentBodyLines"] == 1


def test_every_line_lands_in_exactly_one_kind(tmp_path):
    """갈래 합이 전체와 안 맞으면 어느 갈래가 새는지 모른다.

    `residual` 이 나머지라서 합은 항상 맞는다. 그 사실을 적어 두는 이유는, 합이
    맞는다고 **분류가 맞는 것은 아니기** 때문이다 — 모르는 형식은 residual 로 간다.
    """
    content = _mixed(tmp_path).content()

    assert (
        content["residualLines"]
        + content["attachmentReferenceLines"]
        + content["attachmentBodyLines"]
        + content["revisionLines"]
        + content["canvasLines"]
        == content["rawLines"]
    )


def test_the_note_explains_what_residual_means(tmp_path):
    """「나머지」 가 무엇을 뺀 나머지인지 말하지 않으면 그냥 사람 대화로 읽힌다."""
    note = _mixed(tmp_path).content()["note"]

    assert "residualLines" in note
    assert "나머지" in note
    assert "단정하지 않습니다" in note


def test_the_note_does_not_claim_the_runtime_inspects_raw(tmp_path):
    """**이 시험이 정정의 핵심이다.**

    전에는 「새 구조가 raw 의 첨부 본문을 기동 단계에서 거부한다」 고 적었다.
    코드는 그런 검사를 하지 않는다 — 설정(`separate_attachments`)과 루트 모양
    (`workspaces/` 유무)만 본다. 그 차이를 뭉개면, 옛 raw 를 새 루트에 넣어도
    「어차피 런타임이 막아 준다」 고 믿게 된다. 막아 주지 않는다.
    """
    note = _mixed(tmp_path).content()["note"]

    assert "내용을 검사하지" in note
    assert "실행 중에 걸리지 않으므로" in note
    assert "사람이 지켜야 하는 약속" in note


def test_the_refusals_the_note_cites_exist_in_the_code():
    """근거가 사라지면 경고가 거짓이 된다. 두 거절 문구를 그대로 대조한다."""
    source = (Path(__file__).resolve().parent.parent / "src" / "tybot"
              / "archiving_bot.py").read_text(encoding="utf-8")

    # 루트 모양 검사 — 옛 `workspaces/` 가 있으면 기동하지 않는다.
    assert "requires a new shadow root without legacy workspaces" in source
    # 설정 검사 — 첨부 분리가 꺼져 있으면 기동하지 않는다.
    assert "per-channel-v1 requires separate attachments" in source


def test_the_runtime_forces_separation_instead_of_checking_files():
    """강제하는 것이지 검사하는 것이 아니다. 이 구분이 note 의 근거다."""
    source = (Path(__file__).resolve().parent.parent / "src" / "tybot"
              / "archiving_bot.py").read_text(encoding="utf-8")
    config = source[source.index("def shadow_workspace_config("):]
    config = config[:config.index("class ShadowCollector")]

    assert "replace(cfg, separate_attachments=True)" in config
    # 파일 내용을 읽는 코드가 없다. 있으면 note 를 다시 써야 한다.
    for reading in ("read_text", "raw_lines", "open("):
        assert reading not in config, reading


def test_human_coordinates_exclude_attachment_lines(tmp_path):
    """소급과 대조할 수 있는 분량은 사람 대화 기준이다.

    합쳐 세면 첨부 줄의 좌표까지 포함돼 대조 가능성이 실제보다 높아 보인다.
    """
    provenance = _mixed(tmp_path).provenance()

    assert provenance["coordinatedLines"] == 3
    assert provenance["residualLines"] == 1
    assert provenance["residualCoordinatedLines"] == 1
    assert provenance["residualUncoordinatedLines"] == 0


def test_a_human_line_without_a_coordinate_is_counted_apart(tmp_path):
    source, _, _ = _roots(tmp_path)
    _write(source, lines="\n".join([
        "> [2026-09-01 09:59] 이감리: 좌표 없는 옛 줄입니다.",
        BODY_LINE,
    ]))

    provenance = _plan(tmp_path).provenance()

    assert provenance["residualLines"] == 1
    assert provenance["residualCoordinatedLines"] == 0
    assert provenance["residualUncoordinatedLines"] == 1


def test_attachment_body_lines_are_reported_in_the_attachment_section(tmp_path):
    files = _mixed(tmp_path).attachments()

    assert files["referenceLines"] == 1
    assert files["bodyLines"] == 1
    assert files["identifiedFiles"] == 1


def test_a_matched_attachment_message_does_not_reduce_the_estimate(tmp_path):
    """**좌표가 다른 메시지면** 나머지 줄은 영향받지 않는다.

    하나로 세면 「옮길 것이 거의 없다」 고 잘못 읽는다 — 실측에서 좌표가 맞은 줄은
    첨부 줄이 대부분이었다.
    """
    source, _, _ = _roots(tmp_path)
    _write(source, lines="\n".join([HUMAN_LINE, BODY_LINE]))
    # 소급이 첨부 본문 줄만 이미 가져왔다. **그 줄의 좌표는 사람 줄과 다르다.**
    index = {(WS, CID, "1759100001.000100")}

    content = _plan(tmp_path, collected=index).content()

    assert content["matchedMessages"] == 1
    assert content["rawLinesInMatchedMessages"] == 1
    assert content["residualLinesInMatchedMessages"] == 0
    assert content["residualNewLinesEstimate"] == 1


def test_a_matched_conversation_message_reduces_the_estimate(tmp_path):
    """반대로 나머지 줄의 좌표가 맞으면 추정치가 그만큼 줄어야 한다."""
    source, _, _ = _roots(tmp_path)
    _write(source, lines="\n".join([HUMAN_LINE, BODY_LINE]))
    index = {(WS, CID, "1759099999.000100")}

    content = _plan(tmp_path, collected=index).content()

    assert content["matchedMessages"] == 1
    assert content["residualLinesInMatchedMessages"] == 1
    assert content["residualNewLinesEstimate"] == 0


def test_the_misleading_keys_are_gone(tmp_path):
    """옛 이름을 남겨 두면 누군가 다시 그 수를 쓴다.

    - `newLines` 는 첨부 본문을 포함해 이관 가능량처럼 보였다
    - `humanNewLines` 는 좌표만 보고 확정한 것처럼 보였다
    - `duplicateLines` 는 줄이 넘어왔다는 뜻으로 읽혔다(좌표는 메시지 단위다)
    """
    content = _mixed(tmp_path).content()

    for gone in ("newLines", "duplicateLines", "humanNewLines",
                 "humanDuplicateLines", "humanLines", "unclassifiedLines"):
        assert gone not in content, gone
    assert {
        "matchedMessages", "rawLinesInMatchedMessages",
        "residualLinesInMatchedMessages", "residualNewLinesEstimate",
        "estimate", "estimateCaveat",
    } <= set(content)


# --- 좌표는 메시지 단위다 ---------------------------------------------------------
#
# 2026-10-01 지적. `(workspace, channel_id, message_ts)` 는 **메시지 하나**를 가리킨다.
# writer 는 한 메시지의 사람 발언과 첨부 줄에 **같은 ts** 를 붙인다
# (`pilot._messages_from`: "메시지 하나를 여는 좌표. 첨부에서 뽑은 줄에도 같은 값을
# 남긴다"). 그래서 좌표만으로는 **어느 줄이** 새 자료에 있는지 알 수 없다.
#
# 앞선 시험은 사람 줄과 첨부 줄에 서로 다른 ts 를 줘서 이 경우를 통째로 놓쳤다.

SAME_TS = "1759100000.000100"
SAME_TS_HUMAN = f"> [2026-09-01 10:00|{SAME_TS}] 김현장: 착공계 올립니다."
SAME_TS_REFERENCE = (
    f"> [2026-09-01 10:00|{SAME_TS}] 김현장:"
    " [첨부:변환] 착공계.pdf (pdf, 120KB) · id:F123"
)
SAME_TS_BODY = (
    f"> [2026-09-01 10:00|{SAME_TS}] 김현장:"
    " [첨부추출:착공계.pdf] 공사기간은 2026년 3월까지"
)


def test_one_message_can_hold_a_human_line_and_attachment_lines(tmp_path):
    """세 줄이 같은 좌표를 쓴다. 이 사실이 아래 시험들의 전제다."""
    source, _, _ = _roots(tmp_path)
    _write(source, lines="\n".join(
        [SAME_TS_HUMAN, SAME_TS_REFERENCE, SAME_TS_BODY]
    ))

    content = _plan(tmp_path).content()

    assert content["rawLines"] == 3
    assert content["attachmentReferenceLines"] == 1
    assert content["attachmentBodyLines"] == 1


def test_a_matched_coordinate_cannot_say_which_line_arrived(tmp_path):
    """**이 시험이 정정의 핵심이다.**

    새 자료에 그 메시지의 **첨부 줄만** 있어도 좌표는 맞는다. 그때 옛 사람 발언까지
    「이미 있다」 로 세면 옮길 양을 과소평가하고, 그 차이는 옮긴 뒤에야 드러난다.

    좌표만으로는 판정할 수 없으므로 보고서는 **추정치**라고 말해야 한다.
    """
    source, _, _ = _roots(tmp_path)
    _write(source, lines="\n".join(
        [SAME_TS_HUMAN, SAME_TS_REFERENCE, SAME_TS_BODY]
    ))
    # 새 자료에는 첨부 본문 줄만 들어왔다. 좌표는 같다.
    index = {(WS, CID, SAME_TS)}

    content = _plan(tmp_path, collected=index).content()

    assert content["matchedMessages"] == 1
    assert content["estimate"] is True
    assert "메시지 단위" in content["estimateCaveat"]


def test_the_report_does_not_promise_a_migration_target(tmp_path):
    """확정적인 「이관 대상」 이라는 이름을 쓰지 않는다."""
    content = _mixed(tmp_path).content()

    assert "humanNewLines" not in content
    assert "residualNewLinesEstimate" in content


# --- 사람 대화는 **나머지**다 -----------------------------------------------------
#
# 2026-10-01 지적. 분류가 「알려진 첨부 표시가 아니면 사람 대화」 였다. 그래서
# `unclassifiedLines` 는 구조상 항상 0 이었고, 모르는 형식이 사람 대화에 섞였다.

CANVAS_LINE = f"> [2026-09-01 10:05|{SAME_TS}] 캔버스: 회의실 예약 현황"
REVISION_LINE = "> [2026-09-01 10:06|1759100006.000100] 김현장: [수정 전] 3월까지"


def test_a_canvas_line_is_not_counted_as_conversation(tmp_path):
    """캔버스 스냅샷은 사람이 그 자리에서 한 말이 아니다."""
    source, _, _ = _roots(tmp_path)
    _write(source, lines="\n".join([SAME_TS_HUMAN, CANVAS_LINE]))

    content = _plan(tmp_path).content()

    assert content["canvasLines"] == 1
    assert content["residualLines"] == 1


def test_a_revision_line_is_counted_apart(tmp_path):
    """수정·삭제 이력은 보존 대상이지만 대화 한 줄과 같은 것으로 세지 않는다."""
    source, _, _ = _roots(tmp_path)
    _write(source, lines="\n".join([SAME_TS_HUMAN, REVISION_LINE]))

    content = _plan(tmp_path).content()

    assert content["revisionLines"] == 1
    assert content["residualLines"] == 1


def test_the_residual_is_named_as_a_residual(tmp_path):
    """「사람 대화」 라고 단정하면 모르는 형식이 그 수에 숨는다."""
    content = _mixed(tmp_path).content()

    assert "humanLines" not in content
    assert "residualLines" in content
    assert "나머지" in content["note"]


# --- 한 메시지가 여러 날짜 파일에 걸친다 -------------------------------------------
#
# 2026-10-01 지적. `_ingest_revision` 은 **수정 시각**으로 날짜 파일을 고르고
# **원문 메시지의 좌표**를 그대로 남긴다(`source_ts=message_ts`). 그래서 9월 1일
# 메시지를 9월 5일에 고치면 같은 `(workspace, channel_id, ts)` 가 두 파일에 있다.
#
# 문서별로 센 메시지 수를 더하면 한 메시지가 두 건이 된다.

CROSS_TS = "1759100000.000100"
ORIGINAL_LINE = f"> [2026-09-01 10:00|{CROSS_TS}] 김현장: 공사기간은 2월까지"
EDIT_BEFORE = f"> [2026-09-05 09:00|{CROSS_TS}] 김현장: [수정 전] 공사기간은 2월까지"
EDIT_AFTER = f"> [2026-09-05 09:00|{CROSS_TS}] 김현장: [수정 후] 공사기간은 3월까지"


def _across_two_days(tmp_path):
    source, _, _ = _roots(tmp_path)
    _write(source, "2026-09-01.md", lines=ORIGINAL_LINE)
    _write(source, "2026-09-05.md", lines="\n".join([EDIT_BEFORE, EDIT_AFTER]))
    return source


def test_one_message_spread_over_two_files_counts_once(tmp_path):
    """**이 시험이 정정의 핵심이다.**

    문서별 메시지 수를 더하면 한 메시지가 두 건이 된다. 그 수가 커지면 「이미
    대부분 넘어왔다」 로 읽히고, 그 오해는 옮긴 뒤에야 드러난다.
    """
    _across_two_days(tmp_path)
    index = {(WS, CID, CROSS_TS)}

    content = _plan(tmp_path, collected=index).content()

    assert content["documents"] == 2
    assert content["matchedMessages"] == 1
    # 줄은 파일마다 따로 있으므로 더하는 것이 맞다 — 원문 1줄 + 이력 2줄.
    assert content["rawLinesInMatchedMessages"] == 3


def test_the_same_ts_in_two_channels_counts_twice(tmp_path):
    """채널이 다르면 다른 메시지다. ts 만으로 합치면 남의 채널과 겹친다(원칙 4)."""
    source, _, _ = _roots(tmp_path)
    _write(source, "2026-09-01.md", lines=ORIGINAL_LINE)
    other = source / "workspaces" / WS / "channels" / "C0OTHER99__다른방" / "raw"
    other.mkdir(parents=True, exist_ok=True)
    (other / "2026-09-01.md").write_text(
        _doc(channel_id="C0OTHER99", lines=ORIGINAL_LINE), encoding="utf-8",
    )
    index = {(WS, CID, CROSS_TS), (WS, "C0OTHER99", CROSS_TS)}

    content = _plan(tmp_path, collected=index).content()

    assert content["matchedMessages"] == 2


def test_the_document_count_is_not_called_a_migration_target(tmp_path):
    """경로를 배정할 수 있다는 것과 안전하게 옮길 수 있다는 것은 다르다.

    첨부 본문이 든 옛 raw 도 경로는 배정된다. 「옮길 문서」 라고 부르면 그 수가
    안전한 이관 가능량으로 읽힌다.
    """
    content = _mixed(tmp_path).content()

    assert "pathAssignableDocuments" in content
    assert content["pathAssignableDocuments"] == content["documents"]


def test_the_cli_does_not_say_the_documents_are_movable():
    """화면 문구도 같은 오해를 만든다."""
    source = (Path(__file__).resolve().parent.parent / "scripts"
              / "plan_legacy_migration.py").read_text(encoding="utf-8")

    assert "옮길 문서" not in source
    assert "경로 배정 가능 문서" in source
