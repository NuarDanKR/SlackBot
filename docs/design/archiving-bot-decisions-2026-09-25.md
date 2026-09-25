# Archiving Bot 착수 전 결정 — 2026-09-25 오너 확정

> 상태: **확정.** 아래는 결정이지 제안이 아니다
> 선행: [Archiving Bot 분리](archiving-bot-separation-2026-09-23.md) ·
> [저장 구조 실측·권고](../verification/2026-09-23-archive-layout-benchmark.md)(구조 2)
> 구현: `deploy/sql/archiving_schema.sql` · `src/tybot/archive/archiving_state.py`
> 시험: `tests/test_archiving_schema.py`(31) · `tests/test_archiving_state.py`(46)
> 읽는 사람: Archiving Bot 을 만드는 쪽, 서버 구성을 정하는 쪽(Codex), PF Hermes 개발자

## 0. 지금 하지 않는 것 — **먼저 읽는다**

- **현재 writer 를 끄지 않는다.** TYBot 수집은 그대로 돈다
- **변환 결과를 운영 검색에 연결하지 않는다.** 격리된 import 경로에 둔다
- **새 인터넷 인바운드 포트를 열지 않는다**
- **GCP → DMZ 실시간 전송 경로를 만들지 않는다**

이번 작업의 산출물은 **스키마·상태 전이·회귀시험**까지다.

---

## 1. PF GCP Hermes — 병행 기간에 실시간으로 당기지 않는다

| | |
|---|---|
| 대화 전송 | **하지 않는다.** PF GCP 대화를 TY DMZ 로 실시간으로 보내지 않는다 |
| PF Git archive | 계속 운영한다 |
| 최종 이관 | full commit SHA 와 snapshot 을 고정한다 |
| 변환 | `scripts/archive_layout_convert.py` 를 확장해 **구조 2** 로 일괄 변환한다 |
| 네트워크 | 새 인바운드 포트 없음, GCP→DMZ 실시간 경로 없음 |

실시간 전송을 안 하기로 한 것이 중요하다. 실시간 경로를 만들면 **두 곳에서 같은
대화가 들어오는 구간**이 생기고, 그 구간의 중복·누락은 오류를 내지 않는다.
일괄 변환은 시작과 끝이 있어서 대조할 수 있다.

---

## 2. DM 과 봇 대화 — 사람끼리의 DM 은 안 건드린다

| | |
|---|---|
| 사용자 간 DM | **수집하지 않는다** |
| Archiving 앱 권한 | `im:history` 를 **추가하지 않는다** |
| 봇↔사용자 대화 | 각 봇의 QALog/ConversationAudit 에 기록한다 |
| 그 기록의 자리 | `archive/workspaces` **밖**. 검색·요약·답변 근거에서 제외 |
| 봇 DM 첨부 | 명시적 아카이브 등록 요청이 없으면 **그 요청에서만** 쓴다 |
| 보존 기간 | DB 정책값. **운영값 확정 전 production 전환을 막는다** |

이 결정이 앞선 「DM 은 개인 작업공간」(CLAUDE.md, 2026-09-18)을 좁힌다. 봇과의
대화는 남지만 **근거가 아니라 감사 기록**이고, 사람끼리의 DM 은 아예 안 본다.

`im:history` 를 안 붙이는 것이 이 결정의 집행 수단이다. 권한이 없으면 정책이
느슨해져도 읽을 수 없다 — 「안 읽기로 했다」 보다 「못 읽는다」 가 강하다.

### 구현

- 표 `bot_conversation_audit` — 이름에 `audit` 을 넣었다. 다음 사람이 이 표를 보고
  「대화가 있네, 검색에 넣자」 고 하지 않도록 이름이 용도를 말해야 한다
- 표 `archive_retention_policy` — `retention_days` 가 **nullable** 이다.
  `NULL`은 아직 정하지 않은 값이고, 운영값은 **1일 이상**만 허용한다. `0`을
  즉시 삭제로 해석하면 기록이 생성되자마자 사라져 감사 계약이 성립하지 않는다
- `production_blockers()` — 정책값이 비어 있으면 전환을 막는다.
  안 막으면 기본값이 **「영구 보관」** 이 된다. 개인 대화를 영구 보관하는 것은
  아무도 결정한 적이 없는데 그냥 그렇게 된다

---

## 3. 수정·삭제 — 버리지 않고 쌓는다

