# Hermes 상시 운영 — Google Cloud

> 노트북 대신 GCP VM 한 대에서 24시간 돌립니다. `e2-micro` 무료 등급이라 **추가 비용은 사실상 $0**입니다.
> 명령은 전부 브라우저의 **Cloud Shell**에 붙여넣습니다 — PC에 설치할 것이 없습니다.

## 구조

```
[노트북 — Claude Code]                  [GCP e2-micro · us-west1 · 24시간]
  slack-sync 로 아카이브 갱신              Hermes (systemd, 죽으면 자동 재시작)
        │                                      │
        │                                   /opt/hermes/code     ← 코드 저장소 (읽기)
        │                                   /opt/hermes/archive  ← 자료 저장소 (읽고 쓰기)
        │                                      │
        └── git push ──> GitHub ── git pull ───┤  자료만 15분마다 자동
                        (둘 다 private,        │  (코드는 사람이 pull + 재시작)
                         deploy key 각각)      ├──> Slack (Socket Mode, 나가는 연결만)
                                               └──> Anthropic API
```

**저장소가 둘입니다.** 코드(`hermes`)는 팀끼리 나눠 쓰고, 자료는
팀마다 따로입니다. VM 은 코드를 읽기만 하고, 자동 반영이 커밋·push 하는 곳은 자료뿐입니다.

**인바운드 포트가 하나도 필요 없습니다.** Socket Mode는 봇이 슬랙으로 나가는 연결이라, 방화벽을 전부 닫아도 동작합니다.

---

## 준비물

- GCP 프로젝트 (결제 계정 연결됨)
- GitHub 저장소 **둘 다 private** (코드 `hermes` · 자료는 팀마다 따로)
  - **자료 저장소는 비어 있으면 안 됩니다.** 아래 ⑤ 가 빈 저장소를 받으면 거기서 멈춥니다.
    노트북에서 `npm run init-archive` 로 틀을 넣고 push 한 뒤에 여기를 시작하세요
    (코드 저장소 `README.md` 의 「3. 자료 저장소 만들기」).
- 토큰 3종 (`xoxb-`, `xapp-`, `sk-ant-`) — 노트북 `.env`에 있는 것 그대로

---

## ⚠ 먼저 — 아래 명령의 이 셋을 **자기 값으로 바꾸세요**

이 문서의 명령은 **TEC 프로젝트금융 팀 값이 박힌 채로** 적혀 있습니다. 그대로 복사하면
남의 GCP 프로젝트에 VM 을 만들려 하고, 권한이 없어 실패합니다 — 그런데 **그 실패 메시지가
「내 값이 아니라서」라고 말해주지 않습니다.**

| 이 문서에 적힌 것 | 뜻 | 바꿀 것 |
|---|---|---|
| `whk-hermes` | GCP **프로젝트 ID** | 자기 프로젝트 ID |
| `us-west1-b` | VM 을 둘 **존** | 아래 무료 리전 안에서 고른 존 |
| `hermes` (gcloud 명령의) | **VM 이름** | 아무 이름이나. 바꾸면 모든 명령에서 함께 바꾸세요 |
| `wkimclementia/hermes`<br>`wkimclementia/hermes-archive-tec` | 저장소 주소 | 자기 저장소 — **④·⑤·⑥ 세 곳**에 나옵니다 |

> **앞 셋은 `.env` 에도 적으세요** — `GCP_VM`·`GCP_PROJECT`·`GCP_ZONE`.
> `npm run deployed`(배포 확인)가 그것으로 VM 에 물어봅니다. 안 적으면 그 검사만
> 「못 쟀습니다」로 건너뛰고 나머지는 정상입니다.

> **저장소 주소를 안 넘기면 원저자 저장소를 봅니다.** `setup.sh` 의 기본값이 그것이라,
> 인자 없이 돌리면 **남의 private 저장소**를 읽으려다 실패합니다. 그때 안내가
> 「주소가 틀렸다」고 말해주지 않고 **볼 수도 없는 저장소의 키 등록 화면**을 가리킵니다
> (2026-08-31 에 고쳐서, 이제 기본값을 쓰는 중이면 그 사실을 함께 적습니다).

**리전만은 아무거나 고르면 안 됩니다** — `us-west1`·`us-east1`·`us-central1` 밖에서는
`e2-micro` 가 무료가 아닙니다(②).

**봇 소스와 `config.json` 에는 이 값들이 한 글자도 없습니다.** 봇은 자기가 어느 GCP
프로젝트에서 도는지 모르고 알 필요도 없습니다 — 바꿀 곳은 이 문서의 명령뿐입니다.

