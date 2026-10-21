# 보관 HWP 복구 — Claude 회신

Written for: Codex, 운영. 작성: Claude, 2026-10-20.
범위: [2026-10-20-companion-app-rollout.md](2026-10-20-companion-app-rollout.md)
「Claude assignment: historical HWP recovery」

**커밋하지 않았습니다.** `scripts/inventory_companion_channels.py` · Vision 정책·
원장 · Slack 초대 조작은 건드리지 않았습니다.

## 1. 먼저 — **로컬 `rhwp` CLI 는 없습니다**

범위 문서가 「`rhwp` CLI 또는 다른 검증된 로컬 변환기」 라고 적었는데, 확인해
보니 `rhwp` 쪽에는 **로컬 CLI 가 없습니다.**

| 무엇 | 실제 |
|---|---|
| [rhwp for Slack](https://github.com/postmelee/rhwp-slack) | **호스팅 Slack 앱.** README 에 CLI 설치·명령이 없다. 변환 서버가 문서를 임시로 내려받아 처리한다 |
| `@rhwp/core` | Rust·WASM **렌더링 라이브러리**(Obsidian 플러그인이 쓴다). CLI 가 아니다 |

그래서 「보관 원본에 로컬 rhwp CLI 를 시험」 은 **지금 실행할 수 없습니다.**

그리고 호스팅 rhwp 를 복구에 쓰면 **보관 HWP 원본 전부가 외부 변환 서버로
나갑니다.** 그건 변환이 아니라 **전송**이고, 범위 문서 자신의 외부 전송 승인
경계(§External invitation gates 2)와 B-80/WC-21 §3 을 그대로 지납니다. 파일 한
건씩 승인받는 경로라면 모르되, **과거 전량 복구에 쓸 수는 없습니다.**

### 그래서 변환기를 코드에 박지 않았습니다

후보를 **설정으로 받게** 만들었습니다. 검증된 로컬 변환기가 정해지면 그때
환경변수로 넣으면 되고, 이 모듈을 고치지 않아도 됩니다 — 고치면 **이전 측정과
조건이 달라져 비교가 무의미해집니다.**

현실적인 다음 후보는 **H2Orestart**(LibreOffice 확장)로 보입니다. 지금 복구 경로의
부분 쪽 coverage 가 LibreOffice 의 HWP 임포트 한계에서 오는 것이라면, 그것이
직접 겨냥하는 자리입니다. **아직 시험하지 않았습니다**(§5).

## 2. 만든 것

| 파일 | 내용 |
|---|---|
| `src/tybot/archive/hwp_candidates.py` | **신규.** 후보 변환기 실행·측정 라이브러리. 읽기 전용 |
| `scripts/inventory_hwp_originals.py` | **신규.** 보관 HWP 인벤토리 + 후보 대조 CLI. 읽기 전용 |
| `tests/test_hwp_candidates.py` | **신규 26건** |

Codex 가 만든 `hwp_render.py`(`hwp:rendered-pdf` v1)와 `prepare_hwp_recovery.py`
는 **건드리지 않았습니다.** 제 것은 그 옆에 붙는 **측정 도구**이고, 복구 세대를
만들지 않습니다.

### 인벤토리 — 무엇이 복구 대상인가

「실패한 HWP 를 되살리자」 에는 세 가지가 섞여 있고, 섞어 두면 뒤의 둘이 앞의
하나에 묻힙니다. 사유를 **코드로** 남깁니다(문장이면 집계가 안 됩니다).

| 사유 코드 | 뜻 |
|---|---|
| `excluded-workspace` | `tec` — 범위 문서가 기본 제외로 못박았다 |
| `policy-blocked` | PII 차단. **승인으로 우회하지 않는다** |
| `original-not-retained` | 원본 바이트가 없다 → Slack 재수집이 선행 |
| `original-hash-mismatch` | 바이트가 바뀌었거나 해시가 없다 → **검증된 원본이 아니다** |
| `uploader-provenance-unverified` | 사람 업로더를 **여기서 증명할 수 없다**(아래) |

내려가는 값: workspace · channel · file_id · filetype · 실제 바이트 수 ·
`conversion_state` · `error_code` · `staged_at` · **현재 정본 신원**
(`converter_name`/`converter_version`) · `coverage_state` · 대상 여부 · 제외 사유.

### 사람 업로더 provenance — 할 수 있는 만큼만 말합니다

수집 경로는 이미 봇 메시지를 버립니다. 그래서 **`origin_message_ts` 가 있는
첨부는 사람 메시지로 들어온 것**입니다.

그 좌표가 없으면 **제외합니다.** 파일 단독 복구 경로로 들어온 것은 수집 당시
Slack API 로 업로더를 확인했지만(`file_recovery._shared_file`), **이 스크립트는
Slack 을 부르지 않으므로 그 사실을 여기서 다시 증명할 수 없습니다.** 증명할 수
없는 것을 대상에 넣지 않습니다.

> 운영에서 이 사유로 빠지는 건수가 많으면 알려 주십시오. Slack 을 부르는
> 확인 단계를 따로 둘지는 그때 판단할 일입니다.

### 대조 — 추출량 **차이**이지 누락률이 아닙니다

`--compare` 가 지금 경로(`convert("hwp")`)와 후보 변환기들을 같은 표에 놓습니다.
후보마다 재는 것: 종료 코드·신호·초 · PDF 쪽 수·해시·바이트 · 우리 PDF 경로로 읽은
글자·표 행 수·coverage·**미확인 쪽 번호** · 쪽별 글자 분포(200자 초과 / 50자 이하 / 최대).

비율은 `extracted_ratio` 이고 출력에 **「누락률이 아니다」** 를 같이 찍습니다.
PDF 경로는 Markdown 표의 `|` 를 더하므로 같은 내용이어도 글자가 늘고, 반대로 지금
경로가 중복을 접고 있을 수도 있습니다. **어느 쪽이 맞는지 이 측정은 모릅니다.**

## 3. 지킨 경계

| 요구 | 어떻게 |
|---|---|
| 읽기 전용 / 드라이런 기본 | 두 도구 모두 **쓰기 경로가 없습니다.** 후보는 임시 디렉터리에서 돌고 끝나면 지워집니다 |
| 원본·실패 기록 보존 | metadata·정본·색인·DB 를 열지 않습니다. 원본은 읽기만 합니다 |
| 검증 전 정본 발행 금지 | 발행 코드가 없습니다. 산출 PDF 는 `--dump-dir` 로 **사람 대조용으로만** 나갑니다 |
| **봇 출력 필터 유지** | 풀지 않았습니다. 네 자리를 시험으로 고정했습니다(§4) |
| 인벤토리 도구·Vision 정책·원장 | 열지 않았습니다 |
| 외부 전송 금지 | 후보는 **로컬에서 끝나는 것만**. 기본 후보에 `http`·`curl`·`wget` 이 없는지 시험합니다 |

### `--dump-dir` 는 사내 문서 본문입니다

표·숫자 정확성은 글자 수로 알 수 없어 사람이 원본과 맞춰야 하고, 그러려면 산출물이
파일로 있어야 합니다. **화면에 찍지 않고** 경로만 알려 줍니다. 대조 후 지우고,
로그·인계 문서·티켓에 내용을 붙이지 마십시오(원칙 5).

## 4. 봇 산출물이 원본이 되지 않게 — **시험으로 고정했습니다**

rhwp 를 채널에 초대하면 그 앱이 변환 PDF 를 채널에 올립니다. 그것을 원본으로
세면 **우리 변환본을 우리가 다시 근거로 쓰는 고리**가 생깁니다(원칙 1).

필터는 네 자리에 있고, **하나만 풀려도** 그 경로로 들어옵니다.

| 자리 | 시험 |
|---|---|
| 실시간 수집 `archiving_bot` | `test_realtime_ingest_drops_bot_messages` |
| 소급 수집 `backfill` | `test_backfill_drops_bot_messages` |
| 이력 수집 `collect` | `test_history_collection_drops_bot_messages` |
| 파일 단독 복구 `file_recovery` | `test_file_recovery_refuses_bot_generated_files` |

문장만 보지 않고 **동작**으로도 봅니다 — rhwp 모양의 봇 PDF(`bot_id` 있음)가
업로더 조회까지 가지 못하는지, `bot_id` 가 없는 앱 사용자가 막히는지.

## 5. 아직 못 한 것 — **시험을 서버에서 돌리지 않았습니다**

1. **두 건의 실제 대조를 돌리지 않았습니다.** 개발 PC 에는 원본이 없습니다.
   §6 의 명령을 서버에서 돌려야 숫자가 나옵니다
2. **H2Orestart 를 시험하지 않았습니다.** 설치 여부부터 모릅니다. 후보 설정에
   넣어 돌려야 「부분 coverage 가 LibreOffice 한계인가」 가 답해집니다
3. **표·숫자 정확성은 여전히 미검증입니다.** 범위 문서가 요구한
   「사람이 대표 표·숫자를 원본과 대조」 는 아직 아무도 하지 않았습니다.
   **그 전에는 두 건을 파일럿으로 올리면 안 됩니다** — 범위 문서가 그렇게
   적었고, 저도 그 조건을 바꾸지 않았습니다
4. **`uploader-provenance-unverified` 로 빠지는 실제 건수**를 모릅니다
5. 후보 변환기 실행 시간·메모리를 운영 규모로 재지 않았습니다. LibreOffice 는
   파일마다 수 초~수십 초입니다

## 6. 서버 절차

```bash
# 1) 인벤토리 — 무엇이 대상인가
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/inventory_hwp_originals.py \
  --archive /var/lib/tybot/archive

# 2) 두 건 대조
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/inventory_hwp_originals.py \
  --archive /var/lib/tybot/archive \
  --file-id F0C9LHFTHLM --file-id F0C9VH39NTW --compare

# 3) 사람 대조용 산출물 (대조 후 지운다)
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/inventory_hwp_originals.py \
  --archive /var/lib/tybot/archive \
  --file-id F0C9LHFTHLM --file-id F0C9VH39NTW \
  --compare --dump-dir /var/lib/tybot/tmp/hwp-review
```

후보를 더하려면 **환경변수**입니다(로컬 변환기만).

```bash
TYBOT_HWP_CANDIDATES='[{"name":"h2orestart",
  "command":"soffice --headless --norestore --convert-to pdf --outdir {outdir} {src}",
  "version_command":"soffice --version","output":"source.pdf"}]'
```

## 7. 시험 결과

개발 PC(Windows)에서 돌렸습니다. 서버 절차가 아니라 결과 보고입니다.

| 무엇 | 결과 |
|---|---|
| `ruff check` (`src` · `tests` · `scripts`) | All checks passed |
| 전체 (`test_external_convert.py` 제외) | **5,340 passed · 43 skipped** (255s) |
| `tests/test_hwp_candidates.py` 단독 | **26 passed** |

**시험이 실제로 무는지 확인했습니다.** 세 곳을 되돌려 돌렸습니다 →
**6건 red** (실시간 봇 필터 1 · `tec` 제외 1 · 원본 해시 검증 2 · stderr 누출 2).

구현하면서 잡힌 것 둘 — 둘 다 서버에서도 날 수 있는 것이라 고쳤습니다.

- `shlex` 가 POSIX 규칙이라 경로를 템플릿에 끼워 넣으면 토큰이 갈라졌습니다.
  **쪼갠 뒤에 치환**하도록 바꿨습니다
- 실행 파일 해시가 `OSError` 로 터졌습니다(보호된 경로의 심 링크). 신원 하나를
  못 잡는다고 후보 전체를 못 재면 안 되므로, 못 읽은 사실을 적고 버전 배너로
  버티되 **둘 다 없으면 거부**합니다

## 8. Codex 에게

- §1 이 판단 지점입니다. 호스팅 rhwp 를 복구에 쓸지 말지는 **외부 전송 승인**
  문제이고 제 영역이 아닙니다. 쓰지 않는다면 H2Orestart 같은 로컬 후보를
  정해 주십시오 — 설정으로 바로 들어갑니다
- 제 도구는 `hwp_render.py` 의 복구 세대와 **겹치지 않습니다.** 측정만 합니다.
  둘을 합치지 말아 주십시오 — 합치면 「재어만 보려다 세대를 만들었다」 가 생깁니다
- 인벤토리의 `converter_name`/`converter_version` 은 **지금 정본 신원**입니다.
  재변환이 그 자리를 덮는지 판단하는 값인데, 그 판단은 그쪽 몫이라 비교만 싣고
  결론을 적지 않았습니다
