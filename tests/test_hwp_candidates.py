"""보관 HWP 복구 — 후보 변환기 대조와 **봇 산출물 차단** (2026-10-20).

범위: `docs/verification/2026-10-20-companion-app-rollout.md`

이 파일이 지키는 것 넷.

1. **봇이 만든 파일은 사람 원본이 아니다.** rhwp 같은 변환 앱을 채널에 초대하면
   그 앱이 PDF 를 올린다. 그것을 원본으로 세면 **우리 변환본을 우리가 다시 근거로
   쓰는 고리**가 생긴다(원칙 1). 필터를 푸는 변경이 들어오면 여기서 빨개져야 한다.
2. **읽기 전용이다.** 후보를 돌려 보는 것이 원본·실패 기록을 건드리면 안 된다.
3. **못 돌린 것과 안 되는 것을 가른다.** 미설치는 실패가 아니다.
4. **추출량 차이는 누락률이 아니다.** 어느 쪽이 맞는지 이 측정은 모른다.

실제 사내 HWP 는 fixture 로 넣지 않는다. OLE2 머리 바이트만 합성한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tybot.archive import hwp_candidates as hc

OLE2 = hc.OLE2_MAGIC + b"\x00" * 64

# 명령 템플릿은 **쉘 규칙**으로 읽힌다. 백슬래시는 이스케이프라 슬래시 경로를 쓴다.
PY = Path(sys.executable).as_posix()


# --- 봇 산출물은 원본이 되지 않는다 -----------------------------------------------
#
# **필터를 푸는 변경을 여기서 잡는다.** 수집 경로는 셋이고, 하나만 풀려도 그
# 경로로 들어온 봇 PDF 가 근거가 된다.


def test_realtime_ingest_drops_bot_and_app_messages():
    """실시간 수집. `bot_id`·`app_id` 어느 쪽도 사람 원본이 아니다."""
    source = Path("src/tybot/archiving_bot.py").read_text(encoding="utf-8")

    assert 'event.get("bot_id") or event.get("app_id")' in source
    assert '"skipped-bot-or-system"' in source


def test_backfill_drops_bot_messages():
    """소급 수집. 실시간만 막으면 **옛 메시지로 들어온다.**"""
    source = Path("src/tybot/archive/backfill.py").read_text(encoding="utf-8")

    assert 'if message.get("bot_id") or message.get("app_id"):' in source


def test_history_collection_drops_bot_and_app_messages():
    source = Path("src/tybot/collect.py").read_text(encoding="utf-8")

    assert 'm.get("bot_id")' in source
    assert 'm.get("app_id")' in source


def test_file_recovery_refuses_bot_generated_files():
    """파일 단독 복구. **업로더가 사람인지 Slack 에 직접 묻는다.**"""
    source = Path("src/tybot/archive/file_recovery.py").read_text(encoding="utf-8")

    assert 'raise PermissionError("bot-generated files cannot be recovered as evidence")' in source
    assert 'user.get("is_bot") is not False' in source
    assert 'raise PermissionError("a verified human uploader is required")' in source


def test_a_bot_uploaded_pdf_cannot_pass_the_uploader_check():
    """계약을 문장이 아니라 **동작**으로 본다."""
    from tybot.archive import file_recovery

    class _Client:
        def files_info(self, file):
            return {"file": {
                "id": file, "channels": ["C0C9A86JUSJ"],
                # rhwp 가 변환해 올린 PDF 의 모양.
                "bot_id": "B0RHWP0001", "user": "U0RHWPBOT",
                "permalink": "https://x.slack.com/files/U0RHWPBOT/F0BOT1/out.pdf",
            }}

        def users_info(self, user):  # pragma: no cover - 여기까지 오면 안 된다
            raise AssertionError("봇 파일이 업로더 조회까지 갔다")

    with pytest.raises(PermissionError, match="bot-generated"):
        file_recovery._shared_file(_Client(), "C0C9A86JUSJ", "F0BOT1", set())


def test_an_app_user_uploader_cannot_become_evidence():
    """`bot_id` 가 없어도 앱 사용자면 사람이 아니다."""
    from tybot.archive import file_recovery

    class _Client:
        def files_info(self, file):
            return {"file": {
                "id": file, "channels": ["C1"], "user": "U0APP0001",
                "permalink": "https://x.slack.com/files/U0APP0001/F0APP1/out.pdf",
            }}

        def users_info(self, user):
            return {"user": {"id": user, "is_bot": False, "is_app_user": True}}

    with pytest.raises(PermissionError, match="bot or unknown uploader"):
        file_recovery._shared_file(_Client(), "C1", "F0APP1", set())


# --- 인벤토리: 무엇이 대상인가 ----------------------------------------------------


def _inventory_module():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "inventory_hwp_originals.py"
    spec = importlib.util.spec_from_file_location("inventory_hwp_originals", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["inventory_hwp_originals"] = module
    spec.loader.exec_module(module)
    return module


inv = _inventory_module()


class _Item:
    def __init__(self, workspace="tyit"):
        self.workspace = workspace
        self.channel_id = "C0C9A86JUSJ"
        self.file_id = "F0TEST0001"
        self.filetype = "hwp"


def _staged(tmp_path: Path, data: bytes = OLE2, **over) -> dict:
    import hashlib

    original = tmp_path / "objects" / "F0TEST0001" / "보고.hwp"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(data)
    meta = {
        "slack_file_id": "F0TEST0001",
        "filetype": "hwp",
        "original_state": "retained",
        "object_path": str(original),
        "sha256": hashlib.sha256(data).hexdigest(),
        "origin_message_ts": "1760000000.000100",
        "conversion_state": "failed",
    }
    meta.update(over)
    return meta


def test_a_retained_human_uploaded_original_is_eligible(tmp_path):
    reason, detail = inv.classify(_Item(), _staged(tmp_path))

    assert reason == ""
    assert detail["sha256_matches"] is True


def test_tec_is_excluded_by_default(tmp_path):
    """범위 문서가 기본 제외로 못박았다."""
    reason, _ = inv.classify(_Item(workspace="tec"), _staged(tmp_path))

    assert reason == inv.REASON_WORKSPACE


def test_a_missing_original_is_not_a_recovery_target(tmp_path):
    """원본이 없으면 **Slack 에서 다시 받는 것이 선행**이다."""
    meta = _staged(tmp_path, original_state="missing")
    assert inv.classify(_Item(), meta)[0] == inv.REASON_NO_ORIGINAL

    meta = _staged(tmp_path)
    Path(meta["object_path"]).unlink()
    assert inv.classify(_Item(), meta)[0] == inv.REASON_NO_ORIGINAL


def test_a_changed_original_is_not_a_verified_original(tmp_path):
    """바이트가 바뀌었다. **「검증된 원본」 이 아니다.**"""
    meta = _staged(tmp_path, sha256="b" * 64)

    reason, detail = inv.classify(_Item(), meta)

    assert reason == inv.REASON_HASH_MISMATCH
    assert detail["sha256_matches"] is False


def test_an_original_without_a_hash_is_not_verified(tmp_path):
    """해시가 없으면 **바뀌지 않았다고 말할 수 없다.**"""
    assert inv.classify(_Item(), _staged(tmp_path, sha256=""))[0] == inv.REASON_HASH_MISMATCH


def test_an_attachment_without_message_provenance_is_excluded(tmp_path):
    """사람 메시지로 들어온 좌표가 없다. **여기서는 증명할 수 없다.**"""
    reason, _ = inv.classify(_Item(), _staged(tmp_path, origin_message_ts=""))

    assert reason == inv.REASON_NO_PROVENANCE


def test_a_policy_blocked_file_is_never_a_recovery_target(tmp_path):
    """정책으로 막힌 자료는 **승인으로 우회하지 않는다**(원칙 5)."""
    for over in ({"conversion_state": "blocked"}, {"status": "pii_refused"},
                 {"screen_result": "blocked"}):
        assert inv.classify(_Item(), _staged(tmp_path, **over))[0] == inv.REASON_BLOCKED


def test_every_exclusion_has_a_named_reason(tmp_path):
    """빈 칸은 「안 봤다」 와 「안 된다」 를 섞는다."""
    reasons = {
        inv.REASON_WORKSPACE, inv.REASON_NO_ORIGINAL, inv.REASON_HASH_MISMATCH,
        inv.REASON_NO_PROVENANCE, inv.REASON_BLOCKED,
    }

    assert len(reasons) == 5
    assert all(isinstance(r, str) and r for r in reasons)


# --- 후보 변환기 ------------------------------------------------------------------


def test_a_non_ole2_input_is_skipped_before_running_anything():
    """HWPX(zip)·PDF 를 HWP 변환기에 넣으면 엉뚱한 파서로 들어간다."""
    got = hc.run_candidate(
        hc.Candidate(name="x", command="true {src} {outdir}"), b"PK\x03\x04junk",
    )

    assert got.available is False
    assert "OLE2" in got.skipped
    assert got.exit_code is None, "실행하지 않았어야 한다"


def test_a_missing_converter_is_unavailable_not_failed(tmp_path):
    """**미설치는 실패가 아니다.** 설치하면 되는 일을 버그로 쫓게 하지 않는다."""
    got = hc.run_candidate(
        hc.Candidate(name="ghost", command="tybot-no-such-binary {src} {outdir}",
                     version_command="tybot-no-such-binary --version"),
        OLE2,
    )

    assert got.available is False
    assert got.skipped
    assert got.error == "", "미설치를 오류로 적으면 집계가 틀린다"


def test_a_converter_without_an_identity_is_refused():
    """신원을 못 세우면 쓰지 않는다. 변환기가 바뀌면 같은 원본이 다른 결과를 낸다."""
    quiet = hc.Candidate(
        name="quiet", command=f"{PY} -c pass {{src}} {{outdir}}",
        version_command=f"{PY} -c pass",
    )

    got = hc.run_candidate(quiet, OLE2)

    assert got.available is False
    assert "버전" in got.skipped


def test_a_candidate_that_makes_no_pdf_is_recorded_as_such(tmp_path):
    code = "import sys; sys.stderr.write('no export filter\\n'); sys.exit(1)"
    got = hc.run_candidate(hc.Candidate(
        name="broken",
        command=f"{PY} -c {json.dumps(code)} {{src}} {{outdir}}",
        version_command=f"{PY} --version",
    ), OLE2)

    assert got.available is True
    assert got.exit_code == 1
    assert got.error == "pdf_missing"
    assert "missing_filter" in got.stderr_markers


def test_candidate_stderr_never_carries_document_text():
    """변환기는 처리 중인 셀 값을 오류에 실어 보낸다."""
    leak = (
        "import sys; sys.stderr.write("
        "'Unsupported cell \"김해외동 기성금 3억 2천만원\"\\n'"
        "'at 「2026.09월말 송산서측 1-3공구」\\n'); sys.exit(2)"
    )
    got = hc.run_candidate(hc.Candidate(
        name="leaky",
        command=f"{PY} -c {json.dumps(leak)} {{src}} {{outdir}}",
        version_command=f"{PY} --version",
    ), OLE2)

    from dataclasses import asdict

    dumped = str(asdict(got))
    for secret in ("김해외동", "기성금", "송산서측", "3억", "2026.09"):
        assert secret not in dumped, f"{secret} 가 결과로 샜다"
    assert "unsupported" in got.stderr_markers, "표식은 남아야 진단이 된다"
    assert got.stderr_lines == 2


def test_the_version_banner_is_hashed_not_quoted():
    """버전 배너에 경로·사용자 이름이 섞여 나오는 변환기가 있다."""
    banner = "import sys; sys.stdout.write('Conv 1.0 /home/dan/secret-path\\n')"
    got = hc.run_candidate(hc.Candidate(
        name="chatty",
        command=f"{PY} -c pass {{src}} {{outdir}}",
        version_command=f"{PY} -c {json.dumps(banner)}",
    ), OLE2)

    assert got.identity.get("version_sha256")
    assert len(got.identity.get("version_line", "")) <= 80


def test_a_candidate_run_leaves_nothing_behind(tmp_path, monkeypatch):
    """읽기 전용이다. 작업 파일이 남으면 다음 측정이 앞 측정을 본다."""
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())

    hc.run_candidate(hc.Candidate(name="ghost", command="tybot-no-such {src} {outdir}"), OLE2)

    assert sorted(p.name for p in tmp_path.iterdir()) == before


# --- 후보 설정 --------------------------------------------------------------------


def test_the_default_candidate_set_is_local_only():
    """외부 서버로 올리는 것은 변환이 아니라 **전송**이다."""
    names = [c.name for c in hc.DEFAULT_CANDIDATES]

    assert names == ["libreoffice"]
    for candidate in hc.DEFAULT_CANDIDATES:
        for scheme in ("http://", "https://", "curl", "wget"):
            assert scheme not in candidate.command


def test_extra_candidates_come_from_configuration_not_code():
    """새 후보를 넣을 때 코드를 고치면 **이전 측정과 조건이 달라진다.**"""
    got = hc.configured_candidates(json.dumps([
        {"name": "h2orestart", "command": "soffice --convert-to pdf {src}",
         "version_command": "soffice --version", "output": "source.pdf"},
    ]))

    assert [c.name for c in got] == ["libreoffice", "h2orestart"]


def test_broken_candidate_configuration_falls_back_to_the_default():
    """설정이 깨졌다고 측정을 멈추지 않는다. 기본 기준선은 남는다."""
    assert hc.configured_candidates("not json") == hc.DEFAULT_CANDIDATES
    assert hc.configured_candidates('{"name": "x"}') == hc.DEFAULT_CANDIDATES
    assert hc.configured_candidates('[{"name": "x"}]') == hc.DEFAULT_CANDIDATES
    assert hc.configured_candidates("") == hc.DEFAULT_CANDIDATES


# --- 추출량 차이는 누락률이 아니다 --------------------------------------------------


def test_the_ratio_is_never_called_a_loss_rate():
    """「89% 잃는다」 로 쓰면 그 숫자가 손실률로 인계된다. **우리는 모른다.**"""
    source = Path("scripts/inventory_hwp_originals.py").read_text(encoding="utf-8")

    assert "loss_ratio" not in source
    assert "extracted_ratio" in source
    assert "누락률이 아니다" in source


def test_a_failed_direct_path_is_reported_not_raised():
    """지금 경로가 실패한 것이 복구 대상이다. 거기서 멈추면 대조가 안 된다."""
    got = hc.direct_extraction(b"\x00" * 16, "hwp")

    assert "failed" in got
