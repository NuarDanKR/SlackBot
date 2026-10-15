# PDF 쪽별 OCR 처리 범위 — 변경 내용과 Codex 인계

Written for: Codex. 작성: Claude, 2026-10-15.
담당 범위: `src/tybot/archive/convert.py` · 관련 시험. `file_recovery.py` · 복구 CLI ·
재변환 스크립트는 **건드리지 않았다.**

## 1. 고친 것

서버 사례: 기본 PDF 추출 0/3쪽 → OCR 본문 2,543자 생성 → metadata 는 여전히
`partial` · 미확인 `[1,2,3]`.

본문은 나왔는데 **어느 쪽에서 나왔는지 아무도 몰랐다.** 문서를 통째로 OCR 했기
때문이다. 그 상태에서 「본문이 있으니 성공」 으로 닫으면 안 읽힌 쪽까지 읽은 것이
되고, 봇은 거기 있는 내용을 「없다」 고 답한다.

고친 방향: **OCR 을 쪽마다 돌리고, 쪽마다 읽혔는지 센다.**

- 텍스트 레이어를 쪽별로 읽어 `{쪽번호: 줄}` 과 `못 읽은 쪽` 을 나눈다
- 글자가 안 나온 쪽만 한 쪽짜리 PDF 로 떼어 내 `kordoc --ocr-force` 를 돌린다
- 문서 전체 글자가 `MIN_PDF_TEXT_CHARS`(200)에 못 미치면 **글자가 나온 쪽도** 다시
  본다(머리글만 텍스트인 스캔본) — 그래도 쪽 단위다
- 어느 쪽에서 본문이 나왔는지로 `converted` · `missing` 을 채운다
- 표시 줄(`[`·`#`)만 있는 쪽은 **읽은 쪽으로 세지 않는다**. 줄 자체는 남긴다

## 2. 함수 계약 변경

### 바뀐 것

| 함수 | 전 | 후 |
|---|---|---|
| `config_snapshot()` | 인자 없음 | `config_snapshot(filetype: str = "")`. 인자 없이 부르면 **전과 같은 5개 키**. `"pdf"` 일 때만 `pdf_ocr_max_pages` 가 붙는다 |
| `conversion_stamp(filetype, coverage, *, produced)` | 시그니처 동일 | 안에서 `converter_version(filetype)` · `config_snapshot(filetype)` 를 쓴다 |
| `_pdf(data)` | 통째 OCR 폴백 | 쪽별 OCR. 반환형 `list[str]` 그대로 |

`config_snapshot` 에 형식 인자를 넣은 이유: 이 값이 **revision 에 들어간다.**
`pdf_ocr_max_pages` 를 전역에 넣으면 그 값을 건드린 적도 없는 xlsx·docx 의
config 해시까지 달라지고, 멀쩡한 정본이 전부 옛 판이 된다.

### 새로 생긴 것

```python
CONVERTER_VERSION = "2"                        # 전역 기본값 — 그대로다
_FORMAT_CONVERTER_VERSIONS = {"pdf": "3"}      # 형식별 덮어쓰기
def converter_version(filetype: str) -> str    # 형식별 판. 없으면 전역 판

MAX_PDF_OCR_PAGES = _limit("TYBOT_PDF_OCR_MAX_PAGES", 50)
MIN_PDF_TEXT_CHARS = 200
OCR_NOTICE = "[변환 안내] OCR 사용 - 금액·날짜·조문은 원본 확인 필요"
OCR_PAGE_LIMIT_REACHED = "ocr_page_limit_reached"   # 새 품질 플래그

def _pdf_text_layer(reader) -> tuple[dict[int, list[str]], list[int]]
def _ocr_pdf_page(reader, number: int) -> list[str]
```

`MAX_PDF_OCR_PAGES` 는 **이 모듈에서 유일하게 기본이 무제한이 아닌 상한**이다.
쪽별 OCR 은 비용이 쪽수에 비례하고 쪽마다 120초 상한이 걸린다 — 500쪽 스캔본
하나가 변환기를 반나절 붙든다. 대신 다른 상한과 달리 **조용히 사라지지 않는다**:
걸린 쪽은 미확인으로 세고 `ocr_page_limit_reached` 플래그와 안내 줄을 남긴다.

### coverage 판정 (보수적으로)

| 상황 | 판정 |
|---|---|
| 모든 쪽에서 본문 | `succeeded` |
| 일부 쪽만 OCR 성공 | `converted` = 성공한 쪽 수, 나머지 `missing` → `partial` |
| OCR 이 빈 결과(`ExternalConversionError`) | 그 쪽은 `missing` |
| 한 쪽 OCR 이 예외로 터짐 | 경고 로그 + 그 쪽만 `missing`. 전체를 막지 않는다 |
| OCR 변환기 미설치 | 첫 쪽에서 멈추고 `ocr_unavailable` 플래그. 나머지 쪽도 결과가 같으므로 더 부르지 않는다 |
| 어느 쪽에서도 본문 없음 | `ConvertError` — 성공으로 닫지 않는다 |
| 혼합 텍스트/스캔 | 텍스트 쪽은 OCR 안 함, 스캔 쪽만 OCR. 둘 다 읽히면 `succeeded` |

