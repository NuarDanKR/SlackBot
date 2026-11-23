# VIPBot 매니페스트·전달 계약을 공통 답변 경로에 맞춤

> **폐기된 절체 기록(2026-11-12).** 공유 `tybot.service` 분기가 운영 정본이며
> 독립 `tybot-vipbot.service`와 실행 코드는 삭제됐다. 아래 절차를 다시 실행하지 않는다.

상태: 매니페스트·시험만 변경. **운영 변경 없음** — `tybot-vipbot.service` 는
그대로 돌고 있고 `VIPBOT_SHARED_RUNTIME_ENABLED` 는 계속 `0` 이다.
Slack 앱 설정은 아직 손대지 않았다(§4 가 절차다).

전제 결정: [`2026-11-09-vipbot-shared-tybot-branch.md`](2026-11-09-vipbot-shared-tybot-branch.md)

## 0. 오너 결정 — 다른 것은 **넷**

VIPBot 은 별도 답변 제품이 아니다. 같은 `WorkspaceBot` · 같은 `AnswerEngine` 을
쓰고 다른 것은 넷뿐이다.

1. VIP 모델·프롬프트·비용 상한
2. VIP 사용자 허용 판정 (`bot_access(bot='vipbot')`)
3. 관리형 근거 채널 (`bot_source_channel(bot='vipbot')`)
4. **상세 답변은 요청자 DM 으로만 전달** (`private_answers`)

1~3 은 *무엇을 근거로 답하나*이고 4 는 *누가 그 답을 보나*다. 넷째가 이번에
추가됐고, 그래서 「VIP 는 TYBot 과 똑같이 보인다」 가 아니라 **「만드는 것은
같고 보내는 곳만 다르다」** 가 정확한 문장이다.

## 1. 매니페스트 — 모자란 스코프 넷

VIPBot 매니페스트는 **별도 답변 제품**이던 시절에 쓰였다. 그때 계약은
「Canvas 금지 · Slack 호출 두 개」 였다.

VIPBot 이 구성 분기가 된 뒤로 그 매니페스트는 **계약이 아니라 결함**이다.
스코프가 모자라면 공통 경로는 실패를 삼키고 한 단계 아래 형식으로 내려간다.
그래서 고장이 **조용하다** — 같은 질문에 TYBot 은 Canvas, VIPBot 은 긴 메시지가
나가고 로그에는 경고 한 줄만 남는다.

| 없던 것 | 공통 경로에서 부르는 곳 | 없을 때 |
|---|---|---|
| `canvases:write` | `canvas_answer.create` · `canvases.access.set` | Canvas 답변이 메시지로 되돌아간다 |
| `reactions:write` | `_handle_request` 가 요청마다 `reactions.add` | 👀 가 안 붙어 사람이 "무시당했나" 로 읽는다 |
| `channels:read` · `groups:read` | `_channel_name` → `conversations.info` | 출처·근거 보기·감사기록에 `C09…` 가 남는다 |
| `interactivity.is_enabled` | `evidence_view.blocks` 의 「근거 보기」 | 버튼은 그려지는데 눌러도 아무 일이 없다 |

**전달이 DM 이라고 스코프가 줄지 않는다.** Canvas 를 *만드는* 호출은 똑같이
일어나고, 채널에는 고정 안내 한 줄이 나가므로 `chat:write` 도 그대로 필요하다.
바뀌는 것은 `chat.postMessage` 의 **대상 채널**뿐이다.

`channels:read` 는 권한을 넓히지 않는다. 범위 판정은 `channel_id` 로 하고
(`access.can_access`), 이 스코프가 하는 일은 채널 이름 한 줄이다. 다만 그 한
줄이 출처 형식이라, 없으면 원칙 2 가 요구하는 「사람이 부르는 조직 이름」 이
VIP 답변에서만 사라진다.

`interactivity` 를 켜도 **인바운드 포트는 열리지 않는다.** `request_url` 이
없으므로 상호작용 페이로드도 Socket Mode 로 들어온다.

### 넣지 않은 것

- `channels:history` · `groups:history` · `mpim:history` — 수집은 TYBot·Archiving
  의 일이다. VIP 의 실시간 조회도 VIP 토큰으로 읽지 않는다(§5-2)
- `commands` — Slack 은 한 워크스페이스에서 **같은 슬래시 명령 이름을 두 앱이**
  가질 수 없게 한다. `/피드백` · `/권한` 은 TYBot 이 쥐고 있다(§5-1)
- `reactions:read` · `reaction_added` — 👀 를 **붙이는** 것과 남의 반응을
  **읽는** 것은 다른 스코프다. 공통 봇에는 반응 이벤트 처리기가 아예 없다