---

## ① Cloud Shell 열기

<https://console.cloud.google.com> 접속 → 우측 상단 **터미널 아이콘(>_)** 클릭.

```bash
gcloud config set project whk-hermes
gcloud services enable compute.googleapis.com iap.googleapis.com --project=whk-hermes
```

> **`whk-hermes` 는 TEC 프로젝트금융 팀의 프로젝트 ID 입니다 — 자기 것으로 바꾸세요**(위 표).
>
> **`--project` 를 빼지 마세요.** 아래 gcloud 명령에 전부 박아둔 이유가 있습니다. 플래그를
> 빼면 그 계정의 **기본 프로젝트**로 갑니다(우리 계정의 기본값은 `whk-workspace` 였습니다).
> 기본 프로젝트에 Compute Engine API 가 꺼져 있으면 **명령이 실패하며 "지금 켤까요? (y/N)"
> 을 묻는데, 여기서 `y` 를 누르면 안 됩니다** — 엉뚱한 프로젝트에 VM·방화벽 작업이 실제로
> 실행됩니다. 기본값 `N` 그대로 엔터하고 `--project` 를 붙여 다시 실행하세요.

## ② VM 만들기

**리전을 바꾸지 마세요.** `us-west1`·`us-east1`·`us-central1` 밖에서는 무료가 아닙니다. 디스크도 `pd-standard`여야 합니다.

```bash
gcloud compute instances create hermes \
  --project=whk-hermes \
  --zone=us-west1-b \
  --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-type=pd-standard --boot-disk-size=20GB \
  --tags=hermes
```

## ③ 방화벽 잠그기

IAP(구글 내부 경로)로만 SSH가 되게 하고, 인터넷에서 오는 접속은 전부 막습니다.

```bash
gcloud compute firewall-rules create allow-iap-ssh-hermes \
  --project=whk-hermes \
  --network=default --direction=INGRESS --action=ALLOW \
  --rules=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=hermes
```

기본 규칙은 인터넷 전체(`0.0.0.0/0`)에 22번 포트를 열어둡니다. 지웁니다.

```bash
gcloud compute firewall-rules delete default-allow-ssh default-allow-rdp --project=whk-hermes
```

> **이 프로젝트에 다른 VM이 있다면** 그 VM의 SSH도 함께 막힙니다. Hermes 전용 프로젝트가 아니라면 지우지 말고, 대신 그 규칙의 source-range를 좁히세요.

## ④ 부트스트랩 스크립트 올리기

저장소가 private이라 VM이 아직 받을 수 없습니다. Cloud Shell을 거쳐 넣습니다.

```bash
gh auth login          # 브라우저로 GitHub 인증 (한 번만)
gh repo clone wkimclementia/hermes            # ← 자기 코드 저장소로 바꾸세요
gcloud compute scp \
  hermes/deploy/setup.sh \
  hermes:~/ --project=whk-hermes --zone=us-west1-b --tunnel-through-iap
```

> `gh repo clone` 뒤의 것도 **자기 코드 저장소**입니다. 여기서 가져오는 것은
> `setup.sh` 한 파일뿐이라 남의 것으로 받아도 다음 단계가 굴러가긴 하지만,
> 그 저장소를 못 읽으면 이 줄에서 막힙니다.

## ⑤ 접속해서 실행

```bash
gcloud compute ssh hermes --project=whk-hermes --zone=us-west1-b --tunnel-through-iap
```

```bash
sudo bash ~/setup.sh \
  git@github.com:wkimclementia/hermes.git \
  git@github.com:wkimclementia/hermes-archive-tec.git
```

**두 주소를 자기 저장소로 바꾸세요** (앞이 코드, 뒤가 자료). 이 인자를 빼면 위 표의
원저자 저장소로 갑니다. 주소는 `git@github.com:소유자/이름.git` 모양이어야 합니다 —
다른 모양이면 `setup.sh` 가 별칭을 못 붙였다고 경고하고, 그러면 deploy key 가 안 붙습니다.

Node 설치·스왑·전용 계정 생성까지 하고, **GitHub deploy key(공개키) 두 개를 출력한 뒤 멈춥니다.**

## ⑥ deploy key 등록 — 저장소마다 하나씩, 둘입니다

저장소가 둘이라 키도 둘입니다. **GitHub 은 같은 deploy key 를 두 저장소에 등록하지 못합니다**
("Key is already in use"). `setup.sh` 가 키를 두 개 만들고, 어느 키로 어느 저장소에 붙을지는
`/opt/hermes/.ssh/config` 의 별칭 호스트(`hermes-code`·`hermes-data`)가 가릅니다.

