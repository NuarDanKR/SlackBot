# B-83 VIPBot Slack 경계 — 최소 매니페스트와 DM 전용 전달

작성 2026-11-05 · 신규 4 + 이 문서. **기존 TYBot 답변·수집 코드 수정 0.** 커밋하지 않았다.

| 파일 | |
|---|---|
| `docs/pilot/vipbot-slack-app-manifest.yaml` | 별도 앱 매니페스트 |
| `src/tybot/vipbot_delivery.py` | 전달 정책 (순수, Slack SDK 는 주입) |
| `tests/test_vipbot_delivery.py` | 전달 시험 40건 (대역만) |
| `tests/test_vipbot_boundary.py` | 경계 계약 27건 |

Codex 의 `vip_access.py` · `vip_request.py` 와 미커밋 `embedding_query_run.py` 계열은
건드리지 않았다. 분담대로 Codex 가 **권한·근거 범위**, 여기가 **Slack 경계**다.

## 1. 매니페스트 — 스코프는 나중에 못 좁힌다

이름 `VIPBot` / `vipbot`. Socket Mode 만 쓰고 `request_url` 이 하나도 없다 =
**새로 여는 인바운드 포트가 없다.**

선언한 스코프 **여섯**뿐이다.

| | 왜 |
|---|---|
| `app_mentions:read` | 채널에서 호출을 받는다. 채널 대화는 안 보인다 |
| `chat:write` | DM 과 채널 안내를 보낸다 |
| `im:history` | DM 으로 온 질문을 받는다(`message.im` 의 전제) |
| `im:write` | 요청자와의 DM 을 연다 |
| `users:read` · `users:read.email` | Slack 사용자 ↔ 사번 매핑(`identity.ensure`). 권한 판정의 입구 |

이벤트는 `app_mention` · `message.im` 둘.

**안 넣은 것**(시험이 고정): `channels:history` · `groups:history`(수집) ·
`channels:join`(자동 참여) · `channels:manage` · `files:read`/`files:write`(첨부) ·
`canvases:write`/`canvases:read`(요약 검토) · `commands` · `reactions:*` ·
`message.channels` · `channel_created` · 사용자 토큰 전부.

스코프를 넉넉히 받아 두지 않는 이유는 취향이 아니다. **설치된 뒤에는 좁히기
어렵다** — 워크스페이스마다 다시 승인을 받아야 하고, 그 사이 토큰은 넓은 권한을
들고 돌아다닌다.

## 2. 쓰는 길에 닿지 않는다 (계약 시험)

「부르지 않겠다」 는 약속은 다음 수정에서 죽는다. 그래서 둘을 파일로 잠갔다.

1. **import 전체를 화이트리스트로.** `vipbot_delivery.py` 가 들여올 수 있는 것은
   `__future__` · `collections.abc` · `dataclasses` · `logging` · `tybot.bot_access`
   뿐이다. writer · attachment_writer · backfill · revision_store · files ·
   canvas · store · ingest_ack · dm_inbox … 를 들여오면 **그 자리에서 깨진다**
2. **Slack 호출 이름을 둘로.** `conversations_open` · `chat_postMessage` 뿐이다.
   `conversations_join` · `conversations_history` · `files_upload` · `canvases_*` 는
   이름만 나와도 깨진다

금지 목록이 **실제로 있는 모듈을 가리키는지**도 시험한다 — 오타 난 금지 목록은
아무것도 막지 않으면서 막고 있다고 믿게 한다.

기존 TYBot 코드가 이 모듈을 부르지 않는 것도 고정했다. 별도 앱이므로, 한
프로세스에서 두 앱의 토큰이 섞이면 안 된다.

## 3. 상세는 DM 으로만

채널에서 불러도 답은 채널에 안 남는다. 남기면 **그 방에 있는 모두가 요청자
권한으로 만든 답**을 보게 된다(원칙 3).

