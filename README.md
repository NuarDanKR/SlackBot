# TYBot

태영건설 Slack AI TFT — 워크스페이스별 봇이 대화를 수집해 **MD 아카이브**로 자산화하고,
**권한이 허용된 원문만 근거로** 답한다. 근거가 없으면 추측하지 않는다.

- 설계 원칙·컨벤션: [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md)
- **파일럿 런북(여기부터)**: [`docs/pilot/README.md`](docs/pilot/README.md)
- 서버 배치(Rocky Linux 8): [`docs/deploy/rocky8.md`](docs/deploy/rocky8.md)
- **멀티 워크스페이스 · 권한 3층**: [`docs/multi-workspace.md`](docs/multi-workspace.md)
- 에이전트 구조(마스터봇·정리봇 검토): [`docs/design/agent-architecture.md`](docs/design/agent-architecture.md)
- **그룹웨어 Oracle → PostgreSQL 동기화**: [`docs/design/oracle-sync.md`](docs/design/oracle-sync.md)
  - 인프라·보안 담당자 요청서: [`docs/deploy/infra-request-snapshot-push.md`](docs/deploy/infra-request-snapshot-push.md)
- 사용자 식별·그룹웨어 인증 검토: [`docs/design/identity-and-legacy-login.md`](docs/design/identity-and-legacy-login.md)
- DB 선택·조직 권한 설계: [`docs/design/db-and-acl.md`](docs/design/db-and-acl.md) · [PG vs MariaDB](docs/design/postgres-vs-mariadb.md)
- 전체 아키텍처(기획): [`docs/architecture.md`](docs/architecture.md)

---

## 전체 구조

```mermaid
flowchart TB
    subgraph SL["Slack (워크스페이스마다 앱 1개)"]
        WA["경영본부 워크스페이스<br/>(root)"]
        WB["파일럿 워크스페이스"]
        WC["팀·현장 워크스페이스"]
    end

    subgraph SRV["서버 (Rocky Linux 8) — 프로세스 1개"]
        BOT["tybot.service<br/>WorkspaceBot × N"]
        ENG["AnswerEngine<br/>의도분류 · 검색 · 요약 · 판단"]
        ACL["권한 필터<br/>채널멤버십 / share_with / root"]
        GW["LLM 게이트웨이<br/>모델선택 · 민감도 · 비용상한"]
    end

    subgraph ST["저장 (/var/lib/tybot)"]
        ARC["archive/channels/&lt;ws&gt;/*.md<br/><b>원문 · append only</b>"]
        QA["qa-log/<br/>감사 기록"]
    end

    LLM["Anthropic API"]

    WA <-.->|Socket Mode<br/>아웃바운드 전용| BOT
    WB <-.->|Socket Mode| BOT
    WC <-.->|Socket Mode| BOT

    BOT -->|실시간 수집| ARC
    BOT --> ENG
    ENG --> ACL
    ACL -->|권한 내 원문만| ARC
    ENG --> GW
    GW --> LLM
    BOT -->|질문·근거·비용| QA

    ARC -. 읽기만 .-> ENG

    classDef store fill:#1a3a5c,stroke:#4a7ab5,color:#fff
    classDef ext fill:#3a2a4a,stroke:#7a5a9a,color:#fff
    class ARC,QA store
    class LLM ext
```

**인바운드 포트 0개.** Socket Mode 는 봇이 Slack 으로 나가는 WebSocket 이다.

---

## 질문 처리 흐름

```mermaid
flowchart TD
    Q["@tybot 질문"] --> CMD{"명시 명령?<br/>수집 / 전체수집"}
    CMD -->|예| ING["백필 실행"]
    CMD -->|아니오| CLS["LLM 의도 분류<br/>(Haiku, 실패 시 규칙 폴백)"]

    CLS --> K{"kind"}
    K -->|status / help| SYS["봇 상태 · 사용법<br/>(LLM 답변 생성 없음)"]
    K -->|summary| SUM["기간 스캔"]
    K -->|search| SRCH["핵심어로 원문 검색"]
    K -->|advice| ADV["판단·권고"]
    K -->|out_of_scope| OOS{"아카이브 관련<br/>표현 있나"}
    OOS -->|예| SUM
    OOS -->|아니오| REF["거절"]

    SUM --> PERM
    SRCH --> PERM
    ADV --> PERM
    PERM["권한 필터<br/><b>답변 생성 이전</b>에 범위 축소"] --> HIT{"근거 있나"}

    HIT -->|0건| NONE["추측하지 않는다<br/>문서 목록 + 다음 행동 안내<br/><b>LLM 호출 없음</b>"]
    HIT -->|있음| GEN["LLM 답변 생성<br/>원문 라인만 근거로"]
    GEN --> CITE["출처 부착<br/>타 워크스페이스는 [ws] 표기"]

    CITE --> LOG["감사 기록<br/>journald + JSONL + 일자별 MD"]
    NONE --> LOG
    SYS --> LOG
    ING --> LOG
    REF --> LOG
```

