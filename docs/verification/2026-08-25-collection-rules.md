# 검증 문서 — 채널 규칙 기반 자동 수집 · 캔버스 · 점검 잡

_작성 2026-08-25 · 작성 주체 Claude(봇 담당) · **검증 주체: Codex 또는 다른 에이전트**_

이 문서는 "무엇을 했다"가 아니라 **"어떻게 확인하면 되는지"**를 남긴다.
각 항목은 ① 주장 ② 확인 명령 ③ 기대 출력 ④ 내가 확신하지 못하는 부분 순이다.

전제:
```bash
git config core.hooksPath .githooks
pip install -e ".[dev]"
pytest -q          # 292 passed
ruff check src tests scripts   # All checks passed
```

---

## A. 채널 이름 규칙으로 수집 대상을 가른다

**주장** — `#<본부|실|팀|현장|프로젝트>-<조직명>_<조직코드>-<업무>` 형식만 수집한다.
구 형식(`#팀_자금(ABB540)_주간보고`)은 **폐기**해 인식하지 않는다.

```bash
python - <<'EOF'
import sys; sys.path.insert(0, "src")
from tybot.channels import parse, should_collect
for n in ["#팀-전산_ABB110-주간회의", "#현장-김해외동_180182-채팅방",
          "#팀_자금(ABB540)_주간보고", "#점심메뉴", "#팀-자금-주간보고"]:
    print(should_collect(n), n)
EOF
```
기대: 앞 2개 `True`, 뒤 3개 `False`
(마지막은 **조직코드가 없어서** 탈락 — 코드는 필수다)

이 판정은 자동 참여뿐 아니라 실시간 수집·수동 `수집`/`전체수집`·정기 백필에 모두 적용한다.
봇이 이미 멤버여도 규칙 밖 채널은 읽거나 저장하지 않는다.

```bash
pytest -q tests/test_channels.py    # 19 passed
```

**확신하지 못하는 것**
- 조직명에 `_` 가 들어가는 조직이 실제로 있는지 확인 못 했다. 정규식이 `org` 를 `[^_]+` 로
  잡으므로 `#팀-정보_전략_ABB110-회의` 는 파싱이 어긋난다. 실제 조직명 목록으로 검증 필요.
- 두문자 5개(`본부·실·팀·현장·프로젝트`)가 전부인지 오너 확인 필요. `그룹`·`센터` 등이 있으면
  `PREFIX_KINDS` 에 추가해야 한다.

---

## B. 규칙에 맞는 **공개** 채널은 초대 없이 봇이 참여한다

**주장** — `conversations.join` 으로 봇이 스스로 들어간다. 기동 시 1회 스윕 +
`channel_created`·`channel_rename` 이벤트로 즉시 반영.

```bash
pytest -q tests/test_autojoin.py    # 11 passed
```
코드 확인 지점:
- `src/tybot/autojoin.py::sweep` — 규칙 불일치는 `skipped_rule`
- `src/tybot/slack/pilot.py::WorkspaceBot.autojoin_sweep` — 기동 시 호출(`connect()` 끝)
- 같은 파일 `_register()` 의 `channel_created`·`channel_rename` 핸들러

서버에서:
```bash
journalctl -u tybot | grep autojoin
# [pilot] autojoin joined=3 already=2 skipped=7 private_skipped=0 failed=0
```

**전제 조건** — Slack 앱 매니페스트에 다음이 있어야 한다. 없으면 조용히 동작하지 않는다.
```yaml
scopes: channels:join, channels:read
bot_events: channel_created, channel_rename
```

---

## C. 비공개 채널은 자동 참여가 **불가능**하다

**주장** — Slack 설계상 봇 토큰으로는 비공개 채널에 자가 참여할 수 없고, 목록 조회도 안 된다.
`conversations.join` 은 공개 채널 전용이다.

