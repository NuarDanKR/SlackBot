# HWP 두 건 — 서버 실측 결과

Written for: Codex, TYBot 팀. 작성: Claude, 2026-10-19.
대상: tyit `C0C9A86JUSJ` · `F0C9LHFTHLM`(2026.09) · `F0C9VH39NTW`(2025.12)
절차·도구: [2026-10-19-hwp-converter-crash-diagnosis.md](2026-10-19-hwp-converter-crash-diagnosis.md)

**`convert.py` · 변환기 신원 · revision · 복구 경로는 수정하지 않았습니다**(Codex 담당).
이 문서는 **측정값과 그 한계**만 적습니다. 복구 설계는 포함하지 않습니다.

## 1. 측정한 것

| 항목 | F0C9LHFTHLM | F0C9VH39NTW |
|---|---|---|
| 원본 보존 | 있음 · 29,696B | 있음 · 30,208B |
| 머리 바이트 | `d0cf11e0a1b11ae1` | `d0cf11e0a1b11ae1` |
| HWP 5 서명 | 있음 | 있음 |
| FileHeader 속성 | `0x00000001` (압축 비트만) | `0x00000001` |
| 확장자 ↔ 서명 | 일치 | 일치 |
| kordoc 재실행 | `exit=0 signal=- timeout=False` · `source.md` 생성 | 동일 |
| LibreOffice → PDF | 67쪽 · 229,637B · 글자 있는 쪽 67 | 54쪽 · 217,223B |

환경: `kordoc=/usr/local/bin/kordoc` · `soffice=/bin/soffice` ·
`STATE_DIR=/var/lib/tybot` · `TYBOT_CONVERT_SPOOL` 미설정(별도 변환 워커 없음).

> **RSS 66–67MB 는 「이 호출이 쓴 양」 이 아닙니다.** `RUSAGE_CHILDREN` 의 누적
> 최대라 같은 프로세스에서 여러 건을 돌리면 앞 건의 최대치가 남습니다. 한 건에
> 묶인 값이 필요하면 그 건만 단독 실행해야 합니다. 이 숫자로 과거 메모리 상황을
> 추정하지 마십시오.

## 2. `converter_crashed` — 원인 미확정

**재현되지 않았습니다.** 네 갈래(SIGKILL·SIGSEGV·SIGABRT·V8 fatal) 중 **어느 것도**
나오지 않았고, 두 건 모두 `exit=0` 으로 끝났습니다.

말할 수 있는 것은 여기까지입니다.

- 이번 실행은 성공했다
- 2026-10-16 16:44 당시 무엇이 일어났는지는 **이 실행이 알려 주지 않는다**

말할 수 없는 것 — **아래는 전부 미확정입니다.**

| 추정 | 왜 말할 수 없나 |
|---|---|
| OOM 이었다 | 신호도 표식도 없었다. 지금 RSS 는 과거를 말하지 않는다 |
| 환경이 바뀌어서 고쳐졌다 | 무엇이 바뀌었는지 확인하지 않았다 |
| 일시적 문제였다 | 한 번 성공한 것이 재발하지 않는다는 뜻이 아니다 |
| 파일은 정상이다 | 서명과 확장자가 맞는다는 것뿐이다. **내부가 온전하다는 뜻이 아니다** |

당시 로그가 남아 있다면 그것이 유일한 단서입니다.

```bash
sudo journalctl -u <아카이버 유닛> --since '2026-10-16 16:00' --until '2026-10-16 17:30'
sudo journalctl -k --since '2026-10-16 16:00' --until '2026-10-16 17:30' | grep -i 'killed process'
```

## 3. 추출량 차이 — **누락률이 아닙니다**

같은 원본을 두 경로로 읽었습니다.

| | kordoc 직접 | LibreOffice 렌더 → PDF 경로 |
|---|---|---|
| F0C9LHFTHLM | 4,074자 · 표 10행 · coverage `unknown` `None/None` | 47,140자 · 표 395행 · coverage `partial` `63/67page` |
| F0C9VH39NTW | 4,206자 · 표 13행 · coverage `unknown` `None/None` | 46,782자 · 표 399행 · coverage `partial` `49/54page` |

**PDF 경로가 11.6배·11.1배 더 많은 글자를 냈습니다.**

