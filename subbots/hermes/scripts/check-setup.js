#!/usr/bin/env node
/** Full readiness check by default; explicit offline/archive/live modes. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { runCheck, childLimitMs, formatDuration, reportPhase, phaseBudgetMs } from './_child-run.js';
import { CHECKS, LIVE_CHECKS } from './check-catalog.js';
import { parseMode, selectChecks, discoverTests, validateCatalog, discoverNodeChecks, validateNodeChecks, runOffline } from './check-runner.js';

const CHECK_ROOT = fileURLToPath(new URL('../', import.meta.url));
const { mode, list } = parseMode(process.argv.slice(2));
validateCatalog(CHECKS, discoverTests(CHECK_ROOT), { exists: f => fs.existsSync(path.join(CHECK_ROOT, f)) });
validateNodeChecks(CHECKS, discoverNodeChecks(CHECK_ROOT));
if (list) {
  console.log(JSON.stringify({
    mode, checks: selectChecks(CHECKS, mode),
    live: mode === 'all' || mode === 'live' ? LIVE_CHECKS : [],
  }, null, 2));
} else if (mode === 'offline') {
  process.exitCode = runOffline(selectChecks(CHECKS, mode), { root: CHECK_ROOT }).exitCode;
} else {
  await runOperational(mode);
}

async function runOperational(mode) {
const { WebClient } = await import('@slack/web-api');
const { default: Anthropic } = await import('@anthropic-ai/sdk');
const {
  config, requireEnv, ARCHIVE_DIR, CHANNELS_DIR, DOCS_DIR, DOC_PROJECTS_DIR, ROOT, FULL_ACCESS,
  companyWideDocProjects,
} = await import('../src/config.js');
const {
  assertArchive, listAllChannels, listArchivedChannels, uninvitedChannels, buildArchiveBriefSplit,
  staleChannelRefs,
} = await import('../src/archive.js');
const {
  hasDocuments, listProjects, listDocuments, buildDocumentsBriefSplit, indexGauge,
} = await import('../src/documents.js');
const { listBotChannels, listSlackChannels } = await import('../src/slack-live.js');
const { auditDocIndex } = await import('../src/doc-index-audit.js');
const { reportCardWarnings } = await import('./_card-warnings.js');
const { PROVIDERS } = await import('../src/llm/provider.js');
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => console.log(`  ✗ ${m}`);
let failed = false;
let pySkipped = [];

/** 채널 조회 실패 사유를 종류별로 세어 화면 문구로 접는다. 개수만 보이면 "전 채널이
 * 같은 이유로 실패해도 사람은 개수만 본다" — 그래서 사유 이름(에러 메시지)도 함께 싣는다.
 * 종류가 많으면 많은 것부터 최대 3종만 펼치고 나머지는 "그 외 N건" 으로 접는다 —
 * 접었다는 사실이 보이면 되고, 채널 이름(비공개일 수 있다)은 여기 안 넣는다. */
function summarizeUnreadReasons(reasons, maxKinds = 3) {
  if (reasons.length === 0) return '';
  const counts = new Map();
  for (const r of reasons) counts.set(r, (counts.get(r) || 0) + 1);
  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  const shown = sorted.slice(0, maxKinds).map(([name, n]) => `${name} ${n}`);
  const restCount = sorted.slice(maxKinds).reduce((sum, [, n]) => sum + n, 0);
  if (restCount) shown.push(`그 외 ${restCount}건`);
  return shown.join(' · ');
}

/**
 * 매니페스트에 적어 둔 봇 스코프 목록. 목록을 여기 또 적으면 반드시 어긋나므로 파일에서 읽는다.
 */
function manifestScopes() {
  const yaml = fs.readFileSync(path.join(ROOT, 'slack-app-manifest.yaml'), 'utf8');
  const afterBot = yaml.split(/^\s*bot:\s*$/m)[1] || '';
  const out = [];
  for (const line of afterBot.split('\n')) {
    // 스코프 이름에는 밑줄이 들어간다 (app_mentions:read). [a-z] 만으로는 첫 항목부터 놓친다.
    const m = line.match(/^\s+-\s+([a-z_]+:[a-z_]+)/);
    if (m) out.push(m[1]);
    else if (line.trim() && !line.trim().startsWith('#')) break; // bot: 목록 끝
  }
  // 하나도 못 읽었다면 매니페스트가 바뀌었거나 파서가 깨진 것이다.
  // 빈 목록을 그대로 두면 "빠진 스코프 없음"으로 읽혀 점검이 무의미하게 통과한다.
  if (!out.length) throw new Error('slack-app-manifest.yaml 에서 봇 스코프를 읽지 못했습니다');
  return out;
}

/**
 * 토큰에 실제로 붙어 있는 스코프. 슬랙은 응답 헤더로만 알려준다.
 *
 * missing_scope 오류의 needed 는 "그 API 가 요구하는 전체 목록"이지 "빠진 것"이 아니다.
 * 그대로 보여주면 이미 갖고 있는 스코프까지 부족한 것처럼 읽혀 엉뚱한 곳을 고치게 된다.
 */
async function grantedScopes(token) {
  const res = await fetch('https://slack.com/api/auth.test', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
  });
  return (res.headers.get('x-oauth-scopes') || '').split(',').map((s) => s.trim()).filter(Boolean);
}

console.log('\nHermes 설치 점검\n' + '='.repeat(50));

/**
 * 코드 일관성 검사(CROSS_CHECKS)를 여기서 함께 돌린다.
 *
 * **여기서 검사를 하나씩 세어 적지 않는다.** 무엇을 돌릴지는 `scripts/check-catalog.js`
 * 가 정하고(`CROSS_CHECKS` 는 거기서 뽑는다), 검사마다의 한 줄 설명은 그 카탈로그의
 * `what` 에, **왜 그 검사가 있는지는 그 검사 파일 자신의 머리말**에 있다. 셋이면 충분하고
 * 넷이면 하나는 반드시 안 따라온다.
 *
 * 2026-09-16 까지 여기 서수로 늘어놓은 목록이 있었다. **카탈로그의 node 검사 101개 중
 * 39개**만 적혀 있었는데 「CROSS_CHECKS 를 여기서 함께 돌린다」 뒤에 첫째·둘째로 이어져
 * **전수처럼 읽혔다.** 최근 넷(`check-check-modes`·`check-log-stats`·`check-log-codec`·
 * `check-usage-accounting`)이 빠진 채였고 그 빠짐은 화면에 안 드러났다 — 세어 보기
 * 전에는 아무도 모른다. 아래 「개수를 적지 않는다」가 같은 이유로 이미 적혀 있었는데,
 * 개수 대신 **목록**으로 같은 실수가 다시 자랐다. 지운 39개의 사연은 전부 각 파일
 * 머리말에 그대로 있다(지우기 전 전수 대조함).
 *
 * ── 어떤 종류들인가 ──
 *
 * 이 분류는 개별 파일이 가질 수 없는 것이라 여기 남긴다. **대부분은 「틀려도 에러가 안
 * 나는 자리」를 지킨다** — 숫자만 달라지거나, 목록이 짧아지거나, 검사가 아무것도 안 본 채
 * 초록을 낸다.
 *
 *   · **같은 판정이 두 곳에 나뉘어 갈렸나** — 가장 많은 종류다. 두 언어에 하나씩 있는
 *     경로 계산, 렌더와 그 역파서, 봇과 스킬이 따로 읽는 설정값 같은 것들. 한쪽만 고치면
 *     나머지가 조용히 옛 값을 쓴다
 *   · **목록이 무엇을 모집단으로 삼나** — 목록이 짧아지는 쪽으로 틀리면 「없음」이라는
 *     정상 화면과 구별되지 않는다
 *   · **표시가 붙어야 할 곳에 붙고, 안 될 곳에는 안 붙나** — 첨부 수록 표시, 못 푼 채널
 *     링크, 비공개 자료의 노출 여부
 *   · **안전망이 실제로 켜지나** — 좁혀서 못 찾았을 때 넓히기, 거절 전에 열어 보기 같은
 *     것들. 안 켜져도 답은 정상 모양으로 나간다
 *   · **검사·감사 자신을 지키는 검사** — 감사가 조용히 통과하면 원래 상태로 돌아가는데
 *     그때 `npm run check` 는 초록이라 「대보고 있다」로 읽힌다
 *   · **사람이 읽고 그대로 치거나 붙이는 것이 낡았나** — 문서의 경로·명령·예시. 코드가
 *     아니라 문서가 낡는 자리다
 *   · **빈 손으로 시작하는 사람이 문서대로 하면 되나** — 우리 기계에는 설정이 이미 있어서
 *     설치 경로가 깨져도 우리는 영영 안 밟는다
 *   · **저장소를 팀끼리 나눠 쓰게 되며 성격이 바뀐 자리** — 사업장·비공개 채널 이름이
 *     코드·주석·픽스처에 남아 있나
 *
 * **슬랙보다 먼저 돌린다.** 이 목록은 다 네트워크가 필요 없어서, 토큰이 없거나 슬랙이 죽은 날에도
 * 이것만은 돌아간다. 뒤에 두면 그런 날 아예 안 돌고, 그게 지금까지의 상태였다.
 *
 * 실패해도 멈추지 않고 목록을 끝까지 돌린다 — 첫 실패에서 끊으면 나머지의 상태를 모르는데,
 * 그 모름이 「이상 없음」으로 읽힌다.
 * (여기 두 줄에는 개수를 적지 않는다. 검사를 더할 때마다 낡는데 그 낡음이 화면에
 *  안 드러나서, 2026-08-31 까지 「스물네 개」로 두 번 낡아 있었다.)
 */
