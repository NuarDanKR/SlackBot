#!/usr/bin/env python3
"""
extract_comments.py 시험 — 워드 주석 뽑기.

  python .claude/skills/doc-archive/scripts/test_extract_comments.py

**임시 폴더에서만 돈다.** 가짜 docx 를 `zipfile` 로 만들어 넣는다
(`test_review_batch.py` 가 쓰는 방식과 같다).

**왜 있나** — kordoc 은 워드 주석을 md 에 안 넣는다. 본문은 멀쩡해서 글자 수 검사도
통과하는데, **그 파일을 올린 이유가 통째로 빠진다.** 그러면 봇이 "그 문서는 있는데
검토의견은 없다"고 자신 있게 오답한다. 탐지는 2026-08-09 부터 7.5 점검표가 하고,
이 스크립트는 그 뒤 손으로 하던 **추출**을 맡는다.

절 제목은 `review_batch.py` 의 `COMMENT_SECTION` 과 **같아야 한다** — 어긋나면
점검표가 「주석이 빠졌다」고 계속 ⚠ 를 낸다. 그래서 제목을 여기 박아 두지 않고
그 모듈에서 읽어 대본다.

종료코드: 0 전부 통과 / 1 실패 있음
"""
import sys
import tempfile
import zipfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
import extract_comments as E  # noqa: E402
import review_batch as R  # noqa: E402

FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(path: Path, comments, date=None):
    """`comments` 는 (작성자, [문단, …]) 목록. `None` 이면 comments.xml 자체를 안 넣는다.

    `date` 를 주면 `w:date` 속성을 단다 — 실물 워드가 넣는 모양(`…T16:19:00Z`)으로.
    """
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document {NS}><w:body/></w:document>")
        if comments is None:
            return
        body = ""
        for i, (author, paras) in enumerate(comments):
            runs = "".join(
                f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras
            )
            when = f' w:date="{date}T16:19:00Z"' if date else ""
            body += f'<w:comment w:id="{i}" w:author="{author}"{when}>{runs}</w:comment>'
        z.writestr("word/comments.xml", f"<w:comments {NS}>{body}</w:comments>")


print("[1/6] 주석 2건을 작성자와 함께 뽑는다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "검토의견.docx"
    make_docx(p, [("홍길동", ["문구 확인 부탁드립니다"]),
                  ("TY", ["담당 부서 확인 요청드립니다"])])
    got = E.extract_comments(p)
    if [c["author"] for c in got] == ["홍길동", "TY"]:
        ok("작성자가 순서대로 둘")
    else:
        bad(f"작성자가 {[c.get('author') for c in got]!r} 입니다")
    if [c["text"] for c in got] == ["문구 확인 부탁드립니다", "담당 부서 확인 요청드립니다"]:
        ok("본문이 그대로 둘")
    else:
        bad(f"본문이 {[c.get('text') for c in got]!r} 입니다")

print("[2/6] comments.xml 이 없으면 빈 목록 — 예외를 던지지 않는다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "주석없음.docx"
    make_docx(p, None)
    try:
        got = E.extract_comments(p)
        if got == []:
            ok("빈 목록이 온다 — 주석 없는 문서가 대부분이라 여기서 죽으면 안 된다")
        else:
            bad(f"빈 목록이 아닙니다: {got!r}")
    except Exception as e:
        bad(f"예외를 던졌습니다: {e!r}")

print("[3/6] 절 제목이 review_batch 의 COMMENT_SECTION 과 같다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "검토의견.docx"
    make_docx(p, [("홍길동", ["의견 하나"])])
    section = E.render_section(E.extract_comments(p))
    first = section.split("\n")[0]
    if first == "### 문서 주석 (검토의견)":
        ok("제목 줄이 아카이브에 이미 있는 것과 글자까지 같다")
    else:
        bad(f"제목 줄이 {first!r} 입니다")
    if R.COMMENT_SECTION in first:
        ok(f"점검표가 찾는 문자열({R.COMMENT_SECTION!r})이 제목에 든다 — 어긋나면 ⚠ 가 계속 난다")
    else:
        bad(f"점검표의 {R.COMMENT_SECTION!r} 가 제목에 없습니다")
    if "- **[홍길동]** 의견 하나" in section:
        ok("작성자가 `- **[이름]** 내용` 모양으로 들어간다")
    else:
        bad(f"항목 모양이 다릅니다:\n{section}")

print("[4/6] 주석 본문의 `<`·`&` 가 태그로 안 샌다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "검토의견.docx"
    make_docx(p, [("TY", ["&lt;별첨&gt; 참고 &amp; 확인"])])
    got = E.extract_comments(p)
    if got and got[0]["text"] == "<별첨> 참고 & 확인":
        ok("XML 엔티티가 풀려 원문 그대로다 — 정규식으로 태그를 지우면 글자가 사라진다")
    else:
        bad(f"본문이 {[c.get('text') for c in got]!r} 입니다")

print("[5/6] 한 주석의 여러 문단이 한 항목으로 합쳐진다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "검토의견.docx"
    make_docx(p, [("김철수", ["첫 문단", "둘째 문단"])])
    got = E.extract_comments(p)
    if len(got) == 1:
        ok("문단이 둘이어도 주석은 하나로 센다 — 점검표의 건수와 맞아야 한다")
    else:
        bad(f"{len(got)}건으로 세어졌습니다")
    if got and "첫 문단" in got[0]["text"] and "둘째 문단" in got[0]["text"]:
        ok("두 문단이 다 실린다")
    else:
        bad(f"본문이 {got[0]['text']!r} 입니다" if got else "빈 결과입니다")

print("[6/6] `w:date` 가 있으면 손으로 넣던 것과 같은 모양으로 날짜를 싣는다")
with tempfile.TemporaryDirectory() as d:
    # 2026-08-28 사업장자 md 에 사람이 적어 둔 줄과 글자까지 맞춘다.
    p = Path(d) / "markup.docx"
    make_docx(p, [("김철수", ["제2항과 충돌할 수 있으니 확인 바랍니다."])], date="2026-08-28")
    section = E.render_section(E.extract_comments(p))
    if "- **[김철수 · 2026-08-28]** 제2항과 충돌할 수 있으니 확인 바랍니다." in section:
        ok("`[이름 · 날짜]` 로 실린다 — 실물 대조에서 이 표기가 맞았다")
    else:
        bad(f"머리 모양이 다릅니다:\n{section}")
    # 날짜가 없는 판(.doc 를 변환하면 w:date 가 없어진다)은 이름만 — 사업장가 절의 모양.
    q = Path(d) / "날짜없음.docx"
    make_docx(q, [("TY", ["문구 확인 부탁드립니다"])])
    if "- **[TY]** 문구 확인 부탁드립니다" in E.render_section(E.extract_comments(q)):
        ok("날짜가 없으면 이름만 — 빈 ` · ` 가 안 붙는다")
    else:
        bad("날짜 없는 주석의 머리가 깨집니다")

sys.exit(1 if FAILED else 0)
