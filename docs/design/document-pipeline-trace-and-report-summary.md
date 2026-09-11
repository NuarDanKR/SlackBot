# 첨부 문서 처리 추적과 보고서 종합 답변

> 작성: 2026-09-11  
> 구현 담당: Claude  
> 검증 담당: Codex  
> 상태: 구현 계획  
> 관련 문서: [`thread-follow-up-evidence.md`](thread-follow-up-evidence.md)

## 1. 실제 사례

사용자가 주간업무 보고 HWP/HWPX 파일이 올라온 채널에서 다음과 같이 요청했다.

> 여태까지 수집된 주간 보고 회의 관련 내용을 종합해줘

TYBot은 `주간 보고 회의 자체에 해당하는 회의록이나 논의가 없다`고 답했다. 그러나 같은
채널에는 다음과 같은 파일들이 실제로 있었다.

- `[주간업무보고] 2026.09.10_방글라데시 차토그람 하수도.hwp`
- `공사팀 업무보고(2026.06월말 기준)_호남고철2-5.hwp`
- `[주간보고]광명자원회수시설 (26년09월2주차).hwpx`
- `공사팀 업무보고(2026.08월말 기준)_천안성환-평택...`

응답에는 `문서 62건 · 원문 60줄 사용`이라고 표시됐지만 위 HWP/HWPX는 출처에 보이지
않았다. 이 화면만으로는 아래 원인을 구분할 수 없다.

1. Slack 메시지 또는 첨부 목록 자체가 수집되지 않았다.
2. 원본은 보관됐지만 HWP/HWPX 변환이 실패했다.
3. 변환본은 생겼지만 아카이브 원문에 반영되지 않았다.
4. 원문 반영은 됐지만 검색 색인이 갱신되지 않았다.
5. 검색 가능하지만 최근 60줄, 40,000자 근거 예산 또는 랭킹에서 제외됐다.
6. Hermes까지 전달됐지만 보고서들을 종합하지 못했다.
7. 답은 만들었지만 출처 렌더링에서 HWP/HWPX 원본 링크가 빠졌다.

이 문서는 각 단계를 동일한 파일 ID로 추적하고, 여러 보고서를 종합하는 답변 경로를
별도로 설계한다.

## 2. 현재 코드에서 확인된 위험

### 2.1 `converted`는 답변 가능을 의미하지 않는다

`archive/files.py::stage_files()`는 원본을 저장하고 변환에 성공하면 metadata를
`converted`로 기록한 뒤 변환 줄을 호출자에게 반환한다. 실제 아카이브 쓰기는 그 이후
`writer.ingest()`에서 수행된다.

따라서 다음 상태가 가능하다.

```text
metadata.status = converted
extracted.md 존재
아카이브의 [첨부추출:파일명] 줄 = 없음
```

변환 성공과 아카이브 반영 성공을 같은 상태로 보면 콘솔에는 정상으로 보이지만 답변은
파일을 전혀 읽지 못한다.

### 2.2 수동 재변환 후 색인이 뒤처질 수 있다

`convert_staged_attachments.py --apply`는 변환 줄을 아카이브에 추가하지만 색인 재생성은
별도 `tybot-index` 실행에 맡긴다. 색인에 같은 검색어의 오래된 결과가 일부 있으면
`ArchiveStore.search()`가 파일 스캔으로 폴백하지 않을 수 있어 새 변환 줄이 조용히
검색에서 빠질 수 있다.

### 2.3 보고서가 아니라 채널의 최근 줄을 자른다

기간 요약은 채널마다 최근 `max_lines_per_channel=60`줄을 고른다. 첨부 하나가 수백·수천
줄이면 다음 문제가 생긴다.

- 한 보고서의 꼬리 60줄만 남는다.
- 다른 보고서의 첨부 표시와 본문은 모두 밀려난다.
- 질문과 관련된 보고서 수보다 업로드 순서가 결과를 결정한다.
- 근거가 있었어도 해당 HWP/HWPX 원본 링크를 만들 첨부 표시가 선택되지 않는다.

### 2.4 `여태까지`가 7일로 축소될 수 있다

