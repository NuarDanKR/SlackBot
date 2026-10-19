"""HWP 변환 실패 진단의 **판정 부분** (B-80).

진단 스크립트라 서버에서 돌지만, 판정 자체는 순수 함수다. 이 부분이 틀리면
사람이 엉뚱한 원인을 고치러 간다 — 특히 아래 둘.

1. **확장자와 실제 컨테이너를 가른다.** `.hwp` 로 올라온 것이 실제로는 HWPX(zip)
   이거나 PDF 인 경우가 있고, 그때 kordoc 은 엉뚱한 파서로 들어가 터진다.
   이름만 바꾼 것은 변환이 아니다.
2. **`converter_crashed` 를 한 원인으로 뭉개지 않는다.** SIGKILL(대개 OOM)과
   SIGSEGV(파서가 터짐)는 사람이 할 일이 전혀 다르다.

실제 사내 HWP 는 fixture 로 넣지 않는다. 컨테이너 **머리 바이트**만 합성한다.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _module():
    path = ROOT / "scripts" / "diagnose_hwp_conversion.py"
    spec = importlib.util.spec_from_file_location("diagnose_hwp_conversion", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["diagnose_hwp_conversion"] = module
    spec.loader.exec_module(module)
    return module


dx = _module()


# --- 컨테이너 판별 --------------------------------------------------------------


def _ole2(*, hwp: bool = True) -> bytes:
    body = dx.OLE2_MAGIC + b"\x00" * 40
    return body + (dx.HWP5_SIGNATURE + b"\x00" * 15 + b"\x00" * 8 if hwp else b"\x00" * 40)


def test_a_real_hwp5_is_recognised():
    data = _ole2()
    assert dx.sniff_container(data[:4096], data) == "hwp5"


def test_an_ole2_without_the_hwp_signature_is_not_called_hwp():
    """doc·xls 도 OLE2 다. **서명이 없으면 HWP 라고 하지 않는다.**"""
    data = _ole2(hwp=False)
    assert dx.sniff_container(data[:4096], data) == "ole2"


def test_an_hwpx_renamed_to_hwp_is_caught():
    """이름만 바꾼 것은 변환이 아니다 — 그 사실이 보여야 한다."""
    data = b"PK\x03\x04" + b"\x00" * 30 + b"Contents/section0.xml"
    assert dx.sniff_container(data[:4096], data) == "hwpx"


def test_a_plain_zip_is_not_called_hwpx():
    data = b"PK\x03\x04" + b"\x00" * 30 + b"photos/a.jpg"
    assert dx.sniff_container(data[:4096], data) == "zip"


def test_an_old_hwp3_is_told_apart():
    """HWP 3.0 은 OLE2 가 아니다. 같은 경로로 보내면 파서가 터진다."""
    assert dx.sniff_container(dx.HWP3_SIGNATURE + b"\x1a\x01\x02") == "hwp3"


def test_a_pdf_renamed_to_hwp_is_caught():
    assert dx.sniff_container(b"%PDF-1.7\n%\xe2\xe3") == "pdf"


def test_an_unknown_head_stays_unknown():
    """모르면 모른다고 한다. 찍으면 그 찍은 값으로 사람이 움직인다."""
    assert dx.sniff_container(b"\x00\x01\x02\x03" * 16) == "unknown"


def test_every_container_code_has_a_label():
    for code in ("hwp5", "hwpx", "hwp3", "zip", "ole2", "pdf", "unknown"):
        assert dx.CONTAINERS[code]


# --- HWP 5 속성 비트 -------------------------------------------------------------


def _hwp5_with_flags(value: int) -> bytes:
    head = dx.OLE2_MAGIC + b"\x00" * 64
    header = dx.HWP5_SIGNATURE + b"\x00" * (32 - len(dx.HWP5_SIGNATURE))
    header += b"\x05\x00\x03\x00"  # 버전 4바이트
    header += value.to_bytes(4, "little")
    return head + header + b"\x00" * 64


def test_a_password_protected_document_is_flagged():
    """암호 문서는 **어떤 변환기도 못 연다.** 크래시 원인과 별개로 복구 불가다."""
    flags, why = dx.hwp5_flags(_hwp5_with_flags(1 << 1))

    assert why == ""
    assert flags["password"] is True
    assert flags["distribution"] is False


def test_a_distribution_document_is_flagged():
    flags, _why = dx.hwp5_flags(_hwp5_with_flags(1 << 2))
    assert flags["distribution"] is True


def test_a_plain_compressed_document_is_not_locked():
    flags, _why = dx.hwp5_flags(_hwp5_with_flags(1))
    assert flags["compressed"] is True
    assert not any(flags[k] for k in ("password", "distribution", "drm"))


def test_a_missing_signature_says_so_instead_of_guessing():
    """**정식 OLE2 파싱이 아니다.** 못 읽으면 못 읽었다고 말한다."""
    flags, why = dx.hwp5_flags(b"\x00" * 200)

    assert flags == {}
    assert why


def test_a_truncated_header_says_so():
    flags, why = dx.hwp5_flags(dx.HWP5_SIGNATURE + b"\x00" * 4)
    assert flags == {} and why


# --- 종료 판정 -----------------------------------------------------------------


def _report(**over) -> dx.RunReport:
    base = {"ran": True, "exit_code": 0}
    base.update(over)
    return dx.RunReport(**base)


def test_sigkill_does_not_name_a_culprit():
    """바깥에서 죽인 것은 맞지만 **누가** 죽였는지는 여기서 모른다."""
    said = dx._verdict(_report(exit_code=-9, signal_name="SIGKILL"))

    assert "SIGKILL" in said
    assert "알 수 없다" in said
    assert "journalctl" in said, "확인할 자리는 줘야 한다"
    assert "OOM" not in said, "추정을 사실처럼 인계하면 안 된다"


def test_sigsegv_is_reported_without_diagnosing_the_file():
    """파일 탓인지 변환기 탓인지 **이 실행으로는 모른다.**"""
    said = dx._verdict(_report(exit_code=-11, signal_name="SIGSEGV"))

    assert "SIGSEGV" in said
    for claim in ("파일 구조 문제", "변환기 버그", "메모리가 아니다"):
        assert claim not in said


def test_sigabrt_is_told_apart():
    assert "SIGABRT" in dx._verdict(_report(exit_code=-6, signal_name="SIGABRT"))


def test_a_v8_fatal_is_reported_as_a_marker_not_a_cause():
    """표식은 **있었다**는 사실이다. 원인이 아니다."""
    said = dx._verdict(_report(exit_code=1, stderr_codes=("v8_fatal", "js_heap_oom")))

    assert "v8_fatal" in said and "js_heap_oom" in said
    for claim in ("OOM 이다", "메모리가 모자", "원인은"):
        assert claim not in said


def test_a_timeout_says_we_stopped_it():
    """우리가 끊은 것이다. 변환기가 스스로 죽은 것과 다르다."""
    said = dx._verdict(_report(exit_code=-9, signal_name="SIGKILL", timed_out=True))

    assert "중단했다" in said
    assert "SIGKILL" not in said, "우리가 보낸 신호를 변환기 종료로 읽으면 안 된다"


def test_success_does_not_explain_the_past_failure():
    """**이번 실행만 본다.** 그때 무엇이었는지는 그때 로그에만 있다."""
    said = dx._verdict(_report(exit_code=0))

    assert "exit 0" in said
    assert "과거 실패의 원인은 알려 주지 않는다" in said
    for claim in ("환경이 다르", "일시적", "지금은 변환된다"):
        assert claim not in said


# --- 렌더링본 검사 ---------------------------------------------------------------


def _pdf(pages: int) -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(max(pages, 0)):
        writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_a_rendered_pdf_is_counted_not_assumed(tmp_path):
    """「LibreOffice 가 돌았다」 와 「쪽이 나왔다」 는 다르다."""
    path = tmp_path / "render.pdf"
    path.write_bytes(_pdf(3))

    got = dx.inspect_pdf(path)

    assert got["exists"] is True
    assert got["pages"] == 3
    assert got["text_pages"] == 0, "빈 쪽을 글자 있는 쪽으로 세면 안 된다"


def test_a_missing_pdf_is_reported_as_missing(tmp_path):
    assert dx.inspect_pdf(tmp_path / "none.pdf") == {"exists": False}


def test_a_broken_pdf_does_not_crash_the_diagnosis(tmp_path):
    """진단이 터지면 아무것도 모른 채로 끝난다."""
    path = tmp_path / "render.pdf"
    path.write_bytes(b"not a pdf at all")

    got = dx.inspect_pdf(path)

    assert got["exists"] is True
    assert "error" in got


# --- 미설치 경로 -----------------------------------------------------------------


def test_a_missing_converter_is_not_a_crash(tmp_path, monkeypatch):
    """**미설치와 크래시를 가른다.** 설치하면 되는 일을 버그로 쫓게 하지 않는다."""
    monkeypatch.setattr(dx, "_binary", lambda *a: ("", "ExternalConverterUnavailable: none"))

    got = dx.check_kordoc(b"\x00" * 16, "hwp", tmp_path)

    assert got.ran is False
    assert "미설치" in got.skipped
    assert "크래시가 아니라" in got.verdict


def test_a_missing_converter_never_writes_anything(tmp_path, monkeypatch):
    """조회 전용이다. 변환기가 없을 때도 작업 파일을 남기지 않는다."""
    monkeypatch.setattr(dx, "_binary", lambda *a: ("", "none"))

    dx.check_kordoc(b"\x00" * 16, "hwp", tmp_path)

    assert list(tmp_path.iterdir()) == []


# --- 원본이 없을 때 ---------------------------------------------------------------


class _Item:
    workspace = "tyit"
    channel_id = "C0C9A86JUSJ"
    file_id = "F0TEST0001"
    filetype = "hwp"
    size = 123
    sha256 = "a" * 64
    conversion_state = "failed"
    error_code = "converter_crashed"
    original_state = "missing"
    object_path = None


def test_a_missing_original_stops_the_diagnosis_instead_of_guessing():
    """없는 것을 추측하지 않는다. 재현할 수 없으면 재현할 수 없다고 적는다."""
    row = dx.diagnose(_Item(), render=True)

    assert "blocked" in row
    assert "kordoc" not in row, "원본 없이 변환기를 돌리면 엉뚱한 결과가 나온다"
    assert row["error_code"] == "converter_crashed"


def test_the_report_never_carries_document_body():
    """진단 결과에 본문이 들어가면 운영 로그로 업무 내용이 복사된다(원칙 5)."""
    row = dx.diagnose(_Item(), render=False)

    assert set(row) <= {
        "workspace", "channel_id", "file_id", "filetype", "declared_size",
        "sha256", "conversion_state", "error_code", "original_state", "blocked",
    }


# --- stderr 에 문서 내용이 섞여 와도 새지 않는다 -------------------------------
#
# 변환기는 처리 중인 문단·셀 값을 오류 메시지에 그대로 실어 보낸다. 꼬리 몇 줄만
# 남기는 것으로는 부족하다 — **어디를 자르느냐가 운이 된다.**

LEAKY_STDERR = (
    "FATAL ERROR: Reached heap limit\n"
    '  while parsing cell "김해외동 기성금 3억 2천만원"\n'
    "  at row 「2026.09월말 기준 송산서측 1-3공구」\n"
    "V8_Fatal\n"
)

SECRETS = ("김해외동", "기성금", "송산서측", "3억", "2026.09")


def test_the_raw_stderr_never_enters_the_report():
    """**원시 stderr 를 싣지 않는다.** 표식만 있나 없나로 바꾼다."""
    codes = dx.stderr_codes(LEAKY_STDERR)

    assert "v8_fatal" in codes
    joined = " ".join(codes)
    for secret in SECRETS:
        assert secret not in joined


def test_the_verdict_never_carries_stderr_text():
    """판정 문장에도 섞이면 안 된다 — 그 문장이 인계 문서로 간다."""
    said = dx._verdict(dx.RunReport(
        ran=True, exit_code=1, stderr_codes=dx.stderr_codes(LEAKY_STDERR),
        stderr_lines=len(LEAKY_STDERR.splitlines()), stderr_chars=len(LEAKY_STDERR),
    ))

    for secret in SECRETS:
        assert secret not in said


def test_the_printed_report_never_carries_stderr_text(capsys):
    """출력 전체를 본다. 한 자리만 새도 로그에 남는다."""
    dx.report([{
        "workspace": "tyit", "channel_id": "C0C9A86JUSJ", "file_id": "F0TEST0001",
        "filetype": "hwp", "declared_size": 30208, "actual_bytes": 30208,
        "conversion_state": "failed", "error_code": "converter_crashed",
        "container": "hwp5", "container_label": dx.CONTAINERS["hwp5"],
        "extension_matches": True,
        "kordoc": {
            "ran": True, "exit_code": 1, "signal_name": "", "timed_out": False,
            "stderr_codes": list(dx.stderr_codes(LEAKY_STDERR)),
            "stderr_lines": 4, "stderr_chars": len(LEAKY_STDERR),
            "children_max_rss_mb": 66.0, "seconds": 2.1, "outputs": [],
            "verdict": "exit=1 로 끝났다(신호 아님). stderr 표식: v8_fatal",
        },
    }])
    said = capsys.readouterr().out

    for secret in SECRETS:
        assert secret not in said
    assert "v8_fatal" in said, "표식은 남아야 진단이 된다"
    assert "4줄" in said, "모양은 남긴다 — 「뭔가 나오긴 했다」 를 알아야 한다"


def test_an_unknown_stderr_yields_no_codes_and_no_text():
    """모를수록 **더** 조심한다. 모를 때 통째로 싣는 것이 가장 위험하다."""
    assert dx.stderr_codes("기성금 3억 2천만원 처리 중 알 수 없는 오류") == ()


def test_the_rss_is_labelled_as_a_cumulative_child_maximum(capsys):
    """**한 호출의 사용량이 아니다.** 라벨이 없으면 그 숫자가 OOM 판단에 쓰인다."""
    dx._print_run("kordoc", {
        "ran": True, "exit_code": 0, "children_max_rss_mb": 66.0, "seconds": 1.0,
        "stderr_codes": [], "stderr_lines": 0, "stderr_chars": 0, "verdict": "-",
    })
    said = capsys.readouterr().out

    assert "66.0MB" in said
    assert "이 호출 단독 사용량 아님" in said


# --- 추출 손실 대조 (2026-10-19 실측) -------------------------------------------
#
# kordoc 직접 추출 4,074자 vs 렌더 PDF 경로 47,140자 — **11배**였다. 표는 10행
# 대 395행. 그 상태로 재변환하면 11%짜리 문서가 「변환 성공」 으로 정본이 된다.


def test_the_stats_count_shape_not_content():
    """진단 결과에 본문이 들어가면 운영 로그로 업무 내용이 복사된다(원칙 5)."""
    got = dx._stats([
        "[1쪽]", "| 항목 | 금액 |", "| --- | --- |", "| 기성 | 3.2억 |", "본문 줄",
    ])

    assert got == {"lines": 5, "chars": 45, "table_rows": 3, "page_marks": 1}
    assert all(isinstance(v, int) for v in got.values()), "값은 숫자뿐이다"


def test_the_extracted_ratio_is_reported_when_both_paths_ran(monkeypatch, tmp_path):
    """**추출량 차이**다. 누락률이 아니다 — 어느 쪽이 맞는지는 모른다."""
    lo = tmp_path / "lo-out"
    lo.mkdir()
    (lo / "render.pdf").write_bytes(_pdf(3))

    monkeypatch.setattr(dx, "check_libreoffice",
                        lambda *a, **k: (dx.RunReport(exit_code=0),
                                         {"exists": True, "pages": 3, "text_pages": 3}))

    calls = []

    def fake_convert(kind, data):
        calls.append(kind)
        lines = ["짧다"] if kind == "hwp" else ["길다" * 50]
        return lines, type("C", (), {"state": "partial", "converted": 2,
                                     "total": 3, "unit": "page", "missing": (3,),
                                     "flags": ()})()

    import tybot.archive.convert as cv

    monkeypatch.setattr(cv, "convert_with_coverage", fake_convert)

    got = dx.compare_extraction(b"\x00" * 8, "hwp", tmp_path)

    assert calls == ["hwp", "pdf"], "두 경로를 다 돌려야 대조가 된다"
    assert got["extracted_ratio"] == 50.0
    assert got["via_pdf"]["coverage"] == "2/3page"
    assert got["render"]["sha256"], "렌더링본 해시는 승인 키의 일부다"


def test_a_failed_direct_path_does_not_stop_the_comparison(monkeypatch, tmp_path):
    """지금 경로가 실패한 것이 이번 두 건이다. 거기서 멈추면 대조가 아예 안 된다."""
    monkeypatch.setattr(dx, "check_libreoffice",
                        lambda *a, **k: (dx.RunReport(exit_code=1), {"exists": False}))

    import tybot.archive.convert as cv

    def boom(kind, data):
        raise cv.ConvertError("HWP 변환 실패")

    monkeypatch.setattr(cv, "convert_with_coverage", boom)

    got = dx.compare_extraction(b"\x00" * 8, "hwp", tmp_path)

    assert got["direct"]["failed"] == "ConvertError"
    assert "extracted_ratio" not in got, "한쪽이 없으면 비율을 만들지 않는다"


def test_the_ratio_is_never_called_a_loss_rate():
    """「89% 잃는다」 로 쓰면 그 숫자가 손실률로 인계된다. **우리는 모른다.**

    PDF 경로는 Markdown 표의 `|` 와 구분선을 더하므로 같은 내용이어도 글자가
    는다. 반대로 지금 경로가 중복을 접고 있을 수도 있다.
    """
    import inspect

    source = inspect.getsource(dx)

    assert "loss_ratio" not in source
    assert "extracted_ratio" in source
    assert "누락률이 아니다" in source


def test_missing_pages_keep_their_numbers_not_just_a_count(monkeypatch, tmp_path):
    """개수만 남기면 **어느 쪽을 열어 봐야 하는지** 모른다."""
    lo = tmp_path / "lo-out"
    lo.mkdir()
    (lo / "render.pdf").write_bytes(_pdf(4))
    monkeypatch.setattr(dx, "check_libreoffice",
                        lambda *a, **k: (dx.RunReport(exit_code=0),
                                         {"exists": True, "pages": 4, "text_pages": 2}))

    import tybot.archive.convert as cv

    def fake(kind, data):
        cov = type("C", (), {"state": "partial", "converted": 2, "total": 4,
                             "unit": "page", "missing": (2, 4), "flags": ()})()
        return ["본문"], cov

    monkeypatch.setattr(cv, "convert_with_coverage", fake)

    got = dx.compare_extraction(b"\x00" * 8, "hwp", tmp_path)

    assert got["via_pdf"]["missing_pages"] == [2, 4]
    assert got["via_pdf"]["missing_page_count"] == 2
    # 각 쪽이 어떤 쪽인지도 센다. **무엇이 그려져 있는지는 단정하지 않는다.**
    kinds = got["via_pdf"]["page_kinds"]
    assert set(kinds) == {"2", "4"}
    assert all("글자" in v and "이미지" in v for v in kinds.values())


def test_the_dump_is_written_to_disk_not_printed(tmp_path, capsys):
    """대조용 산출물은 **사내 문서 본문**이다. 화면에 찍지 않는다(원칙 5)."""
    made = dx.dump_for_review(
        tmp_path / "dump", "F0TEST0001",
        direct=["김해외동 기성금 3억"], via_pdf=["| 기성 | 3.2억 |"],
        rendered=b"%PDF-1.4 fake",
    )
    said = capsys.readouterr().out

    assert len(made) == 3
    assert said == "", "덤프 내용이 화면으로 나갔다"
    assert "김해외동" in (tmp_path / "dump" / "F0TEST0001.direct.md").read_text("utf-8")


def test_dump_never_overwrites_existing_files(tmp_path):
    directory = tmp_path / "dump"
    directory.mkdir(mode=0o700)
    existing = directory / "F0TEST0001.direct.md"
    existing.write_bytes(b"previous content")
    with pytest.raises(FileExistsError):
        dx.dump_for_review(directory, "F0TEST0001", direct=["new"], via_pdf=[], rendered=None)
    assert existing.read_bytes() == b"previous content"


def test_dump_rejects_path_components_as_file_ids(tmp_path):
    with pytest.raises(ValueError):
        dx.dump_for_review(tmp_path / "dump", "../outside", direct=["body"], via_pdf=[], rendered=None)
    assert not (tmp_path / "dump").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode enforcement needs a Unix filesystem")
def test_dump_creates_private_directory_and_files(tmp_path):
    directory = tmp_path / "dump"
    paths = dx.dump_for_review(directory, "F0TEST0001", direct=["body"], via_pdf=[], rendered=b"pdf")
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(Path(path).stat().st_mode & 0o777 == 0o600 for path in paths)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode enforcement needs a Unix filesystem")
def test_dump_refuses_a_shared_existing_directory(tmp_path):
    directory = tmp_path / "dump"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(PermissionError):
        dx.dump_for_review(directory, "F0TEST0001", direct=["body"], via_pdf=[], rendered=None)
    assert not list(directory.iterdir())


def test_dump_rejects_symlinks_without_touching_the_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "dump"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    with pytest.raises(ValueError):
        dx.dump_for_review(link, "F0TEST0001", direct=["body"], via_pdf=[], rendered=None)
    assert not list(target.iterdir())


def test_a_converter_that_prints_document_text_to_stderr_does_not_leak(tmp_path):
    """**진짜 하위 프로세스로 돌린다.**

    `stderr_codes()` 만 직접 부르는 시험은 `run_converter` 가 원시 stderr 를 다시
    싣기 시작해도 통과한다 — 실제로 그 변이를 못 잡았다. 변환기가 문단·셀 값을
    오류로 뱉는 상황을 그대로 만든다.
    """
    import sys

    leak = f"import sys; sys.stderr.write({LEAKY_STDERR!r}); sys.exit(1)"
    compile(leak, "<stderr-fixture>", "exec")
    report = dx.run_converter(
        [sys.executable, "-c", leak], cwd=tmp_path,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )

    assert report.exit_code == 1
    assert "v8_fatal" in report.stderr_codes
    assert report.stderr_chars == len(LEAKY_STDERR.strip()) and report.stderr_lines == 4

    everywhere = " ".join([
        *report.stderr_codes, report.verdict, report.skipped,
        str(report.stderr_chars), str(report.stderr_lines),
    ])
    for secret in SECRETS:
        assert secret not in everywhere, f"{secret} 가 보고서로 샜다"

    # dataclass 전체를 훑는다. 새 필드가 늘어도 이 시험이 받친다.
    from dataclasses import asdict

    dumped = str(asdict(report))
    for secret in SECRETS:
        assert secret not in dumped, f"{secret} 가 RunReport 에 남았다"
