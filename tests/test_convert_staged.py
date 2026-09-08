"""이미 올라온 첨부를 지금 코드로 다시 변환한다 (2026-09-08).

변환은 **수집 시점에만** 돌았다. 그때 상한이 낮았거나 라이브러리가 없었거나 승인
대기로 남은 파일은, 코드를 고친 뒤에도 아카이브에 텍스트가 없다. 답변은 정상적으로
나가고 그 파일 내용만 없다.

여기서 지키는 것은 **원문을 고치지 않는 것**이다(원칙 1). 덧붙이기만 한다.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "convert_staged_attachments.py"

CHANNEL = "#팀-전산(ABB155)_공지"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("convert_staged_attachments", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _book(rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _archive(tmp_path: Path, lines: list[str], *, day="2026-09-07") -> Path:
    body = "\n".join(f"> [{day} 09:00] 홍길동: {line}" for line in lines)
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
    # `channel_dir` 이 만드는 것과 같은 모양이어야 덧붙이기가 같은 파일로 간다.
    from tybot.archive.writer import doc_path

    path = doc_path(tmp_path / "archive", "tyit", CHANNEL, channel_id="C1",
                    day=__import__("datetime").date.fromisoformat(day))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return tmp_path / "archive"


def _stage(tmp_path, *, name, file_id, data=b"x", ws="tyit", ch="C1"):
    suffix = f"workspaces/{ws}/channels/{ch}/attachments/{file_id}"
    staged = tmp_path / "staging" / suffix
    staged.mkdir(parents=True, exist_ok=True)
    obj = tmp_path / "objects" / suffix / name
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(data)
    (staged / "metadata.json").write_text(json.dumps({
        "schema_version": 1, "status": "pending_review", "slack_file_id": file_id,
        "name": name, "filetype": name.rsplit(".", 1)[-1], "mimetype": "",
        "declared_size": len(data), "object_path": str(obj),
        "staged_at": "2026-09-07T09:00:00+09:00",
    }, ensure_ascii=False), encoding="utf-8")


def _raw_block(path: Path) -> list[str]:
    """원문 블록의 줄만. 편집이 금지된 것은 여기다."""
    text = path.read_text(encoding="utf-8")
    _, _, raw = text.partition("## 원문")
    return [line for line in raw.splitlines() if line.startswith(">")]


def _run(mod, archive, monkeypatch, capsys, *args):
    monkeypatch.setenv("ARCHIVE_DIR", str(archive))
    code = mod.main(list(args))
    return code, capsys.readouterr().out


# --- 덧붙이기 -----------------------------------------------------------------
def test_it_fills_a_pending_document(mod, tmp_path, monkeypatch, capsys):
    """검수 대기로 남아 텍스트가 없던 파일이 채워진다."""
    pytest.importorskip("openpyxl")
    archive = _archive(tmp_path, [
        "[첨부:검수대기] 가정산서.xlsx (xlsx, 900KB)",
    ])
    _stage(tmp_path, name="가정산서.xlsx", file_id="F1",
           data=_book([["공구", "기성금"], ["3공구", 320000000]]))

    code, out = _run(mod, archive, monkeypatch, capsys, "--apply")

    assert code == 0
    assert "변환한 파일 1건" in out
    doc = next((archive).rglob("*.md")).read_text(encoding="utf-8")
    assert "[첨부추출:가정산서.xlsx]" in doc
    assert "320000000" in doc


def test_the_original_lines_are_untouched(mod, tmp_path, monkeypatch, capsys):
    """원문 블록은 편집 금지다(원칙 1). 덧붙이기만 한다."""
    pytest.importorskip("openpyxl")
    archive = _archive(tmp_path, [
        "회의합시다",
        "[첨부:검수대기] 표.xlsx (xlsx, 10KB)",
    ])
    _stage(tmp_path, name="표.xlsx", file_id="F1", data=_book([["가", 1]]))
    path = next((archive).rglob("*.md"))
    before = _raw_block(path)

    _run(mod, archive, monkeypatch, capsys, "--apply")

    after = _raw_block(path)
    # 금지된 것은 **원문 블록 편집**이다. 프론트매터의 `last_ingested` 는 메타이고
    # 갱신되는 것이 맞다 — 그것까지 고정하면 테스트가 규칙을 잘못 옮긴다.
    for line in before:
        assert line in after, f"원문 줄이 바뀌었다: {line}"
    assert len(after) > len(before), "덧붙여지지 않았다"


def test_running_twice_does_not_double_up(mod, tmp_path, monkeypatch, capsys):
    """운영자가 두 번 돌리는 일은 반드시 생긴다."""
    pytest.importorskip("openpyxl")
    archive = _archive(tmp_path, ["[첨부:검수대기] 표.xlsx (xlsx, 10KB)"])
    _stage(tmp_path, name="표.xlsx", file_id="F1", data=_book([["가", 1]]))

    _run(mod, archive, monkeypatch, capsys, "--apply")
    first = next((archive).rglob("*.md")).read_text(encoding="utf-8")
    _run(mod, archive, monkeypatch, capsys, "--apply")
    second = next((archive).rglob("*.md")).read_text(encoding="utf-8")

    assert first == second, "두 번째 실행이 같은 줄을 또 넣었다"


# --- 하지 않는 것 -------------------------------------------------------------
def test_it_does_nothing_without_apply(mod, tmp_path, monkeypatch, capsys):
    """판정과 조치를 섞으면 되돌릴 수 없다."""
    pytest.importorskip("openpyxl")
    archive = _archive(tmp_path, ["[첨부:검수대기] 표.xlsx (xlsx, 10KB)"])
    _stage(tmp_path, name="표.xlsx", file_id="F1", data=_book([["가", 1]]))
    path = next((archive).rglob("*.md"))
    before = path.read_text(encoding="utf-8")

    code, out = _run(mod, archive, monkeypatch, capsys)

    assert code == 0
    assert path.read_text(encoding="utf-8") == before
    assert "실제로 채우려면" in out


def test_an_image_is_reported_not_converted(mod, tmp_path, monkeypatch, capsys):
    """이미지는 로컬 변환으로 **한 글자도** 안 나온다. 세어 보이기만 한다."""
    archive = _archive(tmp_path, ["[첨부:검수대기] 스캔본.png (png, 120KB)"])
    _stage(tmp_path, name="스캔본.png", file_id="F1")

    code, out = _run(mod, archive, monkeypatch, capsys, "--apply")

    assert code == 0
    assert "변환 대상 아님(이미지·스캔)" in out
    assert "모델이" in out, "무엇이 필요한지 말해야 한다"


def test_an_already_converted_file_is_skipped(mod, tmp_path, monkeypatch, capsys):
    """이미 쓰이고 있는 파일을 다시 넣으면 같은 내용이 두 벌 쌓인다."""
    pytest.importorskip("openpyxl")
    archive = _archive(tmp_path, [
        "[첨부:변환·원본검수대기] 표.xlsx (xlsx, 10KB)",
        "[첨부추출:표.xlsx] 가 | 1",
    ])
    _stage(tmp_path, name="표.xlsx", file_id="F1", data=_book([["가", 1]]))

    code, out = _run(mod, archive, monkeypatch, capsys, "--apply")

    assert code == 0
    assert "이미 변환됨: 1건" in out


def test_a_missing_day_file_is_not_created(mod, tmp_path, monkeypatch, capsys):
    """없는 날 파일을 만들면 **권한을 짐작해** 문서를 세우는 셈이 된다.

    `writer.ingest` 는 기본값(비공개·빈 ACL)으로 새 문서를 만든다. 그건 이 채널의
    실제 권한이 아니다 — 답변 필터가 그 값을 그대로 믿는다.
    """
    pytest.importorskip("openpyxl")
    # 첨부 표시는 9월 7일 파일에 있는데, 스테이징만 있고 그 파일을 지운다.
    archive = _archive(tmp_path, ["[첨부:검수대기] 표.xlsx (xlsx, 10KB)"])
    _stage(tmp_path, name="표.xlsx", file_id="F1", data=_book([["가", 1]]))
    before = {p.name for p in archive.rglob("*.md")}
    next(archive.rglob("*.md")).unlink()

    code, _ = _run(mod, archive, monkeypatch, capsys, "--apply")

    assert code == 0
    after = {p.name for p in archive.rglob("*.md")}
    assert not after, f"없는 문서를 새로 만들었다: {after - before}"


def test_the_index_reminder_is_printed(mod, tmp_path, monkeypatch, capsys):
    """새 줄은 색인을 다시 만들어야 검색된다. 안 그러면 「넣었는데 못 찾는다」."""
    pytest.importorskip("openpyxl")
    archive = _archive(tmp_path, ["[첨부:검수대기] 표.xlsx (xlsx, 10KB)"])
    _stage(tmp_path, name="표.xlsx", file_id="F1", data=_book([["가", 1]]))

    _, out = _run(mod, archive, monkeypatch, capsys, "--apply")

    assert "tybot-index" in out