// `check-sync-for-read.js` 는 여기 넣지 않는다 — 임시 디렉터리에 `git init`/커밋까지
// 만들어 도는 검사라 나머지보다 무겁고, 이 목록은 네트워크 없이 순식간에 끝나는 것들만
// 모은다. 뺀 것은 잊은 게 아니라 정한 것이다 — 돌리려면 `node scripts/check-sync-for-read.js`.
// (`check-line-endings.js` 는 임시 파일 넷을 쓰지만 git 도 네트워크도 안 타서 여기 둔다.
//  읽는 자리를 검사하는 것이라 파일 없이는 잴 수가 없다.)
const CROSS_CHECKS = selectChecks(CHECKS, mode).filter(c => c.runtime === 'node').map(c => [path.basename(c.file), c.what]);

/* 자식 하나의 상한. `HERMES_CHECK_TIMEOUT_MS` 로 조정한다.
 *
 * 상한이 없던 동안 `check-brief-split.js` 가 12분을 돌았고, 그동안 화면은 침묵했다 —
 * 침묵은 「빠르다」 와 화면에서 같다(2026-10-07 TYIT). 느린 서버에서 관문을 통째로
 * 끄게 만들지 않으려고 환경변수로 연다. */
const CHILD_LIMIT_MS = childLimitMs();
/* 구간 합계의 예산. 자식 하나하나는 상한 안인데 **수가 늘어** 구간이 느려지는 종류는
 * 자식 상한으로 못 잡는다 — 그 변화는 에러가 아니라 「아무도 안 돌리게 되는」 습관으로
 * 나타난다. `HERMES_CHECK_PHASE_BUDGET_MS=0` 으로 끌 수 있다. */
const PHASE_BUDGET_MS = phaseBudgetMs();

if (mode !== 'live') {
console.log(`\n[1/6] 코드 일관성  (검사 ${CROSS_CHECKS.length}개 · 개별 상한 ${formatDuration(CHILD_LIMIT_MS)}`
  + `${PHASE_BUDGET_MS ? ` · 구간 예산 ${formatDuration(PHASE_BUDGET_MS)}` : ' · 구간 예산 끔'})`);
const crossStarted = Date.now();
const crossTimes = [];
for (const [file, what] of CROSS_CHECKS) {
  const script = path.join(ROOT, 'scripts', file);
  if (!fs.existsSync(script)) {
    // 없는 것을 통과로 셈하지 않는다 — 이 검사들이 막는 것이 조용한 통과다.
    bad(`${file} 이 없습니다 — ${what}`);
    failed = true;
    continue;
  }
  /* **공통 러너를 쓴다** (`_child-run.js`). 시작 즉시 이름을 찍고, 끝나면 소요 시간을
   * 찍고, 상한을 넘기면 어느 검사인지 적고 중단한다 — 그리고 중단돼도 그때까지의
   * 출력을 보인다.
   *
   * 전에는 여기서 `spawnSync` 를 직접 불렀다. 출력이 버퍼에 갇혀서, 자식이 12분을
   * 돌아도 화면에는 **한 글자도** 안 나왔다(2026-10-07 TYIT). 판독 규칙(`[보임]`·
   * `[못잼]`)은 러너가 그대로 들고 있다. */
  const r = runCheck({ file: script, label: what, limitMs: CHILD_LIMIT_MS, log: console.log, ok, bad });
  /* 통과·실패 화면과 `[보임]`·`[못잼]` 판독은 러너가 들고 있다 — 그 경위와 전례는
   * `_child-run.js` 의 `visibleLines` 주석에 있다. 여기서는 집계만 한다. */
  if (!r.ok) failed = true;
  crossTimes.push({ label: what, ms: r.ms });
}
/* 구간 합계와 예산. PF 인계 문서가 요구하는 「전체 소요 시간」의 재료이기도 하다.
 * 예산을 넘겨도 **여기까지 와서** 알린다 — 중간에 끊으면 나머지 검사의 상태를 모르는데
 * 그 모름이 「이상 없음」 으로 읽힌다. */
if (!reportPhase({
  label: '코드 일관성', ms: Date.now() - crossStarted, children: crossTimes,
  budgetMs: PHASE_BUDGET_MS, log: console.log, bad,
})) failed = true;

/* **파이썬 시험도 관문이 돌린다.** 안 그러면 사람이 손으로 돌릴 때만 도는데,
 * 이 저장소는 이미 같은 일을 겪었다 (`.githooks/pre-commit:234` 참조 — `test-push-gate.sh`
 * 는 2026-08-29 에 만들어졌지만 아무 데서도 부르지 않았다). 위 CROSS_CHECKS 와 같은
 * PASS/FAIL 모양·같은 종료코드 판정을 쓴다 — 새 방식을 안 들인다.
 *
 * **목록을 손으로 안 적는다 (2026-09-03).** 전에는 `slack-sync` 넷을 여기 박아 두었는데,
 * 그 사이 시험 파일이 18개로 늘도록 이 목록은 그대로였다 — 그리고 **늘어난 쪽에
 * 개인정보를 지키는 시험 셋**(`test_screen_personal.py`·`test_xlsx_to_blocks.py`·
 * `test_review_batch.py`)이 들어갔다. 마스킹 정규식을 고치고 `npm run check` 를 돌리면
 * 초록이 나오는데 마스킹 시험은 한 번도 안 돈 상태였다. 훅도 안 부른다
 * (`.githooks/pre-commit` 은 실물 스크립트의 `--gate` 만 부른다).
 *
 * 목록을 여기 또 적으면 반드시 어긋나므로(위 `manifestScopes` 와 같은 이유) **찾아서
 * 돌린다.** 새 시험을 만들면 이름 규칙(`test_*.py`)만 지켜도 저절로 관문에 들어온다.
 *
 * 찾기가 깨지면 0개가 되고 **0개는 조용한 통과다.** 그래서 아래에서 0개를 실패로 센다. */
const PY_TEST_ROOT = path.join(ROOT, '.claude', 'skills');
const PY_SKIP_DIRS = new Set(['node_modules', 'venv', '.venv', '__pycache__', '.git']);

function findPyTests(dir) {
  const out = [];
  if (!fs.existsSync(dir)) return out;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory()) {
      if (PY_SKIP_DIRS.has(e.name)) continue;
      out.push(...findPyTests(path.join(dir, e.name)));
    } else if (/^test_.*\.py$/.test(e.name)) {
      out.push(path.relative(ROOT, path.join(dir, e.name)).replace(/\\/g, '/'));
    }
  }
  return out;
}