| | 코드 저장소 | 자료 저장소 |
|---|---|---|
| 등록할 곳 | <https://github.com/wkimclementia/hermes/settings/keys> | <https://github.com/wkimclementia/hermes-archive-tec/settings/keys> |
| | ↑ **자기 저장소 주소로.** ⑤ 의 출력이 넘긴 주소에서 만든 정확한 링크를 찍어 주니 그것을 쓰세요 | |
| Title | `hermes-vm-code` | `hermes-vm-data` |
| Key | ⑤에서 출력된 **1)** 아래 한 줄 | ⑤에서 출력된 **2)** 아래 한 줄 |
| Allow write access | **체크 안 함** | **체크** |

**자료 저장소에만 쓰기를 켜는 이유**: 자동 반영(ingest)이 커밋·push 하는 곳이 거기뿐입니다.
코드는 VM 이 읽기만 합니다(배포는 사람이 `git pull` + `systemctl restart`).

| | 자료 쪽 체크 안 함 | 체크 함 |
|---|---|---|
| Q&A·요약·위생 점검 | 동작 | 동작 |
| 아카이브 자동 반영(ingest) | **안 됨** — 커밋해도 push 를 못 합니다 | 동작 |
| VM 이 뚫렸을 때 | 저장소를 못 고칩니다 | 자료 저장소를 고칠 수 있습니다 |

자동 반영을 쓰려면 켜야 합니다. 켠 뒤에도 VM 은 `main` 에만 올리고 force push 는 쓰지 않으며,
인바운드 포트가 없고 SSH 는 IAP 로만 열려 있습니다(③).

**나중에 재구축할 때 이 항목을 놓치면 자동 반영이 에러 없이 멈춥니다** — 커밋은 되고 push 만
실패하는데, 그러면 봇은 계속 옛 자료로 답합니다. DM 보고에 "push 가 안 됐습니다" 가 뜹니다.

등록 후 VM에서 **⑤ 와 똑같이 인자를 붙여** 다시 실행합니다. 이번엔 저장소를 받고 의존성까지
설치합니다. **인자를 빼면 이번에는 원저자 저장소를 보게 되어, 방금 등록한 키가 소용없습니다.**

```bash
sudo bash ~/setup.sh \
  git@github.com:wkimclementia/hermes.git \
  git@github.com:wkimclementia/hermes-archive-tec.git
```

## ⑦ 토큰 넣고 시작

```bash
sudo nano /opt/hermes/code/.env
```

`=` 뒤 예시값을 지우고 실제 토큰을 붙여넣습니다 (따옴표·공백 없이). `Ctrl+O` → `Enter` → `Ctrl+X`로 저장.
**`HERMES_DATA_ROOT` 줄은 `setup.sh` 가 이미 적어 두었습니다 — 지우지 마세요.** 그 줄이 없으면
봇이 자료 저장소를 못 찾고, `npm run check` 가 한 줄도 못 찍은 채 스택 트레이스만 냅니다.

```bash
cd /opt/hermes/code
sudo -u hermes npm run check      # 6절 전부 ✓ 확인
sudo systemctl enable --now hermes
```

---

## ⚠️ 노트북 봇을 반드시 끄세요

같은 토큰으로 두 프로세스가 붙으면 **질문마다 두 번 답하고 요약도 두 번** 나갑니다. API 비용도 두 배입니다.

노트북에서 `npm start`가 돌던 창을 찾아 `Ctrl+C`. 작업 스케줄러에 등록해 두셨다면 그것도 해제하세요.

---

## 운영

| 하고 싶은 것 | 명령 |
|---|---|
| 실시간 로그 | `journalctl -u hermes -f` |
| 오늘 로그만 | `journalctl -u hermes --since today` |
| 상태 확인 | `systemctl status hermes` |
| 재시작 | `sudo systemctl restart hermes` |
| 설치 점검 | `cd /opt/hermes/code && sudo -u hermes npm run check` |
| 아카이브 즉시 동기화 | `sudo systemctl start hermes-archive-pull` |
| 동기화 이력 | `systemctl list-timers hermes-archive-pull` |
| 위생 점검 미리보기 | `cd /opt/hermes/code && sudo -u hermes npm run health -- --dry` |