현재 `Intent.days` 기본값은 7이다. `여태까지`, `수집된 전체`, `지금까지`는 별도 기간
표현이 아니므로 planner 또는 정규식 폴백에서 최근 7일로 처리될 수 있다. 사용자의
요청과 실제 조회 범위가 달라진다.

### 2.5 summary가 주제를 보존하지 못할 수 있다

기존 planner 계약은 `terms`를 주로 search/advice에만 요구한다. `주간 보고 회의 관련`처럼
범위 요약 안에 주제가 있어도 summary에서 검색어가 비면 채널 전체 최근 줄을 사용한다.

### 2.6 파일명과 본문 표현이 다르다

파일명에는 `주간업무보고`, `주간보고`, `업무보고`가 있지만 변환 본문에는 `주간 보고
회의`라는 문구가 없을 수 있다. 본문 키워드만 검색하면 보고서 자체를 후보로 찾지 못한다.

## 3. 목표와 비목표

목표:

- Slack 첨부 하나를 `workspace + channel_id + file_id + message_ts`로 전 단계 추적한다.
- 운영자가 실패 지점을 한 번의 진단으로 확인한다.
- `여태까지 수집된 주간 보고` 요청을 전체 기간의 **문서 집합 요약**으로 처리한다.
- 보고서마다 최소 근거 몫을 보장해 업로드 순서와 파일 크기에 좌우되지 않게 한다.
- 누락·실패·제외 문서 수를 답변에 명시해 불완전한 종합을 완전한 것처럼 말하지 않는다.
- TYBot이 ACL과 PII를 적용하고 검증된 텍스트만 Hermes에 전달한다.

비목표:

- HWP/HWPX 변환 엔진 자체를 다시 구현하지 않는다.
- 원본 HWP/HWPX 바이트를 LLM에 직접 전송하지 않는다.
- 승인 요약 문서를 사실 답변의 근거로 사용하지 않는다.
- 누락된 보고서 내용을 파일명으로 추측하지 않는다.
- 문서 변환 오류를 답변 품질 오류로 뭉뚱그리지 않는다.

## 4. 첨부 처리 단계 모델

기존 `metadata.status`는 호환을 위해 유지하고, 진단에서는 아래 단계를 별도로 계산한다.

| 단계 | 성공 판정 | 실패 예시 |
|---|---|---|
| `observed` | Slack file ID와 메시지 시각을 기록 | 이벤트·백필 누락 |
| `stored` | object path, SHA-256, 원본 파일 존재 | 다운로드 권한·토큰 오류 |
| `converted` | `extracted.md` 존재, 추출 줄 수 > 0 | kordoc/LibreOffice 실패 |
| `screened` | 파일 단위 PII 검사 통과 | 등기부등본·주민번호 차단 |
| `archived` | 정확한 file ID에 연결된 추출 줄이 원문 MD에 존재 | writer 실패·연결 유실 |
| `indexed` | 현재 archive content hash가 DB 색인과 일치 | 재색인 미실행·부분 색인 |
| `retrievable` | 현재 RequestContext에서 검색 가능 | ACL·채널 ID 불일치 |
| `selected` | 해당 QA 요청의 evidence refs에 포함 | 랭킹·근거 예산 탈락 |
| `delegated` | Hermes 허용 source ID 목록에 포함 | 전문 봇 입력 한도 탈락 |
| `cited` | 최종 응답 원본 링크에 포함 | 출처 렌더링 누락 |

`converted` 하나만으로 정상 판정을 내리지 않는다. 운영상 답변 가능한 최소 조건은
`archived + retrievable`이며, 특정 질문에 사용됐는지는 `selected`까지 확인해야 한다.

## 5. 추적 식별자와 metadata

첨부 metadata에 다음 선택 필드를 추가한다.

```json
{
  "origin_message_ts": "1789...",
  "origin_thread_ts": "1789...",
  "archive_state": "pending|archived|failed",
  "archive_paths": ["workspaces/tyit/channels/C.../raw/2026-09-11.md"],
  "archive_line_hashes": ["sha256:..."],
  "archived_at": "2026-09-11T...Z",
  "archive_error_code": null
}
```

