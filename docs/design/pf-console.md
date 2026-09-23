# PF 운영 콘솔 (`/pf/`) — 읽기 전용 상태 화면

> **보류 (2026-09-23 오너 결정).** 이 화면은 더 진행하지 않는다.
>
> [Archiving Bot 분리와 Hermes 직접 호출](archiving-bot-separation-2026-09-23.md)로
> 계획이 바뀌면서 **전제가 사라졌다.** 이 화면은 우리 서버에 뜬 standalone
> pf-hermes 의 `health.json` 을 보는 구조인데, 새 계획에서 Hermes 는 자기 수집·
> Git writer 없이 TY DMZ 의 **전문 봇**으로 돈다. 볼 상태 파일이 생기지 않는다.
>
> 같은 문서 §5 가 「외부에서 접근 가능한 새 포트는 열지 않는다」 고도 정했다.
>
> 앞으로는 **TYBot 콘솔(8787)에 통합**한다 — PF 팀 최신 git 반영도 기존 전문 봇
> 화면에서 한다. 아래 내용은 그때 재사용할 판단만 남기고 둔다. 특히
> **상태 파일 allowlist**(§4)와 **서비스별 권한을 역할과 분리하는 것**(§3)은
> 대상이 Archiving Bot·Hermes 실행본으로 바뀔 뿐 그대로 쓸 수 있다.

> 작성: 2026-09-22
> 범위: 오너 계획 [`pf-hermes-owner-plan.md`](pf-hermes-owner-plan.md) §7~§12 의 3단계
> 상태: 구현 완료 · 서버 배포·smoke 대기
> 관련: BACKLOG B-35(TLS), B-64

## 1. 오늘 만든 것과 만들지 않은 것

| 만든 것 | 만들지 않은 것 |
|---|---|
| B-35 TLS·reverse proxy, 두 콘솔 모두 루프백 바인딩 | 외부에서 직접 닿는 포트 |
| `pf-hermes-console.service` — 별도 계정·env·DB role·쿠키 | TYBot 콘솔에 route 추가 |
| 개요·수집/Git·배치·비용·감사 **조회** API | `restart`·`stop`·archive sync·release activate·rollback |
| 서비스 allowlist 와 역할 네 가지 | 임의 shell, env 원문, secret 조회, 아카이브 편집 |
| 상태 파일 **허용 칸만** 읽기 | 상태 파일을 그대로 렌더링 |

운영 action 을 뺀 것은 일정 때문이 아니다. 고정 helper, 중복 실행 lock, timeout,
append-only 감사가 **먼저** 있어야 한다(§7.3, §11). 그것들 없이 버튼을 열면 결과는
둘 중 하나다 — 「눌러도 아무 일 없음」 이거나 「눌렀는데 되돌릴 수 없음」.

## 2. 왜 별도 프로세스인가

React route 하나를 더 두는 것은 격리가 아니다(§8.1).

```
https://<사내콘솔>/      -> tybot-console.service     (127.0.0.1:8787)  user: tybot
https://<사내콘솔>/pf/   -> pf-hermes-console.service (127.0.0.1:8788)  user: tybot-pf
```

같은 프로세스면 이렇게 된다.

- 같은 `EnvironmentFile` 을 읽는다 → PF 코드가 TYBot 의 `DATABASE_URL`·Slack·Anthropic
  키를 들고 있다
- 같은 DB 연결을 쓴다 → PF 화면의 쿼리 하나가 TYBot 질문·원문 표에 닿는다
- 같은 역할 체계를 쓴다 → TYBot `developer` 가 자동으로 PF 를 본다

셋 다 **화면에는 아무 변화도 없다.** 그래서 문서가 아니라 구조로 막는다.

### 네 겹으로 같은 말을 한다

| 겹 | 무엇이 막나 | 어디 |
|---|---|---|
| 프로세스·계정 | `/etc/tybot/tybot.env` 를 읽을 수 없다 | `User=tybot-pf` |
| systemd | 계정 설정이 틀려도 경로가 안 보인다 | `InaccessiblePaths=` |
| DB role | 쿼리를 잘못 짜도 실패한다 | `pf_console_schema.sql` |
| 코드 | `tybot.*` 를 임포트하지 않는다 | `tests/test_pf_isolation.py` |