OCR 안내 줄은 **문서 맨 위에 한 번만** 붙는다(쪽마다 붙으면 본문이 묻힌다).

## 3. 변환기 판 — 다른 형식은 그대로 둔다

`CONVERTER_VERSION` 전역 값을 올리지 않았다. 올리면 아무것도 안 바뀐 xlsx·docx·
pptx·hwpx 정본까지 한꺼번에 옛 판이 되고, 재변환 큐가 멀쩡한 첨부 전부를 다시
읽어 같은 결과를 새 경로에 쌓는다.

- `pdf` → `"3"` (출력도 coverage 도 판 2 와 다르다. **판 2 의 정본을 덮으면 안 된다**)
- 나머지 전부 → `"2"` (변화 없음)

`external_convert.py` 는 **고치지 않았다.** 한 쪽짜리 PDF 를 만드는 일은
`convert.py` 안에서 pypdf 로 하고 기존 `kordoc_lines()` 를 그대로 부른다.
`office_pdf_lines()`(pptx·ppt·docx 폴백)는 여전히 렌더된 PDF 를 통째로 OCR 하는데,
그건 이번 범위 밖이고 손대면 그 형식들의 판까지 올라간다.

## 4. 재변환 필요 조건 — **Codex 가 고쳐야 할 자리**

판을 형식별로 나눴기 때문에 `scripts/drain_conversion_queue.py` 의
`outdated_backfill()` 이 지금 상태로는 **옛 PDF 를 찾지 못한다.** 그 파일은 Codex
담당이라 손대지 않았다.

```python
# scripts/drain_conversion_queue.py:619 — 지금
if str(meta.get("converter_version") or "") == CONVERTER_VERSION:
    continue
```

옛 PDF 는 `"2"` 로 찍혀 있고 `CONVERTER_VERSION` 도 `"2"` 라 **건너뛴다.**
필요한 모양:

```python
from tybot.archive.convert import converter_version
...
current = converter_version(str(meta.get("filetype") or ""))
if str(meta.get("converter_version") or "") == current:
    continue
```

같은 이유로 `:661` 의 `queue.enqueue(..., converter_version=CONVERTER_VERSION)` 도
그 첨부의 형식 판이어야 한다. 전역 값으로 넣으면 차단기(`conversion_breaker`)
키와 큐 행이 실제로 돌린 판과 어긋난다.

### 재변환 대상

이 변경으로 결과가 달라지는 것은 **PDF 뿐**이고, 그중에서도 아래다.

1. `conversion_state` 가 `partial` 이면서 `missing_units` 가 비어 있지 않은 PDF
   → 이번 변경으로 어느 쪽이 읽혔는지 구분된다
2. 전체 OCR 로 `succeeded` 가 찍힌 PDF → **실제로는 일부만 읽혔을 수 있다.**
   이쪽이 더 위험하다. 숫자가 맞다고 적혀 있으니 아무도 다시 보지 않는다
3. `텍스트 레이어 없음` 으로 실패한 PDF → 쪽별로는 일부가 읽힐 수 있다

서버 사례인 PF(invest·archi 등) 스캔 PDF 가 1·2번에 들어간다.
재변환은 새 revision 으로 들어가므로 기존 정본을 덮지 않는다.

### 운영 변수

`TYBOT_PDF_OCR_MAX_PAGES` (기본 50, `0` 은 무제한). **이 값은 pdf 의 config
스냅샷에 들어가므로 바꾸면 pdf revision 이 달라진다** — 의도한 것이다. 바꾼 뒤
나온 변환본은 다른 판이고, 옛 정본을 덮지 않는다.

## 5. 시험

새로 넣은 것 — `tests/test_conversion_coverage.py` (7개):

| 시험 | 지키는 것 |
|---|---|
| `test_ocr_body_does_not_make_every_page_succeeded` | **서버 사례 그대로.** 3쪽 스캔, 1쪽만 OCR 성공 → `converted=1` · `missing=(2,3)` · `partial`. 스텁이 **한 쪽짜리 PDF 로 3번** 불렸는지까지 본다 |
| `test_every_page_ocred_is_a_full_read` | 다 읽힌 스캔본을 미확인으로 만들지 않는다 |
| `test_a_mixed_text_and_scan_pdf_only_ocrs_the_scanned_page` | 혼합 PDF 에서 스캔 쪽만 OCR. 쪽 순서 유지 |
| `test_a_scan_with_no_readable_page_is_refused` | 빈 결과만 오면 `ConvertError` |
| `test_an_ocr_free_host_flags_the_pages_it_could_not_check` | OCR 불가 환경 → `ocr_unavailable` + `partial` |
| `test_the_ocr_page_cap_is_recorded_not_silent` | 상한에 걸린 쪽이 조용히 사라지지 않는다 |
| `test_a_text_pdf_never_calls_the_ocr_converter` | **회귀.** 멀쩡한 텍스트 PDF 는 OCR 을 한 번도 안 부른다 |