| 상황 | 채널 | DM |
|---|---|---|
| 정상 | 고정 안내 한 줄 | 상세 답변 |
| 거절 | **아무것도 안 남는다** | `bot_access.DENY_MESSAGE`(사유 없음) |
| 판정 실패 | 안 남는다 | 안 보낸다 |
| DM 실패 | **안 남는다 — 폴백 없음** | — |

세 가지를 구조로 박았다.

- **채널로 나가는 글은 `CHANNEL_NOTICES` 안에 있어야 한다.** 밖의 글을 채널로
  보내려 하면 `ChannelLeak` 으로 터진다 — 「실수로 섞여 들어가는」 자리를 없앴다
- **DM 실패는 채널 폴백이 아니다.** 못 보낸 답을 방에 떨어뜨리는 폴백은 이
  모듈이 막으려는 사고 그 자체다
- **연 것이 DM 인지 확인한다.** `conversations.open` 응답의 채널 ID 가 `D` 로
  시작하지 않으면 보내지 않는다. 응답 하나가 어긋났을 뿐인데 상세 답변이 공개
  채널로 나가는 길이다
- **거절은 방에 안 적는다.** 누가 물었고 거절당했는지가 남으면 그 자체가 사람에
  대한 정보다. 사유도 사람에게는 안 밝힌다 — 코드로만 남는다

실제 Slack SDK 는 부르지 않는다. 시험은 호출 이름과 인자를 적는 대역을 쓴다.

## 4. 고정 코드

| outcome | 뜻 |
|---|---|
| `delivered` | DM 성공 + 채널 안내 성공 |
| `permission_denied` | 권한 거절 (reason = `no_identity`·`no_grant`·`expired`…) |
| `access_check_failed` | DB 판정 실패 — **Slack 을 아예 안 부른다** |
| `dm_delivery_failed` | reason = `dm_open_failed`·`dm_channel_invalid`·`dm_post_failed` |
| `channel_notice_failed` | DM 은 갔고 채널 안내만 실패 |

`reason` 은 `DELIVERY_REASONS` 집합으로 검증한다. 집합 밖 값이면 결과 객체를 못
만든다 — Slack 오류 메시지가 그 칸으로 새는 길을 막는다.

결과 객체 칸은 넷뿐이다(`outcome`·`reason`·`dm_sent`·`notice_posted`).
**질문·답변·근거를 넣을 칸이 없다.** 로그도 같다 — 예외는 타입 이름만 남는다.

`notice_posted` 가 참인데 `dm_sent` 가 거짓이면 객체 생성이 거부된다. 채널 안내는
「DM 으로 보냈다」 는 말이고, DM 이 안 갔는데 그 말이 방에 남으면 사람은 오지 않는
DM 을 기다린다.

## 시험

67건, Slack·DB·네트워크 없이 돈다.

**돌연변이 11종 전부 잡힘**(생존 0): DM 실패 시 채널 폴백, 거절을 채널에 게시,
안내 문구가 고정 목록을 벗어남, DM 채널 확인 제거, 판정 실패를 허용으로,
답변·오류 본문을 로그에, `reason` 검증 제거, DM 없이 안내 주장 허용,
매니페스트에 `channels:history` 추가, `message.channels` 구독, writer import.

전체 스위트 **7253 passed · 152 skipped · 0 failed**, ruff 0건
(Codex 의 `vip_access`·`vip_request` 와 함께 돌린 결과다).

## 아직 안 한 것

1. **런타임 연결 없음.** 별도 Socket Mode 프로세스에서 `vip_request` 게이트 →
   공통 답변 → 이 전달 경계로 잇는 것은 Codex 몫이다(BACKLOG B-83 진행 목록)
2. **앱 설치 안 함.** 매니페스트는 파일일 뿐이다. 설치·토큰 발급·허용/거절
   운영 시험은 사람이 한다
3. **DM 호출 경로(`message.im`)의 전달 정책은 따로 없다.** DM 으로 물으면 답도
   DM 이라 전달 분기가 필요 없다 — 필요해지면 그때 만든다