- 원문 본문과 사용자 이름을 metadata에 복제하지 않는다.
- 기존 metadata에 필드가 없어도 읽을 수 있어야 한다.
- `permalink`가 파일 링크인지 메시지 링크인지 암묵적으로 해석하지 않는다.
- `origin_message_ts`는 Slack 메시지와 QA evidence를 연결하는 좌표다.
- 오류에는 비민감 코드만 저장하고 상세 traceback은 서비스 로그에 남긴다.

`stage_files()`에 문자열 인자를 계속 늘리지 말고 `AttachmentOrigin` dataclass를 전달한다.

```python
@dataclass(frozen=True)
class AttachmentOrigin:
    workspace: str
    channel_id: str
    message_ts: str
    thread_ts: str = ""
```

실시간 수집과 정기 백필이 같은 구조를 사용해야 한다.

## 6. 아카이브 반영 확인

`stage_files()`가 `list[str]`만 반환하면 어떤 줄이 어느 file ID에서 왔는지 writer 이후에
확인할 수 없다. 반환 타입을 아래처럼 구조화한다.

```python
@dataclass
class StagedAttachmentResult:
    file_id: str
    lines: list[str]
    line_hashes: list[str]
    metadata_path: Path
    warnings: list[str]
```

호출부는 모든 메시지를 `writer.ingest()`한 뒤 결과 파일에서 `line_hashes`를 확인한다.
확인된 첨부만 `archive_state=archived`로 갱신한다.

주의 사항:

- `writer.ingest().written == 0`이 곧 실패는 아니다. 이미 같은 줄이 있으면 멱등 성공이다.
- 따라서 작성 건수가 아니라 실제 원문에서 line hash가 존재하는지 확인한다.
- 일부 첨부만 실패해도 다른 첨부의 상태를 잘못 실패로 바꾸지 않는다.
- metadata 갱신 실패는 원문 쓰기를 롤백하지 않는다. 대신 진단에서 `unknown`으로 표시한다.

수동 재변환 경로도 동일한 확인 함수를 사용한다. 온라인 수집과 재변환 스크립트에
서로 다른 판정 로직을 두지 않는다.

## 7. 색인 최신성

### 7.1 문서별 색인 상태

색인에 문서별 manifest를 둔다.

```text
doc_path, archive_content_sha, indexed_at, indexed_line_count
```

진단 시 현재 MD의 hash와 `archive_content_sha`를 비교한다. 행이 일부 있다는 이유로
최신이라고 판정하지 않는다.

### 7.2 답변 시 안전한 폴백

검색 DB 결과가 있더라도 manifest가 낡은 문서는 해당 문서만 파일 스캔하여 결과를
병합한다. 전체 아카이브를 매번 스캔할 필요는 없다.

```text
DB 검색 결과 + 색인이 낡은 visible document의 파일 검색 결과
```

두 결과는 원문 ref로 dedupe하고 같은 `score_line()`으로 다시 정렬한다.

### 7.3 자동 재색인

원문 쓰기 성공 후 dirty marker 또는 DB queue를 남기고 `tybot-index`가 처리한다. 즉시
질문이 들어와도 7.2의 파일 폴백으로 보여야 하며, 재색인은 성능 최적화이지 정합성 조건이
되어서는 안 된다.

## 8. 읽기 전용 추적 도구

`scripts/trace_attachment_pipeline.py`를 추가한다.

예시:

```bash
python scripts/trace_attachment_pipeline.py \
  --workspace tyit \
  --channel C0BQUGRHV2A \
  --message-ts 1789094137.625899
```

파일명을 추가 필터로 받을 수 있지만 동일 이름이 여러 개면 자동 선택하지 않는다.

출력 예시:

```text
FILE F123  [주간보고]광명자원회수시설 (26년09월2주차).hwpx
  observed      OK   message=1789...
  stored        OK   sha256=... object=yes
  converted     OK   extracted=384 lines
  screened      OK
  archived      FAIL code=archive-lines-missing
  indexed       N/A  archive prerequisite failed
  retrievable   N/A  RequestContext required
```

요구사항:

