# 2026-11-02 `tybot.service` OOM — 원인과 고친 것

조사: 2026-11-03 · 커밋 전 인계 · 기준 커밋 `f93e3fc`

## 운영에서 본 것

| | |
|---|---|
| 죽은 프로세스 | python PID 1029362 · anon-rss 3,184,980 KiB · total-vm 5,673,280 KiB |
| 죽은 방식 | global OOM → SIGKILL (`MemoryMax` 없음) |
| 직전 질문 | 채널 7개 · 검색 42건 · Hermes external-v3 |
| 같은 시각 | Slack TLS timeout · Node deadline 지연 |
| 재시작 뒤 | `MemoryCurrent` 약 1.05 GiB |

## 결론 — 누수가 아니라 **한 질문 안의 되풀이**다

전역 캐시가 질문이 끝난 뒤에도 자라는 곳은 **없었다.** 다음 네 곳을 확인했고
전부 아니었다.

| 의심한 곳 | 판정 |
|---|---|
| `ToolSessionRegistry._sessions` (v3 도구 권한) | **아님** — `specialist_v3_client.call()` 이 `finally` 로 `revoke` |
| `Touched.documents` · `evidence_hits` | **아님** — 실측 12회 검색 누적 +1.6MB |
| `pilot._seen_events` · `_workspace_admin_cache` · `_creator_cache` | **아님** — 개수 상한 있음 / ID→bool 뿐 |
| `ArchiveStore._cache` (원문 MD) | **아님** — 파일 지문으로 캐시, 재파싱 없음 |

진짜 원인은 **첨부 정본에 캐시가 없다**는 것이었다.

```
ToolBox._search  ──┬─ _channels_matching → visible_docs()   ← 전체 재구축 ①
                   ├─ store.search       → visible_docs()   ← 전체 재구축 ②
                   └─ 0건이면 낱말별 재검색 최대 5회 → 각각 visible_docs()  ← ③~⑦

visible_docs() → _docs() → attachment_reader.archive_docs()
                              → evidence() → load_checked() → path.read_text()
                              → as_archive_doc()            → 본문 한 줄 = RawLine 하나
```

`load_checked()` 는 **캐시가 없다.** 그래서 `visible_docs()` 한 번마다 첨부
정본 전부를 디스크에서 다시 읽고, 본문을 전부 다시 `RawLine` 객체로 만들었다.
원문 MD 는 `ArchiveStore._load` 가 진작부터 같은 방식으로 캐시하고 있었고,
**첨부만 빠져 있었다.**

한 번의 `search` 도구 호출이 `visible_docs()` 를 2~7 번 부르고, 그 질문은
검색을 42 번 했다. 42 × (2~7) = **84~294 회의 전체 재구축.**

### 왜 3 GiB 가 되나

| | |
|---|---|
| 상주 (파싱된 아카이브 한 벌) | 재시작 직후 1.05 GiB 로 관측됨 |
| 재구축 1 회의 일시 사용 | 그 벌의 1.4 배 남짓 (파일 대비 2.3 배) |
| 두 벌이 동시에 살아 있는 순간 | ①의 결과를 쥔 채 ②를 만든다 |
| glibc arena | 수백 번의 할당·해제 뒤 OS 로 안 돌아감 |

### 증폭 두 곳

1. **`ArchiveStore.search()`** 가 보이는 **모든 줄**에 대해 좌표표
   (`indexed_lines`)를 만들었다. 실제로 쓰이는 줄은 최대 `CANDIDATE_LIMIT`(500)
   인데도, 표는 아카이브 크기로 커지고 검색마다 새로 만들어졌다. 줄마다
   `rel_path()` 로 경로 문자열도 새로 만들었다.
2. **`ArchiveStore._scan()`** 이 맞은 줄 전부를 `SearchHit` 으로 만들어 담고
   마지막에 잘랐다. 흔한 낱말 하나면 아카이브의 거의 모든 줄이 맞는다.
   `_scan` 은 색인 0건·DB 장애·낡은 문서 보완에서 돌고, **0건일 때의 낱말별
   재검색이 바로 그 경로다.**