const PY_TESTS = findPyTests(PY_TEST_ROOT).sort();

// `python` 이라는 이름이 없는 곳이 있다 — 리눅스(VM)에는 `python3` 만 깔린 경우가 흔하다.
// check-roots.js·check-shared-rules.js 와 같은 방식이다.
const PY_NAMES = process.env.PYTHON ? [process.env.PYTHON] : ['python', 'python3'];

function findPython() {
  for (const name of PY_NAMES) {
    const probe = spawnSync(name, ['--version'], { encoding: 'utf-8' });
    if (!probe.error && probe.status === 0) return name;
  }
  return null;
}

/* 이 시험들(특히 test_verify_archive.py)은 임시 저장소를 만들어 **그 안에** `.env` 를
 * 써 두고, `_shared/paths.py` 가 그것을 읽어 자기만의 격리된 자료 뿌리를 보게 만든다
 * (paths.py 의 우선순위: 환경변수 → .env → 이웃 폴더). 그런데 여기(check-setup.js)는
 * 이미 dotenv 로 진짜 자료 저장소의 HERMES_DATA_ROOT 를 process.env 에 실어 둔 상태라,
 * 그대로 물려주면 환경변수가 이겨서 시험이 자기 임시 저장소가 아니라 **진짜 아카이브**를
 * 보게 되고 `git show :경로` 가 저장소 밖 파일을 찾다 rc=2 로 죽는다 — 2026-09-02 에
 * `npm run check` 로 처음 이 자리에서 실제로 겪었다(사람이 손으로 돌릴 때는 셸에
 * 이 값이 없어 한 번도 안 드러났다). check-roots.js 의 「환경변수를 뺀 채로」와
 * 같은 방식으로 이 값만 지우고 물려준다. */
const PY_ENV = { ...process.env };
delete PY_ENV.HERMES_DATA_ROOT;

const py = findPython();
pySkipped = []; // 종료코드 2 로 「못 쟀다」고 말한 시험들 (선택 의존성 없음)
if (!PY_TESTS.length) {
  // 찾기가 깨져 0개가 된 것은 「시험이 없다」가 아니라 「안 돌렸다」다.
  bad(`${path.relative(ROOT, PY_TEST_ROOT)} 아래에서 test_*.py 를 하나도 못 찾았습니다 — 파이썬 시험이 한 개도 안 돌았습니다`);
  failed = true;
} else if (!py) {
  // 조용히 건너뛰지 않는다 — 파이썬이 없어 관문이 안 돈 것은 "이상 없음"이 아니다.
  bad(`파이썬 시험 ${PY_TESTS.length}개를 못 돌렸습니다 — python/python3 를 찾지 못했습니다 (PATH 확인)`);
  failed = true;
} else {
  console.log(`      파이썬 시험 ${PY_TESTS.length}개 (${py})`);
  for (const rel of PY_TESTS) {
    const script = path.join(ROOT, rel);
    if (!fs.existsSync(script)) {
      bad(`${rel} 이 없습니다`);
      failed = true;
      continue;
    }
    const r = spawnSync(py, [script], { encoding: 'utf-8', env: PY_ENV });
    if (r.status === 0) {
      ok(rel);
    } else if (r.status === 2) {
      /* **종료코드 2 = 못 쟀음** (실패가 아니다). 선택 의존성이 없어 시험이 아예
       * 안 돈 것이다 — `openpyxl` 이 그 경우이고, 그것은 **VM 에 일부러 안 깐다**
       * (`deploy/setup.sh` 의 「3/9 기본 패키지」 · `deploy/test-setup-packages.sh` ③).
       *
       * 전에는 그 넷도 종료코드 1 이라 ✗ 로 세었고, **VM 의 `npm run check` 는 영구히
       * 「점검 실패」**였다 (2026-09-06 VM 실측). 영구히 빨간 검사는 진짜 고장이 나도
       * 화면이 그대로라 구별되지 않는다 — 이 파일이 여러 자리에서 배운 것과 같은 모양이다.
       *
       * **조용히 건너뛰지는 않는다.** 이 파일의 규칙은 「안 돌린 것을 이상 없음으로
       * 읽지 않는다」이므로, 못 쟀다는 것을 화면에 적고 아래 요약에서 개수로 다시 센다.
       * 노트북에서 무엇이 모자란지는 `npm run doctor` 가 따로 본다(⑥ python + openpyxl).
       * 이 규약을 지키는 검사는 `scripts/check-optional-dep-signal.js` 다. */
      pySkipped.push(rel);
      console.log(`  · ${rel} — 못 쟀습니다 (선택 의존성 없음)`);
      for (const line of `${r.stderr || ''}${r.stdout || ''}`.trim().split('\n')) {
        if (line.trim()) console.log(`      ${line}`);
      }
    } else {
      bad(`${rel}  (${py} ${rel})`);
      for (const line of `${r.stderr || ''}${r.stdout || ''}`.trim().split('\n')) {
        if (line.trim()) console.log(`      ${line}`);
      }
      failed = true;
    }
  }
  if (pySkipped.length) {
    console.log(`      파이썬 시험 ${pySkipped.length}개는 **안 돌았습니다** — 위 사유를 보세요 (npm run doctor 로 준비물 확인)`);
  }
}

/* **셸 시험(`deploy/test-*.sh`)도 관문이 돌린다 (2026-09-03).**
 *
 * 바로 위 파이썬 블록과 같은 이유다 — 넷 다 만들어져 있었는데 **어느 자동 관문도 안 불렀다.**
 * `package.json` 에도 `.githooks/` 에도 없었고, 그중 `test-setup-guard.sh` 는 2026-09-01 에
 * 만들어진 뒤로 사람이 손으로 칠 때만 돌았다. 이 파일이 파이썬 목록에서 이미 배운 것을
 * 그대로 따른다: **목록을 여기 또 적지 않고 찾아서 돌린다**(`deploy/test-*.sh`), 그리고
 * **0개는 조용한 통과이므로 실패로 센다.**
 *
 * 재는 대상은 `deploy/setup.sh` 다 — VM 을 세우는 자리라 우리 기계에서는 영영 안 밟는데,
 * 깨지면 07:00 자동 반영이 VM 에서만 조용히 멈춘다.
 *
 * ── bash 를 어떻게 찾나 ──
 *
 * **윈도우에서 `bash` 는 PATH 에 없다.** Git for Windows 가 PATH 에 올리는 것은
 * `…\Git\cmd` 뿐이고 `bash.exe` 는 `…\Git\bin` 에 있다 — 2026-09-03 실측: PowerShell 에서
 * `node -e "spawnSync('bash',…)"` 가 `ENOENT`. 그대로 두면 이 PC 에서 `npm run check` 가
 * **매번 빨간 줄**을 내는데, 그건 고칠 수 없는 빨강이라 곧 화면 전체를 안 읽게 만든다.
 * 그래서 **git 자리에서 짚어 낸다** — git 은 이 저장소의 하드 전제이고(`npm run doctor` 가
 * 없으면 `✗`), `git --exec-path` 는 `…/Git/mingw64/libexec/git-core` 를 주므로 거기서
 * 세 단계 올라가면 `…/Git/bin/bash.exe` 다.
 *
 * **윈도우에서는 그쪽을 PATH 의 `bash` 보다 먼저 본다.** WSL 을 깐 기계는 PATH 에
 * `C:\Windows\System32\bash.exe`(WSL 런처)가 있는데, 그건 윈도우 경로를 인자로 못 받아
 * 시험이 「없는 파일」로 죽는다.
 *
 * ── 못 찾으면 ──
 *
 * **조용히 건너뛰지 않고 실패로 낸다.** 위 파이썬과 같은 판단이다 — 셸이 없어 관문이 안 돈
 * 것은 「이상 없음」이 아니다. `[못잼]` 으로 접지 않는 이유는 **bash 가 없는 것이 정상인
 * 자리가 없어서**다: VM(데비안)에도, 맥·리눅스에도, git 을 깐 윈도우에도 bash 가 있다.
 * 「없는 것이 정상」인 경우에 쓰는 표시를 여기 쓰면 진짜 고장이 접힌다. */
