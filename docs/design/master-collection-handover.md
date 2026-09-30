# Master 수집 기능 분리 — 채널별 writer 인수 준비

작성: 2026-09-30
대상: TYBot 운영자, Claude Code / Codex 구현 담당
상태: **준비 코드만 구현됨.** 인수는 한 채널도 하지 않았다

> `handover.plan()`은 아직 호출부가 없는 순수 계획 함수다. 현재 Archiver 런타임은
> shadow root만 열며 `active` 채널을 운영 archive에 쓰지 않는다. 이 상태에서
> 채널을 `active`로 바꾸면 Master가 쓰기를 멈춘 뒤 운영 원문에 공백이 생길 수
> 있다. Archiver의 운영 writer 연결과 서버 검증 전에는 인수하지 않는다.

관련: [Archiver Shadow 콘솔 운영](archiver-shadow-console-operations-2026-09-28.md) ·
[단일 Supervisor·소급 수집](archiver-supervisor-backfill-console-2026-09-29.md) ·
스키마 `deploy/sql/archiving_schema.sql`

## 0. 이 문서가 다루는 것

TYBot(Master)은 지금 답변도 하고 수집도 한다. 수집을 Archiving Bot 으로 옮기는
것이 목표인데, **한 번에 옮기지 않는다.** 채널 하나씩 넘기고, 넘긴 채널에서만
Master 가 손을 뗀다.

이 단계에서 한 일은 **그 인수가 가능해지는 코드와 시험**뿐이다. 운영 수집을
중지하지 않았고, 어떤 채널도 `active` 로 넘기지 않았다.

## 1. 무엇이 문제였나

`archiving_state.owns_write()` 는 2026-09-25 부터 있었다. 「이 채널을 지금 누가
쓰나」 를 판정하는 함수다. 그런데 **부르는 곳이 시험뿐이었다.**

그래서 콘솔에서 채널을 `active` 로 넘겨도 Master 의 수집 경로 셋은 표를 보지 않고
계속 썼다. 인수한 순간 두 봇이 같은 채널의 같은 파일에 append 한다. 줄이 섞이고
`doc_count` 갱신이 유실되는데, **어느 쪽도 오류를 내지 않는다.**

규칙이 있고 부르는 곳이 없는 상태는 규칙이 없는 것과 같다. 이번 단계는 그
간격을 메운다.

## 2. 문지기를 어디에 뒀나

`src/tybot/archive/write_owner.py` 하나가 판정한다. 운영 원문에 쓰는 경로를 전수
조사해 셋으로 나눴고, 그 목록을 `tests/test_write_paths_inventory.py` 가 든다.
새 `writer.ingest` 호출부가 생기면 그 시험이 깨지고, 깨진 사람이 분류해야 한다.

### 2.1 문지기를 지나는 곳 — Slack 에서 **새 대화**를 가져온다

| 자리 | 언제 | 인수된 채널에서 |
|---|---|---|
| `slack/pilot.py` `_ingest_live` | 실시간 이벤트 | 조용히 건너뛴다(로그만) |
| `slack/pilot.py` `_ingest_channel` | 사람이 시킨 취합 | **왜 안 했는지 말한다** |
| `collect.py` `collect_workspace` | 정시 잡 | 건너뛰고 `skipped_owner` 로 센다 |
| `scripts/backfill_channel_history.py` `backfill_channel` | 운영자 소급 | 건너뛰고 `refused_owner` 로 센다 |
| `scripts/sync_channel_files.py` `sync_workspace` | 운영자 파일 동기화 | 건너뛰고 사유를 찍는다 |

봇만 막으면 스크립트로 같은 채널에 쓸 수 있다. **그 둘은 같은 아카이브다.**

사람이 직접 시킨 것(취합·스크립트)만 말로 답하는 이유: 조용히 넘어가면 사람은
봇이 고장난 줄 알고 다시 누른다. 실시간은 초당 여러 건이라 말하면 시끄러워진다.

