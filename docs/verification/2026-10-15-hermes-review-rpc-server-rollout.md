# 요약 검토 RPC 서버 적용 — 현 상황과 Codex 인계

Written for: Codex. 작성: Claude, 2026-10-15 14:40 KST.
앞선 문서: [인계](2026-10-15-hermes-review-rpc-claude-handoff.md) ·
[worker 회신](2026-10-15-hermes-review-rpc-claude-handback.md) ·
[Python 연결 검증](2026-10-15-hermes-review-python-integration.md)

커밋: `145b3be`(코드) · `baa901f`(문서). 서버 적용까지 끝냈고 **첫 RPC 호출이 실패했다.**

## 1. 서버 상태 — 여기까지 됐다

| 단계 | 결과 |
|---|---|
| DB 스키마 | `summary_review_status_delivery` 적용. `tyslackai` 4개 권한 전부 `true` |
| Node 코드 배포 | 11:13 에 내려와 있었다. **`/opt/tybot` 은 git 저장소가 아니다** — `git pull` 로 배포하지 않는다 |
| 공유 worker 재시작 | 14:28. 그 전까지 **어제 16:42 프로세스**가 돌고 있었다(코드는 있는데 옛 프로세스) |
| `HERMES_RPC_VERSION` | 없어서 `unversioned` 였다. `20261015-a78bd9c` 로 설정 |
| health capability | `["review_candidates_v1"]` · workspace 6개 전부 `true` |
| TYBot 허용 목록 | `TYBOT_HERMES_REVIEW_WORKSPACES=tyit,mgmt,pilot,invest,archi,civil` (Master v2 와 동일) |
| 콘솔 재시작 | 14:30 |

```json
{"status":"ok","contract_version":"v2","version":"20261015-a78bd9c",
 "capabilities":["review_candidates_v1"],
 "workspaces":{"tyit":true,"mgmt":true,"pilot":true,"invest":true,"archi":true,"civil":true}}
```

> 운영은 **공유 worker 하나**로 간다. 6개 전부 `TYBOT_HERMES_SHARED_WORKSPACES` 에 있다.
> `hermes-rpc@tyit.service` 가 아직 떠 있지만 **아무도 안 쓴다** — 정리 대상이다(§5).

## 2. 드라이런 결과 — RPC 가 **한 번** 불렸고 실패했다

```
sudo -u tybot /opt/tybot/.venv/bin/python -m tybot.daily_review \
  --generate-only --result-json /tmp/review-dryrun.json
```

```
대상=75개
generated=0 sent=0 skipped=73 failed=1

review_rpc ws=invest request=441d107b-… status=failed proposed=0 accepted=0
요약 후보 생성 실패 ws=invest ch=C0C711W1H8S code=truncated_output
```

| 결과 | 수 |
|---|---|
| `no-new-source` | 73 — 커서가 머리에 있어 읽을 원문이 없다 |
| `generated-only` | 1 (tyit `C0BQUGRHV2A`) — **기존 대기 후보**다. `generated=0` 이라 새로 만든 것이 아니다 |
| `generate-failed` | 1 (invest `C0C711W1H8S`) — `truncated_output` |

**계약은 설계대로 동작했다.** 잘린 출력이 `empty` 로 접히지 않고 `failed` 로 올라왔고,
커서도 안 넘어갔다. 「장애를 정상 0건으로 숨기지 않는다」 가 첫 실전에서 작동했다.

다만 **결과가 실패다.** 그리고 그 원인은 Python 이 아니라 **Node worker 설정**으로 보인다.

## 3. `truncated_output` 의 원인 — Claude 영역이다

`truncated_output` 은 `stop_reason == "max_tokens"` 다. 근거:

- worker 의 모델 spec 기본값이 `max_tokens: 8000` · `effort: "high"`
  (`src/rpc/config.js:109-110`, `src/rpc/profiles.js:167-168`)
- **검토기가 Q&A 와 같은 spec 을 쓴다** — `run-rpc-shared.js` 의 `reviewerFor(profile)` 가
  `profile.model` 을 그대로 넘긴다(`:88`). Q&A 와 같은 줄이다(`:71`)
- `claude-opus-5-5` 는 **thinking 을 끌 수 없다.** 깊이는 `effort` 로만 조절하고 기본은
  `medium` 인데 우리는 `high` 를 준다. thinking 토큰이 `max_tokens` 를 먹는다
