# 원문 MD 캐시를 바이트 상한 LRU 로 — 설계·실측·예산

2026-11-02 OOM 후속 3차 · 커밋 전 인계 · 범위: `src/tybot/archive/store.py` + 신규 시험 + 이 문서

앞선 두 문서:
[OOM 원인과 고친 것](2026-11-03-oom-archive-rebuild-handback.md)

## 무엇이 남아 있었나

`ArchiveStore._cache` 의 상한은 **개수 4096** 이었고, 넘쳤을 때 치우는 것은
**없어진 파일뿐**이었다.

```python
if len(self._cache) > 4096:
    self._cache = {p: v for p, v in self._cache.items() if p.exists()}
```

살아 있는 원문이 4096개 이하면 **파일이 아무리 커도 전부 상주**한다. 4096개를
넘어도 살아 있는 파일은 한 장도 안 나간다. 이번 OOM 의 직접 원인은 첨부였지만
같은 계열의 위험이 여기 남아 있었다 — 개수로 막으면 「몇 개인가」 를 세는데
메모리는 「몇 바이트인가」 로 쓰인다. 둘이 어긋나는 쪽이 늘 사고다.

## 실측 — 파일 바이트 → 상주 바이트

합성 자료(원문 7.0MB / 첨부 77.9MB, 442,000 줄).

| | 파일 | 상주 | 배수 |
|---|---|---|---|
| 원문 MD 파싱 (`ArchiveDoc`) | 7.0 MB | 16.9 MB | **2.43×** |
| 첨부 `AttachmentDoc` 만 | 77.9 MB | 57.4 MB | 0.74× |
| 첨부 `AttachmentDoc` + `ArchiveDoc` (`DocCache` 가 실제로 쥐는 모양) | 77.9 MB | 194.2 MB | **2.49×** |

줄 하나가 `RawLine` 객체 하나가 되기 때문이다. 그래서 상한은 **파일 바이트**로
세고 배수는 코드 옆에 적어 둔다(`ARCHIVE_RESIDENT_RATIO`).

캐시가 더운 상태의 요청 1회 일시 사용량: **원문 파일 바이트의 0.35배**
(`visible_docs` 2.45MB / `search` 2.54MB @ 원문 7.0MB).

## 예산 — 128MiB 가 맞나

### 프로세스 모양 먼저

