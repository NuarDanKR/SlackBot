# 채널×일 다이제스트 — 「어디를 볼지」 를 먼저 고르는 검색 길잡이 (B-93 2단계)

- 상태: 설계 (2026-11-12, Claude)
- 오너 결정(2026-11-12):
  - 요약 단위는 **일**
  - 첫 대상은 **VIPBot 근거 채널**(`bot_source_channel WHERE bot='vipbot' AND removed_at IS NULL`)
- 관련: [B-93](../../BACKLOG.md#b-93) · [summary-review.md](summary-review.md)(B-56 길잡이 원칙)

## 1. 무엇을 풀려는가

봇은 질문 낱말로 원문 줄을 찾는다. 두 가지가 실패한다.

1. **낱말이 문서와 다르다.** 「재무목표」 를 묻는데 문서에는 「경영목표」·「KPI」 로 적혀 있다
2. **창이 좁다.** 질문 낱말을 많이 맞힌 문서 몇 개가 근거 자리를 독차지하면 답이 든 문서가
   밀려난다(2026-11-12 VIPBot 실측, B-93)

B-93 1단계(전수 탐색·문서당 상한)는 2를 줄였다. 이 문서는 1을 다룬다. 하루치 대화와
그날 올라온 첨부를 사람이 쓰는 말로 요약해 두면, 질문이 그 요약과 맞을 때 **그 날짜·그
첨부의 원문**을 열 수 있다.

## 2. 지키는 선 (변경 없음)

| 선 | 근거 |
|---|---|
| 다이제스트 문장은 **찾는 데만** 쓴다. 답변 근거·출처로 나가지 않는다 | 원칙 1, B-56 |
| 근거는 다이제스트가 가리킨 원문 좌표를 **현재 ACL** 로 다시 연 줄이다 | 원칙 3, `visible_docs` |
| 다이제스트는 원문 좌표 집합을 물고 있다. 원문이 바뀌면 `stale` 이고 재생성 대상이다 | 원칙 1 파생 조건 2·5 |
| 한 다이제스트는 **한 채널·하루**만 묶는다. 채널을 섞지 않는다 | 원칙 1 파생 조건 5 |
| PII 는 원문이 이미 마스킹돼 있고, 다이제스트도 저장 직전에 `sensitive_mask` → `pii_screen` 을 거친다 | 원칙 5 |
| 봇 발언은 입력에 넣지 않는다 | 원칙 1 |

B-88 `auto_bot_summary` 와 **다른 것**이다. 그쪽은 검토자가 볼 개별 사실 후보이고, 여기는
검토 없는 **검색 색인**이다. 출처 표시와 표를 섞지 않는다.

## 3. 데이터

```sql
CREATE TABLE channel_day_digest (
    id                text PRIMARY KEY,
    workspace         text NOT NULL,
    channel_id        text NOT NULL,
    day               date NOT NULL,            -- KST 기준 하루
    revision          text NOT NULL,            -- 입력 지문(아래)
    body              text NOT NULL,            -- 검색용 요약. 근거 아님. 상한 2,000자
    terms             text[] NOT NULL,          -- 같은 뜻 다른 말(경영목표·KPI 등). 상한 40
    source_files      jsonb NOT NULL,           -- [{path, sha256, lines}] 일자 원문 MD
    source_attachments jsonb NOT NULL,          -- [{path, file_id, revision, sha256}]
    model             text NOT NULL,
    provider          text NOT NULL,
    prompt_revision   text NOT NULL,
    stale_at          timestamptz,
    stale_reason      text,
    vector_state      text NOT NULL DEFAULT 'parked',
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace, channel_id, day, revision)
);
-- 검색: lower(replace(body || ' ' || array_to_string(terms,' '), ' ', '')) pg_bigm/trgm 색인
-- 현재 판: 같은 (workspace, channel_id, day) 에서 stale 아닌 가장 새 revision
```

- **`revision`** 은 입력 지문이다. 일자 MD 파일 해시, 첨부 revision 목록, 프롬프트 판,
  모델을 합친 값이다. 같은 입력이면 다시 만들지 않는다
- 권한: 쓰기는 다이제스트 배치 역할이 한다. Master(`tyslackai`)는 SELECT 만 갖는다
  (검색에서 읽는다)

## 4. 만들기 (새벽 배치)

1. 대상 채널은 VIPBot 근거 채널이다. 이후 확대는 콘솔 설정으로 한다
2. 채널마다 **하루치**를 모은다.
   - 그날 일자 MD 의 사람 발언 줄
   - 그날 메시지에 붙은 첨부 정본 본문(문서당 앞부분 상한)
3. 입력 지문이 마지막 다이제스트와 같으면 건너뛴다. 바뀐 날만 다시 만든다
4. LLM 한 번 호출로 `{body, terms[]}` 만 받는다. 출력 검사:
   - 길이 상한을 넘으면 버린다(자르지 않는다)
   - 숫자·날짜는 입력에 있는 것만 허용한다(`summary_review.parse_proposals` 의 숫자 대조 재사용)
   - 마스킹 → 최종 관문
5. 저장하고, 원문 지문이 바뀐 이전 판은 `stale` 로 표시한다

비용·분량:
- 채널×일 1회 호출
- 소급(과거 날짜)은 **콘솔에서 채널·기간을 고르고 분량(호출 수·추정 토큰)을 먼저 센 뒤**
  실행한다(CLAUDE.md 운영 원칙)
- 매일 밤에는 전날과 수정된 날만 돈다

## 5. 쓰기 (질문 시)

```text
질문 ─▶ focus_query 낱말 ─▶ channel_day_digest(body+terms) 키워드 대조 (요청자가 볼 수 있는 채널만)
     ─▶ 상위 다이제스트 N 개 → 그 (채널, 날짜) 의 일자 MD 와 source_attachments
     ─▶ 그 문서들 안에서 전수 탐색(문서당 상한) ─▶ 원문 줄을 검색 결과에 더한다
```

- `summary_guide`·`image_guide` 와 같은 자리(`AnswerEngine` 검색 뒤)에 `digest_guide` 로 붙인다
- 다이제스트 문장은 반환 값에 없다. `SearchHit` 은 원문 줄뿐이다
- 열 때 각 원문 파일의 현재 해시를 다이제스트 `source_files` 와 대조한다. 다르면 그 다이제스트를
  `stale` 로 표시하고 이번에는 쓰지 않는다(그날 원문 전체를 열지 않는다)

## 6. 콘솔

「운영 > 검색 다이제스트」:
- 대상 채널(기본: VIPBot 근거 채널)과 채널별 최근 생성일·stale 수·실패 수
- 소급 실행: 채널·기간 선택 → 분량 미리보기 → 실행. 실행자와 사유를 감사 기록에 남긴다
- 다이제스트 본문은 화면에 **보이지 않는다**(검색 색인이지 읽을거리가 아니다). 건수·날짜만 보인다

## 7. 구현 순서 (이어받는 사람용)

> **진행(2026-11-12, Claude):** §7-1 완료 — `deploy/sql/channel_day_digest_schema.sql`
> (apply-schema 등록, 쓰기 그룹 `tybot_digest`·Master SELECT), `src/tybot/channel_digest.py`
> (`collect_channel_days(root, workspace, channel_id)`·`revision`·`digest_id`·`render_input`),
> `tests/test_channel_digest_input.py`.
> §7-2 완료 — `src/tybot/channel_digest_build.py`(`generate`·`check_output`·`save`·
> `current_revisions`), `tests/test_channel_digest_build.py`. 출력 검사는 모양·길이(넘으면
> 버림)·숫자(원문에 있는 것만, `summary_review.NUMBER_RE` 와 같은 규칙)·마스킹→`pii_screen`.
> §7-3 완료 — `src/tybot/channel_digest_batch.py`(`plan`·`run`·`resolve_dsn`),
> `scripts/build_channel_digests.py`(기본 dry-run, `--apply`·`--since/--until`·`--max-calls`),
> `deploy/tybot-channel-digest.service/.timer`(04:10, 설치만·**켜지 않음**, 콘솔 타이머 목록 포함),
> `tests/test_channel_digest_batch.py`. 모델 `claude-haiku-4-5`, 하루 비용 상한
> `TYBOT_DIGEST_DAILY_LIMIT_USD`(기본 $5).
> §7-4 완료 — `src/tybot/digest_guide.py`(`expand`), `AnswerEngine._with_digest_guide`
> (승인 요약 길잡이 뒤·사진 길잡이 앞, 모든 봇 공통·항상 켜짐 — 표가 없거나 DB 실패면 빈 목록),
> `tests/test_digest_guide.py`. 줄 점수에 다이제스트 terms 를 더해 낱말이 달라도 그날 줄을
> 고른다(terms 는 고르는 데만, 답변에 안 실림).
> §7-5 완료 — API `src/tybot/console/channel_digest_routes.py`(`GET /api/channel-digest`,
> `POST …/preview`, `POST …/run`), 화면 `console-web/src/pages/ChannelDigest.tsx`
> (운영 > 검색 다이제스트), `tests/test_channel_digest_console.py`. 실행은 미리보기에서 본
> 호출 수보다 늘었으면 409, 전체 워크스페이스 관리자만, 감사 `channel_digest_backfill`.
> 배치는 콘솔이 별도 프로세스로 띄운다(날마다 커밋하므로 중단돼도 이어서 돌릴 수 있다).
> **웹 화면은 콘솔 개발자 배포가 필요하다.** API 는 TYBot 배포로 들어간다.
> **다음은 §7-6(서버: 스키마·계정·DSN → dry-run → 오너 확인 → 소급·타이머).**
>
> 서버 준비(§7-6 전에, 한 번):
> 1. 스키마 적용 `sudo /opt/tybot/deploy/apply-schema.sh`
> 2. 로그인 계정 — `CREATE ROLE tybot_digest_runtime LOGIN IN ROLE tybot_digest;` 뒤 `\password`
> 3. DSN 파일 `/etc/tybot/tybot-digest-database-url`(tybot 0600),
>    `postgresql://tybot_digest_runtime:<pw>@127.0.0.1:55432/tyslackai` — **TCP 로**(소켓은 peer 인증이라 실패)
> 4. dry-run: `sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env TYBOT_DIGEST_DATABASE_URL_FILE=/etc/tybot/tybot-digest-database-url /opt/tybot/.venv/bin/python /opt/tybot/scripts/build_channel_digests.py --since 2026-01-01`
>
> 구현 중 정한 것:
> - 아카이브 배치가 둘이라 채널 디렉터리가 아니라 **(workspace, channel_id)** 로 모은다 —
>   `workspaces/<ws>/channels/<id>__*/raw`(수집기 v2)와 `<ws>/<id>__*/archive/raw`(Archiver).
>   파일 머리의 workspace·channel_id 가 다르면 넣지 않는다
> - 첨부 날짜는 **메시지 ts 의 KST 날짜**. ts 없는 첨부는 어느 날에도 넣지 않는다
> - Master 는 SELECT 만 가지므로 §5 의 「질문 시 해시가 다르면 stale 표시」 는 **이번 질문에서
>   쓰지 않기만** 하고, `stale_at` 표시는 배치가 한다

1. 스키마 `deploy/sql/channel_day_digest_schema.sql`, 입력 수집과 지문
   (`src/tybot/channel_digest.py`), 순수 함수로 시험
2. 생성: 프롬프트·출력 검사·저장. LLM 은 대역 router 로 시험
3. 배치 스크립트 `scripts/build_channel_digests.py`(`--dry-run` 분량, `--apply`)와
   systemd timer. **설치 시 비활성**
4. `digest_guide.expand` 와 `AnswerEngine` 연결. 시험 대상:
   - ACL
   - stale 처리
   - 원문 해시 불일치
   - 다이제스트 문장이 근거로 나가지 않는지
5. 콘솔 API와 화면(분량 미리보기·소급)
6. 서버 dry-run: VIPBot 근거 채널의 일 수·예상 호출·토큰 → 오너 확인 → 소급
