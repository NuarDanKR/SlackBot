#!/usr/bin/env python3
"""
워드(docx) 파일의 주석(코멘트)을 뽑아 아카이브 md 에 붙일 절로 만든다.

  python extract_comments.py <docx경로>            # 절을 화면에 찍는다
  python extract_comments.py <docx경로> --count    # 건수만

**왜 필요한가** — kordoc 은 워드 주석을 md 에 안 넣는다. 본문은 멀쩡해서 글자 수
검사도 통과하는데 **그 파일을 올린 이유가 통째로 빠진다.** `사업장가 임대차
계약서_갑사 검토의견 추가` 는 이전 판과 본문 차이가 서명란뿐이고 검토의견
9건(886자)이 전부 주석이었다. 넣고 나면 "그 문서는 있는데 검토의견은 없다"가 되어
봇이 자신 있게 오답한다.

**붙이는 것은 사람이 한다.** 이 스크립트는 절 본문을 만들어 줄 뿐 md 를 고치지
않는다 — 주석에도 개인정보가 들어갈 수 있어 마스킹 판단이 걸린다.

**변경내용추적(`<w:ins>`·`<w:del>`)은 안 싣는다.** 같은 자리에 있지만 성격이 다르고
(문서가 어떻게 고쳐졌나 vs 사람이 무엇을 지적했나), 지금은 범위 밖이다.

절 제목은 `review_batch.py` 의 `COMMENT_SECTION` 과 **같아야 한다** — 7.5 점검표가
그 문자열로 md 를 훑어 주석이 실렸는지 판정한다. 어긋나면 붙여 놓고도 ⚠ 가 계속 뜬다.

종료코드: 0 성공 / 1 파일을 못 읽음
"""

import argparse
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 절 제목. `review_batch.py` 의 `COMMENT_SECTION`("문서 주석") 을 품어야 한다.
# 아카이브에 이미 들어간 절들과 글자까지 같게 맞춘 것이라 바꾸지 말 것.
SECTION_TITLE = "### 문서 주석 (검토의견)"
SECTION_LEAD = "원본 워드 파일의 **주석(코멘트)**에만 있는 내용이다. 본문에는 나오지 않는다."


def extract_comments(docx_path: Path) -> list:
    """워드 주석을 `[{"author": …, "date": …, "text": …}, …]` 로. 없으면 빈 목록.

    `date` 는 `w:date` 의 앞 10자(`YYYY-MM-DD`)이고, 그 속성이 없으면 빈 문자열이다.

    **정규식으로 태그를 지우지 않는다.** 주석 안의 `<`·`&` 는 XML 엔티티로 적혀
    있어서, 정규식으로 `<[^>]+>` 를 지우면 `&lt;별첨&gt;` 같은 대목이 조용히
    사라지거나 뭉개진다. 조용히 틀리는 종류라 파서로 읽는다.

    한 주석에 문단이 여럿이면 **한 항목으로 합친다** — 점검표는 `<w:comment>` 수를
    세므로 건수가 맞아야 한다.

    주석이 없는 문서가 대부분이라 여기서 예외를 던지지 않는다. 못 읽으면 빈 목록이다.
    """
    try:
        with zipfile.ZipFile(docx_path) as z:
            if "word/comments.xml" not in z.namelist():
                return []
            raw = z.read("word/comments.xml")
    except (zipfile.BadZipFile, KeyError, OSError):
        return []

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return []

    out = []
    for c in root.iter(f"{W}comment"):
        paras = []
        for p in c.iter(f"{W}p"):
            # 한 문단 안의 조각(run)을 이어 붙인다. 워드는 글꼴이 바뀌는 자리마다
            # 조각을 나누므로, 조각별로 다루면 한 문장이 여러 줄로 쪼개진다.
            text = "".join(t.text or "" for t in p.iter(f"{W}t"))
            if text.strip():
                paras.append(text.strip())
        text = " ".join(paras).strip()
        if not text:
            continue
        out.append({
            "author": (c.get(f"{W}author") or "").strip(),
            "date": (c.get(f"{W}date") or "")[:10],
            "text": text,
        })
    return out


def render_section(comments: list) -> str:
    """주석 목록 → md 절. 주석이 없으면 빈 문자열.

    머리는 `- **[이름 · YYYY-MM-DD]**` 이고 날짜가 없으면 `- **[이름]**` 이다.
    **두 모양 다 아카이브에 이미 있다** — 날짜 있는 쪽이 2026-08-28 사업장자,
    없는 쪽이 사업장가 합의서(원본이 `.doc` 라 변환을 거치며 `w:date` 를 잃었다).
    손으로 넣은 절을 그대로 재현하려고 원본에 있는 값만 싣는다.
    """
    if not comments:
        return ""
    lines = [SECTION_TITLE, "", SECTION_LEAD, ""]
    for c in comments:
        author = c.get("author") or "이름 없음"
        head = f"{author} · {c['date']}" if c.get("date") else author
        lines.append(f"- **[{head}]** {c.get('text', '')}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="워드 주석을 아카이브 md 절로 뽑는다")
    ap.add_argument("docx", help="원본 docx 경로 (보통 ~/.doc-cache/<채널>/… )")
    ap.add_argument("--count", action="store_true", help="건수와 글자 수만 찍는다")
    args = ap.parse_args()

    p = Path(args.docx)
    if not p.exists():
        print(f"ERROR: 파일이 없습니다 — {p}", file=sys.stderr)
        return 1

    comments = extract_comments(p)
    if args.count or not comments:
        chars = sum(len(c["text"]) for c in comments)
        print(f"주석 {len(comments)}건 · {chars:,}자")
        if not comments:
            print("(주석이 없거나 워드 파일이 아닙니다 — 붙일 절이 없습니다)")
        return 0

    print(render_section(comments), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
