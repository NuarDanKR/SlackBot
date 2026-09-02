# TYBot 관리 콘솔 (프론트엔드)

데이터 현황 · 수집 문서 열람 · API 사용량 · 봇 규칙 편집 · 배포 승인 · 워크스페이스 관리.

**현재 단계: 읽기 경로는 서버에 붙었습니다.** 쓰기(등록·승인·반영)는 아직입니다.
설계 결정과 제한은 [`../docs/design/console.md`](../docs/design/console.md) 를 보세요.

| 화면 | 데이터 출처 |
|---|---|
| 데이터 현황 | `GET /api/status` |
| 수집 문서 열람 | `GET /api/collected` · `/content` · `/audit` |
| API 사용량 | `GET /api/usage` |
| 봇 규칙 편집 (파일 목록) | `GET /api/harness` |
| 봇 규칙 편집 (승인 요청) | 예시 데이터 — BACKLOG B-16 |
| 배포 승인 | 예시 데이터 — BACKLOG B-16 |
| 워크스페이스 관리 | 예시 데이터 — BACKLOG B-16 |
| 이상 사용량 감지 | 예시 데이터 — BACKLOG B-19 |

아직 서버에 붙지 않은 구역에는 화면에 **`예시 데이터`** 배지가 붙습니다. 진짜 값과 섞여
보이지 않게 하려는 표시입니다.

## 함께 띄우기

```bash
# 화면과 API 서버를 한 번에 (console-web 에서)
npm run dev:all

# 또는 따로: API 서버 (저장소 루트에서)
uvicorn tybot.console.app:app --host 127.0.0.1 --port 8787 --app-dir src

# 화면 (console-web 에서) — /api 요청은 위 서버로 넘어갑니다
npm run dev
```

접속하면 회사 이메일·비밀번호를 묻습니다. PostgreSQL `console_user`에 등록한 계정을 씁니다.
활성 계정이 없거나 DB에 연결할 수 없으면 콘솔은 열리지 않습니다.
운영에서는 `CONSOLE_DIST` 를 지정해 API 서버가 화면까지 함께 서빙합니다(웹서버 불필요).

## 실행

```bash
cd console-web
npm install
npm run dev          # http://127.0.0.1:5173
npm run build        # dist/ 생성 (tsc 타입검사 포함)
npm run preview      # 빌드 결과 확인
```

dev·preview 서버는 `127.0.0.1` 에 묶여 있어 같은 PC 에서만 접속할 수 있습니다.
다른 장비에 노출하는 방식은 아직 정하지 않았으며, 기본 바인딩은 변경하지 않습니다.

## 구조

```
src/
├── App.tsx                  # 셸(좌측 메뉴 · 역할 · 테마 · 알림)
├── pages/
│   ├── Dashboard.tsx        # 데이터 현황 — 수집 추이 + 워크스페이스 상태
│   ├── Collected.tsx        # 수집 문서 열람 — MD 미리보기 (관리자 전용 + 열람 기록)
│   ├── Usage.tsx            # API 사용량 — 상한 대비 · 이상 감지 · 모델별
│   ├── Harness.tsx          # 봇 규칙 편집 — MD 편집 → 승인 요청
│   ├── Deploy.tsx           # 배포 승인 — 자동 검사 → 승인 → 반영
│   └── Workspaces.tsx       # 워크스페이스 관리 (관리자 전용)
├── components/
│   ├── Strata.tsx           # 수집 추이 그래프
│   ├── Markdown.tsx         # 마크다운 렌더러 (직접 구현, 이스케이프 우선)
│   ├── Approval.tsx         # 승인 영역 (배포·규칙 편집 공용)
│   └── primitives.tsx       # 배지·지표·머리글·수치 표기
├── mock/
│   ├── types.ts             # 화면이 받는 데이터 모양 = 붙일 API 응답 모양
│   ├── data.ts              # 워크스페이스·사용량·배포·레지스트리
│   ├── harness.ts           # 규칙 MD 파일과 편집 요청
│   └── archive.ts           # 수집된 문서와 열람 기록
└── styles/
    ├── tokens.css           # 서체(@font-face)·색·형태 토큰 (라이트/다크)
    └── app.css              # 레이아웃·컴포넌트
```

`mock/types.ts` 의 필드 이름은 다음 단계에 붙일 API 응답과 1:1로 맞춰 뒀습니다. 배선할 때
컴포넌트를 고치지 않고 데이터 출처만 바꾸는 것이 목표입니다.

## 콘솔 역할

실제 운영에서는 PostgreSQL `console_user`에 저장된 로그인 사용자의 역할로 메뉴와 API 권한이
결정됩니다. 화면에서 메뉴를 숨기는 것과 별도로 API도 같은 권한을 검사합니다.

| | 관리자(admin) | 개발자(developer) | 게스트(guest) |
|---|---|---|---|
| 승인 버튼 | 보입니다 | 요청만 가능 | 보이지 않습니다 |
| 수집 문서 원문 | 확인 후 열람 가능 · 기록 남음 | 목록·상태만 | 목록·상태만 |
| 봇 관리·서비스 로그 | 전체 | 담당 범위 | 보이지 않습니다 |
| 환경변수·사용자 관리 | 가능 | 불가 | 불가 |

