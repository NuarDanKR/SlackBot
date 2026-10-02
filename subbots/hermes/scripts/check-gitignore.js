#!/usr/bin/env node
/**
 * `.gitignore` 가 두 저장소의 **지금 뿌리** 기준으로 맞나.
 *
 *     node scripts/check-gitignore.js [코드저장소] [자료저장소]
 *
 * 종료코드: 0 통과 / 1 실패 있음 · 못 잼
 *
 * ── 왜 필요한가 ──
 *
 * 2026-08-31 에 저장소를 코드와 자료로 가를 때 `.gitignore` 를 절반만 손봤다.
 * 코드 저장소로 간 규칙(`20-operations/21-hermes/logs/` 등)은 지웠는데,
 * **자료 저장소의 새 뿌리에 맞게 `50-resources/` 접두사를 떼는 일은 안 했다.**
 * 그 결과 네 규칙이 아무것도 안 가리켰고, 그중 하나가 문서 화이트리스트였다 —
 * `documents/` 아래 pdf·hwp 가 **무시되지 않는 상태**로 하루를 보냈다.
 *
 * **에러가 안 난다.** git 은 아무것도 안 맞는 규칙을 조용히 넘긴다. 드러나는 자리는
 * 「어느 날 커밋에 바이너리가 섞여 들어간 뒤」이고, 한 번 들어간 바이너리는 히스토리에서
 * 지우기 어렵다. 그래서 사람 눈 대신 이 검사가 본다.
 *
 * **막는 것과 통과시키는 것을 둘 다 시험한다.** 전부 무시하는 `.gitignore` 도 「막는」
 * 시험만 보면 전부 초록인데, 그러면 커밋할 것이 하나도 안 남는다.
 *
 * ── 왜 셸이 아니라 node 인가 (2026-08-31) ──
 *
 * 원래 `.githooks/test-gitignore.sh` 였는데 **아무 데서도 부르지 않았다.** 파일 안
 * 주석 한 줄이 부르는 법의 전부였고, 그러면 손보는 사람의 기억력에 맡긴 것이다 —
 * 이 저장소가 검사 스물여섯 개를 `npm run check` 에 묶은 것과 같은 이유로 옮겼다.
 * 셸인 채로는 그 목록에 못 붙었다. 목록은 `process.execPath` 로 node 만 돌리고,
 * **WHK 의 PowerShell 에는 `sh` 도 `bash` 도 PATH 에 없다**(2026-08-31 실측 —
 * `sh.exe` 는 `C:\Program Files\Git\bin\` 에 있지만 PATH 밖이다). 셸을 찾아 부르게
 * 고칠 수도 있었지만 그 찾기가 실패하면 **「안 셈」으로 조용히 빠지는 자리**가 하나
 * 느는 쪽이라, 판정을 옮겨 셸을 아예 안 쓰기로 했다. **옮기면서 `.sh` 는 지웠다** —
 * 같은 판정을 두 벌로 두면 갈리고, 갈린 판정이 이 저장소가 가장 여러 번 다친 자리다.
 *
 * ── 옮기면서 고친 것 ──
 *
 * 셸판의 `ignored()` 는 `git check-ignore` 의 **종료코드 1(무시 안 됨)과 128(못 잼)을
 * 구별하지 않았다.** 그래서 저장소가 없거나 git 이 화를 내면 「무시 안 됨」으로 읽혔고,
 * 그것이 `want_kept`(커밋되어야 하는 것) 쪽에서는 **OK 로 셈**됐다. 저장소를 통째로
 * 못 읽는 날 화면이 절반 초록이 되는 셈이다. 여기서는 128 을 따로 받아 실패로 낸다.
 *
 * 뿌리도 셸판은 `$HOME/hermes/archive` 로 박혀 있었다. 여기서는 봇과 같은 자리
 * (`src/config.js` 의 `DATA_ROOT` — 환경변수 → `.env` 순)를 쓴다. 자료 저장소를
 * 옮기면 이 검사도 함께 따라간다. 인자로 주면 그것이 이긴다.
 *
 * ── 못 잡는 것 ──
 *
 * `--no-index` 로 판정하므로 **실물 파일을 안 만든다.** 그래서 보는 것은 규칙이지
 * 「지금 작업 트리에 무엇이 있나」가 아니다. 이미 추적 중인 파일은 `.gitignore` 와
 * 무관하게 계속 커밋되는데, 그건 여기서 안 보인다 — `git ls-files` 쪽 일이다.
 */
import { spawnSync } from 'node:child_process';
import { ROOT, DATA_ROOT } from '../src/config.js';

const CODE = process.argv[2] ? process.argv[2] : ROOT;
const DATA = process.argv[3] ? process.argv[3] : DATA_ROOT;

