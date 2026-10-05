#!/usr/bin/env python3
"""
슬랙 채널에 올라온 첨부 실물을 내려받는다.

인증은 Hermes 봇 토큰(코드 저장소 뿌리의 `.env` 안 SLACK_BOT_TOKEN)을 쓴다.
slack-sync 가 쓰는 Slack MCP 커넥터와는 다른 경로다 — MCP 는 바이너리를 base64 로만
돌려줘서 큰 파일을 못 받는다. 대화 텍스트는 slack-sync, 첨부 실물은 이 스크립트.

**`files:read` 스코프가 없으면 파일 대신 로그인 HTML 이 돌아온다.**
에러가 아니라 200 OK 로 HTML 이 오기 때문에 눈치채기 어렵다. 그래서 내려받은 뒤
첫 바이트를 확인해 HTML 이면 실패로 처리한다.

사용:
  python fetch_slack_files.py --dry-run              # 뭐가 받아질지만 본다
  python fetch_slack_files.py --limit 30
  python fetch_slack_files.py --channel 사업장나 --days 30

종료코드: 0 성공 / 1 실패 (스코프 부족 포함)
"""

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from paths import ARCHIVE, CONFIG, DOCS_DIR, ENV_FILE  # noqa: E402
import tz as _tz  # noqa: E402

DEFAULT_ENV = ENV_FILE
DEFAULT_STATE = DOCS_DIR / ".doc-state.json"
DEFAULT_OUT = Path.home() / ".doc-cache"
DEFAULT_CONFIG = CONFIG
#: 대화 쪽 상태 파일. 여기서는 **개명 지도**(`archive_channel_names`)만 읽는다.
DEFAULT_SYNC_STATE = ARCHIVE / ".sync-state.json"

SLACK_API = "https://slack.com/api/"

# 슬랙 호출 하나가 기다릴 수 있는 최대 초. **비워두면 안 된다** — urlopen 은 기본값이
# "무한정 기다림" 이라, 연결이 반쯤 끊기면 프로세스가 그 자리에 영영 서 있는다.
# 2026-08-05 09:00 자동 수집이 파일을 한 건도 못 받고 끝났고, 그 시각 무렵 Wi-Fi
# 드라이버가 반복 리셋되고 있었다 (System 로그 Netwtw14 09:08:11~09:08:52).
TIMEOUT = 30

# doc-archive 가 다루는 확장자. **같은 목록이 세 자리에 있다** —
# 여기, `apply_approvals.py` 의 `DOC_EXTS`, `src/archive-health.js` 의 `DOC_EXTS`.
# 셋이 갈리면 에러가 안 나고 숫자만 달라진다 (수집은 「0건」, 점검은 「N건 남음」).
# 갈렸는지는 `scripts/check-shared-rules.js` 의 절 ④ 가 본다.
#
# doc·pptx 는 LibreOffice 를 거쳐야 읽힌다(스킬 4단계). 여기 없으면 아예 내려받지도 않아
# 캐시에도 상태 파일에도 안 남고, 위생 점검의 미변환 집계에도 안 잡힌다 —
# 있는데 아무 데도 안 보이는 상태가 된다 (2026-08-05 에 실제로 .doc 11건이 그랬다).
# xlsx·xlsm 은 이 경로를 안 탄다 — kordoc 도 LibreOffice 도 안 거치고
# `xlsx_to_blocks.py` 가 시트 단위로 따로 변환한다(스킬 「엑셀 변환」).
DOC_EXTS = {"pdf", "hwp", "hwpx", "docx", "doc", "pptx", "xlsx", "xlsm"}

# 같은 이름이 여러 포맷으로 올라왔을 때 채택 우선순위. **순서가 곧 우선순위다.**
# `archive-health.js` 의 `PREFER` 와 같아야 한다 (같은 검사가 본다).
# 엑셀은 여기 넣지 않는다 — 「같은 이름, 다른 확장자」 규칙이라 엑셀에는 짝이 없고,
# 엑셀의 중복 제거는 축이 다른 규칙(`superseded` — 같은 정규화명의 최신 1건만)이 맡는다.
PREFER_EXTS = ["hwpx", "hwp", "docx", "pdf"]

# 파일명에 쓸 수 없는 글자 (Windows 기준)
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 여기 있던 `GLOB_UNSAFE` 우회는 **2026-08-29 에 걷어냈다.** kordoc 4.5.0 이 파일 인자를
# 글로브로 해석해 이름에 `[`·`]`·작은따옴표가 든 파일을 못 읽던 것인데, 4.10.0 에서
# 고쳐진 것을 실측으로 확인했다 — 같은 pdf 를 `평범.pdf`·`대괄호[1].pdf`·`작은따옴표'.pdf`
# 세 이름으로 변환해 한 건씩도 배치로도 셋 다 5,601자로 같았다.
# 그래서 이제 슬랙 원래 이름이 캐시에도 그대로 남는다.
#
# ⚠️ 없어지지 않은 성질 하나 — kordoc 은 실패해도 **종료코드 0** 을 준다.
# 다른 문자가 또 걸려도 에러로 안 보이므로, 변환 뒤 '출력 md 개수 == 입력 파일 개수'
# 대조는 계속 필요하다.

# 비공개 채널 문서를 공개로 여는 승인 태그 (WHK 결정 2026-08-05).
# **전용 태그만 본다.** '공개' 라는 낱말을 찾으면 "공개해도 되나요?"·"공개 불가" 같은
# 질문·부정문까지 승인으로 잡혀 #비공개가 자료가 팀 앞에 나간다.
PUBLIC_TAG = re.compile(r"\[공개\]")


def read_token(env_file: Path) -> str:
    if not env_file.exists():
        sys.exit(f"ERROR: {env_file} 가 없습니다. Hermes .env 를 먼저 만드세요.")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("SLACK_BOT_TOKEN="):
            tok = line.split("=", 1)[1].strip()
            if len(tok) < 20:
                sys.exit("ERROR: SLACK_BOT_TOKEN 이 예시값 그대로입니다.")
            return tok
    sys.exit(f"ERROR: {env_file} 에 SLACK_BOT_TOKEN 이 없습니다.")


def api(token: str, method: str, **params):
    """슬랙 Web API 호출. 429 는 Retry-After 만큼 기다렸다 다시 시도한다.

    대기 상한(TIMEOUT)에 걸리거나 연결이 끊기면 그것도 재시도 대상이다 — 끊긴 연결
    하나로 수집 전체가 멈춰 서지 않게.
    """
    url = SLACK_API + method + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "3")))
                continue
            raise
        # IncompleteRead: 응답을 읽다 연결이 끊긴 것 — 2026-09-08 에 한 번 났고 재시도로 풀렸다
        except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead) as e:
            if attempt == 4:
                sys.exit(f"ERROR: {method} 연결 실패 — {e}")
            time.sleep(3)
            continue
        if not data.get("ok") and data.get("error") == "ratelimited":
            time.sleep(3)
            continue
        return data
    sys.exit(f"ERROR: {method} 재시도 한도 초과")