---

## 권한 3층 (독립된 축)

```mermaid
flowchart TD
    START["문서 열람 요청"] --> WS{"소유 워크스페이스<br/>== 요청자 워크스페이스?"}

    WS -->|"다름"| WL{"CROSS_WS_READ<br/>화이트리스트에 있나"}
    WL -->|아니오| DENY["차단<br/>(채널명도 노출 안 함)"]
    WL -->|예| ROOT1{"요청자가<br/>root 워크스페이스?"}
    ROOT1 -->|예| ALLOW["허용"]
    ROOT1 -->|아니오| SW{"문서 share_with 에<br/>요청자 워크스페이스가 있나"}
    SW -->|예| ALLOW
    SW -->|아니오| DENY

    WS -->|"같음"| ROOT2{"요청자가<br/>root 워크스페이스?"}
    ROOT2 -->|예| ALLOW
    ROOT2 -->|아니오| PUB{"visibility: public?"}
    PUB -->|예| ALLOW
    PUB -->|아니오| MEM{"그 채널의<br/>멤버인가"}
    MEM -->|예| ALLOW
    MEM -->|아니오| DENY
```

| 축 | 통제 대상 | 정하는 주체 |
|---|---|---|
| 채널 멤버십 | 같은 워크스페이스에서 어느 채널을 보나 | Slack 초대 |
| `share_with` | 이 문서를 어느 **동등** 워크스페이스에 넘길지 | 자료 소유 쪽 사람 |
| root 워크스페이스 | 산하 자료를 취합·열람하는 상위 조직 | 서버 운영자(`ROOT_WORKSPACES`) |

**막는 쪽이 기본값.** 수집기는 원문을 항상 `private` 로 저장하므로, 자동으로 워크스페이스를
넘어가는 자료는 없다. 자세한 내용은 [`docs/multi-workspace.md`](docs/multi-workspace.md).

---

## 환각 방지 4겹이 코드에 박힌 위치

```mermaid
flowchart LR
    L1["1겹<br/>근거 오염 금지"] --> L1D["archive/writer.py<br/>봇 발언·PII 배제<br/>qa-log 를 아카이브 밖에 둠"]
    L2["2겹<br/>답 전 원문 열기"] --> L2D["archive/store.py<br/>요약 섹션은 근거 제외<br/>원문 라인만 반환"]
    L3["3겹<br/>'없다' 남용 금지"] --> L3D["answer.py<br/>0건이면 문서 목록 + 안내<br/>LLM 호출 안 함"]
    L4["4겹<br/>사람이 잡게"] --> L4D["audit.py<br/>질문·근거·권한·비용 기록<br/>출처 강제"]
```

---

## 개발 환경

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -e ".[dev]"
cp .env.example .env                              # 값은 직접 입력, 커밋 금지
pytest                                            # 116 tests
python -m tybot.slack.pilot                       # 로컬 기동
```

> `.env` 는 인라인 주석(`VAR=값  # 설명`)을 쓰지 않는다. 설명은 줄 앞에 단독으로 둔다.

## 구조

```
src/tybot/
├── slack/pilot.py      # 워크스페이스 봇 (Socket Mode, N개 연결)
├── answer.py           # 질의응답 파이프라인 (검색 / 요약 / 판단)
├── intent.py           # 의도 분류 (LLM + 규칙 폴백)
├── access/             # 권한 3층 판정
├── archive/            # store.py=원문 검색 / writer.py=원문 수집
├── audit.py            # 질의응답 감사 기록
├── workspaces.py       # 멀티 워크스페이스 설정 · 크로스 열람 화이트리스트
├── envfile.py          # 설정 파일 로딩(봇·점검 스크립트 공용)
├── gateway/            # LLM 게이트웨이 (모델선택·민감도·비용가드)
└── config.py

deploy/     install.sh · tybot.service · update.sh(자동배포) · wheelhouse.sh
scripts/    check_env.py(설정 점검) · share.py(공유 설정)
```

## LLM 게이트웨이

모든 LLM 호출은 이 라우터를 통한다. 프로바이더 SDK 직접 호출 금지.

```python
from tybot.gateway import Router, Message, Sensitivity

router = Router.from_default_registry(daily_limit_usd=5)
resp = router.complete(
    [Message("user", "이 채널 요약해줘")],
    model="claude-sonnet-5",
    sensitivity=Sensitivity.CONFIDENTIAL,
)
print(resp.text, resp.model, resp.cost_usd)
```

민감도는 모델 티어가 아니라 **벤더 계약(DPA)** 단위로 판정한다. 일별 비용 상한은 전 워크스페이스 합산.