## 실측 (합성 86MB 아카이브 · 원문 아님)

7 채널 × 20 일 × 300 줄 + 첨부 200 장 × 2,000 줄 = 442,000 줄.

| | 고치기 전 | 고친 뒤 |
|---|---|---|
| `visible_docs()` 1회 정점 | **194.9 MB** | **2.5 MB** |
| `_scan()` (흔한 낱말) 정점 | **121.2 MB** | **2.5 MB** |
| where 범위 검색 4건 정점 | **194.9 MB** | **2.7 MB** |
| where 범위 검색 4건 시간 | **14.15 s/건** | **3.38 s/건** |

첨부를 뺀 같은 아카이브(원문 42,000 줄만)에서 `visible_docs()` 는 **2.5 MB**
였다. 즉 195 MB 중 **192 MB 가 첨부 경로**다.

시간도 같이 떨어졌다. 42 건이면 고치기 전 **594 초**가 아카이브 작업에만 들어간다 —
같은 시각의 Slack TLS timeout 과 Node deadline 지연은 이것과 같은 사건으로 본다.

## 고친 것

### 1. 첨부 정본 캐시 (`attachment_reader.DocCache`)

원문 MD 와 **같은 방식**이다 — 열쇠는 경로, 값에 `(mtime_ns, st_size)` 지문.
변환 워커가 정본을 다시 쓰면 지문이 달라져 그 자리에서 다시 읽는다.
**캐시 때문에 옛 변환본이 근거가 되는 일은 없다**(시험으로 박았다).

상한은 **개수가 아니라 바이트**다. 개수로 막으면 큰 PDF 변환본 몇 장이 상한을
통과하면서 메모리를 다 쓴다. 기본 192 MiB(원본 파일 바이트 기준, 메모리에서는
두 배 남짓), `TYBOT_ATTACHMENT_CACHE_MB` 로 바꾼다. 넘치면 오래 안 쓴 것부터
버리고, **버려도 근거가 사라지지 않는다** — 다음 호출이 그 파일만 다시 읽는다.

### 2. 검색 좌표표를 후보 수로 묶음 (`ArchiveStore.search`)

색인이 돌려준 좌표(≤500)만 표에 담는다. 건수(`expected_counts`)는 여전히 전부
세지만 그건 파일당 정수 하나다. 경로 변환은 `_RelPaths` 로 **파일당 한 번**.

### 3. `_scan` 중간 결과 추려 내기

`SCAN_KEEP_FACTOR`(64) × `limit` 를 넘으면 `_rank` 로 추린다. **자르는 함수가
최종 순서를 정하는 함수와 같으므로 순서가 바뀌지 않는다** — 점수나 파일 순서로
대충 자르면 같은 점수 줄 중 어느 것이 근거가 되는지가 조용히 달라진다.
합성 14,400 줄로 추려 내기 전후 결과가 **좌표까지 동일**함을 시험한다.

### 4. 읽기 전용 RSS 관측 (`tybot.rss_probe`) — 기본 꺼짐

`TYBOT_RSS_LOG=1` 일 때만 `/proc/self/status` 를 읽는다. 리눅스가 아니면 조용히
아무것도 안 한다. 남기는 것은 **도구 이름(고정 낱말)과 바이트 수뿐** — 질문·
검색어·원문·채널명·사람 이름·경로·시크릿은 한 글자도 넣지 않는다. 메모리 로그는
장애 때 통째로 퍼 날라지는 자리라, 여기 들어간 것은 아카이브 권한 밖으로 나간다.

```bash
sudo -u tybot /opt/tybot/.venv/bin/python -c \
  "from tybot import rss_probe; print(rss_probe.sample())"
```

## 바뀐 파일

