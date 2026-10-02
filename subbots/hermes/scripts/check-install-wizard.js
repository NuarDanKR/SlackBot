#!/usr/bin/env node
/**
 * 설치 마법사(`hermes-install`)가 스스로 갈리지 않았나 · 가리키는 것이 실재하나.
 *
 *     node scripts/check-install-wizard.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * 마법사는 실행되는 코드가 아니라 **사람이 읽는 문서**다. 0~8단계 표가 저장소의 다른 것
 * 열 개를 가리키는데, 그중 하나가 이름이 바뀌거나 없어져도 저장소 안에서는 아무 신호가
 * 안 난다. 그리고 **깨진 것을 발견하는 사람이 우리가 아니다** — 마법사는 우리가 없는
 * 자리에서 남이 밟는 것이라, 어긋남은 새 팀이 설치 도중에 만난다.
 *
 * 실제로 커밋 `6397aa6` 이 7단계가 부르는 것을 고칠 때 **표와 경고문만 고치고 머리말을
 * 안 고쳤다.** 머리말은 `deploy/setup.sh` 를, 표는 `deploy/README.md` 를 가리켰다.
 * 그 스크립트는 문서 일곱 부분 중 ④⑤뿐이라, 머리말을 따라간 팀은 배포키(⑥)로 가는
 * 길이 없다. **「파일이 있나」만 보면 이것이 안 잡힌다** — `deploy/setup.sh` 는 실재한다.
 *
 * ── 대상이 둘이다 ──
 *
 * 마법사(`SKILL.md`)와 **저장소 뿌리의 `CLAUDE.md`** 를 함께 본다. 둘 다 사람이
 * 읽는 문서이고 둘 다 저장소의 다른 것을 가리키며, 둘 다 **가리키는 것이 없어져도
 * 저장소 안에서 아무 신호가 안 난다.** ②(머리말과 표가 같은 것을 가리키나)는
 * 마법사에만 있는 구조라 마법사에만 적용한다.
 *
 * 그래서 둘을 본다:
 *   ① 마법사가 가리키는 것이 실재하나 (git 이 아는 파일인가 · package.json 에 있는가)
 *   ② 머리말과 0~8단계 표가 같은 것을 가리키나
 *
 * ── 여기서 안 보는 것 ──
 *
 * **줄 번호가 맞는지 안 본다** (`src/config.js:40`). 파일만 보고 번호는 뗀다 —
 * 「그 줄이 정말 그 내용인가」는 기계가 못 본다. 지금 이미 하나가 어긋나 있고
 * 이 검사는 그것을 통과시킨다.
 * **가리키는 문서의 내용이 낡았는지도 안 본다.** 있기만 하면 통과다.
 * **마법사가 실제로 뜨는지도 안 본다** — 그건 사람이 새 창을 열어 밟는 일이다.
 *
 * **슬랙에 안 붙는다.** 파일만 읽는 검사라 토큰도 네트워크도 필요 없다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SKILL = path.join(ROOT, '.claude', 'skills', 'hermes-install', 'SKILL.md');

let ok = true;
const fail = (m) => { ok = false; console.error(`✗ ${m}`); };

const src = fs.readFileSync(SKILL, 'utf-8');
const CLAUDE_MD = path.join(ROOT, 'CLAUDE.md');
const claudeSrc = fs.readFileSync(CLAUDE_MD, 'utf-8');

/**
 * 백틱 안의 것을 뽑는다.
 *
 * **뽑기 전에 줄바꿈을 잇는 것이 핵심이다.** 마법사 머리말은 백틱이 줄에 걸쳐 있다 —
 * `npm run` 뒤에서 줄이 바뀌고 다음 줄이 `init-archive` 로 이어진다. 줄바꿈을 그대로
 * 두고 뽑으면 그 조각이 안 걸리고 **그 뒤로 백틱 짝이 통째로 어긋나** 머리말에서 한
 * 개만 뽑힌다. 그러면 아래 ② 가 댈 것이 없어 **조용히 통과한다** — 이 검사가 막으려는
 * 바로 그 모양이다 (2026-09-02 설계 검증에서 실제로 그렇게 나왔다).
 *
 * 코드 블록을 먼저 걷어내는 것은 그 안의 백틱이 짝을 어긋내기 때문이다.
 */