- Q&A 에서는 안 드러났다. 그쪽은 도구 왕복이라 회차마다 새 예산이고, 출력도 짧다

즉 **검토와 Q&A 의 출력 모양이 전혀 다른데 한 spec 을 공유한 것**이 설계 구멍이다.
검토는 한 번에 후보 10건을 JSON 으로 다 뱉어야 하고, 그 앞에 thinking 이 먼저 들어간다.

### 고칠 방향 (Claude 영역 — Node)

둘 중 하나, 또는 둘 다.

1. **검토 전용 모델 spec** — `profiles.json` 에 `review_model` / `review_max_tokens` /
   `review_effort` 를 더하고 `reviewerFor` 가 그것을 쓰게 한다. Q&A 와 분리한다
2. **기본값 조정** — 검토는 `effort: "low"` 가 맞다(후보를 고르는 일이지 추론이 아니다).
   `max_tokens` 는 16000 쯤

**서버에서 지금 당장 완화**할 수도 있다 — `profiles.json` 의 해당 workspace 에
`"max_tokens": 16000, "effort": "low"` 를 넣고 worker 재시작. 다만 그러면 **Q&A 도 같이
바뀐다**(같은 spec 이므로). 그래서 코드 분리가 맞다.

## 4. 이번에 확인하지 못한 것

드라이런이 RPC 를 **한 번만** 불렀다(73개가 `no-new-source`). 그래서 아래는 **미검증**이다.

- 정상 `ok` 경로 — 후보가 실제로 만들어지고 Python 재검증을 통과하는지
- 정상 `empty` 경로
- **상태 DM 실발송** — `--generate-only` 는 상태 DM 을 전부 건너뛴다(`notify()` 가
  `generate_only` 에서 즉시 반환). `status_sent=0 status_failed=0` 이 그 뜻이다
- 중복 방지(`summary_review_status_delivery`)가 실제로 막는지
- 비용 기록 — `external_review_cost` 로그가 한 줄도 안 나왔다. 실패 경로라 `charged=False`
  로 봉투가 기록됐을 텐데 로그 레벨 확인 필요

커서가 머리에 있어 **새 원문이 쌓여야** 정상 경로를 볼 수 있다. `--force-now --target
<ws>:<ch>` 로 한 채널을 강제하거나, 소급(`--backfill --rounds 1`)으로 과거 구간을
한 회차만 돌리는 방법이 있다. **예약일 커서 강제 초기화나 전체 소급은 쓰지 않는다.**

## 5. 같이 나온 별건 넷

이번 작업에서 드러났지만 요약 검토와는 별개다.

1. **`tec` 워크스페이스에 Slack 토큰이 없다**
   `Slack 토큰이 없어 기동하지 않는 워크스페이스: tec — 봇 관리 > 워크스페이스 연결에서
   봇을 붙이면 다음 기동부터 돕니다`
   채널 36개가 목록에는 있지만 DM 발송자가 없다. `TYBOT_HERMES_REVIEW_WORKSPACES` 에도
   없어 이번 범위 밖이다. 인계 문서의 「Master 없는 TEC 의 sender 지원은 별도」 그 건이다.

2. **`hermes-rpc@tyit.service` 가 아무도 안 쓰는 채로 떠 있다**
   6개 전부 공유로 가므로 받는 요청이 없다. 멈출 때 **공유 worker 와 혼동하지 않도록**
   확인하고 내린다:
   ```bash
   sudo systemctl stop hermes-rpc@tyit && sudo systemctl disable hermes-rpc@tyit
   ```

3. **`revision 본문이 원문과 맞지 않는다` 6건** (mgmt 4 · tec 1 · tyit 1)
   수집 쪽 기존 경고다. 이번 변경과 무관하지만 쌓이고 있다.

4. **콘솔에 `CONSOLE_SECRET` 이 없다**
   `CONSOLE_SECRET 이 없어 임시 키로 세션을 서명합니다. 서버를 재시작하면 모두 다시
   로그인해야 합니다.` — 재시작마다 전원 로그아웃된다.

## 6. 되돌리기

요약 검토만 끄려면 **한 줄**이다.

```bash
sudo sed -i '/^TYBOT_HERMES_REVIEW_WORKSPACES=/d' /etc/tybot/tybot.env
sudo systemctl restart tybot-console
```

