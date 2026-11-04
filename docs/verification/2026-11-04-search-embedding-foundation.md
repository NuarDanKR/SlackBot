# B-12 pgvector 기반 작업 — 스키마·조회 경계

2026-11-04 기준으로 **모델 호출과 답변 배선은 아직 없다.** 이번 단계는 잘못된
활성화가 권한 밖 원문이나 낡은 revision을 검색 결과로 만들지 않게 DB 원장과 조회
경계만 준비한다.

## 구현한 것

- `search_embedding_model_revision`: provider/model/차원/정규화/승인 판을 보존한다.
  기본 상태는 `draft`이며 시크릿은 저장하지 않는다.
- `archive_search_document_revision`: workspace·channel ID·문서 경로·원문 SHA-256·
  청커 판·모델 판을 묶는다. 완성된 revision 하나만 현재 상태가 될 수 있다.
- `archive_search_chunk`: 임베딩과 원문 offset·본문 SHA-256만 저장한다. 질문·답변·
  원문 본문·provider 응답은 저장하지 않는다.
- `tybot.embedding_search.candidates`: 이미 권한을 통과한 workspace와 channel ID를
  필수로 받아 현재·완료·활성 모델 revision만 후보 좌표로 돌려준다.

## 닫혀 있는 것

- 모델 revision 행과 document/chunk 행이 없으므로 배포만으로 검색은 바뀌지 않는다.
- ANN 인덱스는 만들지 않았다. 모델과 차원이 확정되기 전 일반 `vector` 열에 임의
  차원 인덱스를 만들면 다음 모델 판과 섞인다.
- DB 장애는 `None`, 허용 채널 없음은 빈 목록이다. 둘을 같은 0건으로 표시하지 않는다.
- 후보는 사실 근거가 아니다. 호출부는 현재 ACL로 `doc_path`를 다시 열고
  `source_revision`과 offset/hash를 확인한 뒤에만 기존 `SearchHit`으로 바꿀 수 있다.

## DB 불변식

- 저장 벡터 차원은 해당 모델 revision의 선언 차원과 같아야 한다.
- `current` revision은 `ready`, `indexed_at` 존재, 실제 청크 수와 선언 청크 수 일치가
  모두 필요하다.
- PUBLIC 권한은 철회하며 기존 앱 역할 `tyslackai`만 재빌드 가능한 캐시를 관리한다.

## 다음 단계

1. writer를 실제 PostgreSQL 격리 DB에서 trigger·rollback·동시 발행까지 검증한다.
2. 승인된 임베딩 모델 하나를 정하고 같은 실제 질문으로 키워드/혼합 후보를 재생한다.
3. 현재 ACL·원문 revision 재검증을 거친 후보만 기존 검색 점수와 결합한다.
4. 개선율과 오탐·지연·비용을 보고 ANN 인덱스와 운영 활성화 여부를 결정한다.

## 2026-11-04 writer 추가

`tybot.embedding_index_store.EmbeddingIndexWriter`가 청커 결과와 이미 계산된 벡터를
한 트랜잭션으로 발행한다. 이 writer는 모델을 호출하거나 파일을 열지 않는다.

- 원문 SHA-256, 청크의 정확한 원문 slice, 청크별 SHA-256, 유한 벡터와 모델 차원을
  발행 전에 대조한다.
- 문서 좌표별 advisory transaction lock을 잡고, 기존 current revision을 retire한 뒤
  모든 청크가 들어간 새 revision만 `ready/current`로 바꾼다.
- 같은 source/chunker/model revision 재시도는 저장된 좌표와 hash까지 같을 때만 멱등
  성공한다. building/failed 또는 다른 좌표는 덮어쓰지 않는다.
- 청커의 실제 revision 형식 `chunk-v1:max=1200:ov=120`을 저장할 수 있도록 chunker
  revision에만 `=`를 허용했다. 모델 revision 허용 문자는 넓히지 않았다.

단위 계약 시험은 통과했지만 실제 PostgreSQL의 trigger·rollback·동시 발행은 아직
검증하지 않았다. 모델 호출, 검색 결합, 운영 활성화도 여전히 없다.

## 후보 재검증 resolver

`tybot.embedding_resolver.resolve_candidates`는 DB 후보를 근거로 바로 쓰지 않는다.
호출자가 **현재 ACL과 revision을 이미 적용한** source opener를 제공해야 하며, 아래가
전부 같을 때만 해당 원문 조각을 반환한다.

- workspace, channel ID, 상대 doc path
- 전체 source SHA-256
- offset 범위와 해당 조각 SHA-256

권한 거절과 파일 부재는 모두 `source_not_visible`로 합쳐 권한 밖 문서의 존재 여부를
노출하지 않는다. 절대 경로·상위 경로·Windows 경로·NUL·비정규 상대 경로는 opener를
호출하기 전에 거절한다.

`ArchiveStore` adapter는 아직 없다. `visible_docs()`가 적용한 수정·삭제 revision을
보존하는 canonical text 계약 없이 원본 MD 파일을 직접 다시 열면 삭제된 문장이 벡터
근거로 되살아날 수 있기 때문이다. 이 계약과 회귀시험이 다음 선행 작업이다.

### Canonical source 연결 완료

`tybot.archive.embedding_source`가 channel·attachment의 색인 본문과 요청별 opener를
같은 정본 함수로 만든다. 현재 ACL과 revision ledger를 통과한 줄만 사용하며 후보 하나가
첨부 전체를 깨우지 않는다. KST 오늘 자 channel raw와 모든 DM 색인은 아직 제외한다.
정본 판은 청커 판과 합쳐 `source-v1:chunk-v1:max=1200:ov=120`으로 기록한다.

revision ledger는 설정 여부가 아니라 실제 DB 연결과 표 존재까지 확인한다. 이 probe가
실패하면 색인을 시작하지 않는다. channel ID 변경 감지·실제 DB writer 검증·검색 점수
결합은 여전히 남아 있다.

## 정본에서 모델 입력까지의 계획 경계

`tybot.embedding_index_plan.plan_source`는 정본과 writer 사이의 마지막 순수 검증
경계다. channel·attachment만 받고 DM과 알 수 없는 종류는 거절한다. NUL·CRLF가 남은
본문, 정본 SHA 불일치, 비정규 문서 좌표, 청크의 본문·좌표·상한·덮임·복원 불일치도
모델 호출 전에 고정 코드로 거절한다.

통과한 계획만 정본 판·청커 판·모델 revision이 묶인 `DocumentKey`와 청크별 SHA-256을
내보낸다. 원문과 청크 본문은 dataclass 표현에서 제외했다. 이 계층은 모델을 호출하거나
DB를 열지 않으며, 검색 결합이나 운영 활성화를 뜻하지 않는다.
