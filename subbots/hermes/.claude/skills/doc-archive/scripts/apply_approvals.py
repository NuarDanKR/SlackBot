#!/usr/bin/env python3
"""
슬랙 스레드의 `[공개]` 승인을 문서 md 메타에 옮겨 적는다.

**왜 따로 있나.** 승인은 문서가 아카이브에 들어간 **뒤에** 달린다. 그런데 수집
(`fetch_slack_files.py`)은 이미 변환된 파일을 태그를 읽기 전에 건너뛴다(그쪽 342·347행).
그래서 `doc-archive` 를 다시 돌려도 그 승인은 화면에 안 뜨고, 팀은 댓글을 달았는데 봇은
계속 막는 상태가 조용히 이어진다. 2026-08-05 의 승인 1건이 이틀 동안 그랬고, 그 사이
스킬을 두 번 돌렸지만 한 번도 뜨지 않았다. Hermes 위생 점검(09:00)이 그 상태를 DM 으로
알리고, **고치는 것이 이 스크립트다.**

**여는 일만 한다. 닫지 않는다.** 슬랙에는 승인을 취소하는 태그가 없다. 열려 있는 문서를
여기서 닫으면 근거 없이 닫는 것이 되므로, 이미 열린 문서는 건드리지 않고 건너뛴다.

**다시 변환하지 않는다.** 원본은 이미 md 로 들어가 있고 고칠 것은 메타 두 줄뿐이다.
재변환하면 같은 문서가 두 벌 쌓인다.

무엇을 고치나 (`documents.js` 의 `loadDocument` 가 보는 두 줄):
  `**열람**: 비공개`  →  `**열람**: 공개`
  `**비공개 사유**: …` 줄 삭제
  `**공개승인**: 슬랙 스레드 [공개] 댓글 · <승인 시각> · <승인한 사람>` 줄 추가

**건별로 고를 수 있다.** `--apply` 만 주면 걸린 승인을 전부 여는데, 그 순간 그 문서들이
팀 전원의 답변 근거가 된다. 목록에 번호가 찍히므로 `--only 2,4` 로 그것만 연다.

사용:
  python apply_approvals.py                    # 뭐가 열릴지만 본다 (기본)
  python apply_approvals.py --apply            # 걸린 승인 전부
  python apply_approvals.py --only 2,4 --apply # 목록의 2·4번만
  python apply_approvals.py --only 합의서 --apply  # 파일명에 '합의서' 가 든 것만
  python apply_approvals.py --channel 비공개나  # 그 채널만
  python apply_approvals.py --days 0           # 기간 제한 없이 (기본 60일)

고친 뒤에는 `verify_format.py` 로 검증하고, **커밋·push 는 사람 승인을 받는다.**
push 하는 순간 그 문서가 팀 전원의 답변 근거가 된다.

종료코드: 0 성공 / 1 실패
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from paths import DOCS_DIR  # noqa: E402
from fetch_slack_files import (  # noqa: E402
    DEFAULT_CONFIG, DEFAULT_ENV, DEFAULT_STATE, PUBLIC_TAG,
    active_deferred, api, bot_channels, history_with_threads, is_bot_message,
    name_key, private_channels, read_token, skip_channels,
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# doc-archive 가 다루는 확장자. **같은 목록이 세 자리에 있다** — 여기,
# `fetch_slack_files.py` 의 `DOC_EXTS`, `src/archive-health.js` 의 `DOC_EXTS`.
# 갈렸는지는 `scripts/check-shared-rules.js` 의 절 ④ 가 본다.
DOC_EXTS = {"pdf", "hwp", "hwpx", "docx", "doc", "pptx", "xlsx", "xlsm"}

# 메타 한 줄 안에서 `**열람**:` 의 값만 집는다. 값 안에 `·` 를 쓰면 항목 구분과 헷갈리므로
# 거기서 끊는다 — `verify_format.py` 의 parse_meta 와 같은 규칙이다.
ACCESS_RE = re.compile(r"(\*\*열람\*\*:\s*)([^·\n]*)")
# 그 줄에 항목이 몇 개나 있는지 (혼자 있는 줄만 통째로 지울 수 있다)
META_KEY_RE = re.compile(r"\*\*([^*]+)\*\*:")


def doc_path(doc: str):
    """`.doc-state.json` 의 `doc` 값을 실제 경로로. 없으면 None.

    값에 `projects/` 접두가 붙은 것과 안 붙은 것이 섞여 있었다(손으로 적는 자리라 그렇다).
    **2026-08-27 에 41건을 짧은 형태로 통일했지만** 둘 다 계속 받아준다 — 다시 섞일 수
    있고, 여기서 못 찾으면 승인을 조용히 놓친다.
    """
    if not doc:
        return None
    rel = str(doc).replace("\\", "/")
    if rel.startswith("projects/"):
        rel = rel[len("projects/"):]
    p = DOCS_DIR / "projects" / rel
    return p if p.exists() else None


def user_name(token, user_id, cache):
    """승인한 사람의 표시 이름. 못 찾으면 ID 그대로 (메타에 빈칸을 남기지 않는다)."""
    if not user_id:
        return "이름 미상"
    if user_id in cache:
        return cache[user_id]
    d = api(token, "users.info", user=user_id)
    p = (d.get("user") or {}).get("profile") or {} if d.get("ok") else {}
    name = (p.get("display_name") or p.get("real_name") or "").strip() or user_id
    cache[user_id] = name
    return name


def rewrite_meta(text: str, approval_line: str):
    """메타 두 줄을 고친 본문을 돌려준다. 못 고치면 (None, 사유).

    메타 블록은 파일 첫머리의 연속된 `>` 줄이다 (`archive.js` 의 metaBlock 과 같은 정의).
    거기 밖의 `열람` 이라는 낱말은 건드리지 않는다 — 본문에 그 말이 나오는 문서가 있다.
    """
    lines = text.split("\n")

    start = end = None
    for i, line in enumerate(lines):
        if line.startswith(">"):
            if start is None:
                start = i
            end = i
        elif start is not None:
            break
    if start is None:
        return None, "메타 블록('>' 줄)이 없습니다"

    out, done, reason_dropped = [], False, False
    for i, line in enumerate(lines):
        if start <= i <= end:
            keys = [k.strip() for k in META_KEY_RE.findall(line)]
            # 열람·공개승인 이 다른 항목과 한 줄에 있으면 **손대지 않는다.**
            # 고쳐 봐야 `verify_format.py` 의 4-0 규칙에 걸리는 md 가 나오고, 그것이
            # VM 자동 반영의 관문(runGate)에 걸리면 그날 대화 반영까지 통째로 롤백된다.
            if len(keys) > 1 and ({"열람", "공개승인"} & set(keys)):
                return None, ("'열람'·'공개승인' 이 다른 항목과 한 줄에 있습니다 — "
                              "그 줄을 먼저 따로 떼세요 (verify_format 도 ✗ 를 냅니다)")
            # `**비공개 사유**` 는 이제 사실이 아니다. 혼자 있는 줄이면 통째로 지운다.
            if len(keys) == 1 and keys[0] == "비공개 사유":
                reason_dropped = True
                continue
            # 옛 `**공개승인**` 줄은 지운다 — 새 줄을 그냥 더하면 같은 키가 두 줄이 되고,
            # 파서는 **나중 줄**을 쓰므로 방금 확인한 승인이 옛 기록에 덮인다.
            if len(keys) == 1 and keys[0] == "공개승인":
                continue
            if not done and ACCESS_RE.search(line):
                line = ACCESS_RE.sub(lambda m: m.group(1) + "공개", line, count=1)
                out.append(line)
                out.append(approval_line)
                done = True
                continue
        out.append(line)

    if not done:
        return None, "메타에 '**열람**' 줄이 없습니다"
    if not reason_dropped:
        # 지울 줄이 없었다는 뜻. 다른 항목과 한 줄에 섞여 있으면 손으로 봐야 한다.
        for line in lines[start:end + 1]:
            if "비공개 사유" in line:
                return None, "'비공개 사유' 가 다른 항목과 한 줄에 있습니다 — 손으로 고치세요"
    return "\n".join(out), None


def read_md(path: Path):
    """(본문, 그 파일의 줄바꿈). **바이트로 읽는다.**

    `read_text` + `write_text` 조합은 윈도우에서 LF 파일을 통째로 CRLF 로 바꿔 놓는다 —
    메타 두 줄만 고쳤는데 파일 전체가 diff 로 잡히고, 줄끝 앵커가 있는 정규식이
    **에러 없이** 안 맞게 된다. `insert_entry.py` 의 read_lines 와 같은 이유다.
    """
    raw = path.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), nl


def write_md(path: Path, text: str, nl: str):
    """tmp 에 쓰고 바꿔 끼운다 — 중간에 죽어도 반쪽짜리 md 가 남지 않게.

    여기서 고치는 것은 `열람`·`공개승인` 두 줄, 즉 **그 문서를 팀 전원에게 열지 말지**다.
    반쯤 쓰이면 열람 상태가 어중간해지고, 잘린 md 는 에러 없이 검색에서만 빠져
    봇이 "그런 자료가 없다"고 단정한다.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.replace("\n", nl).encode("utf-8"))
    tmp.replace(path)


