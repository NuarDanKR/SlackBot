# 일정 알림을 개인 DM으로 전환하는 구현 계약

_결정: 2026-09-01 · 구현 담당: Claude · 검증 담당: Codex_

## 1. 결정과 이유

TYBot은 여러 Slack 워크스페이스에서 동작한다. 워크스페이스의 조직 단위가 일정하지 않다.
일부는 팀 단위이고 일부는 본부 단위이므로, 모든 팀마다 공지 채널을 만들고
`schedule_channel`을 관리하는 방식은 운영 비용이 크다.

자동 일정 알림의 기본 전달 수단을 **개인 DM**으로 전환한다.

- 자동 알림: 개인 DM
- `/일정`: 현재의 수동 조회 기능 유지
- 팀 공지 채널: 필요한 조직만 선택적으로 유지
- Oracle 직접 조회: 금지. 동기화된 PostgreSQL만 사용
- 개인 일정(`Schedule.Person`): 범위 밖. 승인된 팀 일정 폴더만 사용

이 변경은 `schedule_channel`과 기존 채널 공지 데이터를 삭제하는 작업이 아니다. DM 기능을
별도 경로로 추가하고 파일럿이 끝난 뒤 채널 자동 알림의 기본 활성화 여부를 결정한다.

## 2. 핵심 원칙

### 2.1 권한은 추측하지 않는다

DM 수신자는 다음 연결이 모두 확인될 때만 만든다.

```text
승인된 일정 폴더
  -> 폴더 ACL의 조직코드
  -> 재직 중인 employee.org_code
  -> 검증된 user_identity.emp_no
  -> 사용자가 선택한 대표 Slack 워크스페이스와 사용자 ID
```

하나라도 없거나 여러 행으로 모호하면 보내지 않는다. 이름, 이메일 일부, Slack 표시 이름으로
추측하지 않는다. `workspace`와 `user_identity`가 준비되지 않은 사용자는 `no_identity`로 집계만
하고 메시지를 보내지 않는다.

### 2.2 같은 사람에게 한 번만 보낸다

한 사람이 여러 워크스페이스에 가입했더라도 대표 수신 워크스페이스는 하나다. 사용자가
`/일정 알림`을 켠 워크스페이스를 대표값으로 저장한다. 다른 워크스페이스에서 다시 켜면
대표값을 그곳으로 옮기고 이전 경로로는 더 보내지 않는다.

중복 방지 키는 Slack 사용자 ID가 아니라 다음 값이다.

```text
(source_folder_id, date_id, emp_no, reminder_minutes)
```

워크스페이스가 바뀌거나 프로세스가 재시작돼도 같은 알림을 다시 보내지 않는다.

### 2.3 DM은 아카이브에 넣지 않는다

일정 제목과 장소는 MD 아카이브, QA 근거, 애플리케이션 로그에 쓰지 않는다. 발송 테이블에도
본문을 저장하지 않는다. `source_folder_id`, `date_id`, `emp_no`, 상태, 시각, 비민감 오류 코드만
남긴다.

## 3. 폴더 ACL 모델

현재 `schedule_folder.org_code`는 대표 조직 하나만 담는다. 실제 Oracle 폴더 ACL은 하나의
폴더를 여러 조직에 열 수 있다. 예를 들어 같은 폴더가 본사팀과 협력사 조직에 동시에 보일 수
있다. 개인 DM 수신자를 대표 조직 하나로 계산하면 누락 또는 오발송이 생긴다.

다대다 허용 목록을 추가한다.

```sql
CREATE TABLE IF NOT EXISTS schedule_folder_org (
    source_folder_id bigint NOT NULL
        REFERENCES schedule_folder(source_folder_id) ON DELETE RESTRICT,
    org_code         text NOT NULL REFERENCES org_unit(code) ON DELETE RESTRICT,
    enabled          boolean NOT NULL DEFAULT true,
    approved_by      text NOT NULL CHECK (btrim(approved_by) <> ''),
    approved_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_folder_id, org_code)
);
```

manifest의 `folders` 배열은 후보를 제시하는 자료일 뿐이다. 새 조직 ACL을 자동 승인하지 않는다.
관리자가 `schedule_folder`와 `schedule_folder_org`를 승인해야 DM 대상이 된다.

## 4. 사용자 수신 설정