**위생 점검**은 매일 09:00 에 「대화 동기화 N일 밀림 / 미변환 첨부 N건」을 본인 DM 으로
보냅니다(이상 없으면 안 보냄). 이 VM 에서 돌지만 **git 에 쓰지 않습니다** — 상태 파일 두 개를
읽고 슬랙 조회 API 만 쓸 뿐입니다.

**16:00 에 한 번 더 돕니다** (2026-08-06 부터, `healthPre`). 이쪽은 첨부만 보고 최근 2일만
훑습니다 — 일일 요약(17:30)이 그날 올라온 문서의 본문까지 싣게 되면서, 「지금 `doc-archive` 를
돌려 push 하면 오늘 요약에 실린다」를 알리는 자리가 필요해졌기 때문입니다. 미리보기는
`npm run health -- --dry --pre`.

> **deploy key 는 쓰기 권한이 있어야 합니다** (2026-08-04 부터). 위생 점검은 git 에 안 쓰지만,
> 같은 VM 의 **아카이브 자동 반영(매일 07:00 과 17:00)이 커밋·push 를 합니다.** 읽기 전용으로 되돌리면
> 자동 반영이 push 단계에서 막히고, 그 사실은 봇 DM 에만 뜹니다.
> 확인: `sudo -u hermes git -C /opt/hermes/archive push --dry-run origin main`

접속은 언제나 Cloud Shell에서:

```bash
gcloud compute ssh hermes --project=whk-hermes --zone=us-west1-b --tunnel-through-iap
```

### 아카이브 갱신 흐름

```
노트북에서 Claude Code → "슬랙 동기화" (slack-sync 스킬)
  → git commit + push
     → VM 이 15분 안에 자동으로 pull
```

**push를 안 하면 VM의 아카이브는 갱신되지 않습니다.** 이 구성의 유일한 수동 고리입니다.

다만 "최근", "이번 주" 같은 질문은 봇이 `fetch_recent_slack`으로 슬랙을 직접 조회하므로 아카이브 신선도와 무관하게 답합니다.

### 코드를 고쳤을 때

아카이브 md는 mtime을 보고 다시 읽히므로 **재시작이 필요 없습니다.** 하지만 소스를 고쳐 push했다면 반영하려면 받아온 뒤 재시작해야 합니다.

**자동 pull 은 자료 저장소만 받습니다.** 코드는 사람이 두 줄을 칩니다.

```bash
sudo -u hermes git -C /opt/hermes/code pull   # 코드를 받아오고
sudo systemctl restart hermes                 # 재시작
```

`config.json` 은 자료 저장소에 있어 15분 안에 자동으로 들어오지만, 봇이 기동할 때 한 번만
읽으므로 **재시작 전에는 옛 설정이 계속 돕니다.** 받아온 것을 확인하려면
`sudo systemctl start hermes-archive-pull` 로 즉시 동기화한 뒤 재시작하세요.

#### ⚠️ 유닛 파일을 고쳤으면 두 줄로는 안 붙습니다

`deploy/` 안의 **유닛 파일**(`hermes.service` · `hermes-archive-pull.service` ·
`hermes-archive-pull.timer`)을 고쳤을 때는 위 두 줄이 **아무 일도 안 합니다.** 그 파일들은
저장소에 있는 것이 아니라 **`/etc/systemd/system/` 에 설치된 사본**을 systemd 가 읽기
때문입니다. `git pull` 은 저장소만 갱신하고 `systemctl restart` 는 **이미 읽어 둔 옛
정의**로 다시 띄웁니다.

```bash
sudo -u hermes git -C /opt/hermes/code pull
sudo install -m 644 /opt/hermes/code/deploy/hermes.service /etc/systemd/system/
sudo systemctl daemon-reload      # ← 이 줄이 빠지면 옛 정의로 돕니다
sudo systemctl restart hermes
```

타이머를 고쳤으면 `hermes-archive-pull.{service,timer}` 도 같이 설치하고
`sudo systemctl restart hermes-archive-pull.timer` 까지 합니다.
**위 install 줄은 `setup.sh` 631~634 행과 같은 것입니다** — 첫 설치가 하는 일을 손으로
한 번 더 하는 것뿐이라, 그 줄이 바뀌면 여기도 바뀝니다.

**`npm run deployed` 는 이것을 못 잡습니다** — 그 명령이 대보는 것은
`git -C /opt/hermes/code rev-parse HEAD` 즉 **코드 해시**뿐이라, 유닛 파일이 낡은 채여도
**초록을 냅니다.** 「받아왔다」와 「그 정의로 돌고 있다」가 다른 말인데 확인이 앞엣것만
봅니다. 실제로 돌고 있는 정의는 이렇게 봅니다.