### 2.2 문지기를 안 지나는 곳 — 이미 있는 줄을 채운다

`scripts/convert_staged_attachments.py` · `scripts/drain_conversion_queue.py`

원본 바이트는 이미 `objects/` 에 있고 **Slack 을 부르지 않는다**(시험이 고정한다).
하는 일은 이미 아카이브에 있는 첨부 줄에 변환 텍스트를 채우는 것이다.

여기를 막으면 인수 **전**에 들어온 첨부가 영영 변환 안 된 채 남는다. 그건 중복이
아니라 누락이고, 누락 쪽이 나쁘다. 변환 워커는 어느 봇의 소유도 아니다 —
`tybot-convert` 라는 별도 서비스 계정으로 돈다.

### 2.3 소유권 밖

- `archiving_bot.py` — Archiver 는 자기 root(shadow)에 쓴다. 운영 경로를 열 때는
  `write_owner.archiver_may_write_live()` 를 지나야 한다(**아직 안 열렸다**)
- `archive/migrate.py` — v1 → v2 이행. 채널이 아니라 아카이브 전체를 옮긴다
- `scripts/archive_layout_bench.py` — 실측. 임시 경로에만 쓴다
- `slack/pilot.py` `_ingest_dm` — DM 은 그 사람 한 명의 기록이고 Archiver 가 손대는
  영역이 아니다

### 2.4 답변·조회는 그대로다

인수는 **누가 쓰나**를 정할 뿐 누가 읽나를 정하지 않는다. 답변·실시간 Slack 조회·
검색은 이 판정을 부르지 않고, 읽기 권한은 `access.RequestContext` 가 본다
(절대 원칙 3). `test_master_handover.py` 가 답변 경로에 `write_owner` 가 들어오지
않는 것을 고정한다.

## 3. 둘이 동시에 쓰지 않는 이유

「주인은 하나」 라는 값만으로는 부족하다. **표를 못 읽는 순간**이 오기 때문이다.
그때 두 봇이 각자 「아마 내 것」 이라고 판단하면 둘 다 쓴다.

그래서 모르는 상태의 기본값을 **서로 반대**로 뒀다.

| 상황 | Master | Archiver |
|---|---|---|
| 표에 행이 없다 | **쓴다** | 안 쓴다 |
| DB 를 못 읽는다 | **쓴다**(degraded) | 안 쓴다 |
| 모르는 `mode`/`writer_owner` 값 | **쓴다** | 안 쓴다 |
| `off` · `shadow` | 쓴다 | 안 쓴다(그림자에만) |
| `active` + 전역 스위치 켬 | 안 쓴다 | 쓴다 |
| `active` + 전역 스위치 끔 | 안 쓴다 | **안 쓴다** |
| `paused` | 안 쓴다 | 안 쓴다 |

Master 가 기본값인 이유는 **전환 전 상태가 그것**이기 때문이다. 반대로 두면 DB 가
잠깐 끊긴 사이 운영 수집이 통째로 멈추고, 그 멈춤은 오류로 보이지 않는다.

`active` 인데 전역 스위치가 꺼져 있으면 **아무도 안 쓴다.** 이 조합은 누락이므로
인수 전에 스위치부터 켜야 한다(§5). 그래도 이렇게 두는 이유는, 사고 때 한 번에
내릴 차단기가 있어야 하기 때문이다 — 차단기를 내린 뒤 Master 가 자동으로 되받으면
그 사이 어느 쪽이 무엇을 썼는지 알 수 없게 된다.

시험 `test_write_owner.py::test_no_state_lets_both_bots_write` 가 모드·주인·좌표·
스위치의 **모든 조합**에서 참인 writer 가 둘이 되지 않는 것을 확인한다.

## 4. cutover_ts 는 런타임에서 거르지 않는다

`archive_channel_mode.cutover_ts` 는 **대조용 기록**이다. 메시지마다 ts 로 주인을
가르지 않는다.

