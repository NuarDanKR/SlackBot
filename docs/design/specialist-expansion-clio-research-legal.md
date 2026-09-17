# 전문 봇 확장: Clio·공식정보 조사·법령 준수 분석

> 작성: 2026-09-17  
> 상태: 설계 확정 · 구현 전  
> 관련 백로그: B-38, B-39, B-44, B-52, B-53, B-54, B-55

## 1. 목적

현재 운영 가능한 업무 답변 전문 봇은 사내 문서를 근거로 검색·요약·분석하는 Hermes다.
따라서 다음 요청은 하나의 전문 봇으로 안전하게 처리할 수 없다.

1. "Slack 평가판이 끝나면 어떤 기능이 사라지는가"처럼 최신 외부 사실이 필요한 요청
2. "업로드한 PPT로 발표 스크립트를 작성해 달라"처럼 근거 검색 후 새 산출물을 작성하는 요청
3. "특정 공종의 준수 법령을 알려 달라"처럼 사내 사실과 시점이 있는 공식 법령을 결합하는 요청

목표는 마스터가 직접 업무 답변을 쓰게 만드는 것이 아니다. 마스터는 맥락을 해석하고
작업을 분해하며, 승인된 전문 능력과 도구를 선택하고, 권한·근거·출력을 검증한다.
전문적인 문장은 해당 전문 봇이 작성한다.

## 2. 현재 구조 평가

### 2.1 재사용할 기반

- `SpecialistRequest`와 `AuthorizedEvidence` 공통 계약
- capability 기반 후보 선택과 워크스페이스별 승인·중지
- 전문 봇 호출·오류·지연·사용 근거 감사 기록
- 프롬프트 계약 Git/ZIP 검역과 HTTP v2 격리 런타임 기반
- 마스터가 ACL과 PII를 적용한 뒤 근거를 전달하는 구조
- 전문 봇이 출처 문구를 만들지 않고 마스터가 EvidenceRef로 출처를 붙이는 원칙

### 2.2 구현 전에 메워야 할 간극

- capability와 어댑터 키가 코드 allowlist에 고정되어 있다.
- 사내 아카이브 밖의 공식 웹·법령을 표현하는 근거 타입이 없다.
- 전문 봇별 조회 도구 권한이 아니라 Hermes용 ToolBox에 결합되어 있다.
- `Hermes 검색 -> Clio 작성` 같은 다단계 작업 계약이 없다.
- 일반 답변의 3,000자 상한과 Canvas 산출물 분량 정책이 분리되지 않았다.
- 계약 테스트가 실제 모델 품질·근거 충실도까지 실행하지 않는다.

## 3. 확정하는 역할

| 구성요소 | 책임 | 하지 않는 것 |
|---|---|---|
| 마스터 TYBot | 맥락 해석, 작업 분해, capability 선택, 권한·PII·출처·산출물 검증 | 근거 기반 업무 문장 직접 작성 |
| Hermes | 권한 안의 사내 원문 검색, 사실 추출, 요약·분석 | 외부 웹 조회, 장문 보고서 대필 |
| Clio | 마스터가 승인한 EvidenceRef로 발표문·보고서·Canvas 작성 | 독자 검색, 권한 확대, 출처 생성 |
| 공식정보 조사 봇(가칭 Argos) | 승인된 공식 사이트에서 최신 제품·정책 사실 조사 | 일반 웹 전체 검색, 모델 기억만으로 최신 사실 단정 |
| 법령 준수 봇(가칭 Nomos) | 공식 법령·고시와 승인된 현장 사실을 결합해 준수사항 분석 | 법률 자문 확정, 비공식 블로그를 법적 근거로 사용 |

봇 이름은 등록 시 바꿀 수 있다. 계약에서는 이름이 아니라 capability를 식별자로 사용한다.

## 4. capability 체계

기존 capability에 다음 값을 추가한다.

| capability | 담당 | 대표 산출물 |
|---|---|---|
| `presentation_script` | Clio | 슬라이드별 발표 대본, 예상 질의 |
| `report_generation` | Clio | 정식 보고서, 비교 분석서 |
| `external_product_research` | Argos | 최신 제품 기능·정책 비교 |
| `regulatory_compliance` | Nomos | 적용 후보 법령과 준수 체크리스트 |

`long_form_report`처럼 분량만 나타내는 값을 capability로 사용하지 않는다. 분량과
전달 방식은 `output_policy`이고, capability는 필요한 전문 능력을 나타낸다.

