#!/usr/bin/env python3
"""문제 패킷을 훑어 **묶음**으로 접는다. 원문은 출력하지 않는다.

## 왜 이 스크립트가 있나
패킷은 문제 200건까지 담고, 건마다 질문·답변·출처·추적이 통째로 들어간다. 그걸
처음부터 끝까지 읽으면 두 가지가 같이 일어난다.

1. 문맥이 원문으로 가득 찬다 — 그런데 **원문은 고칠 대상이 아니다.** 고칠 것은
   원문을 그렇게 만든 코드다.
2. 같은 원인의 100건이 서로 다른 100개 문제로 보인다. 그러면 100번 같은 조사를 한다.

그래서 먼저 접는다. 무엇이 몇 번, 어떤 조합으로 났는지와 **그 묶음에 속한 QA ID**만
낸다. 어느 묶음을 열어 볼지 정한 다음 `--detail` 로 그 건만 연다.

## 쓰는 법
    python summarize_packet.py qa-issues/answer-issues-2026-09-16-all.md
    python summarize_packet.py <파일> --detail QA-ID [QA-ID ...]
    python summarize_packet.py <파일> --group error --limit 5

`--detail` 도 질문·답변 본문은 기본적으로 **접어서** 낸다(`--with-text` 로만 편다).
원문을 봐야 고칠 수 있는 경우는 생각보다 드물다 — 대개는 분류·결과·오류·추적이면
충분하고, 본문은 판단이 갈릴 때만 필요하다.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# "### 12. 2026-09-16T09:00:00+09:00 · tyit · #팀-전산_abb155-공지"
HEADING = re.compile(r"^### (\d+)\.\s*(.*)$")
FIELD = re.compile(r"^- ([^:]+):\s*(.*)$")


def parse(text: str) -> list[dict]:
    """문제 후보 상세를 항목 목록으로 바꾼다.

    보고서 형식은 `tybot.answer_issues.render_report` 가 정한다. 형식이 바뀌면 여기가
    조용히 빈 목록을 내는 대신 **소리를 내야 한다** — 그래서 호출 쪽에서 0건을 두
    가지로 갈라 본다(문제가 없음 / 형식이 어긋남).
    """
    items: list[dict] = []
    current: dict | None = None
    section = ""
    for line in text.splitlines():
        # 보고서 끝에는 `## Claude 작업 규칙` 같은 상위 절이 붙는다. 여기서 끊지 않으면
        # 그 글이 **마지막 문제의 피드백**으로 딸려 들어가고, 그러면 있지도 않은
        # 피드백을 읽고 엉뚱한 것을 고치게 된다.
        if line.startswith("## "):
            current = None
            section = ""
            continue
        heading = HEADING.match(line)
        if heading:
            head = heading.group(2)
            parts = [part.strip() for part in head.split("·")]
            current = {
                "index": int(heading.group(1)),
                "at": parts[0] if parts else "",
                "workspace": parts[1] if len(parts) > 1 else "",
                "channel": parts[2] if len(parts) > 2 else "",
                "fields": {},
                "sections": defaultdict(list),
            }
            items.append(current)
            section = ""
            continue
        if current is None:
            continue
        if line.startswith("**") and line.endswith("**"):
            section = line.strip("*")
            continue
        field = FIELD.match(line)
        if field and not section:
            current["fields"][field.group(1).strip()] = field.group(2).strip()
            continue
        if section:
            current["sections"][section].append(line)
    return items


def _clean(value: str) -> str:
    return value.strip().strip("`") or "-"


def _first(value: str, sep: str) -> str:
    """구분자 앞 조각만. **자르고 나서** 백틱을 뗀다.

    순서를 바꾸면 `summary` / 출처: `llm` 에서 바깥 백틱만 떨어지고 안쪽 것이 남아,
    같은 값이 서로 다른 묶음으로 갈린다.
    """
    return _clean(value.split(sep)[0])


def codes_of(item: dict) -> list[str]:
    raw = item["fields"].get("분류", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def qa_id(item: dict) -> str:
    return _clean(item["fields"].get("QA ID", ""))


def error_class(item: dict) -> str:
    """오류 문자열에서 **되풀이되는 부분**만 남긴다.

    같은 고장이 메시지의 숫자·ID 때문에 다 다르게 보이면 묶이지 않는다. 예외 이름이
    있으면 그것을, 없으면 첫 낱말들을 쓴다.
    """
    raw = _clean(item["fields"].get("오류", ""))
    if raw == "-":
        return "-"
    named = re.search(
        r"\b([A-Z][A-Za-z]*(?:Error|Exception|Timeout|Exceeded|Failure|Refused))\b",
        raw,
    )
    if named:
        return named.group(1)
    # 예외 이름이 없으면 앞 낱말로 묶는다. 다만 **따옴표 안의 값과 숫자는 뺀다** —
    # 워크스페이스 키나 ID 가 섞이면 같은 고장이 워크스페이스마다 다른 묶음이 된다.
    stripped = re.sub(r"""['\"`][^'\"`]*['\"`]|\d+""", "", raw)
    return " ".join(stripped.split()[:4]) or "-"