가르면 같은 채널에 대해 두 가지 판정 기준(채널 단위·메시지 단위)이 생기고, 경계
에서 어느 쪽도 안 쓰는 구간이 만들어지기 쉽다. 판정은 채널 단위 하나로 두고,
인수 전후에 중복·누락이 있었는지는 좌표를 기준으로 **나중에 대조**한다.

경계를 좁히는 것은 인수 절차(§5)다. 그 절차는 겹치는 구간을 아예 만들지 않고,
**비는 구간**을 만든 뒤 그 범위를 소급으로 메운다.

## 5. 한 채널을 인수하는 절차 — **비우고 넘긴다** (아직 실행하지 않음)

계획 함수: `src/tybot/archive/handover.py` · 시험: `tests/test_handover.py`

### 5.1 왜 바로 안 넘기나

`shadow → active` 로 한 번에 가면, Master 의 판정 캐시(`write_owner.CACHE_SECONDS`,
30초)가 만료되기 전까지 **두 봇이 같은 메시지를 각자 쓴다.**

그 중복은 `writer.ingest()` 가 걸러 준다 — append 전에 그 채널의 `raw/*.md` 를 전부
읽고 `dedupe_line()`(`> [시각] 화자: 본문` 에서 Slack ts 표기를 뗀 값)으로 이미 있는
줄을 뺀다. (`IncomingMessage.dedupe_key` 는 캔버스 스냅샷 전용이다. 사람 메시지에는
붙지 않으므로 여기서 일하는 것은 `dedupe_line` 이다.)

그런데 그 거름망이 성립하려면 조건이 셋이다.

1. **같은 파일에 쓴다** — 두 봇의 archive root 와 채널 디렉터리가 같아야 한다
2. **줄 문자열이 글자 단위로 같다** — 두 봇이 같은 `collect._messages_from` 을 쓰므로
   형식은 같지만, 화자 이름은 **각자의 토큰으로** `users_info` 를 불러 만든다.
   Archiver 앱에 `users:read` 가 없으면 이름 대신 `U…` 가 들어가고 그 줄은 중복으로
   안 잡힌다
3. **동시에 쓰지 않는다** — `writer.ingest()` 가 `archive_write_lock` 안에서 돈다.
   그런데 이 락은 **못 잡으면 경고만 남기고 진행한다**(`lock.archive_write_lock`)

조건이 다 맞아도 겹친 구간은 **경합**이다. 무엇이 언제 쓰였는지 재구성할 수 없고,
틀어졌을 때 범위를 모른다.

### 5.2 그래서 가운데를 비운다

```text
shadow ──pause──▶ paused ──settle(60초)──▶ active ──backfill──▶ 끝
   ▲                  │
   └────rollback──────┘
```

겹침 대신 **빔**이 생긴다. 둘 다 사고지만 성질이 다르다.

| | 겹침 | 빔 |
|---|---|---|
| 언제 생기나 | 경합. 정확한 범위를 모른다 | 사람이 누른 두 시각 사이. **범위를 안다** |
| 고치는 법 | 줄을 찾아 지운다 | 그 범위를 소급으로 읽는다 |
| 쓰는 주체 | 두 봇 | 소급 하나 — **화자 문자열이 저절로 같다** |

범위를 알기 때문에 고칠 수 있다. 그것이 이 순서를 고른 이유다.

### 5.3 비우는 동안에는 그림자도 멈춘다

`channel_membership.is_collectible()` 은 `shadow` 와 `active` 에서만 참이다. 즉
`paused` 동안에는 **Archiver 의 그림자 수집도 멈춘다.** 그 구간은 어디에도 안 남고
Slack 에만 있다.

그래서 `handover.plan()` 은 소급 작업이 만들어지기 전에는 **끝났다고 말하지 않는다**
(`active` 여도 `action == "backfill"` 로 남는다). 소급을 안 걸면 그 구간은 Slack 의
보존 기간이 지나는 날 사라지고, 그때는 아무도 그런 구간이 있었다는 것을 모른다.