## 5. 전문 봇 manifest v2

새 전문 봇은 코드 상수에 키를 추가하는 대신 관리자 승인 대상 manifest를 제출한다.
아래 필드를 최소 계약으로 한다.

```toml
schema = "tybot-specialist/v2"
key = "clio"
name = "Clio"
version = "1.0.0"
release_type = "prompt-contract"
execution_mode = "prompt"

capabilities = ["presentation_script", "report_generation"]
input_sources = ["archive", "slack_live"]
tool_scopes = []
output_modes = ["canvas"]
max_output_chars = 12000
citation_policy = "master-rendered"
```

외부정보 조사 봇의 예시는 다음과 같다.

```toml
schema = "tybot-specialist/v2"
key = "official-research"
name = "Official Research"
version = "1.0.0"
release_type = "http-service"
execution_mode = "http"

capabilities = ["external_product_research"]
input_sources = ["official_web"]
tool_scopes = ["official_web.search", "official_web.open"]
output_modes = ["thread", "canvas"]
max_output_chars = 3000
citation_policy = "master-rendered"
freshness_days = 7
allowed_domains = ["slack.com"]
```

검증 규칙:

- capability, source, tool scope, output mode는 TYBot이 제공한 열거형만 허용한다.
- `key`, endpoint, socket 경로, 허용 도메인을 전문 봇 출력으로 변경할 수 없다.
- 등록 요청은 개발자가 하고 capability·도구·워크스페이스 승인은 관리자가 한다.
- 버전이나 manifest hash가 승인값과 다르면 호출하지 않는다.
- 임의 인터넷 접근은 manifest의 `allowed_domains`로 대체할 수 없다. 실제 네트워크
  egress와 조회 도구 양쪽에서 제한한다.

## 6. 근거 계약 확장

모든 근거는 공통 헤더와 종류별 좌표를 갖는다.

```python
EvidenceRef = ArchiveEvidenceRef | SlackLiveRef | WebEvidenceRef | LawEvidenceRef
```

공통 필드:

- `evidence_id`: 요청 안에서 변하지 않는 opaque ID
- `source_type`: `archive | slack_live | official_web | law`
- `title`, `retrieved_at`, `content_sha256`
- `authorized_for`: 요청자·워크스페이스·결정 ID에 묶인 권한 증표
- `excerpt`: 전문 봇에 전달할 최소 텍스트

추가 필드:

| 종류 | 필수 좌표 |
|---|---|
| Archive | workspace, channel_id, document path, line/page |
| SlackLive | workspace, channel_id, message timestamp, permalink |
| Web | canonical URL, publisher, published/updated date, retrieval date |
| Law | 법령·고시명, 조문, 공포/시행일, 관할, 공식 URL, retrieval date |

전문 봇은 본문과 `used_evidence_ids`만 반환한다. URL이나 `출처:` 문구를 직접 만들어
반환하지 않는다. 마스터는 사용 ID가 요청에 있던 ID인지 검증하고 사용자용 출처를 붙인다.

외부 자료는 중앙 아카이브의 사람 원문으로 저장하지 않는다. 조회 캐시는 별도 저장소에
본문 hash, URL, 조회일, 만료일과 함께 두며 봇 답변을 검색 근거로 재사용하지 않는다.

## 7. 도구 권한

도구는 전문 봇별로 기본 거부한다.

| scope | 기능 | 허용 대상 |
|---|---|---|
| `archive.search` | 권한 안의 사내 근거 후보 검색 | Hermes |
| `archive.read` | 선택된 원문 구간 읽기 | Hermes |
| `official_web.search` | 허용된 공식 도메인 검색 | Argos |
| `official_web.open` | 검색 결과 공식 페이지 읽기 | Argos |
| `law.search` | 공식 법령·고시 검색 | Nomos |
| `law.open` | 특정 조문과 시행 이력 읽기 | Nomos |
| `artifact.compose` | Canvas용 구조화 문서 생성 | Clio |

전문 봇에는 Slack 토큰, DB 자격, 파일 경로, 원문 저장소 자격을 제공하지 않는다.
모든 도구 호출은 마스터가 요청자 ACL, PII, 도메인, 크기, 시간 제한을 다시 검사한다.

## 8. 처리 흐름

### 8.1 PPT 기반 발표 스크립트