기본값은 미수신이다. 사용자가 Slack에서 명시적으로 켜야 한다. 파일럿에서 검증 후 조직 단위
기본 활성화 정책을 별도로 결정한다.

```sql
-- 이 FK가 선호 행의 사번과 Slack 신원이 실제로 같은 사람임을 보장한다.
ALTER TABLE user_identity
    ADD CONSTRAINT user_identity_workspace_user_emp_key
    UNIQUE (workspace, slack_user, emp_no);

CREATE TABLE IF NOT EXISTS schedule_dm_preference (
    emp_no            text PRIMARY KEY REFERENCES employee(emp_no) ON DELETE RESTRICT,
    workspace         text NOT NULL REFERENCES workspace(key) ON DELETE RESTRICT,
    slack_user        text NOT NULL,
    reminder_minutes  smallint[] NOT NULL DEFAULT ARRAY[30]::smallint[],
    enabled           boolean NOT NULL DEFAULT true,
    updated_by        text NOT NULL CHECK (btrim(updated_by) <> ''),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace, slack_user, emp_no)
        REFERENCES user_identity(workspace, slack_user, emp_no) ON DELETE RESTRICT,
    CONSTRAINT schedule_dm_preference_reminders CHECK (
        reminder_minutes = ARRAY[30]::smallint[]
        OR reminder_minutes = ARRAY[10]::smallint[]
        OR reminder_minutes = ARRAY[30, 10]::smallint[]
    )
);
```

마이그레이션은 기존 제약 존재 여부를 확인해 반복 실행 가능하게 작성한다. `emp_no IS NULL`인
신원은 알림을 켤 수 없다.

## 5. 발송 큐와 이력

채널용 `schedule_delivery`를 변경하지 않는다. 개인 DM은 별도 테이블로 둬 기존 공지 기능의
외래키와 멱등 키를 깨지 않는다.

```sql
CREATE TABLE IF NOT EXISTS schedule_dm_delivery (
    id                bigserial PRIMARY KEY,
    source_folder_id  bigint NOT NULL,
    date_id           bigint NOT NULL,
    emp_no            text NOT NULL REFERENCES employee(emp_no) ON DELETE RESTRICT,
    workspace         text NOT NULL REFERENCES workspace(key) ON DELETE RESTRICT,
    slack_user        text NOT NULL,
    reminder_minutes  smallint NOT NULL CHECK (reminder_minutes IN (10, 30)),
    scheduled_for     timestamptz NOT NULL,
    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'sending', 'retry', 'sent',
                                        'cancelled', 'expired', 'no_identity', 'failed')),
    attempts          integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at   timestamptz,
    locked_at         timestamptz,
    locked_by         text,
    slack_message_ts  text,
    sent_at           timestamptz,
    cancelled_at      timestamptz,
    last_error        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_folder_id, date_id)
        REFERENCES schedule_occurrence(source_folder_id, date_id) ON DELETE RESTRICT,
    UNIQUE (source_folder_id, date_id, emp_no, reminder_minutes)
);
```

`last_error`에는 Slack 오류 코드와 비민감 요약만 저장한다. 제목, 장소, 사용자 이름, Slack
메시지 본문은 금지한다.

## 6. 대상 계산

플래너는 1분마다 PostgreSQL에서 다음 조건을 모두 만족하는 행만 큐에 넣는다.

1. `schedule_occurrence.source_deleted_at IS NULL`
2. `schedule_folder.enabled`
3. `schedule_folder_org.enabled`
4. `employee.active`이고 `employee.org_code = schedule_folder_org.org_code`
5. `schedule_dm_preference.enabled`
6. 선호 행의 `(workspace, slack_user, emp_no)`가 현재 `user_identity`와 일치
7. 시작 시각이 설정한 10분 또는 30분 알림 구간에 들어옴
8. `is_all_day = false`

조직 트리의 부모·자식으로 임의 확장하지 않는다. Oracle 폴더 ACL에서 승인된 정확한 조직코드만
사용한다. 전근 또는 퇴직으로 현재 `employee` 행이 달라지면 아직 보내지 않은 큐는 취소한다.