const ticks = (s) =>
  [...s
    .replace(/```[\s\S]*?```/g, ' ')   // 코드 블록은 통째로 뺀다
    .replace(/\s+/g, ' ')              // 줄바꿈으로 끊긴 백틱을 잇는다
    .matchAll(/`([^`]+)`/g)]
    .map((m) => m[1].trim());

/**
 * 백틱 조각을 「npm 명령」과 「파일 이름 모양」으로 가른다. 줄 번호(`:40`)는 뗀다.
 *
 * 규칙이 둘뿐이라 `HERMES_DATA_ROOT`·`not_in_channel`·`--all-channels`·`git pull`
 * 같은 것은 **확장자가 없어 애초에 안 들어온다.** 예외로 적어 둘 필요가 없다.
 */
function classify(list) {
  const npm = new Set();
  const files = new Set();
  for (const t of list) {
    const m = /^npm run ([a-z][\w:-]*)/.exec(t);
    if (m) { npm.add(m[1]); continue; }
    const bare = t.replace(/:\d+$/, '');
    if (/\s/.test(bare)) continue;                  // `git pull` 같은 명령
    if (/\.[A-Za-z0-9]+$/.test(bare)) files.add(bare);
  }
  return { npm, files };
}

/* ── ① 마법사가 가리키는 것이 실재하나 ────────────────────────────────── */

/**
 * 후보로 걸리지만 **코드 저장소에 없는 것이 정상**인 것들.
 *
 * 예외는 함정이다 — `check-stale-paths.js` 가 적어 둔 대로 「다음 사람이 여기 붙여
 * 통과시키는」 자리다. 그래서 반대로 못 박는다: **여기 든 것이 저장소에 있으면 ✗** 다.
 * 진짜 파일을 예외로 옮겨 통과시키는 길이 그 순간 막힌다.
 *
 * **이 맵은 마법사(`targets`)와 CLAUDE.md(`claudeTargets`) 둘 다에 걸린다** — 한 벌만
 * 있고 안 나뉜다. 그래서 여기 한 줄을 더할 때, 그 문자열이 CLAUDE.md 본문에도 우연히
 * 나오면 **CLAUDE.md 쪽 실재 확인도 아무 신호 없이 함께 면제된다.** 마법사 쪽 오탐을
 * 없애려고 짧은 맨 이름을 넣을 때 특히 조심한다 — 두 바닥(`FLOOR`·`CLAUDE_FLOOR`)이
 * 따로인 것과는 별개로, 이 맵은 갈라져 있지 않다.
 */
const OUTSIDE = new Map([
  ['.env', '설치 중에 사람이 만든다'],
  ['.hermes-install.json', '설치 진행 상태 — gitignore 에 있다'],
  ['config.json', '자료 저장소 쪽이다'],
  ['check-fixtures.json', '자료 저장소 쪽이다 — 설치 중에 사람이 만든다'],
  ['documents/index.md', '자료 저장소 안이다'],
  ['setup.sh', '맨 이름이다 — 실물은 deploy/setup.sh'],
  ['config.js', '맨 이름이다 — 실물은 src/config.js'],
  ['join_channels.py', '맨 이름이다 — 실물은 .claude/skills/doc-archive/scripts/join_channels.py'],
  ['auth.test', '슬랙 API 이름이지 파일이 아니다'],
  ['foo.js', '예시용 가짜 이름이다 — 「행 번호로 가리키지 말라」 규칙의 본보기라 실물이 없다'],
  ['.githooks', '폴더 이름이다 — git ls-files 는 폴더를 안 낸다'],
]);

/**
 * 뽑아낸 것이 이 아래면 **정규식이 안 걸린 것**으로 본다.
 * 0개를 뽑고 「다 통과」로 끝나는 것이 이 시스템에서 가장 비싼 실패다.
 * 2026-09-02 실측이 11(npm 5 + 파일 6)이라 바닥을 11 로 둔다. 이 파일 6개는 이미
 * OUTSIDE 로 걸러진 뒤의 수다 — OUTSIDE 로 실제로 빠지는 것은 `.githooks` 뿐이다.
 * `core.hooksPath` 는 백틱 안에서 항상 `git config --get core.hooksPath` 처럼
 * 공백 있는 여러 낱말로만 나와서, classify() 의 공백 검사에서 애초에 파일 후보가
 * 되지 않는다 — OUTSIDE 에 넣어도 걸러낼 일이 없는 죽은 줄이라 안 넣는다.
 */
const FLOOR = 11;

const tracked = new Set(
  execFileSync('git', ['-C', ROOT, 'ls-files'], { encoding: 'utf-8' })
    .split('\n')
    .filter(Boolean),
);

const all = classify(ticks(src));
const claudeAll = classify(ticks(claudeSrc));
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf-8'));

const targets = [...all.files].filter((f) => !OUTSIDE.has(f));
const total = targets.length + all.npm.size;

if (total < FLOOR) {
  fail(`마법사에서 뽑아낸 것이 ${total}개뿐입니다 (바닥 ${FLOOR}) — 뽑는 규칙이 안 걸린 것으로 봅니다`);
}

for (const f of targets) {
  if (!tracked.has(f)) fail(`마법사가 가리키는 것이 저장소에 없습니다: \`${f}\``);
}
for (const n of all.npm) {
  if (!pkg.scripts || !pkg.scripts[n]) {
    fail(`마법사가 가리키는 명령이 package.json 에 없습니다: \`npm run ${n}\``);
  }
}
for (const [f, why] of OUTSIDE) {
  if (tracked.has(f)) {
    fail(`검사 밖으로 둔 \`${f}\` 가 저장소에 있습니다 ("${why}") — 예외에서 빼고 검사 대상으로 두세요`);
  }
}