```text
사용자 요청 + 스레드 맥락
  -> 마스터: presentation_script 판정
  -> Hermes: 현재 권한에서 지정 PPT와 관련 원문 검색·읽기
  -> 마스터: EvidenceRef와 coverage 검증
  -> Clio: 근거만 사용해 슬라이드별 발표문 작성
  -> 마스터: 숫자·날짜 원문 대조, used_evidence_ids 검증
  -> Canvas 생성 + 스레드 링크 + 출처
```

Clio 소스를 받을 수 없는 현재에는 등록용 가짜 어댑터나 마스터 대필 폴백을 만들지
않는다. 사용자가 발표문을 요청하면 Clio 준비 전임을 명시하고, 원하면 Hermes가
3,000자 이내 핵심 내용 요약만 제공한다.

Canvas 기본 구조:

1. TYBot 생성 문서 고지
2. 발표 목적·대상·예상 시간
3. 슬라이드별 핵심 메시지와 발표 대본
4. 반드시 원문 확인이 필요한 숫자·날짜
5. 예상 질의와 근거 있는 답변 범위
6. 출처

### 8.2 최신 제품·정책 정보

```text
마스터: external_product_research 판정
  -> 공식정보 조사 봇: 공식 도메인 검색·열람
  -> 마스터: 최신성·공식성·used_evidence_ids 검증
  -> 3,000자 이하는 스레드, 초과하거나 비교표가 필요하면 Canvas
```

- 공식 자료를 찾지 못하면 일반 지식으로 보충하지 않는다.
- 기능 종료·가격·라이선스는 조회 기준일을 본문에 표시한다.
- 서로 다른 공식 페이지가 충돌하면 두 기준일과 적용 조건을 함께 표시한다.
- 사용자 질문에 사내 프로젝트명·사람·계약정보가 포함되어도 외부 검색어에는 보내지 않는다.

### 8.3 공종 관련 준수 법령

```text
마스터: 공종·지역·발주 유형·공사 단계 확인
  -> 누락 시 확인 질문
  -> Hermes: 현장 사실만 근거화
  -> Nomos: 공식 법령·고시 근거 조회
  -> 마스터: 두 근거 집합을 분리 검증
  -> Nomos: 적용 후보·조건·체크리스트 작성
  -> 마스터: 고위험 고지와 출처 부착
```

출력은 반드시 구분한다.

- 확인된 현장 사실
- 적용 가능성이 있는 법령·조문
- 적용 판단에 필요한 추가 사실
- 시점별 신고·교육·검사 체크리스트
- 법무·안전 담당자 확인이 필요한 사항

"준수 완료", "위법", "적법" 같은 결론은 근거와 승인된 판단 절차 없이 단정하지 않는다.

## 9. 다단계 오케스트레이션 계약

자유로운 에이전트 간 대화는 만들지 않는다. 마스터가 승인된 정적 pipeline만 실행한다.

```python
PipelineSpec(
    capability="presentation_script",
    stages=(
        Stage("retrieve", specialist="hermes"),
        Stage("compose", specialist="clio"),
    ),
)
```

규칙:

- 각 stage의 입력·출력 schema와 timeout을 고정한다.
- 다음 stage에는 이전 봇의 자유 텍스트 전체가 아니라 EvidenceRef와 검증된 구조화
  중간 결과만 전달한다.
- `decision_id`, `pipeline_id`, `stage_id`, 전문 봇 버전, 사용 근거 ID를 기록한다.
- stage 실패 시 같은 capability의 승인 후보만 재시도한다.
- Clio 실패를 Hermes 장문 작성이나 마스터 대필로 바꾸지 않는다.

## 10. 분량과 산출물 정책

- 일반 스레드 답변: 출처 제외 최대 25줄, 본문 최대 3,000자
- 일반 Canvas 요약: 기본 3,000자 안에서 표와 항목을 우선 사용
- Clio 정식 산출물: 요청 목적별 별도 상한. manifest 상한을 넘으면 실패 처리
- 분량 숫자만으로 Clio를 선택하지 않는다. 발표문·보고서라는 목적을 마스터가 판단한다.
- 표는 비교·일정·수치처럼 열 구조가 안정적인 경우 사용한다. 긴 서술을 억지로 표에 넣지 않는다.
- 날짜, 금액, 단위, 기간 표기는 한 Canvas 안에서 통일하고 원문 값을 반올림하지 않는다.

## 11. 오류와 사용자 안내