- 기본 동작은 파일과 DB를 수정하지 않는 read-only다.
- 파일명과 상태는 출력하지만 추출 본문과 PII OCR 결과는 출력하지 않는다.
- `--user` 또는 테스트용 RequestContext가 있을 때만 `retrievable`을 판정한다.
- `--qa-record`를 주면 selected, delegated, cited 단계까지 확인한다.
- 종료 코드는 모두 정상 0이 아니라 `0=전 단계 정상`, `1=처리 실패`, `2=입력/환경 오류`로
  구분한다.
- 같은 판정 함수를 콘솔 진단 API에서도 재사용할 수 있게 script 안에 로직을 넣지 않는다.

### 8.1 현재 사례 판정 기준

이 도구로 네 HWP/HWPX를 확인하면 원인이 즉시 다음처럼 갈린다.

| 최초 실패 단계 | 판정 |
|---|---|
| `observed` | 수집 이벤트 또는 최근 15건 백필 범위 문제 |
| `stored` | Slack 파일 다운로드 문제 |
| `converted` | HWP/HWPX 변환 문제 |
| `archived` | 변환 결과와 writer 연결 문제 |
| `indexed` | 색인 최신성 문제 |
| `retrievable` | 권한 또는 채널 identity 문제 |
| `selected` | 답변 검색·요약 선택 문제 |
| `delegated` | Hermes 근거 예산 문제 |
| `cited` | 답변 출처 표시 문제 |

## 9. 문서 집합 요약 의도

`여태까지 수집된 주간 보고 회의 관련 내용을 종합해줘`는 일반 최근 요약이 아니라
`document_set_summary`로 처리한다. 외부 intent 종류를 늘리지 않아도 `Intent`에 다음
필드를 둘 수 있다.

```python
time_scope: Literal["default", "bounded", "all"] = "default"
document_query: list[str] = field(default_factory=list)
```

분류 규칙:

- `여태까지`, `지금까지`, `전체 기간`, `수집된 모든` -> `time_scope=all`
- 명시 날짜나 `최근 N일` -> `bounded`
- 기간 언급 없음 -> 기존 기본 기간
- summary에도 `terms` 또는 `document_query`를 반드시 추출
- `주간 보고 회의` -> `주간보고`, `주간업무보고`, `업무보고`, `회의`를 후보로 하되
  동의어 확장은 코드의 검토된 사전에서 수행

LLM이 임의 동의어로 다른 문서 종류를 추가하지 못하게 한다.

## 10. 문서 후보 찾기

후보는 다음 순서로 찾는다.

1. 현재 RequestContext로 visible docs를 제한한다.
2. 채널 질문이면 현재 channel ID로 다시 제한한다.
3. 첨부 파일명과 `[첨부:*]`, `[첨부추출:*]` 표식을 document query로 검색한다.
4. 변환 본문도 topic terms로 검색한다.
5. 동일 file ID의 표식과 추출 줄을 하나의 `DocumentEvidence`로 묶는다.
6. metadata의 현재 상태를 결합한다.

```python
@dataclass
class DocumentEvidence:
    attachment_ref: AttachmentRef
    title: str
    status: str
    source_link: str
    hits: list[SearchHit]
    total_extracted_lines: int
```

파일명 일치는 후보 선정에 쓸 수 있지만 파일명만으로 내용 사실을 만들면 안 된다.

## 11. 보고서별 공정한 근거 선택

채널당 마지막 60줄 방식 대신 문서 단위로 예산을 배분한다.

1. 후보 문서 수를 먼저 확정한다.
2. 각 문서에서 제목·표 머리글·질문 관련 줄·숫자 주변 줄·마지막 합계 줄을 선택한다.
3. 각 문서에 최소 줄 수와 최소 문자 예산을 보장한다.
4. 남은 예산은 관련도에 따라 추가 배분한다.
5. 한 문서가 전체 specialist 입력을 독점하지 못하게 문서별 상한을 둔다.

예시 기본값:

```text
최대 후보 문서        20건
문서별 최소 근거      8줄
문서별 최대 근거      40줄
전체 specialist 입력  adapter 계약 범위 이내
```

