# `/일정` 명령 — 인계 문서

_2026-08-31 · 데이터 경로 완성. `/일정` 명령 구현 완료 — 남은 것은 수신기와 KST 대조._

`/일정` 을 만들 에이전트가 먼저 읽는 문서다. **이미 있는 것을 다시 만들지 말 것.**

---

## 0. 지금까지 된 것

```
Oracle(BPROD)                봇 서버                        Slack
V_TYSLACK_SCHEDULE  ──┐
V_TYSLACK_SCHEDULE_   │  schedule_export.py    schedule_occurrence
  FOLDER            ──┴─▶  (JSONL+manifest) ─▶  schedule_delivery  ─▶  공지 채널
                             [완료]              [수신기 미구현]        [미구현]
```

| | 상태 |
|---|---|
| Oracle 뷰 2개 (`V_TYSLACK_SCHEDULE`, `..._FOLDER`) | **완료** · 폴더 269개 조회 확인 |
| `scripts/schedule_export.py` | **완료** · live 7건 / reconcile 28건 실제 추출 확인 |
| `tests/test_schedule_export.py` | **완료** 28건 |
| `deploy/sql/schedule_schema.sql` (테이블 5개) | **완료** · 실제 DB 적용됨 |
| 스냅샷 → PostgreSQL 수신기 | **미구현** |
| 발송 잡 (30분/10분 전 공지) | **미구현** |
| `/일정` 슬래시 명령 | **구현됨** · `src/tybot/schedule.py` + 명령/버튼 · 테스트 46건 |

전송 방식은 2026-08-31에 **방식 A(봇서버가 Oracle 직접 조회)** 로 확정됐다.
SFTP·내부망 배치서버는 쓰지 않는다. `docs/deploy/infra-request-snapshot-push.md` 참고.

---

## 1. 데이터가 어디 있나

`/일정` 은 **Oracle 을 직접 조회하지 않는다.** `schedule_occurrence` 를 읽는다.
이유는 두 가지다.

- 사용자가 명령을 누를 때마다 내부망 DB 를 때리면, 사람 수만큼 조회가 늘고
  내부망 장애가 Slack 응답 실패로 그대로 번진다
- 이미 1분마다 동기화된 사본이 있다. 최신성은 충분하다

```sql
-- 우리 팀 공지 채널에 연결된 폴더의 앞으로 7일 일정
SELECT o.starts_at, o.ends_at, o.subject, o.place, o.is_all_day
  FROM schedule_occurrence o
  JOIN schedule_channel c ON c.source_folder_id = o.source_folder_id
 WHERE c.workspace = $1
   AND c.enabled
   AND o.source_deleted_at IS NULL
   AND o.starts_at < now() + interval '7 days'
   AND o.ends_at   > now()
 ORDER BY o.starts_at;
```

`schedule_occurrence` 필드는 `scripts/schedule_export.py` 의 JSONL 필드와 **이름이 같다**
(`source_folder_id`, `date_id`, `event_id`, `subject`, `place`, `starts_at`, `ends_at`,
`is_all_day`, `is_repeat`, `source_modified_at`). 번역 계층을 만들지 말 것.

---

## 2. 지켜야 할 것

### 권한 — 채널에서 물으면 그 채널이 볼 수 있는 것만

`schedule_channel` 이 권한 경계다. **`schedule_occurrence` 를 직접 훑지 말고 항상
`schedule_channel` 과 조인**한다. 조인을 빼면 다른 팀 일정이 보인다.

DM 에서 `/일정` 을 받으면 그 사람의 사번 → `user_identity` → `employee.org_code` →
`schedule_folder.org_code` 로 좁힌다. **매핑이 없으면 아무것도 보여주지 않는다.**
추측하지 않는다 — 사번을 잘못 맞히면 남의 팀 일정이 보인다.

### 아카이브 금지

일정 제목·장소를 **MD 아카이브에 쓰지 않는다**([CLAUDE.md](../../CLAUDE.md) 원칙 1·5).
봇이 `/일정` 으로 출력한 내용도 아카이브에 남기지 않는다. `schedule_occurrence` 는
답변 근거로 검색되는 테이블이 아니다.

### 로그 금지

`subject`·`place` 를 로그에 남기지 않는다. 추출기와 스키마 주석이 같은 규칙을 쓴다.
남길 것은 건수·`date_id`·`source_folder_id` 다.