### 5.4 순서

> 전제(`handover.Preconditions`): 다섯이 다 참이어야 시작한다.
> `shadow_compared` · `speaker_parity` · `live_switch` · `archiver_joined` ·
> `schema_gate_open`. 하나라도 빠지면 `plan()` 이 이름을 대고 막는다.

1. 같은 기간의 그림자 수집본과 운영 원문을 대조한다(`shadow_compared`)
2. **두 앱이 같은 화자 문자열을 만드는지 확인한다**(`speaker_parity`). 다르면 소급이
   메운 구간에서 같은 사람이 두 이름으로 남는다 — 오류가 아니라 한 사람이 둘로
   보이는 모양이라 눈에 잘 안 띈다
3. 전역 스위치 `archiver_writes_live` 를 켠다(release gate 통과가 선행)
4. 그 채널을 `shadow → paused` 로 내린다. **이 시각의 Slack ts 를 적어 둔다**(`P`)
5. `DRAIN_SETTLE_SECONDS`(60초) 기다린다. Master 의 캐시가 30초이므로 두 배로 둔다 —
   시계가 정확히 맞지 않는다. 덜 기다리고 올리면 비우려던 단계가 겹침이 된다
6. 인수 좌표와 함께 `paused → active` 로 올린다. `writer_owner` 와 `cutover_ts` 는
   모드와 **같은 문장**으로 움직인다(`plan_mode_change`)
7. `handover.backfill_window(P)` 가 주는 범위로 소급을 건다. 시작은 `P` 보다 120초
   앞이고 **끝은 비어 있다**(지금까지)
8. 소급 결과의 `written` 과 `duplicate` 를 본다. 겹쳐 읽은 앞부분이 `duplicate` 로
   세어지면 2번 확인이 실제로 맞았다는 뜻이다 — **여기서 화자 문자열 전제가 사후에
   한 번 더 검증된다**

### 5.5 왜 소급 범위를 넓게 잡나

시작을 `P` 보다 앞에 두는 이유: Master 가 캐시 때문에 `P` 뒤로도 잠깐 더 썼고, 언제
멈췄는지 정확히 모른다. 뒤에서 시작하면 **한 건도 못 찾는 구간**이 남는데 그건 안
보인다. 앞에서 시작하면 이미 쓴 것을 다시 읽지만 `dedupe_line` 이 거른다.

끝을 비우는 이유도 같다. 끝을 박으면 Archiver 가 실시간으로 쓰기 시작한 지점과
사이가 벌어질 수 있고, 그 틈은 아무 오류도 내지 않는다.

`P` 를 못 읽으면 `backfill_window()` 는 범위를 **좁히지 않고** 빈 값 둘을 돌려준다
(= 채널 전체). 좁은 범위로 「메웠다」 고 말하는 것이 못 메운 것보다 나쁘다.

### 5.6 되돌리기 — **두 단계다**

`_MODE_EDGES` 에 `active → shadow` 가 **없다.** 한 번에 넘기면 멈추지 않은 채 주인이
바뀌고, 그건 인수할 때와 똑같은 겹침이다. 길은 하나뿐이다.

```text
active ──(좌표 불필요)──▶ paused ──(새 좌표 필요)──▶ shadow
```

1. **먼저 멈춘다**(`active → paused`). 좌표가 필요 없다 — 급할 때 좌표를 정하느라
   못 멈추면 안 된다. 이 순간 양쪽 다 운영 원문에 안 쓴다
2. **그다음 넘긴다**(`paused → shadow`). 여기서 **새 역인수 좌표**를 받는다. 좌표
   없이 소유권만 바꾸면 그 경계에서 중복·누락이 생기고, 기존 인수 좌표보다 앞선
   좌표도 거절된다(이미 넘긴 구간을 다시 넘기게 된다)
