# 전문 봇 2단계 - 격리 실행과 배포

> 상태: 구현 대기 설계 확정 (2026-09-10)
>
> 1단계 계약 가져오기: [`specialist-deployment.md`](specialist-deployment.md)  
> 상위 권한 모델: [`bot-hierarchy.md`](bot-hierarchy.md)

## 목적

2단계는 Hermes처럼 Python이 아닌 코드와 자체 처리 로직을 가진 전문 봇을 TYBot과
분리해 실행한다. 전문 봇 개발자는 공개 Git 릴리스 또는 소스 ZIP을 제출하고, TYBot
관리자는 검사 결과와 적용 범위를 승인한다.

소스를 받는 것과 그 소스를 신뢰하는 것은 다른 일이다. 업로드한 코드는 TYBot 프로세스,
배포 스크립트 또는 콘솔 프로세스에서 직접 실행하지 않는다. 검역, 빌드, 계약 검사,
승인, 활성화를 통과한 불변 이미지 하나만 격리 런타임에서 실행한다.

## 확정 결정

| 항목 | 결정 |
|---|---|
| 실행 단위 | 전문 봇 하나당 OCI 컨테이너 하나와 systemd unit 하나 |
| 컨테이너 엔진 | Rocky Linux의 Podman. Docker 소켓은 콘솔에 노출하지 않음 |
| TYBot 연결 | 호스트의 Unix domain socket. 외부 수신 포트를 열지 않음 |
| 운영 산출물 | 태그가 아니라 `sha256:` OCI image digest로 고정 |
| 소스 입력 | 공개 Git의 불변 태그 또는 제한된 소스 ZIP |
| 빌드 위치 | TYBot/콘솔과 분리된 일회성 builder unit. 시크릿과 운영 볼륨 없음 |
| 활성화 | 관리자 승인 후 smoke/contract 검사 통과 시에만 가능 |
| 중지·롤백 | DB 라우팅을 먼저 끄고 이전 승인 digest로 전환. TYBot 재배포 불필요 |
| 런타임 자격 | 전문 봇 전용 자격만 파일로 주입. Slack, DB, 아카이브 자격은 금지 |
| 네트워크 | 기본 차단. 승인된 provider egress profile만 예외 허용 |

Kubernetes 도입을 전제로 하지 않는다. 현재 단일 Rocky 서버에서 구현 가능해야 하며,
나중에 오케스트레이터로 옮겨도 이미지 digest와 HTTP 계약은 그대로 유지한다.

팀이 운영하는 외부 HTTPS endpoint를 TYBot이 직접 호출하는 형태는 이번 2단계 범위가
아니다. 그것은 근거가 다른 서버로 나가고 새 네트워크 경계가 생기므로 별도 승인과 설계가
필요하다.

## 신뢰 경계

```text
브라우저
  -> tybot-console (메타데이터와 업로드만 수신)
       -> quarantine/ (실행 금지, noexec)
       -> DB: 제출·검사·승인 기록

root 소유 deploy helper
  -> 일회성 builder container (운영 시크릿 없음)
       -> source checkout/unpack
       -> dependency/build/test/SBOM
       -> local OCI image store: digest 고정

tybot.service
  -> ACL/PII 필터가 끝난 question + evidence
  -> /run/tybot-subbots/<key>/http.sock
       -> specialist container
  <- text + runtime metadata
  -> 출력 계약 검사 -> 출처는 TYBot이 부착 -> Slack
```

콘솔 사용자에게 Podman 권한, root shell, 임의 systemd unit 작성 권한을 주지 않는다.
콘솔은 DB에 작업을 요청할 뿐이다. 고정된 root helper가 허용 목록에 있는 동작만 수행한다.

## 배포 수명주기

상태는 소스 상태와 런타임 상태를 섞지 않는다.

```text
uploaded
  -> source_rejected
  -> source_verified
       -> building
            -> build_failed
            -> image_ready
                 -> contract_failed
                 -> awaiting_approval
                      -> rejected
                      -> approved
                           -> deploying
                                -> deploy_failed
                                -> standby
                                     -> enabled
                                     -> disabled
                                     -> unhealthy -> disabled
```

1. 개발자가 Git URL+태그 또는 ZIP을 제출한다.
2. 콘솔은 파일 형식과 크기만 검사하고 검역 저장소에 넣는다. 이 단계에서는 실행하지 않는다.
3. deploy helper가 별도 builder에서 source manifest, lockfile, 금지 파일, 라이선스와 취약점,
   계약 테스트를 검사한다.