/**
 * CLAUDE.md 몫. **바닥을 따로 둔다** — 마법사와 문서 성격이 달라 한 바닥으로 묶으면
 * 한쪽이 통째로 안 뽑혀도 다른 쪽 개수로 가려진다.
 * 2026-09-02 실측이 7(npm 2 + 파일 5)이라 바닥을 7 로 둔다.
 */
const CLAUDE_FLOOR = 7;
const claudeTargets = [...claudeAll.files].filter((f) => !OUTSIDE.has(f));
const claudeTotal = claudeTargets.length + claudeAll.npm.size;

if (claudeTotal < CLAUDE_FLOOR) {
  fail(`CLAUDE.md 에서 뽑아낸 것이 ${claudeTotal}개뿐입니다 (바닥 ${CLAUDE_FLOOR}) — 뽑는 규칙이 안 걸린 것으로 봅니다`);
}
for (const f of claudeTargets) {
  if (!tracked.has(f)) fail(`CLAUDE.md 가 가리키는 것이 저장소에 없습니다: \`${f}\``);
}
for (const n of claudeAll.npm) {
  if (!pkg.scripts || !pkg.scripts[n]) {
    fail(`CLAUDE.md 가 가리키는 명령이 package.json 에 없습니다: \`npm run ${n}\``);
  }
}

/* ── ② 머리말과 0~8단계 표가 같은 것을 가리키나 ──────────────────────────────
 *
 * 머리말은 표를 **줄여 적은 것**이다. 그래서 줄여 적는 것(`.claude/…/join_channels.py`
 * → `join_channels.py`)은 통과시키고, **늘려 적거나 다른 것을 적는 것**만 잡는다.
 * 접미로만 맞추는 이유가 그것이다 — 양방향으로 맞추면 `deploy/setup.sh` 가 표 안의
 * 괄호 속 `setup.sh` 에 걸려 **이빨이 없어진다.**
 */

const INTRO_FROM = '각 단계가 실제로 부르는 것은';
const INTRO_TO = '이고,';

function introChunk() {
  const a = src.indexOf(INTRO_FROM);
  if (a < 0) return null;
  const b = src.indexOf(INTRO_TO, a);
  if (b < 0) return null;
  return src.slice(a, b);
}

function tableCells() {
  const lines = src.split('\n');
  const head = lines.findIndex((l) => l.startsWith('| 단계 ') && l.includes('부르는 것'));
  if (head < 0) return null;
  const cells = [];
  for (let i = head + 1; i < lines.length; i++) {
    const l = lines[i];
    if (!l.startsWith('|')) break;
    if (/^\|[\s:|-]+$/.test(l)) continue;           // |---|---|---| 구분선
    const cols = l.split('|').slice(1, -1);
    if (cols.length < 2) continue;
    cells.push(cols[1]);
  }
  return cells.length ? cells : null;
}

const intro = introChunk();
const cells = tableCells();

if (intro === null) {
  fail('머리말의 「각 단계가 실제로 부르는 것은 …이고,」 문장을 못 찾았습니다 — 문장을 고쳤으면 이 검사도 함께 고치세요');
}
if (cells === null) {
  fail('0~8단계 표(「| 단계 | 부르는 것 |」)를 못 찾았습니다 — 표를 고쳤으면 이 검사도 함께 고치세요');
}

