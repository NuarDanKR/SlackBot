# 벡터 후보 → EvidenceRef 출처 연결 (B-12)

작성 2026-11-04 · 새 파일: `src/tybot/archive/embedding_evidence.py` ·
`tests/test_embedding_evidence.py`. `embedding_source.py` 는 줄자리를 함께
내도록 넓혔다(정본 글자는 **그대로**). Codex 소유 파일은 안 건드렸다.

## 문제

벡터 DB 가 주는 것은 `(doc_path, start_offset, end_offset)` 이고, 창구가 그
구간의 **정본 글자**를 돌려준다. 그런데 답변에 붙어야 하는 것은 글자가 아니다.

- **출처**(원칙 2)는 `[조직명]업무명, 📄문서(날짜)` 이고 `SearchHit.citation()`
  이 만든다 → 벡터 결과도 `SearchHit` 이 되어야 한다
- **후속 질문**(원칙 1)이 이어 가는 것은 `EvidenceRef` 좌표다 → 벡터 결과도
  좌표를 남겨야 한다

## 한 일

정본을 만드는 함수가 **본문과 줄자리를 같이** 낸다(`canonical_lines`).
그 자리로 청크 구간 → 원문 줄을 찾고, 줄에서 `SearchHit` 과 `EvidenceRef` 를
만든다.

```
VectorCandidate ─(resolver)─▶ 정본 구간
                                 │  embedding_evidence.evidence_for
                                 ▼
                      원문 줄  ──▶ SearchHit  ──▶ citation()   (출처, 원칙 2)
                               └─▶ EvidenceRef ─▶ resolve_refs (후속, 원칙 1)
```

### 왜 한 함수가 둘 다 내나

자리를 따로 계산하면 정본을 만드는 규칙과 자리를 세는 규칙이 갈린다. 갈려도
**오류가 안 난다** — 사람이 출처를 눌러 가면 그 자리에 그 문장이 없을 뿐이다.

구체적으로 걸리는 곳이 NUL 이다. 전에는 줄들을 다 이어 붙인 **뒤** 한 번
고르게(`normalize`) 했다. 그러면 NUL 하나가 빠질 때마다 뒤쪽 offset 이 통째로
밀린다. 지금은 **줄마다 고른 뒤 이어 붙인다.** 글자 결과는 같고(시험이
붙들고 있다), 자리는 셀 수 있다.

### 지킨 선

| | |
|---|---|
| 근거는 **원문 한 줄** | 청크 조각은 어느 줄인지 고르는 데만 쓰고 버린다. 그 모양의 문장은 파일에 없다(원칙 1·2) |
| 출처는 **기존 자리**에서 | `SearchHit.citation()`. 벡터 경로가 따로 만들지 않는다 |
| 점수는 **안 만든다** | `score=1` (`resolve_refs` 와 같다). 키워드와 섞는 것은 별도 결정이고, 여기서 임의로 섞으면 B-12 완료 조건의 측정이 불가능해진다 |
| 권한은 **창구 하나** | `VisibleSources.entry()` 가 `open()` 과 같은 문을 지난다. 여기서 다시 열지 않고, 건너뛰지도 않는다 |
| 경계에 걸친 줄도 집는다 | 반만 걸쳤다고 버리면 겹침을 둔 긴 행의 이음매에서 근거가 사라진다 |
| 같은 줄은 한 번만 | 긴 행은 겹쳐 쪼개지므로 같은 줄이 두 청크에 있다. 출처가 두 번 붙으면 사람은 두 군데서 확인된 것으로 읽는다 |
| 상한 둘 | 청크당 12줄, 전체 `MAX_REFS`(40). **둘 다** 필요하다 — 바깥 상한만 두면 한 청크 안에서 예산을 넘긴다 |

### 사유 코드

`source_not_visible` · `chunk_lines_missing` · `chunk_lines_capped` ·
`no_line_coordinate` · `ref_limit_reached`. 업무 내용을 안 담는 고정 낱말이고,
「없다」 와 「권한이 없다」 를 구분하지 않는다(resolver 와 같은 이유).

## 연결됐다는 증거

`test_the_refs_reopen_through_resolve_refs` — 벡터가 만든 좌표를 **기존
`ArchiveStore.resolve_refs` 가 연다.** 줄도 출처 문자열도 같다.

그리고 바뀌면 닫힌다.

- 줄이 수정되면 지문이 안 맞아 `hash_mismatch`
- ACL 이 바뀌면 `permission_changed`
- 창구가 안 연 좌표는 `source_not_visible`

## 결과

시험 **25건**(+기존 50건 통과), 돌연변이 **10종 전부 잡힘**, ruff 0건.
전체 6682 passed · 172 skipped · 3 failed(전부터 있던 `route.original_router`,
무관).

## 아직 안 한 것 — 다음 결정

**답변 파이프라인에 안 붙였다.** 붙이려면 키워드 결과와 **섞는 규칙**이 먼저
있어야 하고, 그건 B-12 완료 조건이 「수치로 제시」 하라고 한 바로 그 항목이다
(재현율·무근거 감소·지연·비용·잘못 늘어난 후보).

붙이는 자리는 한 줄이다.

```python
sources = embedding_source.VisibleSources(store, ctx)
resolution = embedding_resolver.resolve_candidates(found, open_visible=sources.open)
got = embedding_evidence.evidence_for(resolution.resolved, sources, store.root)
# got.hits 를 키워드 hits 와 어떻게 합칠지가 남은 결정이다
```

남은 주의 둘.

1. **`score=1` 인 채로 `_rank` 에 섞으면 벡터 결과가 전부 바닥에 깔린다.**
   섞기 전에 점수 규칙을 정해야 한다 — `search_index.score_line` 을 벡터 줄에도
   먹일지, 거리로 별도 축을 둘지
2. **DM 은 여전히 색인되지 않는다**(`iter_index_sources` 가 막는다). 그래서 이
   경로로는 DM 근거가 나오지 않는다. 스키마에 `kind` 가 생긴 뒤의 일이다

## Codex 리뷰 보강

리뷰에서 세 경계를 추가로 닫았다.

1. 줄별 정규화는 `normalize(row + "\n")`으로 한다. `normalize(row) + "\n"`이면
   단독 CR로 끝난 줄이 기존 전체 정규화의 줄바꿈 1개와 달리 2개가 되어 정본 revision과
   뒤쪽 offset이 바뀐다.
2. resolver 통과 뒤 파일이 바뀌는 경우를 막는다. `evidence_for`가 현재 source revision,
   bounds, 청크 SHA-256과 resolver가 돌려준 본문을 다시 대조한 뒤에만 원문 줄을 만든다.
   바뀌면 `source_changed`로 닫는다.
3. `limit`과 `per_chunk`는 각각 `1..MAX_REFS`, `1..MAX_LINES_PER_CHUNK`만 허용한다.
   0으로 청크별 상한을 끄거나 상수보다 큰 값을 넘겨 안전 상한을 넓힐 수 없다.

출처·정본·resolver·기존 근거 회귀시험 133건이 통과했다. 답변 파이프라인 연결은
여전히 하지 않았다.
