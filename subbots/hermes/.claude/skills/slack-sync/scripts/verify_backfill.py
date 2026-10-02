#!/usr/bin/env python3
"""백필이 실제로 무엇을 넣었나 — **슬랙을 정답지로 삼아** 재는 검증기.

왜 따로 쓰나
------------
42채널 시험은 **지운 달을 되채워 원본과 대는** 방식이라 정답지가 원본 md 다. 그런데
백필을 만든 이유는 **원본이 한 번도 안 가졌던 과거**를 채우는 것이고, 그 경로는
대볼 정답지가 없어 **검증된 적이 없다**.

그래서 정답지를 슬랙에서 직접 가져온다. 그리고 **읽는 코드를 일부러 가른다** —
백필은 JS(`src/ingest/backfill.js`)이고 이것은 파이썬이며, 페이징도 백필의
`conversations.history` 커서 방식이 아니라 **하루 단위 창**으로 다시 훑는다.
같은 코드로 재면 같은 버그를 같이 갖는다.

무엇을 보나
-----------
① `ts` 집합이 md 헤더와 **양방향으로** 같은가 — 「md 에 없는 것」뿐 아니라
   「슬랙에 없는데 md 에 있는 것」도 본다. 원본 대조로는 구조적으로 못 보던 방향이다
② 경계 위(=백필이 안 건드려야 하는 구간) 헤더 수가 안 변했는가
③ 한 번 더 돌렸을 때 늘어난 헤더가 0인가 (멱등)

②③ 은 백필을 실제로 돌린 전후를 대야 하므로 `--snapshot` 으로 상태를 떠 두고
나중에 `--compare` 로 댄다. ① 은 지금 당장 돌릴 수 있다.

거르는 규칙을 안 베낀다
-----------------------
백필이 무엇을 거르는지(`keepMessage`)를 여기 옮겨 적으면 「읽는 코드를 갈랐다」가
무의미해진다 — 같은 판단 실수를 함께 한다. 그래서 이 검증기는 **아무것도 안 거르고
전부 센 뒤 갈래별로 분류만** 한다. 판정은 사람이 한다:

- `사람`  — 봇도 시스템도 아닌 메시지. **여기 불일치가 나면 진짜 결함이다**
- `봇`    — `bot_id` 나 `subtype: bot_message`. 아카이브가 일부러 안 담는다
- `시스템` — `channel_join` 등 `subtype` 이 있는 것
- `스레드답글` — `thread_ts != ts`. 아카이브는 답글을 부모 블록 안에 넣으므로
  헤더가 따로 안 생긴다

사용법
------
    python verify_backfill.py --channel 사업장가 --from 2026-06-01 --to 2026-09-03
    python verify_backfill.py --all --from 2026-08-01 --to 2026-09-03
    python verify_backfill.py --snapshot before.json --all
    python verify_backfill.py --compare before.json after.json
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

KST = timezone(timedelta(hours=9))
SLACK = "https://slack.com/api/"

# md 헤더: `**2026-08-31 08:14 · 홍길동**`
# 관대하게 잡는다 — 계약 검사는 이 도구의 일이 아니고, 여기서 좁게 잡으면
# 「md 에 있는데 못 봤다」가 「슬랙에만 있다」로 뒤집혀 나온다.
HEADER_RE = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*·\s*(.+?)\*\*\s*$")


def api(token, method, params, tries=5):
    """슬랙 호출. 429 는 Retry-After 만큼 기다렸다 다시 친다."""
    for attempt in range(tries):
        url = SLACK + method + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "5"))
                time.sleep(wait + 1)
                continue
            raise
        if data.get("ok"):
            return data
        if data.get("error") == "ratelimited":
            time.sleep(5)
            continue
        raise RuntimeError(f"{method}: {data.get('error')}")
    raise RuntimeError(f"{method}: 재시도 {tries}회 실패")


def user_map(token):
    out, cursor = {}, None
    while True:
        p = {"limit": 200}
        if cursor:
            p["cursor"] = cursor
        d = api(token, "users.list", p)
        for u in d.get("members", []):
            prof = u.get("profile") or {}
            out[u["id"]] = (
                prof.get("display_name") or prof.get("real_name") or u.get("name") or u["id"]
            )
        cursor = (d.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


def channels(token):
    out, cursor = [], None
    while True:
        p = {"limit": 200, "types": "public_channel,private_channel", "exclude_archived": "false"}
        if cursor:
            p["cursor"] = cursor
        d = api(token, "conversations.list", p)
        out.extend(d.get("channels", []))
        cursor = (d.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


def history_by_day(token, ch_id, start, end):
    """**하루 단위 창**으로 훑는다 — 백필의 커서 페이징과 일부러 다른 길.

    커서를 쓰면 커서 자체가 틀렸을 때 검증기도 같이 틀린다. 날짜로 창을 잘라
    각 창을 독립으로 받으면, 창이 겹치거나 빠지는 것을 개수로 알아챌 수 있다.
    """
    msgs, day = [], start
    while day < end:
        nxt = day + timedelta(days=1)
        cursor = None
        while True:
            p = {
                "channel": ch_id,
                "oldest": f"{day.timestamp():.6f}",
                "latest": f"{nxt.timestamp():.6f}",
                "inclusive": "false",
                "limit": 200,
            }
            if cursor:
                p["cursor"] = cursor
            d = api(token, "conversations.history", p)
            msgs.extend(d.get("messages", []))
            if not d.get("has_more"):
                break
            cursor = (d.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        day = nxt
    return msgs


def kind_of(m):
    if m.get("bot_id") or m.get("subtype") == "bot_message":
        return "봇"
    if m.get("subtype"):
        return "시스템"
    if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
        return "스레드답글"
    return "사람"


def slot(ts):
    """분 단위 KST 문자열. md 헤더가 분까지만 적으므로 그 해상도로 맞춘다."""
    return datetime.fromtimestamp(float(ts), KST).strftime("%Y-%m-%d %H:%M")


def read_md_headers(path, start, end):
    """md 의 헤더를 (분, 작성자) 다중집합으로. 구간 밖은 뺀다.

    **깨진 헤더를 따로 돌려준다.** 어긋내기 시험에서 `**2026-08-30 09:99 · 유령**` 을
    심었더니 이 함수가 조용히 넘겼다 — 헤더처럼 생겼는데 파싱이 안 되면 「md 에 없다」로
    읽혀, 그 자리는 양방향 대조 어디에도 안 나온다. 조용히 빠지는 것이 이 도구가
    잡으려는 바로 그 종류라 세어서 보여준다.
    """
    c, broken = Counter(), []
    if not path.exists():
        return c, broken
    for line in path.read_text(encoding="utf-8").split("\n"):
        m = HEADER_RE.match(line)
        if not m:
            # 헤더 자리처럼 생겼는데(`**` + 네자리 연도) 위 규칙에 안 맞는 줄
            if re.match(r"^\*\*\d{4}[-.]", line):
                broken.append(line.strip()[:70])
            continue
        when = f"{m.group(1)} {m.group(2)}"
        try:
            t = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except ValueError:
            broken.append(line.strip()[:70])   # 달력에 없는 날짜·시각
            continue
        if start <= t < end:
            c[(when, m.group(3).strip())] += 1
    return c, broken


def md_path(archive, name):
    p = Path(archive) / "slack-export" / "channels" / f"{name}.md"
    return p


def compare_channel(token, ch, archive, start, end, umap, verbose):
    raw = history_by_day(token, ch["id"], start, end)
    by_kind = defaultdict(Counter)
    for m in raw:
        who = umap.get(m.get("user", ""), m.get("username") or m.get("bot_id") or "?")
        by_kind[kind_of(m)][(slot(m["ts"]), who)] += 1

    md, broken = read_md_headers(md_path(archive, ch["name"]), start, end)
    people = by_kind["사람"]

    missing = people - md          # 슬랙에 있는데 md 에 없다
    extra = md - people            # md 에 있는데 슬랙에 없다  ← 원본 대조로는 못 보던 방향
    return {
        "channel": ch["name"],
        "slack_people": sum(people.values()),
        "md_headers": sum(md.values()),
        "missing": missing,
        "extra": extra,
        "bot": sum(by_kind["봇"].values()),
        "system": sum(by_kind["시스템"].values()),
        "replies": sum(by_kind["스레드답글"].values()),
        "broken": broken,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", dest="to", required=True)
    ap.add_argument("--archive", default=os.environ.get("HERMES_ARCHIVE", str(Path.home() / "hermes" / "archive")))
    ap.add_argument("--snapshot", help="지금 헤더 수를 이 파일에 떠 둔다 (②③ 용)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    # **토큰 자리를 `--archive` 에 묶지 않는다.** 처음에 `Path(a.archive).parent/"code"/".env"`
    # 로 유도했는데, `--archive` 로 임시 사본을 가리키는 순간(어긋내기 시험이 바로 그것이다)
    # 토큰을 못 찾아 죽었다. 재는 대상과 인증은 별개다.
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        for env in (Path(os.environ.get("HERMES_CODE", "")) / ".env" if os.environ.get("HERMES_CODE") else None,
                    Path.home() / "hermes" / "code" / ".env",
                    Path(a.archive).parent / "code" / ".env"):
            if env and env.exists():
                for line in env.read_text(encoding="utf-8").split("\n"):
                    if line.startswith("SLACK_BOT_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if token:
                break
    if not token:
        sys.exit("SLACK_BOT_TOKEN 이 없습니다 — 환경변수나 ~/hermes/code/.env 에 두세요")

    start = datetime.strptime(a.frm, "%Y-%m-%d").replace(tzinfo=KST)
    end = datetime.strptime(a.to, "%Y-%m-%d").replace(tzinfo=KST)

    umap = user_map(token)
    chans = channels(token)
    if a.channel:
        chans = [c for c in chans if c["name"] == a.channel]
        if not chans:
            sys.exit(f"채널 없음: {a.channel}")
    elif not a.all:
        sys.exit("--channel 또는 --all 이 필요합니다")

    print(f"구간 {a.frm} ~ {a.to} · 채널 {len(chans)}개 · 아카이브 {a.archive}")
    print()
    snap, tot_missing, tot_extra, tot_broken = {}, 0, 0, 0
    for ch in sorted(chans, key=lambda c: c["name"]):
        try:
            r = compare_channel(token, ch, a.archive, start, end, umap, a.verbose)
        except RuntimeError as e:
            print(f"  !! #{ch['name']}: {e}")
            continue
        snap[ch["name"]] = r["md_headers"]
        nm, nx = sum(r["missing"].values()), sum(r["extra"].values())
        tot_missing += nm
        tot_extra += nx
        nb = len(r["broken"])
        tot_broken += nb
        if nm or nx or nb or a.verbose:
            flag = "✗" if (nm or nx or nb) else "OK"
            print(f"{flag} #{r['channel']}  슬랙(사람) {r['slack_people']} / md헤더 {r['md_headers']}"
                  f"  · 없는 것 {nm} · 남는 것 {nx}"
                  f"  (봇 {r['bot']} · 시스템 {r['system']} · 답글 {r['replies']})")
            for b in r["broken"][:5]:
                print(f"     깨진 헤더: {b}")
            for (when, who), n in sorted(r["missing"].items())[:10]:
                print(f"     슬랙에만: {when} · {who}" + (f" ×{n}" if n > 1 else ""))
            for (when, who), n in sorted(r["extra"].items())[:10]:
                print(f"     md에만:   {when} · {who}" + (f" ×{n}" if n > 1 else ""))

    print()
    print(f"합계 — 슬랙에만 있는 것 {tot_missing} · md 에만 있는 것 {tot_extra} · 깨진 헤더 {tot_broken}")
    if a.snapshot:
        Path(a.snapshot).write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"헤더 수를 {a.snapshot} 에 떴습니다 (백필 뒤 다시 떠서 대면 ②③ 이 된다)")


if __name__ == "__main__":
    main()
