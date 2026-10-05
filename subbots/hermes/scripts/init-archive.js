#!/usr/bin/env node
/**
 * 빈 자료 저장소에 씨앗을 넣는다.
 *
 *     npm run init-archive -- <자료저장소경로>
 *     node scripts/init-archive.js <자료저장소경로>
 *
 * 종료코드: 0 성공(만들었거나 이미 있음) / 1 실패
 *
 * ── 왜 필요한가 ──
 *
 * 코드 저장소는 팀끼리 나눠 쓰고 자료 저장소는 팀마다 따로다. 새 팀의 자료 저장소는
 * **빈 채로 시작하는데 채우는 길이 없었다** (2026-09-01 실측):
 *   · `.gitignore` 가 없어 첫 `git add` 에 원본 pdf·hwp 와 `.env` 가 들어간다
 *   · `slack-export/index.md` 가 없어 `assertArchive()` 가 막는다
 *   · `git clone <빈 저장소>` 는 **종료코드 0** 이라 setup.sh 가 성공으로 읽는다
 *
 * ── 복사만 한다 ──
 *
 * `git init`·`git add`·커밋은 하지 않는다. 무엇이 커밋되는지는 사람이 봐야 한다.
 * 이미 있는 파일은 **덮지 않는다** — 팀이 자기 값으로 고쳐 둔 것을 되돌리면 안 된다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// --- TYBot 연동 모드 관문 -----------------------------------------------------
// CLI 는 사람이 직접 치는 자리다. 깊은 곳에서 던지면 스택만 보이고 무엇을 해야
// 하는지 안 보이므로, **여기서 먼저** 사람 말로 멈춘다.
import { isTybotMode, ArchiveWriteBlocked } from '../src/mode.js';
if (isTybotMode()) {
  const blocked = new ArchiveWriteBlocked('npm run init-archive');
  console.error(blocked.message);
  process.exit(2);
}

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const TEMPLATE = path.join(ROOT, 'archive-template');

/* [씨앗 파일 이름, 자료 저장소에 놓일 이름].
 *
 * `.gitignore` 만 이름이 다르다 — 씨앗을 `.gitignore` 라는 이름으로 두면 **그 파일이
 * 코드 저장소 안에서도 진짜로 작동한다.** git 은 어느 폴더의 `.gitignore` 든 그 아래
 * 전체에 적용하므로, 씨앗 안의 `documents/**` 규칙이 `archive-template/documents/` 를
 * 무시하게 된다. 지금은 거기 든 것이 다 `.md` 라 아무 일도 안 나지만, 앞으로 씨앗에
 * `.md` 아닌 파일을 하나 더하면 `git add` 가 **조용히 건너뛴다** (2026-09-01 실측). */
const FILES = [
  ['gitignore.tpl', '.gitignore'],
  ['slack-export/index.md', 'slack-export/index.md'],
  ['documents/index.md', 'documents/index.md'],
];

const dest = process.argv[2];
if (!dest) {
  console.error('자료 저장소 경로를 주세요:  npm run init-archive -- <경로>');
  console.error('그 경로는 자료 저장소 뿌리입니다 — 코드 저장소가 아닙니다.');
  process.exit(1);
}
const root = path.resolve(dest);

if (fs.existsSync(root) && !fs.statSync(root).isDirectory()) {
  console.error(`폴더가 아닙니다: ${root}`);
  process.exit(1);
}
if (!fs.existsSync(path.join(TEMPLATE, FILES[0][0]))) {
  console.error(`씨앗을 찾지 못했습니다: ${TEMPLATE}`);
  console.error('코드 저장소 뿌리에서 돌리세요.');
  process.exit(1);
}
/* 코드 저장소 자신을 대상으로 삼는 것을 막는다.
 *
 * README 가 「**코드 저장소 폴더에서** 씨앗을 넣습니다」라고 안내하므로, 그 폴더에서
 * `npm run init-archive -- .` 을 치는 것이 가장 흔한 오타다. 전에는 그대로 통과해서
 * 코드 저장소 안에 아카이브 껍데기를 만들었고, 코드 저장소의 `.gitignore` 는 그것을
 * 안 막으므로 뒤이은 `git add .` 에 **팀끼리 나눠 쓰는 저장소로** 섞여 들어갔다.
 * 경고 문구는 있었지만 인자를 아예 안 준 갈래에만 있었다 — 말로만 막고 있었다. */
if (root === ROOT) {
  console.error('여기는 코드 저장소입니다. 자료 저장소를 clone 한 폴더를 주세요.');
  console.error(`  코드 저장소: ${ROOT}`);
  process.exit(1);
}

console.log(`자료 저장소: ${root}\n`);
let made = 0;
let kept = 0;
for (const [src, rel] of FILES) {
  const to = path.join(root, rel);
  if (fs.existsSync(to)) {
    console.log(`  있음  ${rel}  (그대로 둡니다)`);
    kept += 1;
    continue;
  }
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.copyFileSync(path.join(TEMPLATE, src), to);
  console.log(`  만듦  ${rel}`);
  made += 1;
}

// 문서 md 가 들어갈 자리. 비어 있어도 폴더가 있어야 doc-archive 가 첫 문서를 넣는다.
const projects = path.join(root, 'documents', 'projects');
if (!fs.existsSync(projects)) {
  fs.mkdirSync(projects, { recursive: true });
  console.log('  만듦  documents/projects/');
  made += 1;
}
// 채널 md 가 들어갈 자리.
const channels = path.join(root, 'slack-export', 'channels');
if (!fs.existsSync(channels)) {
  fs.mkdirSync(channels, { recursive: true });
  console.log('  만듦  slack-export/channels/');
  made += 1;
}

const hasConfig = fs.existsSync(path.join(root, 'config.json'));
console.log(`\n만든 것 ${made}개 · 그대로 둔 것 ${kept}개`);
console.log('\n다음에 할 일');
if (!hasConfig) {
  console.log(`  1. 설정을 복사하고 자기 값으로 채웁니다`);
  // 돌고 있는 기계의 명령으로 적는다. 전에는 늘 `cp` 였는데 이 명령은 노트북에서
  // 돌고, 윈도우에서 그대로 치면 명령을 못 찾는다. README 도 같은 자리를 `copy` 로
  // 적고 있어서 화면과 문서가 갈려 있었다.
  console.log(`     ${process.platform === 'win32' ? 'copy' : 'cp'} config.example.json "${path.join(root, 'config.json')}"`);
  console.log(`  2. 코드 저장소의 .env 에 HERMES_DATA_ROOT=${root} 를 적습니다`);
  console.log('  3. npm run check');
  console.log('  4. 자료 저장소에서 git add . && git commit && git push  (승인 절차대로)');
} else {
  console.log('  config.json 이 이미 있습니다. npm run check 로 확인하세요.');
}
console.log('\n대화·문서는 아직 비어 있습니다. slack-sync · doc-archive 로 채웁니다.');