4. 빌드 결과를 OCI 이미지로 만들고 digest, SBOM, 검사 결과를 DB에 기록한다.
5. 개발자는 적용 워크스페이스와 변경 이유를 포함해 승인 요청을 만든다.
6. 관리자는 source commit/bundle hash, image digest, 검사 결과, 권한 범위를 보고 승인한다.
7. deploy helper가 후보 컨테이너를 `standby`로 띄우고 health와 고정 smoke 요청을 실행한다.
8. 모두 통과하면 DB의 active deployment를 원자적으로 바꾸고 라우팅을 연다.
9. 오류율 또는 연속 실패가 기준을 넘으면 circuit breaker가 즉시 라우팅을 닫는다. 컨테이너
   중지는 뒤따르는 정리이며, 마스터 폴백은 DB 전환 직후부터 적용된다.

요청자와 승인자는 달라야 한다. 관리자가 직접 등록할 필요가 있을 때도 다른 관리자가
승인하며, 단일 관리자 비상 운영은 별도의 break-glass 감사 이벤트로만 허용한다.

## 소스 패키지 계약

2단계 manifest는 1단계 `prompt-contract`와 구분한다.

```toml
schema = "tybot-specialist/v2"
key = "hermes"
name = "Hermes"
domain = "내부 문서"
release_type = "http-service"
contract_version = "v2"
version = "1.2.0"
runtime = "nodejs22"
entrypoint = ["node", "dist/server.js"]
health_path = "/v1/health"
complete_path = "/v1/complete"
network_profile = "anthropic-only"
```

허용 runtime은 코드 상수로 등록한다. 첫 구현은 `nodejs22` 하나만 지원하고, 임의 이미지,
Dockerfile, shell entrypoint, Python 경로 또는 외부 실행 URL 입력은 받지 않는다.

### Git 입력

- 공개 HTTPS GitHub 저장소와 불변 tag만 허용한다. branch와 움직이는 `latest`는 빌드 입력으로
  쓰지 않는다.
- tag가 가리킨 commit을 기록하고 승인 직전에 동일 tag를 다시 조회해 일치시킨다.
- submodule, Git LFS pointer, nested repository는 첫 구현에서 거부한다.
- `package-lock.json`이 필수이며 npm 외 package manager는 첫 구현에서 거부한다.

### ZIP 입력

- 최대 25MB, 파일 2,000개, 압축 해제 합계 100MB, 압축률 100:1로 제한한다.
- ZIP SHA-256을 업로드 중 계산하고 검역 경로는 서버가 생성한 UUID만 사용한다.
- 절대 경로, `..`, symlink/hardlink/device, 암호화 파일, executable bit, 중첩 archive,
  `node_modules`, `.git`, `.env`, key/certificate 파일을 거부한다.
- 1단계의 2MB 계약 ZIP endpoint와 분리한다. 1단계 업로드 제한을 넓히지 않는다.
- 승인 전 원본 ZIP은 다운로드할 수 없고, 관리자에게도 파일명·해시·검사 결과만 보인다.

## 무시크릿 빌드

빌드는 임의 코드 실행이다. 따라서 `tybot-console` 또는 `tybot.service`에서 npm 명령을
실행해서는 안 된다.

- builder는 일회성 컨테이너이며 read-only base image, 임시 workspace와 제한된 CPU/메모리/
  PID/시간을 사용한다.
- `/etc/tybot`, `/var/lib/tybot/archive`, DB socket, Podman socket, 호스트 홈을 mount하지 않는다.
- dependency 다운로드 단계만 npm registry allowlist egress를 허용한다.
- `npm ci --ignore-scripts` 후 정해진 `npm run build`와 `npm test`를 실행한다. build/test 자체도
  불신 코드이므로 동일 sandbox 안에서만 돈다.
- lockfile 불일치, lifecycle script 필요, native addon, 네트워크 테스트는 첫 구현에서 실패다.
- 최종 이미지는 runtime base에 build output과 production dependency만 복사하며 compiler,
  source ZIP, Git metadata를 넣지 않는다.
- 이미지에는 OCI label로 source commit/bundle hash, version, contract version을 기록한다.
- 생성한 SBOM, image digest와 검사 결과만 보관한다. 로그는 시크릿 패턴을 마스킹하고 최대
  크기와 보존 기간을 둔다.

빌드 실패 사유는 정규화한 코드와 마지막 제한 로그만 콘솔에 보인다. 예:
`manifest-invalid`, `lockfile-missing`, `dependency-denied`, `test-failed`, `timeout`,
`vulnerability-blocked`. 빌드 로그에 업로드 소스나 환경변수 전체를 출력하지 않는다.

## 런타임 격리