const SH_TEST_DIR = path.join(ROOT, 'deploy');

function findShTests(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true })
    .filter((e) => e.isFile() && /^test-.*\.sh$/.test(e.name))
    .map((e) => path.relative(ROOT, path.join(dir, e.name)).replace(/\\/g, '/'));
}

const SH_TESTS = findShTests(SH_TEST_DIR).sort();

function findBash() {
  const candidates = [];
  if (process.env.BASH) {
    candidates.push(process.env.BASH);
  } else {
    if (process.platform === 'win32') {
      const ep = spawnSync('git', ['--exec-path'], { encoding: 'utf-8' });
      const execPath = !ep.error && ep.status === 0 ? (ep.stdout || '').trim() : '';
      if (execPath) candidates.push(path.join(execPath, '..', '..', '..', 'bin', 'bash.exe'));
    }
    candidates.push('bash');
  }
  for (const c of candidates) {
    const probe = spawnSync(c, ['--version'], { encoding: 'utf-8' });
    if (!probe.error && probe.status === 0) return c;
  }
  return null;
}

const sh = findBash();
if (!SH_TESTS.length) {
  // 찾기가 깨져 0개가 된 것은 「시험이 없다」가 아니라 「안 돌렸다」다.
  bad(`${path.relative(ROOT, SH_TEST_DIR)} 아래에서 test-*.sh 를 하나도 못 찾았습니다 — 셸 시험이 한 개도 안 돌았습니다`);
  failed = true;
} else if (!sh) {
  bad(`셸 시험 ${SH_TESTS.length}개를 못 돌렸습니다 — bash 를 찾지 못했습니다`
    + ' (윈도우는 Git for Windows 의 …\\Git\\bin\\bash.exe · 다른 곳은 PATH · 직접 지정은 BASH 환경변수)');
  failed = true;
} else {
  console.log(`      셸 시험 ${SH_TESTS.length}개 (${sh})`);
  for (const rel of SH_TESTS) {
    // 경로는 ROOT 기준 상대경로 + 슬래시로 넘긴다. 윈도우 절대경로(`C:\…`)를 그대로
    // 주면 셸이 `\` 를 이스케이프로 읽을 수 있고, 시험 스크립트는 자기 위치를 `$0` 에서
    // 잡으므로(`HERE=$(cd "$(dirname "$0")" && pwd)`) cwd 만 맞으면 된다.
    const r = spawnSync(sh, [rel], { cwd: ROOT, encoding: 'utf-8' });
    if (r.status === 0) {
      ok(rel);
    } else {
      bad(`${rel}  (bash ${rel})`);
      for (const line of `${r.stderr || ''}${r.stdout || ''}`.trim().split('\n')) {
        if (line.trim()) console.log(`      ${line}`);
      }
      failed = true;
    }
  }
}

}

// 2. 환경변수
//
// requireEnv 는 "비어 있지 않은가"만 본다. .env.example 을 복사만 하고 값을 채우지 않으면
// `xoxb-` 같은 접두사만 남는데, 그것도 비어 있지는 않아서 그대로 통과해 버린다.
// 접두사 뒤에 실제 값이 붙어 있는지까지 확인한다. (실제 토큰은 50자 이상)
let env = {}, usable = {};
if (mode !== 'archive') {
const TOKENS = {
  SLACK_BOT_TOKEN: { prefix: 'xoxb-', where: 'api.slack.com/apps → 내 앱 → OAuth & Permissions' },
  SLACK_APP_TOKEN: { prefix: 'xapp-', where: 'api.slack.com/apps → 내 앱 → Basic Information → App-Level Tokens' },
  ANTHROPIC_API_KEY: { prefix: 'sk-ant-', where: 'console.anthropic.com → API Keys' },
};

console.log('\n[2/6] 환경변수');
env = requireEnv(Object.keys(TOKENS));

for (const [k, { prefix, where }] of Object.entries(TOKENS)) {
  const v = env[k];
  if (!v.startsWith(prefix)) {
    bad(`${k} 은 ${prefix} 로 시작해야 합니다 — ${where}`);
  } else if (v.length <= prefix.length + 10) {
    bad(`${k} 이 .env.example 의 예시값 그대로입니다 (${v.length}자) — ${where} 에서 실제 값을 복사해 .env 에 붙여넣으세요`);
  } else {
    usable[k] = true;
    ok(`${k} (${prefix}…, ${v.length}자)`);
    continue;
  }
  failed = true;
}

}

/* config.json 의 models.*.provider 를 여기서 확인한다 — 안 그러면 오타가 통과했다가
 * 다음 예정 실행(다이제스트) 에 가서야 createModel 이 던진다. 그때는 오타 낸 사람이
 * 이미 떠난 뒤다. 목록은 PROVIDERS 에서 그대로 가져온다 — 여기 따로 적으면 provider
 * 를 새로 더하는 날 한쪽만 고쳐진다.
 *
 * **토큰 검사 밖에 둔다.** 설정을 읽는 데는 토큰이 필요 없는데 위 블록 안에 있으면
 * `check:archive` 에서 통째로 건너뛴다 — 아카이브만 도는 자리가 생기는 날 그쪽은
 * 오타를 영영 못 본다. 두 검사가 한 관문에 묶일 이유가 없다. */
for (const [name, spec] of Object.entries(config.models || {})) {
  if (name.startsWith('_') || !spec || typeof spec !== 'object') continue; // 주석용 키(_qaMaxTokens 등)
  const provider = spec.provider === undefined ? 'anthropic' : spec.provider;
  if (PROVIDERS[provider]) {
    ok(`models.${name}.provider = ${provider}`);
  } else {
    bad(`models.${name}.provider 가 ${JSON.stringify(provider)} 로 모르는 값입니다 — 쓸 수 있는 값: ${Object.keys(PROVIDERS).join(', ')}`);
    failed = true;
  }
}

