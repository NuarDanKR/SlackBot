"""웹 콘솔 작업 보드의 현황판 표를 **항목에서 만든다.**

표를 손으로 적으면 「표에 적힌 상태」 와 「항목에 적힌 상태」 가 두 곳이 되고,
두 곳은 반드시 갈라진다. 이 프로젝트가 반복해 데인 모양이다 — 오류는 안 나고
숫자만 어긋난다. 그래서 정본은 각 항목의 메타 칸 하나뿐이고, 표는 여기서 찍는다.

    python scripts/build_console_board.py            # 현황판을 갱신
    python scripts/build_console_board.py --check    # 어긋나면 0 이 아닌 코드

읽는 것은 `docs/web-console-handoff.md` 안의 항목들이다.

    <a id="wc-16"></a>
    # WC-16. Archiver 운영 화면 — 수집 전환·소급

    | | |
    |---|---|
    | 상태 | 🟠 진행중 — 뒤에 붙는 한 줄이 현황판의 비고가 된다 |
    | 우선 | 🔥 최우선 |
    | 담당 | 양쪽 |
    | 등록 | 2026-10-14 |
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "web-console-handoff.md"

BEGIN = "<!-- BOARD:BEGIN 손으로 고치지 마세요 — scripts/build_console_board.py 가 씁니다 -->"
END = "<!-- BOARD:END -->"

# 상태 → 정렬 순서. 끝난 것은 아래로 내린다 — 위에 쌓이면 지금 할 일이 안 보인다.
STATUS_ORDER = {
    "요청됨": 0,
    "진행중": 0,
    "확인 필요": 1,
    "대기": 2,
    "보류": 3,
    "완료": 4,
    "취소": 5,
}
PRIORITY_ORDER = {"최우선": 1, "높음": 2, "보통": 3, "낮음": 4}
OWNERS = {"웹콘솔", "봇개발", "양쪽", "오너"}

ITEM = re.compile(r'(?m)^<a id="wc-(\d+)"></a>\n# WC-(\d+)\.\s*(?P<title>.+)$')


class BoardError(Exception):
    """항목이 규칙을 어겼다. **고치기 전에는 표를 찍지 않는다.**"""


def _cell(block: str, name: str) -> str:
    """메타 표의 첫 칸만 읽는다.

    항목 본문에도 `| 상태 |` 로 시작하는 표가 있을 수 있다(WC-14 가 그렇다).
    뒤엣것을 읽으면 조용히 엉뚱한 값이 보드에 올라간다.
    """
    found = re.search(rf'(?m)^\| {name} \| (.+?) \|$', block)
    if not found:
        raise BoardError(f"`| {name} |` 칸이 없습니다")
    return found.group(1).strip()


def _split(text: str, label: str, allowed: dict | set) -> tuple[str, str]:
    """`🟠 진행중 — 비고` 를 (진행중, 비고) 로 가른다."""
    head, _, note = text.partition(" — ")
    word = head.replace("🔴", "").replace("🟠", "").replace("❓", "")
    word = word.replace("⏸", "").replace("⚪", "").replace("✖", "")
    word = word.replace("✅", "").replace("🔥", "").strip()
    if word not in allowed:
        raise BoardError(f"모르는 {label}: {text!r}")
    return word, note.strip()


def rows() -> list[dict[str, str]]:
    text = DOC.read_text(encoding="utf-8")
    marks = list(ITEM.finditer(text))
    if not marks:
        raise BoardError(f"{DOC} 에 항목이 없습니다")

    out: list[dict[str, str]] = []
    for mark, nxt in zip(marks, [*marks[1:], None], strict=True):
        if mark.group(1) != mark.group(2):
            raise BoardError(f"WC-{mark.group(1)}: 앵커와 제목 번호가 다릅니다 (WC-{mark.group(2)})")
        block = text[mark.end() : nxt.start() if nxt else len(text)]
        num = mark.group(1)
        try:
            status, note = _split(_cell(block, "상태"), "상태", STATUS_ORDER)
            priority, _ = _split(_cell(block, "우선"), "우선순위", PRIORITY_ORDER)
            owner = _cell(block, "담당")
        except BoardError as exc:
            raise BoardError(f"WC-{num}: {exc}") from exc
        if owner not in OWNERS:
            raise BoardError(f"WC-{num}: 모르는 담당 {owner!r} — {sorted(OWNERS)} 중 하나")
        title = re.sub(r'^【[^】]*】\s*', "", mark.group("title")).strip()
        out.append(
            {
                "wc": f"WC-{num}",
                "title": title,
                "status": status,
                "note": note,
                "priority": priority,
                "owner": owner,
            }
        )

    seen = [i["wc"] for i in out]
    if len(set(seen)) != len(seen):
        raise BoardError(f"번호가 겹칩니다: {sorted(seen)}")
    out.sort(key=lambda i: (STATUS_ORDER[i["status"]], PRIORITY_ORDER[i["priority"]], i["wc"]))
    return out


def table(items: list[dict[str, str]]) -> str:
    badge = {
        "요청됨": "🔴 요청됨",
        "진행중": "🟠 진행중",
        "확인 필요": "❓ 확인 필요",
        "대기": "⏸ 대기",
        "보류": "⚪ 보류",
        "취소": "✖ 취소",
        "완료": "✅ 완료",
    }
    out = ["| WC | 우선 | 내용 | 담당 | 진행 상태 | 문서 |", "|---|---|---|---|---|---|"]
    for i in items:
        mark = "🔥 최우선" if i["priority"] == "최우선" else i["priority"]
        note = f" — {i['note']}" if i["note"] else ""
        out.append(
            f"| **{i['wc']}** | {mark} | {i['title']} | {i['owner']} "
            f"| {badge[i['status']]}{note} | [열기](#{i['wc'].lower()}) |"
        )
    counts: dict[str, int] = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    tally = " · ".join(
        f"{badge[s]} {n}" for s, n in sorted(counts.items(), key=lambda kv: STATUS_ORDER[kv[0]])
    )
    out += ["", f"모두 {len(items)}건 — {tally}"]
    return "\n".join(out)


def render(current: str, body: str) -> str:
    if BEGIN not in current or END not in current:
        raise BoardError("문서에 BOARD:BEGIN/END 표시가 없습니다")
    head = current[: current.index(BEGIN) + len(BEGIN)]
    tail = current[current.index(END) :]
    return f"{head}\n\n{body}\n\n{tail}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="쓰지 않고 어긋났는지만 본다")
    args = ap.parse_args()

    try:
        current = DOC.read_text(encoding="utf-8")
        wanted = render(current, table(rows()))
    except BoardError as exc:
        print(f"현황판을 만들지 못했습니다: {exc}", file=sys.stderr)
        return 2

    if wanted == current:
        print("현황판이 항목과 맞습니다.")
        return 0
    if args.check:
        print(
            "현황판이 항목과 어긋납니다. `python scripts/build_console_board.py` 로 맞추세요.",
            file=sys.stderr,
        )
        return 1
    DOC.write_text(wanted, encoding="utf-8")
    print(f"{DOC} 갱신")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
