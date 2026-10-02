#!/usr/bin/env python3
"""
문서 md 가 Hermes 파싱 계약을 지키는지 검사한다.

계약이 깨져도 Hermes 는 **에러를 내지 않는다.** 검색이 조용히 0건이 되고
봇은 "아카이브에 없습니다" 라고 답한다. 그래서 눈으로 보는 대신 여기서 잡는다.

검사 항목 (원본: 코드 저장소의 src/archive.js, documents.js)
  1. `# ` 제목이 정확히 하나
  2. `## ` 헤딩이 'YYYY-MM' 과 '회차 요약' 뿐            → readChannel 월 슬라이싱
  3. 회차 헤더가 1건 이상 (splitMessages 정규식)         → 검색 히트 단위
  4. 메타 블록에 '열람' 이 있고 값이 공개/비공개          → fail-closed 로 안 빠지게
  5. 메타의 '수록 회차' 가 실제 회차 수와 일치
  5-1. 엑셀: 메타의 '시트' 개수·이름이 본문의 시트 헤더와 일치
  5-2. 엑셀: 본문의 가린 칸(***)과 '마스킹' 메타 줄이 둘 다 있거나 둘 다 없음
  6. 회차 날짜가 속한 월 섹션과 맞음
  7. 회차·월이 최신순으로 정렬됨
  8. 월 섹션 안의 본문이 전부 회차 헤더 **아래**에 있음   → 헤더 없는 본문은 검색에서 사라진다

사용:
  python verify_format.py <md 경로> [...]
  python verify_format.py --all          # documents/projects 전체

종료코드: 0 통과 / 1 실패
"""

import argparse
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from paths import CONFIG, DATA_ROOT, PROJECTS  # noqa: E402

# `ROOT` 는 **자료 저장소**다 — 아래 relative_to 로 문서 경로를 짧게 보일 때 쓴다.
ROOT = DATA_ROOT

# `archive.js` 의 `splitMessages` 와 같은 정규식
ENTRY_RE = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2})[^*]*\*\*\s*$")
MONTH_RE = re.compile(r"^##\s+(\d{4}-\d{2})\s*$")
H2_RE = re.compile(r"^##\s+(.*?)\s*$")
H1_RE = re.compile(r"^#\s+")
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
ALLOWED_H2 = {"회차 요약"}

_PRIVATE_CACHE = None


def private_channels() -> set:
    """Hermes `config.json` 의 privateChannels. 코드와 같은 목록을 봐야 판정이 안 갈린다.

    **못 읽으면 멈춘다.** 예전에는 빈 집합으로 넘어가 「비공개 채널 검사를 건너뜁니다」 한 줄만
    찍었는데, 종료코드는 problems 개수로만 정해져 그대로 ✓ 가 됐다. 자동 반영의 관문(runGate)이
    이 스크립트를 부르므로, 그 상태에서는 **관문의 비공개 검사가 통째로 빈 검사**가 되고 아무도
    모른다. 이 파일은 검사할 문서와 같은 저장소(자료 저장소) 뿌리에 있어, 못 읽는다는 것
    자체가 이상 신호다 (2026-08-10 · 자리는 2026-08-31 에 옮김).
    """
    global _PRIVATE_CACHE
    if _PRIVATE_CACHE is None:
        cfg = CONFIG
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            raise SystemExit(
                f"✗ privateChannels 를 읽지 못했습니다 — {cfg}\n  {e}\n"
                "  이게 없으면 비공개 채널 문서 검사가 빈 검사가 되어 통과합니다. 검사를 멈춥니다."
            )
        _PRIVATE_CACHE = {str(c).lstrip("#").strip() for c in d.get("privateChannels", [])}
    return _PRIVATE_CACHE


META_KEY_RE = re.compile(r"\*\*([^*]+)\*\*:")
META_PAIR_RE = re.compile(r"\*\*([^*]+)\*\*:\s*([^·\n]+)")