고정값은 테스트 후 조정하되 답변에 아래 coverage를 표시한다.

```text
대상 보고서 11건 · 내용 반영 8건 · 변환 실패 2건 · 검수/상태 미확인 1건
```

일부 문서가 빠졌으면 `전체를 종합했다`고 표현하지 않는다.

## 12. 큰 문서 집합과 Hermes

모든 보고서 근거가 specialist 입력 한도 안에 들어오면 한 번에 Hermes에 전달한다. 한도를
넘으면 문서별로 검증 가능한 사실 후보를 추출한 뒤 최종 종합한다.

중간 추출 규칙:

- 결과를 아카이브 또는 장기 기억에 저장하지 않는다.
- 각 사실은 허용된 `EvidenceRef` 하나 이상을 반환해야 한다.
- 금액·날짜·기관명·사람 이름은 원문과 정확히 일치해야 한다.
- 인용문 hash 또는 source ID가 원문에 없으면 해당 사실을 폐기한다.
- 최종 종합에는 검증된 사실과 원문 좌표만 전달한다.
- 문서별 추출 실패는 다른 문서의 내용으로 보충하지 않고 coverage에서 실패로 표시한다.

이는 한 요청 안의 제한된 처리이며 Hermes의 요약을 이후 질문의 원문 근거로 저장하는
구조가 아니다.

## 13. 답변 형식

보고서 종합 답변은 최소한 다음 구조를 가진다.

```text
주요 현황
- ...

공통 쟁점
- ...

현장별 차이 또는 조치사항
- ...

확인 범위
- 대상 11건 / 내용 확인 8건 / 변환 실패 2건 / 미확인 1건
- 확인하지 못한 파일: ...

출처
- 각 보고서 원본 링크
```

- `회의록이 없다`와 `업무보고 문서는 있으나 회의 발언은 없다`를 구분한다.
- 파일명에 `주간보고`가 있고 내용이 변환되지 않았으면 `자료가 없다`고 하지 않는다.
  `파일은 있으나 내용을 확인하지 못했다`고 답한다.
- 사람 의견과 문서 수치는 작성자·문서·기준일을 구분한다.
- 원본 링크는 실제로 내용을 사용했거나 미확인 상태를 알리는 관련 파일만 붙인다.

## 14. 콘솔 연계

기존 수집 > 아카이브 진단에 파일별 최초 실패 단계를 추가한다.

필터:

- 워크스페이스
- 채널
- 형식
- 최초 실패 단계
- 변환 상태
- 업로드 기간

상세 화면:

- 파일명, 원본 링크, 크기, 업로드 시각
- 단계별 상태와 시각
- 공개 가능한 실패 사유
- 추출 줄 수와 아카이브 반영 줄 수
- 색인 최신 여부
- 최근 답변 선택 여부와 QA record 링크

원문 미리보기는 기존 권한 정책을 유지한다. PII 차단 파일은 관리자라도 OCR 추출 본문을
콘솔 API로 제공하지 않는다.

## 15. 관측성

파일 처리 로그는 file ID를 공통 correlation key로 쓴다.

```text
attachment_stage ws=tyit ch=C... file=F... result=converted lines=384
attachment_archive ws=tyit ch=C... file=F... result=archived lines=384
attachment_index ws=tyit ch=C... file=F... result=indexed
answer_document_set qa=... candidates=11 selected=8 failed=2 unknown=1
```

파일명과 본문은 일반 INFO 로그에 반복하지 않는다. 상세 파일명은 권한이 있는 콘솔과
read-only 진단 명령에서만 확인한다.

## 16. 구현 순서

Claude는 현재 진행 중인 스레드 후속 질문 변경을 먼저 끝내고 커밋한 뒤 착수한다. 같은
파일을 동시에 수정하지 않는다.