| | |
|---|---|
| create/change/delete | **append-only 로 보존** |
| 연결 | `message_ts` 기준 revision 으로 앞뒤를 잇는다 |
| 일반 검색 | **최신 비삭제 revision 만** |
| 과거·삭제 revision | 감사 조회에서만 |
| PII·법적 삭제 | 본문을 남기지 않고 **좌표·hash·사유 tombstone** 만 |

### 지금 코드는 반대로 한다

`archiving_bot.ingest_event` 가 `subtype not in (None, "file_share")` 로
**`message_changed`·`message_deleted` 를 통째로 버린다.** 결과는 두 가지다 —
사람이 고친 문장은 아카이브에 안 들어오고, 지운 문장은 남는다.

틀린 숫자를 고쳐도 봇은 영원히 옛 숫자를 근거로 답한다. 절대 원칙 7(숫자가
엇갈리면 두 시점 함께 표기)이 작동할 **재료 자체가 없다.**

### 원칙 1 과 어떻게 같이 서는가

「원문은 안 고친다」 와 「사람이 고친 게 원문이다」 는 부딪힌다. 둘을 함께 지키는
길은 하나뿐이다 — **고친 것을 새 revision 으로 쌓고, 검색은 최신만 본다.**
앞의 revision 은 한 글자도 안 바뀐다.

### 구현

- 표 `archive_message_revision` — 키가 `(workspace, channel_id, message_ts, revision_no)`.
  덮어쓰는 경로가 없다
- 뷰 `archive_message_current` — 최신 revision 을 고른 뒤 `delete`·`redact`를
  **뷰 자체에서 제외한다.** 호출자가 필터를 빼먹어도 지워진 메시지는 나오지 않는다
- 제약 `..._first_is_create` — 첫 revision 은 반드시 `create`. `change` 로 시작하면
  원본이 없다는 뜻이고, 그때 무엇이 무엇으로 바뀌었는지 말할 수 없다
- 제약 `..._redact_is_bare` — `redact` 는 `body_sha256` 을 **비운다.**
  짧은 본문은 사전 대입으로 해시에서 되찾히므로, 해시를 남기면 「본문을 남기지
  않는다」 를 지킨 것이 아니다

---

## 4. 첨부 — 별도 정본, 덮어쓰지 않음

| | |
|---|---|
| raw 에 넣는 것 | `file_id` · `message_ts` · 링크 **만** |
| 원본·변환 MD | 별도 정본으로 저장 |
| 경로 | `writer.channel_dir` 의 `<channel-id>__<slug>` 를 **재사용** |
| revision | source SHA + 변환기 이름/버전 + 설정/스키마의 결정적 hash |
| 재변환 | 이전 revision 을 **덮지 않는다** |
| 과거 raw | 수정하지 않는다. 파일럿부터 정본을 **소급 생성** |
| legacy 중복 | mapping 이 확정되면 정본을 근거로 쓰고 legacy 는 제외 |

덮지 않는 이유: 옛 변환본을 근거로 인용한 답변이 **이미 나가 있을 수 있다.**
덮으면 사람이 출처를 눌렀을 때 인용된 문장이 없다.

경로를 재사용하는 이유: 새 이름 규칙을 만들면 같은 채널이 두 이름으로 존재하게
되고, 둘을 잇는 코드가 어딘가에 생긴다. 그 코드가 틀리는 날 첨부가 사라진다.

### 구현

- `attachment_revision()` — 네 재료의 결정적 hash. 같은 입력·같은 변환기면 같은
  값이 나와 **재변환이 멱등**하고, 변환기를 고치면 자동으로 다른 revision 이 된다
- 표 `archive_attachment_revision` — 키에 `revision` 이 들어 있어 덮을 수 없다.
  `superseded` 는 **상태**이지 삭제가 아니다
- 네 재료를 열로 보관한다. 없으면 나중에 그 revision 을 재현할 수 없다
- `is_backfill` · `legacy_doc_path` — 소급 생성분과 legacy 중복 제외에 쓴다

---

## 5. 콘솔·DB — 운영 손잡이는 콘솔에

| | |
|---|---|
| 채널별 mode | `off` / `shadow` / `active` / `paused` 를 **DB** 에서 |
| 콘솔에서 관리 | 첨부 분리, edit/delete, ACK, pilot, writer owner |
| env/secret store | Slack token, DB bootstrap DSN, 암호키 |
| Archiver DB role | **설정 조회 + 상태 기록만.** 전용 최소권한 |
| 설정 변경 | append-only audit |