| 파일 | |
|---|---|
| `src/tybot/archive/attachment_reader.py` | `DocCache` 신규 · `load_checked`/`evidence`/`archive_docs` 에 `cache=` |
| `src/tybot/archive/store.py` | `ArchiveStore._attachments` · `_RelPaths` · `SCAN_KEEP_FACTOR` · `search`/`_stale_hits`/`_scan` |
| `src/tybot/specialist_tools.py` | 도구 경계에 `rss_probe.observe(name)` 3줄 |
| `src/tybot/rss_probe.py` | 신규 |
| `tests/test_archive_memory_growth.py` | 신규 15건 |

B-81 파일(`image_description_*`, `deploy/tybot-image-description@.*`)과
`answer_status` 관련 파일은 **건드리지 않았다.**

## 시험

`tests/test_archive_memory_growth.py` 15건. 자료는 전부 합성이다 — 원문·검색어·
사람 이름을 fixture 에 넣지 않는다(원칙 5).

변이 시험으로 **실제로 잡는지** 확인했다.

| 고친 것을 되돌리면 | 깨지는 시험 |
|---|---|
| 첨부 캐시 제거 | `attachments_are_read_once…` · `attachment_lines_are_not_rebuilt…` · `repeated_requests_do_not_grow_memory` |
| `_scan` 추려 내기 제거 | `scan_holds_less_than_an_unbounded_scan_would` |
| `_RelPaths` 제거 | `rel_path_is_computed_once_per_file…` (4건 → 800건) |

전체: `6440 passed · 84 skipped · 3 failed` · `ruff check src tests scripts` 0건.

**실패 3건은 전부 기존**이다. 내 변경을 stash 하고 돌려도 같은 3건이 깨진다.

- `test_image_description_clearance_admin.py::test_write_routes_are_registered_before_the_static_mount`
- `test_image_description_policy_routes.py::test_both_routes_are_registered_on_the_app`
- `test_image_description_policy_routes.py::test_the_write_routes_come_before_the_static_mount`

셋 다 `route.original_router` 를 단언한다. FastAPI 0.117.1 / Starlette 0.48.0 은
그 속성을 **달지 않는다**(`include_router` 뒤 모든 라우트에서 `hasattr` 거짓).
B-81 쪽 판단이라 그대로 뒀다.

## 미검증 · 남은 것

| | |
|---|---|
| **운영 DB·운영 아카이브에서 재지 않았다** | 합성 86MB 로만 쟀다. 실제 아카이브의 첨부 분량을 모르면 기본 192 MiB 상한이 맞는지 알 수 없다 |
| **1.05 GiB 상주 자체는 안 줄였다** | `ArchiveStore._cache` 는 개수(4096)로만 막히고, 그 상한은 **없어진 파일만** 치운다. 파일이 4096개 넘게 살아 있으면 캐시는 줄지 않는다 |
| **`MemoryMax` 가 없다** | 상한이 없으면 다음 회귀도 global OOM 으로 나타난다. 적어도 `MemoryHigh` 는 걸어 두는 편이 낫다 |
| `revision_reader.apply` 가 **병합 문서마다 DB 를 본다** | `_docs()` 호출마다 채널 수만큼 왕복한다. 메모리가 아니라 지연 쪽이고 이번에 안 건드렸다 |
| `Touched` 의 `in` 검사 | `SearchHit.__eq__` 가 `raw_lines` 리스트까지 비교한다. 실측에서 문제가 안 났지만(동일 객체 지름길) 구조상 O(히트²) |
| 콘솔 화면 없음 | RSS 관측은 env 스위치뿐이다. CLAUDE.md 「운영 기능은 콘솔에 둔다」 기준으로는 진단 예외에 해당한다고 보고 넘겼다 |

## 운영에서 확인할 것 (배포 뒤)

```bash
sudo systemctl set-environment TYBOT_RSS_LOG=1
sudo systemctl restart tybot   # manager 환경은 새 프로세스부터 반영된다
# 채널 여러 개 + 검색 많은 질문을 한 번 던지고
sudo journalctl -u tybot -g 'tybot.rss' --since '-10 min'
systemctl show tybot -p MemoryCurrent
sudo systemctl unset-environment TYBOT_RSS_LOG
sudo systemctl restart tybot   # 조사 스위치를 실행 중 프로세스에서도 끈다
```

