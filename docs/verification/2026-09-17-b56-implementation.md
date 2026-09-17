# B-56 구현 기록 — 승인 요약을 원문 검색 길잡이로

> 작성: 2026-09-17 (Claude)
> 설계: [`docs/design/summary-review.md`](../design/summary-review.md) §승인 요약을 검색 길잡이로
> 상태: 구현·테스트 완료 · **운영 검증 대기**(DB 스키마 적용·실제 채널 회차)

## 1. 한 줄

승인 요약은 **찾는 데만** 쓰고, 답은 그 요약이 가리킨 원문을 **요청자의 현재
권한으로 다시 열어** 만든다. 열리지 않거나 좌표가 어긋나면 그 항목은 버리고
검색 길잡이에서 제외하고 사유를 남긴다.

## 2. 변경 파일

### 새로 만든 것

| 파일 | 하는 일 |
|---|---|
| `src/tybot/summary_guide.py` | 승인 요약 조회·질문 대조·좌표 재개봉·stale 표시 |
| `tests/test_summary_guide.py` | 길잡이 계약 테스트 16건 |

### 고친 것

| 파일 | 무엇을 |
|---|---|
| `deploy/sql/summary_review_schema.sql` | `evidence_message_ts` · `stale_at` · `stale_reason` · 길잡이 인덱스 |
| `src/tybot/answer.py` | `_with_approved_guide()` — 검색 결과 **뒤에** 길잡이 줄을 더한다 |
| `src/tybot/archive/store.py` | `split_stamp()` · `RawLine.message_ts` · 합칠 때 좌표 있는 줄 우선 |
| `src/tybot/archive/writer.py` | `IncomingMessage.source_ts` · `format_line` · `dedupe_line` |
| `src/tybot/collect.py`, `src/tybot/slack/pilot.py` | 수집 두 경로 모두 Slack `ts` 전달 |
| `src/tybot/summary_review.py` | `SourceLine.message_ts` · `Proposal.evidence_message_ts` · `message_link()` · Canvas 후보별 링크 · `approved()` 가 stale 제외 |
| `tests/test_archive_v2.py`, `tests/test_summary_review*.py` | 좌표 보존·중복·링크 회귀 |

`subbots/hermes` 는 수정하지 않았다. 전문 봇은 우리가 권한 필터한 텍스트만 받고,
길잡이는 그 텍스트를 만드는 **앞단**에서 끝난다.

## 3. DB 마이그레이션

`summary_review_schema.sql` 에 `ALTER TABLE … IF NOT EXISTS` 로 넣었다. 이 파일은
이미 `apply-schema.sh` 목록에 있으므로 별도 절차가 없다.

```bash
sudo /opt/tybot/deploy/apply-schema.sh --dry-run
sudo /opt/tybot/deploy/apply-schema.sh
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_schema_drift.py
```

적용 전에도 봇은 죽지 않는다 — `active_items()` 의 조회가 실패하면 길잡이 없이
예전과 같은 검색으로 내려간다(경고 로그 1줄). 다만 **길잡이는 동작하지 않는다.**

## 4. 흐름

```text
answer()  ─ store.search(query, ctx)                 ← 원문 검색이 먼저
          └ _with_approved_guide(query, hits, ctx)
                └ summary_guide.expand()
                     1) active_items()   approved · not superseded · not stale · hash 있음
                     2) matched()        search_index.score_line() — 검색과 같은 점수
                     3) _line_of()       인용·시각·작성자·전체 원문 hash가 그대로인가
                     4) resolve_refs()   요청자의 **현재** ACL 로 재개봉
                     5) mark_stale()     어긋난 항목만 길잡이에서 제외
```

검색 결과를 밀어내지 않도록 **뒤에** 붙이고, 같은 줄은 한 번만 남긴다. 상한은
`max_hits + 8`.

## 5. 원문 줄 형식 변경

```text
전: > [2026-09-16 10:00] 홍길동: 공정률은 62.5%입니다
후: > [2026-09-16 10:00|1758012345.123456] 홍길동: 공정률은 62.5%입니다
```

- 좌표는 시각 칸 안이다. 본문 뒤에 붙이면 그 글자가 원문 텍스트가 된다(원칙 1).
- 옛 줄(좌표 없음)은 그대로 읽힌다. `RawLine.message_ts` 가 빈 문자열일 뿐이다.
- 중복 판정은 좌표를 떼고 본다(`writer.dedupe_line`). 그러지 않으면 이미 쌓인
  메시지가 좌표만 다른 줄로 한 번 더 쌓이고, 그 중복은 오류로 보이지 않는다.
- `1234567890.123456` 모양이 아닌 값은 적지 않는다. permalink 로 나갈 값이라
  모양이 틀리면 열리지 않는 링크가 출처가 된다.

## 6. 운영에서 확인할 것

- [ ] 스키마 적용 후 `check_schema_drift.py` 가 0으로 끝나는가
- [ ] 새로 수집된 줄에 좌표가 붙는가 (`grep -c '|1' <채널>/raw/<오늘>.md`)
- [ ] Canvas 후보별 「이 근거 메시지 열기」 링크가 그 메시지를 여는가
- [ ] 승인 뒤, 승인 전에는 못 찾던 표현으로 물었을 때 원문이 근거로 붙는가
- [ ] 그 답변의 출처가 **문서·Slack 링크**이고 검토 Canvas 가 아닌가
- [ ] 원문을 지운(또는 채널을 나간) 뒤 같은 질문에 내용이 나오지 않는가
- [ ] `stale_at` 이 찍힌 항목이 길잡이와 기존 승인 요약에서 제외되는가

## 7. 하지 않은 것

- **승인 문장을 `raw_line` 색인에 넣지 않았다.** 넣는 순간 파생 요약이 원문 검색
  결과로 섞이고, 그 뒤로는 어느 줄이 사람이 한 말인지 구별할 수 없다.
- **stale 항목을 자동으로 새 후보로 복제하지 않는다.** 근거가 사라졌거나 바뀐
  과거 문장을 다시 승인 대상으로 올리면 잘못된 원문을 재확인시키게 된다. 지금은
  길잡이와 기존 승인 요약에서 제외하고 `stale_reason`으로 남긴다. 이후 같은 사실이
  새 원문으로 다시 수집되면 정상 후보 생성 경로를 탄다.
- **실시간(live) 조회 경로에는 길잡이를 붙이지 않았다.** 그쪽은 아직 아카이브에
  없는 대화를 보는 자리라 승인 요약이 가리킬 좌표가 없다.
- **붙인 자리는 `AnswerEngine.answer()` 하나다.** 구체 사실 질문이 근거를 모으는
  자리이고, 그 근거가 그대로 전문 봇에게 간다. `summarize()`·`advise()` 와 전문 봇이
  스스로 부르는 `ToolBox._search()` 는 그대로 뒀다 — 요약·권고는 기간으로 범위를
  잡지 한 문장을 찾는 일이 아니고, 도구 검색까지 넓히면 같은 승인 항목이 한 질문에
  두 번 근거를 밀어 넣는다. 필요해지면 그때 측정하고 붙인다.
