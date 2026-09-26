"""변환기 신원 — **재변환도 최초 수집과 같은 것을 적는다.**

결정: 2026-09-26 오너 QA 2번.

정본 revision 은 원본 해시·변환기 이름·변환기 판·설정에서 나온다
(`attachment_doc.revision_for`). 최초 수집만 그 넷을 적고 재변환이 안 적으면,
더 나은 변환기의 결과가 **옛 신원으로 계산된 같은 경로**로 가고 writer 가
거절한다 — 사람에게는 「다시 돌렸는데 안 바뀐다」 로만 보인다.

DB 도 Slack 도 필요 없다. metadata.json 을 실제로 쓰고 무엇이 적히는지 본다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tybot.archive import attachment_doc
from tybot.archive.convert import (
    CONVERTER_VERSION,
    FALLBACK_CONVERTER,
    Coverage,
    conversion_stamp,
)

ROOT = Path(__file__).resolve().parent.parent
IDENTITY = ("converter_name", "converter_version", "converter_config")


def _script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _meta(tmp_path: Path, **over) -> Path:
    payload = {
        "schema_version": 1,
        "slack_file_id": "F1",
        "name": "주간보고.pptx",
        "filetype": "pptx",
        "sha256": "a" * 64,
        "conversion_state": "failed",
        "staged_at": "2026-09-23T10:00:00+00:00",
    }
    payload.update(over)
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _revision_of(meta_path: Path) -> str:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return attachment_doc.revision_for(
        sha256=str(meta.get("sha256") or ""),
        converter_name=str(meta.get("converter_name") or ""),
        converter_version=str(meta.get("converter_version") or ""),
        config=meta.get("converter_config") or {},
        staged_at=str(meta.get("staged_at") or ""),
    )


# --- 공통 함수 -------------------------------------------------------------------

def test_the_fallback_is_a_different_converter():
    """폴백은 정밀 변환과 동등하지 않다(설계 §6). 같은 이름이면 같은 판이 된다."""
    fallback = Coverage(flags=(FALLBACK_CONVERTER,))

    assert conversion_stamp("pptx", None, produced=True)["converter_name"] == "pptx:primary"
    assert conversion_stamp("pptx", fallback, produced=True)["converter_name"] == "pptx:fallback"


def test_a_failed_attempt_gets_no_conversion_time():
    """실패한 시도를 「이때 변환됨」 으로 적으면 최신 판정이 실패 쪽으로 기운다."""
    stamp = conversion_stamp("pptx", None, produced=False)

    assert "converted_at" not in stamp
    assert stamp["converter_version"] == CONVERTER_VERSION


# --- 재변환 경로 -----------------------------------------------------------------

@pytest.mark.parametrize("which", ["drain", "convert_staged"])
def test_a_successful_reconversion_records_the_converter(tmp_path, which):
    """두 재변환 경로 **둘 다** 적어야 한다. 하나만 고치면 그쪽만 조용히 겹친다."""
    meta_path = _meta(tmp_path)
    _reconvert(which, meta_path, Coverage(unit="slide", total=3, converted=3))

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert [key for key in IDENTITY if key not in meta] == []
    assert meta["converter_name"] == "pptx:primary"
    assert meta["converted_at"], "변환이 끝난 시각이 있어야 최신 판정이 선다"


@pytest.mark.parametrize("which", ["drain", "convert_staged"])
def test_a_fallback_success_gets_its_own_revision(tmp_path, which):
    """primary 가 실패하고 fallback 이 성공한 판은 **다른 판이다.**

    같은 revision 이 되면 정밀 변환본과 폴백 변환본이 한 경로를 두고 다투고,
    먼저 쓴 쪽이 남는다 — 어느 쪽인지는 실행 순서가 정한다.
    """
    (tmp_path / "a").mkdir(exist_ok=True)
    (tmp_path / "b").mkdir(exist_ok=True)
    primary = _meta(tmp_path / "a")
    fallback = _meta(tmp_path / "b")

    _reconvert(which, primary, Coverage(unit="slide", total=3, converted=3))
    _reconvert(which, fallback, Coverage(flags=(FALLBACK_CONVERTER,)))

    assert _revision_of(primary) != _revision_of(fallback)
    # 폴백은 **부분 변환본**이다 — 다 읽은 것으로 닫으면 없는 내용을 「없다」 고 답한다.
    assert json.loads(fallback.read_text(encoding="utf-8"))["conversion_state"] == "partial"


def test_the_first_stage_and_a_reconversion_agree_on_the_identity(tmp_path):
    """수집과 재변환이 **같은 신원 표기**를 써야 revision 이 이어진다."""
    meta_path = _meta(tmp_path)
    _reconvert("drain", meta_path, Coverage(unit="slide", total=3, converted=3))

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    fresh = conversion_stamp("pptx", None, produced=True)

    assert meta["converter_name"] == fresh["converter_name"]
    assert meta["converter_version"] == fresh["converter_version"]
    assert meta["converter_config"] == fresh["converter_config"]


def _reconvert(which: str, meta_path: Path, coverage: Coverage) -> None:
    """두 스크립트의 「성공으로 닫는」 지점을 같은 모양으로 부른다."""
    body = ["표지", "1분기 집행률 72%"]
    if which == "drain":
        drain = _script("drain_conversion_queue")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        drain._write_meta(
            meta_path, meta, status="converted", code="", body=body, coverage=coverage,
        )
        return

    convert_staged = _script("convert_staged_attachments")
    item = type("_Item", (), {
        "meta_path": meta_path, "name": "주간보고.pptx", "object_path": "",
    })()
    convert_staged._record_result(item, status="converted", body=body, coverage=coverage)