def parse_meta(meta_text: str) -> dict:
    """
    메타 블록을 `{키: 값}` 으로. **`documents.js` 의 parseMeta 와 같은 규칙이어야 한다.**

    한 줄에 키가 하나면 줄 끝까지가 값이고(그래야 '주요 항목' 처럼 값 안에 '·' 를 쓰는 항목이
    첫 가운뎃점에서 안 잘린다), 여럿이면 '·' 앞까지가 값이다.

    규칙이 갈리면 **검사는 통과인데 봇은 막는** 상태가 조용히 생긴다 — 예전에는 여기서
    `\\S+` 로 첫 낱말만 봐서 `열람: 공개 (스레드 승인)` 이 ✓ 였고 봇은 비공개로 막았다.
    """
    out = {}
    for line in meta_text.split("\n"):
        keys = list(META_KEY_RE.finditer(line))
        if not keys:
            continue
        if len(keys) == 1:
            k = keys[0]
            out[k.group(1).strip()] = line[k.end():].strip()
            continue
        for m in META_PAIR_RE.finditer(line):
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def meta_lines(lines) -> list:
    """첫머리의 인용(`>`) 메타 블록을 `>` 를 뗀 줄 목록으로.

    **`archive.js` 의 `metaBlock` 과 같은 규칙이어야 한다** — 특히 블록 **안의 빈 줄은
    건너뛰고 계속 읽는다**(그쪽 `else if (started && line.trim() === '') continue`).
    여기서 멈춰 버리면 빈 줄 뒤의 `열람`·`공개승인` 을 못 보는데, 봇은 그 줄을 읽으므로
    **검사는 통과인데 공개 여부 판정이 갈리는** 상태가 조용히 생긴다.

    `review_batch.py` 도 이 함수를 쓴다 — 파서를 두 벌 두면 반드시 갈린다.
    """
    meta, started = [], False
    for line in lines:
        if line.startswith(">"):
            started = True
            meta.append(line.lstrip("> ").strip())
        elif started and line.strip() == "":
            continue
        elif started:
            break
    return meta


