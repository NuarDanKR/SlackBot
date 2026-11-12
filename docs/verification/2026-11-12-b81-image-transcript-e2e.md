# B-81 후속 — 이미지 설명·OCR 전사 분리와 사진 검색 E2E 운영 검증

- 작성: Claude, 2026-11-12
- 선행: Codex `002998a`. 함께 쓰는 Codex 구성 요소는 다음과 같다. **이번 작업에서 수정하지 않았다.**
  - Google provider
  - Vision 전용 암호화 자격·관리 API
  - systemd 자격 파일
- 관련 문서:
  - [분담](2026-10-27-archiver-image-description-handoff.md)
  - [LOGIN 계정 인계](2026-10-28-vision-runtime-login-handback.md)

## 1. 무엇이 바뀌었나

```text
수집(Archiver/Master)  ─▶ 원본 보관 + 로컬 OCR(kordoc)        ← 늘 먼저, 모델을 기다리지 않음
                                │
승인된 사진만 (정책 ON·clearance·전송 승인·무료 카탈로그)
                                ▼
Vision worker 1회 호출 ─▶ JSON { description, tags, uncertainty, transcript{lines, uncertainty} }
                                ▼
저장 가드 ─▶ archiver_image_description_revision  [AI 이미지 설명·검색용]   (Vision 전용)
         └▶ archiver_image_transcript_revision   [AI 텍스트 인식·검색용]   (Vision 전용)
         └▶ image_search_entry  ← 두 revision 의 안전 판정을 지난 검색 텍스트만 (Master 읽기 전용)
                                ▼
질문 ─▶ image_guide: 투영 낱말 대조 → 투영 PII 재검사 → 원본 존재·해시·사람 업로드 재확인
                   → 현재 ACL(visible_docs)로 그 사진의 원문 줄(로컬 OCR 줄·채널 첨부 줄)을 다시 연다
                                ▼
답변 근거 = 원문 줄. 설명·전사 문장은 근거로 나가지 않는다.
```

| 요구 | 구현 | 시험 |
|---|---|---|
| 1. 로컬 OCR 먼저, 원본 수집 성공 유지 | 수집 경로(`files.py`·`convert.py`·`writer.py`)는 Vision 모듈을 import하지 않는다. 로컬 OCR이 실패해도 원본은 보관된다 | `test_ingest_never_waits_for_or_imports_the_vision_path`, `test_a_failed_local_ocr_still_keeps_the_original` |
| 2. 설명과 OCR 전사를 다른 revision으로 저장 | 프롬프트 판을 `2026-11-12.1`로 올렸다. `transcript` 칸은 별도로 파싱하고 별도 표·라벨로 저장한다. 설명에 전사 8글자 이상이 그대로 옮겨지면 설명을 보류한다(`description_quotes_transcript`) | `test_publish_writes_two_revisions_with_full_provenance`, `test_a_description_that_quotes_the_transcript_is_withheld` |
| 3. 후보 → worker → revision → 검색 → 원문 재개봉 E2E | `image_guide.expand`를 `AnswerEngine._with_image_guide`로 승인 요약 길잡이 바로 뒤에 붙였다 | `test_end_to_end_*` |
| 4. 검색·답변 직전 ACL·PII·원본 재검증 | ACL은 `visible_docs(ctx)`, PII는 `screen_line`·`contains_sensitive`로 다시 본다. 원본은 `load_original`이 경로·symlink·해시·사람 업로드를 확인한다. 가장 새 정본의 해시가 투영과 달라도 뺀다 | `test_a_requester_without_channel_access_gets_nothing` 외 6건 |
| 5. Google 오류·timeout·무료 한도 소진 시 로컬 OCR로 복귀 | 실패하면 revision과 투영이 생기지 않는다. 검색은 로컬 OCR을 그대로 찾고 원본은 남는다 | `test_model_failure_publishes_nothing_and_local_ocr_still_answers`(4종), `test_a_worker_timeout_closes_*` |
| 6. 자동 활성화 금지, 콘솔에서 DB 자격 관리 | 정책 OFF·timer 비활성 기본은 그대로다. 콘솔 「운영 > 이미지 설명」이 Codex 자격 API로 마스크 조회·교체·사용 중지(사유 필수)를 하고, 기존 현황 API로 정책·카탈로그·작업을 본다 | `npx tsc --noEmit` 통과. 화면은 수동 확인 대기 |
| 7. provenance·불확실성 보존 | 두 revision과 투영 모두에 provider/model/prompt 판, 원본 좌표(ws·채널·파일·SHA-256), 불확실성을 남긴다. 전사 revision은 job과 설명 revision에 묶이고 마스킹 수를 기록한다 | `test_uncertainty_is_kept_on_both_revisions`, 투영 시험 |

### 저장 가드의 순서

1. **설명 가드(기존).** 숫자·식별자 모양이 있으면 응답 전체를 막는다. 이때 **전사도 저장하지 않는다.** 규칙을 어긴 응답의 전사를 믿을 근거가 없다.
2. **전사 형식.** 상한은 줄 수 200, 줄 길이 300자, 전체 8000자다. 넘으면 자르지 않고 버린다.
3. **마스킹.** 줄마다 `sensitive_mask`를 거친다(원칙 5).
4. **최종 관문.** 마스킹이 끝난 본문을 `pii_screen.screen_document`로 다시 본다. 등기부·명단처럼 문서 자체가 민감물이면 전사를 저장하지 않는다. 설명은 그대로 저장된다.
5. **비용 가드(기존).** 비용이 확인되지 않거나 0보다 크면 아무것도 저장하지 않고 무료 카탈로그를 차단한다.