기능 스위치는 `(name, scope, scope_key)`로 식별한다. `scope`는 `global`·
`workspace`·`channel` 중 하나이고, 전역이 아니면 대상 key가 반드시 있어야 한다.
그래야 같은 기능을 전체에는 끄고 파일럿 workspace에만 켤 수 있다.

ENV 로 두면 채널을 늘릴 때마다 SSH 가 필요하고, 그건 **쓸 수 있는 사람이 한 명**
이라는 뜻이다. 실제로 그래서 「대기 31건·처리 0건」 이 났다(CLAUDE.md).

### `tybot_archiver` 역할이 **받지 못하는** 것

| 무엇 | 왜 |
|---|---|
| 어디에도 `DELETE` | 아카이빙 봇은 지우는 일을 하지 않는다. 권한이 있으면 언젠가 쓴다 |
| 설정 표의 `UPDATE` | 봇이 자기 모드를 바꿀 수 있으면 **shadow 가 안전장치가 아니게 된다** |
| 감사 표의 `UPDATE` | 고쳐지는 감사는 감사가 아니다 |
| 설정 변경 감사의 `INSERT` | 설정을 바꾸지 못하는 봇이 변경 기록을 만들 이유도 없다 |
| 시크릿 표 일체 | 절대 원칙 6 |

---

## 6. ACK 와 인수

| | |
|---|---|
| 상태 | `received` · `raw_written` · `attachment_pending` · `ready` · `partial` · `refused` · `failed` |
| 분리 | 메시지 readiness 와 첨부 readiness를 **따로** 든다 |
| 금지 | attachment ready 전에 「검색 가능」·「변환 완료」 라고 말하지 않는다 |
| 인수 | 채널별 `writer_owner` 와 cutover watermark. 두 writer 의 동시 쓰기를 막는다 |
| PII 거부 | 본문 저장 없이 **좌표·hash·reason_code** 만 |

메시지와 첨부를 따로 드는 이유: 본문은 즉시 쓰이고 첨부 변환은 큐를 지난다.
한 칸으로 합치면 느린 쪽에 맞춰 거짓말하게 된다 — 본문은 이미 있는데 「아직」
이라고 하거나, 첨부가 아직인데 「됐다」 고 한다.

### 구현

- 제약 `..._ready_needs_attachments` — 표가 막는다. 코드 규칙으로만 두면 한 경로가
  빠지고 **그 경로만** 거짓말한다
- `plan_ingest_change()` — 같은 것을 **말하기 전에** 막는다. 표는 저장될 때만 본다
- `searchable_claim()` — 사람에게 할 말을 한 자리에서 만든다. 호출부마다 쓰면 한
  군데가 「올렸습니다」 를 「검색됩니다」 로 적고, 그게 제일 안 들킨다
- `off → active` 경로가 **없다.** 그림자를 건너뛰면 「누락 0 · 권한 유출 0 ·
  첨부 손실 0」 을 확인할 기회가 사라진다. 급할 때 건너뛰고 싶어지는 단계다
- `active` 로 갈 때 `writer_owner` 와 `cutover_ts` 가 **같이** 움직인다. 따로 두면
  「active 인데 주인은 master」 가 만들어지고, 그 상태에서 둘이 다 쓴다
- `paused`에서는 어느 writer도 운영 원문을 쓰지 않는다. 재개할 때 이전 owner를
  보고 `active` 또는 `shadow`로 돌아간다
- `active` 이후 `shadow`로 롤백할 때는 새 역인수 좌표를 받고 운영 owner를
  Master로 돌린다. 좌표 없이 owner만 바꾸는 롤백은 허용하지 않는다
- cutover 는 **뒤로 못 간다.** 되돌리면 이미 넘긴 구간을 두 writer 가 다 썼다고
  생각한다
- 표 `archive_refusal` — 본문·일치 문자열을 안 든다. PII 를 막으려고 만든 표가
  PII 저장소가 되면 안 된다

---

## 7. PF 변환 스크립트 보완 — **운영 이관 전에**

현재 `read_pf` 는 `**시각 · 화자**` 블록을 **전부 사람 raw 로** 처리한다.
봇 대화도, 사람이 쓴 요약도 같은 모양이면 같이 들어온다. 그러면 봇 출력이 원문이
되고 그게 근거가 된다 — 절대 원칙 1 이 막으려던 바로 그것이다.

