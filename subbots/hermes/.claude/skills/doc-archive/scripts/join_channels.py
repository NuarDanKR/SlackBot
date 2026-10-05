#!/usr/bin/env python3
"""
Hermes 봇을 공개 채널에 참여시킨다.

**이것은 슬랙에 흔적을 남기는 쓰기 작업이다.** 채널마다 "Hermes님이 채널에
참여함" 이 뜨고 팀원 눈에 보인다. 되돌리려면 채널마다 나가야 한다.
그래서 기본 동작은 목록만 보여주는 것이고, 실제 참여는 --confirm 이 있어야 한다.
스킬은 WHK 가 "해" 라고 한 뒤에만 --confirm 을 붙인다.

비공개 채널은 봇이 스스로 못 들어간다 — 사람이 /invite 해야 한다.

사용:
  python join_channels.py                    # 목록만 (기본)
  python join_channels.py --confirm          # 실제 참여
  python join_channels.py --confirm --include-empty   # 대화 없는 채널까지

종료코드: 0 성공 / 1 실패
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from paths import ENV_FILE  # noqa: E402

DEFAULT_ENV = ENV_FILE
SLACK_API = "https://slack.com/api/"


def read_token(env_file: Path) -> str:
    if not env_file.exists():
        sys.exit(f"ERROR: {env_file} 가 없습니다.")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("SLACK_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"ERROR: {env_file} 에 SLACK_BOT_TOKEN 이 없습니다.")


def api(token, method, post=False, **params):
    url = SLACK_API + method
    if post:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(params).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    else:
        req = urllib.request.Request(
            url + "?" + urllib.parse.urlencode(params),
            headers={"Authorization": f"Bearer {token}"},
        )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def paged(token, method, key="channels", **params):
    out, cursor = [], None
    while True:
        d = api(token, method, **params, **({"cursor": cursor} if cursor else {}))
        if not d.get("ok"):
            sys.exit(f"ERROR: {method} — {d.get('error')}")
        out += d.get(key, [])
        cursor = (d.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


def main():
    # 연동 모드에서는 원문을 쓰지 않는다. **맨 앞이어야** 한다 —
    # 인자 해석이나 Slack 호출이 먼저 돌면 막기 전에 밖으로 나간다.
    from mode import exit_if_blocked
    exit_if_blocked("채널 참여(join_channels)")
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=str(DEFAULT_ENV))
    ap.add_argument("--confirm", action="store_true", help="실제로 참여한다 (없으면 목록만)")
    ap.add_argument("--include-empty", action="store_true", help="대화 0건인 채널까지 포함")
    args = ap.parse_args()

    token = read_token(Path(args.env_file))

    allc = paged(token, "conversations.list",
                 types="public_channel,private_channel", limit=200, exclude_archived="true")
    mine = paged(token, "users.conversations",
                 types="public_channel,private_channel", limit=200, exclude_archived="true")
    joined = {c["id"] for c in mine}

    missing = [c for c in allc if c["id"] not in joined]
    private = [c for c in missing if c.get("is_private")]
    public = [c for c in missing if not c.get("is_private")]
    if not args.include_empty:
        empty = [c for c in public if not c.get("num_members")]
        public = [c for c in public if c.get("num_members")]
    else:
        empty = []

    print(f"\n전체 {len(allc)}개 · 봇 참여 중 {len(joined)}개 · 미참여 {len(missing)}개")
    if empty:
        print(f"\n건너뜀 — 멤버 0명 (--include-empty 로 포함 가능): {', '.join(c['name'] for c in empty)}")
    if private:
        print(f"\n봇이 스스로 못 들어감 (사람이 /invite @Hermes 해야 함):")
        for c in private:
            print(f"  🔒 {c['name']}")

    if not public:
        print("\n참여할 공개 채널이 없습니다.")
        return 0

    print(f"\n참여 대상 공개 채널 {len(public)}개:")
    for c in public:
        print(f"  #{c['name']} ({c.get('num_members')}명)")

    if not args.confirm:
        print("\n목록만 보여줬습니다. 실제로 참여하려면 --confirm 을 붙이세요.")
        print("참여하면 각 채널에 '참여함' 메시지가 남고 팀원에게 보입니다.")
        return 0

    print("\n참여 시작…")
    okc = failc = 0
    for c in public:
        d = api(token, "conversations.join", post=True, channel=c["id"])
        if d.get("ok"):
            okc += 1
            print(f"  ✓ #{c['name']}")
        else:
            failc += 1
            print(f"  ✗ #{c['name']} — {d.get('error')}")
            if d.get("error") == "missing_scope":
                print("\n    channels:join 스코프가 없습니다. 매니페스트 반영 후 앱을 재설치하세요.")
                break
        time.sleep(0.4)  # 슬랙 rate limit 여유

    print(f"\n참여 {okc}개 · 실패 {failc}개")
    return 1 if failc else 0


if __name__ == "__main__":
    sys.exit(main())
