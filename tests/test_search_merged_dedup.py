"""일자 파일을 합친 문서에서 **검색이 줄을 잃지 않는지.**

`_merge()` 는 한 채널의 일자 파일을 문서 하나로 합치면서 모든 줄에 같은
`doc.path`(가장 최근 파일)를 달아 준다. 그런데 `lineno` 는 파일마다 1부터 다시
세므로, **다른 날의 다른 줄이 `(doc.path, lineno)` 로는 같은 키**가 된다.
`_rank` 가 그 키로 중복을 지우고 있었기 때문에 둘 중 하나가 검색 결과에서 조용히
빠졌다.

오류도 0건도 아니고 「그 말은 없었다」 로만 보이는 종류의 실패라, 사람이 눈치채기
어렵다. 실측(PF 자료 43채널 2061줄)에서 **1750줄**이 다른 줄과 키가 겹쳤다.

발견 경위: 저장 구조 동등성 시험(`test_archive_layout_equivalence.py`)에서
구조 1 은 2건, 구조 2 는 1건이 나왔다. 구조 차이가 아니라 구조 2 쪽 버그였다.
"""

from __future__ import annotations

from tybot.access import RequestContext
from tybot.archive.store import ArchiveStore

CTX = RequestContext(workspace="pilot")

HEAD = """---
schema_version: 2
workspace: pilot
channel: "#팀_자금(ABB540)_주간보고"
channel_id: C0FUND
source_date: {day}
visibility: public
acl: []
share_with: []
doc_count: 1
last_ingested: 2026-09-{n:02d}T17:00+09:00
---

## 원문 (자동 취합, 편집 금지)
"""


def _write(tmp_path, day: str, n: int, lines: list[str]):
    d = tmp_path / "workspaces" / "pilot" / "channels" / "C0FUND__주간보고" / "raw"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.md").write_text(
        HEAD.format(day=day, n=n) + "\n".join(lines) + "\n", encoding="utf-8"
    )


def test_same_lineno_in_different_day_files_both_survive_search(tmp_path):
    """서로 다른 날의 **첫 줄**이 둘 다 검색에 나온다."""
    _write(tmp_path, "2026-09-03", 3, ["> [2026-09-03 10:00] 전산팀: 서버 점검 예정"])
    _write(tmp_path, "2026-09-04", 4, ["> [2026-09-04 10:00] 전산팀: 서버 점검 예정"])

    hits = ArchiveStore(tmp_path).search("서버 점검", CTX, limit=20)

    assert len(hits) == 2, "같은 lineno 라는 이유로 한 줄이 사라졌다"
    assert {hit.line.ts for hit in hits} == {"2026-09-03 10:00", "2026-09-04 10:00"}


def test_genuinely_duplicated_hit_is_still_collapsed(tmp_path):
    """진짜 같은 줄은 여전히 한 번만 나온다.

    이 중복 제거가 왜 있는지 잊지 않기 위한 짝 시험이다 — 색인 결과와 낡은 문서
    파일 스캔이 겹칠 때 같은 사실이 두 번 인용되는 것을 막는다. 키를
    `source_path` 로 바꾼 뒤에도 그 보호가 남아 있어야 한다.
    """
    _write(
        tmp_path,
        "2026-09-03",
        3,
        [
            "> [2026-09-03 10:00] 전산팀: 서버 점검 예정",
            "> [2026-09-03 11:00] 전산팀: 서버 점검 완료",
        ],
    )
    store = ArchiveStore(tmp_path)
    docs = store.visible_docs(CTX)
    hits = store._scan("서버 점검", ["서버", "점검"], docs, 20)
    # 같은 문서를 두 번 넣어도(색인 + 파일 스캔이 겹친 모양) 건수는 그대로다.
    doubled = store._rank(hits + hits, 20)

    assert len(doubled) == len(hits) == 2