`tybot.service` 는 `tybot.slack.pilot` 하나를 띄우고, **`ArchiveStore` 는 한 개를
모든 워크스페이스 봇이 공유**한다([pilot.py:4429](../../src/tybot/slack/pilot.py#L4429) →
`WorkspaceBot(cfg, store=store, …)`). **상한이 워크스페이스 수만큼 곱해지지 않는다.**
`archiving_bot`·콘솔은 각각 별도 유닛이다.

### 계산

| | 파일 상한 | 배수 | 상주 |
|---|---|---|---|
| 원문 캐시 `TYBOT_ARCHIVE_CACHE_MB=128` | 128 MiB | 2.43 | **311 MiB** |
| 첨부 캐시 `TYBOT_ATTACHMENT_CACHE_MB=192` | 192 MiB | 2.49 | **478 MiB** |
| 요청 1회 일시 (원문 상한에서) | — | 0.35 | **45 MiB** |
| | | | **합 834 MiB** |

`MemoryHigh=1536MiB` 기준 남는 자리 **702 MiB** — 인터프리터·라이브러리·
워크스페이스별 Slack 클라이언트·psycopg·LLM 게이트웨이·glibc 단편화 몫이다.

### 판정 — 서버 실측으로 확정 (2026-11-03)

```
raw 38.0 MiB -> 상주 약 92 MiB
```

**읽기 (b) 였다.** 원문 아카이브는 38 MiB 뿐이고, 재시작 직후 `MemoryCurrent`
1.05 GiB 의 대부분은 캐시 밖이다.

| | |
|---|---|
| `MemoryCurrent` 관측 | 1,075 MiB |
| 원문 캐시 (38 MiB × 2.43) | **92 MiB** |
| 캐시 밖 나머지 | **983 MiB** — 인터프리터·라이브러리·워크스페이스 봇·psycopg·게이트웨이, 그리고 **회수되지 않은 glibc arena** |

983 MiB 중 얼마가 arena 인지는 모른다. 고치기 전에는 한 질문이 195MB 급 할당을
수십 번 반복했으므로(1차 문서) **적지 않을 것**이고, 그 몫은 이번 고침으로
줄어든다. 다만 **줄어든다고 가정하고 예산을 짜지는 않는다.**

#### 원문 캐시 128 MiB — 그대로 둔다

실제 아카이브가 38 MiB 라 **상한이 걸리지 않는다.** 지금은 아무것도 바꾸지
않고, 아카이브가 3.4 배로 자랄 때까지의 보험으로 남는다. 상주를 늘릴 수 없는
변경이므로 이대로 확정한다.

#### 첨부 캐시 192 MiB — **그대로 둔다**

```
att 133.9 MiB -> 상주 약 333 MiB
```

실제 첨부가 **133.9 MiB** 라 192 MiB 상한이 **안 걸린다.**

그리고 여기서 앞선 계산 하나를 고친다. **캐시 상주와 「요청이 쥐는 것」은 같은
메모리다 — 더해지지 않는다.** `_docs()` 가 돌려주는 목록은 캐시가 들고 있는
바로 그 `ArchiveDoc` 객체를 가리킨다(`cache.as_archive` 가 캐시된 객체를
그대로 돌려준다). 실측:

| | 요청이 쥔 동안 | 정점 | 놓은 뒤 |
|---|---|---|---|
| 캐시 충분 | **+0.9 MB** | +2.7 MB | +0.3 MB |
| 캐시 0 (끔) | +137.8 MB | +195.1 MB | +0.2 MB |

(합성 첨부 77.9MB · 첨부 문서 200개는 양쪽 같다)

**따라서 상한을 133.9 MiB 아래로 내리면 메모리가 줄지 않는다.** 요청은 어차피
첨부 전부를 쥐고, 캐시만 못 쥐게 되어 **호출마다 다시 만든다.** 더 나쁘다.

게다가 `evidence()` 는 파일을 **정렬 순서로 한 바퀴** 돈다. 작업 집합보다 작은
LRU 에 순차 훑기는 **적중률이 0 에 수렴하는** 최악의 조합이다. 「절약」 하려고
조이면 캐시를 없앤 것과 같아진다.

#### 확정된 예산

| | |
|---|---|
| 캐시 밖 | 983 MiB |
| 원문 (38.0 MiB 파일) | 92 MiB |
| 첨부 (133.9 MiB 파일) | 333 MiB |
| 요청 1회 일시 | 13 MiB |
| **합계** | **1,421 MiB** · `MemoryHigh` 1536 대비 **+115 MiB** |

**맞는다. 기본값(원문 128 · 첨부 192)을 바꾸지 않는다.**

실제로는 이보다 나을 가능성이 높다. 983 MiB 에는 **고치기 전의 회수 안 된
arena** 가 섞여 있고(한 질문이 195MB 급 할당을 수십 번 반복했다), 첨부 333 MiB 는
그 빈 arena 를 쓰게 된다. 다만 **그렇게 될 것이라고 가정하고 예산을 짜지 않았다.**

#### 성장 여유 — 여기가 진짜 한계다

| | |
|---|---|
| 첨부가 쓸 수 있는 자리 | 1536 − (983+92+13) = **448 MiB** |
| 파일 기준 | 448 / 2.49 = **180 MiB** |
| 지금 | 133.9 MiB |
| 남은 여유 | **46 MiB (+34%)** |

첨부가 180 MiB 를 넘으면 `MemoryHigh` 에 닿는다. **상한을 올리든 내리든
막지 못한다** — `_docs()` 가 호출마다 첨부 **전부**를 객체로 만들기 때문에
상주가 총 첨부 바이트에 그냥 비례한다. 상한은 캐시를 묶을 뿐 **요청이 쥐는
것을 묶지 않는다.**

고치려면 첨부 근거를 **게으르게** 만들어야 한다 — 검색이 실제로 건드린 첨부만
열기. 지금은 `search()` 가 쓰든 말든 전부 연다. 별도 작업으로 남긴다(아래).

### 후속 완료 — 검색 경로의 게으른 첨부 적재 (2026-11-03)

`ArchiveStore.search()` 는 이제 권한이 허용된 채널 원문만 먼저 열고, 검색 DB가
돌려준 첨부 좌표와 같은 file-id의 revision 형제만 연다. `converted_at` 기준 최신
revision 검증, ACL, raw 중복 제거는 기존 함수를 그대로 거친다. DB 좌표가 오래된
revision을 가리키면 과거 본문을 되살리지 않는다.

DB를 못 읽거나 후보 좌표가 현재 파일과 맞지 않을 때도 첨부 전체 목록을 만들지
않는다. file-id 한 묶음씩 열어 바로 점수화하고, 중간 검색 결과만 제한된 크기로
유지한다. 따라서 색인 장애가 다시 전체 첨부 객체 동시 상주로 이어지지 않는다.

합성 첨부 24개 회귀시험에서 스트리밍 검색의 할당 고점이 전체 `visible_docs()`
적재의 절반 미만임을 강제한다. 후보 한 개 검색은 그 file-id만 읽고, 경로 이탈,
오래된 revision, DB 장애 폴백도 별도 시험한다.

`visible_docs()` 자체와 감사·제목 목록처럼 **전체 자료를 명시적으로 요구하는**
호출은 기존 동작을 유지한다. 이번 OOM 경로인 질의 검색만 게으르게 바꿨다.

#### 그래도 안 맞으면

| | 바꾸는 것 | 비고 |
|---|---|---|
| **A** | 첨부 근거 **게으른 적재** | 상주를 총 첨부 바이트에서 떼어 내는 유일한 길. 별도 작업 |
| B | 오래된 첨부를 아카이브 루트 **밖**으로 옮김 | 운영 조치. 근거에서 빠지므로 합의가 필요하다 |
| C | `MemoryHigh` 를 올림 | **권하지 않는다.** 유닛 상한은 마지막 그물이고, 늘리면 회귀가 다시 host OOM 으로 나타난다 |
| D | 첨부 상한을 내림 | **효과 없다.** 위에서 잰 대로 메모리가 안 줄고 호출마다 다시 만든다 |

#### 배포 뒤 확인

```bash
systemctl show tybot -p MemoryCurrent     # 1,421 MiB 근처 또는 그 아래면 예산대로
```

## 고친 것

### 바이트 상한 LRU (`RawCache`)

- 열쇠는 경로, 값에 `(mtime_ns, st_size)` 지문. **지문이 달라지면 바로 무효**
- dict 의 삽입 순서를 사용 순서로 쓴다 — 맨 앞이 가장 오래 안 쓴 것
- `TYBOT_ARCHIVE_CACHE_MB` (기본 128, `0` 이면 완전히 끈다)

### 상한보다 큰 파일 한 장은 들고 있지 않는다

「마지막 한 장은 남긴다」 로 두면 그 한 장이 상한을 무시하고 상주한다 — 상한을
둔 이유가 큰 파일인데 그 경우에만 상한이 꺼지는 꼴이다. **돌려주기는 한다**:
근거는 사라지지 않고, 다음 호출이 그 파일만 다시 읽을 뿐이다(고치기 전과 같은
비용이지 더 나쁜 비용이 아니다). 작은 항목들을 쫓아내지도 않는다.

### 형식 위반은 사유 문자열로 계속 캐시한다

문자열은 **길이가 곧 비용**이라 따로 센다(`_cost`). 그래서 큰 파일이 깨졌을
때도 사유는 캐시된다 — 안 그러면 호출마다 그 큰 파일을 다시 열어 다시 거절한다.
사유를 안 들고 있으면 「깨진 큰 파일」 이 가장 비싼 경로가 된다.

### 같은 파일을 둘이 동시에 파싱하지 않는다

경로마다 문(`_gate`). 먼저 온 쪽이 만들고 뒤는 기다렸다 **만들어진 것을 받는다**
(문 안에서 캐시를 다시 본다 — 이 재확인이 없으면 문은 순서만 세우고 중복 파싱은
그대로다). 예외가 나도 풀린다. 쓰는 사람이 없으면 치운다.

실측(8 스레드 동시 진입, 같은 파일):

| | 문 없음 | 문 있음 |
|---|---|---|
| 파싱 횟수 | 8 | **1** |
| 할당 고점 | 한 벌의 2배 초과 | **1배 수준** |

### 낡은 벌을 다시 읽기 **전에** 놓는다

`_peek` 이 지문 불일치를 보면 그 자리에서 자리를 돌려준다. `_put` 이 끝날 때
치워도 숫자는 맞지만, 그 사이 — 같은 파일을 다시 파싱하는 동안 — 낡은 벌과
새 벌이 **둘 다** 메모리에 있다. 큰 파일에서는 그 한 순간이 두 배고, 이번 OOM 이
바로 그 모양이었다.

### 이력이 무한히 쌓이지 않는다

| | |
|---|---|
| 캐시 항목 | 바이트 상한으로 하드 바운드 |
| 없어진 파일 | `source_docs()` 가 지금 파일 목록으로 `prune()`. 목록을 못 받는 경로(`broken()`)는 512 회 삽입마다 `_sweep_missing()` |
| 「너무 크다」 이력 | `OVERSIZE_HISTORY=256` 으로 FIFO |
| 경로별 문 | 쓰는 사람이 0 이면 즉시 제거 (`gates_open` 으로 확인 가능) |

`prune()` 은 **안 보인다고 지우지 않는다** — 없어진 것만 지운다. DM 은 요청마다
범위가 달라서, 안 보이는 것을 지우면 매 호출이 서로의 캐시를 무효로 만든다.

### 로그에 경로를 넣지 않는다

상한 초과 경고는 **건수와 바이트만** 남긴다. 아카이브 경로에는 채널명이 들어
있고 채널명은 조직 이름이다(원칙 2 가 그걸 출처로 쓴다). 로그는 장애 때 통째로
퍼 날라지는 자리라, 여기 들어간 것은 아카이브 권한 밖으로 나간다고 봐야 한다.
어느 파일인지는 `RawCache.oversize` 로 **메모리에서만** 본다.

> 첨부 캐시(`attachment_reader.DocCache`)의 같은 경고는 **경로를 싣는다.**
> 이번 범위 밖이라 안 고쳤다. 맞추는 편이 낫다고 보지만 판단은 Codex 몫이다.

## 캐시가 답을 바꾸지 않는다

가장 중요한 시험이다. 캐시를 켠 결과와 끈 결과(`max_bytes=0`)가 **좌표까지**
같아야 한다 — 출처와 후속 질문이 그 좌표 위에 서 있다(원칙 1·2). 캐시가 좌표를
바꾸면 「방금 그 문서」 가 다른 줄을 연다. **오류 없이.**

비교 항목: `workspace` · `channel_id` · `source_path` · `lineno` · `ts` ·
`message_ts` · `content_hash` · `score` · `citation()`.

| 시험 | |
|---|---|
| `results_and_evidence_coordinates_match_with_and_without_cache` | 질의 4종 × 켬/끔 |
| `resolve_refs_returns_the_same_line_with_and_without_cache` | `EvidenceRef` 복원 좌표 + 탈락 사유 코드 |
| `a_thrashing_cache_does_not_lose_evidence` | 상한 2KiB 로 조여 축출이 계속 도는 상태에서 3회 반복 |

## 시험

`tests/test_archive_raw_cache.py` **33건.** 자료는 전부 합성이다 — 원문·검색어·
사람 이름·실경로를 fixture 에 넣지 않는다(원칙 5).

| 요구 | 시험 |
|---|---|
| 1 바이트 LRU | `the_cap_counts_bytes_not_files` · `many_small_files_are_all_kept…` · `eviction_takes_the_least_recently_used` |
| 2 기본값 128 | `the_default_cap_is_128_mib` · `the_cap_comes_from_the_environment` |
| 3 상한 초과 단일 파일 | `a_file_larger_than_the_cap_is_returned_but_not_cached` · `…does_not_evict_the_others` · `search_still_finds_an_oversize_file` |
| 4 지문 무효화 | `a_rewritten_file_is_reparsed` · `a_stale_entry_releases_its_bytes_immediately` · `…dropped_before_the_new_one_is_built` |
| 5 동시 파싱 1회 | `concurrent_requests_parse_a_file_only_once` · `…does_not_double_the_peak` · 문 해제 2건 |
| 6 SchemaError 유지 | `a_schema_violation_is_cached_as_a_reason_string` · `a_broken_file_larger_than_the_cap_still_caches_its_reason` · `broken_and_fixed_still_works` |
| 7 이력 바운드 | `deleted_files_release_their_bytes` · `the_oversize_history_is_bounded` · `a_sweep_runs_without_the_file_list` · `prune_keeps_files_outside_the_listed_scope` |
| 8 결과·좌표 동일 | 위 3건 |
| 9 42회 평탄 | `forty_two_rounds_stay_flat` · `…_while_the_cache_thrashes` |
| 10 로그 비노출 | `the_oversize_warning_carries_no_path_or_content` |

### 돌연변이 시험 — 9종 전부 잡힌다

| 되돌린 것 | 잡은 시험 |
|---|---|
| 상한을 바이트 → 개수(4096) | 8건 |
| 상한보다 큰 파일도 보유(마지막 한 장 남김) | 6건 |
| 경로별 문 제거 | 2건 |
| SchemaError 문자열 캐시 제거 | 2건 |
| 사유 문자열 비용을 파일 크기로 | 1건 |
| 없어진 파일 치우기 제거 | 2건 |
| oversize 이력 무한 증가 | 1건 |
| 지문 불일치 시 자리 즉시 반환 제거 | 1건 |
| 경고에 경로 싣기 | 1건 |

마지막에서 둘째는 **처음엔 아무 시험도 잡지 못했다.** `_put` 이 넣을 때 낡은
항목을 치우므로 최종 숫자는 같았기 때문이다. 다시 읽는 **도중**을 재는 시험을
추가해서 잡았다.

## 결과

- 전체: **6494 passed · 85 skipped · 3 failed** · `ruff check src tests scripts` **0건**
- 실패 3건은 **기존**이다(`route.original_router` — FastAPI 0.117.1 /
  Starlette 0.48.0 은 그 속성을 달지 않는다). 내 변경과 무관하고 B-81 쪽 판단이다
- 커밋하지 않았다

## 미검증 · 남은 것

| | |
|---|---|
| **첨부 성장 여유가 46 MiB 뿐이다** | 180 MiB 를 넘으면 `MemoryHigh` 에 닿고, **어떤 상한으로도 못 막는다**. 첨부 근거 게으른 적재가 다음 작업이다 |
| 983 MiB 중 arena 몫을 모른다 | 이번 고침으로 줄겠지만 줄어든다고 가정하고 예산을 짜지 않았다. 배포 뒤 `MemoryCurrent` 를 다시 보면 갈린다 |
| 배수 2.43/2.49 는 **합성 자료 기준** | 줄이 아주 길거나 짧은 실제 아카이브에서는 달라진다. 한 줄이 객체 하나이므로 **줄 수**가 많을수록 배수가 커진다 |
| 첨부 캐시 경고가 경로를 싣는다 | 범위 밖이라 안 고쳤다 |
| 콘솔에서 캐시 상태를 못 본다 | `held_bytes` · `held_files` · `oversize` 는 메모리에만 있다. CLAUDE.md 「운영 기능은 콘솔에 둔다」 기준으로는 붙이는 편이 맞다 |
| `revision_reader.apply` 의 문서당 DB 왕복 | 지연 쪽이고 이번에도 안 건드렸다 |

---

# 덧붙임 — 시험 두 곳을 고쳤다 (2026-11-03)

`test_archive_raw_cache.py` 를 추가한 뒤
`test_attachment_doc_cache.py::test_forty_two_searches_stay_flat_while_the_cache_thrashes`
가 **같이 돌릴 때만** 깨졌다. 혼자 돌리면 통과했다. 파고든 결과 **제품 코드
문제가 아니었고**, 대신 시험 쪽 결함 두 개가 나왔다.

## 1. 재는 방법이 틀렸다 — 프로세스 계단을 「증가」 로 읽었다

라운드별 고점이 이랬다.

```
1083 1086 … 1087 1087 | 2965 2965 … 2965 2965
                  └ 30번째 라운드에서 1.9MB 계단, 그 뒤로는 완전히 평탄
```

매 실행 **바이트까지 같은** 값이었다. 캐시 상태를 같이 찍어 보니 42 라운드 내내
그대로였다 — 첨부 캐시 133KB, 원문 캐시 2개. **데이터는 늘지 않았다.**

원인은 측정 기준이다. 시작 시점 하나(`base`)를 잡고 `peak - base` 를 쟀는데,
같이 도는 다른 시험이 프로세스 수준에서 한 번 올려놓은 양이 그 뒤 **모든**
라운드에 그대로 섞인다. 「이번 라운드가 쓴 양」 이 아니라 「프로세스가 지금까지
쓴 양」 을 잰 셈이다.

고침: 라운드마다 `gc.collect()` 한 뒤 **그 시점의 `current` 를 기준**으로 잰다.

```python
gc.collect()
before, _ = tracemalloc.get_traced_memory()
tracemalloc.reset_peak()
...일을 한다...
_, high = tracemalloc.get_traced_memory()
peaks.append(high - before)
```

이러면 다른 시험이 무엇을 남겨 놓든 영향을 받지 않고, **진짜 누수(라운드마다
조금씩 느는 모양)는 그대로 잡힌다.** 같은 고침을 원문 캐시 쪽 `_peaks` 에도 했다.

## 2. thrash 시험이 thrash 를 안 하고 있었다

```python
monkeypatch.setenv("TYBOT_ATTACHMENT_CACHE_MB", "1")   # 1 MiB
```

그런데 그 시험의 첨부 20장은 전부 합쳐 **133 KB** 다. 상한이 **한 번도 걸리지
않았다.** 이름만 thrash 이고 실제로는 앞 시험과 같은 것을 재고 있었다.

고침: 작업 집합을 먼저 재고 그 **1/4** 로 상한을 잡는다. 그리고 상한이 실제로
걸렸는지(`held_bytes < working_set`)를 단언한다.

```python
working_set = sum(p.stat().st_size for p in attachment_reader.source_files(tmp_path))
cap = working_set // 4
store._attachments = attachment_reader.DocCache(max_bytes=cap)
...
assert store._attachments.held_bytes <= cap
assert store._attachments.held_bytes < working_set   # 축출이 돌았나
```

이제 이 시험이 실제로 **적중률 0 에 가까운 최악**을 돈다. `evidence()` 는 파일을
정렬 순서로 한 바퀴 돌기 때문에, 작업 집합보다 작은 LRU 에서는 돌아올 때마다
앞의 것이 이미 빠져 있다. 그 상태에서도 라운드별 사용량이 평탄함을 본다.

실행 시간이 45초 → 2분 30초로 늘었다. **늘어난 만큼이 전에 안 하던 일이다.**

## 결과

같은 조합을 **3회 연속** 돌려 전부 통과. 전체 **6497 passed · 85 skipped ·
3 failed**(기존 `original_router` 3건) · ruff 0건.

> 범위 밖이지만 `tests/test_attachment_doc_cache.py` 를 고쳤다. 거짓 실패를
> 남기면 다음 사람이 그 시험을 믿지 않게 되고, 「상한이 안 걸리는 상한 시험」 은
> 통과해도 아무것도 지키지 않는다. 둘 다 시험 파일 안에서 끝나는 수정이다.