| 상황 | 처리 |
|---|---|
| Clio 미등록 | 준비 전임을 알리고 가능한 핵심 요약 범위를 제시 |
| 공식 페이지 없음 | 공식 근거를 찾지 못했다고 종료 |
| 법령 적용 정보 부족 | 필요한 공종·지역·단계만 확인 질문 |
| 전문 봇 timeout | 같은 capability의 승인 후보 1회 전환 후 명시적 실패 |
| 잘못된 evidence ID | 계약 위반으로 출력 폐기·전문 봇 자동 차단 후보 기록 |
| 외부 도구에 PII 포함 | 호출 전 차단하고 감사 로그에 내용이 아닌 사유 코드만 기록 |
| Canvas 생성 실패 | 본문 전체를 스레드에 붙이지 않고 재시도 가능한 상태 안내 |

## 12. 보안 요구사항

1. 외부 검색에는 사내 원문을 전달하지 않는다.
2. 외부 페이지의 지시문은 데이터로 취급하며 도구·권한 변경 명령으로 해석하지 않는다.
3. HTTP 전문 봇은 B-38의 Unix socket, HMAC, digest 고정, 무시크릿 컨테이너를 따른다.
4. 전문 봇은 다른 전문 봇을 직접 호출하지 않는다.
5. 전문 봇 출력과 Canvas를 아카이브 원문으로 재수집하지 않는다.
6. 외부 조회 URL은 SSRF 방지를 위해 DNS/IP 재검증과 redirect 상한을 적용한다.
7. 법령·제품 정보 캐시는 만료 후 자동 근거로 사용하지 않는다.

## 13. QA 계약

각 전문 봇은 다음 계약 평가를 제공한다.

- 정상 요청 10건 이상
- 범위 밖 요청 거절 5건 이상
- 근거 없음, 부분 근거, 상충 근거
- 숫자·날짜·단위 원문 보존
- prompt injection이 포함된 문서·웹 페이지
- PII가 포함된 검색 질문의 외부 전송 차단
- 만료된 제품 정보와 개정 전 법령
- timeout, 빈 출력, 잘못된 evidence ID
- 비활성화·버전 불일치·workspace 미승인
- Canvas 생성 실패와 재시도

Clio 평가는 발표문·보고서의 문체만 보지 않고 모든 사실 문장이 전달된 EvidenceRef에서
확인되는지를 검사한다. Argos와 Nomos는 공식 출처 비율 100%가 활성화 조건이다.

## 14. 구현 순서

### 단계 A: B-53 공통 기반

1. capability/source/tool/output 열거형과 manifest v2 schema
2. 하드코딩된 전문 봇 키를 승인 레지스트리로 대체
3. 종류별 EvidenceRef와 마스터 출처 렌더러
4. 전문 봇별 tool scope와 감사 로그
5. 정적 pipeline 실행기와 계약 평가 실행기

### 단계 B: B-52 Clio

Clio 소스·계약·배포물이 제공된 뒤에만 시작한다. `Hermes -> Clio` pipeline,
Canvas 작성, 수치 대조 평가를 먼저 staging에서 검증한다.

### 단계 C: B-54 공식정보 조사

공식 웹 조회 도구와 도메인 승인 화면을 먼저 만들고 조사 전문 봇을 연결한다.
Slack 정책 질문을 골든셋으로 사용하되 특정 공급자에 종속된 코드로 만들지 않는다.

### 단계 D: B-39/B-55 법령 준수

B-39에서 공식 법령 조회 도구를 검증한 뒤 Nomos를 연결한다. 사내 현장 사실과 외부
법령을 하나의 검색 요청에 섞지 않고 마스터 pipeline에서 결합한다.

## 15. 완료 기준

- 새 전문 봇이 TYBot 코드에 key를 하드코딩하지 않고 승인·중지·롤백된다.
- 승인하지 않은 capability, source, tool, domain은 호출 전에 거부된다.
- 세 대표 요청이 각각 올바른 pipeline으로 라우팅된다.
- 사용자에게 표시한 모든 출처가 실제 `used_evidence_ids`와 일치한다.
- 전문 봇 장애 시 마스터가 업무 답변을 대신 작성하지 않는다.
- 외부 조회에 사내 원문·PII가 전송되지 않았음을 감사 기록으로 확인할 수 있다.
- 다른 개발자가 템플릿 저장소만으로 계약 검사와 품질 평가를 실행할 수 있다.