```bash
systemctl cat hermes | head -20          # 지금 systemd 가 읽고 있는 정의
systemctl show hermes -p ExecStart        # 저장소의 것과 눈으로 대보기
```

> 2026-09-03 회차에는 하는 사람이 알아채서 `install` + `daemon-reload` 를 함께 했는데,
> **그 사실이 어디에도 안 적혀 있었습니다.** 다음 사람은 두 줄만 치고 「배포했다」로
> 읽습니다 (워크스페이스 `handoff-hermes-잔여24건.md` 에서 옮겨 옴, 2026-09-06).

### 예약된 회차 시각에는 `setup.sh` 가 스스로 거부합니다

재시작은 **돌던 회차를 끊습니다.** 자동 반영이 끊기면 되돌리기(`src/ingest/index.js` 의
`rollback()`)가 **안 돕니다** — 프로세스가 통째로 죽으면 그 코드가 돌 기회가 없습니다.
그러면 자료 저장소 작업 트리가 반쯤 고쳐진 채 남고 **다음 회차가 `git pull` 에서 멈춥니다.**

그래서 `setup.sh` 를 다시 돌리면 7단계에서 시각을 보고, 예약된 회차와 겹치면
**아무것도 건드리지 않고 멈춥니다** (WHK 결정 2026-09-03).

| | |
|---|---|
| 막는 창 | 예약 시각 **앞 10분 · 뒤 15분** |
| 시각의 원본 | **자료 저장소 `config.json` 의 `digest.*.cron`** — 여기에도 `setup.sh` 에도 안 적혀 있습니다. `enabled` 가 true 인 것을 전부 찾으므로 예약을 늘려도 저절로 들어옵니다 |
| 기준 시간대 | 같은 파일의 `timezone`. **기계 시각을 안 봅니다** — UTC 로 도는 기계에서 판정이 뒤집히지 않게 |
| 종료코드 | **3** (「일부러 안 했다」). 0 이면 부르는 쪽이 「배포했다」로 읽고, 1 은 진짜 고장과 못 가릅니다 |
| 안 막는 때 | 봇이 안 돌고 있을 때 · 자료 저장소가 아직 없을 때(첫 설치) — 끊길 회차가 없습니다 |

막히면 화면이 **언제 다시 오면 되는지**를 찍어 줍니다. 지금 꼭 해야 하면 위 대가를
받아들이고 `--force` 를 붙입니다.

```bash
sudo bash ~/setup.sh <코드저장소> <자료저장소> --force
```

> **바로 위의 두 줄짜리 배포(`git pull` + `systemctl restart`)는 이 관문 밖입니다.**
> 관문은 `setup.sh` 안에만 있습니다. 두 줄로 배포할 때는 사람이 시각을 봐야 합니다 —
> 예약 시각은 `config.json` 의 `digest.*.cron` 에서 확인하세요.

---

## 비용

| | |
|---|---|
| e2-micro (무료 리전) | $0 — 월 무료 시간이 그 달 전체 시간과 같음 |
| pd-standard 20GB | $0 — 30GB까지 무료 |
| 이그레스 | $0 — 북미발 1GB/월 무료, 봇 트래픽은 수십 MB |

GCP 추가 비용은 사실상 없고, 지출은 Anthropic API(월 $100~200)가 전부입니다.

리전이나 디스크 종류를 잘못 골랐을 때 바로 알 수 있게 예산 알림을 걸어두세요.

<https://console.cloud.google.com/billing/budgets> → 예산 만들기 → 월 $5, 임계값 100%

---

## 문제가 생기면

| 증상 | 확인 |
|---|---|
| 봇이 응답 없음 | `systemctl status hermes` — `active (running)` 인지 |
| 계속 재시작 | `journalctl -u hermes -n 50` — 대개 `.env` 토큰 오류 |
| 5번 재시작 후 멈춤 | 설정 오류. 고친 뒤 `sudo systemctl reset-failed hermes && sudo systemctl start hermes` |
| 아카이브가 옛날 것 | 노트북에서 push 했는지. `systemctl status hermes-archive-pull` |
| `git pull` 실패 | deploy key 만료·삭제. `sudo -u hermes git -C /opt/hermes/archive pull` 로 원문 확인 |
| SSH 안 됨 | `--tunnel-through-iap` 빠뜨렸는지. IAP 방화벽 규칙 존재 확인 |
| 답이 두 번 옴 | 노트북 봇이 아직 돌고 있음 |

무슨 일이든 먼저: `journalctl -u hermes -n 50`