따라서 자동 참여 스윕은 **초대가 필요한 비공개 채널 목록을 만들 수 없다.** 목록에 보이는 비공개
채널은 이미 봇이 멤버인 곳뿐이다. 초대 대상 확인은 채널 관리자 절차나 일회성 사용자 토큰 도구로 한다.

**검증 방법** — 서버에서 직접 확인하는 게 가장 확실하다:
```bash
# 비공개 채널 ID 를 알고 있어도 봇 토큰으로는 실패해야 한다
curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  -d "channel=<비공개채널ID>" https://slack.com/api/conversations.join | jq .error
# 기대: "method_not_supported_for_channel_type" 또는 "channel_not_found"
```

대안 도구: `scripts/invite_bot.py` (관리자 사용자 토큰 `xoxp-`, **서버 저장 금지**)
```bash
SLACK_ADMIN_TOKEN=xoxp-... SLACK_BOT_TOKEN=xoxb-... python scripts/invite_bot.py --dry-run
```

**한계(도구가 스스로 안내함)** — 그 토큰 주인이 **멤버인** 비공개 채널만 초대 가능.
전체를 처리하려면 Enterprise Grid 의 `admin.conversations.invite` 가 필요하다(현재 플랜 밖).

**검증 요청** — 이 스크립트는 **실제 토큰으로 돌려보지 못했다.** `conversations.invite` 의
에러 문자열(`already_in_channel`) 처리가 실제 응답과 맞는지 확인이 필요하다.

---

## D. 조직 코드를 프론트매터에 남긴다

**주장** — 채널명에서 뽑은 `org_kind`·`org_code`·`org_name` 을 새 문서 생성 시 기록한다.
조직 개편·워크스페이스 통합 뒤에도 문서 출신을 추적하기 위해서다.

```bash
pytest -q tests/test_archive.py -k org    # 2 passed
```
실제 파일 확인:
```bash
find /var/lib/tybot/archive/workspaces/<ws>/channels -path '*/raw/*.md' -type f -print
head -14 /var/lib/tybot/archive/workspaces/<ws>/channels/<채널ID>__*/raw/<날짜>.md
# org_kind: team / org_code: ABB110 / org_name: 전산
```

**주의** — 기존 문서에는 이 필드가 없다. `load_doc` 이 선택 필드로 읽으므로 스키마 검사는
통과한다. 소급 적용은 하지 않았다(원문 파일 편집 금지 원칙).

---

## E. 캔버스 수집 — **가장 검증이 필요한 부분**

**확인된 것** (Slack 공식 문서, context7 로 조회)
- `conversations.info` 의 채널 `properties` 에서 캔버스 파일 ID 를 얻는다.
  근거: `conversations.canvases.create` 문서의 "You can retrieve the ID of an existing
  channel canvas by checking the channel properties via the conversations.info method."
- 캔버스는 파일(`F...`)로 존재한다.

**확인하지 못한 것 — 여기가 위험 지점**
- **캔버스 본문을 돌려주는 전용 메서드를 문서에서 찾지 못했다.** `canvases.sections.lookup` 은
  섹션 id 만 준다. 그래서 `files.info` → `url_private_download` 다운로드 경로로 구현했다.
- 실제 응답이 마크다운인지 HTML 인지 확인하지 못했다. 둘 다 처리하도록 짰다.
- `canvases:read` 스코프가 실제로 필요한지/충분한지 확인하지 못했다. 매니페스트에 넣어뒀다.

**그래서 이렇게 방어했다** — UTF-8 `text/plain`·`text/markdown` 또는 HTML만 허용한다.
그 외 MIME, 바이너리 제어문자, 디코딩 실패, API 조회 실패는 **추측해서 파싱하지 않고**
`[캔버스:미변환]` 줄만 남기고 경고를 올린다. 내용 해시 기반 `[수집키:...]` 를 함께 기록해
같은 스냅샷은 수집 시각이 달라도 다시 쓰지 않는다.

```bash
pytest -q tests/test_canvas.py    # 12 passed (모두 가짜 클라이언트)
```