`anon_delta_kib` 가 검색 도구 호출마다 **수십 MiB 씩 늘면** 이 고침이 안 걸린
경로가 남아 있다는 뜻이다.

---

# 보완 (Codex 리뷰 반영, 2026-11-03)

초안 `DocCache` 에 대한 지적 6건을 반영했다. 이번 라운드에서 고친 것은
`attachment_reader.py` **한 파일**과 신규 시험·이 문서뿐이다.
`store.py` · `specialist_tools.py` 는 앞 라운드 상태 그대로다.

## 1. 상한보다 큰 파일 한 장도 들고 있지 않는다

초안의 `while self._bytes > self._max and len(self._items) > 1` 은 **마지막 한
장을 남겼다.** 그래서 상한보다 큰 변환본 한 장이 들어오면 다른 것을 전부 밀어낸
뒤 혼자 상한을 넘겨 상주했다 — 상한을 둔 이유가 바로 큰 변환본이었는데,
그 경우에만 상한이 꺼지는 꼴이다.

이제 `_put` 이 **넣기 전에** 크기를 본다.

```python
if self._max <= 0 or size > self._max:
    ... 한 번 경고하고 그냥 안 넣는다
    return
```

- 돌려주기는 한다 — **근거는 사라지지 않는다.** 들고 있지 않을 뿐이고, 다음
  호출이 그 파일만 다시 읽는다(고치기 전과 같은 비용이지 더 나쁜 비용이 아니다)
- 큰 파일이 **작은 것들을 쫓아내지도 않는다**(애초에 들어오지 않는다)
- 왜 캐시가 안 듣는지 운영이 알도록 `(파일 크기 > 상한)` 과
  `TYBOT_ATTACHMENT_CACHE_MB` 를 **revision 당 한 번** 경고한다. 호출마다
  찍으면 로그가 그 한 줄로 덮인다
- 축출 고리도 고쳤다 — 방금 넣은 것에 닿으면 멈춘다(무한 고리 없음)

## 2. 실제 경로에 붙어 있다

`load_checked` 와 `as_archive_doc` **둘 다** 지난다. 한쪽만 붙으면 파일은 안
읽는데 줄 객체는 매번 다시 만들고, 부피의 대부분이 줄 객체라 그러면 고친 것이
거의 없다.

| | |
|---|---|
| `ArchiveStore.__init__` | `self._attachments = attachment_reader.DocCache()` |
| `ArchiveStore._docs` | `archive_docs(self.root, channels, cache=self._attachments)` |
| `archive_docs` | `evidence(..., cache=cache)` → `load_checked(..., cache=cache)` |
| `archive_docs` | `cache.as_archive(path, doc, lambda: as_archive_doc(doc, root))` |

`test_the_store_routes_both_read_paths_through_its_cache` 가 `visible_docs()` 를
6 회 불러 `load` 4회 · `as_archive_doc` 4회(파일 수와 같음)임을 단언한다.

## 3. 같은 파일을 둘이 동시에 파싱하지 않는다

`visible_docs()` 는 워크스페이스 봇들이 공유하는 인스턴스에서 불린다. 두 요청이
같은 큰 정본을 캐시가 채워지기 **전**에 만나면 둘 다 헛걸음하고 **같은 본문이
메모리에 두 벌** 올라갔다. 한 벌이 200MB 면 그 순간 400MB — 고치려던 것과 같은
모양이다.

경로마다 문(`_gate`)을 둔다. 먼저 들어간 쪽이 만들고, 뒤에 온 쪽은 기다렸다가
**만들어진 것을 받는다**(문 안에서 캐시를 다시 본다 — 이 재확인이 없으면 문은
순서만 세울 뿐 중복 파싱을 못 막는다). 문은 경로별이라 다른 파일은 나란히 간다.
쓰는 사람이 없으면 치운다 — 문이 쌓이면 그게 다음 누수다.

