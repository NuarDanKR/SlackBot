#!/usr/bin/env node
/**
 * 색인 지문이 **자료 저장소의 git** 을 보고 있나.
 *
 *     node scripts/check-brief-stamp.js
 *
 * 종료코드: 0 통과 / 1 실패 있음
 *
 * ── 왜 남겨 두는가 ──
 *
 * `claude.js` 의 `briefStamp()` 은 상주 봇의 색인 메모리 캐시를 버릴지 정하는 **유일한 키**다.
 * 지문이 안 바뀌면 자료가 바뀌어도 봇은 옛 색인으로 계속 답한다 — 재시작 전까지.
 *
 * 그 지문의 다섯 자리 중 둘이 git 신호(`refs/heads/main`·`packed-refs`)인데, 이건
 * 「커밋이 들어왔다 = 자료가 바뀌었다」를 잡으려는 것이다. 나머지(두 `index.md` 와
 * `.sync-state.json` 의 수정시각)는 **로컬에서 커밋 전에 손댄 경우를 위한 보조**다. `claude.js` 의 주석이 밝히듯, 예전에
 * `index.md` 만 보던 시절은 *"slack-sync 가 늘 index.md 를 함께 고쳐서 우연히 동작하고
 * 있었을 뿐"* 이었고 그래서 git 신호를 넣었다.
 *
 * **2026-08-31 저장소를 둘로 가르며 그 git 신호가 조용히 죽었다.** 그전에는 코드가 자료와
 * 한 저장소 안에 있어서 `ROOT/../..` 가 곧 그 저장소였는데, 갈린 뒤로 그 자리는 저장소가
 * 아닌 상위 폴더(VM `/opt`, 이 PC `C:\Users\TY`)를 가리킨다. `statSync` 가 없는 파일에
 * 0 을 내주므로 **에러가 안 난다.** 지문은 계속 만들어지고 값도 그럴듯하다 — 두 자리가
 * 영원히 `0-0-` 일 뿐이다. 이력 전체를 대보니 `index.md` 가 늘 함께 바뀌어 아직 오답이
 * 난 적은 없지만, 그건 바로 위에서 「우연」이라고 부른 그 상태다.
 *
 * 그래서 이 검사는 두 가지를 본다. 하나만으로는 못 막는다:
 *
 *   ① 보는 자리가 **자료 저장소 안**인가 — 코드 저장소를 보게 되면 그쪽도 git 이라
 *      ②를 통과해 버린다. 그런데 색인의 원천(`slack-export`·`documents`)은 자료 쪽에만 있다.
 *   ② 자료 저장소가 실제로 git 저장소일 때 **git 신호가 살아 있나** — 경로만 맞고 한 단계
 *      어긋나면(`.git` 을 빠뜨리는 등) 값이 다시 0 이 된다.
 *
 * ②는 자료 저장소가 git 이 아닐 때는 건너뛴다. 그때 0 은 고장이 아니라 사실이고,
 * 지문은 `index.md` 두 자리로 내려앉는다 — 없는 상태를 실패로 세면 이 검사가 곧 무시된다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { briefStampPaths } from '../src/claude.js';
import { DATA_ROOT } from '../src/config.js';

let bad = 0;
const fail = (m) => {
  console.log(`  ✗ ${m}`);
  bad += 1;
};
const ok = (m) => console.log(`  ✓ ${m}`);

const paths = briefStampPaths();
const gitPaths = paths.filter((p) => p && p.split(path.sep).includes('.git'));

/* 지문의 모양 자체가 바뀌면 아래 판정이 헛돈다. 먼저 그것부터 붙잡는다. */
if (paths.length !== 5) {
  fail(`지문이 보는 자리가 5개가 아니라 ${paths.length}개입니다 — 이 검사를 함께 고치세요.`);
}
if (gitPaths.length !== 2) {
  fail(`git 신호가 2개가 아니라 ${gitPaths.length}개입니다 — 이 검사를 함께 고치세요.`);
}

/* ① 보는 자리가 자료 저장소 안인가. */
const outside = gitPaths.filter((p) => !path.resolve(p).startsWith(path.resolve(DATA_ROOT) + path.sep));
if (outside.length) {
  fail('지문의 git 신호가 자료 저장소 밖을 봅니다 — 색인의 원천은 전부 자료 저장소 안입니다.');
  console.log(`      자료 저장소: ${path.resolve(DATA_ROOT)}`);
  for (const p of outside) console.log(`      보는 곳:     ${p}`);
  console.log('      상주 봇이 자료가 바뀌어도 옛 색인을 계속 씁니다 (재시작 전까지). 에러는 안 납니다.');
} else {
  ok('지문의 git 신호가 자료 저장소를 봅니다');
}

/* ② 자료 저장소가 git 이면 신호가 실제로 살아 있나. */
const dataIsRepo = fs.existsSync(path.join(DATA_ROOT, '.git'));
if (!dataIsRepo) {
  /* `[못잼]` 표시를 단다 — 이 갈래는 종료코드 0 이라 `check-setup.js` 가 통과로 적는데,
   * 그 화면에 이 줄이 안 올라가면 「②를 안 쟀다」가 초록 한 줄에 묻힌다. 표시를 안 달고
   * 문구에 기대던 시절에는 「안 댔습니다」가 그쪽 말버릇 목록에 없어 실제로 묻혔다. */
  console.log('[못잼]      · 자료 저장소가 git 이 아니라 git 신호는 안 댔습니다 (지문은 index.md 둘로 돕니다)');
} else {
  const alive = gitPaths.filter((p) => fs.existsSync(p));
  if (!alive.length) {
    fail('자료 저장소는 git 인데 지문의 git 신호가 둘 다 없는 파일을 가리킵니다.');
    for (const p of gitPaths) console.log(`      없음: ${p}`);
    console.log('      statSync 가 0 을 내므로 지문은 만들어지지만 그 두 자리가 영원히 0 입니다.');
  } else {
    ok(`git 신호가 살아 있습니다 (${alive.length}/2 — 갓 클론한 저장소는 packed-refs 쪽만 있습니다)`);
  }
}

if (bad) {
  console.log('\n색인 캐시가 자료 변경을 놓칩니다.');
  process.exit(1);
}
console.log('\n지문이 자료 저장소를 봅니다.');