한 겹만 두면 그 겹이 틀렸을 때 아무도 모른다. 넷이 같은 말을 하면 하나가 어긋났을 때
나머지가 막고, 그 사실이 시험 실패로 드러난다.

## 3. 권한 — 「로그인됨」 과 「볼 수 있음」 은 다르다

회사 계정(`console_user`)은 공유한다. 같은 사람을 두 번 만들 이유가 없고, 두 벌이면
퇴사자 계정이 한쪽에만 남는다.

**서비스 권한은 공유하지 않는다.** `console_user_service` 에 행이 없으면 TYBot
관리자라도 `/pf/` 에서 아무것도 못 본다(§8.2). 역할은 넷이다.

| 역할 | 오늘 | 나중에 |
|---|---|---|
| `viewer` | 조회 | — |
| `operator` | 조회 | health check, archive sync, restart, stop |
| `developer` | 조회 | release 제출 |
| `approver` | 조회 | release 승인 (자기 것은 못 한다) |

오늘 넷이 전부 조회만 하는데도 지금 만드는 이유: 나중에 역할을 새로 만들면 **그
사이에 들어온 계정이 전부 한 등급 위로 뜬다.**

권한은 세션에 담지 않는다. 담으면 회수해도 그 사람의 쿠키가 만료될 때까지 계속 보인다.
요청마다 DB 에서 다시 읽는 쪽이 느리지만, 이 화면의 요청 수는 사람이 누르는 만큼뿐이다.

### 권한 없음과 없음을 구분하지 않는다
권한 밖 서비스와 존재하지 않는 서비스가 **같은 404 를 같은 문구로** 답한다. 가르면
키를 바꿔 넣어 보는 것만으로 무엇이 존재하는지 알 수 있다. 로그인 실패도 같다 —
「그런 계정 없음」 과 「비밀번호 틀림」 을 가르면 로그인 화면이 회사 계정 명부 조회
도구가 된다.

## 4. 상태 파일 — 허용한 칸만 읽는다

Hermes 가 `<state_dir>/health.json` 에 원자적으로 쓴다(§7.4). **우리가 쓰는 파일이
아니다.**

계약에는 토큰·질문·답변·문서 본문·비공개 채널 이름을 넣지 않기로 돼 있다. 그런데
계약은 코드가 아니다. 실수로 하나 들어가면 우리 화면이 그것을 그대로 사내에 띄운다.

그래서 **읽는 쪽에서 막는다.** `health.FIELDS` 에 있는 칸만 꺼내고 나머지는 통째로
버린다. 금지어를 지우는 방식(denylist)은 쓰지 않는다 — 새 이름의 칸이 하나 생길
때마다 뚫리고, 뚫린 것을 알 방법이 없다.

오류도 같다. 코드는 `ERROR_CODES` 목록으로 접고(그 밖은 `unknown`), 메시지 칸은
읽지 않는다. 예외 메시지에는 경로·식별자·때로는 질문 조각이 섞인다.

### 다섯 가지 상태를 구분한다

| 판정 | 뜻 | 사람이 할 일 |
|---|---|---|
| `unavailable` | 상태 파일이 없다 | 아직 안 붙었거나 경로가 다르다 |
| `unreadable` | 있는데 못 읽는다(권한·깨짐) | 권한이나 기록 코드를 본다 |
| `incomplete` | 읽었는데 계약 항목이 없다 | 프금팀에 그 칸을 요청한다 |
| `stale` | 칸은 다 있는데 기록이 낡았다 | 프로세스가 멈췄는지 본다 |
| `ok` | 최신이고 칸이 다 있다 | 없음 |

빈 화면 하나로 뭉뚱그리면 다섯이 똑같이 보인다. 그러면 **아직 안 붙은 것과 어제
죽은 것을 구분할 수 없고**, 사람은 둘 다 「원래 그런가 보다」 로 읽는다.

`unavailable` 을 경고색으로 칠하지 않는 이유도 같다. 이관 전까지는 계속 그 상태이고,
빨간 화면이 며칠 이어지면 사람은 색을 안 보게 된다.

판정과 함께 **이유와 파일 경로, 마지막 기록 시각**을 같이 낸다. 판정만 보이면 임계값이
틀렸을 때 알 방법이 없다.

## 5. 화면

한 서비스에 대해 다섯 화면. 넷은 **같은 상태 파일 한 장**에서 나온다 — 파일을 네 번
읽으면 개요는 정상인데 비용 화면만 「멈춤」 인 상황이 생기고, 그때 어느 쪽이 맞는지
판정할 근거가 없다.