전문 봇 systemd unit은 템플릿으로 고정하고 DB나 업로드에서 unit 내용을 만들지 않는다.

```text
tybot-specialist@hermes.service
  container: localhost/tybot-specialist/hermes@sha256:<digest>
  socket:    /run/tybot-subbots/hermes/http.sock
  user:      specialist-hermes (TYBot/console과 다름)
```

필수 격리 기준:

- read-only root filesystem, tmpfs `/tmp`, `no-new-privileges`, capability 전부 제거
- host PID/IPC/user namespace 공유 금지, privileged 금지
- CPU 1, 메모리 512MB, PID 128, 요청 본문 256KB, 응답 64KB 상한
- 호스트 경로 mount 금지. 예외는 해당 봇의 socket 디렉터리와 읽기 전용 secret 파일 하나
- Slack 토큰, `DATABASE_URL`, workspace encryption key, archive 경로를 환경에 넣지 않음
- journald 로그에는 request/evidence/answer를 남기지 않음. request ID와 오류 코드만 허용
- restart rate limit과 watchdog 적용. 반복 장애는 `unhealthy` 처리

`network_profile=none`이 기본이다. `anthropic-only` 같은 profile은 인프라가 목적지 allowlist를
실제로 강제할 수 있을 때만 활성화한다. 단순 DNS 이름 확인만으로는 우회 가능하므로 profile
강제 수단이 준비되지 않으면 HTTP 서비스 배포 자체를 막는다.

## HTTP 와이어 계약 v2

transport는 Unix socket 위 HTTP/1.1이다. 전문 봇은 TYBot API를 호출하지 않는다.

### 요청

```json
{
  "schema": "tybot.specialist.request/v2",
  "request_id": "uuid",
  "authorization_id": "opaque-id",
  "workspace": "tyit",
  "question": "질문",
  "evidence": [
    {"id": "e1", "text": "권한 및 PII 필터를 통과한 원문 일부"}
  ],
  "limits": {"deadline_ms": 15000, "max_output_chars": 20000}
}
```

- 동일 요청의 evidence는 한 workspace와 한 `authorization_id`만 사용한다.
- 파일 경로, Slack URL, 사용자 토큰, 채널 ID, 사용자 이메일을 보내지 않는다.
- evidence `id`는 요청 안에서만 유효한 임의 식별자다.
- 요청 본문은 로그와 `specialist_call`에 저장하지 않는다.

### 성공 응답

```json
{
  "schema": "tybot.specialist.response/v2",
  "request_id": "uuid",
  "text": "전문가가 작성한 본문",
  "version": "1.2.0",
  "contract_version": "v2",
  "evidence_ids": ["e1"],
  "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": null}
}
```

TYBot은 schema, request ID, 활성 digest의 기대 버전, 길이, UTF-8, evidence ID를 검사한다.
`evidence_ids`는 비어 있지 않은 부분집합이어야 하며, TYBot만 그 ID를 원래 출처 링크로
변환한다. 응답 버전은 관찰값일 뿐 DB의 승인 버전을 변경하지 않는다.
`text`에 `출처:`, Slack archive URL, `file://`, HTML/script, 알 수 없는 링크가 있으면 계약
위반으로 폐기한다. `usage`는 관찰용이며 TYBot 비용 상한 계산에는 사용하지 않는다.

오류 응답은 `{"error":{"code":"..."}}` 형태의 허용 코드만 받는다. stack trace와 내부
메시지는 응답하지 않는다. 4xx/5xx, JSON 오류, version 불일치, 15초 초과는 모두 마스터
폴백으로 처리한다. 재시도는 하지 않는다. 사용자가 보낸 한 질문이 전문 봇에서 중복 처리되면
비용과 지연이 늘기 때문이다.

## 인증과 시크릿

Unix socket 권한이 1차 인증이다. 여기에 요청별 HMAC을 추가해 잘못된 로컬 프로세스가
전문 봇을 호출하는 것을 막는다.

- 전문 봇별 32-byte shared secret을 기존 workspace Fernet key로 암호화해 DB에 저장한다.
- 런타임에는 root helper가 `/run/credentials/...` 파일로 주입한다. 환경변수로 전달하지 않는다.
- 서명 대상은 timestamp, nonce, method, path와 request body SHA-256이다.
- 허용 시계 오차 30초, nonce 재사용 금지. 응답도 request ID와 body hash로 서명한다.
- 콘솔에는 mask, 생성일, 교체일만 보이고 복호화 API는 만들지 않는다.
- 교체 시 구/신 키를 최대 5분 겹쳐 무중단 전환한 뒤 구 키를 폐기한다.