3. 멈춘 구간은 §5.4-7 과 같은 방식으로 소급해 메운다
4. 되돌린 채널은 **처음부터 다시 간다.** `handover.plan()` 이 다시 `pause` 부터
   답한다 — `active` 로 바로 돌아가는 길은 없다

`paused` 는 이전 주인(`archiver`)과 인수 좌표를 **기억한다.** 잊으면 재개할 때 어느
모드로 갈지 판정할 수 없다.

### 5.7 되돌려도 안 되돌아가는 것

롤백은 **앞으로 누가 쓰나**만 되돌린다. 인수된 동안 Archiver 가 운영 원문에 쓴 줄은
그대로 남는다. 지우려면 그 범위를 알아야 하고, 그래서 `cutover_ts` 를 기록한다(§4).

지운다는 결정은 사람이 한다. 코드가 자동으로 지우지 않는다 — 원문을 지우는 일은
되돌릴 수 없고, 롤백의 이유가 「Archiver 가 잘못 썼다」 가 아니라 「속도가 안 난다」
일 수도 있다.

## 6. 이번 단계에서 하지 않은 것

- 운영 수집 중지 — Master 는 모든 채널에서 그대로 쓴다
- 어떤 채널의 `active` 전환, `archiver_writes_live` 켜기
- 스키마 변경 — `archive_channel_mode` 를 그대로 읽기만 한다. release gate 지문
  변화 없음
- 답변·실시간 조회 경로 수정 — 인수는 **누가 쓰나**를 정할 뿐 누가 읽나를 정하지
  않는다. 읽기 권한은 `access.RequestContext` 가 본다(절대 원칙 3).
  `test_master_handover.py::test_reading_paths_do_not_ask_who_owns_the_writer` 가
  답변 경로가 이 판정을 부르지 않는 것을 고정한다

## 7. 남은 것

- **Archiver 쪽 호출부.** `write_owner.archiver_may_write_live()` 는 있지만 아직
  Archiving Bot 이 부르지 않는다. 지금은 shadow 경로에만 쓰므로 필요 없고, 운영
  경로를 열 때 이 함수를 지나게 해야 한다
- **인수 전후 대조 도구.** `cutover_ts` 를 기준으로 두 경로의 같은 구간을 비교해
  중복·누락을 세는 스크립트. 콘솔에서 부를 수 있어야 한다
- **계획 함수를 부르는 곳.** `handover.plan()` 은 다음에 할 일을 값으로 돌려주지만
  아직 콘솔이 부르지 않는다. 콘솔 분리 작업이 끝난 뒤 그 화면이 이 값을 읽어
  버튼을 만들면 된다. 표는 안 바꿔도 된다 — 단계는 모드 하나에서 읽는다
- **화자 문자열 대조.** `Preconditions.speaker_parity` 는 지금 **사람이 확인해 값으로
  넘기는** 항목이다. 두 토큰으로 같은 사용자 몇 명을 조회해 비교하는 진단 스크립트가
  있으면 좋다(운영 토큰이 필요하므로 서버에서 돈다)
- **비운 시간의 출처.** `plan(drained_seconds=...)` 도 지금은 호출부가 넘긴다.
  `archive_config_audit` 의 `paused` 전환 행 시각에서 뽑으면 사람이 안 적어도 된다
- **캐시 만료 대신 알림.** 지금은 30초 만료로 인수를 반영한다. generation 을 보고
  즉시 무효화하면 §5.4-5 의 60초 대기가 짧아진다. 없애지는 못한다 — Archiver 쪽
  반영 지연은 그대로이기 때문이다

---

# 부록. 첨부 경로와 raw 경로의 이름이 다른 건 (이행안, 미실행)

지시대로 **지금 옮기지 않는다.** 필요해질 때 쓸 이행안만 적는다.

## A.1 지금 모양

```text
workspaces/<ws>/channels/<ID>__<이름>/raw/2026-09-30.md        ← 사람 원문
workspaces/<ws>/channels/<ID>/attachments/<file-id>/<rev>.md   ← 첨부 정본
```

