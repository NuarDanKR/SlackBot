# 벡터 정본 창구 — 인계 (B-12)

작성 2026-11-04 · 범위: `src/tybot/archive/embedding_source.py` 와 그 시험.
**모델 호출·DB writer·점수 결합은 손대지 않았다.** Codex 소유 파일
(`embedding_resolver` · `embedding_index_store` · `embedding_search` ·
`search_embedding_schema.sql` 와 그 시험)은 한 줄도 고치지 않았다.

## 한 줄

색인기와 resolver 가 **같은 함수로 만든 같은 글자**를 보게 했다. 창구는
`VisibleSources.open(workspace, channel_id, doc_path)` 하나이고, 그 뒤는 전부
`ArchiveStore` 의 권한 판정과 `revision_reader` 를 지난 뒤의 줄이다.

## 요구별 결과

| # | 요구 | 결과 |
|---|---|---|
| 1 | resolver 가 원본 MD 를 직접 안 연다 | **됨** — resolver 는 `open_visible` 콜러블만 받는다. 그 모듈이 파일을 만지지 않는다는 것을 ast 로 본다 |
| 2 | 수정·삭제된 문장이 안 되살아난다 | **됨** — 정본은 `revision_reader.apply` 를 지난 줄로만 만든다. 줄이 빠지면 `source_revision` 이 바뀌고 옛 벡터는 전부 떨어진다 |
| 3 | ACL 판정 전에 본문·존재를 안 읽는다 | **됨** — 문이 둘이다. 첫 문은 파일을 안 연다(경로 문자열 + 이미 보이는 채널 범위). 없는 경로와 권한 밖 경로가 **같은 `None`** 으로 끝난다 |
| 4 | channel·attachment·DM 구분, DM 은 본인만 | **됨** — `kind` 로 구분하고 DM 은 문이 셋이다(`_files` 범위 · `can_access` · `dm_user` 재확인) |
| 5 | 좌표가 안정적이다 | **됨, 단서 있음** — 아래 ③ |
| 6 | NUL·CRLF·UTF-8·source_revision·offset 기준 명시 | **됨** — 모듈 머리말의 표. offset 은 **Python `str` 인덱스**(코드포인트) |
| 7 | 후보 하나가 첨부 전부를 깨우지 않는다 | **됨** — 첨부 20장 중 후보 1건에 `load_checked` **1회** |
| 8 | 같은 정본을 양쪽이 쓴다 | **됨** — 같은 함수(`canonical_text`)이고, 왕복 시험이 붙들고 있다 |
| 9 | 권한·revision·삭제·첨부 교체 회귀 | **됨** — 넷 다 시험 있음 |
| 10 | 못 하면 handback | 이 문서의 「막히는 지점」 |

시험 **50건**, 돌연변이 **12종 전부 잡힘**, ruff 0건.
전체 6638 passed · 169 skipped · 3 failed(전부터 있던 `route.original_router` 3건,
FastAPI 0.117.1/Starlette 0.48.0 이 안 만드는 속성 — 내 변경과 무관).

## 정본 계약 (요구 6)

| 항목 | 값 |
|---|---|
| 인코딩 | UTF-8. `source_revision` = UTF-8 바이트의 SHA-256 소문자 16진수 |
| 줄바꿈 | `\n` 만. CRLF·단독 CR 은 `\n` 으로 |
| NUL | 제거(`text_normalize.remove_nul`) — PostgreSQL `text` 가 못 받는다 |
| offset | Python `str` 인덱스. **바이트도 자소도 아니다** |
| 채널·DM 한 줄 | `[<표시시각>] <화자>: <본문>\n` |
| 첨부 한 줄 | 본문 그대로 + `\n` |
| 끝 | 줄이 하나라도 있으면 항상 `\n` 으로 끝 |
| 판 | `SOURCE_PLAN = "source-v1"` |

채널 줄에 화자를 싣는 이유: 키워드 점수(`search_index.score_line`)가 이미
`speaker + text` 를 본다. 두 경로가 다른 것을 보면 같은 질문에 다른 순서가 나온다.
첨부에는 **안 싣는다.** 줄마다 파일명을 붙이면 Markdown 표·제목이 깨져
`chunking` 이 경계를 못 본다.