첫 큐 생성은 `INSERT ... ON CONFLICT`로 멱등하게 처리한다. 같은 일정의 시각 또는 대표
워크스페이스가 바뀌면 `pending`/`retry`/`expired`/`cancelled` 상태의 기존 행만 새
`scheduled_for`, `workspace`, `slack_user`로 갱신하고 `pending`으로 되돌린다. `sent` 행은
수정하거나 다시 보내지 않는다. 여러 워커가 발송할 때는 `FOR UPDATE SKIP LOCKED`를 사용한다.

## 7. Slack 명령과 화면

고령 사용자를 고려해 복잡한 명령 인자를 외우게 하지 않는다.

- `/일정`: 기존 오늘·내일 조회
- `/일정 알림`: 현재 상태와 버튼 표시
- `알림 받기`: 현재 워크스페이스를 대표 수신 위치로 저장, 기본 30분
- `10분 전`, `30분 전`, `둘 다`: segmented controls에 해당하는 Slack 버튼/라디오 UI
- `알림 끄기`: `enabled=false`. 발송 이력은 삭제하지 않음

알림을 켤 때 신원 매핑이 없으면 저장하지 않고 “계정 연결이 필요합니다”라고만 안내한다.
다른 워크스페이스에서 이미 켜져 있으면 대표 위치가 이동한다는 확인 문구를 보여 준다.

DM 예시:

```text
[30분 전] 14:00 주간회의
장소: 본사 3층
그룹웨어 팀 일정 기준입니다. 변경 여부는 원본 일정을 확인해 주세요.
```

제목이나 장소가 비어 있으면 임의로 추론하지 않는다. 장소 줄을 생략하고 제목은 “제목 없음”으로
표시한다.

## 8. 지연, 취소, 재시도

- 예정 발송 시각보다 10분 넘게 늦은 알림은 `expired`로 끝내고 보내지 않는다.
- 봇이 오래 중단됐다가 살아나도 과거 알림을 몰아서 보내지 않는다.
- 일정이 삭제되면 기존 pending/retry 행을 취소한다. 시작 시각이 바뀌면 같은 고유키의
  미발송 행을 새 발송 시각으로 갱신한다.
- 이미 발송된 뒤 일정이 변경·취소된 경우 정정 DM은 파일럿 범위에서 보내지 않는다.
  사용자는 `/일정` 또는 그룹웨어 원본에서 최신 상태를 확인한다.
- Slack 429는 `Retry-After` 뒤 재시도한다.
- 일시 오류는 지수 백오프, 최대 5회다.
- `channel_not_found`, `user_not_found`, `account_inactive` 등 영구 오류는 재시도하지 않는다.
- 발송 성공 후 DB 기록 실패 가능성 때문에 Slack API 호출에는 가능한 경우 멱등 식별자를 사용하고,
  최소한 전송 직전 `sending` 상태와 락을 커밋한다. 재기동 시 stale lock 복구 규칙을 테스트한다.

## 9. 프로세스 구성

`/일정` 요청이나 DM 발송 시 Oracle에 접속하지 않는다.

```text
Oracle -> schedule_export.py -> schedulesync -> PostgreSQL
                                              -> DM planner/sender -> Slack Web API
```

새 모듈은 `src/tybot/schedule_dm.py`에 두고, 기존 `src/tybot/notify.py`의 수신자 매핑,
재시도, 로그 비식별화 패턴을 재사용한다. 1분 주기의 별도 oneshot 서비스와 timer를 권장한다.

- `deploy/tybot-schedule-dm.service`
- `deploy/tybot-schedule-dm.timer`
- `Environment=TYBOT_ENV_FILE=/etc/tybot/tybot.env`
- `User=tybot`
- 인터넷 인바운드 포트 추가 없음

서비스는 `load_workspaces()`로 등록된 워크스페이스별 봇 토큰을 읽고, 큐 행의 `workspace`와
정확히 일치하는 클라이언트로만 DM을 보낸다. DB `workspace`에 없거나 환경변수에 없는 키는
그 행만 실패 처리하고 다른 워크스페이스 발송은 계속한다.

## 10. 구현 순서

1. 반복 실행 가능한 스키마 마이그레이션과 롤백 문서
2. `schedule_folder_org` 승인 데이터 입력 및 콘솔 조회
3. `/일정 알림` 설정 UI와 대표 워크스페이스 저장
4. DM 플래너와 멱등 큐 생성
5. Slack DM 발송, 재시도, 만료 처리
6. systemd service/timer와 설치 스크립트 반영
7. 파일럿은 `tyit`의 테스트 사용자 2~3명만 수신 설정
8. KST 기준 30분/10분 실제 시각 대조 후 범위 확대