def _keys(r):
    """그 항목을 **정확히** 가리키는 이름들 — 파일명, `채널/파일명`, md 경로."""
    ks = [str(r.get("file", "")), f"{r.get('channel', '')}/{r.get('file', '')}"]
    md = r.get("md")
    if md is not None:
        try:
            ks.append(md.relative_to(DOCS_DIR).as_posix())
        except ValueError:
            ks.append(str(md))
    return [k.lower() for k in ks if k]


def _split_only(raw):
    """`--only` 값 하나를 토큰으로 쪼갠다. **번호일 때만 쉼표로 쪼갠다.**

    쉼표를 늘 구분자로 보면 이름에 쉼표가 든 문서(`보고서(1,2월).pdf`)를 이름으로
    영영 못 고른다 — 조각 둘로 갈려 각각 아무것도 못 맞추고 「맞는 항목이 없습니다」만
    나온다. 번호로 고르는 길은 남기되(`--only 2,4`), 그 형태일 때만 쪼갠다.
    """
    s = str(raw).strip()
    if re.fullmatch(r"\d+(\s*,\s*\d+)*", s):
        return [t.strip() for t in s.split(",") if t.strip()]
    return [s] if s else []


def select(items, tokens):
    """`--only` 로 고른 것만. (고른 목록, 아무것도 못 맞춘 토큰) 을 돌려준다.

    **이름으로 고르는 것을 권한다.** 번호는 화면에 찍힌 순서(1부터)인데, 이 목록은 실행할
    때마다 슬랙에서 새로 만들어 다시 정렬한다 — 목록을 본 뒤 명령을 치는 사이에 승인이
    하나 늘거나 반영되면 **같은 번호가 다른 문서를 가리킨다.** 그 문서가 비공개면 그 순간
    팀 전원의 답변 근거가 된다 (2026-08-10). 이름은 그 사이에 안 변한다.

    이름은 파일명 조각, `채널/파일명`, md 경로 어느 것으로도 된다 — 같은 파일명이 여러
    사업장에 있을 때(`본부별-주간보고` 등) 채널을 붙여 가릴 수 있어야 하기 때문이다.

    **못 맞춘 토큰을 조용히 넘기지 않는다** — 오타 하나로 열릴 줄 알았던 문서가
    안 열리면, 팀은 승인했는데 봇은 계속 막는 상태가 그대로 이어진다.
    """
    picked, unmatched, numeric, ambiguous = [], [], [], []
    for token in [t for raw in tokens for t in _split_only(raw)]:
        if token.isdigit():
            i = int(token)
            hits = [items[i - 1]] if 1 <= i <= len(items) else []
            numeric.append(token)
        else:
            low = token.lower()
            # ① 완전일치 먼저 — `채널/파일명`·md 경로·파일명 전체를 그대로 준 경우.
            hits = [r for r in items if low in _keys(r)]
            if not hits:
                # ② 조각으로 찾는다. **여럿이 걸리면 고르지 않는다** — 짧은 조각(`주간`·`보고`)이
                #    보지도 않은 문서를 함께 여는 것이 번호 문제의 완화판이라 같이 막는다.
                hits = [r for r in items if low in str(r.get("file", "")).lower()]
                if len(hits) > 1:
                    ambiguous.append((token, hits))
                    hits = []
        if not hits and not any(t == token for t, _ in ambiguous):
            unmatched.append(token)
        for r in hits:
            if r not in picked:
                picked.append(r)
    if numeric:
        print(
            f"! 번호({', '.join(numeric)})로 골랐습니다 — 이 번호는 **이번 실행의 목록 순서**입니다.\n"
            "  아래 「고른 것」이 열려고 한 문서가 맞는지 확인하세요. 다음부터는 이름으로 고르면 안전합니다.",
            file=sys.stderr,
        )
    return picked, unmatched, ambiguous


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=str(DEFAULT_ENV))
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="privateChannels·skipChannels 를 읽을 Hermes config.json")
    ap.add_argument("--channel", action="append", help="채널명. 여러 번 줄 수 있다. 생략하면 비공개 채널 전부")
    ap.add_argument("--only", action="append", metavar="번호|파일명조각",
                    help="이것만 연다. 목록에 찍힌 번호(`--only 2,4`) 또는 파일명 조각. "
                         "여러 번 줄 수 있다. 생략하면 걸린 승인 전부")
    ap.add_argument("--days", type=int, default=60,
                    help="최근 N일 안에 올라온 파일만 본다. 0 이면 제한 없음 (기본 60 — 위생 점검과 같은 창)")
    ap.add_argument("--apply", action="store_true", help="실제로 md 를 고친다 (기본은 목록만)")
    args = ap.parse_args()

    token = read_token(Path(args.env_file))
    state_path = Path(args.state)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"ERROR: {state_path} 를 읽지 못했습니다 ({e})")
    slack_files = state.get("slack_files", {})
    # **영구 제외는 ID 와 이름 둘 다로 본다** (2026-08-13). 같은 자료를 다시 올리면 파일 ID 가
    # 새로 생기는데, 제외 판정은 그 **자료**에 대해 내린 것이다. 여기만 ID 뿐이었다 — 그래서
    # 다시 올린 등기부등본 스레드에 `[공개]` 가 달리면 「아직 변환 전」으로 보고해서
    # **영구 제외한 자료를 변환하라고 권했다.** `archive-health.js` 의 `pendingDocuments` 안
    # 승인 후보 걸러내기, `fetch_slack_files.py` 의 수집이 같은 판정을 쓴다 — 셋이 갈리면 안 된다
    # (`scripts/check-shared-rules.js` ③ 이 세 곳을 함께 본다).
    excluded = set(state.get("excluded", {}))
    excluded_names = {name_key(v.get("channel", ""), v.get("name", ""))
                      for v in (state.get("excluded") or {}).values()}
    # 만기 전 보류는 「이번엔 안 넣기로 한 것」이라 승인도 옮기지 않는다. 만기가 지나면
    # 저절로 빠져 다시 뜬다 — 판정은 fetch_slack_files.active_deferred 한 곳에만 있다.
    # **이름으로도 본다** — 같은 자료를 다시 올리면 파일 ID 가 바뀌는데, 판정은 그 자료에
    # 대해 내린 것이다. `archive-health.js` 의 `pendingDocuments` 의 `deferredNames` 와 같은 규칙이고,
    # 갈리면 위생 점검은 조용한데 여기 `--apply` 는 보류해 둔 문서를 실제로 공개로 연다.
    df = active_deferred(state)
    deferred = set(df)
    deferred_names = {name_key(v.get("channel", ""), v.get("name", "")) for v in df.values()}
    # 「최신 1건이 대신한 옛 판」도 승인을 옮기지 않는다 — 옛 판은 애초에 대상이 아니다.
    # **여기 세 갈래(excluded·deferred·superseded)는 `archive-health.js` 의 `DECIDED_OUT` 과
    # 같아야 한다.** `DOC_EXTS` 에 xlsx 가 없던 동안은 이 갈래에 닿는 엑셀 후보 자체가
    # 여섯 확장자 걸러내기에서 먼저 빠져 조용했지만, 엑셀이 `DOC_EXTS` 에 들어간 지금은
    # 실제로 걸릴 수 있다 (2026-08-28, 비공개 채널 #비공개나 의 물린 엑셀 1건).
    superseded = set(state.get("superseded", {}))
    superseded_names = {name_key(v.get("channel", ""), v.get("name", ""))
                        for v in (state.get("superseded") or {}).values()}

    by_name = {}
    for fid, v in slack_files.items():
        by_name[name_key(v.get("channel", ""), v.get("name", ""))] = (fid, v)

    private = private_channels(Path(args.config))
    skip = skip_channels(Path(args.config))

    # **비공개 채널만 본다.** 공개 채널 문서는 원래 공개고, 거기에 `공개승인` 을 적으면
    # verify_format.py 가 ✗ 를 낸다.
    chans = [c for c in bot_channels(token)
             if c["name"] in private and c["name"] not in skip]
    if args.channel:
        want = {c.lstrip("#") for c in args.channel}
        missing = want - {c["name"] for c in chans}
        chans = [c for c in chans if c["name"] in want]
        for n in sorted(missing):
            print(f"! #{n} — 봇이 안 들어갔거나 비공개 채널 목록에 없습니다")
    if not chans:
        sys.exit("ERROR: 대상 채널이 없습니다.")

    # 정수로 보낸다 — 소수가 7자리를 넘으면 슬랙이 ok:true 에 빈 목록을 준다
    # (`fetch_slack_files.py` 의 `main` 안 `oldest` 주석에 실측이 있다).
    oldest = str(int(time.time() - args.days * 86400)) if args.days else None

    print(f"대상 비공개 채널 {len(chans)}개 · {'최근 ' + str(args.days) + '일' if args.days else '기간 제한 없음'}\n")

    to_fix, already, pending, problems = [], 0, [], []
    seen = set()
    cache = {}

    for c in chans:
        for m in history_with_threads(token, c["id"], oldest):
            # 봇이 올린 첨부는 애초에 아카이브 대상이 아니다 (WHK 결정 2026-08-12).
            # 여기서 안 빼면 **수집은 넣지도 않은 파일**을 「승인 미반영」으로 올려,
            # `--only` 로 열려고 해도 대상 문서가 없어 매번 `pending` 으로만 남는다.
            # 판정은 `fetch_slack_files.is_bot_message` 한 곳에서 온다.
            if is_bot_message(m):
                continue
            for f in m.get("files", []):
                name = f.get("name") or ""
                ext = (Path(name).suffix.lstrip(".").lower() or (f.get("filetype") or "").lower())
                if ext not in DOC_EXTS:
                    continue
                approver = next(
                    (x for x in (m.get("_thread_msgs") or []) if PUBLIC_TAG.search(x.get("text") or "")),
                    None,
                )
                if approver is None:
                    continue
                fid = f.get("id")
                key = name_key(c["name"], name)
                if fid in excluded or key in excluded_names:
                    continue
                if fid in deferred or key in deferred_names:
                    continue
                if fid in superseded or key in superseded_names:
                    continue
                if key in seen:
                    continue

                rec = slack_files.get(fid)
                if rec is None:
                    hit = by_name.get(key)
                    rec = hit[1] if hit else None
                if rec is None:
                    seen.add(key)
                    pending.append((c["name"], name))
                    continue

                md = doc_path(rec.get("doc"))
                if md is None:
                    seen.add(key)
                    problems.append((c["name"], name, f"상태 파일이 가리키는 `{rec.get('doc')}` 를 찾지 못했습니다"))
                    continue

                text = md.read_text(encoding="utf-8")
                head = text[:4000]
                access = (ACCESS_RE.search(head).group(2).strip() if ACCESS_RE.search(head) else "")
                if access == "공개" and "**공개승인**" in head:
                    already += 1
                    seen.add(key)
                    continue

                seen.add(key)
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(approver.get("ts", 0))))
                who = user_name(token, approver.get("user"), cache)
                to_fix.append({
                    "channel": c["name"], "file": name, "md": md,
                    "line": f"> **공개승인**: 슬랙 스레드 [공개] 댓글 · {when} · {who}",
                    "when": when, "who": who,
                })

    if already:
        print(f"이미 반영된 승인 {already}건 — 건드리지 않습니다\n")

    if pending:
        print(f"아직 변환 전인데 승인이 달린 첨부 {len(pending)}건 — 이 스크립트가 아니라 `doc-archive` 로 넣습니다:")
        for ch, n in pending:
            print(f"  #{ch} {n}")
        print("  (넣을 때 메타에 `공개승인` 을 함께 적으세요)\n")

    if problems:
        print(f"손이 필요한 것 {len(problems)}건:")
        for ch, n, why in problems:
            print(f"  #{ch} {n} — {why}")
        print()

    if not to_fix:
        print("반영할 승인이 없습니다.")
        return 0

    to_fix.sort(key=lambda r: (r["channel"], r["file"]))

    print(f"반영할 승인 {len(to_fix)}건:")
    for i, r in enumerate(to_fix, 1):
        rel = r["md"].relative_to(DOCS_DIR).as_posix()
        print(f"  {i}. #{r['channel']} {r['file']}")
        print(f"     → {rel}")
        print(f"     → 열람: 공개 · 승인 {r['when']} · {r['who']}")
        print(f"     → 고를 때: --only \"{r['channel']}/{r['file']}\"")
    print("  번호로도 되지만 번호는 실행할 때마다 다시 매겨집니다 — 이름으로 고르세요.")

    # **건별로 고른다.** `--apply` 는 걸린 승인을 전부 열어버리는데, 그 순간 그 문서가
    # 팀 전원의 답변 근거가 된다. 하나만 열고 싶은 날이 대부분이라 번호로 고를 수 있게 뒀다.
    if args.only:
        picked, unmatched, ambiguous = select(to_fix, args.only)
        # 루프 변수를 `token` 으로 두면 194 줄의 슬랙 봇 토큰을 덮어쓴다. 지금은 아래에서
        # 토큰을 안 써서 안 깨지지만, 한 줄만 붙어도 인증 자리에 `--only` 문자열이 간다.
        for sel in unmatched:
            print(f"! --only '{sel}' 에 맞는 항목이 없습니다")
        if ambiguous:
            # 짐작으로 열지 않는다 — 여는 것은 되돌릴 수 없다(슬랙에 승인 취소 태그가 없다).
            for sel, hits in ambiguous:
                print(f"\n✗ --only '{sel}' 에 {len(hits)}건이 걸립니다 — 어느 것인지 정할 수 없어 멈춥니다:")
                for r in hits:
                    print(f"    --only \"{r['channel']}/{r['file']}\"")
            print("\n위 이름 중 하나를 그대로 주세요. 아무것도 열지 않았습니다.")
            return 1
        if not picked:
            print("\n--only 로 고른 항목이 없습니다. 아무것도 하지 않습니다.")
            return 1
        skipped_n = len(to_fix) - len(picked)
        to_fix = picked
        print(f"\n--only 로 {len(to_fix)}건만 고릅니다 (나머지 {skipped_n}건은 그대로 둡니다):")
        for r in to_fix:
            print(f"  #{r['channel']} {r['file']}")

    if not args.apply:
        print("\n(목록만 보였습니다. 실제로 고치려면 --apply)")
        return 0

    changed, failed = [], []
    for r in to_fix:
        text, nl = read_md(r["md"])
        new, why = rewrite_meta(text, r["line"])
        if new is None:
            failed.append((r, why))
            continue
        write_md(r["md"], new, nl)
        changed.append(r)

    print(f"\n{len(changed)}건을 고쳤습니다.")
    for r, why in failed:
        print(f"! {r['md'].name} — {why}")

    if changed:
        print("\n다음 두 가지를 반드시 하세요:")
        print("  1) python .claude/skills/doc-archive/scripts/verify_format.py --all")
        print("  2) 커밋·push 는 사람 승인을 받는다 — push 하는 순간 이 문서가 팀 전원의 답변 근거가 됩니다")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
