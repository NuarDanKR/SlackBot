# DB 선택 · 조직 기반 권한 설계

_결정 문서 · 2026-08-19_

## 0. 대전제 — DB는 진실이 아니다

| 계층 | 저장소 | 성격 |
|---|---|---|
| **원문(진실)** | MD 파일 (`/var/lib/tybot/archive`) | 불변·append only. 사람이 열어볼 수 있음 |
| **인덱스/캐시** | PostgreSQL | **언제든 MD 에서 재빌드 가능**한 파생물 |
| **조직 마스터** | PostgreSQL ← Oracle 동기화 | 레거시가 원본, 우리는 읽기 복제 |
| **감사 로그** | PostgreSQL | 질문·근거·권한 판정 기록 (재빌드 불가, 백업 대상) |

DB를 넣는 이유는 "MD 를 대체"가 아니라 **매 질문마다 수천 개 MD 를 전부 읽는 게 불가능**해서다.
`DROP DATABASE` 해도 아카이브만 살아있으면 전부 복구된다 — 이 성질을 깨는 설계는 하지 않는다.

## 1. DB 추천: **PostgreSQL 16**

한 DB로 네 가지를 다 커버한다. 별도 검색엔진(Elasticsearch)·벡터DB를 따로 운영하지 않아도 된다.

| 필요 | PostgreSQL 해법 |
|---|---|
| 조직 트리(본부>팀>현장) | 재귀 CTE (`WITH RECURSIVE`) — 상위/하위 조회가 한 쿼리 |
| 한국어 전문검색 | **`pg_bigm`** 확장 (bigram 색인). 기본 tsvector 는 한국어 형태소 분석이 없어 무용지물 |
| 임베딩 검색(2단계) | **`pgvector`** 확장 |
| 감사 로그·트랜잭션 | 기본 제공 |

### 후보 비교
| 후보 | 판단 |
|---|---|
| **PostgreSQL + pg_bigm + pgvector** | **채택.** 단일 엔진, 오픈소스, RL8 지원 良, 한국어 검색·벡터·조직트리 전부 커버 |
| SQLite + FTS5 | 파일럿엔 충분하고 지금도 가능. 단 동시 쓰기 약하고 원격 접속·역할 분리 불가 → 멀티 워크스페이스 가면 갈아엎어야 함 |
| MariaDB 10.11+ | 재귀 CTE 는 동일 지원. 단 **한국어 부분일치 색인이 없고**(MySQL 의 ngram 파서 미이식) 벡터는 11.8+ 필요 → bigram 테이블 자작 필요 |
| Elasticsearch | 검색은 최강이나 JVM 운영부담·메모리·라이선스 이슈. 이 규모엔 과잉 |
| MongoDB | 조직 트리 권한 판정에 조인이 필요 — 관계형이 맞다 |

> MariaDB 와의 상세 비교(서버 관리자 검토용): [`postgres-vs-mariadb.md`](postgres-vs-mariadb.md)
>
> 한국어 검색 품질을 더 끌어올려야 하면 `PGroonga`(형태소·한국어 강함)로 교체 가능.
> 색인 크기와 운영 복잡도가 커지므로 **`pg_bigm` 으로 시작**하고 검색 품질 불만이 나오면 그때 바꾼다.

### 설치 (Rocky Linux 8)
```bash
sudo dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-8-x86_64/pgdg-redhat-repo-latest.noarch.rpm
sudo dnf -qy module disable postgresql        # RL8 기본 모듈(PG10/13)과 충돌 방지
sudo dnf install -y postgresql16-server postgresql16-contrib pg_bigm_16 pgvector_16
sudo /usr/pgsql-16/bin/postgresql-16-setup initdb
sudo systemctl enable --now postgresql-16
```
```sql
CREATE DATABASE tybot ENCODING 'UTF8';
\c tybot
CREATE EXTENSION pg_bigm;
CREATE EXTENSION vector;      -- 임베딩 도입 시
```
- 접속은 **localhost 소켓만** (`listen_addresses = 'localhost'`). 봇과 DB가 같은 서버 → 네트워크 노출 0.
- 앱 계정은 최소권한: `tybot_app` (해당 스키마 CRUD만, SUPERUSER 금지).