### 보존

제목·장소는 일정 종료 **7일 후 NULL** 로 비운다(`details_purged_at`).
그래서 `/일정` 이 과거를 조회하면 제목이 비어 있을 수 있다. **비어 있음을 정상으로
처리**하고 "제목 없음" 같은 문구로 대체한다. 예외를 던지지 말 것.

### 종일 일정

`is_all_day = true` 는 30분/10분 전 알림에서 제외된다(공지가 무의미하므로).
그러나 `/일정` **조회에는 포함**한다. 사용자는 오늘 종일 일정을 알고 싶어 한다.

---

## 3. 명령 설계 제안

기존 `/채널`·`/투표` 와 같은 자리에 붙인다(`src/tybot/slack/`).

| 입력 | 동작 |
|---|---|
| `/일정` | 오늘·내일 |
| `/일정 오늘` · `/일정 내일` · `/일정 이번주` | 해당 기간 |
| `/일정 9월` | 그 달 |

응답은 **ephemeral 기본**이 안전하다. 채널에 뿌리면 일정 목록이 대화에 섞여
아카이브 수집 대상이 될 수 있다. "채널에 공유" 버튼을 따로 두는 편이 낫다.

출력에 `updated_at` 대신 **마지막 동기화 시각**을 붙이면 사용자가 최신성을 판단할 수
있다. `schedule_sync_run` 에서 `mode='live' AND status='applied'` 의 최신 `applied_at`
을 쓴다. **5분을 넘으면 "동기화 지연" 을 함께 표시**한다 — 조용히 낡은 데이터를
보여주는 것이 가장 나쁘다.

---

## 4. 먼저 필요한 것 (의존)

`/일정` 은 `schedule_occurrence` 에 데이터가 있어야 동작한다. 지금은 **비어 있다.**
아래 둘 중 하나가 먼저 끝나야 한다.

1. **스냅샷 수신기** — `schedule_export.py` 가 만든 폴더를 읽어 upsert.
   `src/tybot/orgsync.py` 를 본뜨면 된다(체크섬 검증 → 검사 → 트랜잭션 1개 → 이력).
   **`horizon_start`~`horizon_end` 범위 안에서만** 누락 행을 삭제로 판정한다.
   범위 밖 일정을 지우면 안 된다.
2. 시험용으로 손으로 몇 행 넣기 — 명령 UI 를 먼저 만들 때만.

`schedule_folder` 에 폴더를 등록해야 `schedule_channel` 연결이 가능하다.
manifest 의 `folders` 배열에 `folder_id`·`folder_name`·`org_code`·`org_name` 이 있으니
그것으로 콘솔에서 후보를 제시하면 된다. 예: 전산팀(`ABB155`)에는
654 업무(전산팀) · 9063 근태(전산팀) · 8 회사일정 이 붙는다.

---

## 5. 확인해야 할 것 하나

**시각이 KST 로 맞는지 아직 사람이 확인하지 않았다.** 그룹웨어 원본에 시간대가 없어
추출기가 `+09:00` 을 붙인다. 여기가 틀리면 알림이 9시간 어긋난다.

명령 쪽은 **표시를 항상 KST 로 변환**하고 그것을 테스트로 고정했다
(`test_utc_input_is_displayed_in_kst`). 그래서 남은 위험은 **추출기가 붙이는 오프셋**
하나다. `schedule_occurrence` 에 데이터가 들어오면 실제 일정 하나를 그룹웨어 화면과
대조할 것 — `/일정 오늘` 출력의 시각이 그룹웨어와 같아야 한다.

---

## 6. 관련 파일

| 파일 | |
|---|---|
| `deploy/sql/oracle_tyslack_schedule_view.sql` | Oracle 뷰 2개 + 근거 |
| `scripts/schedule_export.py` | 추출기 (완료) |
| `deploy/sql/schedule_schema.sql` | 받는 쪽 테이블 5개 |
| `docs/deploy/infra-request-snapshot-push.md` | 방식 A 구성·검증 |
| `src/tybot/orgsync.py` | 수신기를 만들 때 본뜰 것 |
| `src/tybot/polls.py` · `poll_view.py` | 명령·모달을 만들 때 본뜰 것 |