let bad = 0;
const fail = (m) => {
  console.log(`  ✗ ${m}`);
  bad += 1;
};
const ok = (m) => console.log(`  ✓ ${m}`);

/**
 * 무시되나. true / false / null(못 잼).
 * 실물이 없어도 판정할 수 있게 `--no-index` 를 쓴다 — 시험용 파일을 만들지 않는다.
 */
function ignored(repo, p) {
  const r = spawnSync('git', ['-C', repo, 'check-ignore', '-q', '--no-index', '--', p], {
    encoding: 'utf8',
  });
  if (r.error) return [null, r.error.message];
  if (r.status === 0) return [true, null];
  if (r.status === 1) return [false, null];
  // 128 등. `git` 이 저장소를 못 읽었거나 인자를 못 알아들은 것이다.
  return [null, `${(r.stderr || '').trim() || `종료코드 ${r.status}`}`];
}

/** 이 자리가 정말 git 저장소인가. 아니면 아래 판정이 전부 「못 잼」이 된다. */
function isRepo(repo) {
  const r = spawnSync('git', ['-C', repo, 'rev-parse', '--git-dir'], { encoding: 'utf8' });
  return !r.error && r.status === 0;
}

/* 판정표. [경로, 이름, 무시되어야 하나] */
const DATA_CASES = [
  ['slack-export/.archive-run-lock.json', 'archive-run 잠금', true],
  ['slack-export/.decision-stamp.json', '07:00 결정 기록', true],
  ['slack-export/조각.tmp', '대화 원자적쓰기 조각', true],
  ['slack-export/channels/조각.tmp', '채널 원자적쓰기 조각', true],
  ['documents/projects/사업장나/x.pdf', '문서 pdf (화이트리스트)', true],
  ['documents/projects/사업장나/x.hwp', '문서 hwp (화이트리스트)', true],
  ['documents/projects/사업장나/x.xlsx', '문서 xlsx (화이트리스트)', true],
  ['.env', '토큰 파일', true],
  ['node_modules/x', 'node_modules', true],
  ['documents/projects/사업장나/20260101-x.md', '문서 md', false],
  ['documents/.doc-state.json', '문서 상태 파일', false],
  ['documents/index.md', '문서 색인', false],
  ['slack-export/channels/사업장나.md', '채널 md', false],
  ['slack-export/index.md', '대화 색인', false],
  ['hermes-log/2026-08.md', '대화 로그 md (여기 것은 커밋한다)', false],
  ['config.json', '설정', false],
];

const CODE_CASES = [
  ['.env', '토큰 파일', true],
  ['logs/qa-2026-08.jsonl', '봇 원본 로그 (대화 내용 섞임)', true],
  ['.ingest-tmp/x', '자동 반영 임시', true],
  ['node_modules/x', 'node_modules', true],
  ['.claude/skills/doc-archive/scripts/__pycache__/x.pyc', '파이썬 캐시', true],
  ['src/index.js', '봇 소스', false],
  ['.env.example', '토큰 예시', false],
  ['.claude/skills/_shared/paths.py', '공용 경로 모듈', false],
  ['deploy/setup.sh', '배포 스크립트', false],
];

function run(label, repo, cases) {
  console.log(`\n── ${label} (${repo}) ──`);
  if (!isRepo(repo)) {
    fail(`${label}를 git 저장소로 읽지 못했습니다 — ${cases.length}건을 **안 셌습니다**`);
    return;
  }
  for (const [p, name, wantIgnored] of cases) {
    const [got, why] = ignored(repo, p);
    if (got === null) {
      fail(`${name} — 못 쟀습니다 (${p}) — ${why}`);
    } else if (got === wantIgnored) {
      ok(`${name} — ${wantIgnored ? '무시됨' : '커밋 가능'} (${p})`);
    } else if (wantIgnored) {
      fail(`${name} — **무시 안 됨** (${p})`);
    } else {
      fail(`${name} — **무시됨(커밋 안 됨)** (${p})`);
    }
  }
}

run('자료 저장소', DATA, DATA_CASES);
run('코드 저장소', CODE, CODE_CASES);

if (bad) {
  console.log(`\n.gitignore 가 어긋났습니다 — ${bad}건.`);
  console.log('무시되어야 할 것이 안 무시되면 **커밋에 바이너리가 섞여 들어가고**,');
  console.log('커밋되어야 할 것이 무시되면 **자료가 조용히 안 올라갑니다.** 둘 다 에러가 안 납니다.');
  process.exit(1);
}
console.log(`\n.gitignore 가 두 저장소의 지금 뿌리와 맞습니다 (${DATA_CASES.length + CODE_CASES.length}건).`);