> **이 숫자를 손실률로 읽지 마십시오.** 어느 쪽이 맞는지 이 측정은 모릅니다.
>
> - PDF 경로는 Markdown 표의 `|` 와 구분선을 **더합니다.** 같은 내용이어도 글자가 늘어납니다
> - 반대로 kordoc 쪽이 중복을 접고 있을 수도 있습니다
> - 렌더 PDF 의 원시 텍스트(표 기호 없이)는 37,468자·37,687자입니다
>
> **정확성은 사람이 원본과 대조해야 확정됩니다.** §5 에 방법을 적었습니다.

참고 숫자 — 렌더 PDF 쪽별 글자 수:

| | 쪽 수 | 200자 초과 쪽 | 50자 이하 쪽 | 중앙값 | 최대 |
|---|---|---|---|---|---|
| F0C9LHFTHLM | 67 | 37 | 13 | 284 | 3,837 |
| F0C9VH39NTW | 54 | 34 | 5 | 358 | 4,956 |

## 4. 미확인 쪽

PDF 경로의 coverage 가 `partial` 입니다. 미확인 쪽이 남습니다.

| | 미확인 |
|---|---|
| F0C9LHFTHLM | 4쪽 |
| F0C9VH39NTW | 5쪽 |

F0C9VH39NTW 는 한 쪽에서 OCR 도 빈 결과였습니다.

```
PDF 34쪽 OCR 실패: kordoc 결과에 읽을 수 있는 본문이 없습니다
```

**쪽 번호와 유형은 아직 확인하지 않았습니다.** 측정 당시 스크립트가 개수만
세고 있었습니다. 번호와 쪽별 글자·이미지 개수를 세도록 고쳤으니 다시 돌려야
합니다(§5). 「이미지만 있는 쪽」 과 「렌더링이 실패한 쪽」 은 사람이 할 일이
다르고, **그 구분 전에는 Vision 이 필요한지 말할 수 없습니다.**

## 5. 아직 확인하지 못한 것

### (가) 미확인 쪽의 번호·유형 — **다시 돌려야 합니다**

```bash
sudo -u tybot TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/diagnose_hwp_conversion.py \
  --archive /var/lib/tybot/archive \
  --file-id F0C9LHFTHLM --file-id F0C9VH39NTW --compare-render
```

쪽 번호와 쪽별 「글자 N자 · 이미지 M개」 가 나옵니다. **무엇이 그려져 있는지는
세지 않습니다** — 개수만 보고 사람이 판단합니다.

### (나) 대표 표·숫자의 원본 대조 — **사람만 할 수 있습니다**

추출량 차이는 분량이지 정확성이 아닙니다. 표의 숫자·단위·행열 정렬이 맞는지는
**한글 원본을 열고 눈으로 맞춰야** 확정됩니다.

```bash
sudo -u tybot TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/diagnose_hwp_conversion.py \
  --archive /var/lib/tybot/archive \
  --file-id F0C9LHFTHLM --file-id F0C9VH39NTW \
  --compare-render --dump-dir /var/lib/tybot/tmp/hwp-review
```

`<file-id>.direct.md` · `<file-id>.via-pdf.md` · `<file-id>.rendered.pdf` 가
저장됩니다. **사내 문서 본문입니다** — 대조한 뒤 지우고, 로그·인계 문서·티켓에
내용을 붙이지 마십시오(원칙 5).

대조할 때 볼 것:

- 대표 표 한 개의 **행·열 수**가 원본과 같은가
- 그 표의 **합계 숫자**가 같은가 (합계는 사람이 가장 자주 묻는 값입니다)
- **단위**(원/천원/백만원)가 바뀌지 않았는가
- kordoc 쪽에 **없는 내용**이 PDF 쪽에 있는가, 혹은 그 반대인가

### (다) 그 밖에 미검증

1. **크래시 원인**(§2)
2. **다른 HWP 의 추출량 차이.** 이 두 건만의 현상인지 모릅니다.
   `--workspace tyit --compare-render` 로 범위를 넓힐 수 있으나 파일마다
   LibreOffice 렌더링이 돌아 느립니다
3. **LibreOffice 렌더링의 재현성.** 한 번씩만 돌렸습니다. 폰트 구성이 다르면
   쪽이 밀려 `rendered_sha256` 이 달라질 수 있는데, 같은 입력에 같은 해시가
   나오는지 확인하지 않았습니다