if (intro !== null && cells !== null) {
  const A = classify(ticks(intro));
  const B = classify(ticks(cells.join('\n')));
  const covered = (t, set) => [...set].some((c) => c === t || c.endsWith('/' + t));

  for (const t of A.files) {
    if (!covered(t, B.files)) {
      fail(`머리말이 0~8단계 표에 없는 것을 가리킵니다: \`${t}\` — 표에 있는 것: ${[...B.files].join(' · ')}`);
    }
  }
  for (const n of A.npm) {
    if (!B.npm.has(n)) {
      fail(`머리말이 0~8단계 표에 없는 명령을 가리킵니다: \`npm run ${n}\``);
    }
  }
}

/* ── ③ README.md 「처음 설치」 절의 단계 번호와 마법사 0~N단계 표가 같은 번호를 쓰나 ──
 *
 * 실제로 README 8단계(`check-fixtures.json` 만들기)가 마법사 표에 통째로 없던 적이
 * 있었다(2026-09-02). 마법사대로만 설치한 팀은 그 파일을 안 만들고, 실물 아카이브를
 * 재는 검사 셋이 조용히 「재지 못했습니다」로 건너뛰는데 마법사는 「전부 통과」를 찍는다.
 * ①은 가리키는 파일이 실재하나만 보고, ②는 마법사 안에서 머리말과 표가 맞나만 본다 —
 * 둘 다 README 와의 번호 어긋남은 못 잡는다. 그래서 따로 잰다.
 */
const README = path.join(ROOT, 'README.md');
const readmeSrc = fs.readFileSync(README, 'utf-8');

function readmeStepNumbers() {
  const start = readmeSrc.indexOf('## 처음 설치');
  if (start < 0) return null;
  const end = readmeSrc.indexOf('\n## ', start + 1);
  const section = readmeSrc.slice(start, end < 0 ? readmeSrc.length : end);
  const nums = [...section.matchAll(/^### (\d+)\./gm)].map((m) => Number(m[1]));
  return nums.length ? nums : null;
}

function wizardStepNumbers() {
  const lines = src.split('\n');
  const head = lines.findIndex((l) => l.startsWith('| 단계 ') && l.includes('부르는 것'));
  if (head < 0) return null;
  const nums = [];
  for (let i = head + 1; i < lines.length; i++) {
    const l = lines[i];
    if (!l.startsWith('|')) break;
    if (/^\|[\s:|-]+$/.test(l)) continue;
    const cols = l.split('|').slice(1, -1);
    if (cols.length < 2) continue;
    for (const m of cols[0].matchAll(/\d+/g)) nums.push(Number(m[0]));
  }
  return nums.length ? nums : null;
}

const readmeNums = readmeStepNumbers();
const wizardNums = wizardStepNumbers();

if (readmeNums === null) {
  fail('README.md 의 「처음 설치」 절에서 `### N.` 단계 헤더를 못 찾았습니다 — 절 이름이나 헤더 모양을 고쳤으면 이 검사도 함께 고치세요');
}
if (wizardNums === null) {
  fail('마법사 0~N단계 표에서 단계 번호를 못 뽑았습니다 — 「| 단계 |」 표 머리를 고쳤으면 이 검사도 함께 고치세요');
}
if (readmeNums !== null && wizardNums !== null) {
  const a = new Set(readmeNums);
  const b = new Set(wizardNums);
  const onlyInReadme = [...a].filter((n) => !b.has(n)).sort((x, y) => x - y);
  const onlyInWizard = [...b].filter((n) => !a.has(n)).sort((x, y) => x - y);
  if (onlyInReadme.length) {
    fail(`README 「처음 설치」 단계 번호가 마법사 표에 없습니다: ${onlyInReadme.join(', ')}`);
  }
  if (onlyInWizard.length) {
    fail(`마법사 표의 단계 번호가 README 「처음 설치」 에 없습니다: ${onlyInWizard.join(', ')}`);
  }
}

if (ok) console.log('✓ 설치 마법사·CLAUDE.md — 가리키는 것 실재 · 머리말과 0~N단계 표가 같은 것을 가리킴 · README 단계 번호와 일치');
process.exitCode = ok ? 0 : 1;