| # | 할 일 | 왜 |
|---|---|---|
| 1 | `human_raw` · `bot_conversation` · `derived_summary` · `attachment` 분류 | 지금은 넷이 한 덩어리다 |
| 2 | `bot_conversation` 은 raw 가 아니라 **감사 import** 결과로 출력 | §2 의 「봇 대화는 근거가 아니다」 |
| 3 | 작성자 ID 가 없으면 **사람이 승인한** bot speaker mapping 을 입력받는다. 추측 금지 | 추측한 화자는 틀려도 티가 안 난다 |
| 4 | `--channel-map` 으로 실제 channel ID · visibility · state 적용 | 지금은 `legacy-<hash>` 와 `private` 고정 |
| 5 | mapping 이 없거나 모호한 채널은 **운영 import 에서 제외** | ACL 을 추측으로 붙이면 유출이다 |
| 6 | documents/projects 첨부를 `file_id`·provenance 와 함께 별도 정본으로 | §4 |
| 7 | dry-run manifest 와 unknown/partial/ambiguous 보고서 | 무엇을 못 옮겼는지 세어서 보여 준다 |

4·5번이 실측 보고서 §9 의 **P0** 과 같은 항목이다 — 「이름 추정으로 연결하면 ACL 이
틀릴 수 있음」.

---

## 8. 이번에 만든 것

| 무엇 | 자리 |
|---|---|
| 스키마 | `deploy/sql/archiving_schema.sql` (`apply-schema.sh` 목록에 등록) |
| 상태 전이 | `src/tybot/archive/archiving_state.py` |
| 스키마 회귀시험 | `tests/test_archiving_schema.py` — 31건 |
| 상태 전이 회귀시험 | `tests/test_archiving_state.py` — 46건 |

### 왜 스키마와 코드 **둘 다** 에 두는가

스키마의 `CHECK` 는 **저장된 값**이 모순되지 않게 한다. 그런데 「`received` 에서
바로 `ready` 로 뛰었다」 는 두 값 다 합법이라 `CHECK` 로는 못 잡는다 — 전이는
**두 상태의 관계**라서 한 행만 보고 판정할 수 없다.

반대로 코드만 두면 psql·콘솔의 다른 화면·나중에 누가 쓸 배치가 그냥 통과한다.

표는 결과를, 코드는 경로를 지킨다.

### 시험이 진짜로 잡는지 확인했다

스키마와 상태 전이에 각각 변이를 넣어 시험이 실패하는지 봤다. 통과만 하는 시험은
지켜 주지 않는다.

| 넣은 변이 | 결과 |
|---|---|
| 첨부 미완에도 `ready` 허용 | 잡힘 |
| archiver 에게 `DELETE` 부여 | 잡힘 |
| revision 키에서 `revision_no` 제거 | 잡힘 |
| `retention_days` 를 `NOT NULL DEFAULT 0` 으로 | 잡힘 |
| 거부 표에 본문 열 추가 | 잡힘 |
| `off → active` 허용 | 잡힘 |
| `redact` 가 본문 해시 유지 | 잡힘 |
| `off` 인데 주인 유지 | **처음엔 못 잡았다** — 시험이 이미 `master` 인 채널에서 출발해 차이가 없었다. 실제 경로(`active → paused → shadow → off`)로 고쳤다 |

---

## 9. 다음 — 구현 순서

1. 콘솔 화면: 채널 mode · writer owner · 기능 스위치 · 보존 정책
   (CLAUDE.md — CLI 만 만들고 「나중에 붙인다」 하지 않는다)
2. `ingest_event` 에 revision 경로를 붙인다(§3). **스위치는 꺼진 채로**
3. ACK 기록을 `archive_ingest_state` 에 붙이고 Master 가 답변 전에 본다(§6)
4. 첨부 정본 reader 완성 → 그 뒤에 분리 스위치를 켠다(§4)
5. PF 변환 스크립트 §7 의 일곱 가지
6. 파일럿 채널 shadow → 대조 → 채널별 인수

**4번 순서가 중요하다.** 읽는 쪽이 붙기 전에 분리를 켜면 그 본문이 조용히
답변에서 빠진다. `production_blockers()` 가 그 조합을 막는다.