전문 봇 자체 provider key가 필요한 경우에도 별도 암호문으로 저장해 해당 컨테이너에만
credential file로 넣는다. TYBot의 공용 LLM 키를 전문 봇에 주지 않는다.

## DB 변경

기존 `specialist_bot`, `specialist_change_request`, `specialist_call`은 유지하고 다음 표를
추가한다. endpoint URL을 사람이 입력하는 방식은 쓰지 않는다. socket 경로는 key에서
코드가 계산한다.

```text
specialist_source
  id, specialist, source_type(git|zip), repository_url, release_ref,
  source_commit, source_name, bundle_sha256, quarantine_key,
  status, submitted_by, submitted_at

specialist_build
  id, source_id, runtime, status, image_digest, sbom_sha256,
  checks(jsonb), error_code, started_at, finished_at

specialist_deployment
  id, specialist, build_id, state(standby|active|retired|failed),
  deployed_by, deployed_at, health, error_code, last_checked_at

specialist_runtime_secret
  specialist, kind(hmac|provider), ciphertext, mask, enabled,
  updated_at, updated_by
```

`specialist_bot`에는 `execution_mode`(`prompt|http`)와 `active_deployment_id`를 추가한다.
`execution_mode=http`이고 active deployment가 없거나 health가 정상이 아니면 라우터 후보에서
제외한다. 삭제 대신 disable/retire를 사용한다.

`specialist_call`에는 `deployment_id`, `runtime_version`, `http_status`, `fallback_reason`을
추가한다. 질문, evidence, 응답 본문, HMAC, provider key는 저장하지 않는다.

## 콘솔 API와 권한

기존 1단계 API와 혼동되지 않게 runtime endpoint를 분리한다.

```text
POST /api/specialist-runtime/sources/git
POST /api/specialist-runtime/sources/upload
GET  /api/specialist-runtime/sources/{id}
POST /api/specialist-runtime/sources/{id}/build
GET  /api/specialist-runtime/builds/{id}
POST /api/specialist-runtime/builds/{id}/request-approval
POST /api/specialist-runtime/requests/{id}/approve
POST /api/specialist-runtime/requests/{id}/reject
POST /api/specialist-runtime/deployments/{id}/activate
POST /api/specialist-runtime/specialists/{key}/disable
POST /api/specialist-runtime/specialists/{key}/rollback
POST /api/specialist-runtime/specialists/{key}/check
```

- 개발자: 자기 제출물 업로드, 빌드 요청, 검사 결과 열람, 승인 요청
- 관리자: 승인/반려, 활성화, 중지, 롤백, 시크릿 교체
- 게스트: 상태와 비민감 버전 정보만 열람
- workspace scope를 제출, 승인, 라우팅 조회 모든 단계에서 다시 검사한다.
- 상태 변경은 CSRF 보호와 append-only `console_audit_event`를 거친다.
- 감사 metadata에는 source/build/deployment ID, digest, 상태, 오류 코드만 기록한다.

콘솔 화면은 기존 전문 봇 관리 안에 `계약형`과 `실행형` 탭을 둔다. 실행형 상세에는 소스,
빌드, 승인, 배포, health의 진행 상태와 이전 digest 롤백 버튼을 한 흐름으로 표시한다.
임의 명령, 환경변수, Dockerfile, endpoint URL을 입력하는 UI는 만들지 않는다.

## 헬스와 자동 차단

- `/v1/health`는 모델 호출 없이 2초 안에 version, contract version, status만 반환한다.
- TYBot 시작 시와 1분마다 active specialist를 확인한다.
- 3회 연속 transport 실패 또는 최근 5분 계약 위반률 20% 이상이면 circuit을 연다.
- circuit이 열리면 새 요청은 전문 봇을 호출하지 않고 즉시 마스터로 간다.
- 자동 복구는 health 3회 성공 후 `standby`까지만 한다. `enabled` 복귀는 관리자 동작이다.
- 콘솔에는 마지막 성공 시각, 연속 실패, p50/p95 지연, 폴백률, active digest를 표시한다.

## 실패와 롤백 원칙

| 실패 | 동작 |
|---|---|
| 소스/빌드 검사 실패 | 실행하지 않고 제출물 상태만 실패로 변경 |
| 배포 smoke 실패 | 기존 active 유지, 후보만 failed |
| 런타임 timeout/5xx | 해당 요청은 마스터 폴백, 오류 코드 기록 |
| 계약 위반 | 응답 폐기, 마스터 폴백, 위반률 누적 |
| 반복 장애 | 라우팅 disable, 운영 대시보드 경고 |
| 새 버전 품질 저하 | 이전 승인 digest로 원자적 롤백 |
| TYBot 재시작 | DB active digest와 unit 상태를 reconcile |