실측(8 스레드가 같은 대형 정본에 동시 진입):

| | 문 없음(초안) | 문 있음 |
|---|---|---|
| `load` 호출 | 8 | **1** |
| 할당 고점 | 1,109,755 B | **529,214 B** |
| 한 벌 기준 | **2.1 배** | 1.0 배 |

문이 잠긴 채 남지 않는 것도 본다 — 만들다 예외가 나도 풀린다(안 풀리면 그
파일이 영원히 막힌다).

## 4. 되풀이해도 평탄하다

`search` 42 회를 돌며 **라운드마다** 할당 고점을 재고, 뒤쪽 절반의 최대가
앞쪽 절반의 최대보다 크게 높지 않은지 본다. 두 가지로 돌린다.

| 시험 | 조건 |
|---|---|
| `forty_two_searches_stay_flat` | 기본 상한 — 캐시가 다 들어가는 경우 |
| `…_while_the_cache_thrashes` | `TYBOT_ATTACHMENT_CACHE_MB=1` — **축출이 계속 도는** 더 어려운 경우 |
| `…_eighty_six_megabytes_stays_flat` | 사고 규모 그대로(86MB · 42회). **기본 꺼짐** |

86MB 짜리는 생성에만 분 단위가 들어 기본 스위트에서 빼고 env 로 연다.
한 번 돌려 확인했다 — **통과, 3분 41초.** 같은 자료로 잰 42회 전체 고점은
**4.7MB**(고치기 전 한 회가 194.9MB).

```bash
TYBOT_OOM_FULL_SCALE=1 python -m pytest tests/test_attachment_doc_cache.py -k full_scale -q
```

## 5. 상한·무효화·축출·비활성 시험

| | |
|---|---|
| 비활성 (`max_bytes=0`) | 한 바이트도 안 들고 있고, 「너무 크다」 로 기록하지도 않는다 |
| 지문 무효화 | 재변환본을 쓰면 `AttachmentDoc` 과 `ArchiveDoc` **둘 다** 무효 |
| LRU 축출 | 최근에 쓴 것은 남고 오래된 것이 나간다(다시 읽히는지로 확인) |
| 상한보다 큰 한 장 | 보유 0 · 다른 항목 불변 · 경고 1회 |
| 못 읽는 파일 | 캐시를 건너뛴다. 없는 파일을 「없다」 로 굳히지 않는다 |
| 축출 중 근거 손실 | 상한 1MiB 로 조여도 첨부 문서 수가 그대로다(느려질 뿐) |

## 6. 유닛 상한과의 관계 (Codex `deploy/tybot.service`)

Codex 가 `MemoryHigh=1536M` · `MemoryMax=2048M` 을 넣었다 — 앞 라운드에서
「미검증」 으로 남겼던 자리다. **두 숫자는 같이 봐야 한다.**

```
상주 기준선 ~1.05 GiB  +  캐시 192MiB(파일 바이트, 메모리로는 두 배 남짓 ≈ 440MB)
                       ≈ 1.5 GiB  =  MemoryHigh
```

기본 상한을 올리면 `MemoryHigh` 에 먼저 닿아 조용히 느려진다(회수 압력).
**운영 아카이브의 첨부 분량을 재기 전에는 기본값 192MiB 를 올리지 말 것.**

## 시험 현황

`tests/test_attachment_doc_cache.py` 19건(1건은 env 로 열림) · 전부 합성 자료.
변이 시험으로 실제 잡는지 확인했다.

| 되돌리면 | 깨지는 시험 |
|---|---|
| 「마지막 한 장은 남긴다」 로 복귀 | `a_single_file_larger_than_the_cap_is_not_kept` |
| 경로별 문 제거 | `concurrent_requests_parse_a_large_file_only_once` · `…_do_not_double_the_peak` |