## 디자인 규칙 (고칠 때 지킬 것)

### 서체
| 역할 | 글꼴 | 쓰는 곳 |
|---|---|---|
| 제목 | 나눔스퀘어 Neo | `.page-title` · `.section-title` · 카드 제목 |
| 일반 UI | 나눔스퀘어 라운드 | 본문 기본값 · 설명문 · 라벨 · 버튼 |
| 문서 미리보기 | 마루 부리 | `.md` (수집 문서·규칙 문서 미리보기) **한 곳만** |
| 수치·해시·경로·토큰 | IBM Plex Mono | 숫자와 ASCII 만 |

한글 글꼴은 모두 네이버 공식 CDN(`hangeul.pstatic.net`)에서 `@font-face` 로 불러옵니다.

**주의 · 나눔스퀘어(구버전) 함정.** 처음에는 jsDelivr 의 구 나눔스퀘어 woff2 를 물렸는데,
그 파일에는 **한글 글리프가 없어서** 제목이 조용히 나눔스퀘어 라운드로 폴백됐습니다.
파일 크기로 구분할 수 있습니다 — 한글이 들어 있으면 woff2 가 **380KB 이상**이고,
155KB 짜리는 라틴 서브셋입니다. 그래서 네이버가 현재 배포하는 **Neo** 로 교체했습니다.

**화면 설명문에 마루 부리를 쓰지 않습니다.** 명조꼴은 긴 글을 읽는 자리에만 맞습니다.
두 줄짜리 화면 설명에 쓰면 UI 가 아니라 문서처럼 보이고, 나눔스퀘어 라운드와 섞이지 않습니다.

한글 라벨에 Mono 를 쓰지 않습니다 — 글리프가 없어 폴백되고 자간이 어긋납니다.

### 색
CI 컬러 `#800020` 는 **강조·포인트 전용**입니다. 브랜드 표식, 활성 메뉴, 주요 실행 버튼,
사용량 최고점 막대 — 이 네 곳에만 씁니다. 상태는 기능색으로 표시합니다(파랑·초록·주황·빨강).

채워진 면은 다크 모드에서도 `--brand-solid`(원색)를 씁니다. 얇은 선·배지 글자만
`--brand`(한 단 밝은 같은 계열)를 씁니다.

### 문체
사용자는 개발자가 아닙니다. 화면 문장은 친절하고 자세하게, 사람이 하는 일로 씁니다.
버튼 이름과 결과 알림의 말을 맞춥니다.

## 마크다운 렌더러

외부 라이브러리를 쓰지 않고 [`Markdown.tsx`](src/components/Markdown.tsx) 에서 직접
렌더링합니다. 들어오는 텍스트가 사내 대화 원문과 사용자가 편집한 규칙 파일이라, **먼저 전부
이스케이프하고 그다음 아는 문법만 태그로 바꾸는** 순서를 지킵니다. 링크는 태그로 만들지 않습니다.

문법 수정 시 주입 검증을 다시 돌려 주세요.

```bash
./node_modules/.bin/esbuild src/components/Markdown.tsx --format=esm --jsx=automatic \
  --outfile=/tmp/md.mjs
# react import 줄을 지운 뒤 renderMarkdown 출력에 허용 태그(class 속성만) 외가
# 나오지 않는지 확인합니다.
```

## 백엔드 연결

읽기 API 는 `src/tybot/console/` 에 있습니다. 화면이 쓰는 필드 이름과 API 응답이 같은 모양이라,
`src/mock/*` 를 `fetch` 로 바꾸면 컴포넌트는 고칠 것이 없습니다.

```bash
# 저장소 루트에서
pip install -e ".[console]"
uvicorn tybot.console.app:app --host 127.0.0.1 --port 8787 --app-dir src
```

| 엔드포인트 | 화면 | 대응하는 목데이터 |
|---|---|---|
| `GET /api/me` | 셸(역할·이름) | `mock/data.ts` `users` |
| `GET /api/status` | 데이터 현황 | `workspaces` |
| `GET /api/usage` | API 사용량 | `usage` |
| `GET /api/collected` | 수집 문서 열람(목록) | `mock/archive.ts` `collectedDocs` |
| `GET /api/collected/content?path=` | 수집 문서 열람(본문) | `CollectedDoc.content` |
| `GET /api/collected/audit` | 원문 열람 기록 | `readAudit` |
| `GET /api/harness` | 봇 규칙 편집 | `mock/harness.ts` `harnessFiles` |

인증은 `Authorization: Bearer <토큰>` 입니다. 토큰은 서버의 `CONSOLE_USERS` 에서 관리합니다.

아직 없는 것: 쓰기 엔드포인트(워크스페이스 등록, 승인·반려, 되돌리기). 화면의 해당 버튼은
지금은 알림만 띄웁니다.