한 채널이 `channels/` 아래 **디렉터리 둘**을 가진다. 이름 규칙이 다르기 때문이다.

- raw 는 `<ID>__<이름>` — 사람이 파일 트리를 열었을 때 어느 채널인지 알아보게
- 첨부는 `<ID>` — 채널 이름이 바뀌어도 경로가 안 흔들리게

## A.2 지금 깨진 것은 없다

reader 두 곳이 모두 `*` 글롭을 쓴다.

- `store.ArchiveStore._files()` → `*/channels/*/raw/*.md`
- `attachment_reader.ATTACHMENT_GLOB` → `*/channels/*/attachments/*/*.md`

둘 다 디렉터리 이름을 해석하지 않으므로 **읽기는 정상**이다. 출처 표기도 경로가
아니라 프론트매터(`channel`, `source_label`)에서 만든다. 즉 지금 문제는 기능이
아니라 **사람이 트리를 볼 때의 혼동**이다.

## A.3 옮긴다면 — 바꿀 쪽은 첨부다

raw 를 `<ID>` 로 바꾸면 이미 쌓인 원문 디렉터리를 전부 이름 변경해야 하고, 그것은
답변 근거가 들어 있는 경로다. 첨부를 `<ID>__<이름>` 으로 맞추는 쪽이 건드리는
양이 적다.

바꿔야 하는 곳:

| 무엇 | 자리 |
|---|---|
| 정본 경로 생성 | `attachment_doc.AttachmentDoc.relative_path()` |
| staging·objects 경로 | `files.attachment_storage()` (`archive.parent` 아래 같은 접미사) |
| reader 글롭 | `attachment_reader.ATTACHMENT_GLOB` (이미 `*` 라 그대로 동작) |
| 채널 디렉터리 해석 | `writer.channel_dir()` 의 `<ID>__*` 재사용 규칙을 첨부도 쓰게 |

## A.4 호환을 유지하는 순서

1. **읽기를 먼저 넓힌다.** `attachment_reader` 가 `<ID>` 와 `<ID>__<이름>` 두
   모양을 모두 읽게 한다. 이 상태로 한 배포를 지나 보낸다
2. **쓰기를 새 모양으로** 바꾼다. 기존 파일은 그대로 둔다 — 이 시점에 두 모양이
   공존하고, 1 덕분에 둘 다 읽힌다
3. **옮긴다.** `shadow_root.py` 와 같은 방식으로 계획·복사·해시 검증을 나누고,
   원본은 검증 기간 동안 지우지 않는다. 같은 `file_id` 가 두 경로에 있으면
   내용 해시가 같을 때만 건너뛰고 다르면 거부한다
4. **옛 경로 읽기를 뗀다.** 뗄 때 남은 파일이 0 인지 먼저 센다. 글롭을 떼는 날
   파일은 그대로 있고 근거만 사라진다(`store._files()` 의 v1 경고와 같은 사고)

각 단계 사이에 **배포를 하나씩 끼운다.** 읽기와 쓰기를 한 배포에 같이 바꾸면
롤백할 때 새 모양으로 쓴 파일을 읽을 수 없게 된다.

## A.5 출처 표기는 바뀌지 않는다

출처는 `ArchiveDoc.citation()` 이 만든다(`[조직명]업무명, 📄문서(날짜)`). 조직 이름은
프론트매터의 `channel` 에서, 문서 이름은 **파일 이름**(`2026-09-30.md`)에서 온다 —
디렉터리 이름은 들어가지 않는다. 그래서 A.3~A.4 를 해도 **사람이 보는 출처 문자열은 그대로**
이고, 이미 답변에 나간 출처가 무효가 되지 않는다.

`EvidenceRef`(후속 질문이 다시 여는 원문 좌표)는 경로를 들고 있으므로, A.4-1 의
넓힌 읽기가 배포된 뒤에 옮겨야 한다. 순서를 바꾸면 「방금 그 문서 다시」 가 옮기는
동안 열리지 않는다.