## 2. 스키마 스케치

```sql
-- 조직 트리 (Oracle 에서 동기화)
CREATE TABLE org_unit (
  code        TEXT PRIMARY KEY,          -- 'ABB540'
  name        TEXT NOT NULL,             -- '건축현장관리팀'
  kind        TEXT NOT NULL,             -- hq | team | site | project
  parent_code TEXT REFERENCES org_unit(code),
  synced_at   TIMESTAMPTZ NOT NULL
);

-- 채널 → 조직 매핑 (채널 명명규칙에서 파싱 + 수동 보정)
CREATE TABLE channel (
  workspace   TEXT NOT NULL,
  name        TEXT NOT NULL,             -- '#현장_김해외동(180182)_채팅방'
  org_code    TEXT REFERENCES org_unit(code),
  share_level TEXT NOT NULL DEFAULT 'org_internal',  -- 아래 3절
  PRIMARY KEY (workspace, name)
);

-- MD 원문 라인 인덱스 (MD 에서 재빌드 가능)
CREATE TABLE raw_line (
  id          BIGSERIAL PRIMARY KEY,
  workspace   TEXT NOT NULL,
  channel     TEXT NOT NULL,
  doc_path    TEXT NOT NULL,             -- MD 파일 경로 = 출처 표기의 근거
  line_no     INT  NOT NULL,
  spoken_at   TIMESTAMPTZ NOT NULL,
  speaker     TEXT NOT NULL,
  body        TEXT NOT NULL,
  content_sha TEXT NOT NULL,             -- 멱등 재색인용
  UNIQUE (doc_path, line_no, content_sha)
);
CREATE INDEX raw_line_bigm ON raw_line USING gin (body gin_bigm_ops);
CREATE INDEX raw_line_recent ON raw_line (workspace, channel, spoken_at DESC);

-- 감사 로그 (재빌드 불가 — 백업 대상)
CREATE TABLE audit_query (
  id         BIGSERIAL PRIMARY KEY,
  asked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  slack_user TEXT NOT NULL,
  workspace  TEXT NOT NULL,
  channel    TEXT NOT NULL,
  question   TEXT NOT NULL,
  scope      JSONB NOT NULL,             -- 권한 판정 결과(허용된 채널 목록)
  sources    JSONB NOT NULL,             -- 실제 근거로 쓴 doc_path/line_no
  model      TEXT, cost_usd NUMERIC(10,5)
);
```

**출처 표기는 항상 `doc_path` + `line_no` 로 MD 원문을 다시 가리킨다.**
DB 안의 텍스트가 아니라 MD 가 근거라는 원칙이 여기서 지켜진다.

## 3. 권한: 폴더냐 DB냐 → **셋 다, 역할이 다르다**

| 층 | 수단 | 막는 것 | 못 막는 것 |
|---|---|---|---|
| 1. 물리 격리 | **디렉터리 / 저장소 분리** | 저장소 유출·오배포 시 통째 노출 | 상속·임시권한·감사 |
| 2. 문서 자체 | **MD 프론트매터** (`visibility`, `acl`, `share_level`) | DB 없이도 판단 가능(오프라인·git 리뷰) | 조직 트리 계산 |
| 3. 판정 엔진 | **PostgreSQL** (조직 트리 + 채널 매핑 + 감사) | 상속·통합조회·이력 추적 | 파일 자체 유출 |

판정은 **AND**다. 디렉터리에 있고, 프론트매터가 허용하고, DB 규칙이 허용해야 근거로 쓴다.
**DB 장애 시엔 프론트매터만으로 판정 = 자동으로 좁아지는 쪽(막는 쪽)으로 폴백**한다.

