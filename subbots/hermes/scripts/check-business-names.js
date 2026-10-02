#!/usr/bin/env node
/**
 * 코드 저장소에 **TEC 사업장·비공개 채널 이름**이 남아 있나.
 *
 *     node scripts/check-business-names.js
 *
 * 종료코드: 0 없음(또는 잴 이름 자체가 없음) / 1 있음·못 잼
 *
 * ── 왜 남겨 두는가 ──
 *
 * 2026-08-31 에 저장소가 코드와 자료로 갈리면서 **코드 저장소를 팀끼리 나눠 쓰기로**
 * 했다. 그 순간 성격이 바뀐 것들이 있다 — 사고 기록 주석·시험 픽스처·문서 예시에
 * 사업장과 비공개 채널 이름이 들어 있고, 그중에는 실제 금액과 거래상대까지 있다
 * (2026-09-01 실측: 이름 44개가 351줄 · 57파일).
 *
 * CLAUDE.md 규칙이 이미 있다 — 「막힌 자료는 **이름조차** 밝히지 않는다」. 그 규칙은
 * 봇 답변용으로 쓰였지만 **저장소를 통째로 남에게 주는 쪽이 더 세다.**
 *
 * **한 번 걷어내는 것보다 다시 들어오는 것을 막는 쪽이 본체다.** 앞으로 주석을 쓸
 * 때마다 같은 일이 반복된다.
 *
 * ── "못 잼" 과 "잴 이름이 없음" 을 가른다 ──
 *
 * names() 는 세 가지를 돌려줄 수 있다.
 *   · null       — config.json 을 못 읽었거나 못 파싱했다, 또는 projects/·channels/
 *                  폴더를 **못 읽었다**(권한 등). 이때는 무엇이 새는지 잴 수가 없으므로
 *                  **실패**다. 안 셈을 통과로 읽지 않는다.
 *   · 빈 목록    — config.json 은 읽었고 privateChannels·skipChannels 가 비어 있고
 *                  projects/·channels/ 도 비어 있(거나 아직 없)다. **막 init-archive 로
 *                  씨앗만 넣은 새 팀의 자료 저장소가 이 모양이다** — 이름이 하나도 없으니
 *                  샐 것도 없다. 이건 통과다.
 *   · [...이름]  — 실제로 대볼 이름이 있다.
 *
 * 폴더를 **못 읽는** 것과 폴더가 **아예 없는** 것은 다르게 본다.
 * ENOENT(폴더 없음)는 「아직 없다」와 같은 뜻으로 취급해 빈 목록에 합류시킨다 —
 * init-archive 로 막 씨앗을 넣은 저장소도, 문서 기능을 아직 안 켠 저장소도 이 모양이다.
 * 그 밖의 오류(권한 거부 등 — 폴더는 있는데 못 열었다)만 "못 읽었다"로 보고 null 을
 * 돌려준다. 이 둘을 가르지 않으면 자료 저장소를 막 만든 팀이 `npm run check` 를
 * 돌릴 때마다 영구히 ✗ 를 보게 된다 — 이 검사가 지키려는 사람이 바로 그 팀이다.
 *
 * ── 재료가 셋이 아니라 넷이고, 출처별 개수를 화면에 찍는다 ──
 *
 * 이름 재료는 `privateChannels` · `skipChannels` · `documents/projects/` ·
 * **`slack-export/channels/*.md`** 넷이다. 넷째를 2026-09-01 에 더했다 — 그전에는
 * **문서 폴더가 없는 채널**(문서가 한 건도 안 올라온 사업장)이 목록에 아예 안 들어
 * 검사가 그 이름을 영원히 못 봤다.
 *
 * 그리고 **출처별 개수를 초록일 때도 찍는다.** 목록이 조용히 줄어드는 것이 이 검사의
 * 고장 방식이기 때문이다 — `documentsPath` 오타 하나로 사업장 몫이 0이 되어도 검사는
 * 그냥 초록을 낸다. 개수가 함께 보이면 그때 눈에 띈다.
 *
 * ── 못 잡는 구간 ──
 *
 *   · 자료 저장소를 못 읽으면 이름 목록을 못 만든다. 그때는 **실패**다 — 안 셈을
 *     통과로 읽지 않는다.
 *   · 이미 끝난 사업장이 `documents/projects/` 와 `channels/` 양쪽에서 사라지면
 *     그 이름은 못 본다.
 *   · 길이 3자 미만인 이름은 안 센다 — 일상 낱말과 겹쳐 헛걸림이 난다. 그래도 겹치는
 *     이름(예: 일상 낱말과 같은 채널명)은 헛걸림이 날 수 있다. 그때는 이름을 바꾸는
 *     것이 아니라 **그 줄에서 이름을 빼는 것**이 답이다.
 *   · `_` 로 시작하는 이름은 **`#` 이 앞에 붙었을 때만** 센다. 통째로 빼면 그런 채널을
 *     `#` 과 함께 적은 자리가 영원히 안 걸리고, 그냥 세면 `_` 로 시작하는 흔한
 *     식별자·파일명에 전부 걸린다. `#` 이 붙은 자리는 슬랙 채널을 가리키는 것이
 *     거의 확실하다.
 *
 * ── 초록을 「다 걷혔다」로 읽지 말 것 ──
 *
 * 이 검사는 **자료 저장소의 채널·사업장 이름과 글자 그대로** 대볼 뿐이다. 아래는
 * 구조적으로 못 본다 — 전수조사(2026-09-01)에서 넷 다 실물로 확인됐다.
 *
 *   · **사람 이름** — 목록에 없으므로 안 걸린다. 실제 담당자 이름과 휴대폰·
 *     이메일이 마스킹 시험 픽스처에 원본으로 남아 있었다.
 *   · **회사·팀 이름** — 이 목록으로는 안 걸린다. 프롬프트 쪽은 `check-bootstrap.js` 의
 *     ⑨ 가 자리표시자로 막았고, **정체성 칸은 아래 「정체성 축」이 본다.** 그 둘 밖
 *     (주석·문서 본문)은 여전히 안 걸린다.
 *   · **금액·문서 제목·연락처·법인등록번호** — 이름이 아니라 안 걸린다.
 *
 * 넷 중 둘은 2026-09-01 에 막았다. 「문서 폴더가 없는 공개 채널」은 재료에
 * `channels/*.md` 를 더해 목록에 넣었고, **「줄여 쓴 이름」은 아래 약칭 축이 본다** —
 * 다만 그쪽은 실패가 아니라 경고이고 헛걸림이 섞인다. 위 둘은 그대로 열려 있다.
 *
 * 그러니 이 검사가 초록이라는 것은 **「대볼 수 있는 것으로는 안 걸린다」**는 뜻이지
 * 「나가도 안전하다」는 뜻이 아니다. 사람이 눈으로 본 것은 그 자리에서 함께 고친다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const read = (p) => { try { return fs.readFileSync(p, 'utf8'); } catch { return null; } };

// 자료 저장소 자리 — check-bootstrap.js 와 같은 순서로 직접 읽는다.
const DATA_ROOT = (() => {
  if (process.env.HERMES_DATA_ROOT) return path.resolve(process.env.HERMES_DATA_ROOT);
  const env = read(path.join(ROOT, '.env')) || '';
  const m = env.match(/^\s*HERMES_DATA_ROOT\s*=\s*(.+?)\s*$/m);
  if (m) return path.resolve(m[1].replace(/^["']|["']$/g, ''));
  return path.resolve(ROOT, '..', 'hermes-archive');
})();

// ENOENT(폴더 자체가 없음) = "아직 없다" → 빈 목록으로 계속 진행.
// 그 밖(권한 거부 등, 폴더는 있는데 못 읽음) = 진짜 "못 읽었다" → 못 잼(null).
function dirEntries(dir) {
  try {
    return fs.readdirSync(dir, { withFileTypes: true });
  } catch (e) {
    return e.code === 'ENOENT' ? [] : null;
  }
}

function names() {
  const raw = read(path.join(DATA_ROOT, 'config.json'));
  if (raw === null) return null;
  let cfg;
  try { cfg = JSON.parse(raw); } catch { return null; }

  const 설정 = new Set();
  for (const n of cfg.privateChannels || []) 설정.add(n);
  for (const n of (cfg.digest && cfg.digest.skipChannels) || []) 설정.add(n);

  const projects = dirEntries(path.join(DATA_ROOT, cfg.documentsPath || 'documents', 'projects'));
  if (projects === null) return null;
  const 사업장 = new Set(projects.filter((d) => d.isDirectory()).map((d) => d.name));

  const chans = dirEntries(path.join(DATA_ROOT, cfg.archivePath || 'slack-export', 'channels'));
  if (chans === null) return null;
  const 채널 = new Set(chans.filter((d) => d.isFile() && d.name.endsWith('.md')).map((d) => d.name.slice(0, -3)));

  const all = new Set([...설정, ...사업장, ...채널]);
  return {
    출처: `설정 ${설정.size} + 사업장 ${사업장.size} + 채널 ${채널.size} = ${all.size}개`,
    // 그대로 대보는 이름. 3자 미만은 일상 낱말과 겹쳐 헛걸림이 난다.
    plain: [...all].filter((n) => !n.startsWith('_') && n.length >= 3),
    // `_` 로 시작하는 이름은 `#` 이 앞에 붙었을 때만 센다 (머리말 「못 잡는 구간」 참조).
    // `#` 이 이미 이름을 슬랙 채널로 못박으므로 여기엔 길이 조건을 걸지 않는다.
    hashOnly: [...all].filter((n) => n.startsWith('_')),
  };
}

const list = names();
if (list === null) {
  console.log('  ✗ 자료 저장소에서 이름 목록을 만들지 못해 **재지 못했습니다**');
  console.log(`      본 곳: ${DATA_ROOT}`);
  console.log('      재지 못한 것은 「없다」가 아닙니다.');
  process.exit(1);
}
/* ── 커밋 메시지 축 (`--메시지 <파일>`) ──────────────────────────────────────
 *
 * **이 저장소는 배포용이다** — 코드·스킬·배포 절차를 다른 팀에 그대로 넘긴다. 그래서
 * 사업장·비공개 채널 이름이 들어가면 안 되고, 아래 파일 훑기가 그것을 막는다.
 * **그런데 커밋 메시지는 아무것도 안 봤다.** 2026-09-03 에 실제로 났다 — 코드에는
 * 이름을 안 넣었는데 커밋 메시지에 채널 이름 한 줄이 들어갔고, push 했다면 팀이 보는
 * 이력에 영구히 남았다. 파일만 재는 검사는 그 자리를 구조적으로 못 본다.
 *
 * 대는 것은 **파일 훑기와 똑같은 목록**이다(`names()` 하나가 원본이다 — 여기 또 적으면
 * 값이 바뀌는 날 갈린다). 다만 **약칭 축은 안 쓴다** — 그쪽은 헛걸림이 섞여 ⚠ 로 내는
 * 축인데, 커밋 메시지는 사람이 쓰는 산문이라 헛걸림이 훨씬 잦고 막으면 못 고칠 빨강이 된다.
 *
 * ── 다른 배포용 저장소에 옮길 때 ──
 *
 * 이 축이 지키는 것은 「이 저장소」가 아니라 **「배포용 저장소」라는 성질**이다. 새 배포용
 * 저장소가 생기면 `.githooks/commit-msg` 와 이 스크립트를 함께 가져가면 된다. 이름의
 * 출처는 그 팀의 자료 저장소(`HERMES_DATA_ROOT`)이므로 **옮긴 곳에서는 그 팀 이름을
 * 막는다** — 목록을 새로 적을 것이 없다.
 * **자료 저장소 자신에는 걸지 마라.** 거기는 사업장 이름이 자료 그 자체다.
 */