def today_kst() -> date:
    """만기를 재는 「오늘」. **기계 로컬 시간대로 재지 않는다.**

    Hermes 쪽(`archive-health.js` 의 `activeDeferred`)은 `config.timezone`(Asia/Seoul)로
    일부러 맞추고 "UTC 로 재면 안 된다"는 주석까지 달아 뒀다. 이 스크립트는 사람의
    셸에서 도는데 그 기계가 UTC 면 자정 언저리에서 두 판정이 하루 어긋난다 —
    만기 당일의 보류가 한쪽에서는 살아 있고 한쪽에서는 이미 후보로 돌아온다.

    **`_shared/tz.py` 로 위임한다 (2026-09-03).** 전에는 여기서 "Asia/Seoul" 을 직접
    하드코딩했다 — config.json 의 timezone 값이 바뀌어도 이 자리는 안 따라갔다.
    이름(`today_kst`)은 그대로 둔다 — `review_work.py`·`decide.py`·`apply_edits.py` 가
    `from fetch_slack_files import today_kst` 로 이 이름을 그대로 가져다 쓴다.
    """
    return _tz.today()


def deferred_is_active(rec: dict, today=None) -> bool:
    """보류가 아직 살아 있나. 만기가 없거나 못 읽으면 **살아 있는 것으로 보지 않는다**.

    fail-open 쪽을 고른 이유: 만기를 못 읽어 영원히 거르면 그 파일은 후보에도 알림에도
    안 나타나 어디에서도 안 보인다. 후보로 돌려놓으면 사람이 다시 보고 정할 수 있다.
    """
    try:
        return date.fromisoformat(str(rec.get("until", ""))) >= (today or today_kst())
    except ValueError:
        return False


def active_deferred(state: dict, today=None) -> dict:
    """만기가 안 지난 보류만 (`.doc-state.json` 의 `deferred`).

    **거르는 곳 전부가 이 판정을 써야 한다.** `decide.py`·`apply_approvals.py` 가 이걸
    import 하고, Hermes 쪽은 `archive-health.js` 의 `activeDeferred` 가 같은 규칙을 옮겨
    적은 것이다. 갈리면 한쪽은 「N건 남음」, 한쪽은 「0건」이라고 알린다
    (2026-08-04 에 `known_names` 대조가 없어 실제로 34건 vs 1건이 났다).
    """
    return {k: v for k, v in (state.get("deferred") or {}).items() if deferred_is_active(v, today)}


def private_channels(config_path: Path) -> set:
    """`config.json` 의 privateChannels — 코드가 비공개로 취급하는 채널."""
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        # `#` 은 **한 글자만** 뗀다 — 비교 함수(`canon_channel`)와 JS `normalizeChannel`
        # 이 그 규칙이다. `lstrip("#")` 로 전부 떼면 `##` 이름에서 두 언어가 조용히 갈린다.
        return {re.sub(r"^#", "", str(c)).strip() for c in cfg.get("privateChannels", [])}
    except Exception as e:
        sys.exit(f"ERROR: {config_path} 의 privateChannels 를 읽지 못했습니다 ({e})")


def report_rejected_private(rejected, config_path: Path):
    """선언이 없어 **내려받지 않은** 비공개 채널을 알린다.

    코드는 슬랙의 실제 비공개 여부가 아니라 **손으로 적은 목록**을 본다. 둘이 어긋나면
    비공개 채널 문서가 공개로 아카이브되어 봇이 전사 앞에서 인용한다.
    2026-08-05 에 실제로 그랬다 — `#비공개나` 이 목록에 없어 문서 5건이 공개로 들어갔다.
    그때 막은 것은 대화 수집뿐이었고, 여기는 **경고만 하고 계속 내려받았다**
    (2026-09-16 에 닫았다).

    안내는 대화 수집(`ingest/slack-archive.js:99-101`)과 **같은 세 마디**로 한다 —
    내려받지 않았다 · 비공개로 다룰 것이면 privateChannels 에 넣어라 ·
    아예 안 다룰 것이면 digest.skipChannels 에 넣어라.
    """
    if not rejected:
        return
    print("\n" + "!" * 60)
    print("슬랙에서 비공개인데 config.json 의 privateChannels 에 없는 채널 —"
          " 내려받지 않았습니다")
    for n in rejected:
        print(f"  #{n}")
    print("넣으면 봇이 공개 채널 답변에 인용하게 됩니다.")
    print(f"비공개로 다룰 것이면 {config_path} 의 privateChannels 에 이름을 넣고,")
    print("아예 다루지 않을 것이면 digest.skipChannels 에 넣으세요.")
    print("!" * 60)


def skip_channels(config_path: Path) -> set:
    """`config.json` 의 digest.skipChannels — 봇이 아예 다루지 않는 채널.

    Hermes 쪽(요약·자동 반영·미변환 집계)과 **같은 목록을 봐야 한다.** 여기만 안 보면
    "제외했다"고 적어둔 채널의 첨부가 조용히 디스크로 내려받아진다. 비공개 채널을
    제외했을 때 특히 문제가 된다.
    """
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        # `#` 은 한 글자만 뗀다 — `private_channels` 와 같은 이유.
        return {re.sub(r"^#", "", str(c)).strip() for c in cfg.get("digest", {}).get("skipChannels", [])}
    except Exception as e:
        # 설정을 못 읽으면 "제외 없음" 으로 넘어가지 않는다 — 조용히 새는 쪽이라 멈춘다.
        sys.exit(f"ERROR: {config_path} 의 skipChannels 를 읽지 못했습니다 ({e})")