if (mode !== 'live') {
// 3. 아카이브
console.log('\n[3/6] 슬랙 아카이브');
try {
  assertArchive();
  const all = listAllChannels();
  const archived = listArchivedChannels();
  ok(`${ARCHIVE_DIR}`);
  ok(`채널 ${all.length}개 (md 파일 있는 것 ${archived.length}개)`);
  const priv = all.filter((c) => c.private).map((c) => `#${c.name}`);
  ok(`비공개 지정: ${priv.join(', ') || '없음'} — 공개 채널 답변에서 인용 차단됨`);

  /* 가리기(redactPrivateMentions)가 **지금 몇 줄을 지우고 있는지** 보여준다.
   *
   * 가리기는 채널 이름이 든 줄을 통째로 뺀다. 이름이 전부 `#채널` 참조로만 쓰이는
   * 채널은 딱 맞아떨어지지만, **일상 업무 낱말과 같은 이름**을 쓰는 채널이 하나라도
   * 있으면 그런 말이 든 자료가 들어오는 순간 그 줄이 공개 답변에서 **조용히** 사라진다
   * — 봇도 사람도 모른다.
   * 숫자를 눈에 보이게 둬서, 갑자기 늘면 알아채게 한다. */
  const countLines = (dir) => {
    const tally = new Map(config.privateChannels.map((c) => [c, 0]));
    const walk = (d) => {
      if (!fs.existsSync(d)) return;
      for (const e of fs.readdirSync(d, { withFileTypes: true })) {
        const p = path.join(d, e.name);
        if (e.isDirectory()) walk(p);
        else if (e.name.endsWith('.md')) {
          for (const line of fs.readFileSync(p, 'utf8').split('\n')) {
            for (const c of config.privateChannels) if (line.includes(c)) tally.set(c, tally.get(c) + 1);
          }
        }
      }
    };
    walk(dir);
    return tally;
  };
  const convo = countLines(CHANNELS_DIR);
  const docs = DOC_PROJECTS_DIR ? countLines(DOC_PROJECTS_DIR) : new Map();
  const parts = config.privateChannels.map(
    (c) => `#${c} ${convo.get(c) || 0}+${docs.get(c) || 0}줄`,
  );
  ok(`가리기가 지우는 줄 (대화+문서): ${parts.join(' · ')}`);
  console.log('      비공개 채널 이름이 든 줄은 그 자리를 못 보는 사람에게 통째로 빠집니다.');
  console.log('      갑자기 늘었다면 채널 이름이 일상 업무 낱말과 겹친 것일 수 있습니다.');

  /* 대화 색인은 **상한도 접기도 없이** 통째로 실린다 (buildArchiveBriefSplit).
   * 문서 색인은 6,000자에서 접혀 더 안 늘지만 이쪽은 채널이 늘면 그대로 는다 —
   * 2026-08-05 18,416자 → 2026-08-17 25,982자. 질문마다 붙는 고정비라
   * 조용히 오르는 것을 여기서 보이게 둔다. 넘어도 봇은 정상 동작한다. */
  const ARCHIVE_BRIEF_WARN = config.limits.archiveBriefWarnChars ?? 30000;
  const archiveBrief = buildArchiveBriefSplit({ access: FULL_ACCESS });
  const briefChars = archiveBrief.common.length;
  if (briefChars > ARCHIVE_BRIEF_WARN) {
    bad(
      `대화 색인이 ${briefChars.toLocaleString()}자로 경고선(${ARCHIVE_BRIEF_WARN.toLocaleString()}자)을 넘었습니다 — ` +
      `질문마다 실리는 고정비가 오릅니다. slack-export/index.md 와 채널 md 의 '핵심 쟁점' 줄을 줄이세요`,
    );
    // 경고선은 상한이 아니다. 봇은 정상 동작하므로 failed 로 올리지 않는다.
  } else {
    ok(`대화 색인 ${briefChars.toLocaleString()}자 (경고선 ${ARCHIVE_BRIEF_WARN.toLocaleString()}자) · 추가분 최대 ${archiveBrief.extra.length.toLocaleString()}자`);
  }

  /* 채널당 `핵심 쟁점` 상한 (WHK 결정 2026-09-21, 400자).
   *
   * **총합만 재면 규칙을 못 잰다** — 위의 경고선은 색인 전체를 보므로, 한 채널이 상한의
   * 세 배가 되어도 총합이 여유 안이면 조용하다. 색인이 자라는 것의 93%가 이 줄이고
   * (2026-09-20 실측), 2026-09-21 에 실제로 경고선을 넘었다.
   *
   * **막지 않고 보이기만 한다** — 규칙이 생긴 날 이미 14채널이 넘고 있었다. 그것을
   * 실패로 올리면 관문이 곧 꺼진다(이 저장소가 이미 겪은 자리). 줄이는 것은 요약 변경
   * 승인 절차를 타야 하므로 `slack-sync`·`archive-inbox` 가 그 줄을 만질 때 한다.
   *
   * **빼는 절차와 기계는 `issue-trim` 스킬에 있다** — 이 줄이 ⚠ 를 낼 때 가는 곳이다.
   * 이 상수는 그 스킬의 `verify_trim.py` 가 **이름으로 찾아 읽는다.** 이름을 바꾸면
   * 거기서 시끄럽게 죽는다(조용한 기본값 없음) — 상한을 두 곳에 적지 않으려는 것이다. */
  const ISSUE_LINE_MAX = 400;
  const over = [];
  for (const md of fs.readdirSync(CHANNELS_DIR).filter((f) => f.endsWith('.md'))) {
    const line = fs.readFileSync(path.join(CHANNELS_DIR, md), 'utf8')
      .split('\n').find((l) => l.startsWith('> **핵심 쟁점**'));
    if (line && line.replace(/\r$/, '').length > ISSUE_LINE_MAX) {
      over.push([md.replace(/\.md$/, ''), line.replace(/\r$/, '').length]);
    }
  }
  if (over.length) {
    over.sort((a, b) => b[1] - a[1]);
    /* **✗ 가 아니라 ⚠ 다** — 규칙이 생긴 날 이미 14개였다. 매 회차 ✗ 로 띄우면
     * 「원래 빨간 것」이 되어 진짜 실패와 구별이 안 된다(이 저장소가 겪은 자리).
     * 목록도 앞의 다섯만 낸다 — 열넷을 매번 늘어놓으면 화면에서 안 읽힌다. */
    const head = over.slice(0, 5).map(([n, c]) => `${n} ${c}`).join(' · ');
    const rest = over.length > 5 ? ` · 그 밖 ${over.length - 5}개` : '';
    console.log(
      `  ⚠ '핵심 쟁점' 줄이 채널당 ${ISSUE_LINE_MAX}자를 넘는 채널 ${over.length}개 — ` +
      `그 줄을 만질 때 issue-trim 스킬로 끝난 쟁점을 빼서 줄이세요 (막지 않습니다): ${head}${rest}`,
    );
  } else {
    ok(`'핵심 쟁점' 줄이 전부 채널당 ${ISSUE_LINE_MAX}자 안입니다`);
  }
} catch (e) {
  bad(e.message);
  failed = true;
}

// 4. 문서 아카이브
//
// 브리프 길이를 함께 찍는 이유: 이건 매 질문의 시스템 프롬프트에 통째로 실린다.
// 문서가 늘수록 조용히 부풀어 캐시 생성 비용만 올라가므로 눈에 보이게 둔다.
const DOC_BRIEF_LIMIT = config.limits.docBriefMaxChars ?? 6000;

// 바닥·경고선(옛 DOC_BRIEF_FLOOR_WARN)은 indexGauge() 의 foldedSeries 로 대체됐다 —
// 아래 [4/6] 바닥 블록 주석 참조. indexGauge() 자체는 src/documents.js 에 있다 — 이
// 파일은 import 만으로 도는 순수 함수를 담을 수 없어(전체가 로드되며 곧장 점검을
// 시작한다) 따로 검사(check-series-fold.js)로 지킬 수 없었다. 정의·docstring 은 그쪽에 있다.

console.log('\n[4/6] 문서 아카이브');
if (!DOCS_DIR) {
  console.log('  - 꺼져 있음 (config.json 에 documentsPath 없음). 문서 검색 도구가 붙지 않습니다.');
} else if (!hasDocuments()) {
  bad(`${DOCS_DIR} 가 없거나 projects/ 폴더가 비어 있습니다 — doc-archive 스킬을 먼저 돌리세요`);
  /* **✗ 를 찍었으면 종료코드도 올린다 (2026-09-03).** 여기만 `failed = true` 가 빠져 있어서,
   * 문서 아카이브가 통째로 없는 상태에서 이 아래 문서 검사가 **전부 안 도는데** 마지막 줄이
   * 「점검 통과 — npm start 로 실행하세요」였다 (사본에서 빈 문서 폴더로 재현: ✗ 를 찍고
   * exit 0). `documentsPath` 를 설정해 두고 그 폴더가 없는 것은 설정과 실물이 갈린 상태다 —
   * 문서 검색 도구를 아예 안 붙이기로 한 위 `!DOCS_DIR` 갈래(`-` 로 적는다)와 다르다. */
  failed = true;
} else {
  const projects = listProjects();
  const docs = listDocuments();
  const rounds = docs.reduce((n, d) => n + d.entries.length, 0);
  ok(`${DOCS_DIR}`);
  ok(`사업장 ${projects.length}개 · 문서 ${docs.length}개 · 회차 ${rounds}건`);

  /* index.md 가 주장하는 숫자·목록을 방금 센 실물과 대본다.
   *
   * **✗ 가 아니라 ⚠ 다.** 이 값은 봇에 안 간다 — 봇 색인은 각 md 메타에서 만들어지고
   * index.md 에서 실리는 것은 「변환하지 못한 것」 절 하나뿐이다. 틀린 숫자를 보는 것은
   * 사람뿐이라 `npm run check` 를 통째로 실패시킬 이유가 없다.
   *
   * **못 읽으면 「맞음」이 아니라 ⚠ 다** — auditDocIndex 가 그렇게 돌려준다. */
  const INDEX_MD = path.join(DOCS_DIR, 'index.md');
  if (!fs.existsSync(INDEX_MD)) {
    console.log(`  ⚠ ${INDEX_MD} 가 없어 색인 목록을 대보지 못했습니다`);
  } else {
    const problems = auditDocIndex({
      indexText: fs.readFileSync(INDEX_MD, 'utf8').replace(/\r\n/g, '\n'),
      docs,
      projects,
    });
    if (problems.length === 0) {
      ok('index.md 의 숫자와 목록 줄 수가 실물과 맞습니다');
    } else {
      console.log(`  ⚠ index.md 가 실물과 ${problems.length}곳 다릅니다 (봇에는 안 갑니다 — 사람이 보는 값입니다)`);
      for (const p of problems) console.log(`      ${p}`);
    }
  }

  const broken = docs.filter((d) => d.broken);
  if (broken.length) {
    bad(`메타 블록을 못 읽은 문서 ${broken.length}개 — 비공개로 처리되어 답변에 안 나옵니다:`);
    broken.forEach((d) => console.log(`      ${d.project}/${d.file}`));
    failed = true;
  }
  const priv = docs.filter((d) => d.private && !d.broken);
  ok(`비공개 문서 ${priv.length}개 — 공개 채널 답변에서 차단됨`);

  /* companyWideDocProjects(전사 종합 카드가 훑는 폴더 목록, 자료 저장소 config.json)가
   * 실물 폴더와 대보이는지 확인한다. 비어 있는 것은 킬 스위치(기능을 일부러 끔)라
   * 조용히 넘어간다 — ✗ 를 찍을 자리는 "목록에는 있는데 실물이 없는" 경우뿐이다.
   * 안 재면 오타·폴더 개명이 킬 스위치와 똑같이 "카드가 안 뜬다"로만 보여 구분이
   * 안 된다(2026-09-11 최종 검토 Important 2). 선례는 위 663-667 — 같은 모양이다. */
  if (!Array.isArray(companyWideDocProjects)) {
    bad(`config.json 의 search.companyWideDocProjects 는 배열이어야 합니다 (지금 타입: ${typeof companyWideDocProjects})`);
    failed = true;
  } else if (companyWideDocProjects.length) {
    const missing = companyWideDocProjects.filter((f) => !projects.includes(f));
    if (missing.length) {
      bad(`companyWideDocProjects 에 실물 폴더가 없는 이름 ${missing.length}개: ${missing.join(' · ')} — 전사 종합 카드가 그만큼 조용히 빠집니다`);
      failed = true;
    } else {
      ok(`companyWideDocProjects ${companyWideDocProjects.length}개 폴더 모두 실물과 맞습니다`);
    }
  }

  /* 안전망(outsideWhenNarrowedMaxHits)을 0 이하로 두면 — 그 자체는 정당한 되돌리기
   * 스위치다(archive.js:965) — autoNarrow 폴백에서 전사 종합 카드가 버려진다.
   *
   * `check-outside-hits.js` 는 이미 이 설정값을 「되돌리기 상태」로 알아보고 [1/8]~[4/8]·
   * [6/8]·[7/8]을 「재지 못했다」고 적지만, 그 문구에 카드 얘기가 없다 — 그래서 안전망을
   * 끄면 카드가 조용히 반쪽만 죽는데 그 사실이 npm run check 어디에도 안 보였다.
   * 이 표시가 정확히 이런 「조용히 초록」을 막으려고 만든 장치다(위 337-349 주석,
   * check-outside-hits.js:97-110 참고).
   *
   * **문구는 `_card-warnings.js` 가 만든다** — 여기 본문에 박아 두면 자동으로 부를 방법이
   * 없어 수동 확인만 남는다(2026-09-11 이월 Minor 고침). 조건과 문구는 그 파일에서
   * `check-doc-card-kill-switch.js` [3/3] 이 동작으로 재고, 이 호출이 살아 있는지는 같은
   * 검사의 배선 줄이 본다. */
  const outsideMax = config.limits.outsideWhenNarrowedMaxHits ?? 0;
  reportCardWarnings({ companyWideCount: companyWideDocProjects.length, outsideMax });

  /* 색인은 이제 두 덩이다 — 누구에게나 같은 공통분과 권한별 추가분 (buildDocumentsBriefSplit).
   * 상한은 공통분에만 건다. 추가분은 자기 예산(docBriefPrivateMaxChars)을 따로 쓰고,
   * 합쳐서 재면 비공개 채널을 다 볼 수 있는 이 자리(FULL_ACCESS)에서만 넘쳐 보인다. */
  const { common, extra } = buildDocumentsBriefSplit({ access: FULL_ACCESS });
  if (common.length > DOC_BRIEF_LIMIT) {
    bad(`문서 색인 공통분이 ${common.length}자로 상한(${DOC_BRIEF_LIMIT}자)을 넘었습니다 — 시스템 프롬프트가 비대해집니다`);
    failed = true;
  } else {
    ok(`문서 색인 공통분 ${common.length}자 (상한 ${DOC_BRIEF_LIMIT}자) · 추가분 최대 ${extra.length}자`);
  }

  /* 비공개 추가분(extra)은 공통분과 별도 예산(docBriefPrivateMaxChars)을 쓴다 — 위
   * buildDocumentsBriefSplit 호출부 주석 참조. 여유가 예산의 10% 아래로 좁아지면 곧
   * 접히기 시작해도 아무 계기가 없었다 (2026-09-10 todo). 대화 추가분(위 661행
   * archiveBrief.extra)은 예산 자체가 없는 자리라 여기서 안 잰다. */
  const PRIVATE_LIMIT = config.limits.docBriefPrivateMaxChars ?? 1500;
  const privateMargin = PRIVATE_LIMIT - extra.length;
  if (privateMargin < Math.ceil(PRIVATE_LIMIT * 0.1)) {
    console.log(`  ⚠ 문서 색인 비공개 추가분 여유가 ${privateMargin}자입니다 (예산 ${PRIVATE_LIMIT}자) — 곧 접히기 시작해도 아무 계기가 없던 자리 (2026-09-10 todo)`);
  }

  /* 총량은 지표가 못 된다 — 상한 안에서 최대한 펼치는 구조라 무엇을 해도 늘 상한 근처다.
   * 봐야 할 것은 **접을 수 있는 것을 전부 접었을 때 남는 양**(바닥)과 상한 사이의 거리다.
   * indexGauge() 가 예산 0 으로 그 바닥을 재고, check-setup·pre-commit·7.5 점검표가
   * 전부 이 함수 하나를 본다 — 세 자리가 따로 계산하면 한쪽만 고쳤을 때 조용히 갈린다.
   *
   * 바닥이 상한을 넘어도 **아무것도 안 깨진다.** 접기를 멈추고 길어진 채 낼 뿐이다.
   * 그때부터 limits.docBriefMaxChars 설정이 아무 의미가 없어지는데, 그 사실이
   * 어디에도 안 드러나는 것이 문제였다 (2026-08-05 부터 열려 있던 질문). 아래
   * `floor > DOC_BRIEF_LIMIT` 갈래는 **2026-09-10 부터 시리즈도 접히므로 구조적으로
   * 도달하지 않는다** — 도달하면 접기 루프 자체가 고장 난 것이라는 신호로 남긴다. */
  const gauge = indexGauge();
  if (gauge.floor > DOC_BRIEF_LIMIT) {
    bad(`문서 색인 바닥이 ${gauge.floor}자로 상한(${DOC_BRIEF_LIMIT}자)을 넘었습니다`
      + ` — 접기로는 못 줄입니다. ${gauge.parts}`);
    failed = true;
  }

  /* 바닥·경고선 대신 「접힌 시리즈」가 경보다 (2026-09-10) — 시리즈도 접히게 된 뒤로
   * 바닥이 상한을 넘는 일은 구조적으로 없고, 공간 부족의 첫 증상은 시리즈가 색인에서
   * 사라지는 것이다. 접혀도 검색에는 걸리지만, 색인에 없으면 봇이 그 문서의 존재를
   * 짐작할 힌트가 준다. 바닥 숫자는 정보로 계속 찍는다 — floorParts 의 조각과 함께. */
  if (gauge.foldedSeries.length) {
    console.log(`  ⚠ 접힌 시리즈 ${gauge.foldedSeries.length}건 — ${gauge.foldedSeries.join(' · ')}`);
    console.log(`    (색인 공간이 모자라기 시작했습니다. 바닥 ${gauge.floor}자 — ${gauge.parts})`);
  } else {
    ok(`문서 색인 바닥 ${gauge.floor}자 · 접힌 시리즈 0건 — ${gauge.parts}`);
  }
}

}