## 2. 운영 적용 절차

> 이 절차의 어느 단계도 모델을 켜지 않는다. 정책 ON과 timer 활성화는 4단계에서 **따로** 한다.

1. **배포 후 스키마 적용**
   ```bash
   sudo /opt/tybot/deploy/apply-schema.sh
   ```
   - `archiver_image_transcript_schema.sql`이 Codex 스키마 바로 뒤에 돈다.
   - `pg_bigm`이 없으면 경고를 내고 `pg_trgm`으로 대신 만든다.
2. **드리프트 확인(읽기 전용)**
   ```bash
   sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
     /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_schema_drift.py
   ```
   - 정상인 모습:
     - `archiver_image_transcript_revision`은 Vision 전용이다. `tyslackai`에 권한이 있으면 drift다.
     - `image_search_entry`는 `tyslackai` SELECT만 있다.
3. **콘솔에서 Vision 자격 등록**
   - 「운영 > 이미지 설명 > Vision 전용 API 자격」에서 provider `google`, 키, 사유를 넣는다.
   - 저장 뒤에는 가린 값만 보인다. 키를 넣어도 정책과 카탈로그는 바뀌지 않는다.
4. **활성화(별도 결정·별도 확인)**
   1. 카탈로그에서 무료·Vision·리전·민감도·자격 준비를 검증한다. 이 단계는 Codex 경계다.
   2. 정책 ON은 콘솔 정책 화면에서 사유와 함께 한다.
   3. clearance와 전송 승인을 받는다.
   4. 그 뒤 `tybot-image-description@<ws>.timer`를 사람이 켠다. LOGIN 인계 문서의 `check_vision_runtime.py --require-login`이 `exit 0`이어야 한다.
5. **E2E 확인**
   1. 승인된 사진 1장을 처리한다.
   2. 콘솔 작업 목록에서 상태가 `완료` 또는 `불확실`인지 본다.
   3. 그 사진의 **로컬 OCR에 없는 낱말**(예: 장비 이름)로 질문한다.
   4. 답변 출처가 그 사진의 Slack 첨부 메시지인지 확인한다.
   5. 답변에 `[AI ` 라벨 문장이 근거로 인용되지 않는지 확인한다.
6. **복귀 확인**
   - timer를 끄거나 카탈로그의 `quota_state`가 `exhausted`인 상태에서 같은 질문을 한다.
   - 로컬 OCR에 있는 낱말로는 여전히 찾아져야 한다.

### 되돌리기

- **길잡이만 끄기:** `image_search_entry` 조회가 실패하면 답변은 예전 검색과 같다. 표를 비우거나 권한을 회수하면 된다.
- **원본은 건드리지 않는다:** 이 경로는 아카이브 원문을 쓰지 않는다. 원본과 로컬 OCR은 그대로 남는다.

## 3. 검증 결과 (개발 PC)

- **집중 시험:** `tests/test_image_transcript_e2e.py` 47건과 store 32건이 통과했다.
- **기존 회귀:** 이미지 설명 관련 기존 시험이 모두 통과했다.
- **변이 시험 16건 모두 검출.** 대상 가드는 다음과 같다.
  - 누출 검사, 마스킹, 최종 관문, 상한 자르기, 불확실성 코드 필터
  - 전사 게시, 라벨, UNION
  - 결과의 전사 칸
  - 투영 PII 재검사, 원본 재확인, 가장 새 정본 선택, ACL(`visible_docs`), 파일 좌표, 답변 호출 지점
- **실제 Google 호출과 실DB는 아직 검증하지 않았다.** 이번 시험은 가짜 router와 가짜 원장 손잡이로 돈다.

## 4. 남은 것·판단이 필요한 것

1. **timeout·429의 원장 상태.** 호출 예외면 결과에 provider 영수증이 없다. 그래서 job이 `failed/model_timeout`이 아니라 `unknown/publish_result_unknown`으로 닫힌다.
   - 재전송은 없다.
   - 다만 운영 화면에서 「결과 미확정」으로 보인다.
   - 고치면 비용 가드도 함께 움직인다. 비용이 확인되지 않은 실패를 무료 카탈로그 자동 차단으로 볼지의 문제다. 이 부분은 Codex 소유이므로 Codex 검토로 넘긴다.
2. **AI 전사의 지위.** 지금 AI 전사는 **찾는 데만** 쓰고 인용하지 않는다(원칙 1). 로컬 OCR이 실패해 첨부 정본에 본문이 없는 사진은 「사진이 올라온 자리(채널 첨부 줄)」만 근거가 된다. AI 전사를 로컬 OCR처럼 인용 가능한 기계 추출로 올릴지는 오너 결정 사항이다.
3. **벡터 검색.** 투영은 `vector_state='parked'`로 시작한다. 임베딩 provider 승인(B-12) 전에는 키워드(pg_bigm/trgm)만 쓴다.
4. **콘솔 현황.** 기존 시험이 「현황 조회는 revision 표를 읽지 않는다」는 경계를 지킨다. 그래서 작업별 설명·전사 생성 여부는 화면에 아직 없다. 필요하면 본문 없이 집계만 내는 별도 API를 둔다.
5. **VIPBot.** VIPBot 답변 엔진에는 아직 연결하지 않았다. TYBot `AnswerEngine`에만 붙였다.
6. **평가 세트.** 실제 현장 사진 품질 평가는 운영 승인 뒤 정책 ON 상태에서 해야 한다. 지금은 합성 응답으로 계약만 검증했다.
