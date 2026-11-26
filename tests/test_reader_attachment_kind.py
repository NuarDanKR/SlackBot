"""채널 관리 수집량 — 첨부 종류 판정."""
from __future__ import annotations

import pytest

from tybot.console.reader import attachment_kind


@pytest.mark.parametrize(("line", "kind"), [
    ("[첨부:자동변환] 보고서.hwp (hwp, 240KB) · <https://x|원본 파일> · id:F1", "document"),
    ("[첨부:변환] [주간업무보고] 2026.09.10 (최종).pdf (pdf, 1200KB)", "document"),
    ("[첨부:검수대기] 현장사진.jpg (jpg, 3000KB) · <https://x|원본 파일>", "image"),
    ("[첨부:미지원] 화면.heic (image/heic, 10KB)", "image"),
    ("[첨부:미지원] 영상.mp4 (mp4, 50000KB)", "other"),
    ("[첨부:수집제외] 구글문서 (gdoc)", "document"),
    ("[첨부:처리실패] 이름없음 (?, 0KB)", "other"),
])
def test_attachment_kind(line, kind):
    assert attachment_kind(line) == kind


def test_canvas_counts_as_attachment_not_talk(tmp_path, monkeypatch):
    """캔버스 줄은 대화 1건이 아니라 첨부(캔버스)로 센다(2026-11-26)."""
    from tybot.archive.store import ArchiveStore
    from tybot.console import reader

    raw = tmp_path / "workspaces" / "fin" / "channels" / "C1" / "raw"
    raw.mkdir(parents=True)
    (raw / "2026-11-20.md").write_text("""---
workspace: fin
channel: "#팀_자금_주간보고"
channel_id: C1
visibility: private
acl: [#팀_자금_주간보고]
schema_version: 2
source_date: 2026-11-20
last_ingested: 2026-11-20T10:00:00+09:00
---

## 원문 (자동 취합, 편집 금지)
> [2026-11-20 09:00|1795000000.000100] 김수현: 회의록 올립니다
> [2026-11-20 09:01|1795000060.000100] 김수현: [첨부:변환] 회의록.docx (docx, 20KB)
> [2026-11-20 09:02|1795000120.000100] 이순신: 현장사진 공유
> [2026-11-20 09:02|1795000120.000100] 이순신: [첨부:검수대기] 사진.png (png, 900KB)
> [2026-11-20 09:05] 김수현: [캔버스:수집] 주간 정리 [수집키:k1]
> [2026-11-20 09:05] 김수현: [캔버스본문:주간 정리] 첫 줄
> [2026-11-20 09:05] 김수현: [캔버스본문:주간 정리] 둘째 줄
""", encoding="utf-8")
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path))
    docs = [d for d in reader.collected_docs(ArchiveStore(tmp_path)) if d.get("channelId") == "C1"]
    assert docs, ArchiveStore(tmp_path).broken()
    doc = docs[0]
    assert doc["messages"] == 2
    assert doc["attachmentKinds"] == {"document": 1, "image": 1, "canvas": 1, "other": 0}
    assert doc["attachments"] == 3