/* ── 스테이징 축 (`--스테이징`) ────────────────────────────────────────────
 *
 * **파일 훑기가 커밋 관문에 없었다** (2026-09-06 에 실제로 뚫렸다). 아래 기본 갈래는
 * `npm run check` 에서만 돌고, `.githooks/commit-msg` 는 **메시지만** 본다. 그래서
 * 파일 본문에 이름을 쓴 커밋은 아무것도 안 막았다 — 그날 스킬 문서 본문에 비공개 채널
 * 이름을 적었는데, 잡아 준 것은 이 검사가 아니라 **커밋 메시지 쪽에 같은 이름이 우연히
 * 함께 들어간 것**뿐이었다. 메시지에만 안 썼으면 그대로 나갔다.
 *
 * 대는 것은 **인덱스에 담긴 내용**이지 작업 트리가 아니다. 이 저장소가 세 관문에서
 * 이미 정한 규칙이다(`.githooks/pre-commit` 의 「대는 것은 스테이징이지 작업 트리가
 * 아니다」) — 둘을 같게 두면 `git add -p` 로 조각만 담았을 때 화면에서 본 것과 다른
 * 조합이 통과한다.
 *
 * 목록은 `names()` 하나가 원본이다. 약칭 축은 여기서도 안 쓴다 — 헛걸림이 섞여 있어
 * 막으면 못 고칠 빨강이 된다(메시지 축과 같은 이유).
 */