def summarize(items: list[dict]) -> str:
    out: list[str] = [f"문제 후보 {len(items)}건", ""]

    def table(title: str, counter: Counter, note: str = "") -> None:
        if not counter:
            return
        out.append(f"## {title}")
        if note:
            out.append(note)
        width = max(len(str(key)) for key in counter)
        for key, count in counter.most_common():
            out.append(f"  {str(key).ljust(width)}  {count:>4}")
        out.append("")

    table("분류", Counter(code for item in items for code in codes_of(item)))
    table("워크스페이스", Counter(item["workspace"] or "-" for item in items))
    table(
        "의도 / 결과",
        Counter(
            f"{_first(item['fields'].get('의도', ''), '/')}"
            f" → {_first(item['fields'].get('결과', ''), '·')}"
            for item in items
        ),
    )
    table(
        "오류 종류",
        Counter(error_class(item) for item in items if error_class(item) != "-"),
        "같은 예외가 여러 건이면 원인은 하나일 가능성이 높다.",
    )
    table("채널", Counter(item["channel"] or "-" for item in items), "한 채널에 몰리면 자료 쪽 문제다.")

    # 같은 (분류 조합 + 오류 종류) 를 하나로 본다. 이것이 「고칠 단위」다.
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in items:
        groups[(",".join(sorted(codes_of(item))) or "-", error_class(item))].append(item)

    out.append("## 고칠 단위 (분류 × 오류 종류)")
    out.append("건수가 많은 순. 한 줄이 한 묶음이고, 묶음 하나가 대개 수정 하나다.")
    out.append("")
    for (codes, error), members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ids = [qa_id(item) for item in members]
        shown = ", ".join(ids[:6]) + (f" 외 {len(ids) - 6}건" if len(ids) > 6 else "")
        out.append(f"- [{len(members):>3}건] {codes} / {error}")
        out.append(f"        QA: {shown}")
    out.append("")
    out.append("어느 묶음을 열어 볼지 정한 뒤 --detail <QA ID> 로 그 건만 연다.")
    return "\n".join(out)


def detail(items: list[dict], wanted: list[str], *, with_text: bool) -> str:
    by_id = {qa_id(item): item for item in items}
    out: list[str] = []
    for key in wanted:
        item = by_id.get(key)
        if item is None:
            out.append(f"## {key} — 패킷에 없습니다")
            continue
        out.append(f"## {key}")
        out.append(f"- 시각/범위: {item['at']} · {item['workspace']} · {item['channel']}")
        for name, value in item["fields"].items():
            if name != "QA ID":
                out.append(f"- {name}: {value}")
        traces = "\n".join(item["sections"].get("라우팅·전문 봇 추적", [])).strip()
        if traces:
            out.append("- 추적:")
            out.append(traces)
        feedback = "\n".join(item["sections"].get("연결된 피드백", [])).strip()
        if feedback:
            out.append("- 피드백:")
            out.append(feedback)
        if with_text:
            for name in ("질문", "실제 답변", "출처"):
                body = "\n".join(item["sections"].get(name, [])).strip()
                if body:
                    out.append(f"- {name}:")
                    out.append(body)
        else:
            out.append("  (질문·답변 본문은 접었습니다 — 필요하면 --with-text)")
        out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", help="내려받은 문제 패킷 Markdown 경로")
    parser.add_argument("--detail", nargs="+", default=[], help="자세히 볼 QA ID")
    parser.add_argument("--group", default="", help="이 분류 코드가 붙은 건만 (error, no_evidence, slow, specialist_failure, feedback)")
    parser.add_argument("--limit", type=int, default=0, help="--group 과 함께, 앞에서 N건만")
    parser.add_argument("--with-text", action="store_true", help="질문·답변 본문까지 편다")
    args = parser.parse_args()

    path = Path(args.packet).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"패킷을 읽지 못했습니다: {exc}", file=sys.stderr)
        return 1

    items = parse(text)
    if not items:
        if "## 문제 후보 상세" in text:
            print("문제 후보가 0건입니다. 고칠 것이 없습니다.")
            return 0
        print(
            "패킷 형식을 알아보지 못했습니다. 이 파일이 콘솔 > 운영 현황에서 받은"
            " 문제 패킷이 맞는지 확인하세요.",
            file=sys.stderr,
        )
        return 2

    if args.group:
        items = [item for item in items if args.group in codes_of(item)]
        if args.limit:
            items = items[: args.limit]

    if args.detail:
        print(detail(items, args.detail, with_text=args.with_text))
    else:
        print(summarize(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