| 화면 | 답하는 질문 | 안 하는 것 |
|---|---|---|
| 개요 | 무엇이 돌고 있나. 소유자는 누구인가 | — |
| 수집 · Git | Slack 수집과 자료 저장소가 따라오고 있나 | merge·push·force push |
| 배치 | 정기 작업이 마지막으로 언제 돌았나 | 다른 서비스의 systemd 조작 |
| 비용 | 오늘 모델을 얼마나 썼나 | 상한 설정 — **상한 주인은 프금팀이다**(§7.1) |
| 감사 | 이 화면에서 누가 무엇을 했나 | 질문·답변·본문 |

비용 화면이 「상한 주인은 프금팀」 을 명시하는 이유: 우리가 걸 수 있다고 착각하면
초과했을 때 **아무도 안 막는다.**

개요 맨 아래에 「이 화면에서 할 수 없는 것」 을 적는다. 없는 기능을 찾아 헤매는 것을
막고, 그 작업들이 지금 어디서 이뤄지는지(승인된 수동 runbook) 알려 준다.

## 6. 감사

조회만 여는 오늘도 기록한다. action 을 열 때 감사를 같이 만들면 **그 사이의 조작은
아무 데도 안 남는다.**

`pf_audit_event` 는 append only 다 — PF role 에 `UPDATE`·`DELETE` 권한을 주지 않는다.
본문·시크릿은 담지 않고, 값이 필요하면 식별자만 적는다.

감사 화면은 권한 있는 **서비스**의 기록과 **본인의** 서비스 무관 기록(로그인)을 보여
준다. 남의 로그인 시각은 그 사람의 근무 시간표가 된다.

## 7. B-35 TLS

이 작업의 절반은 PF 가 아니라 TYBot 콘솔이다. 2026-09-01 부터 `--host 0.0.0.0` 로
사내망 전체에 **평문으로** 열려 있었다. 로그인 비밀번호와 세션 쿠키가 매 요청 사내망을
그대로 지나갔고, 그 화면은 아카이브 원문 열람·환경설정 변경·배포 승인을 쥐고 있다.

이제 밖으로 나가는 문은 nginx 하나다.

**순서가 있다.** `CONSOLE_COOKIE_SECURE=1` 을 TLS 보다 먼저 켜면 브라우저가 쿠키를
보내지 않아 로그인이 안 되고, 화면에는 「비밀번호가 틀렸다」 처럼 보인다. 그 상태로
비밀번호를 세 번 바꾸게 된다.

## 7.1 배포가 콘솔을 잠그지 않게 (2026-09-22 사고)

바인딩을 루프백으로 바꾼 커밋이 배포되자 **콘솔에 아무도 접속할 수 없게 됐다.**

`update.sh` → `install.sh` 가 unit 을 복사하고 재시작한다. 그래서 「문을 닫는 쪽」은
자동으로 적용됐는데, nginx 배치는 수동 단계라 「새 문을 여는 쪽」은 따라오지 않았다.
화면에는 「API 서버가 응답하지 않습니다」 만 남았고, 복구하려면 SSH 가 필요했다 —
콘솔을 쓰는 사람이 항상 SSH 를 쓸 수 있는 것은 아니다.

「실패한 배포가 현재 서비스를 중지해서는 안 된다」(오너 계획 §9). 그래서
`install.sh` 의 `console_bind_guard` 가 nginx 가 **실제로 콘솔을 받고 있는지** 본다.

| nginx 상태 | 하는 일 |
|---|---|
| 있고·활성이고·설정이 있고·문법이 맞음 | drop-in 을 지운다. 루프백(목표 상태) |
| 넷 중 하나라도 아님 | drop-in 으로 예전 바인딩 유지 + **크게 알린다** |

조용히 평문으로 되돌리지 않는다. 조용하면 임시 상태가 영구 상태가 된다 — B-35 가
3주 걸린 이유가 그것이다. 경고는 표준오류로 내고, drop-in 파일 안에 끝내는 방법을 적는다.

nginx 를 붙이고 나면 다음 배포에서 drop-in 이 저절로 사라진다. 사람이 지울 것을
기억하지 않아도 된다.