### 디렉터리 구조 (물리 소유권은 워크스페이스·Slack 채널 ID)
```
/var/lib/tybot/archive/workspaces/<workspace>/channels/
└── <channel-id>__<initial-name>/raw/YYYY-MM-DD.md
```
- 조직은 이름·소속 변경이 있으므로 물리 경로가 아니라 `org_kind`·`org_code`와 DB 관계로 표현한다.
- v1 `archive/channels/<workspace>/<channel>.md`는 전환 기간 읽기 호환만 유지한다.

## 4. 조직 상속 규칙 — 여기가 사고 나는 지점

요구사항: "AA현장 채널의 봇은 소속 팀(건축현장관리팀)과 본부(건축본부) 내용까지 알려준다."

이건 **상향 조회**(하위 조직이 상위 자료를 봄)다. 그대로 열면 본부의 인사·원가·수주 자료가
전 현장에 노출된다. 원칙 3(막는 쪽이 기본값)과 정면충돌하므로 **문서 등급으로 게이팅**한다.

| `share_level` | 의미 | 누가 보나 |
|---|---|---|
| `org_public` | 산하 조직에 공유 | 그 조직 + **모든 하위 조직** |
| `org_internal` (**기본값**) | 해당 조직 내부 | 그 조직 + **상위 조직**(하향 조회) |
| `restricted` | 명시 ACL만 | `acl:` 에 적힌 대상만 |

규칙 세 줄:
1. **하향(본부 → 현장 자료 조회)은 기본 허용.** 상위 조직은 산하를 관리할 책임이 있다.
2. **상향(현장 → 본부 자료 조회)은 `org_public` 만.** 본부가 "산하 공유"로 표시한 것만 내려간다.
3. **형제(다른 팀·다른 현장)는 항상 차단.** exec 화이트리스트만 예외.

→ AA현장 채널에서 물으면: AA현장 전부 + 건축현장관리팀의 `org_public` + 건축본부의 `org_public`.
   옆 현장(김해외동)은 안 보인다.

> **결정 필요**: 기본값을 뒤집을지(팀 자료는 산하 현장에 기본 공개). 팀 단위는 공개가 자연스럽고
> 본부 단위는 위험하다 → **kind 별 기본값**(team=org_public, hq=org_internal)도 선택지.

## 5. Oracle 레거시 연동

```python
# python-oracledb thin 모드 — Instant Client 설치 불필요(RL8 배포 간단)
import oracledb
conn = oracledb.connect(user=..., password=..., dsn=...)  # 읽기 전용 계정
```
- **읽기 전용 계정**, 조회 대상 뷰만 화이트리스트. DDL·DML 권한 없음.
- 동기화: 조직/현장 마스터를 **야간 1회 + 수동 트리거**로 `org_unit` 에 upsert.
  실시간 조회 금지 — 내부망 부하와 장애 전파를 막는다.
- 현장 코드(`180182`)가 채널명에 이미 들어있으므로 **채널명 → 현장코드 → 조직 트리** 매핑이 자동으로 붙는다.
  이것이 채널 명명규칙을 강제하는 실질적 이유다.
- 동기화 실패 시: 마지막 성공분 유지 + 경고. **조직 정보가 없으면 상속 없이 자기 채널만** (막는 쪽).

## 6. 단계 계획

| 단계 | 내용 | 완료 조건 |
|---|---|---|
| 0 (현재) | MD 파일 + 파일 스캔 검색 | 파일럿 합격 기준 통과 |
| 1 | PostgreSQL 인덱서 (`raw_line` 재빌드 + pg_bigm 검색) | MD ↔ DB 재빌드 멱등성 확인 |
| 2 | Oracle → `org_unit` 동기화, 채널→조직 매핑 | 채널명만으로 조직 자동 분류 |
| 3 | 상속 규칙 + `share_level` 적용, 감사 로그 | "옆 현장 자료가 안 보인다" 회귀 테스트 |
| 4 | pgvector 임베딩 (검색 품질 부족할 때만) | 키워드 검색 대비 개선 측정 |