def check(path: Path):
    """(문제 목록, 정보 목록)"""
    problems, notes = [], []
    lines = path.read_bytes().decode("utf-8").replace("\r\n", "\n").split("\n")

    # 1. 제목
    h1 = [l for l in lines if H1_RE.match(l)]
    if len(h1) != 1:
        problems.append(f"'# ' 제목이 {len(h1)}개입니다 (1개여야 함). 본문 헤딩 강등을 빠뜨렸는지 보세요")

    # 2. ## 헤딩
    months, bad_h2 = [], []
    for i, l in enumerate(lines):
        m = H2_RE.match(l)
        if not m:
            continue
        if MONTH_RE.match(l):
            months.append((i, MONTH_RE.match(l).group(1)))
        elif m.group(1) in ALLOWED_H2:
            pass
        else:
            bad_h2.append(m.group(1))
    if bad_h2:
        problems.append(
            f"허용되지 않은 '## ' 헤딩: {bad_h2} — 월 섹션이 여기서 잘립니다. "
            "본문 헤딩을 2단계 강등하세요"
        )

    # 3. 회차
    entries = [(i, ENTRY_RE.match(l).group(1)) for i, l in enumerate(lines) if ENTRY_RE.match(l)]
    if not entries:
        problems.append("회차 헤더가 없습니다 — 검색에 절대 걸리지 않습니다")

    # 4. 메타
    meta = meta_lines(lines)
    meta_text = "\n".join(meta)
    # parse_meta("") 는 {} 를 돌려주므로 메타가 없어도 안전하다 — 아래 5-1 이 이 값을 쓰는데,
    # else 분기 안에서만 만들면 메타가 아예 없는 문서에서 NameError 로 죽는다.
    meta_kv = parse_meta(meta_text)
    if not meta:
        problems.append("메타 블록('>' 줄)이 없습니다 — 비공개로 처리되어 답변에 안 나옵니다")
    else:
        # 4-0. 열람·공개승인 은 **자기 줄에 혼자** 있어야 한다.
        #
        # 다른 항목과 한 줄에 있으면 파서가 값을 `·` 로 끊어 읽는다. 그래서 **값 안에 적은
        # 설명이 키가 된다** — 2026-08-07 에 「비공개 사유」 값 안에 설명하려고
        # `**열람**: 공개` 라고 적었더니 실제로 `열람` 키로 잡혔다.
        #
        # 그때 문서가 열리지는 않았다(실측). 값이 줄 끝까지 삼켜져 '공개' 와 정확히 같지
        # 않았고, `**공개승인**` 은 뒤에 콜론이 없어 키가 아니었다. **하지만 한 줄이
        # `**열람**: 공개 · **공개승인**: 값` 모양이 되는 순간 그 문서는 열린다** — 이것도
        # 실측으로 확인했다. 차이가 가운뎃점 위치와 콜론 하나뿐이라 문구를 손보다 넘어가기
        # 쉽다. 값 안에 메타 표기를 쓰지 않는 것이 규칙이고, 이 검사가 그것을 강제한다.
        for line in meta:
            keys = [k.strip() for k in META_KEY_RE.findall(line)]
            if len(keys) > 1:
                mixed = sorted({"열람", "공개승인"} & set(keys))
                if mixed:
                    problems.append(
                        f"'{', '.join(mixed)}' 가 다른 항목과 한 줄에 있습니다 ({', '.join(keys)}) — "
                        "그 줄만 따로 두세요. 값 안에 '**열람**:' 같은 메타 표기를 쓰면 키로 잡힙니다"
                    )

        access = meta_kv.get("열람")
        if access is None:
            problems.append("메타에 '**열람**' 이 없습니다 — 비공개로 처리됩니다 (fail-closed)")
        elif access not in ("공개", "비공개"):
            # 봇은 `열람` 값이 '공개' 와 **정확히 같을 때만** 연다(documents.js 의 loadDocument).
            # 예전에는 여기서 첫 낱말만 봐서 `열람: 공개 (스레드 승인)` 같은 줄이 ✓ 로 통과했는데,
            # 봇은 그것을 비공개로 막았다 — 검사는 통과인데 답변에는 안 나오는 상태가 된다.
            problems.append(f"'**열람**: {access}' — '공개' 또는 '비공개' 여야 합니다 (뒤에 덧말을 붙이면 봇이 막습니다)")

        # 4-1. 비공개 채널 문서의 공개 승인 (WHK 결정 2026-08-05)
        # documents.js 는 비공개 채널 문서를 `열람: 공개` + `공개승인` **두 줄이 다 있을 때만**
        # 연다. 한 줄만 있으면 봇은 계속 막는데 md 는 공개라고 말하는 어긋난 상태가 된다.
        approved_val = meta_kv.get("공개승인")
        approved = bool(approved_val)
        if path.parent.name in private_channels():
            if access == "공개" and not approved:
                problems.append(
                    "비공개 채널 문서인데 '**공개승인**' 이 없습니다 — "
                    "봇은 계속 비공개로 막습니다. 슬랙 스레드의 [공개] 댓글을 옮겨 적으세요"
                )
            elif access == "공개" and approved:
                notes.append(f"비공개 채널이지만 공개 승인됨 — {approved_val}")
            elif access == "비공개" and approved:
                problems.append(
                    "'**공개승인**' 이 있는데 '**열람**: 비공개' 입니다 — 봇은 막습니다. "
                    "공개하려면 '열람'을 '공개'로, 아니면 '공개승인' 줄을 지우세요"
                )
        elif approved:
            problems.append(
                "'**공개승인**' 은 비공개 채널 문서에만 씁니다 — 공개 채널 문서에는 불필요합니다"
            )

    # 5. 수록 회차
    cm = re.search(r"\*\*수록 회차\*\*:\s*(\d+)건", meta_text)
    if cm and int(cm.group(1)) != len(entries):
        problems.append(
            f"메타의 수록 회차 {cm.group(1)}건 ≠ 실제 {len(entries)}건 — insert_entry.py --refresh 를 돌리세요"
        )

    # 5-1. 시트 (엑셀)
    #
    # 엑셀은 '수록 회차' 대신 '시트' 를 쓴다 — 시트는 개정 이력이 아니라 한 파일의 구획이라,
    # 회차로 세면 documents.js 의 isSeriesDoc 이 시리즈로 굳혀 색인에서 영원히 안 접힌다.
    #
    # **메타에 키가 없는 경우를 본문으로 잡는다.** 예전에는 `if sheet_meta:` 로 시작해서
    # 그 줄을 아예 안 쓰면 검사가 하나도 안 돌았다 — 그런데 봇의 `isSheetDoc` 은 바로
    # 그 한 줄로만 엑셀을 가르므로, 빠뜨린 문서는 시트마다 회차 하나로 세어져 색인에서
    # **영원히 안 접히는 줄**이 된다(실측: 엑셀 4건이 메타가 있으면 `그 외 4건` 으로
    # 접히고, 없으면 네 줄을 그대로 차지하는데 이 검사는 양쪽 다 0건이라고 답했다).
    sheet_meta = meta_kv.get("시트")
    # 본문 시트 이름은 **양쪽 끝 공백을 떼고** 견준다. `parse_meta` 가 메타 값을 이미
    # 떼기 때문에, 안 떼면 끝에 공백이 붙은 시트 이름(어느 사업장의 산정내역 엑셀에
    # 실재한다) 같은 것이 화면에 똑같이 보이면서 영영 안 맞는다. 사람이 md 를 고쳐도
    # 메타 쪽이 다시 떼여 벗어날 길이 없다. 봇의 `fold()` 도 공백을 떼고 찾으므로
    # `read_document sheet:` 는 원래부터 정상이었다 — 이건 거짓 실패였다.
    got = [m.group(1).strip()
           for m in (re.search(r"— 시트 \d+/\d+: (.+?)\*\*", lines[i]) for i, _ in entries) if m]
    if not sheet_meta:
        if got:
            problems.append(
                f"본문에 시트 헤더가 {len(got)}건 있는데 '**시트**' 메타 줄이 없습니다 — "
                "봇이 시트를 회차로 세어 색인에서 영원히 안 접힙니다 "
                "('> **시트**: 이름 · 이름' 한 줄을 넣으세요)"
            )
    else:
        want = [s.strip() for s in sheet_meta.split("·") if s.strip()]
        if len(got) != len(entries):
            problems.append(
                f"'**시트**' 가 있는데 회차 {len(entries)}건 중 {len(got)}건만 시트 헤더입니다 "
                "— 헤더는 '**YYYY-MM-DD · 원본명 — 시트 n/N: 이름**' 이어야 합니다"
            )
        elif got != want:
            problems.append(
                f"메타의 시트 {want} ≠ 본문의 시트 {got} — xlsx_to_blocks.py 의 meta.json 과 맞추세요"
            )

    # 5-2. 마스킹 (엑셀)
    #
    # `meta.json` 의 masked 정보는 xlsx_to_blocks.py 가 만들지만 md 에는 사람이 옮겨
    # 적어야 한다 — 안 옮기면 **가려진 이유가 md 어디에도 안 남는다.** 봇은 `***` 로
    # 빈 셀을 보고 "자료에 없다"고 답하는데, 실제로는 "가려져 있다"가 맞는 답이다.
    #
    # 양쪽으로 다 본다: 본문에 `***` 가 있는데 `마스킹` 줄이 없으면 ✗(위 문제),
    # `마스킹` 줄이 있는데 본문에 `***` 가 없으면 ✗(메타가 낡았거나 마스킹이
    # 실제로는 안 걸렸다는 뜻 — 2026-08-13 에 한쪽만 보는 검사가 반나절 동안
    # 갈린 채로 통과했던 것과 같은 함정이라 처음부터 양방향으로 만든다).
    body_text = "\n".join(lines)
    has_mask_marks = "***" in body_text
    if sheet_meta and has_mask_marks and not meta_kv.get("마스킹"):
        problems.append(
            "본문에 가린 칸(***)이 있는데 '**마스킹**' 메타 줄이 없습니다 — "
            "'> **마스킹**: 대표자명 · 핸드폰 (N행)' 한 줄을 넣으세요"
        )
    if meta_kv.get("마스킹") and not has_mask_marks:
        problems.append(
            "'**마스킹**' 메타 줄이 있는데 본문에 가린 칸(***)이 없습니다 — "
            "메타가 낡았거나 마스킹이 안 걸렸습니다"
        )

    # 6. 회차가 제 월 섹션에 있는지
    for idx, date in entries:
        owner = None
        for mi, mon in months:
            if mi < idx:
                owner = mon
            else:
                break
        if owner is None:
            problems.append(f"회차 {date} 가 월 섹션 밖에 있습니다")
        elif owner != date[:7]:
            problems.append(f"회차 {date} 가 '## {owner}' 섹션 안에 있습니다")

    # 7. 정렬
    mons = [m for _, m in months]
    if mons != sorted(mons, reverse=True):
        problems.append(f"월 섹션이 최신순이 아닙니다: {mons}")
    dates = [d for _, d in entries]
    if dates != sorted(dates, reverse=True):
        problems.append("회차가 최신순이 아닙니다")

    # 8. 월 섹션 안에 회차 헤더 없이 놓인 본문
    #
    #   splitMessages 는 회차 헤더를 기준으로 자르므로, 헤더보다 **위**에 있는 본문은
    #   어느 회차에도 안 붙어 검색에서 통째로 사라진다. 에러도 안 나고 회차 수도 안 변해서
    #   3번 검사(회차 1건 이상)에는 걸리지 않는다 — insert_entry.py 에 헤더 없는 블록을
    #   넘겼을 때가 이 형태다 (2026-08-04 사업장나 잔금수금 7회차에서 실제로 겪었다).
    entry_at = {i for i, _ in entries}
    for mi, mon in months:
        end = next((j for j in range(mi + 1, len(lines)) if H2_RE.match(lines[j])), len(lines))
        first = next((j for j in range(mi + 1, end) if j in entry_at), None)
        if first is None:
            problems.append(
                f"'## {mon}' 섹션에 회차 헤더가 없습니다 — 이 섹션 본문은 검색에 안 걸립니다"
            )
            first = end
        orphan = [lines[j] for j in range(mi + 1, first) if lines[j].strip() and lines[j] != "---"]
        if orphan:
            problems.append(
                f"'## {mon}' 섹션에서 회차 헤더보다 앞에 본문 {len(orphan)}줄이 있습니다 — "
                f"검색에 안 걸립니다. 첫 줄: {orphan[0][:60]!r} "
                "(insert_entry.py 에 넘기는 블록 첫 줄이 '**YYYY-MM-DD · 원본파일명**' 인지 보세요)"
            )

    # 9. 이미지만 있고 본문이 없는 회차 (OCR 을 빠뜨린 스캔본)
    #
    #   스캔 PDF 는 본문이 한 글자도 없어도 쪽마다 `![image](...)` 가 한 줄씩 붙어
    #   파일 길이로는 수백~수천 자로 잡힌다. 그래서 "결과가 200자 미만이면 OCR" 을
    #   파일 길이로 재면 통과해 버린다 (2026-08-04 어느 사업장의 계약서 4건이 414·446·
    #   734·3,166자로 통과했는데 실제 텍스트는 전부 0자였다).
    #   이대로 들어가면 봇은 문서가 있다고 답하면서 내용은 대지 못한다 —
    #   검색이 0건이 되는 것보다 알아채기 어렵다.
    for idx, (i, date) in enumerate(entries):
        end = entries[idx + 1][0] if idx + 1 < len(entries) else len(lines)
        block = "\n".join(lines[i + 1 : end])
        text = IMG_RE.sub("", block).strip()
        imgs = len(IMG_RE.findall(block))
        if imgs and len(text) < 200:
            problems.append(
                f"회차 {date}: 이미지 {imgs}개뿐이고 본문이 {len(text)}자입니다 — "
                "OCR 을 빠뜨린 스캔본입니다. kordoc 에 --ocr 를 붙여 다시 변환하세요"
            )

    # 참고 — 문제는 아니지만 눈여겨볼 것
    fake = [l for l in lines if re.match(r"^\*\*\d{4}", l) and not ENTRY_RE.match(l)]
    if fake:
        notes.append(f"회차로 오인될 뻔한 줄 {len(fake)}개 (현재는 정규식에 안 걸림): {fake[:2]}")

    return problems, notes, len(entries)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="검사할 md 경로")
    ap.add_argument("--all", action="store_true", help="documents/projects 전체")
    args = ap.parse_args()

    targets = [Path(p) for p in args.paths]
    if args.all:
        targets += sorted(PROJECTS.rglob("*.md"))
    if not targets:
        print("검사할 파일이 없습니다. 경로를 주거나 --all 을 쓰세요.")
        return 1

    failed = 0
    for path in targets:
        if not path.exists():
            print(f"✗ {path} — 파일 없음")
            failed += 1
            continue
        problems, notes, n = check(path)
        rel = path.relative_to(ROOT) if ROOT in path.parents else path
        if problems:
            failed += 1
            print(f"✗ {rel}  (회차 {n}건)")
            for p in problems:
                print(f"    - {p}")
        else:
            print(f"✓ {rel}  (회차 {n}건)")
        for note in notes:
            print(f"    · {note}")

    print(f"\n{len(targets) - failed}/{len(targets)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