**첨부 정본은 빈 줄을 잃는다** — `attachment_reader.as_archive_doc` 이 빈 줄을
`RawLine` 으로 만들지 않기 때문이다. 표·제목은 남고 문단 구분만 사라진다.

## 좌표 계약 (요구 5)

`(workspace, channel_id, doc_path)` 가 한 벌이고 셋 다 본다.

**문서는 파일 단위다.** `ArchiveStore._docs()` 가 일자 파일을 채널 하나로 합치지만,
합친 문서의 `path` 는 그중 최신 파일이라 날마다 바뀐다. 그걸 좌표로 쓰면 어제
색인한 행을 오늘 못 연다. 그래서 **합쳐서 권한·revision 을 적용한 뒤, 살아남은
줄을 원래 파일별로 되돌려** 정본을 만든다. `doc_path` 는 키워드 색인과 **같은
함수**(`search_index.rel_path`)가 만든다.

## 막히는 지점 — 넘기기 전에 정해야 하는 것

### ① 오늘 자 파일은 벡터가 계속 낡는다 (운영상 가장 큰 것)

`source_revision` 은 **파일 하나의 정본 전체**에 대한 지문이다. 채널 일자 파일은
하루 내내 append 되므로, 메시지 하나가 들어올 때마다 그 파일의 지문이 바뀌고
resolver 는 그 파일의 후보를 **전부** `source_revision_changed` 로 버린다.

재색인 전까지 **오늘 대화는 벡터로 답에 못 들어간다.** 오류도 0건도 아니고,
그냥 조용히 빠진다.

고르는 수는 셋이다.

1. **받아들인다** — 오늘 자료는 키워드 경로와 실시간 조회가 이미 덮는다.
   벡터는 어제까지의 기억으로 쓴다. 가장 단순하고 지금 코드 그대로다
2. **닫힌 날만 색인한다** — 색인 잡이 오늘 자 파일을 건너뛴다. 낭비가 줄지만
   「어제까지」 가 명시적이 된다
3. **청크 단위로만 검증한다** — resolver 가 `source_revision` 을 먼저 보므로
   **Codex 파일을 고쳐야** 한다. 내 범위 밖이고, 고치면 「문서가 통째로 바뀐 것」
   을 못 보게 되므로 권하지 않는다

👉 **오너·Codex 결정 필요.** 권고는 2번.

### ② revision DB 가 없으면 색인을 거부한다 (의도된 동작)

`iter_index_sources` 는 `DATABASE_URL` 이 없으면 `SourceUnavailable` 로 멈춘다.

이유: DB 를 못 보는 동안 `revision_reader` 는 수정·삭제 표시가 붙은 좌표를
**통째로 감춘다**(막는 쪽이 기본값). 그 상태로 색인하면 「그 문장이 원래 없는
문서」 가 정본으로 굳는다.

그 결과 **답변 쪽에도 그림자가 있다** — revision DB 가 죽으면 표시가 붙은
문서의 정본이 짧아져 그 문서의 벡터가 **전부** 떨어진다. 안전하지만, 장애 중에
「벡터 검색이 고장 났다」 로 보인다. 운영 알림에 이 인과를 적어 둬야 한다.

### ③ `channel_id` 가 바뀌면 그 문서는 재색인해야 한다

`_docs()` 는 합칠 때 **진짜 Slack ID 를 만들어 낸 ID(`legacy-…`)보다 앞세우고**,
표시명이 유일하면 별칭으로도 묶는다. 그래서 다음 두 경우 좌표가 바뀐다.

- 마이그레이션으로 진짜 ID 가 뒤늦게 붙은 채널
- `channel_id` 가 아예 없는 v1 원문(표시명이 좌표가 된다) → **채널 이름을 바꾸면** 바뀐다

바뀌면 옛 행은 못 열리고 resolver 가 `source_not_visible` 로 버린다. 막는 쪽이라
새는 일은 없지만, **재색인 전까지 그 채널은 벡터에서 사라진다.** 색인 잡에
「좌표가 바뀐 문서」 를 찾아내는 자리가 필요하다.

### ④ DM 을 색인에 넣으려면 스키마에 칸이 하나 더 있어야 한다

지금 `iter_index_sources` 의 기본값은 **DM 제외**다(`dm_scope=""`).