if (process.argv.includes('--스테이징')) {
  let 목록;
  try {
    목록 = execFileSync('git', ['-C', ROOT, '-c', 'core.quotePath=false', 'diff', '--cached', '--name-only', '--diff-filter=ACMR'], { encoding: 'utf8' })
      .split('\n').filter((f) => f && !f.endsWith('.pyc'));
  } catch (e) {
    // 못 읽은 것은 「볼 것 없음」이 아니다.
    console.error(`  ✗ 스테이징 목록을 읽지 못해 **재지 못했습니다**: ${e.message}`);
    process.exit(1);
  }

  const 걸린것 = [];
  const 못읽음 = [];
  for (const f of 목록) {
    let t;
    try {
      // 작업 트리가 아니라 **인덱스**에서 꺼낸다.
      t = execFileSync('git', ['-C', ROOT, 'show', `:${f}`], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    } catch { 못읽음.push(f); continue; }
    t.split('\n').forEach((l, i) => {
      const found = [
        ...list.plain.filter((n) => l.includes(n)),
        ...list.hashOnly.filter((n) => l.includes('#' + n)),
      ];
      if (found.length) 걸린것.push([f, i + 1, found, l]);
    });
  }

  if (못읽음.length) {
    console.error(`  ✗ 스테이징된 파일 ${못읽음.length}개를 인덱스에서 읽지 못해 **재지 못했습니다**: ${못읽음.slice(0, 5).join(' · ')}`);
    process.exit(1);
  }
  if (!걸린것.length) {
    console.log(`  ✓ 스테이징된 ${목록.length}개 파일에 사업장·비공개 채널 이름이 없습니다 (이름 ${list.plain.length + list.hashOnly.length}개 기준)`);
    process.exit(0);
  }

  console.error('');
  console.error('커밋을 멈춥니다 — 스테이징된 파일 본문에 사업장·비공개 채널 이름이 들어 있습니다.');
  console.error('이 저장소는 다른 팀에 그대로 넘기는 배포용이고, **파일은 이력에 영구히 남습니다.**');
  console.error('');
  for (const [f, n, 이름들, l] of 걸린것.slice(0, 20)) {
    console.error(`  ${f}:${n}  [${이름들.join(' · ')}]`);
    console.error(`    ${l.trim().slice(0, 100)}`);
  }
  if (걸린것.length > 20) console.error(`  … 외 ${걸린것.length - 20}줄`);
  console.error('');
  console.error('  고치는 법 — 이름을 빼고 뜻만 남기세요. 이 저장소가 이미 쓰는 방식입니다:');
  console.error('    ✗ `<사업장>.md` 는 월 헤딩이 하나뿐인데 …');
  console.error('    ✓ 어느 채널 md 는 월 헤딩이 하나뿐인데 …');
  console.error('');
  console.error(`  이름 출처: ${list.출처} · 자료 저장소 ${DATA_ROOT}`);
  process.exit(1);
}

if (process.argv.includes('--메시지')) {
  const 자리 = process.argv[process.argv.indexOf('--메시지') + 1];
  if (!자리) {
    console.error('  ✗ `--메시지` 뒤에 커밋 메시지 파일 경로가 없습니다');
    process.exit(1);
  }
  const 원문 = read(자리);
  if (원문 === null) {
    // 못 읽은 것은 「깨끗하다」가 아니다 — 이 파일이 여러 번 배운 것이다.
    console.error(`  ✗ 커밋 메시지를 읽지 못해 **재지 못했습니다**: ${자리}`);
    process.exit(1);
  }
  /* **`#` 로 시작하는 줄을 주석이라며 버리면 안 된다** (2026-09-03, 별개 저장소에서
   * 진짜 커밋으로 재현). 처음에는 「git 안내 주석은 커밋에 안 들어간다」로 걷어냈는데,
   * `git commit -F`·`-m` 의 정리 방식은 `whitespace` 라 **`#` 줄이 그대로 커밋된다.**
   * `#채널명 을 뺀다` 라고 쓴 메시지가 검사를 통과하고 이력에 남았다.
   *
   * 안 걷어내도 헛걸림이 안 나는 이유가 있다 — 편집기로 쓸 때 git 이 붙이는 안내에는
   * 브랜치 이름과 **스테이징된 파일 경로**가 들어가는데, **이 저장소의 파일 경로에는
   * 사업장 이름이 있을 수 없다**(아래 파일 훑기가 그것을 이미 막는다). 그러니 안내가
   * 걸린다면 그건 헛걸림이 아니라 **경로나 브랜치 이름에 이름이 샜다는 뜻**이고,
   * 그때는 걸리는 쪽이 맞다. */
  const 줄들 = 원문.split('\n').map((l, i) => [i + 1, l]);

  let cfg = null;
  try { cfg = JSON.parse(read(path.join(DATA_ROOT, 'config.json'))); } catch { /* names() 가 이미 걸렀다 */ }
  const org = cfg && typeof cfg.org === 'string' ? cfg.org.trim() : '';
  const ws = cfg && typeof cfg.workspace === 'string' ? cfg.workspace.trim() : '';
  const 팀이름 = [...new Set([org, ...org.split(/\s+/)])].filter((v) => v.length >= 2);
  const 주소 = [...new Set([ws, ws.replace(/\.slack\.com$/i, '')])].filter((v) => v.length >= 4);

  const 걸린것 = [];
  for (const [n, l] of 줄들) {
    for (const 이름 of list.plain) if (든다(l, 이름)) 걸린것.push([n, '사업장·채널', 이름, l]);
    for (const 이름 of list.hashOnly) if (l.includes(`#${이름}`)) 걸린것.push([n, '사업장·채널', `#${이름}`, l]);
    for (const v of 주소) if (든다(l, v)) 걸린것.push([n, '워크스페이스 주소', v, l]);
    for (const v of 팀이름) if (든다(l, v)) 걸린것.push([n, '팀 이름', v, l]);
  }

  if (!걸린것.length) {
    console.log(`  ✓ 커밋 메시지에 사업장·비공개 채널 이름이 없습니다 (이름 ${list.plain.length + list.hashOnly.length}개 기준)`);
    process.exit(0);
  }

  console.error('');
  console.error('커밋을 멈춥니다 — 커밋 메시지에 사업장·비공개 채널 이름이 들어 있습니다.');
  console.error('이 저장소는 다른 팀에 그대로 넘기는 배포용이고, **메시지는 이력에 영구히 남습니다.**');
  console.error('');
  for (const [n, 갈래, 이름, l] of 걸린것) {
    console.error(`  ${n}행  [${갈래}: ${이름}]`);
    console.error(`    ${l.trim().slice(0, 100)}`);
  }
  console.error('');
  // 예시에 진짜 이름을 쓰면 **이 파일 자신이 아래 파일 훑기에 걸린다** (2026-09-03 에
  // 실제로 그렇게 잡혔다). 자리표시자로 적는다.
  console.error('  고치는 법 — 이름을 빼고 뜻만 남기세요. 이 저장소가 이미 쓰는 방식입니다:');
  console.error('    ✗ `<사업장>.md` 는 월 헤딩이 하나뿐인데 …');
  console.error('    ✓ 어느 채널 md 는 월 헤딩이 하나뿐인데 …');
  console.error('  자리·건수로 가리키면 이름 없이도 충분히 짚힙니다 (「채널 2개」·「어느 자금운영보고 md」).');
  console.error('');
  console.error(`  이름 출처: ${list.출처} · 자료 저장소 ${DATA_ROOT}`);
  process.exit(1);
}

const 대볼것 = list.plain.length + list.hashOnly.length;
if (대볼것 === 0) {
  console.log('  ✓ 자료 저장소에 사업장·비공개 채널 이름이 아직 없어 샐 것이 없습니다');
  console.log(`      본 곳: ${DATA_ROOT}`);
  console.log(`[보임] 이름 출처: ${list.출처}`);
  process.exit(0);
}

// `core.quotePath=false` 가 **꼭 있어야 한다.** 기본값에서 git 은 한글처럼 ASCII 가 아닌
// 파일 이름을 `"\353\254\270…"` 같은 8진 이스케이프로, 따옴표까지 붙여서 내놓는다. 그
// 문자열로는 파일이 안 열려서 아래 읽기가 실패하고, 전에는 그것을 그냥 건너뛰었다 —
// 2026-09-01 에 한글 이름 파일 3개(42줄)가 그렇게 **한 번도 세어지지 않은 채** 검사가
// 초록이었다. 그 안에 비공개 채널 이름과 실제 금액·거래상대가 들어 있었다.
const files = execFileSync('git', ['-C', ROOT, '-c', 'core.quotePath=false', 'ls-files'], { encoding: 'utf8' })
  .split('\n').filter((f) => f && !f.endsWith('.pyc'));

let bad = 0;
const rows = [];
// **못 읽은 파일은 「볼 것 없음」이 아니다.** 위 사고가 조용했던 진짜 이유가 이 자리라,
// 세어서 화면에 내고 하나라도 있으면 실패시킨다. 원인이 인코딩이든 권한이든 마찬가지다.
const unread = [];
const texts = [];
for (const f of files) {
  const t = read(path.join(ROOT, f));
  if (t === null) { unread.push(f); continue; }
  const lines = t.split('\n');
  texts.push([f, lines]);
  const hits = [];
  lines.forEach((l, i) => {
    const found = [
      ...list.plain.filter((n) => l.includes(n)),
      ...list.hashOnly.filter((n) => l.includes('#' + n)),
    ];
    if (found.length) hits.push({ no: i + 1, names: found });
  });
  if (hits.length) { bad += hits.length; rows.push({ f, hits }); }
}

/* ── 약칭 축 ── 실패가 아니라 경고(⚠)다.
 *
 * 위 대보기는 **이름 전체**만 본다. 사람은 네 글자 이름을 두 글자로 줄여 쓰므로
 * 그 자리는 원리상 한 줄도 안 걸린다 — 2026-09-01 실측으로 여섯 부류가 실물로 있었다.
 * 그러니 여기서 초록이라는 것은 **그 부류를 봤다는 뜻이 아니라 안 봤다는 뜻**이었다.
 * 축을 만드는 것이 목적이고, **0 으로 만드는 것은 목적이 아니다.**
 *
 * 후보는 이름의 한글 덩어리마다 앞·뒤 2~3자다. 헛걸림이 많이 섞이므로 두 갈래로 뺀다.
 *   ① 이름 3개 이상이 공유하는 후보 — `예시마을_*` 같은 접두어를 여럿이 공유하면
 *      `예시`·`마을` 은 그 팀의 일반 낱말이지 한 사업장을 가리키는 말이 아니다.
 *      (접두어는 지어낸 것이다 — 실제 접두어를 여기 적으면 325행의 자기 규칙에 어긋난다.
 *      2026-09-05 에 실제 접두어가 적혀 있던 것을 재검증이 잡아 바꿨다.)
 *   ② 코드에서 너무 자주 나오는 후보(기본 30줄 초과) — 실측에서 `사업장`(484줄)처럼
 *      우리가 자리표시자로 쓰는 낱말이 여기 걸린다.
 * **뺀 것은 이름을 적어 화면에 낸다.** 조용히 줄이면 이 축도 위 사고와 같은 모양이 된다.
 *
 * 그래도 남는 것에는 헛걸림이 섞인다. 그것까지 없애려면 낱말 사전이 필요한데, 사전을
 * 들이면 이 축은 사전에 없는 이름을 조용히 놓치기 시작한다 — 그 교환은 하지 않는다.
 */
const 약칭상한 = 30;
function 약칭축() {
  const 후보 = new Map(); // 약칭 → 그것을 낳은 이름들
  for (const n of [...list.plain, ...list.hashOnly]) {
    for (const seg of n.match(/[가-힣]{2,}/g) || []) {
      const outs = new Set();
      for (const k of [2, 3]) if (seg.length > k) { outs.add(seg.slice(0, k)); outs.add(seg.slice(-k)); }
      if (seg.length <= 3) outs.add(seg);
      for (const c of outs) {
        if (!후보.has(c)) 후보.set(c, new Set());
        후보.get(c).add(n);
      }
    }
  }
  const 공유 = [...후보].filter(([, ns]) => ns.size >= 3).map(([c]) => c);
  const 볼것 = [...후보.keys()].filter((c) => !공유.includes(c));

  const 걸림 = new Map(); // 약칭 → [{f, no, line}]
  for (const [f, lines] of texts) {
    lines.forEach((l, i) => {
      for (const c of 볼것) if (l.includes(c)) {
        if (!걸림.has(c)) 걸림.set(c, []);
        걸림.get(c).push({ f, no: i + 1, line: l.trim() });
      }
    });
  }
  const 흔함 = [...걸림].filter(([, hs]) => hs.length > 약칭상한).map(([c]) => c);
  for (const c of 흔함) 걸림.delete(c);

  /* ── 손으로 뺀 것 (2026-09-03) ──
   *
   * 355줄을 전부 눈으로 대본 결과, 아래 낱말은 **한글 일상 낱말·업계 통용어**와 겹쳐서
   * 걸릴 뿐 실제 사업장·채널 이름이 아니었다 (예: `로드`="파일을 로드한다"·`문단`=워드
   * 문단·`관련`="~관련"·`재무`="재무 자료"·`기타`="(기타)" 집계 버킷·`보증`="지급보증"
   * 일반 재무 용어·`협의회`/`의회`="채권단협의회" 익명화 픽스처). 다 대봤고, **뺀 낱말이
   * 잡던 줄 중 진짜 이름이 섞인 것은 없었다** — 겹쳤던 실제 이름은 다른 약칭이 여전히
   * 잡는다(예: 채널 이름 둘은 그 이름의 다른 조각이, 사업장 약칭 둘은 그 이름의 다른
   * 조각이 각각 대신 잡는다 — 진짜 이름은 이 주석에도 안 쓴다, 이 파일도 훑기 대상이다).
   * 그래서 이 낱말들만 뺀다 — 사전을 들이는 게 아니라 **실측으로 확인된 순수 헛걸림만**
   * 손으로 더한다. `구로`는 27줄 중 26줄이 "도구로"(tool-wise) 헛걸림이지만 1줄이
   * 진짜(사업장 약칭이 실제로 쓰인 예문)라 **안 뺐다** — 짧고 흔한 조각이라도 하나라도
   * 진짜가 섞이면 그 조각은 남긴다.
   *
   * 다시 검증하려면: node scripts/check-business-names.js --약칭 로 손으로 다시 대본다.
   */
  const 손으로뺌 = [
    '로드', '문단', '관련', '기타', '동산', '전사', '재무', '서비스', '서비', '비스',
    '클로', '협의회', '의회', '지표', '그램', '리더', '경기', '개발', '금융', '보증', '간보고',
  ];
  const 수동 = 손으로뺌.filter((c) => 걸림.has(c));
  for (const c of 수동) 걸림.delete(c);

  const 줄수 = [...걸림.values()].reduce((a, hs) => a + hs.length, 0);
  const 뺀것 = [...공유, ...흔함, ...수동];
  if (!걸림.size) {
    console.log(`[보임] ⚠ 약칭 축: 후보 ${볼것.length}개 중 걸린 것 없음`
      + `${뺀것.length ? ` (일반 낱말로 뺌 ${뺀것.length}개: ${뺀것.join(' ')})` : ''}`);
    return;
  }
  /* 순서가 중요하다. **파일 수 많은 것부터 내면 헛걸림이 위를 다 차지한다** — 실측에서
   * 위 여덟 자리가 `로드`·`구로`·`금융`·`문단` 같은 흔한 조각이었고, 진짜인 사업장 약칭 셋은
   * 파일 수가 적어 그 아래로 밀렸다. 진짜 약칭 유출은 대개
   * **길고(3자) 드물다.** 그래서 긴 것 먼저, 같으면 파일 수 적은 것 먼저 낸다. */
  const 파일수 = (hs) => new Set(hs.map((h) => h.f)).size;
  const 순 = [...걸림].sort((a, b) => b[0].length - a[0].length || 파일수(a[1]) - 파일수(b[1]));
  const 앞 = 순.slice(0, 8).map(([c, hs]) => `${c}(${파일수(hs)}파일)`);
  console.log(`[보임] ⚠ 약칭 축: 후보 ${볼것.length}개 중 ${걸림.size}개가 코드에 있습니다 · 줄 ${줄수}개`
    + ` — 헛걸림이 섞입니다. 0 으로 만드는 것이 목표가 아닙니다`);
  console.log(`[보임]    길고 드문 것부터(진짜일 확률 순): ${앞.join(' ')}${순.length > 8 ? ` … 그 외 ${순.length - 8}개` : ''}`
    + ` · 전부 보기: node scripts/check-business-names.js --약칭`);
  if (뺀것.length) console.log(`[보임]    일반 낱말로 뺌 ${뺀것.length}개: ${뺀것.join(' ')}`);

  if (!process.argv.includes('--약칭')) return;
  for (const [c, hs] of 순) {
    console.log(`\n   ── ${c}  ${hs.length}줄 / 파일 ${파일수(hs)}개`
      + `  ← ${[...후보.get(c)].join(' · ')}`);
    for (const h of hs) console.log(`      ${h.f}:${h.no}  ${h.line.slice(0, 100)}`);
  }
}

/* ── 정체성 축 ── 「이 저장소는 누구 것인가」로 읽히는 자리. 경고가 아니라 실패다.
 *
 * 위 대보기는 채널·사업장 이름만 본다. **팀 이름과 슬랙 워크스페이스 주소는 그 목록에
 * 아예 없어서** 2026-09-02 까지 계속 초록이었고, 그날 실물로 여섯 자리를 찾았다 —
 * 매니페스트 설명(붙여넣으면 **그 팀 슬랙 앱 설명에 우리 팀 이름이 뜬다**) · 설치
 * 1단계의 워크스페이스 주소(새 팀엔 없는 곳이라 **첫 단계부터 막힌다**) · README 한 줄
 * 소개 · package.json · systemd Description.
 *
 * **두 갈래로 나눈 이유가 있다.** 팀 이름은 「원저자 저장소는 이것이다」라고 알려주는
 * 자리에서는 **맞는 말**이다 (`deploy/setup.sh` 의 기본값 경고문 · `deploy/README.md`
 * 의 바꿀 값 표 · `README.md` 의 VM 값 안내). 그 자리까지 실패로 내면 고칠 수 없는
 * 빨강이 되고, 사람은 이 검사를 무시하는 법을 배운다. 그래서:
 *
 *   ① 워크스페이스 주소 — 코드 저장소 **어디에도** 있으면 안 된다. 고유한 문자열이라
 *      헛걸림이 없고, 「원저자 것이다」라고 알려줄 이유도 없는 값이다.
 *   ② 팀 이름 — **정체성 칸에서만** 본다. 그 칸은 새 팀이 그대로 쓰게 되는 값이라
 *      거기 우리 이름이 있으면 예외 없이 틀린 것이다. 칸 밖은 안 본다.
 *
 * **칸을 못 찾으면 실패다.** 파일 모양이 바뀌어 뽑기가 헛돌면 이 축은 조용히 초록이
 * 되는데, 그것이 이 검사의 고장 방식이다 (위 「출처별 개수」 주석과 같은 이유).
 */
const 정체성칸 = [
  ['package.json', 'description',
    (t) => (t.match(/"description"\s*:\s*"((?:[^"\\]|\\.)*)"/) || [])[1]],
  ['slack-app-manifest.yaml', 'display_information.description',
    // `^\s{2}description:` 은 `  long_description:` 에 안 걸린다 (두 칸 뒤가 'l' 이다).
    (t) => (t.match(/^\s{2}description:\s*(.+)$/m) || [])[1]],
  ['slack-app-manifest.yaml', 'long_description',
    (t) => {
      const after = t.split(/^\s{2}long_description:\s*\|\s*$/m)[1];
      if (after === undefined) return undefined;
      const body = [];
      for (const l of after.split('\n')) {
        // 첫 조각은 `|` 와 본문 사이의 빈 것이다. 여기서 끊으면 본문을 한 줄도 못 본다.
        if (!l.trim()) { if (body.length) break; continue; }  // 본문 뒤 빈 줄 = 블록 끝
        if (!/^\s{4}/.test(l)) break;                          // 들여쓰기가 빠짐 = 블록 끝
        body.push(l.trim());
      }
      return body.join(' ');
    }],
  ['deploy/hermes.service', 'Description=',
    (t) => (t.match(/^Description=(.*)$/m) || [])[1]],
  // README 의 한 줄 소개 = 파일에서 **처음 나오는** 인용 줄.
  ['README.md', '한 줄 소개',
    (t) => (t.match(/^>\s*(.+)$/m) || [])[1]],
];

/**
 * ASCII 낱말은 **낱말 경계로** 대본다 — `TEC` 를 그냥 포함으로 보면 `architecture`
 * 같은 영어 낱말에 걸려 고칠 수 없는 빨강이 난다. 한글은 조사가 붙어 경계가 없으므로
 * 포함으로 본다. 경계에서 `-` 를 낱말 쪽으로 치는 것은 저장소 주소(`…-archive-tec`)가
 * 팀 이름으로 걸리지 않게 하기 위함이다 — 그 자리는 ②의 대상이 아니다.
 */
function 든다(하이스택, 낱말) {
  const s = String(하이스택);
  if (!/^[\x20-\x7E]+$/.test(낱말)) return s.toLowerCase().includes(낱말.toLowerCase());
  const esc = 낱말.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(^|[^\\w-])${esc}($|[^\\w-])`, 'i').test(s);
}

function 정체성축() {
  let cfg = null;
  try { cfg = JSON.parse(read(path.join(DATA_ROOT, 'config.json'))); } catch { /* names() 가 이미 걸렀다 */ }
  const org = cfg && typeof cfg.org === 'string' ? cfg.org.trim() : '';
  const ws = cfg && typeof cfg.workspace === 'string' ? cfg.workspace.trim() : '';
  // 팀 이름은 통째로도, 공백으로 쪼갠 낱말로도 대본다 — 문서는 대개 쪼갠 쪽을 쓴다.
  const 팀이름 = [...new Set([org, ...org.split(/\s+/)])].filter((v) => v.length >= 2);
  const 주소 = [...new Set([ws, ws.replace(/\.slack\.com$/i, '')])].filter((v) => v.length >= 4);

  // 새 팀은 아직 안 채운 것이 정상이다. 여기서 막으면 이 축이 지키려는 팀이 막힌다.
  if (!팀이름.length && !주소.length) {
    console.log('[보임] 정체성 축: 자료 저장소에 org·workspace 가 없어 **재지 못했습니다**');
    return [];
  }

  const 문제 = [];

  // ① 워크스페이스 주소 — 코드 저장소 어디에도 있으면 안 된다.
  for (const [f, lines] of texts) {
    lines.forEach((l, i) => {
      const hit = 주소.filter((v) => 든다(l, v));
      if (hit.length) 문제.push(`${f}:${i + 1} — 워크스페이스 주소 (${hit[0]})`);
    });
  }

  // ② 팀 이름 — 정체성 칸에서만.
  for (const [f, 칸, 뽑기] of 정체성칸) {
    const t = read(path.join(ROOT, f));
    if (t === null) { 문제.push(`${f} — 못 읽었습니다 (정체성 칸 「${칸}」)`); continue; }
    const v = 뽑기(t);
    if (v === undefined || v === null || !String(v).trim()) {
      문제.push(`${f} — 정체성 칸 「${칸}」 을 못 찾았습니다 (파일 모양이 바뀌었나요)`);
      continue;
    }
    const hit = 팀이름.filter((n) => 든다(v, n));
    if (hit.length) 문제.push(`${f} 의 「${칸}」 에 팀 이름이 있습니다 (${hit.join(' · ')})`);
  }

  if (문제.length) {
    console.log(`  ✗ 정체성 축 — 새 팀이 그대로 쓰게 되는 자리에 우리 것이 ${문제.length}곳`);
    for (const m of 문제.slice(0, 10)) console.log(`      ${m}`);
    if (문제.length > 10) console.log(`      … 그 외 ${문제.length - 10}곳`);
    console.log('      워크스페이스 주소는 「자기 팀 워크스페이스」로, 팀 이름은 팀 중립 문구로 바꾸세요.');
    console.log('      「원저자 저장소는 이것이다」라고 알려주는 자리는 이 축의 대상이 아닙니다.');
  } else {
    console.log(`[보임] 정체성 축: 주소 ${주소.length}개 · 팀 이름 ${팀이름.length}개로`
      + ` 정체성 칸 ${정체성칸.length}개와 파일 ${texts.length}개를 대봄`);
  }
  return 문제;
}

if (unread.length) {
  console.log(`  ✗ 파일 ${unread.length}개를 못 읽어 **재지 못했습니다** (훑은 파일 ${files.length}개)`);
  for (const f of unread.slice(0, 5)) console.log(`      ${f}`);
  if (unread.length > 5) console.log(`      … 그 외 ${unread.length - 5}개`);
  console.log('      재지 못한 것은 「없다」가 아닙니다.');
  process.exit(1);
}

if (bad) {
  console.log(`  ✗ 사업장·비공개 채널 이름이 든 줄 ${bad}개 · 파일 ${rows.length}개`
    + ` (이름 ${대볼것}개 기준 · 파일 ${files.length}개 훑음)`);
  console.log(`      이름 출처: ${list.출처}`);
  for (const r of rows.slice(0, 10)) {
    console.log(`      ${r.f}  ${r.hits.length}줄  (예: ${r.hits[0].no}행 — ${r.hits[0].names.join(' · ')})`);
  }
  if (rows.length > 10) console.log(`      … 그 외 ${rows.length - 10}개 파일`);
  console.log('\n코드 저장소는 팀끼리 나눠 씁니다. 이름을 일반 이름으로 바꾸거나,');
  console.log('뜻이 이름에 걸린 줄은 자료 저장소로 옮기고 여기서는 그 파일을 가리키세요.');
  console.log('실행에 닿는 프롬프트 예시는 config.json 의 promptExamples 로 뺍니다.');
} else {
  console.log(`  ✓ 코드 저장소에 사업장·비공개 채널 이름이 없습니다`
    + ` (이름 ${대볼것}개로 파일 ${files.length}개를 대봄 · 못 읽음 0개)`);
  // `[보임]` = 통과여도 `npm run check` 화면에 올려 달라는 표시 (check-setup.js 가 읽는다).
  // 이 숫자가 조용히 줄어드는 것이 이 검사의 고장 방식이라 초록일 때도 보여야 한다.
  console.log(`[보임] 이름 출처: ${list.출처}`
    + `${list.hashOnly.length ? ` · 그중 '#' 이 붙어야 세는 이름 ${list.hashOnly.length}개` : ''}`);
}
// 축이 둘이다. **한쪽이 걸렸다고 다른 쪽을 건너뛰지 않는다** — 건너뛰면 한 번에
// 하나씩만 보이고, 고치고 다시 돌릴 때마다 새 빨강이 나온다.
const 정체성문제 = 정체성축();
약칭축();
process.exit(bad || 정체성문제.length ? 1 : 0);