1. 단계 판정용 모델과 read-only `trace_attachment_pipeline.py` 구현
2. 현재 사례를 재현하는 네 HWP/HWPX fixture 및 단계별 테스트 추가
3. `AttachmentOrigin`과 metadata 선택 필드 추가
4. writer 이후 archive line hash 확인 및 `archive_state` 갱신
5. 문서별 색인 manifest와 stale-document 파일 폴백 구현
6. `time_scope=all`, summary topic/document query 분류 구현
7. `DocumentEvidence` 그룹과 문서별 근거 예산 구현
8. Hermes 대용량 문서 집합 계약 및 coverage 응답 구현
9. 콘솔 진단 API와 화면 연결
10. 운영 서버에서 실제 네 파일을 read-only trace하고 결과를 검증 문서에 기록

## 17. 필수 테스트

### 파이프라인

- Slack 첨부를 받았지만 원본 다운로드가 실패하면 최초 실패 단계가 `stored`다.
- HWP 변환 실패는 `converted`에서 멈추고 archived/indexed를 정상으로 표시하지 않는다.
- 변환 성공 후 writer 실패는 metadata `converted`와 별개로 `archived=failed`다.
- 이미 같은 원문 줄이 있어 writer가 0건을 써도 hash가 있으면 archived 성공이다.
- 수동 재변환도 온라인 수집과 동일한 archive 판정을 사용한다.
- 색인 일부가 존재하더라도 문서 hash가 낡으면 새 줄을 파일에서 찾아 병합한다.

### 기간과 후보

- `여태까지 수집된`은 7일이 아니라 `time_scope=all`이다.
- 채널에서 요청하면 다른 채널의 주간보고가 후보에 들어오지 않는다.
- HWP/HWPX 파일명이 topic과 맞고 본문에는 topic 문구가 없어도 후보가 된다.
- 동일 파일명의 서로 다른 file ID가 합쳐지지 않는다.
- 파일 표식만 있고 추출 본문이 없으면 후보에는 포함하되 내용 미확인으로 센다.

### 근거 예산

- 첫 문서가 수천 줄이어도 뒤의 보고서들이 최소 근거를 받는다.
- 11개 보고서 fixture에서 11개가 모두 coverage에 집계된다.
- 변환 실패 보고서는 Hermes 원문 입력에 들어가지 않는다.
- 입력 한도를 넘으면 생략 건수와 파일을 명시하고 완전한 종합이라고 말하지 않는다.

### 답변과 권한

- `회의록 없음`, `업무보고 있음`, `내용 미확인`을 서로 구분한다.
- Hermes가 사용한 모든 사실에 허용된 source ID가 있다.
- 현재 채널 밖 자료, PII 차단 OCR, 봇의 이전 답변이 Hermes 입력에 없다.
- 최종 링크는 선택된 보고서와 관련 실패 보고서에만 해당한다.
- root workspace 사용자도 채널 질문에서는 현재 채널 자료만 본다.

### 회귀

- 일반 사실 검색, 최근 7일 요약, 후속 질문, Canvas 응답이 유지된다.
- 구형 attachment metadata도 읽힌다.
- `pytest`와 `ruff check src tests scripts`가 통과한다.

## 18. 완료 조건

- 운영 서버의 네 HWP/HWPX 각각에 최초 실패 단계가 확인된다.
- 변환 완료 파일은 아카이브·색인·답변 선택까지 같은 file ID로 추적된다.
- 실제 질문을 다시 했을 때 네 문서가 후보 및 coverage에 표시된다.
- 내용 확인이 가능한 보고서는 각각 최소 한 개 이상의 검증된 근거를 제공한다.
- 확인하지 못한 문서는 사유와 원본 링크가 표시된다.
- 답변이 `자료가 없다`와 `자료는 있으나 읽지 못했다`를 혼동하지 않는다.
- Codex가 단계별 fixture, 권한 누출, specialist 입력, 전체 테스트와 lint를 검증한다.

## 19. 운영 확인 결과 기록

구현 후 실제 서버 확인 결과는 별도 문서
`docs/verification/2026-09-11-weekly-report-pipeline.md`에 남긴다.

다음 값을 파일별로 기록한다.

```text
workspace/channel/file_id/message_ts
stored/converted/screened/archived/indexed/retrievable/selected/delegated/cited
최초 실패 단계와 비민감 오류 코드
재처리 전후 상태
QA record ID
```

파일 본문, OCR 결과, 토큰, 사용자 개인정보는 검증 문서에 넣지 않는다.