켜면 `archive_search_document_revision` 에 개인 기록의 좌표가 들어간다. 답할 때
여는 문은 `VisibleSources` 하나뿐이고 그건 제대로 막는다 — 그런데 **표 자체에는
그게 DM 인지 적을 칸이 없다.** `kind` 칸이 없으면 나중에 누가
`SearchScope.channel_ids` 에 DM 채널을 넣어도 SQL 쪽에서 걸러지지 않는다(열리지는
않는다 — 열리는 자리는 창구뿐이다 — 대신 후보만 잔뜩 나오고 전부 버려진다).

👉 **Codex 결정 필요**: `archive_search_document_revision` 에 `kind` 를 더하고
DM 은 아예 별도 `model_revision`/범위로 가르는 편이 낫다. 그 전까지 DM 색인은
켜지 않는 것을 권한다.

### ⑤ `chunker_revision` 에 정본 판도 같이 적어야 한다

스키마에는 정본 판(`source-v1`)을 넣을 칸이 없다. 정본 모양을 바꾸면 본문이
달라져 `source_revision` 이 전부 바뀌므로 **스스로 무효화되기는 한다.** 그래도
「왜 전부 바뀌었나」 를 나중에 읽을 수 있어야 한다.

`_CHUNKER_REVISION_RE` 가 `[A-Za-z0-9._:=-]` 를 받으므로 그대로 들어간다:

```
chunker_revision = "source-v1:chunk-v1:max=1200:ov=120"
```

👉 색인 잡을 쓰는 사람이 이 모양으로 적어 주기 바란다.

### ⑥ `ArchiveStore` 의 비공개 멤버 둘에 기대고 있다

`_visible_channel_docs` 와 `_visible_predicate` 를 쓴다. 공개 `visible_docs()` 를
쓰면 **후보 하나에 첨부 전부가 객체로 올라온다**(요구 7 위반이자 2026-11-02 OOM
과 같은 길). 권한 판정을 베껴 오는 것은 더 나쁘다 — 판정이 두 곳으로 갈리면
갈려도 오류가 안 난다(원칙 3).

👉 `store.py` 는 Codex 도 고치는 파일이라 내가 안 건드렸다. 둘을 **공개 API 로
올리는 것**을 권한다(이름만 바꾸면 되고, 지금 동작은 그대로).

### ⑦ 출처(원칙 2)는 아직 안 붙었다

창구는 **본문**을 돌려준다. 답변에 붙는 출처 문자열은 여전히
`SearchHit.citation()` 이 만든다. 벡터로 찾은 조각을 답에 쓰려면 그 조각이 어느
줄에서 왔는지 `EvidenceRef` 로 옮기는 자리가 필요하고, 그건 점수 결합과 함께
다음 작업이다. **지금 상태로는 벡터 결과를 답변에 그대로 실으면 안 된다.**

## 「안 한 것」 목록

- 모델 호출 · 임베딩 생성 — 없음
- DB writer 호출 — 없음(계약만 맞췄다)
- 키워드/벡터 점수 결합 — 없음
- `BACKLOG.md` B-12 상태 — 안 건드렸다(Codex 와 겹친다)

## Codex 결정 (2026-11-04)

1. KST 오늘 자 channel raw는 색인하지 않는다. 오늘 자료는 키워드·실시간 조회가
   맡고, 벡터 색인은 닫힌 날짜까지만 명시적으로 다룬다. 첨부는 이 제한과 무관하다.
2. `kind`가 스키마에 생기기 전에는 `dm_scope` 요청 자체를 거절한다. 단순 기본값
   제외가 아니라 호출해도 시작되지 않는 상태다.
3. channel ID·표시명 좌표 변경 감지와 옛 current revision retire는 색인 잡의
   남은 완료조건으로 둔다.
4. DB의 `chunker_revision`에는 `source-v1:<chunk-plan>`을 저장한다.
   `source_chunker_revision()`이 이 값을 만든다.
5. `ArchiveStore.visible_channel_docs()`와 `visible_predicate()`를 공개 API로 올렸다.
   기존 private 이름은 호환 wrapper이며 새 정본 창구는 공개 API만 사용한다.

추가 수정: `revision_reader.enabled()`는 환경변수 존재만 확인하므로 DB 가 죽거나 표가
없어도 참이었다. 색인 전에는 `revision_reader.available()`로 실제 연결과
`archive_message_revision` 존재를 확인한다. 실패하면 정본을 만들지 않는다.
