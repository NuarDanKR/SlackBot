"""첨부 진단 스크립트.

「봇이 첨부파일을 못 읽는다」 에는 **서로 다른 원인 넷**이 섞여 있고 넷 다 오류를
내지 않는다. 이 스크립트는 그 넷을 갈라 보인다 — 판정만 하고 아무것도 바꾸지 않는다.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnose_attachments.py"

CHANNEL = "#팀_전산(ABB155)_공지"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("diagnose_attachments", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _archive(tmp_path: Path, lines: list[str]) -> Path:
    body = "\n".join(f"> [2026-09-07 09:00] 홍길동: {line}" for line in lines)
    doc = (
        "---\n"
        "workspace: tyit\n"
        f'channel: "{CHANNEL}"\n'
        "channel_id: C1\n"
        "visibility: private\n"
        f'acl: ["{CHANNEL}"]\n'
        "last_ingested: 2026-09-07T17:00+09:00\n"
        "---\n\n"
        "## 요약 (사람이 관리, 봇은 수정 금지)\n-\n\n"
        "## 원문 (자동 취합, 편집 금지)\n"
        f"{body}\n"
    )
    path = tmp_path / "channels" / "tyit" / "t.md"
    path.parent.mkdir(parents=True)
    path.write_text(doc, encoding="utf-8")
    return tmp_path


# --- 네 가지 원인을 가른다 ---------------------------------------------------
def test_it_separates_converted_from_staged(mod, tmp_path, monkeypatch, capsys):
    """자동 변환된 것과 미지원 형식은 사람이 할 일이 다르다."""
    monkeypatch.setenv("ARCHIVE_DIR", str(_archive(tmp_path, [
        "[첨부:자동변환] 가정산서.xlsx (xlsx, 900KB)",
        "[첨부추출:가정산서.xlsx]",
        "[첨부:미지원] 스크린샷.png (png, 120KB)",
    ])))

    assert mod.main() == 0
    out = capsys.readouterr().out

    assert "[첨부:미지원] 1건" in out
    assert "[첨부:자동변환] 1건" in out
    assert "변환본이 들어간 파일: 1건" in out


def test_a_truncated_conversion_is_reported(mod, tmp_path, monkeypatch, capsys):
    """상한(400줄)을 넘으면 뒤가 없다. **표는 뒤에 합계가 있다.**

    그 값을 답변이 못 보는데, 답변은 정상적으로 나간다.
    """
    monkeypatch.setenv("ARCHIVE_DIR", str(_archive(tmp_path, [
        "[첨부추출:가정산서.xlsx]",
        "…(이하 생략, 총 1240줄)",
    ])))

    mod.main()
    out = capsys.readouterr().out

    assert "변환본이 잘린 문서" in out
    assert "합계" in out, "왜 문제인지 말해야 한다"


def test_an_empty_archive_says_so(mod, tmp_path, monkeypatch, capsys):
    """첨부가 한 번도 수집되지 않은 것과, 수집됐지만 못 읽은 것은 다른 문제다."""
    monkeypatch.setenv("ARCHIVE_DIR", str(_archive(tmp_path, ["평범한 대화입니다"])))

    mod.main()

    assert "첨부가 수집된 적이 없습니다" in capsys.readouterr().out


def test_a_missing_archive_is_an_error_not_an_empty_report(mod, tmp_path, monkeypatch):
    """경로가 틀린 것을 「첨부 없음」 으로 보고하면 조사가 엉뚱한 데로 간다."""
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "없는경로"))

    assert mod.main() == 1


# --- 아무것도 바꾸지 않는다 --------------------------------------------------
def test_it_only_diagnoses(mod):
    """승인·삭제·재작성을 하지 않는다. 판정과 조치를 섞으면 되돌릴 수 없다."""
    source = SCRIPT.read_text(encoding="utf-8")

    # **호출 모양만 본다.** 「approve」 를 문자열로 찾으면 `status=APPROVED` 같은
    # 읽기 상수까지 걸려, 검사가 잡음이 되고 그러면 아무도 안 본다.
    for forbidden in (
        "write_text(",
        "unlink(",
        "rmtree(",
        "os.remove(",
        "set_status(",
        "approve(",
        "mkdir(",
    ):
        assert forbidden not in source, f"진단 스크립트가 {forbidden} 를 쓴다"