> 화면의 오류 문구도 같이 고쳤다. 전에는 `uvicorn ...` 한 줄을 적어 두었는데 그건
> **개발 PC 기준**이고, 서버에는 `uvicorn` 이 PATH 에 없다. 그날 사람이 그 줄을
> 그대로 붙여 넣고 `command not found` 만 봤다. 틀린 명령은 안내가 아니라 한 단계
> 더 헤매게 만드는 것이다.

## 8. 서버 적용

```bash
# 스키마 → PF DB role → 설치 → 권한. 스크립트가 순서와 확인을 맡는다.
sudo /opt/tybot/deploy/apply-schema.sh

sudo -u postgres psql -p 55432 -d tyslackai -c   "CREATE ROLE tybot_pf_console LOGIN PASSWORD '<암호>';"
sudo /opt/tybot/deploy/apply-schema.sh          # GRANT 가 role 존재를 보고 붙는다

sudo /opt/tybot/deploy/setup-pf-console.sh --dry-run
sudo /opt/tybot/deploy/setup-pf-console.sh
sudo vi /etc/tybot-pf/console.env               # 암호와 세션 키를 넣는다
sudo systemctl enable --now pf-hermes-console
sudo /opt/tybot/deploy/setup-pf-console.sh --verify

# 권한. **이 행이 없으면 로그인은 되는데 아무것도 안 보인다.**
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/pf_grant.py   add <회사이메일> --role viewer
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/pf_grant.py list
```

`setup-pf-console.sh --verify` 가 「빠뜨려도 조용히 실패하는」 셋을 각각 다른 문장으로
말한다. 셋 다 화면에서는 똑같이 「PF 콘솔이 고장났나」 로 보이기 때문이다.

| 빠진 것 | 화면에 보이는 것 |
|---|---|
| `dist-pf` 빌드 | 빈 404 — API 는 뜨는데 화면이 없다 |
| DB role · env 값 | 로그인이 503, 「서버 오류」 |
| `console_user_service` 행 | 로그인은 되는데 「볼 수 있는 서비스가 없습니다」 |

### 주소
- nginx 없이: `http://<서버>:8788/pf/` — **`/pf/` 까지 붙여야 한다.** 앱이 라우트를
  그 아래 등록하므로 8788 루트에는 아무것도 없다
- nginx 를 붙인 뒤: `https://<서버>/pf/`

### 어드민이라는 역할은 없다
TYBot 콘솔의 `admin` 과 PF 의 역할은 **다른 축**이다. PF 에서 무엇을 볼 수 있는지는
`console_user_service` 행이 정하고, TYBot 관리자라도 그 행이 없으면 아무것도 못 본다.

오늘은 역할 넷이 전부 조회만 한다. 그래서 「PF 어드민」 을 만드는 일은
`pf_grant.py add <이메일> --role viewer` 한 줄이다. 나중에 운영 action 을 열 때
`operator`·`approver` 가 갈린다.

## 8.1 옛 절차 (참고)

```bash
# 0. 스키마 (managed_service · console_user_service · pf_audit_event)
sudo /opt/tybot/deploy/apply-schema.sh

# 1. PF DB role
sudo -u postgres psql -p 55432 -d tyslackai -c \
  "CREATE ROLE tybot_pf_console LOGIN PASSWORD '<암호>';"
sudo /opt/tybot/deploy/apply-schema.sh     # GRANT 가 role 존재를 보고 붙는다

# 2. 전용 계정과 설정
sudo useradd --system --no-create-home --shell /sbin/nologin tybot-pf
sudo mkdir -p /etc/tybot-pf && sudo chmod 750 /etc/tybot-pf
sudo vi /etc/tybot-pf/console.env     # 아래 값. 파일 권한 0640, 소유 root:tybot-pf
sudo chown root:tybot-pf /etc/tybot-pf/console.env && sudo chmod 640 /etc/tybot-pf/console.env

# 3. 화면 빌드 (개발 PC에서)
#   cd console-web && npm run build:all      → dist/ 와 dist-pf/
#   서버로 dist-pf/ 를 /opt/tybot/console-web/dist-pf 로 옮긴다

# 4. unit
sudo cp /opt/tybot/deploy/pf-hermes-console.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pf-hermes-console

# 5. TLS (B-35) — 스크립트가 nginx 설치·인증서·SELinux·설정·기동·바인딩 전환까지 한다
sudo /opt/tybot/deploy/setup-nginx-tls.sh --dry-run    # 무엇을 할지 먼저 본다
sudo /opt/tybot/deploy/setup-nginx-tls.sh
#
# 방화벽은 스크립트가 건드리지 않는다 — 지금 좁혀 둔 출발지 범위가 사람이 정한
# 것이기 때문이다. 스크립트 마지막에 명령을 출력하므로 범위를 보고 직접 적용한다.

# 6. **그다음에** Secure 쿠키
#   /etc/tybot/tybot.env   : CONSOLE_COOKIE_SECURE=1
#   /etc/tybot-pf/console.env : PF_CONSOLE_COOKIE_SECURE=1
sudo systemctl restart tybot-console pf-hermes-console
```