전문 봇 장애 때문에 Slack 답변 전체가 실패해서는 안 된다. 배포 helper 실패도 현재 active
전문 봇과 TYBot을 중지하지 않아야 한다.

## 구현 순서

Claude는 아래 순서로 작은 커밋을 만든다. 각 단계는 그 단계의 테스트가 통과해야 다음으로
넘어간다.

1. **계약과 DB**: v2 request/response 모델, schema migration, store, 역할별 API 테스트.
2. **HTTP adapter**: Unix socket client, HMAC, 엄격한 response validator, timeout/fallback,
   circuit breaker. fake Unix server로 계약 테스트.
3. **런타임 템플릿**: 고정 systemd/Podman unit과 root helper. arbitrary argument 거부 테스트.
4. **소스 검역**: Git tag와 ZIP 제한, quarantine metadata, 해시, 정리 timer.
5. **격리 builder**: `nodejs22` allowlisted pipeline, resource/network 제한, SBOM과 digest 기록.
6. **승인·배포**: standby smoke, active 전환, disable/rollback, startup reconcile.
7. **콘솔 UI**: 실행형 탭, 진행 상태, 검사 로그, 승인, health, 롤백.
8. **Hermes pilot**: TYIT 한 workspace에서 shadow 호출 후 수동 활성화. 전사 동시 활성화 금지.

## 필수 검증

### 단위·계약 테스트

- 서로 다른 authorization ID 또는 workspace의 evidence 혼합 거부
- 전문 봇 응답의 자체 출처, URL, 알 수 없는 evidence ID, 과대 응답 거부
- timeout, invalid JSON, 4xx/5xx, version 불일치에서 마스터 폴백
- 비활성·미승인·unhealthy digest가 라우팅 후보에 포함되지 않음
- HMAC 위조, 만료, nonce 재사용 거부
- call/audit/build 로그에 질문·근거·답변·시크릿이 남지 않음

### 배포 보안 테스트

- ZIP path traversal, symlink, archive bomb, `.env`, key, nested archive 거부
- branch, mutable tag, submodule, lockfile 없음, lifecycle script 의존, native addon 거부
- build 컨테이너에서 `/etc/tybot`, archive, DB, Podman socket 접근 실패
- runtime 컨테이너에서 Slack/DB/archive 접근 실패
- 허용되지 않은 인터넷 목적지 접근 실패
- CPU, 메모리, PID, 로그, build time 제한 확인

### 운영 테스트

- 새 후보 실패 시 기존 active digest가 계속 응답
- disable 직후 새 호출이 마스터로 폴백
- 이전 digest 롤백과 TYBot 재시작 후 상태 reconcile
- 전문 봇 프로세스 kill, socket 제거, malformed response에서 Slack 답변 유지
- 데스크톱 콘솔에서 개발자/관리자/게스트별 버튼과 데이터 범위 확인

전체 `pytest`, `ruff`, 프론트엔드 타입 검사와 빌드를 통과해야 한다. 실제 Podman 격리 검사는
Rocky staging 서버의 별도 통합 테스트로 두고, 통합 테스트 없이 `enabled` 전환을 허용하지
않는다.

## 이번 단계에서 하지 않는 것

- 전문 봇 소스를 TYBot 저장소 또는 `/opt/tybot/src`에 복사
- 업로드 ZIP을 콘솔 프로세스에서 압축 해제하거나 실행
- 사용자 Dockerfile, shell command, 임의 endpoint URL 허용
- 전문 봇이 Slack, PostgreSQL, Oracle, 중앙 아카이브를 직접 조회
- 전문 봇이 출처, 접근 권한 또는 최종 Slack 표시 형식을 결정
- 전문 봇 출력의 아카이브 저장 또는 학습 데이터 재사용
- 외부 공개 포트 추가

## 구현 전 인프라 게이트

다음 세 항목이 확인되지 않으면 5단계 이후를 구현해도 운영 활성화하지 않는다.

1. Rocky 운영 서버에서 Podman과 systemd 연동을 사용할 수 있는가.
2. 컨테이너별 egress allowlist를 실제로 강제할 방화벽/프록시가 있는가.
3. `/var/lib/tybot/subbot-quarantine`, image store, build log의 용량과 보존 정책이 정해졌는가.

이 게이트는 새 외부 포트를 여는 승인이 아니다. TYBot과 전문 봇 사이 통신은 Unix socket으로
끝내며, 외부 접근이 필요한 provider egress만 별도로 승인한다.