- `app_home_opened` — 홈 탭을 열지 않으므로(`home_tab_enabled: false`) 그
  처리기의 유일한 갈래(`tab == "home"`)가 닫혀 있다
- `files:write` · `canvases:read` · `channels:join` · `channels:manage` ·
  `groups:write` · 모든 수집·수명주기 이벤트 — 쓰는 길. 운영 원문 writer 는
  TYBot 하나다

### 스코프로는 막지 못하는 것

채널에 글을 쓸 권한(`chat:write`)은 어차피 있어야 한다 — 고정 안내가 거기로
나간다. 즉 **「상세는 DM 으로만」 은 매니페스트가 아니라 코드가 지키는 규칙**
이다(`private_answers`). 그래서 그 동작을 시험이 따로 고정한다(§3).

## 2. 전달 계약 — 코드가 실제로 하는 일

`src/tybot/slack/pilot.py` (Codex 작업, 읽기만 함) 기준.

| 상황 | 채널 | 요청자 DM |
|---|---|---|
| 채널 멘션, 정상 답변 | 👀 + 고정 안내 한 줄 | 본문·출처·blocks |
| 채널 멘션, Canvas 답변 | 👀 + 고정 안내 한 줄 | Canvas 링크, 권한은 `user_ids` |
| 채널 멘션, 권한 거절 | 👀 만 | `DENY_MESSAGE` |
| 채널 멘션, 처리 실패 | 👀 만 | 실패 안내 |
| 채널 멘션, **DM 열기/전송 실패** | 👀 만 | — (아무것도 안 감) |
| DM 질문 | — | 그 DM 에 그대로 답한다 |
| TYBot (모든 경우) | 기존대로 채널·스레드 | — |

세 가지가 설계에서 중요하다.

- **진행 안내가 채널에 안 뜬다.** `private_channel_request` 면 `progress.start()`
  를 아예 부르지 않는다. 「확인하고 있습니다」 한 줄도 누가 무엇을 물었는지를
  드러낸다
- **Canvas 권한은 `user_ids`.** `channel_ids` 로 주면 링크를 숨겨도 그 채널
  사람이 파일 목록에서 찾을 수 있다 — DM 전용 전달이 무의미해진다
- **DM 이 실패하면 안내도 안 남긴다.** 안내만 남기면 사람은 DM 을 열어 보고
  아무것도 없는 것을 확인한다. 「답이 갔다」 보다 나쁘다 — 어디를 봐야 하는지
  모르게 만든다

👀 는 채널에 남는다. 본문이 아니라 「받았다」 는 신호이고, 질문 자체는 이미
그 채널에 보인다. 이 표시마저 없으면 사람은 봇이 무시했다고 읽는다.

## 3. 시험

### `tests/test_vipbot_boundary.py` (선언)

- **교체** 「Canvas 금지」 스코프 목록 → 공통 경로가 부르는 스코프 집합
- **교체** 「Slack 호출 둘뿐」 → legacy 독립 런타임 **동결** 계약. 절체 전까지
  운영에서 돌고 있으므로 보호는 두되 더 자라지 못하게 한다. 더할 곳은 공통
  경로다 — 그래야 TYBot 과 VIP 가 **같이** 바뀐다
- **교체** 「기존 코드가 VIP 경로를 물면 안 된다」 → 「공통 분기가 legacy
  `vipbot_delivery` 를 부르면 안 된다」. 전제가 바뀌었다 — 한 프로세스에 두
  토큰이 뜨는 것은 이제 설계이고, 토큰이 **같아지는** 것만 막으면 된다
  (`_require_distinct_runtime_tokens`). 남은 금지는 전달 경로가 둘이 되는 것이다
- **유지** 별도 앱 · Socket Mode · 사용자 토큰 없음 · 수집 스코프/이벤트 없음 ·
  `files:write` 금지
- **신규** `vip_workspace` · `vip_profile` · `vip_access` import 화이트리스트
- **신규** 매니페스트 ↔ 공통 경로 Slack 호출 **양방향** 대조. 표에만 있고 코드에
  없는 호출도, 코드에 있고 분류되지 않은 호출도 깨진다

> `tybot.archive.store` 를 금지 목록에서 뺐다. 그것은 **읽는** 쪽이고 공통 답변
> 엔진이 그것으로 근거를 연다. 읽기와 쓰기를 한 목록에 섞으면 둘 중 하나는
> 반드시 틀린다.

### `tests/test_vip_common_answer_path.py` (동작, 신규)

같은 `WorkspaceBot` 클래스로 TYBot 구성과 VIP 구성을 만들어 실제 `_handle` 을
지나가게 한다(엔진·QA·Slack 클라이언트만 대역). 두 축을 고정한다.

