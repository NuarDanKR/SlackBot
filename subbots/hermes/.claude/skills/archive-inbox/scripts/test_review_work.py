#!/usr/bin/env python3
"""
review_work.py 시험. **임시 파일에서만 돌고 저장소는 안 건드린다.**

  python .claude/skills/archive-inbox/scripts/test_review_work.py

종료코드: 0 전부 통과 / 1 실패 있음

── 왜 남겨 두는가 ──

이 스크립트가 하는 일은 **사람이 판정할 근거를 화면에 붙이는 것**이다. 근거를 못 찾아
조용히 빈 자리로 두면, 사람은 근거가 없는 줄 모르고 DM 문구만 보고 정하게 된다.
2026-08-12 에 DM 이 「내부 승인 진행」이라 적었는데 원문은 「진행예정」이었던 건이 있어
(요약 불일치 5건 중 2건), 그 차이가 실제로 반영될 뻔했다. 그래서 「못 찾았다」가
빈 문자열과 다른 모양으로 나오는지를 시험으로 잡아 둔다.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_work as R  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok &= bool(cond)
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))


MD = """> **채널**: #테스트
> **기간**: 2026-08-01 ~ 2026-08-11 · **실제 메시지**: 3건
> **핵심 쟁점**: 310억 조기상환(2026-08-11 예정)

---

## 2026-08

**2026-08-11 14:20 · 김실무**

310억 조기상환 완료했습니다.
상세 내역은 다음과 같습니다.
초기 사업비 41억도 회수했습니다.

**2026-08-10 09:05 · 박담당**