if (mode !== 'archive') {
// 5. Anthropic
// 키가 아직 예시값이면 호출해 봐야 401 만 돌아온다. 위에서 이미 지적했으므로 건너뛴다.
console.log('\n[5/6] Anthropic API');
if (!usable.ANTHROPIC_API_KEY) {
  console.log('  - 건너뜀 (키를 채운 뒤 다시 돌리세요)');
} else try {
  const anthropic = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
  const m = await anthropic.models.retrieve(config.models.qa.id);
  // max_input_tokens 는 모델에 따라 null 로 올 수 있다 (그대로 나누면 0K 로 찍힌다).
  const ctx = m.max_input_tokens ? ` (context ${(m.max_input_tokens / 1000).toFixed(0)}K)` : '';
  ok(`${m.display_name} 사용 가능${ctx}`);

  // models.retrieve 는 크레딧을 쓰지 않는다. 잔액이 0 이어도 통과하므로
  // 이것만 보고 "준비됐다"고 판단하면 첫 질문에서야 실패를 알게 된다.
  // 실제 추론을 한 번 돌려 확인한다 (토큰 수십 개, 1원 미만).
  await anthropic.messages.create({
    model: config.models.qa.id,
    max_tokens: 16,
    output_config: { effort: 'low' },
    messages: [{ role: 'user', content: 'ping' }],
  });
  ok('실제 호출 성공 — 크레딧 잔액 있음');
} catch (e) {
  const msg = e.message || '';
  if (/credit balance is too low/i.test(msg)) {
    bad('크레딧 잔액이 부족합니다. 키는 유효하지만 호출이 거부됩니다.');
    bad('  → console.anthropic.com → Plans & Billing 에서 크레딧을 충전하세요.');
  } else {
    bad(`API 키 확인 실패: ${msg}`);
  }
  failed = true;
}

// 6. Slack
console.log('\n[6/6] Slack');
if (!usable.SLACK_BOT_TOKEN) {
  console.log('  - 건너뜀 (봇 토큰을 채운 뒤 다시 돌리세요 — 미초대 채널 목록도 이때 나옵니다)');
} else try {
  const slack = new WebClient(env.SLACK_BOT_TOKEN);
  const auth = await slack.auth.test();
  ok(`워크스페이스 ${auth.team} · 봇 ${auth.user} (${auth.user_id})`);

  // 스코프를 먼저 본다. 부족한 채로 채널을 조회하면 missing_scope 만 돌아와
  // "무엇이 빠졌는지"가 아니라 "그 API 가 뭘 요구하는지"만 알게 된다.
  const granted = await grantedScopes(env.SLACK_BOT_TOKEN);
  const wanted = manifestScopes();
  const missingScopes = wanted.filter((s) => !granted.includes(s));

  if (missingScopes.length) {
    bad(`스코프 ${missingScopes.length}/${wanted.length}개가 설치되지 않았습니다: ${missingScopes.join(', ')}`);
    console.log('');
    console.log('    매니페스트를 고쳐 저장하는 것만으로는 반영되지 않습니다. 앱을 다시 설치해야 합니다:');
    console.log('      api.slack.com/apps → 내 앱 → App Manifest 에서 매니페스트가 최신인지 확인');
    console.log('      → Install App → Reinstall to Workspace → 허용');
    console.log('      → 봇 토큰(xoxb-)이 새로 발급되면 .env 를 갱신하세요');
    console.log('');
    failed = true;
    throw new Error('scope-incomplete');
  }
  ok(`스코프 ${wanted.length}개 설치됨`);

  const joined = await listBotChannels(slack);
  ok(`봇이 들어가 있는 채널 ${joined.length}개`);

  /* latest_reply 결측을 센다.
   *
   * 이 필드는 **세 자리**에서 「이 스레드를 볼지」를 정한다:
   *   - src/archive-health/attachments.js  channelAttachments  — 첨부 후보 (폴백 `|| m.ts` 있음)
   *   - src/slack-live.js      fetchRecentSlack    — 실시간 조회 (폴백 없음)
   *   - src/ingest/archive-api.js  grownThreads  — **[정정] 댓글이 들어오는 길** (폴백 없음)
   *
   * 슬랙이 이 필드를 안 주면 뒤 둘은 스레드가 통째로 빠지는데 **에러가 안 난다.**
   * 그래서 한 번 재고 닫지 않고 계속 재는 자리로 둔다 (WHK 2026-08-27).
   *
   * 구간은 새 숫자를 두지 않고 limits.threadLookbackDays 를 그대로 쓴다 — 재는 구간과
   * 게이트가 실제로 도는 구간이 같아야 판정이 성립한다.
   *
   * 채널마다 첫 페이지(200건)만 본다. **그 한계를 화면에 적는다** — 안 적으면
   * 「전부 봤다」로 읽힌다. */
  const lookbackDays = config.limits?.threadLookbackDays ?? 30;
  const oldestTs = String(Math.floor(Date.now() / 1000) - lookbackDays * 86400);
  let threads = 0;
  let missing = 0;
  let unread = 0;
  let truncated = 0;
  const unreadReasons = [];
  for (const ch of joined) {
    try {
      const res = await slack.conversations.history({
        channel: ch.id, oldest: oldestTs, limit: 200,
      });
      for (const m of res.messages || []) {
        if (!m.reply_count) continue;
        threads += 1;
        if (!m.latest_reply) missing += 1;
      }
      // 200건을 넘는 채널은 첫 페이지 뒤가 잘린다 — 더 받아 오지 않고(호출이 늘어난다),
      // 잘렸다는 사실만 「안 셈」과 같은 자리에 남긴다. 안 남기면 「스레드 N건 중 결측 0건」이
      // 오래된 스레드가 표본에서 통째로 빠진 채로 나가 「전부 봤다」로 읽힌다.
      if (res.has_more) truncated += 1;
    } catch (e) {
      // 못 읽은 채널은 「없음」이 아니라 「안 셈」이다. 0 에 섞으면 조용해진다.
      // 사유(에러 이름)도 함께 모은다 — 채널 이름은 비공개일 수 있어 안 적는다.
      unread += 1;
      unreadReasons.push(e?.data?.error || e?.message || '원인 미상');
    }
  }
  const scope = `채널 ${joined.length - unread}개 · 각 최근 200건`
    + (truncated ? ` · 200건에서 잘린 채널 ${truncated}개 (그 앞 스레드는 안 셈)` : '')
    + (unread ? ` · 조회 못 한 채널 ${unread}개 (${summarizeUnreadReasons(unreadReasons)})` : '');
  if (threads === 0) {
    console.log(`  ⚠ 최근 ${lookbackDays}일에 답글 달린 스레드를 하나도 못 봤습니다 (${scope}) — 잰 것이 없어 판정 못 함`);
  } else if (missing === 0) {
    ok(`최근 ${lookbackDays}일 스레드 ${threads}건 중 latest_reply 없는 것 0건 (${scope})`);
  } else {
    console.log(`  ⚠ 최근 ${lookbackDays}일 스레드 ${threads}건 중 latest_reply 없는 것 ${missing}건 (${scope})`);
    console.log('      게이트 세 자리가 이 필드에 기댑니다 — archive-health/attachments.js 의 channelAttachments(첨부 후보),');
    console.log('      slack-live.js 의 fetchRecentSlack(실시간 조회), ingest/archive-api.js 의 grownThreads.');
    console.log('      뒤 둘은 폴백이 없어 스레드가 통째로 빠집니다 — [정정] 댓글이 그 길로 들어옵니다.');
  }

  const live = await listSlackChannels(slack);
  const { actionable, ignored } = uninvitedChannels({
    live,
    known: listAllChannels(),
    joinedIds: new Set(joined.map((c) => c.id)),
    archived: new Set(listArchivedChannels()),
    skip: config.digest?.skipChannels || [],
  });

  if (actionable.length === 0) {
    ok('초대가 필요한 채널이 없습니다');
  } else {
    console.log(`\n  ! 아직 초대되지 않은 채널 ${actionable.length}개`);
    console.log('    각 채널에 들어가서 아래를 입력하세요:\n');
    for (const c of actionable) {
      // 재지 않은 것을 적지 않는다. 예전에는 md 가 없는 것을 「대화 없는 빈 채널」이라고
      // 적었는데, md 가 없는 이유는 대화가 없어서일 수도 skipChannels 라서일 수도
      // 수집이 아직 안 돼서일 수도 있다.
      const facts = [
        c.members === null ? null : `멤버 ${c.members}명`,
        c.hasArchive ? null : '아카이브 md 없음',
        c.inSlack ? null : '슬랙 채널 목록에 없음 (보관됐거나 봇이 못 보는 비공개)',
      ].filter(Boolean);
      console.log(`      #${c.name}${c.private ? '  🔒' : ''}  (${facts.join(' · ')})`);
    }
    console.log(`\n      /invite @${auth.user}\n`);
    console.log('    초대 안 된 채널은 요약과 최신 조회에서 빠집니다.');
    console.log('    (아카이브 md 는 그대로 읽으므로 Q&A 는 계속 동작합니다.)');
  }

  // 안 다루기로 정한 채널까지 「초대하세요」로 안내하면 목록의 절반이 할 일이 아닌 것이
  // 되어 목록 자체를 흘려보게 된다. 빼되 감추지는 않는다.
  if (ignored.length) {
    console.log(`\n  - 안 다루기로 한 채널 ${ignored.length}개 (config.json 의 digest.skipChannels)`);
    console.log(`    ${ignored.map((c) => `#${c.name}`).join(' · ')}`);
  }

  /* 위 `ignored` 는 **실재하면서 skip 인 것**이라, 어느 채널과도 안 맞는 설정 줄은 여기서
   * 그냥 사라진다. 대조 자료를 다 쥐고도 안 보여준 자리다 — 2026-08-10 개명으로 죽은
   * `skipChannels` 한 줄이 21일간 이 화면에서 보이지 않았다. */
  const stale = staleChannelRefs({ live, known: listAllChannels(), lists: {
    privateChannels: config.privateChannels || [],
    skipChannels: config.digest?.skipChannels || [],
  } });
  const renamed = stale.filter((s) => s.kind === 'renamed');
  if (renamed.length) {
    // 경고가 아니라 고장이다 — 그 줄은 막으라고 적어 둔 것을 안 막고 있고, 고치는 것은
    // config.json 한 줄이다. 종료코드를 안 올리면 「✗ 인데 통과」가 되어 곧 안 읽힌다.
    failed = true;
    bad(`설정이 옛 이름을 가리킵니다 ${renamed.length}줄 — 그 줄은 아무것도 안 막습니다`);
    for (const s of renamed) {
      console.log(`      ${s.list} 의 "${s.name}" → 지금 이름은 "${s.now}" 입니다`);
    }
    console.log('      config.json 을 고친 뒤 VM 에서 `sudo systemctl restart hermes` 까지 해야 붙습니다.');
  } else {
    ok('설정의 채널 이름이 슬랙 실물과 맞습니다');
  }
  // 없어진 채널·처음 보는 이름은 대개 그대로 두는 줄이라(커밋 db37d7d) 경고가 아니라
  // 사실로만 적는다. 매일 울리면 위 개명 경고까지 함께 안 읽힌다.
  const rest = stale.filter((s) => s.kind !== 'renamed');
  if (rest.length) {
    const label = (s) => `${s.name}(${s.list === 'privateChannels' ? '비공개' : 'skip'})`;
    const gone = rest.filter((s) => s.kind === 'gone').map(label);
    const unknown = rest.filter((s) => s.kind === 'unknown').map(label);
    const parts = [
      gone.length ? `없어진 채널 ${gone.length}개 — ${gone.join(' · ')}` : null,
      unknown.length ? `슬랙에서 확인 안 됨 ${unknown.length}개 — ${unknown.join(' · ')}` : null,
    ].filter(Boolean);
    console.log(`    설정에만 남은 이름: ${parts.join(' / ')}`);
  }
} catch (e) {
  // 스코프 부족은 바로 위에서 이미 구체적으로 안내했으므로 다시 찍지 않는다.
  if (e.message !== 'scope-incomplete') {
    bad(`Slack 확인 실패: ${e.data?.error || e.message}`);
    if (e.data?.error === 'missing_scope') {
      // needed 는 "빠진 것"이 아니라 "이 API 가 요구하는 전체"다. 오해하지 않도록 그대로 적는다.
      bad(`  이 API 가 요구하는 스코프: ${e.data.needed}`);
    }
    failed = true;
  }
}

}

console.log('\n' + '='.repeat(50));
/* **못 쟀음을 초록 한 줄에 묻지 않는다.** 「전부 통과」와 「몇 개는 안 돌았고 나머지는
 * 통과」는 다른 사실이다 — 묻으면 다음 사람이 안 돈 것을 돈 것으로 읽는다. */
if (mode !== 'all') {
  console.log(mode + ': ' + (failed ? '점검 실패' : '선택 범위 점검 통과') + (pySkipped.length ? ' (못 잰 Python 시험 ' + pySkipped.length + '개)' : '') + ' — 전체 운영 준비 판정은 npm run check');
} else if (failed) {
  console.log('점검 실패 — 위 ✗ 항목을 고치고 다시 돌리세요.\n');
} else if (pySkipped.length) {
  console.log(`점검 통과 (다만 파이썬 시험 ${pySkipped.length}개는 못 쟀습니다 — 위 · 줄) — npm start 로 실행하세요.\n`);
} else {
  console.log('점검 통과 — npm start 로 실행하세요.\n');
}
process.exit(failed ? 1 : 0);

}