**생성은 공통** — 본문·출처가 TYBot 과 **글자까지 같다**(VIP 는 그 본문이 DM 으로
간다는 점만 다르다). Canvas 판단과 Canvas 본문도 같은 코드가 만든다. 근거 없는
답은 양쪽 다 빈 Canvas 로 싸지 않는다. 질문 첨부는 권한 판정 뒤 공통 리더가 VIP
자기 토큰으로 열고, 출처는 Slack permalink 이며, 임시 근거는 좌표로 남지 않는다
(`channel_id is None`). 후속 질문은 좌표만 잇고 이전 **답변 문장은 프롬프트에
넣지 않는다**.

**전달은 VIP 가 다름** — 위 §2 표의 일곱 줄을 전부 시험으로 박았다. 특히
DM 실패 세 경우(열기 실패 · DM 아닌 채널 반환 · 전송 예외)에서 **채널에
아무것도 남지 않는지**를 각각 본다.

> 이 세 개가 이 파일에서 가장 중요하다. 「DM 이 안 되면 채널에라도」 는 고장
> 났을 때 가장 자연스러운 수습처럼 보이고, 그 순간 VIP 답변이 통째로 공개된다.
> 코드가 의도적으로 **안 하는** 일은 시험이 없으면 다음 사람이 「친절하게」
> 되살린다.

TYBot 쪽도 함께 고정했다 — 채널 답변, 스레드 답변(**본문이** 그 스레드에 간다),
Canvas 의 `channel_ids` 권한. DM 전용 전달을 넣다가 TYBot 전달이 따라 바뀌는 것을
막는다.

### 돌연변이

매니페스트 7종 · 대조표 3종 · 구성 플래그 2종, **전부 잡힘**.

| | 바꾼 것 | 잡은 시험 |
|---|---|---|
| 1 | `canvases:write` 제거 | 스코프 집합 · 호출↔스코프 대조 |
| 2 | `is_enabled: true` → `false` | 근거 보기 · trigger 호출 |
| 3 | `reactions:write` 제거 | 스코프 집합 · 대조 |
| 4 | `channels:read` 제거 | 스코프 집합 · 대조 |
| 5 | `files:write` 추가 | 금지 스코프 · 집합 |
| 6 | `channels:history` 추가 | 금지 스코프 · 집합 · TYBot 전용 확인 |
| 7 | `message.channels` 이벤트 추가 | 금지 이벤트 · 집합 |
| 8 | 표에 없는 호출(`chat_scheduleMessage`) 추가 | 「표에만 있고 코드에 없다」 |
| 9 | `TYBOT_ONLY_CALLS` 에서 `conversations_history` 제거 | 분류 강제 |
| 10 | 표에서 `canvases_create` 제거 | 분류 강제 |
| 11 | **VIP `private_answers=False`** (구성 유실) | 14건 — DM 전용 계약 전부 |
| 12 | **TYBot `private_answers=True`** (전달 오염) | 4건 — TYBot 채널·스레드·Canvas |

11·12 가 특히 중요하다. `build_bots` 가 `private_answers` 를 빠뜨리면 VIP 답변이
조용히 채널로 나간다 — 그 회귀를 14건이 잡는다. 반대로 공통 봇의 기본값이
바뀌면 TYBot 전달이 통째로 DM 으로 바뀌고, 그것도 4건이 잡는다.

## 4. 실제 Slack 앱에 반영하는 절차 (아직 하지 않음)

절체(`VIPBOT_SHARED_RUNTIME_ENABLED=1`) **전에** 한다. 스코프를 먼저 넓히는 것은
안전하다 — 매니페스트에 있어도 공통 분기가 꺼져 있으면 아무도 부르지 않는다.
반대로 분기를 먼저 켜면 첫 Canvas 답변에서 `missing_scope` 가 난다.

1. api.slack.com/apps → VIPBot → App Manifest 에 이 파일을 붙여넣는다
2. **재설치가 필요하다.** 스코프가 늘었으므로 워크스페이스 관리자가 다시
   승인해야 하고, 승인 전까지 **봇 토큰은 옛 스코프 그대로**다
3. 재설치 뒤 봇 토큰이 바뀐다 — 콘솔의 VIPBot 자격증명을 갱신한다
   (`console/bot_identity`). 갱신 전에는 공통 분기가 옛 토큰으로 뜬다
4. 확인(채널에서): ① 멘션 → **채널에 안내 한 줄만** 남고 본문은 DM 에 오는가
   ② 「캔버스로」 요청 → 링크가 DM 에만 있고 채널에 없는가
   ③ 권한 없는 사용자 → 거절도 DM 인가