끄면 기존 경로(내부 모델 직접 호출)로 돌아간다. DB 표와 Node worker 는 그대로 둬도 무해하다.

## 7. Codex 가 이어서 볼 것 (제안)

| | |
|---|---|
| **Python** | 실패 경로의 비용 기록이 실제로 남는지(로그 레벨). `truncated_output` 반복 시 재시도 간격이 1시간인지 |
| **운영** | 새 원문이 쌓인 뒤 정상 `ok`·`empty` 경로와 상태 DM 실발송 확인 |
| **Node(Claude)** | §3 의 검토 전용 spec 분리. 이건 제 영역이라 필요하면 제가 하겠습니다 |

§3 이 막히면 모든 검토 호출이 `truncated_output` 으로 떨어지고, 그건 검토자에게
**장애 상태 DM** 으로 나간다(`--generate-only` 가 아닌 실제 타이머 회차에서).
타이머가 돌기 전에 §3 을 먼저 보는 쪽을 권합니다.

## 8. Codex 후속 확인·수정 (2026-10-15, 서버 재검증 대기)

- 확정 증거는 **invest 한 요청의 `max_tokens` 종료**다. thinking이 예산을 소진했다는
  설명은 가능한 원인이지 응답 블록/사용량을 추가 확인한 확정 진단이 아니다.
  모든 검토가 동일하게 실패한다고 일반화하지 않는다.
- 개별/공유 worker 모두 Q&A spec과 검토 spec을 분리했다. 검토 기본값은 같은 모델 ID,
  `maxTokens=16000`, `effort=low`. Q&A 8000/high와 API 키 선택은 바꾸지 않았다.
- 공유 profile은 `review_model`/`review_max_tokens`/`review_effort`, 개별 env는
  `HERMES_RPC_REVIEW_MODEL`/`HERMES_RPC_REVIEW_MAX_TOKENS`/`HERMES_RPC_REVIEW_EFFORT`.
  새 필드를 넣지 않아도 기본값이 적용된다. 잘못된 설정은 해당 workspace를 거부한다.
- 기존 `truncated_output` 실패 계약·서명·취소·원문 검증은 유지한다. 잘린 JSON 재사용이나
  자동 재시도로 추가 비용을 발생시키는 변경은 하지 않았다.
- §4 비용 설명 정정: 서명과 계약 검증을 통과한 `failed` 응답도 metrics로 비용을
  기록한 다음 `charged=True`로 설정하고 예외를 낸다. `truncated_output` 자체만으로
  봉투 비용 경로에 들어가지 않는다. 현재 등록 모델의 비용 기록은 경고 로그가 없으므로
  `external_review_cost` 경고 부재만으로 미기록이라고 판정할 수 없다.
- 드라이런은 상태 DM을 검증하지 않았고, 정상 ok/empty와 실제 전달/중복 방지는 여전히
  서버 확인이 필요하다. 현재 재시도 제한은 같은 digest 실패 1시간이며 강제 실행은
  오너가 한 채널에서 명시적으로 제한을 우회하는 절차다.

배포 후에는 먼저 진행 중 요청을 확인하고 **공유 worker만 재시작**한다.
invest `C0C711W1H8S`를 한 회차 `--generate-only --force-now --target ...`로 확인하고,
새 로그의 request/workspace/status/output_tokens를 대조한다. 원문 커서는 초기화하지 않는다.
이후 실제 상태 DM과 중복 방지를 한 채널에서 확인한 뒤 전체 예약을 검증한다.
실제 모델 정상 출력은 아직 확인하지 않았다.

로컬 검증: Node review 48건, worker 계약/클라이언트/gateway 79건 통과.
전체 회귀 4594 passed / 72 skipped / 7 warnings(기존 Windows guard 인코딩).
그 뒤 Python 사전 출력 견적도 worker 기본 16000과 맞췄고, 관련 최신 190건 통과.
최종 JSON 외 모델 출력도 예산을 쓸 수 있으므로 4096 가정을 제거했다. 단가와
실제 보고 토큰 기반 비용 기록 방식은 바꾸지 않았다. Ruff·catalog·check-mode 검사 통과.
서버 확인은 미완료다. 이 후속 변경을 반영한 배포와 공유 worker 재시작 후 재검증한다.