`tests/test_converter_identity.py` (3개):

- `test_only_the_changed_format_gets_a_new_version` — pdf 만 올라갔고 나머지 8개 형식은 그대로
- `test_the_pdf_only_setting_stays_out_of_other_formats_config` — `pdf_ocr_max_pages` 가 xlsx config 에 안 샌다
- `test_the_new_pdf_converter_does_not_overwrite_the_old_canonical` — 판 2 와 판 3 의 revision 이 다르다

### 결과

```
.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_external_convert.py
4966 passed, 31 skipped in 311.10s
ruff check src tests scripts → All checks passed!
```

`tests/test_external_convert.py` 3건(`test_run_overrides_inherited_home` ·
`test_native_crash_is_not_misreported_as_oom` · `test_timeout_reaps_converter`)은
**이 변경 전에도 이 개발 PC 에서 실패한다**(`NameError: name 'base_events'` —
Windows 하위 프로세스/asyncio 환경 문제). `git stash` 로 확인했다. 서버에서 다시
봐 주면 좋겠다.

## 6. 안 한 것

- `file_recovery.py` · `scripts/recover_archiver_files.py` · 재변환 스크립트 — Codex 담당
- `external_convert.py` — 고칠 필요가 없었다(§3)
- `office_pdf_lines()` 의 통째 OCR — pptx·docx 판이 같이 올라가므로 범위 밖
- 원문·기존 metadata 수동 수정 — 없다. 재변환으로만 바뀐다

## 7. Codex 통합 검토 (2026-10-16)

형식별 판 계약을 재변환 큐에 연결했다. `outdated_backfill()`은 파일 형식의
`converter_version(filetype)`와 비교하며, 큐에도 해당 판과 변환기 이름을 기록한다.
기존 성공 작업이 새 판의 재변환을 가리지 않도록 큐 유일 키에 들어가는
`pipeline_version`을 `<기본 pipeline>:<변환기 이름>:<대상 판>`으로 구분했다.
변환기 판 필드만 바꾸면 큐의 ON CONFLICT 조건 때문에 기존 성공 행은 다시
등록되지 않는 점도 함께 보완했다. 구버전 `succeeded`와 `partial` PDF를 모두
확인하고 다른 형식의 현재 판은 그대로 두는 회귀시험을 추가했다.

페이지 coverage에는 경계 조건 두 개를 추가했다. 짧은 텍스트 레이어 때문에
OCR이 필요하다고 판정한 쪽은 OCR 실패 후 기존 머리글만으로 읽음 처리하지 않는다.
OCR 상한 밖의 해당 페이지도 미확인으로 남긴다. 기존 추출 문장은 보존하되
페이지 확인 수와는 구분한다. 같은 PDF 판 3의 통합 보완이다.

파일 복구 영수증이 있는 자료는 초기 staging 판이 그대로 남는 구조이므로,
메시지 큐의 `--outdated`에서는 별도 처리 건수로 표시하고 등록하지 않는다.
이 자료는 `recover_archiver_files.py --reconvert`가 보관 원본의 해시와 권한을
확인하고 변환 이력을 만든다. 새 이력의 coverage·PII 검사를 통과한 뒤 별도 정본을
게시하고, 색인은 기존 TYBot 자격증명의 `--index-only`로 분리한다.
절차: `docs/deploy/channel-file-sync.md`의 지정 재변환·전체 진단 절.

관련 109건 통과 및 전체 Ruff 통과. 실제 모델/OCR 응답, 서버 원본의 3/3쪽 판정,
서버 소켓/서비스 상태는 로컬 대본 시험으로 검증했다고 간주하지 않는다.
최종 전체 실행은 제외 없이 4,689 passed · 72 skipped · 7 warnings로 통과했다.
이 환경의 `test_external_convert.py` 9건도 통과했다. 경고 7건은 Windows의 기존
guard subprocess 인코딩 경고다. 중간 실행에서 발견한 운영 문서의 bare Python
명령은 가상환경 경로로 수정했다. 검토 RPC 예산 시험은 난수 HMAC의 끝이 LF일 때
현재 키 파일 규칙에 의해 31바이트가 되는 비결정성이 있어, 해당 예산 시험만
고정된 유효 시험 키로 바꿨다. 운영 키 파서·서명 규칙은 변경하지 않았다.
실제 서버 적용·OCR 결과·서비스 상태는 후속 검증으로 남는다.