5. 확인(DM 에서): ④ DM 질문이 그 DM 에서 답되는가 ⑤ 파일 붙여 질문
   ⑥ 답변의 「근거 보기」 클릭
6. 같은 질문을 TYBot 에도 보내 **본문·출처 형식**을 눈으로 대조한다.
   TYBot 은 전처럼 채널에 답해야 한다

## 5. 남은 차이

### 5-1. 슬래시 명령이 VIP 에 없다 (중간, 플랫폼 제약)

`/피드백` · `/권한` · `/일정` 은 TYBot 이 쥐고 있고, Slack 은 한 워크스페이스에서
같은 명령 이름을 두 앱이 갖는 것을 허용하지 않는다. VIP 사용자는 같은 명령을
TYBot 에서 써야 한다.

「UX 가 TYBot 과 같다」 에서 유일하게 **구조적으로** 맞출 수 없는 항목이다.
안내 문구로 메울지, VIP 전용 이름(`/vip-피드백`)을 둘지는 오너 판단이다.

주의: `/피드백` 은 **답변 품질을 신고하는 유일한 입구**다. VIP 답변에 대한
피드백이 TYBot 명령으로 들어오면, 그 기록은 TYBot QA 디렉터리에 남는다 —
VIP QA 와 분리해 둔 것과 어긋난다. 절체 전에 정하는 편이 낫다.

### 5-2. 실시간 조회는 TYBot 토큰으로 읽는다 (낮음, 설계대로)

`live_bots` 에 VIP 인스턴스는 들어가지 않는다. VIP 의 실시간 근거는 그
워크스페이스를 소유한 TYBot 인스턴스가 읽는다. 범위는 VIP 의
`evidence_sources` 로 좁혀지므로 권한이 새지는 않지만, **TYBot 이 그 채널에
없으면 조용히 0건**이 된다. VIP 근거 채널을 등록할 때 TYBot 멤버십도 같이
확인해야 한다.

### 5-3. 첫 사용 안내가 VIP 사용자에게 가지 않는다 (낮음, 의도)

홈 탭을 열지 않으므로 `on_home_opened` 의 유일한 갈래가 닫혀 있다. VIP 사용자는
TYBot 에서 이미 안내를 받았을 가능성이 높아 이번에는 열지 않았다.

다만 **DM 전용 전달은 설명이 필요하다** — 채널에서 불렀는데 안내 한 줄만 남으면
처음 쓰는 사람은 고장으로 읽는다. 고정 안내 문구(`PRIVATE_ANSWER_NOTICE`)가 그
역할을 하는지 ④ 시험에서 사람 눈으로 확인할 것.

### 5-4. (해소) DM 질문이 중앙 아카이브에 쓰이던 문제

직전 회차에 보고한 결함 — `route_message` 가 DM 이면 `_ingest_dm` 을 무조건
불러 VIPBot 이 중앙 원문 writer 가 되던 것 — 은 Codex 가 고쳤다.
`operational_owner` 로 `_ingest_dm` 과 `_mirror_dm_handoff` 를 둘 다 막는다
(`src/tybot/slack/pilot.py:1350`). 재확인함.

## 6. 결과

- `pytest tests/test_vip_common_answer_path.py tests/test_vipbot_boundary.py` — 99 passed
- VIP·공통 답변 관련 11개 파일 — 324 passed
- 전체 pytest — §7
- `ruff check src tests scripts` — All checks passed
- 커밋하지 않았다

## 7. 전체 pytest

**8004 passed · 222 skipped · 0 failed** (604s). 경고 7건은 기존 것으로 이 작업과
무관하다.

직전 실행에서 났던 4건(`test_derived_text_nul.py` · `test_diagnose_attachments.py`)은
Codex 가 `archive/convert.py` · `files.py` 를 고치는 **도중에** 10분짜리 실행이 반쯤
쓰인 트리를 읽은 것이었고, 이번 실행에서 사라졌다.

## 8. 수정한 파일

```
 M docs/pilot/vipbot-slack-app-manifest.yaml       스코프·상호작용·DM 전용 주석
 M tests/test_vipbot_boundary.py                   선언 계약 재작성
?? tests/test_vip_common_answer_path.py            동작 계약 (신규)
?? docs/verification/2026-11-09-vipbot-manifest-common-path.md
```

`src/tybot/slack/pilot.py` · `src/tybot/vip_workspace.py` ·
`src/tybot/console/bot_identity.py` · `tests/test_workspace_bot_variants.py` 는
Codex 작업 중이라 읽기만 했다. `tybot-vipbot.service` 는 건드리지 않았다.