**실제 검증 절차 (Codex 가 해줬으면 하는 것)**
1. 테스트 채널에 캔버스를 만들고 내용을 몇 줄 쓴다
2. `@tybot 수집` 실행
3. 응답에 `캔버스 N줄 포함` 이 뜨는지 확인
4. `journalctl -u tybot | grep 캔버스` 로 경고 여부 확인
5. 아카이브 MD 에 `[캔버스본문:...]` 줄이 들어갔는지, **내용이 원본과 같은지** 대조
6. 실패하면 어떤 에러/응답이 왔는지 기록 → `src/tybot/archive/canvas.py` 의 가정 수정

**되돌리는 법** — 캔버스 수집만 끄려면 `canvas_lines` 호출부 두 곳
(`pilot.py::_ingest_channel`, `collect.py::collect_workspace`)을 제거한다. 다른 수집 경로에 영향 없다.

---

## F. 아카이브 점검 잡 (B-01)

**주장** — 15분마다 스키마 위반·수집 밀림·중복을 점검하고 **리포트만** 남긴다. 원문은 안 건드린다.

```bash
pytest -q tests/test_tidy.py    # 15 passed
ARCHIVE_DIR=./archive REPORTS_DIR=/tmp/r python -m tybot.tidy
```
기대: `tidy docs=N lines=M errors=0 warns=K` + `/tmp/r/tidy-<날짜>.md`

**가장 중요한 성질 2개** (테스트로 고정돼 있음)
- `test_inspect_never_modifies_originals` — 점검 전후 파일 바이트가 동일
- `test_report_is_written_outside_archive` — 리포트가 `archive/` 밖이고 검색에 안 잡힘
  (안에 쓰면 그게 다시 근거로 검색돼 요약 재귀가 시작된다)

---

## G. 회귀 위험 — 이번 변경이 깨뜨릴 수 있는 것

| 위험 | 확인 방법 |
|---|---|
| 구 형식·규칙 밖 채널에 봇이 이미 있음 | 실시간·수동·정기 수집 모두 건너뛴다. 기존 문서는 수정하지 않는다 |
| 자동 참여로 채널 수가 급증 | `AUTOJOIN_CHANNELS=0` 으로 스윕과 채널 이벤트 참여를 모두 끈다 |
| 캔버스 수집이 rate limit 소모 | 수동·정기 수집에서 `conversations.info` + `files.info` 각 1회/채널 |
| 봇 답변이 캔버스에 섞임 | 캔버스는 사람이 쓰는 문서다. 봇은 캔버스에 쓰지 않는다 |

---

## I. 매니페스트 이중 관리 — 자동 대조

**주장** — 같은 매니페스트가 두 곳에 있다(`docs/pilot/slack-app-manifest.yaml`,
`console-web/src/components/SetupGuide.tsx` 의 `MANIFEST` 상수). 한쪽만 고치면
**화면 안내를 보고 만든 앱에 권한이 빠져** 봇이 오류 없이 반쪽만 동작한다.

```bash
pytest -q tests/test_manifest_sync.py    # 5 passed
```
스코프 16개·이벤트 6개 목록과 핵심 설정 3개(`socket_mode_enabled`,
`messages_tab_read_only_enabled`, `token_rotation_enabled`)를 대조한다.

실제로 잡는지 확인:
```bash
# 저장소 매니페스트에서 스코프 한 줄을 지우고 테스트 → 실패해야 한다
pytest -q tests/test_manifest_sync.py
git checkout docs/pilot/slack-app-manifest.yaml
```

---

## H. 이번에 손대지 않은 것 (다른 담당 영역)

- `console-web/`, `src/tybot/console/` — 관리 콘솔은 다른 에이전트 담당
- `src/tybot/lock.py` — 다른 세션 작업
- 실시간 수집 경로의 캔버스 반영 — 캔버스 편집 이벤트를 구독하지 않았다.
  현재는 `수집` 명령·정기 백필 시점의 스냅샷만 남는다(B-22)