4. **HWPX 는 보지 않았습니다.** `_hwpx` 는 안전 XML 폴백이 있어 경로가 다릅니다
5. **변환이 Archiver 의 어느 env 로 도는지** 확인하지 않았습니다. 저는
   `/etc/tybot/tybot.env` · `tybot` 계정으로 돌렸습니다

## 6. 관측 — revision 이 같습니다

측정값만 적습니다. **판단은 Codex 몫입니다.**

```
F0C9LHFTHLM  revision 옛=e33970776804 새=e33970776804 충돌=True
F0C9VH39NTW  revision 옛=55bf8b030131 새=55bf8b030131 충돌=True
```

그 값이 아카이브에 이미 있는 파일 이름과 같습니다.

```
archive/workspaces/tyit/channels/C0C9A86JUSJ/attachments/F0C9LHFTHLM/e33970776804.md  (873B)
archive/workspaces/tyit/channels/C0C9A86JUSJ/attachments/F0C9VH39NTW/55bf8b030131.md  (873B)
```

원본 해시 · `staged_at` · 변환기 신원(`hwp:primary/2`)이 그대로라 같은 값이
나옵니다. 또 하나 관측: **설치된 변환기 자체의 판은 `converter_version` 에도
`config_snapshot()` 에도 들어가지 않습니다.**

어떻게 다룰지는 적지 않습니다 — 변환기 신원과 revision 은 Codex 담당입니다.

## 7. 도구 변경 (2026-10-19 검수 반영)

`scripts/diagnose_hwp_conversion.py` · `tests/test_diagnose_hwp_conversion.py`.

| 바꾼 것 | 왜 |
|---|---|
| **원시 stderr 를 출력에서 제거.** 허용 목록 표식(`v8_fatal`·`segfault`·`bad_alloc` 등)과 줄 수·글자 수만 남긴다 | 변환기는 처리 중인 문단·셀 값을 오류에 실어 보낸다. 꼬리를 자르면 **어디를 자르느냐가 운이 된다** |
| 표식이 하나도 안 걸리면 **「모름」 으로 남긴다** | 모를수록 더 조심한다. 모를 때 통째로 싣는 것이 가장 위험하다 |
| RSS 를 `children_max_rss_mb` 로 바꾸고 「이 호출 단독 사용량 아님」 을 함께 찍는다 | 라벨이 없으면 그 숫자가 이 건의 메모리로 읽히고 그대로 OOM 판단에 쓰인다 |
| 판정 문구에서 원인 단정을 뺐다 | SIGKILL 에 「OOM 이 흔하다」, SIGSEGV 에 「메모리가 아니다」, 성공에 「환경이 달랐다」 를 적고 있었다. **전부 추정이었다** |
| `loss_ratio` → `extracted_ratio`, 「추출량 차이」 로 표기 | 어느 쪽이 맞는지 모른다. 누락률로 쓰면 그 숫자가 손실률로 인계된다 |
| 미확인 쪽의 **번호**와 쪽별 글자·이미지 개수 | 개수만 남기면 어느 쪽을 열어 봐야 하는지 모르고, 그 쪽은 영원히 미확인으로 남는다 |
| `--dump-dir` | 표·숫자 정확성은 글자 수로 알 수 없다. 사람이 대조하려면 파일이 있어야 한다. **화면에는 찍지 않는다** |

시험 38건. 새로 넣은 것 중 핵심:

- `test_a_converter_that_prints_document_text_to_stderr_does_not_leak` —
  **진짜 하위 프로세스**로 문서 내용을 stderr 에 뱉게 하고, `RunReport` 전체를
  훑어 한 자리도 안 새는지 본다
- `test_the_printed_report_never_carries_stderr_text` — 출력 전체를 본다
- `test_the_rss_is_labelled_as_a_cumulative_child_maximum`
- `test_the_ratio_is_never_called_a_loss_rate` — 소스에 `loss_ratio` 가 없는지까지
- `test_missing_pages_keep_their_numbers_not_just_a_count`
- `test_the_dump_is_written_to_disk_not_printed`

**시험이 실제로 무는지 확인했습니다.** `stderr_codes` 를 원시 문자열로 되돌리면
누출 시험이 빨개지고, RSS 라벨·미확인 쪽 번호를 빼면 각각 1건씩 빨개집니다.

```
ruff check src tests scripts  → All checks passed
pytest tests/test_diagnose_hwp_conversion.py  → 38 passed
```
