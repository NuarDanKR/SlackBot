# 공유 Hermes RPC - TYBot 측 구현과 운영 전환

2026-10-14 갱신, B-76. Node 구현 및 로컬 통합 검증은 완료했고 Linux 서버 검증은 남아 있다.
Claude의 `5aab006` P1/P2 보완도 재검증했다. TYIT만 먼저 공유 모드로 전환한다.
Claude 계약은 [worker 인계 문서](2026-10-12-hermes-shared-rpc-handoff.md)를 따른다.

## Codex 검증 결과

- 관련 pytest 70건 통과: endpoint 선택, 실제 workspace/ToolBox 보존, 공유 HMAC을
  사용한 동시 요청의 근거 격리, 타 요청 token 거절, HTTP 429 처리, 기존 Master 회귀.
- 변경 Python 파일 Ruff 통과, `git diff --check` 통과.
- Node 공유 worker wire 왕복 30건과 개별 worker 21건 통과.
- Python/Node 계약/배포 통합 pytest 100건 통과. Linux 운영 시험은 서버에서 진행한다.
- Claude 작업 중인 `subbots/hermes/**`는 이번 Python 구현에서 변경하지 않는다.

## 구현된 TYBot 동작

`specialist_v3_endpoints.EndpointPolicy`가 서버 설정을 시작 시 고정한다.
`TYBOT_HERMES_SHARED_WORKSPACES`는 기본 빈 목록이며
`TYBOT_MASTER_V2_WORKSPACES`의 부분집합만 허용한다. wildcard는 금지한다.
명시한 Hermes 요청만 공유 소켓과 `hermes-shared.hmac`을 선택한다.
나머지 workspace와 다른 specialist는 기존 개별 연결을 유지한다.

공유 credential identity는 `hermes:shared`다. 실제 request.workspace,
authorization_id, 요청별 ToolBox/ACL은 변경하지 않는다. 기존 ToolSessionRegistry가
request별 token/근거/만료 상태를 관리한다. 연결 실패·서명 실패·HTTP 429에는 다른
worker로 재시도하지 않는다. Claude에게 넘긴 서비스 이름/소켓/인증은 다음과 같다.

| 항목 | 값 |
|---|---|
| service | `hermes-rpc-shared.service` |
| 받기 소켓 | `/run/tybot-subbots/hermes-shared/http.sock` |
| 도구 소켓 | `/run/tybot/specialist-tools.sock` |
| TYBot HMAC | `/etc/tybot/credentials/specialist-v3/hermes-shared.hmac` |
| worker HMAC | `/etc/hermes-rpc-shared/hmac` |

## 통합 후 서버 적용 순서

1. Claude handback의 Node 설정과 systemd 239 지원을 리뷰하고 양쪽 회귀시험을 실행한다.
2. 공유 worker 한 계정, 환경파일, workspace profile을 준비한다. TYIT만 먼저 등록한다.
   공유 전용 HMAC은 새로 만들고 양쪽 파일에 같은 바이트를 설치한다. 기존 TYIT 키는 유지한다.
3. shared worker를 시작하고 소켓 소유권·TYBot 접근·서명된 health를 검사한다.
   이후 TYBot 시작 시 로그에서 `shared_hermes=tyit`가 보이는지 확인한다.
4. TYBot env의 기존 항목을 편집한다. 같은 변수를 중복 append하지 않는다.

```text
TYBOT_MASTER_V2_WORKSPACES=tyit
TYBOT_HERMES_SHARED_WORKSPACES=tyit
TYBOT_SPECIALIST_V3_HMAC_DIR=/etc/tybot/credentials/specialist-v3
```

5. TYBot을 재시작하고 새 스레드 질문에서 shared service 완료 로그와
   `specialist_call`의 `phase=external-v3`, `result=success`, 사용 근거를 확인한다.
6. MGMT profile과 배정/라우트를 준비한 뒤 두 목록에 mgmt를 명시적으로 추가한다.
   TYIT/MGMT 동시 요청과 워크스페이스 범위, 각자의 자료·출처를 검증한다.
7. pilot/invest/archi/civil을 한 곳씩 추가한다. 배정과 route=active만으로는 부족하며
   worker profile과 Master/shared 목록까지 일치해야 한다. 수집 상태는 별도로 검증한다.
8. 모든 대상의 운영 시험 뒤 shared service 자동 시작을 활성화한다. 기존 개별 서비스는
   사용 여부를 확인해 중지/disable하되 credential과 설정은 롤백 기간 보존한다.

## 롤백

TYIT를 개별 worker로 되돌리려면 기존 `hermes-rpc@tyit`의 정상 health를 먼저 확인하고
shared 목록에서 tyit만 제거한 뒤 TYBot을 재시작한다. Master v2 목록은 유지하므로
이전 개별 PF Hermes RPC 경로로 돌아간다. 기존 worker가 준비되지 않은 새 workspace는
shared 목록만 제거하면 없는 개별 소켓을 호출하게 된다. 해당 workspace의 Master 전환
설정과 배정/라우트도 검토해 알려진 이전 상태로 복구해야 한다.

shared worker 한 프로세스를 재시작하면 연결된 전체 workspace에 영향을 준다.
전체 health는 모델 응답 성공이나 workspace별 profile 준비를 증명하지 않는다.
실제 도구 호출을 포함한 질문과 출처 확인까지 운영 검증으로 기록한다.