def bot_channels(token):
    out, cursor = [], None
    while True:
        d = api(token, "users.conversations",
                types="public_channel,private_channel", limit=200,
                exclude_archived="true", **({"cursor": cursor} if cursor else {}))
        if not d.get("ok"):
            sys.exit(f"ERROR: users.conversations — {d.get('error')}")
        out += [{"id": c["id"], "name": c["name"], "is_private": bool(c.get("is_private"))}
                for c in d.get("channels", [])]
        cursor = (d.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


def is_bot_message(m):
    """봇이 올린 메시지인가 — Hermes 든 다른 봇이든 첨부를 수집에서 뺀다.

    WHK 결정 2026-08-12. 봇이 올리는 것은 아카이브를 읽어 만든 2차 가공물이거나
    알림이라, 그대로 걷어 들이면 **봇이 자기 산출물을 원본 근거로 삼아 답한다.**
    대화 쪽에는 같은 제외가 이미 있다 (`src/slack-live.js` 의 `isBotMessage`).

    봇 계정으로 올린 글에는 `bot_id` 가 예외 없이 붙으므로 그것 하나면 된다 —
    봇 자신의 user id 를 `auth.test` 로 물어볼 필요가 없어졌다. 물어보던 판이
    2026-08-11 부터 보류 상태로 대기 중이었는데, 그때 남은 걱정이 「호출이 실패하면
    폴백이 **모든** 봇의 첨부를 뺀다」였다. 이제 그게 정책이라 걱정 자체가 사라졌다.

    `app_id` 는 **앱이 사람 이름으로 올린 글**을 잡는다 (WHK 결정 2026-08-26).
    사람이 앱에게 시켜 보내면 슬랙은 그것을 그 사람의 메시지로 표시해서 `bot_id` 도
    `bot_message` 도 없고 `app_id` 만 붙는다 — 첨부가 딸려 있으면 AI 가 만든 자료가
    사람이 올린 원본으로 문서 아카이브에 들어간다. JS 쪽(`isBotMessage`)에 같은 조건이
    있고, `check-shared-rules.js` 가 두 언어를 맞대 본다.
    """
    return (bool(m.get("bot_id")) or m.get("subtype") == "bot_message"
            or bool(m.get("app_id")))


def history_with_threads(token, channel_id, oldest=None):
    """채널 본문 + 스레드 답글의 메시지를 모두 돌려준다.

    각 메시지에 그 메시지가 **속한 스레드의 답글 텍스트 전부**를 `_thread_texts` 로 붙인다.
    비공개 채널 문서를 공개로 여는 `[공개]` 승인 댓글을 이걸로 찾는다
    (WHK 결정 2026-08-05). 파일이 본문에 붙었든 답글로 올라왔든 같은 스레드를 보게 하려고
    답글 쪽에도 같은 목록을 붙인다.

    답글 **객체**도 `_thread_msgs` 로 함께 붙인다. `apply_approvals.py` 가 누가 언제
    승인했는지를 메타에 적으려면 텍스트만으로는 부족해서다 — 같은 스레드를 두 번 조회하지
    않으려고 여기서 한 번에 준다.
    """
    msgs, cursor = [], None
    while True:
        p = {"channel": channel_id, "limit": 200}
        if oldest:
            p["oldest"] = oldest
        if cursor:
            p["cursor"] = cursor
        d = api(token, "conversations.history", **p)
        if not d.get("ok"):
            print(f"  ! history 실패 ({d.get('error')}) — 건너뜀")
            return msgs
        batch = d.get("messages", [])
        # 답글에 자료 파일만 붙는 스레드가 흔하다. 본문만 보면 통째로 놓친다.
        for m in batch:
            replies = []
            if m.get("thread_ts") and m.get("reply_count"):
                r = api(token, "conversations.replies",
                        channel=channel_id, ts=m["thread_ts"], limit=200)
                if r.get("ok"):
                    replies = [x for x in r.get("messages", []) if x.get("ts") != m["thread_ts"]]
            texts = [x.get("text") or "" for x in replies]
            m["_thread_texts"] = texts
            m["_thread_msgs"] = replies
            msgs.append(m)
            for x in replies:
                x["_thread_texts"] = texts
                x["_thread_msgs"] = replies
                msgs.append(x)
        cursor = (d.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return msgs


def safe_name(name: str) -> str:
    return UNSAFE.sub("_", name).strip() or "unnamed"


def stem(name: str) -> str:
    """확장자를 뺀 이름. **`pathlib` 을 쓰지 않는다** (2026-08-15).

    슬랙이 주는 `name` 은 경로가 아니라 파일 이름인데 `Path(name).stem` 은 그것을
    경로로 읽는다. 그래서 `dir/file.pdf` 가 `file` 이 되고, 더 나쁘게는 **답이
    기계마다 달라진다** — `C:file.pdf` 가 이 PC(WindowsPath)에서는 `file`,
    VM(PosixPath)에서는 `C:file` 이다. 수집은 이 PC 에서 돌고 위생 점검은 VM 에서
    도는데, 두 키가 갈리면 한쪽은 '34건 남음' 한쪽은 '0건' 이라고 말한다.

    `archive-health.js` 의 `stem` 과 같은 규칙이다 — 마지막 점 앞까지, 소문자로.
    점이 맨 앞이면(`.hidden`) 확장자로 보지 않는다.
    """
    dot = name.rfind(".")
    return (name[:dot] if dot > 0 else name).lower()


def name_key(channel: str, name: str) -> str:
    """확장자를 뺀 (채널, 이름). 같은 자료의 다른 포맷 판을 알아보는 키.

    Hermes 위생 점검의 `archive-health.js` 의 `nameKey` 와 같은 판정이어야 한다.
    갈리면 한쪽은 '34건 남음', 한쪽은 '0건' 이라고 알린다.
    **주석으로 지키지 않는다** — `check-shared-rules.js` ①-c 가 두 구현을 대조한다.
    """
    return f"{channel}|{stem(name)}"


def archive_channel_names(sync_state: dict) -> dict:
    """슬랙의 **현재** 채널 이름 → 아카이브가 그 채널에 쓰는 이름.

    채널을 개명해도 **대화 md 파일명은 안 바꾼다**(`slack-archive.js` 의 `fileName`).
    그래서 아카이브가 아는 이름과 슬랙의 현재 이름이 갈리고, 그 화석이
    `.sync-state.json` 의 `channels[<id>].file` 이다. 이름이 안 바뀐 채널은 둘이 같다.

    **이 지도를 안 보면 개명 순간 두 가지가 조용히 어긋난다** (2026-09-16 실제로 겪었다):

      ① **사업장이 두 벌로 갈린다.** 문서 쪽은 사업장을 「그 채널」로 정하는데, 그 값이
         새 이름이면 `documents/projects/<새이름>/` 이라는 빈 폴더가 새로 생긴다.
         같은 사업장 자료가 둘로 나뉘고 봇 색인에도 두 줄이 선다.
      ② **제외·보류·물림 판정이 리셋된다.** 그 판정들은 파일 ID 와 `name_key`(채널+이름)
         **양쪽**으로 거는데, 저장된 기록의 채널은 개명 **전** 이름이라 새 이름으로 만든
         키와 안 맞는다. 같은 자료를 다시 올리면 ID 도 새로 생기므로 두 갈래가 다 빗나가
         「안 넣기로 한 것」이 후보로 돌아온다. **에러는 안 난다.**

    `archive.js` 의 `archiveChannelNames` 와 같은 판정이어야 한다 —
    갈렸는지는 `check-shared-rules.js` 가 본다.
    """
    out = {}
    for v in (sync_state.get("channels") or {}).values():
        if not isinstance(v, dict):
            continue
        now = str(v.get("name") or "").lstrip("#").strip()
        if not now:
            continue
        # `file` 이 없는 옛 기록은 개명 전이라 이름이 곧 파일명이다.
        out[now] = str(v.get("file") or now).lstrip("#").strip() or now
    return out


def archive_channel(name: str, amap: dict) -> str:
    """개명을 되짚은 사업장 이름. 지도에 없으면 그대로 — 개명 안 한 채널이 그쪽이다."""
    return amap.get(name, name)


def current_channel_names(sync_state: dict) -> dict:
    """**아카이브 이름 → 슬랙의 현재 이름.** `archive_channel_names` 의 반대 방향이다.

    `config.js` 의 `currentChannelNames` 와 **같은 판정이어야 한다** — 갈렸는지는
    `check-shared-rules.js` 가 본다. 개명 안 한 채널은 넣지 않는다: 지도가 항등이면
    줄이 없는 것과 같다.

    **`aka` 의 사이 이름도 철자로 넣는다** (2026-09-16). `file` 은 맨 처음 이름 하나만
    들어서, x → y → z 로 두 번 개명하면 가운데 y 가 어디에도 안 남았다. 수집이 개명을
    감지할 때 직전 이름을 `channels[<id>].aka` 에 사슬로 남기므로(`slack-archive.js` 의
    `renameAka`), 그 철자들도 전부 현재 이름으로 모아야 y 시절 철자로 적은 config 줄이
    산다.
    """
    out = {}
    for v in (sync_state.get("channels") or {}).values():
        if not isinstance(v, dict):
            continue
        now = re.sub(r"^#", "", str(v.get("name") or "")).strip()
        if not now:
            continue
        spellings = [v.get("file")] + list(v.get("aka") or [])
        for s in spellings:
            then = re.sub(r"^#", "", str(s or "")).strip()
            if then and then != now:
                out[then] = now
    return out


def canon_channel(name: str, cmap: dict) -> str:
    """개명을 되짚어 **현재 이름**으로 모은 철자. `config.js` 의 `canonWith` 와 같다."""
    n = re.sub(r"^#", "", str(name or "")).strip()
    return cmap.get(n, n)


def is_declared_private(name: str, private: set, cmap: dict) -> bool:
    """`config.json` 의 `privateChannels` 에 **어느 철자로 적혀 있어도** 맞힌다.

    `config.js` 의 `isPrivateWith` 와 같은 판정이어야 한다 — 대보는 이름도, config 에
    적힌 이름도 **양쪽을 다 되짚어** 댄다. 한쪽만 되짚으면 config 에 옛 이름을 적어 둔
    비공개 채널이 개명되는 순간 「선언이 없다」로 잡혀, 그 채널 문서가 조용히 안 쌓인다.
    """
    target = canon_channel(name, cmap)
    return any(canon_channel(c, cmap) == target for c in private)


def is_listed_skip(name: str, skip: set, cmap: dict) -> bool:
    """`digest.skipChannels` 에 **어느 철자로 적혀 있어도** 맞힌다.

    `is_declared_private` 와 같은 「양쪽 철자 canon 대조」다. skip 만 글자 그대로 대면,
    개명 순간 skip 줄이 죽어 안 다루기로 한 **공개** 채널이 조용히 다시 내려받아진다 —
    2026-08-10 에 대화 수집 쪽에서 21일간 벌어진 것과 같은 모양이다. JS 쪽 쓰기 세 자리
    (`fetchWindow`·`ingestConversations`·`backfillTargets`)도 2026-09-16 부터 같이 되짚는다.
    """
    target = canon_channel(name, cmap)
    return any(canon_channel(c, cmap) == target for c in skip)


def select_channels(chans, private: set, skip: set, cmap: dict):
    """수집이 실제로 돌 채널과, 선언이 없어 **거부한** 채널을 갈라 돌려준다.

    **순서가 판정의 일부다.** `skipChannels` 를 **먼저** 걷어낸다 — 아예 안 다루기로 한
    채널은 알릴 대상이 아니다. 거부 안내문이 "아예 다루지 않을 것이면 digest.skipChannels
    에 넣으세요"라고 말하는데, 넣은 뒤에도 매일 경고가 뜨면 그 안내와 모순이고 진짜
    경고가 그 속에 묻힌다.

    거부 판정은 `ingest/slack-archive.js:97-104` 와 같은 자리다 — 슬랙에선 비공개인데
    `privateChannels` 에 없는 채널은 **내려받지 않는다**(fail closed). 내려받으면 그
    채널 이름의 사업장 폴더가 생기고, 읽기 계층은 폴더 이름으로 비공개를 판정하므로
    (`documents/access.js:10-18`) 공개 문서로 취급한다.

    **`skip` 도 개명을 되짚는다** (2026-09-16). 전에는 글자 그대로 대서 「여기만 고치면
    넷이 갈린다」고 미뤄 뒀는데, 읽기 쪽(`withoutSkipped`)에 이어 쓰기 넷(여기와
    `fetchWindow`·`ingestConversations`·`backfillTargets`)을 함께 고쳤다. 판정은
    `is_listed_skip` — `is_declared_private` 와 같은 양쪽 canon 대조다.
    """
    kept, rejected = [], []
    for c in chans:
        name = re.sub(r"^#", "", str(c.get("name") or "")).strip()
        # 아래 `is_declared_private` 와 같은 이유로 원본 이름을 그대로 댄다 —
        # 판정 함수가 자기 안에서 한 번만 `#` 을 뗀다.
        if is_listed_skip(c.get("name"), skip, cmap):
            continue
        # `is_declared_private` 는 원본 이름을 받아 **자기 안에서 한 번만** `#` 을
        # 뗀다(`canon_channel`). 이미 한 번 뗀 `name` 을 또 넣으면 `##foo` 같은 이름은
        # 두 번 떼여 `foo` 가 되어 버려, 위 `#` 정규화를 한 글자만 떼도록 고친 뜻이
        # 이 갈래에서만 무효가 된다 — 그래서 여기는 원본 `c.get("name")` 을 그대로 댄다.
        if c.get("is_private") and not is_declared_private(c.get("name"), private, cmap):
            rejected.append(name)
            continue
        kept.append(c)
    return kept, sorted(rejected)


def load_filters(state: dict, refetch=frozenset()) -> dict:
    """이미 읽어들인 상태 파일 → 후보를 거를 때 쓰는 집합 여덟.

    `refetch` 에 든 파일 ID 는 **`known` 과 `known_names` 에서 뺀다** — 이미 변환해
    아카이브에 들어간 문서를 규칙이 바뀌어 다시 돌려야 할 때 쓰는 정규 경로다
    (`--refetch`). 두 집합 다에서 빼야 한다: ID 만 빼면 이름이 남아 `other_format`
    으로 다시 막힌다. 다른 갈래(`excluded`·`deferred`·`superseded`)는 **안 건드린다**
    — 그것들은 「안 넣기로 정한 것」이라 규칙 개정과 축이 다르고, 되돌리는 길이
    따로 있다(`decide.py --clear`).

    이름을 뺄 때는 **그 ID 의 항목만** 제외한다. 같은 이름의 다른 기록이 있으면
    그쪽은 그대로 막힌 채로 남아야 한다 — 지목하지 않은 문서까지 열리면 안 된다.

    **다섯 갈래를 ID 와 이름 둘 다로 본다.** 같은 자료를 다른 포맷으로 올리거나
    다시 올리면 파일 ID 가 새로 생기는데, 판정은 그 **자료**에 대해 내린 것이라
    따라가야 한다. 2026-08-12 까지 `excluded` 만 ID 로 봤고, 그래서 영구 제외한
    자료를 다시 올리면 세 숫자가 갈렸다 — 일일 요약 하단은 이름으로 봐서 「0건」,
    위생 점검·상황판은 새 ID 라 「1건」, 수집은 후보로 다시 내려받아 **제외 결정이
    무효화**됐다.
    세 곳(여기 · `archive-health.js` 의 `pendingDocuments`·`unconvertedAmong`)이
    같아야 한다.

    `deferred` 는 `active_deferred()` 를 거쳐 **만기 전 것만** 담는다. 만기가 지난
    것은 후보로 돌아와야 사람이 다시 본다 — 판정은 `decide.py` 한 곳에만 둔다.

    `known_names`(변환 끝난 자료의 이름)는 **다른 포맷 판**을 알아보는 데 쓴다.
    같은 자료를 hwpx 와 pdf 로 함께 올리면 원본 포맷 하나만 변환하고 나머지 판은
    상태 파일에 안 남는다. 그래서 ID 만 보면 그 버려진 판이 영영 새것으로 잡힌다
    (2026-08-04: 이 대조를 넣기 전 '새 첨부 34건' 중 진짜 새것은 1건이었다).

    dict 가 아닌 값은 이름 집합에서 건너뛴다 — 상태 파일이 망가졌을 때 여기서
    죽는 대신 그 항목만 이름 경로를 못 타게 한다(ID 경로는 그대로 산다).
    """
    def names(d):
        return {name_key(v.get("channel", ""), v.get("name", ""))
                for v in d.values() if isinstance(v, dict)}

    slack_files = state.get("slack_files") or {}
    ex = state.get("excluded") or {}
    df = active_deferred(state)
    sup = state.get("superseded") or {}
    refetch = set(refetch or ())
    kept = {k: v for k, v in slack_files.items() if k not in refetch}
    return {
        "known": set(kept),
        "known_names": names(kept),
        "excluded": set(ex),
        "excluded_names": names(ex),
        "deferred": set(df),
        "deferred_names": names(df),
        "superseded": set(sup),
        "superseded_names": names(sup),
    }


def classify(rec: dict, filters: dict) -> str | None:
    """후보 하나 → 제외 사유. `None` 이면 후보로 남는다.

    돌려주는 값이 곧 화면에 세는 이름이다:
    `known`(조용히 거른다) · `excluded` · `deferred` · `superseded` · `other_format`.

    **순서를 바꾸지 말 것.** 같은 파일이 두 칸에 들어 있으면 먼저 보는 쪽으로 세어져
    화면 숫자가 갈린다. 시험 [9/9] 가 이 순서를 지킨다.

    **갈래를 지우면 두 자리가 빨개진다** — 손으로 도는 `test_fetch_filters.py` 와
    `npm run check` 에 묶여 자동으로 도는 `check-shared-rules.js` ③-b 다. 후자는
    2026-08-26 까지 `excluded` 한 갈래만 먹여서, 나머지 넷은 분기를 통째로 지워도
    자동 관문이 초록이었다. 갈래를 더하거나 이름을 바꾸면 **양쪽 다** 고친다.
    """
    if rec["id"] in filters["known"]:
        return "known"
    key = name_key(rec["channel"], rec["name"])
    if rec["id"] in filters["excluded"] or key in filters["excluded_names"]:
        return "excluded"
    if rec["id"] in filters["deferred"] or key in filters["deferred_names"]:
        return "deferred"
    # 「최신 1건이 대신한 옛 판」. 안 거르면 `prune_cache` 가 지운 원본을 다음 수집이
    # 다시 받아와 「매일 받아서 매일 지우는」 루프가 **에러 없이** 돈다.
    # `prune_cache.py` 14~17행의 「부분집합」 조건이 이 세 줄에 걸려 있다.
    if rec["id"] in filters["superseded"] or key in filters["superseded_names"]:
        return "superseded"
    if key in filters["known_names"]:
        return "other_format"
    return None


def plan_fetch(found, limit, is_cached):
    """이번 회차에 무엇을 받고 무엇을 남길지 정한다.

    **`limit` 은 「실제로 내려받을 건수」다 — 캐시에 이미 있는 것은 한도를 안 먹는다.**

    전에는 상한을 자른 **뒤에** 캐시를 확인해서(`found[:limit]` → `dest.exists()`)
    이미 받아둔 파일도 한도를 먹었다. 목록은 최신순이라 앞쪽 `limit` 건이 전부 캐시에
    있으면 **그 회차는 한 건도 안 받고 끝나고 다음 회차도 똑같다** — 뒤에 있는 새 파일이
    영영 안 받아진다. **에러는 안 난다**: 종료코드 0 이고 「받음 N」도 정상으로 보였다.
    옛 수집 래퍼(2026-09-02 삭제)가 `--limit 100` 으로 우회하던 것이 이것 때문이다.

    캐시에 있는 것을 **한도와 무관하게 전부** 돌려주는 이유: 그것은 이미 디스크에 있고
    아직 변환이 안 된 것이라 매니페스트에 실려야 한다. 여기서 빼면 받아만 놓고 영영
    변환 안 되는 파일이 생긴다.

    `is_cached` 를 함수로 받는 것은 시험이 임시 폴더 없이 재게 하려는 것이다
    (`test_fetch_limit.py`).

    반환: (받을 것, 이미 있는 것, 한도 때문에 남은 것) — 셋 다 `found` 의 순서를 지킨다.
    """
    to_fetch, in_cache, remaining = [], [], []
    for r in found:
        if is_cached(r):
            in_cache.append(r)
        elif len(to_fetch) < limit:
            to_fetch.append(r)
        else:
            remaining.append(r)
    return to_fetch, in_cache, remaining


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """tmp 에 쓰고 바꿔 끼운다 — 중간에 죽어도 반쪽짜리 파일이 남지 않게.

    `insert_entry.py` 의 `write_lines()` 와 같은 방식이다. tmp 는 대상과 **같은 폴더**에
    둔다 — 다른 드라이브로 옮기면 rename 이 원자적이지 않아진다.

    여기서 잘리면 조용히 틀린다: 잘린 pdf 는 변환에서 시끄럽게 실패하지만, 잘린
    매니페스트는 **정상 JSON 처럼 보이는 채로 건수만 적게** 나올 수 있다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def atomic_write_text(path: Path, text: str) -> None:
    """utf-8 로 원자적으로 쓴다 — `atomic_write_bytes` 의 텍스트판."""
    atomic_write_bytes(path, text.encode("utf-8"))


def download(token, url, dest: Path) -> str | None:
    """돌려주는 값: None 이면 성공, 문자열이면 실패 사유."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read()
    except Exception as e:
        return f"다운로드 실패: {e}"
    head = data[:15].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return "로그인 HTML 이 돌아왔습니다 — 봇에 files:read 스코프가 없습니다"
    if len(data) < 100:
        return f"내용이 너무 짧습니다 ({len(data)} 바이트)"
    atomic_write_bytes(dest, data)
    return None


def autoprune(state_path, out_dir, *, dry_run):
    """수집을 시작하기 전에 **변환이 끝난 원본을 스스로 지운다.** (지운 건수, MB)

    왜 여기인가 — 캐시 정리(SKILL.md 10단계)는 사람이 손으로 돌리는 자리였고,
    **건너뛰어도 아무도 알려주지 않았다.** 위생 점검(09:00)은 VM 에서 도는데 캐시는
    이 PC 에 있어 볼 수가 없고, 스케줄러에도 정리를 부르는 작업이 없다. 그래서 그냥
    쌓였다 — 2026-08-04 에 처음 비웠을 때 309건 282 MB 였다. 수집은 doc-archive 를
    돌릴 때마다 **반드시 지나가는 자리**라, 여기 두면 빠뜨릴 수가 없다
    (WHK 결정 2026-08-23).

    **이번 회차 원본은 안 지운다** — 지우는 것은 `.doc-state.json` 에 이미 기록된
    것뿐이고, 그 기록은 변환·검토가 끝난 뒤(9단계)에 쓰인다. 그래서 7.5 점검표가
    주석 대조에 쓰는 **이번 배치의 원본은 다음 실행 전까지 그대로 남는다.**

    **`--dry-run` 에서는 한 건도 안 지운다.** 「뭐가 있나 보기만」이 지우는 명령이
    되면 안 된다.
    """
    from prune_cache import mb, remove, select   # 순환 임포트를 피해 여기서 부른다

    doomed, _kept = select(state_path, out_dir)
    if not doomed:
        return 0, 0.0
    size = mb(doomed)
    if dry_run:
        print(f"변환 끝난 원본 {len(doomed)}건 ({size:.1f} MB) — dry-run 이라 안 지웁니다")
        return 0, size
    failed = remove(doomed, out_dir)
    n = len(doomed) - len(failed)
    print(f"변환 끝난 원본 {n}건 ({size:.1f} MB) 정리했습니다")
    for p, e in failed:
        print(f"  ! 못 지웠습니다: {p.name} — {e}")
    return n, size


def main():
    # 연동 모드에서는 원문을 쓰지 않는다. **맨 앞이어야** 한다 —
    # 인자 해석이나 Slack 호출이 먼저 돌면 막기 전에 밖으로 나간다.
    from mode import exit_if_blocked
    exit_if_blocked("Slack 첨부 내려받기(fetch_slack_files)")
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=str(DEFAULT_ENV))
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="skipChannels 를 읽을 Hermes config.json")
    ap.add_argument("--sync-state", default=str(DEFAULT_SYNC_STATE),
                    help="개명 지도를 읽을 대화 쪽 .sync-state.json")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--channel", action="append", help="채널명. 여러 번 줄 수 있다. 생략하면 봇이 속한 전부")
    # 목록의 정본은 파일 상단의 DOC_EXTS · PREFER_EXTS 다 (거기 주석 참조).
    ap.add_argument("--exts", default=",".join(sorted(DOC_EXTS)))
    ap.add_argument("--prefer", default=",".join(PREFER_EXTS),
                    help="같은 이름이 여러 포맷으로 올라왔을 때 채택 우선순위")
    ap.add_argument("--days", type=int, help="최근 N일만")
    ap.add_argument("--limit", type=int, default=30,
                    help="이번 실행에서 **새로 내려받을** 최대 건수 "
                         "(이미 캐시에 있는 것은 한도를 안 먹는다)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--manifest", help="결과 JSON 을 쓸 경로")
    ap.add_argument("--refetch", action="append", metavar="파일ID",
                    help="이 파일 ID 는 이미 아카이브에 들어 있어도 다시 받는다. "
                         "규칙을 고쳐 다시 변환해야 할 때만 쓴다. 여러 번 줄 수 있다")
    args = ap.parse_args()

    token = read_token(Path(args.env_file))
    exts = {e.strip().lower().lstrip(".") for e in args.exts.split(",") if e.strip()}
    out_dir = Path(os.path.expanduser(args.out))

    # 받기 전에 캐시부터 턴다. 10단계를 사람이 빠뜨려도 안 쌓이게 하는 자리다.
    autoprune(Path(args.state), out_dir, dry_run=args.dry_run)

    refetch = {t.strip() for raw in (args.refetch or []) for t in str(raw).split(",") if t.strip()}

    filters = load_filters({})
    state_path = Path(args.state)
    if state_path.exists():
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            filters = load_filters(state_data, refetch=refetch)
            # **반드시 화면에 찍는다.** 조용히 다시 받으면 같은 문서가 두 벌 쌓인
            # 이유를 나중에 못 찾는다. 기록에 없는 ID 를 준 경우도 알린다 —
            # 오타 하나면 「다시 받았다」고 믿은 채 아무것도 안 일어난다.
            if refetch:
                known_ids = set(state_data.get("slack_files") or {})
                hit = sorted(refetch & known_ids)
                miss = sorted(refetch - known_ids)
                print(f"다시 받기: {len(hit)}건 (--refetch)")
                if miss:
                    print(f"! 기록에 없는 ID {len(miss)}건은 --refetch 가 할 일이 없습니다: {', '.join(miss)}")
        except Exception as e:
            print(f"! 상태 파일을 읽지 못했습니다 ({e}) — 전부 새것으로 봅니다")

    # 개명 지도. **못 읽어도 멈추지 않되 조용히 넘어가지도 않는다** — 지도가 비면
    # 개명된 채널의 사업장이 새 이름으로 잡혀 폴더가 두 벌이 된다(위 주석 ①).
    amap, cmap = {}, {}
    sync_path = Path(args.sync_state)
    try:
        _state = json.loads(sync_path.read_text(encoding="utf-8"))
        amap = archive_channel_names(_state)
        cmap = current_channel_names(_state)
    except Exception as e:
        print(f"! 개명 지도를 읽지 못했습니다 ({sync_path}: {e}) — 채널의 현재 이름을 "
              f"그대로 사업장으로 씁니다. 개명된 채널이 있으면 폴더가 두 벌이 됩니다")

    chans = bot_channels(token)

    # **거부가 여기 있다.** 선언 없는 비공개 채널은 내려받지 않는다(fail closed).
    # 개명 되짚기에 쓰는 `cmap` 은 위(개명 지도 자리)에서 이미 만들어 둔다.
    before = len(chans)
    chans, rejected = select_channels(
        chans, private_channels(Path(args.config)), skip_channels(Path(args.config)), cmap)
    report_rejected_private(rejected, Path(args.config))
    if rejected:
        print(f"선언 안 된 비공개 채널 {len(rejected)}개 건너뜀 — 내려받지 않았습니다")
    dropped = before - len(chans) - len(rejected)
    if dropped:
        print(f"제외 채널 {dropped}개 건너뜀 (config.json 의 digest.skipChannels)")

    if args.channel:
        want = {c.lstrip("#") for c in args.channel}
        chans = [c for c in chans if c["name"] in want]
        missing = want - {c["name"] for c in chans}
        if missing:
            # **「봇이 안 들어갔다」만이 아니다.** `chans` 는 이미 `select_channels` 를 거쳐
            # skipChannels 로 뺀 것과 선언 안 된 비공개 채널이라 거부된 것도 빠진 뒤다 —
            # 봇이 멀쩡히 들어가 있어도 여기 걸린다. 형제 스크립트
            # `apply_approvals.py` 의 같은 메시지(315행)와 같은 결로 셋을 다 알린다.
            print(f"! 봇이 안 들어갔거나 · digest.skipChannels 로 제외됐거나 · "
                  f"선언 안 된 비공개 채널이라 거부된 채널: {', '.join(sorted(missing))}")
    if not chans:
        sys.exit("ERROR: 대상 채널이 없습니다.")

    # 개명된 채널은 **눈에 띄게** 알린다. 사업장 폴더를 만드는 것은 사람이라, 화면에
    # 안 뜨면 새 이름으로 폴더를 만들게 된다 (2026-09-16 에 실제로 그럴 뻔했다).
    renamed = [(c["name"], archive_channel(c["name"], amap))
               for c in chans if archive_channel(c["name"], amap) != c["name"]]
    if renamed:
        print("\n" + "=" * 60)
        print("개명된 채널이 있습니다 — 사업장 이름은 아카이브 쪽을 씁니다")
        for now, proj in renamed:
            print(f"  #{now} → 사업장 「{proj}」  (documents/projects/{proj}/)")
        print("새 이름으로 폴더를 만들면 같은 사업장이 두 벌로 갈립니다.")
        print("=" * 60)

    # **정수로 보낸다.** 슬랙은 oldest 의 소수가 7자리를 넘으면 `ok: true` 에 에러도 없이
    # **빈 목록**을 돌려준다 (2026-08-06 실측: 같은 채널·같은 시각에 자릿수만 바꿔 호출 →
    # 정수·1자리·6자리는 4건, 7자리·9자리는 0건). `str(time.time() - N*86400)` 은 그때그때
    # `1785743305.4107215` 처럼 7자리를 만들어서, --days 를 붙이면 오늘 올라온 첨부가
    # 통째로 안 보였다. 자릿수가 6 이하로 떨어지는 순간에는 우연히 동작해서 더 헷갈렸다.
    oldest = str(int(time.time() - args.days * 86400)) if args.days else None

    found, downloaded, skipped, failed = [], [], [], []
    n_bot = 0
    # `classify()` 가 돌려주는 이름을 그대로 키로 쓴다. `known` 은 지금도 조용히
    # 걸러지므로 세기만 하고 안 찍는다 — 출력을 바꾸는 것은 이 작업 범위가 아니다.
    n = {"known": 0, "excluded": 0, "deferred": 0, "superseded": 0, "other_format": 0}
    for c in chans:
        for m in history_with_threads(token, c["id"], oldest):
            # 봇이 올린 첨부는 아예 보지 않는다 (WHK 결정 2026-08-12).
            # **확장자를 보기 전에 거른다** — 이건 포맷 문제가 아니라 출처 문제라,
            # 「다른 포맷으로 이미 있음」 같은 집계에 섞이면 안 된다.
            # Hermes 위생 점검(`archive-health.js` 의 `channelAttachments`)이 같은 판정을
            # 쓴다 — 갈리면 DM 은 「미변환 N건」인데 여기는 「새 첨부 0건」이 된다.
            if is_bot_message(m):
                n_bot += len(m.get("files", []))
                continue
            for f in m.get("files", []):
                # 파일명 확장자를 먼저 본다. 슬랙의 filetype 은 .hwp 를 'binary' 로
                # 보고해서, 그걸 믿으면 한글 문서가 통째로 걸러진다 (에러 없이 0건).
                name = f.get("name") or ""
                suffix = Path(name).suffix.lstrip(".").lower()
                ext = suffix or (f.get("filetype") or "").lower()
                if ext not in exts:
                    continue
                # 스레드에 `[공개]` 댓글이 있으면 표시해 둔다. 판정은 변환 단계에서
                # 사람이 메타 `공개승인` 줄로 옮겨 적어야 실제로 열린다(doc-archive 6단계).
                approved = any(PUBLIC_TAG.search(t) for t in (m.get("_thread_texts") or []))
                rec = {
                    "id": f.get("id"),
                    # **사업장 이름이지 슬랙의 현재 이름이 아니다.** 개명된 채널은 둘이
                    # 갈리고, 여기서 현재 이름을 쓰면 `name_key`·캐시 폴더·`.doc-state.json`
                    # 이 전부 새 이름으로 찍혀 사업장이 두 벌이 된다
                    # (`archive_channel_names` 머리말의 ①②).
                    # 슬랙에서 어디로 가야 하는지는 `slack_channel` 이 들고 있다.
                    "channel": archive_channel(c["name"], amap),
                    "slack_channel": c["name"],
                    "name": f.get("name") or f.get("id"),
                    "ext": ext,
                    "size": f.get("size"),
                    "ts": m.get("ts"),
                    "date": time.strftime("%Y-%m-%d", time.localtime(float(m.get("ts", 0)))),
                    "user": m.get("user"),
                    "url": f.get("url_private_download"),
                    "public_tag": approved,
                }
                reason = classify(rec, filters)
                if reason:
                    n[reason] += 1
                    continue
                if not rec["url"]:
                    skipped.append({**rec, "reason": "다운로드 URL 없음 (권한 제한 파일)"})
                    continue
                if not any(x["id"] == rec["id"] for x in found):
                    found.append(rec)

    # 같은 자료를 hwpx 와 pdf 로 함께 올리는 일이 흔하다 (주간보고 첨부 등).
    # 둘 다 변환하면 같은 내용이 두 벌 쌓여 검색이 중복 히트로 찬다.
    # 이름(확장자 제외)과 채널이 같으면 원본 포맷 쪽 하나만 남긴다.
    prefer = [e.strip().lower() for e in args.prefer.split(",") if e.strip()]
    rank = {e: i for i, e in enumerate(prefer)}
    best, dropped = {}, []
    for r in found:
        # 위 `known_names`·`excluded_names` 와 **같은 키**여야 한다. 전에는 여기만
        # 인라인으로 따로 계산해서, 한쪽만 고치면 조용히 갈릴 자리였다.
        key = name_key(r["channel"], r["name"])
        cur = best.get(key)
        if cur is None:
            best[key] = r
        elif rank.get(r["ext"], 99) < rank.get(cur["ext"], 99):
            dropped.append({**cur, "reason": f"같은 자료의 {r['ext']} 판을 채택"})
            best[key] = r
        else:
            dropped.append({**r, "reason": f"같은 자료의 {cur['ext']} 판을 채택"})
    if dropped:
        print(f"중복 포맷 {len(dropped)}건 제외 (같은 이름, 다른 확장자)")
        skipped += dropped
    found = list(best.values())

    found.sort(key=lambda r: r["ts"] or "", reverse=True)

    def dest_of(r):
        return out_dir / safe_name(r["channel"]) / f"{r['id']}_{safe_name(r['name'])}"

    # **캐시 판정을 `--dry-run` 에서도 똑같이 한다.** 전에는 dry-run 분기가 캐시 확인보다
    # 먼저 와서 이미 받아둔 것까지 `[dry]` 로 찍었다 — 미리보기가 실제 동작과 달랐다.
    to_fetch, in_cache, remaining = plan_fetch(
        found, args.limit, lambda r: dest_of(r).exists())
    fetch_ids = {r["id"] for r in to_fetch}
    cache_ids = {r["id"] for r in in_cache}

    # 조용히 빼면 "34건이라더니 왜 1건이지" 를 다음 사람이 알 수 없다. 이유별로 찍는다.
    if n_bot:
        print(f"봇이 올린 첨부 {n_bot}건 제외 (봇 산출물은 근거로 삼지 않습니다)")
    if n["other_format"]:
        print(f"이미 변환된 자료의 다른 포맷 판 {n['other_format']}건 제외 (상태 파일의 이름과 대조)")
    if n["excluded"]:
        print(f"일부러 뺀 첨부 {n['excluded']}건 제외 (.doc-state.json 의 excluded)")
    if n["deferred"]:
        print(f"보류 중인 첨부 {n['deferred']}건 제외 (만기가 지나면 저절로 돌아옵니다 — decide.py --list)")
    if n["superseded"]:
        print(f"최신이 대신한 첨부 {n['superseded']}건 제외 (decide.py --list 에 무엇이 대신했는지 있습니다)")

    print(f"\n대상 채널 {len(chans)}개 · 새 첨부 {len(found)}건 (확장자 {','.join(sorted(exts))})")

    # `[공개]` 가 달린 것은 눈에 띄게 알린다. 조용히 지나가면 변환할 때 `공개승인` 줄을
    # 빠뜨려, 팀이 공개하기로 한 자료가 계속 비공개로 남는다.
    tagged = [r for r in found if r.get("public_tag")]
    if tagged:
        print(f"\n스레드에 [공개] 태그가 달린 첨부 {len(tagged)}건 — 변환 시 메타에 `공개승인` 을 적을 것:")
        for r in tagged:
            print(f"  [공개] #{r.get('slack_channel') or r['channel']} {r['name']}")
    if remaining:
        print(f"이번 실행은 새로 {len(to_fetch)}건만 받습니다. 남은 {len(remaining)}건은 다시 실행하세요.")
        print("  (이미 받아둔 것은 한도를 안 먹으므로 회차를 거듭하면 결국 전부 받아집니다)")

    # `found` 를 그대로 돌아 순서를 지킨다 — 세 갈래로 나눠 돌면 매니페스트의 차례가
    # 캐시/새것으로 갈려 버린다.
    for r in found:
        dest = dest_of(r)
        if r["id"] in cache_ids:
            r["path"] = str(dest)
            downloaded.append(r)
            print(f"  = 이미 있음 {r['name']}")
            continue
        if r["id"] not in fetch_ids:
            continue  # 한도 때문에 이번엔 안 받는 것
        if args.dry_run:
            print(f"  [dry] {r['date']} #{r.get('slack_channel') or r['channel']} {r['name']} ({r['size']} B)")
            r["path"] = str(dest)
            downloaded.append(r)
            continue
        err = download(token, r["url"], dest)
        if err:
            failed.append({**r, "reason": err})
            print(f"  ✗ {r['name']} — {err}")
            if "files:read" in err:
                print("\n스코프가 빠져 있어 더 진행해도 전부 실패합니다. 중단합니다.")
                print("  api.slack.com/apps → Hermes → OAuth & Permissions → files:read 추가 → Reinstall")
                break
        else:
            r["path"] = str(dest)
            downloaded.append(r)
            print(f"  ✓ {r['date']} #{r.get('slack_channel') or r['channel']} {r['name']}")

    result = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "channels": [c["name"] for c in chans],
        "total_new": len(found),
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
    }
    if args.manifest:
        atomic_write_text(
            Path(args.manifest), json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(f"\n매니페스트: {args.manifest}")

    print(f"\n받음 {len(downloaded)} · 건너뜀 {len(skipped)} · 실패 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