기존 `schedule_channel`과 채널 발송 코드는 이 단계에서 삭제하지 않는다.

## 11. 필수 테스트

- 같은 사번이 세 워크스페이스에 있어도 대표 워크스페이스로 한 번만 발송
- 다른 워크스페이스에서 알림을 켜면 대표 위치가 이동
- 신원 매핑 없음, 퇴직자, 다른 조직, 미승인 폴더는 0건
- 한 폴더의 복수 승인 조직은 각각 정확한 재직자에게만 발송
- 같은 스냅샷과 같은 플래너를 여러 번 실행해도 delivery 1행
- 10분/30분/둘 다 설정별 정확한 큐 생성
- 종일 일정은 자동 DM 없음
- 취소·시간 변경·전근 후 pending 큐 취소
- 10분 이상 늦은 큐는 발송하지 않고 `expired`
- Slack 429와 일시 오류 재시도, 영구 오류 종료
- 로그와 DB 오류 열에 제목·장소·사용자 이름이 없음
- `workspace` DB 행과 환경변수 불일치 시 해당 워크스페이스만 실패
- DM 메시지가 MD 아카이브와 QA 검색 근거에 들어가지 않음

## 12. 완료 조건

- `pytest`와 `ruff check src tests scripts` 통과
- 파일럿 사용자가 `/일정 알림`에서 30분 알림을 켜고 끌 수 있음
- 동일 사용자의 멀티 워크스페이스 중복 DM이 없음
- 실제 Oracle 일정 한 건으로 KST 발송 시각을 사람이 대조함
- 제목·장소가 로그, MD, 발송 이력 본문에 남지 않음을 확인함
- 서비스 재시작과 15분 중단 복구 시험에서 과거 알림 폭주가 없음

---

## 13. 구현 현황 (2026-09-01)

| 단계 | 상태 |
|---|---|
| 1. 스키마 마이그레이션·롤백 | **완료** · `deploy/sql/schedule_dm_schema.sql` |
| 2. `schedule_folder_org` 승인 데이터·콘솔 조회 | **미구현** (관리자 입력 + 콘솔 담당) |
| 3. `/일정 알림` 설정 UI | **완료** · `schedule_dm.settings_blocks` + pilot 핸들러 |
| 4. DM 플래너·멱등 큐 | **완료** · `schedule_dm.plan` |
| 5. 발송·재시도·만료 | **완료** · `schedule_dm.send_due` |
| 6. systemd service/timer·install.sh | **완료** · `tybot-schedule-dm.{service,timer}` |
| 7. 파일럿 수신자 2~3명 | **대기** (2번 이후) |
| 8. KST 실시각 대조 | **대기** (사람이 확인) |

테스트 47건(`tests/test_schedule_dm.py`). §11 의 항목을 그대로 고정했다.

### 설계와 다르게 간 곳
- 문서는 `src/tybot/notify.py` 의 재시도·비식별 로깅 패턴 재사용을 전제했지만
  **그 모듈은 존재하지 않는다.** 패턴을 `schedule_dm.py` 에서 처음 정의했다.
- 조회용 연결(`db.connect`)은 autocommit 이고 발송 잡은 트랜잭션이 필요하다.
  `_commit()` 이 그 차이를 흡수한다 — 호출부마다 분기하면 한 곳을 빠뜨렸을 때
  조용히 커밋되지 않는다.

### 남은 위험
- `schedule_folder_org` 가 비어 있으면 **아무에게도 가지 않는다.** 이것이 안전한 기본값이고
  의도한 동작이지만, 승인 입력 전까지 기능이 조용히 0건인 상태로 보인다.
- KST 대조(§12)는 코드로 못 막는다. `schedule_occurrence` 에 데이터가 들어온 뒤
  실제 일정 하나로 30분 전 DM 시각을 사람이 대조해야 한다.

### 운영
```bash
psql -U tybot -d tybot -f deploy/sql/schedule_dm_schema.sql
sudo systemctl enable --now tybot-schedule-dm.timer
journalctl -u tybot-schedule-dm -f
# 큐만 만들어 보기(발송 없음)
sudo -u tybot /opt/tybot/.venv/bin/python -m tybot.schedule_dm --plan-only
```