`/etc/tybot-pf/console.env`:

```
PF_DATABASE_URL=postgresql://tybot_pf_console:<암호>@127.0.0.1:55432/tyslackai
PF_CONSOLE_SECRET=<openssl rand -base64 32>
PF_CONSOLE_DIST=/opt/tybot/console-web/dist-pf
PF_STATE_ROOT=/var/lib/tybot-subbots

# **TLS 를 붙인 뒤에 1 로 바꾼다.** 지금처럼 nginx 없이 평문으로 여는 동안 1 이면
# 브라우저가 쿠키를 보내지 않아 로그인이 되지 않고, 화면에는 「비밀번호가 틀렸다」
# 처럼 보인다. 그 상태로 비밀번호를 세 번 바꾸게 된다.
PF_CONSOLE_COOKIE_SECURE=0
```

> **nginx 없이 여는 동안**(2026-09-22 결정) 두 콘솔 모두 `0.0.0.0` 으로 열린다.
> `install.sh` 의 안전장치가 drop-in 으로 그렇게 유지하고, nginx 를 붙이면 다음
> 배포에서 저절로 루프백으로 돌아간다. **방화벽에서 8787·8788 출발지를 필요한
> 대역으로 좁혀 두어야 한다** — 평문이므로 세션 쿠키를 주워 담을 수 있다.

### 권한 부여

```sql
INSERT INTO console_user_service (email, service, role, granted_by)
VALUES ('<회사이메일>', 'pf-hermes', 'viewer', '<관리자 이메일>')
ON CONFLICT DO NOTHING;
```

## 9. 배포 검증 (§11.1 6단계)

- [ ] `curl -sI http://<서버>:8787/` 가 연결되지 않는다
- [ ] `curl -sI http://<서버>:8788/` 가 연결되지 않는다
- [ ] `https://<서버>/` 로 로그인되고 쿠키에 `Secure` 가 붙는다
- [ ] `https://<서버>/pf/` 로 PF 계정 로그인 → 쿠키 `pf_console`, `Path=/pf`
- [ ] 상태 파일이 없는 지금, `/pf/` 가 오류가 아니라 **「상태 없음」** 으로 보인다
- [ ] PF 권한이 없는 TYBot 관리자 계정으로 `/pf/` → 「볼 수 있는 서비스가 없습니다」
- [ ] `sudo -u tybot-pf cat /etc/tybot/tybot.env` 가 거부된다
- [ ] `sudo -u tybot-pf ls /var/lib/tybot/archive` 가 거부된다
- [ ] PF DB role 로 `SELECT * FROM raw_line LIMIT 1` 이 권한 오류로 거부된다
- [ ] TYBot 콘솔 회귀 — 로그인·데이터 현황·배포 화면이 그대로 뜬다
- [ ] 로그아웃 뒤 `/pf/` 새로고침이 로그인 화면으로 돌아간다

**하나라도 실패하면 `/pf/` 를 사용자에게 열지 않는다.** 내일 이관은 SSH 와 승인된
수동 runbook 으로 진행한다 — 미완성 콘솔을 시간에 맞춰 공개하는 것보다 콘솔 없이
이관하는 편이 안전하다(§11).

## 10. 다음 (오늘 범위 밖)

1. 고정 배포 helper(§7.3) — 승인 commit 검사 → 검역 clone → lockfile → build/test →
   읽기 전용 release → smoke → symlink 원자 전환 → 실패 시 복원
2. 중복 실행 lock, timeout, action 감사
3. 그 뒤에 `operator` 의 health check·archive sync·restart·stop
4. 그 뒤에 `developer` 제출 · `approver` 분리 승인 · activate · rollback