다음 주 이사회 부의 예정입니다.
"""

with tempfile.TemporaryDirectory() as d:
    md = Path(d) / "테스트.md"
    md.write_text(MD, encoding="utf-8")

    print("\ncontext_for")

    # ① 근거 인용이 든 메시지 블록을 헤더째 찾아온다 — 헤더가 있어야 언제·누가가 보인다
    ctx = R.context_for(md, '"310억 조기상환 완료했습니다"')
    check("원문 블록을 헤더째 찾는다", "2026-08-11 14:20 · 김실무" in ctx, repr(ctx[:30]))
    # 41억 문장은 인용 줄에서 **두 줄** 떨어져 있다 — 같은 줄이면 항진식이고, 바로
    # 다음 줄이면 창이 깨져도 인용 ±1 패딩에 걸려 초록이다 (2026-09-05 되돌리기 대조).
    check("본문도 함께 온다", "초기 사업비 41억" in ctx)

    # ② 못 찾으면 조용히 빈 문자열을 주지 않는다.
    #    빈 값은 「원문에 없다」와 「검색이 어긋났다」를 같은 모양으로 만든다.
    miss = R.context_for(md, '"있지도 않은 문장입니다"')
    check("못 찾으면 못 찾았다고 적는다", "찾지 못" in miss, repr(miss[:40]))

    # ③ 근거에 인용부호가 아예 없는 경우도 빈 자리로 두지 않는다
    none = R.context_for(md, "")
    check("근거가 비어도 빈 문자열이 아니다", bool(none.strip()))

    # ④ 띄어쓰기가 달라도 찾는다 — 모델이 인용하며 다듬는 폭만큼
    spaced = R.context_for(md, '"310억  조기상환 완료했습니다"')
    check("띄어쓰기 차이를 넘어 찾는다", "김실무" in spaced)

    # ⑤ 긴 메시지 — 근거 문장이 헤더에서 멀리 있어도 화면에 있어야 한다.
    #    2026-08-12 실사용에서 22줄짜리 메시지의 근거 2건이 창 밖으로 잘렸다. 화면에는
    #    같은 메시지의 앞부분이 멀쩡히 보여서, 사람은 그게 전부인 줄 알고 후보 문구만
    #    보고 정하게 된다 — 이 도구가 하려는 일과 정반대다.
    long_md = Path(d) / "긴메시지.md"
    filler = "\n".join(f"• 중간 문장 {i} 입니다" for i in range(1, 19))
    long_md.write_text(
        "> **채널**: #긴것\n\n---\n\n## 2026-08\n\n"
        "**2026-08-12 07:30 · 김철수**\n"
        "약정서 구조 및 혼동 이슈 관련 질의사항\n"
        f"{filler}\n"
        "• 귀사의 출재 없이 약정만으로 구상권이 당연 인정될지 명확하지 아니합니다\n\n"
        "**2026-08-11 10:00 · 김실무**\n앞 메시지입니다\n",
        encoding="utf-8")

    far = R.context_for(long_md, '"귀사의 출재 없이 약정만으로 구상권이 당연 인정될지 명확하지 아니합니다"')
    check("헤더에서 멀어도 근거 문장이 화면에 있다", "귀사의 출재 없이" in far, repr(far[-40:]))
    check("그래도 헤더는 함께 온다", "2026-08-12 07:30 · 김철수" in far)
    check("생략한 자리는 생략했다고 적는다", "생략" in far)
    check("다음 메시지까지 딸려오지 않는다", "앞 메시지입니다" not in far)

    # ⑥ 인용이 여럿이면 처음 걸린 것에서 멈추지 않는다.
    #    실제 근거는 대개 뒤엣것이고, 앞엣것은 메시지 제목인 경우가 많다.
    multi = R.context_for(
        long_md,
        '"약정서 구조 및 혼동 이슈 관련 질의사항" / '
        '"귀사의 출재 없이 약정만으로 구상권이 당연 인정될지 명확하지 아니합니다"')
    check("인용이 여럿이면 뒤엣것도 화면에 있다", "귀사의 출재 없이" in multi, repr(multi[:40]))
    check("앞엣 인용도 함께 있다", "약정서 구조 및 혼동" in multi)

    # ⑦ 요약 자리는 원문이 아니다 — 인용을 찾을 때 건너뛴다.
    #    「반영」은 원문 표현을 그대로 요약에 옮겨 적는 일이라, 반영한 다음부터는 같은
    #    문장이 요약에도 있다. 파일 전체에서 찾으면 요약이 먼저 걸리고, 그 위에는 메시지
    #    헤더가 없어 「원문」이라며 요약+구분선을 보여주게 된다 — 반영을 한 번 할 때마다
    #    그 채널에서 원문을 잃는다 (2026-08-12 첫 반영 직후 #사업장다 에서 발생).
    echoed = Path(d) / "반영된채널.md"
    echoed.write_text(
        "# #반영된채널\n\n"
        "> **핵심 쟁점**: 기존 매수의향자 매입의사 철회로 매각 협의 무산\n\n"
        "---\n\n## 2026-08\n\n"
        "**2026-08-12 10:33 · 이영희**\n"
        "• 기존 매수의향자 매입의사 철회\n"
        "• 만기 도래로 처분의견 요청\n",
        encoding="utf-8")

    ech = R.context_for(echoed, '"기존 매수의향자 매입의사 철회"')
    check("요약이 아니라 메시지를 찾는다", "2026-08-12 10:33 · 이영희" in ech, repr(ech[:40]))
    check("요약 줄을 원문이라며 보여주지 않는다", "핵심 쟁점" not in ech)

    # ⑧ 요약에만 있고 메시지에는 없으면 「찾았다」고 하면 안 된다
    only = R.context_for(echoed, '"매각 협의 무산"')
    check("요약에만 있으면 못 찾았다고 적는다", "찾지 못" in only, repr(only[:40]))

    # ⑨ 인용이 원문에서 **여러 줄에 걸쳐** 있어도 찾는다.
    #    슬랙에서는 항목과 그 설명이 다른 줄에 있는 것이 흔한데(`- 회사 : 미팅` 다음 줄에
    #    `＊협의 내용`), 모델은 그 둘을 이어 한 문장으로 인용한다. 줄 하나씩 찾으면 어느
    #    줄에도 통째로 안 들어 있어 「원문에서 찾지 못했습니다」가 된다 — 그런데 스킬 문서는
    #    그 문구를 「근거가 원문에 없으니 대개 빼」로 읽으라고 적고 있어, 멀쩡한 항목이
    #    버려진다 (2026-08-13 실사용: 8건 중 3건이 이렇게 나왔고 셋 다 원문이 있었다).
    wrapped = Path(d) / "여러줄.md"
    wrapped.write_text(
        "# #여러줄\n\n> **핵심 쟁점**: 임차인 협의\n\n---\n\n## 2026-08\n\n"
        "**2026-08-12 17:15 · 윤서준**\n"
        "2.임대완료 미분양 상가 관련\n"
        " - 을상사 : 이영희 대표 미팅\n"
        "  ＊월세 및 보증금 조정 요청에 따른 협의\n"
        "    (세부내용 익일 보고 예정)\n",
        encoding="utf-8")

    wrap = R.context_for(wrapped, '"을상사 : 이영희 대표 미팅 ＊월세 및 보증금 조정 요청에 따른 협의"')
    check("줄을 넘어가는 인용도 찾는다", "찾지 못" not in wrap, repr(wrap[:60]))
    check("그 경우에도 헤더가 함께 온다", "2026-08-12 17:15 · 윤서준" in wrap)
    # 「못 찾음」 문구도 근거를 그대로 되뇌므로, 인용 문장이 화면에 있는지로는 판정이
    # 안 된다. 원문에만 있는 줄로 본다.
    check("원문 블록이 렌더된다 (원문에만 있는 줄로 확인)", "세부내용 익일 보고 예정" in wrap)

    # ⑩ 그래도 없는 것은 없다고 해야 한다 — 이어 붙이기가 「아무거나 걸리는」 검색이
    #    되면 ⑧ 이 무너진다. 줄을 이어 붙여도 원문에 없는 문장은 못 찾아야 한다.
    still = R.context_for(wrapped, '"있지도 않은 문장을 길게 적어 봅니다"')
    check("이어 붙여도 없는 것은 못 찾는다", "찾지 못" in still, repr(still[:40]))

    # ⑪ 여러 줄에 걸친 인용은 **걸친 줄이 전부** 화면에 있어야 한다.
    #    ⑤ 와 같은 사고인데 원인이 다르다 — 걸린 자리는 찾았는데 창이 인용의 시작 줄만
    #    한 줄로 치고 잘라서, 인용의 뒷줄이 「생략」 안으로 접힌다. 화면에는 앞부분이
    #    멀쩡히 보이므로 사람은 그게 인용 전부인 줄 안다. 긴 메시지(span 초과)에서만 난다.
    long_wrap = Path(d) / "긴여러줄.md"
    long_wrap.write_text(
        "> **채널**: #긴것\n\n---\n\n## 2026-08\n\n"
        "**2026-08-12 07:30 · 김철수**\n"
        + "\n".join(f"• 중간 문장 {i} 입니다" for i in range(1, 19)) + "\n"
        " - 을상사 : 이영희 대표 미팅\n"
        "  ＊월세 및 보증금 조정 요청에 따른 협의\n"
        "    (세부내용 익일 보고 예정)\n",
        encoding="utf-8")

    span3 = R.context_for(
        long_wrap,
        '"을상사 : 이영희 대표 미팅 ＊월세 및 보증금 조정 요청에 따른 협의 (세부내용 익일 보고 예정)"')
    check("긴 메시지에서도 인용의 첫 줄이 있다", "을상사 : 이영희" in span3)
    check("인용의 가운데 줄도 있다", "월세 및 보증금 조정" in span3)
    check("인용의 마지막 줄도 있다 — 접히면 안 된다", "세부내용 익일 보고 예정" in span3, repr(span3[-60:]))
    check("그래도 앞은 접는다 (창은 여전히 좁다)", "생략" in span3)

    # ⑫ 메시지 경계를 넘는 인용은 찾지 않는다.
    #    줄을 이어 붙이는 것이 안전한 근거는 「메시지 사이에 헤더 줄이 있고 그 글자도
    #    줄기에 함께 들어간다」는 것뿐이다. 그 근거를 주석으로만 두면, 나중에 누가
    #    「헤더는 원문이 아니니 빼자」고 정리하는 순간 조용히 무너진다.
    boundary = Path(d) / "경계.md"
    boundary.write_text(
        "> **채널**: #경계\n\n---\n\n## 2026-08\n\n"
        "**2026-08-12 07:30 · 갑**\n앞 메시지 끝 문장입니다\n\n"
        "**2026-08-11 07:30 · 을**\n뒤 메시지 첫 문장입니다\n",
        encoding="utf-8")

    across = R.context_for(boundary, '"앞 메시지 끝 문장입니다 뒤 메시지 첫 문장입니다"')
    check("메시지 경계를 넘는 인용은 찾지 않는다", "찾지 못" in across, repr(across[:40]))

    # ⑬ 모델이 인용 가운데를 `...` 로 건너뛰어 적어도 찾는다.
    #    그 표시는 「여기서 중간을 뺐다」는 뜻이라 원문에는 없다. 인용이 통째로 이어져
    #    있다고 치고 찾으면 원문 어디에도 없어 「못 찾음」이 된다 — 인용이 하나뿐인
    #    항목에서 이러면 멀쩡한 근거가 「빼」로 읽힌다.
    #    실측: 과거 목록 12건·인용 18건 중 이 모양이 2건, 안 걸린 인용은 그중 1건뿐이다.
    elided = Path(d) / "생략.md"
    elided.write_text(
        "> **채널**: #생략\n\n---\n\n## 2026-08\n\n"
        "**2026-08-12 17:50 · 강태우**\n"
        "금일 갑금융이 을기관과 미팅을 하여, 가산이자는 제하고 청구하는 것이"
        " 결의사항이라는 공문을 확인했고, 이를 무력화하려면 소송을 해야 하는데 이 또한"
        " 쉽지 않다고 설명하니, 그러면 시간 끌지 말고 빠르게 마무리하자는 의견을 전해왔답니다.\n",
        encoding="utf-8")

    cut = R.context_for(elided, '"결의사항이라는 공문을 확인했고 ... 그러면 시간 끌지 말고 빠르게 마무리하자는 의견을"')
    check("가운데를 건너뛴 인용도 찾는다", "찾지 못" not in cut, repr(cut[:50]))
    check("그 경우에도 헤더가 함께 온다", "2026-08-12 17:50 · 강태우" in cut)

    # 가운뎃점 하나짜리 유니코드 생략부호도 같다
    cut2 = R.context_for(elided, '"결의사항이라는 공문을 확인했고 … 그러면 시간 끌지 말고 빠르게 마무리하자는 의견을"')
    check("… (유니코드 생략부호)도 같다", "찾지 못" not in cut2, repr(cut2[:50]))

    # ⑭ 그렇다고 아무 조각이나 걸리면 안 된다. 조각이 짧으면 흔한 낱말이라 엉뚱한
    #    메시지가 걸린다 — 「못 찾음」보다 나쁜 것은 틀린 원문을 원문이라고 보여주는 것이다.
    tiny = R.context_for(elided, '"금일 ... 공문 ... 의견"')
    check("조각이 짧으면 찾았다고 하지 않는다", "찾지 못" in tiny, repr(tiny[:50]))

    # ⑮ 「짧다」의 기준은 **접기 전** 길이다 — `QUOTE_RE` 가 인용으로 인정하는 기준과
    #    같아야 한다. 접은 뒤로 재면 띄어쓴 짧은 인용이 통째로 버려진다:
    #    `약정 체결 업무` 는 8자라 인용으로 뽑히는데 공백을 지우면 6자가 된다.
    #    (아카이브 전량 대조에서 이것 때문에 3건이 퇴행했다 — 시험은 통과하고 있었다.)
    short = Path(d) / "짧은인용.md"
    short.write_text(
        "> **채널**: #짧은것\n\n---\n\n## 2026-08\n\n"
        "**2026-08-12 09:00 · 김실무**\n"
        "‘약정 체결 업무’는 계획대로 진행하되 담당 부서를 확인해 주세요\n",
        encoding="utf-8")

    tight = R.context_for(short, "'약정 체결 업무'")
    check("띄어쓴 짧은 인용도 찾는다 (기준은 접기 전 길이)", "찾지 못" not in tight, repr(tight[:50]))
    check("QUOTE_RE 와 같은 하한을 쓴다", R._needles("약정 체결 업무") == ["약정체결업무"],
          repr(R._needles("약정 체결 업무")))

    # ⑮-2 모델은 **여러 줄에 걸친 원문을 ` / ` 로 이어** 한 인용으로 적는다. 그 글자는
    #     원문에 없고, 그 자리의 원문은 `·`(U+00B7)인데 그것은 문장 구분자라 일부러
    #     안 지운다(⑬ 의 짝). 그래서 접기만으로는 영영 안 걸려 항목이 「근거 못 찾아 뺀
    #     후보」로 밀리고, SKILL 이 그 문구를 「대개 빼」로 읽으라고 안내하므로
    #     **멀쩡한 근거가 버려진다.** 2026-09-10 `#사업장가` 회차에서 실제로 그랬다.
    #     붙여 쓴 `/`(날짜·`채권/채무`)는 원문의 글자이므로 **쪼개면 안 된다.**
    slash = Path(d) / "이어적기.md"
    slash.write_text(
        "> **채널**: #이어적기\n\n---\n\n## 2026-09\n\n"
        "**2026-09-09 17:04 · 김실무**\n"
        "1. 미납입 호실 중 조기납부 유도 진행 호실 회수(3호실) 완료\n"
        "   · 대출 잔액 약 40억 감소 (900억 → 860억)\n"
        "   · 실수금 약 7억이며 채권/채무 상계는 별건으로 검토한다\n",
        encoding="utf-8")

    joined = R.context_for(slash, '"대출 잔액 약 40억 감소 (900억 → 860억) / 실수금 약 7억이며"')
    check('두 줄을 " / " 로 이어 적은 인용도 찾는다', "찾지 못" not in joined, repr(joined[:60]))
    check('쪼개도 원문에 없는 인용은 못 찾는다',
          "찾지 못" in R.context_for(slash, '"이행청구를 완료했고 / 상장은 다음 달로 미루었습니다"'))
    check('붙여 쓴 /(채권/채무)는 원문의 글자라 쪼개지 않는다',
          "찾지 못" not in R.context_for(slash, '"실수금 약 7억이며 채권/채무 상계는 별건으로"'))

    # ⑯ 월 헤딩(`## YYYY-MM`)은 원문이 아니다 — 거기서 시작하는 인용은 안 걸려야 한다.
    #    줄 하나씩 찾던 때는 헤딩을 넘는 needle 을 만들 수 없어 닿지 않았는데, 줄을 이어
    #    붙이면서 닿게 됐다. 걸리면 블록이 헤딩 한 줄로 줄어드는 데서 끝나지 않는다 —
    #    `first = min(hits)` 라 헤딩이 본문 맨 앞줄이고, **같은 항목의 진짜 근거까지 덮는다.**
    #    화면에는 `## 2026-08` 과 「채널 md 를 여세요」만 남는다.
    heading = Path(d) / "월헤딩.md"
    heading.write_text(
        "# #시험\n\n> **핵심 쟁점**: 없음\n\n---\n\n"
        "## 2026-08\n"
        "**2026-08-12 09:00 · 김실무**\n"
        "잔금 수금 264억 완료했습니다 그리고 이어집니다\n",
        encoding="utf-8")

    onhead = R.context_for(heading, '"## 2026-08 **2026-08-12 09:00 · 김실무**"')
    check("월 헤딩에서 시작하는 인용은 못 찾았다고 적는다", "찾지 못" in onhead, repr(onhead[:44]))

    masked = R.context_for(
        heading,
        '"## 2026-08 **2026-08-12 09:00 · 김실무**" / "잔금 수금 264억 완료했습니다"')
    check("헤딩이 진짜 근거를 덮지 않는다", "잔금 수금 264억" in masked, repr(masked[:60]))
    check("덮이지 않은 쪽의 헤더가 온다", "2026-08-12 09:00 · 김실무" in masked)

    # ⑰ 정규화는 **생산 쪽(JS)과 같아야 한다.**
    #    관문(`summary.js` 의 verifyEvidence)이 이 판정으로 항목을 내보낼지 정하고,
    #    화면(여기)이 같은 판정으로 원문을 찾는다. 화면 쪽이 덜 지우면 **관문은 통과시켰는데
    #    화면은 「원문에 없다」고 하는** 조합이 생기고, 문서는 그 문구를 「빼」로 읽으라고 한다.
    #    실측(2026-08-13, 아카이브 전량): 백틱 440건 · 별표 141건 · 따옴표 25건이 그 상태였다.
    #    JS 는 곧은 따옴표 `"`·`'` 만 지운다(둥근 따옴표 아님) — 코드포인트로 확인했다.
    check("강조 기호를 지운다", R._fold("*강조* `코드` _밑줄_ ~물결~") == "강조코드밑줄물결",
          repr(R._fold("*강조* `코드` _밑줄_ ~물결~")))
    check("곧은 따옴표를 지운다", R._fold("\"큰따옴표\" '작은따옴표'") == "큰따옴표작은따옴표",
          repr(R._fold("\"큰따옴표\" '작은따옴표'")))
    check("둥근 따옴표는 남긴다 (JS 도 안 지운다)", R._fold("“둥근”") == "“둥근”",
          repr(R._fold("“둥근”")))
    check("영문은 소문자로 맞춘다", R._fold("Tr.A 318억") == "tr.a318억", repr(R._fold("Tr.A 318억")))
    check("공백은 그대로 지운다", R._fold("가 나  다") == "가나다")

    # ⑱ 글머리표(`•`)는 원문의 글자인데 모델은 인용에서 떨어뜨린다.
    #    슬랙에서 항목을 나열하면 `• ` 로 시작하는 줄이 되고, 모델이 앞 줄과 이어 인용하면
    #    그 표시가 빠진 문장이 된다. 접기가 그것을 안 지우면 **원문에 있는 근거를
    #    「원문에서 찾지 못했습니다」로 보여주고**, 이 스킬 문서는 그 문구를 「대개 빼」로
    #    읽으라고 한다. 2026-08-20 07:00 회차의 `#사업장사` 항목이 실제로 그랬다.
    #    아카이브 전량 실측: `•` 305회 중 261회(86%)가 줄머리다.
    #
    #    **`·`(U+00B7)는 안 지운다** — 2,295회 중 줄머리가 9회뿐인 문장 구분자라,
    #    지우면 서로 다른 문장이 같은 값으로 접혀 엉뚱한 원문이 걸린다.
    check("글머리표를 지운다", R._fold("• 항목 ◦ 하위 ∙ 가운데") == "항목하위가운데",
          repr(R._fold("• 항목 ◦ 하위 ∙ 가운데")))
    check("가운뎃점(·)은 남긴다 — 글머리표가 아니라 문장 구분자다",
          R._fold("가·나") == "가·나", repr(R._fold("가·나")))

    bullet = Path(d) / "글머리표.md"
    bullet.write_text(
        "> **채널**: #글머리\n\n---\n\n## 2026-08\n\n"
        "**2026-08-19 17:53 · 홍길동**\n"
        "<회의와 별도 내용>\n"
        "• 레지던스 위탁운영계약에 따라 갑PFV는 을에이치엠에 매출계좌 접근에 대한"
        " 권한을 줄 예정임\n",
        encoding="utf-8")

    over = R.context_for(bullet, '"<회의와 별도 내용> 레지던스 위탁운영계약에 따라 갑PFV는 을에이치엠에"')
    check("글머리표를 넘어 이어 적은 인용도 찾는다", "찾지 못" not in over, repr(over[:50]))
    check("그 경우에도 헤더가 함께 온다", "2026-08-19 17:53 · 홍길동" in over)
    miss = R.context_for(bullet, '"• 을에이치엠이 위탁운영계약을 해지하겠다고 통보해 왔습니다"')
    check("글머리표를 무시해도 원문에 없는 인용은 못 찾았다고 적는다", "찾지 못" in miss,
          repr(miss[:50]))

    # ⑲ 백슬래시를 지우나 (할 일 185 의 다섯째 원인, 2026-09-11). 모델이 `\"` 로
    #    이스케이프해 적은 인용이 원문의 `"` 와 같은 값으로 접혀야 한다.
    #    **기대값을 직접 적는다** — 두 `_fold` 를 서로 견주면 접기가 통째로 망가져
    #    늘 빈 문자열을 내도 통과한다(⑰·⑱ 과 같은 양식).
    #    왜 지우기로 했나와 그 대가(원화 기호 75회)는 `review_work._fold` 의 설명에.
    check("백슬래시 이스케이프 따옴표를 지운다",
          R._fold(r'회신에 \"직접 수령\" 이라고 적혀 있었다') == "회신에직접수령이라고적혀있었다",
          repr(R._fold(r'회신에 \"직접 수령\" 이라고 적혀 있었다')))
    check("이스케이프 안 한 원문도 같은 값으로 접힌다",
          R._fold('회신에 "직접 수령" 이라고 적혀 있었다') == "회신에직접수령이라고적혀있었다",
          repr(R._fold('회신에 "직접 수령" 이라고 적혀 있었다')))

    # ⑳ `.some()` 구멍의 **화면 쪽** (할 일 185-곁, 2026-09-11). 관문
    #    (`summary.js` 의 verifyEvidence)은 인용 안의 조각이 **하나만 걸려도** 통과시키는데,
    #    화면은 걸린 쪽의 원문 창만 보여준다. 그래서 **앞 절반은 실존하고 뒤 절반은 지어낸**
    #    인용이 들어오면 사람이 보는 것은 멀쩡한 원문뿐이고, 못 찾은 조각이 있다는 사실
    #    자체가 화면에 안 나온다 — 관문도 사람도 못 거른다.
    #    관문 규칙을 어떻게 정하든 이 표시는 맞다 (WHK 2026-09-11).
    #    **헤더는 실물 계약대로** 적는다 — `HEADER_RE` 가 `**YYYY-MM-DD HH:MM · 이름**` 을
    #    요구하고, 어긋나면 `context_for` 가 「원문에서 찾지 못했습니다」로 이른 반환해서
    #    빨강→초록이 성립하지 않는다. 둘째 check 가 그 확인이다 — **고치기 전에도 PASS** 여야 한다.
    halfmade = Path(d) / "반쪽.md"
    halfmade.write_text(
        "## 2026-09\n\n**2026-09-10 09:00 · 홍길동**\n\n앞조각은 원문에 있는 문장입니다\n",
        encoding="utf-8")

    half = R.context_for(halfmade, '"앞조각은 원문에 있는 문장입니다 ... 지어낸 반쪽 조각입니다요"')
    check("못 찾은 조각이 「원문에 없음」으로 표시됨", "원문에 없음" in half, repr(half))
    check("걸린 조각의 원문 창은 그대로 나옴", "앞조각은 원문에 있는" in half, repr(half[:40]))
    # 못 찾은 조각의 **값**도 화면에 있어야 한다 — 「무언가 못 찾았다」만으로는 사람이
    # 어느 쪽이 지어낸 것인지 모른다. 접힌 모양(공백을 지운 것)으로 나온다.
    check("못 찾은 조각의 값이 화면에 있다", "지어낸반쪽조각입니다요" in half, repr(half[-40:]))
    # 시끄러우면 안 된다 — 전부 찾은 인용에는 이 줄이 안 붙어야 한다.
    quiet = R.context_for(md, '"310억 조기상환 완료했습니다"')
    check("전부 찾은 인용에는 경고가 안 붙는다", "원문에 없음" not in quiet, repr(quiet[-40:]))

    print("\nsummary_block")

    # 같은 자리에 후보가 둘이면 화면에 둘 다 나와야 한다. 파일에만 담기고 화면에 안
    # 나오면 잃은 것과 같다 — 사람은 화면을 보고 정한다.
    twin = {
        "kind": "summary", "channel": "사업장가", "file": "없는채널",
        "type": "새 쟁점", "where": "핵심 쟁점",
        "was": "언급 없음", "now": "앞 후보 — 매입의사 철회",
        "evidence": '"기존 매수의향자 매입의사 철회"',
        "more": [{"now": "뒤 후보 — 만기 8/25 대응", "evidence": '"만기 도래"'}],
    }
    block = "\n".join(R.summary_block(twin, 1, 0))
    check("첫 후보가 화면에 있다", "앞 후보 — 매입의사 철회" in block)
    check("둘째 후보도 화면에 있다", "뒤 후보 — 만기 8/25 대응" in block, repr(block[:60]))
    check("후보가 몇 건인지 적는다", "후보 2건" in block, repr(block.split("\n")[0]))
    check("후보마다 번호가 붙는다", "후보 1:" in block and "후보 2:" in block)

    # 후보가 하나뿐인 흔한 경우는 번호를 붙이지 않는다 — 셀 것이 없는데 세면 시끄럽다
    single = dict(twin); single.pop("more")
    one = "\n".join(R.summary_block(single, 1, 0))
    check("후보가 하나면 번호를 안 붙인다", "후보:" in one and "후보 1:" not in one)

    print("\nsummary_end")

    # 요약 자리는 첫 월 헤딩 앞까지 — `summary.js` 의 extractSummary 와 같은 범위
    check("월 헤딩 앞까지가 요약", R.summary_end(md) == 6, f"end={R.summary_end(md)}")

print("\nsummary_changed_in  (요약 끝 = 6줄)")

# 메시지만 늘어난 커밋 — VM 의 자동 반영이 매일 하는 모양이다. 여기가 걸리면 관문이
# 곧 성가신 것이 되어 꺼진다.
DIFF_BODY_ONLY = """@@ -30,0 +31,2 @@
+**2026-08-12 09:00 · 김실무**
+새 메시지입니다.
"""
check("메시지만 늘어난 것은 요약 변경이 아니다", not R.summary_changed_in(DIFF_BODY_ONLY, 6))

# 핵심 쟁점이 바뀌면 사람 승인의 대상이다
DIFF_TOPIC = """@@ -3 +3 @@
-> **핵심 쟁점**: 310억 조기상환(2026-08-11 예정)
+> **핵심 쟁점**: 조기상환 완료(2026-08-11)
"""
check("핵심 쟁점이 바뀌면 요약 변경", R.summary_changed_in(DIFF_TOPIC, 6))

# 건수 줄은 derive.js·sync_index.py 가 고치는 값이라 뺀다.
# 안 빼면 sync_index 를 돌린 뒤 커밋이 매번 막히고, 그러면 훅을 지우게 된다.
DIFF_COUNT = """@@ -2 +2 @@
-> **기간**: 2026-08-01 ~ 2026-08-11 · **실제 메시지**: 3건
+> **기간**: 2026-08-01 ~ 2026-08-12 · **실제 메시지**: 5건
"""
check("건수 줄만 바뀐 것은 요약 변경이 아니다", not R.summary_changed_in(DIFF_COUNT, 6))

# 건수 줄과 핵심 쟁점이 함께 바뀌면 승인 대상이다 — 건수에 묻어 들어가면 안 된다
DIFF_MIXED = DIFF_COUNT + """@@ -3 +3 @@
-> **핵심 쟁점**: 옛 문장
+> **핵심 쟁점**: 새 문장
"""
check("건수와 함께 바뀌어도 요약 변경은 잡는다", R.summary_changed_in(DIFF_MIXED, 6))

# 지운 줄은 새 파일 줄 번호로 못 맞춘다 — 그 자리로 보고 막는 쪽으로 간다
DIFF_DELETE = """@@ -3 +2,0 @@
-> **핵심 쟁점**: 통째로 지움
"""
check("요약 줄을 지운 것도 잡는다", R.summary_changed_in(DIFF_DELETE, 6))

print("\nis_count_line")

# ⑤ 자동으로 갱신되는 건수 줄. 사람 승인의 대상이 아니라 관문에서 뺀다 —
#    안 빼면 sync_index.py 를 돌린 뒤 커밋이 매번 막힌다.
check("건수 줄은 제외", R.is_count_line("> **기간**: 2026-08-01 ~ 2026-08-11 · **실제 메시지**: 3건"))
check("핵심 쟁점은 요약", not R.is_count_line("> **핵심 쟁점**: 310억 조기상환"))
check("메시지 헤더는 요약이 아니다", not R.is_count_line("**2026-08-11 14:20 · 김실무**"))

print("\napplied_after  (반영한 것을 목록에서 빼는 판정)")

# 「반영」은 `.sync-state.json` 의 `applied` 에 적히는데(2026-09-07 부터. 그전에는
# 로컬 전용 `.decision-stamp.json`) 목록이 그 기록을 안 읽어서, 반영·커밋·push 를
# 끝낸 4건이 그대로 다시 보였다 (2026-08-12 첫 실사용).
# 무조건 감추면 내일 같은 자리가 **다시** 어긋나도 영영 안 보인다 — 그래서 「이 목록이
# 만들어진 뒤에 정한 것」만 감춘다. 목록이 새로 만들어지면 저절로 다시 보인다.
GEN = "2026-08-12T03:51:00.990Z"
check("목록보다 나중에 정했으면 감춘다",
      R.applied_after({"at": "2026-08-12T12:58:11+09:00"}, GEN))
check("목록보다 먼저 정한 것은 감추지 않는다",
      not R.applied_after({"at": "2026-08-11T09:00:00+09:00"}, GEN))
check("같은 시각대라도 시간대를 맞춰 비교한다",
      not R.applied_after({"at": "2026-08-12T03:50:00+00:00"}, GEN))
check("정한 기록이 없으면 감추지 않는다", not R.applied_after(None, GEN))
check("시각을 못 읽으면 감추지 않는다", not R.applied_after({"at": "어제"}, GEN))
check("목록에 만든 시각이 없으면 감추지 않는다",
      not R.applied_after({"at": "2026-08-12T12:58:11+09:00"}, ""))

print("\nconfirmed_at  (그 항목을 마지막으로 다시 본 시각)")

# 위 applied_after 는 목록 전체의 `generated` 와 견준다. 그 전제는 「도장이 새로워졌다 =
# 그 항목을 새 증거로 다시 봤다」인데, **요약이 아닌 항목에는 성립하지 않는다** —
# 새 채널·개명·note·파생값은 `pending-work.js` 의 mergeItems 에서 무조건 이월되는데
# (다시 안 잡히므로 치우면 그대로 사라진다) 07:00 회차는 대조를 했으므로 도장을
# **정당하게** 앞당긴다. 그러면 반영을 끝낸 항목이 다음 날 아침에 그대로 다시 뜬다.
# 「빼」·「나중에」는 저장소에 남아 걸러지는데 옳은 답인 「반영」만 안 붙는 셈이다.
# 그래서 항목에 붙은 `lastSeen`(그 회차에 실제로 검출됐을 때만 찍힌다)과 견준다.
NEWER = "2026-08-13T22:00:00.000Z"
check("항목에 lastSeen 이 있으면 그것과 견준다",
      R.confirmed_at({"lastSeen": "2026-08-09T22:00:00.000Z"}, NEWER)
      == "2026-08-09T22:00:00.000Z")
check("lastSeen 이 없으면 목록의 generated 로 물러선다",
      R.confirmed_at({}, NEWER) == NEWER)
# 옛 형식 파일에는 lastSeen 이 없다. 물러서는 쪽이 `generated` 라야 **보이는 쪽**으로
# 틀린다 — 안 보이는 쪽으로 틀리면 사람이 영영 모른다 (applied_after 와 같은 방향).
check("빈 lastSeen 도 없는 것으로 본다", R.confirmed_at({"lastSeen": ""}, NEWER) == NEWER)

# 이어 붙였을 때 실제로 감춰지는가 — 이것이 이 고침의 목적이다
_applied = {"at": "2026-08-12T23:00:00+00:00"}
_item = {"lastSeen": "2026-08-12T22:00:00.000Z"}
check("반영한 비요약 항목은 다음 대조 회차에도 감춰진다",
      R.applied_after(_applied, R.confirmed_at(_item, NEWER)))
check("도장으로 견주면 되살아난다 (고치기 전 동작)",
      not R.applied_after(_applied, NEWER))

# 위 둘은 판정 함수만 본다. **`load_items` 가 실제로 그 값을 넘기는지**는 따로 봐야 한다 —
# 함수를 만들어 두고 호출 자리를 안 바꾸면 시험은 전부 통과하는데 화면은 그대로다
# (2026-08-13 에 역검증이 이걸 잡았다: 호출 자리를 되돌렸는데 시험이 안 빨개졌다).
with tempfile.TemporaryDirectory() as d:
    dd = Path(d)
    work, stamp, state = dd / "work.json", dd / "stamp.json", dd / "state.json"
    # 07:00 회차가 도장을 앞당겼지만 이 항목은 이번에 다시 검출되지 않았다(lastSeen 이 옛 값).
    work.write_text(json.dumps({
        "generated": "2026-08-13T22:00:00.000Z",
        "items": [{
            "id": "b|new-channel", "kind": "new-channel", "channel": "b",
            "firstSeen": "2026-08-09", "lastSeen": "2026-08-12T22:00:00.000Z",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    # 「반영」은 **저장소에 들어가는 상태 파일**에 있다 (2026-09-07 부터) — 로컬 도장
    # 파일에 넣어 두면 화면만 감추고 VM 은 계속 센다. 도장 파일은 일부러 비워 둔다:
    # 옛 자리에서 읽고 있으면 여기가 빨개진다.
    stamp.write_text("{}", encoding="utf-8")
    state.write_text(json.dumps(
        {"applied": {"b|new-channel": {"at": "2026-08-12T23:00:00+00:00"}}},
    ), encoding="utf-8")

    _saved = (R.PENDING_WORK, R.STAMP, R.STATE)
    R.PENDING_WORK, R.STAMP, R.STATE = work, stamp, state
    try:
        got = R.load_items()
    finally:
        R.PENDING_WORK, R.STAMP, R.STATE = _saved
    check("load_items 가 반영한 비요약 항목을 뺀다", got == [], f"{len(got)}건 남음")

print("\ngate — git 이 실패하면 「볼 것 없음」이 아니다")

# 관문은 「담긴 채널 md 가 없다」와 「저장소를 못 읽었다」를 **같은 0** 으로 답하고 있었다.
# 그러면 훅이 걸려 있는데 아무것도 안 지키는 상태가 되고, 그 사실이 화면에 안 나온다.


class _FakeRun:
    def __init__(self, rc=128, out="", err="fatal: not a git repository"):
        self.returncode, self.stdout, self.stderr = rc, out, err


_keep_run = R.subprocess.run
try:
    R.subprocess.run = lambda *a, **k: _FakeRun()
    _code = R.gate()
finally:
    R.subprocess.run = _keep_run
check("git 이 실패하면 관문이 1 을 낸다", _code == 1, repr(_code))

# 스킬을 `<저장소>/.claude/skills/<이름>/scripts/` 가 아닌 곳에 깔면 ROOT 가 엉뚱한 곳을
# 가리킨다. 그때 「통과」라고 답하면 안 된다 — 무엇을 봤는지 모르는 상태다.
with tempfile.TemporaryDirectory() as _d:
    _keep_root = R.ROOT
    try:
        R.ROOT = Path(_d)
        _code2 = R.gate()
    finally:
        R.ROOT = _keep_root
check("저장소가 아닌 곳에서는 통과시키지 않는다", _code2 == 1, repr(_code2))

print("\n할 일 목록 — 「없다」와 「못 읽는다」는 다른 사건이다")

# `_read_json` 은 둘을 같은 None 으로 뭉갰다. 그러면 `load_items()` 가 [] 를 돌려주고
# 종료코드 0 으로 끝나서 상황판에 「아침 보고 0건」이 찍힌다 — 파일이 있으니
# `board.py` 의 `probe_work` 도 실패 경로로 안 간다. 아무 데서도 안 드러난다.

with tempfile.TemporaryDirectory() as _d:
    _missing = Path(_d) / "없는파일.json"
    check("파일이 없으면 0건이다 (예외가 아니다)", R.read_pending_work(_missing) == {})

    _broken = Path(_d) / ".pending-work.json"
    # git pull 충돌 표식이 들어간 모양. 이 파일은 추적되는 파일이라 실제로 이렇게 된다.
    _broken.write_text('<<<<<<< HEAD\n{"items": []}\n=======\n', encoding="utf-8")
    try:
        R.read_pending_work(_broken)
        _raised = False
    except R.PendingWorkUnreadable:
        _raised = True
    check("있는데 못 읽으면 예외를 낸다", _raised)

    # main() 이 그 예외를 0 이 아닌 값으로 끝내는지. 0 이면 상황판이 「0건」이라고 적는다.
    _keep_pw = R.PENDING_WORK
    try:
        R.PENDING_WORK = _broken
        _code3 = R.main(["--json"])
        _code4 = R.main([])
    finally:
        R.PENDING_WORK = _keep_pw
check("--json 이 0 으로 끝나지 않는다", _code3 != 0, repr(_code3))
check("화면 출력도 0 으로 끝나지 않는다", _code4 != 0, repr(_code4))

# 관문은 목록을 안 본다 — 아침 목록이 깨졌다고 커밋이 막히면 안 된다.
check("`--gate` 는 목록을 읽지 않는다", "load_items" not in R.gate.__code__.co_names)

print("\n해시는 스테이징을 본다 — 작업 트리가 아니다")

# 도장은 작업 트리에서 찍는데 관문이 그것을 그대로 대면, `git add -p` 로 조각만 골라
# 담았을 때 **승인받은 것과 다른 조합이 커밋된다.** 문서 쪽 `review_batch.py` 와 같다.
# **여기서 만드는 것은 완전히 별개인 임시 저장소다** — WHK 저장소를 건드리지 않는다.
with tempfile.TemporaryDirectory() as _d:
    _repo = Path(_d)
    _sub = lambda *a: R.subprocess.run(["git", *a], cwd=_repo, capture_output=True, text=True)
    _sub("init", "-q")
    _sub("config", "user.email", "t@t"); _sub("config", "user.name", "t")
    _f = _repo / "채널.md"
    _f.write_text("> **핵심 쟁점**: 승인받은 문장\n", encoding="utf-8")
    _sub("add", "채널.md")
    _f.write_text("> **핵심 쟁점**: 승인 안 받은 문장\n", encoding="utf-8")

    _keep_root = R.ROOT
    try:
        R.ROOT = _repo
        _staged, _work = R.staged_fingerprint(_f), R.fingerprint(_f)
    finally:
        R.ROOT = _keep_root
    check("두 해시가 갈린다", _staged != _work, f"{_staged} vs {_work}")
    check("인덱스 해시는 담긴 내용의 것이다",
          _staged == R._digest("> **핵심 쟁점**: 승인받은 문장\n".encode("utf-8")), _staged)

# 줄끝만 다른 것은 같은 내용으로 본다 — 안 맞추면 관문이 늘 막아 곧 꺼진다.
check("줄끝 차이는 해시를 가르지 않는다", R._digest(b"a\r\nb\r\n") == R._digest(b"a\nb\n"))

print("\n화면 번호와 --only 번호는 같은 목록을 봐야 한다")

# 파일 순서는 이월분(비요약)이 앞, 새 요약 후보가 뒤다 (`mergeItems` 가 이월을 앞에 쌓는다).
# 화면(render)은 요약 계열을 먼저 번호 매기므로, load_items 가 그 순서로 주지 않으면
# **화면의 1번과 `decide_work --only 1` 이 서로 다른 항목**이 된다 — 사람이 요약을
# 빼려다 비요약 항목을 영구 제외하게 되는 자리다 (2026-09-04 리뷰에서 확인).
with tempfile.TemporaryDirectory() as _d:
    _dp = Path(_d)
    _work_f = _dp / "pending-work.json"
    _state_f = _dp / "state.json"
    _work_f.write_text(json.dumps({
        "generated": "2026-09-04T22:00:00Z",
        "items": [
            {"id": "chA|renamed", "kind": "renamed", "channel": "chA", "detail": "옛이름 → 새이름"},
            {"id": "chB|note|abc0", "kind": "note", "channel": "chB", "detail": "확인할 것"},
            {"id": "chC|summary", "kind": "summary", "channel": "chC", "summaryHash": "h1"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    _state_f.write_text("{}", encoding="utf-8")
    _keep = R.PENDING_WORK, R.STATE, R.STAMP
    try:
        R.PENDING_WORK, R.STATE, R.STAMP = _work_f, _state_f, _dp / "stamp.json"
        _items = R.load_items()
    finally:
        R.PENDING_WORK, R.STATE, R.STAMP = _keep
    check("요약 계열이 목록 맨 앞에 온다",
          bool(_items) and _items[0].get("kind") in R.SUMMARY_KINDS,
          "순서: " + ", ".join(str(it.get("kind")) for it in _items))
    check("비요약끼리는 파일 순서 그대로다",
          [it.get("id") for it in _items if it.get("kind") not in R.SUMMARY_KINDS]
          == ["chA|renamed", "chB|note|abc0"])

print("\n전부 통과" if _ok else "\n실패 있음")
sys.exit(0 if _ok else 1)
