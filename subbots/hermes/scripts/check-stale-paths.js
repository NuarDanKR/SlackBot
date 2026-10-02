#!/usr/bin/env node
/**
 * 사람이 읽고 그대로 치는 것들이 **이사 전 자리를 가리키고 있나.**
 *
 *     node scripts/check-stale-paths.js
 *
 * 종료코드: 0 통과 / 1 실패 있음
 *
 * ── 왜 남겨 두는가 ──
 *
 * 2026-08-31 에 저장소가 코드와 자료로 갈렸다. 스크립트는 `_shared/paths.py` 한 자리로
 * 옮겼지만 **문서는 안 옮겨져** 이사 전 워크스페이스 경로가 그대로 남았다
 * (실측: SKILL.md 에만 56줄). 이것들은
 * 장식이 아니라 **그대로 실행되거나 그대로 설정에 붙는 값**이라, 낡으면 이렇게 나타난다:
 *
 *   · SKILL.md — `git ls-files -- 50-resources/documents` 가 **빈 출력에 종료코드 0**.
 *     없는 경로를 대도 git 은 화내지 않아서, doc-archive 11단계의 「원본 바이너리가
 *     새어 들어갔나」 검사가 **아무 파일도 안 본 채 깨끗하다고 보고**했다.
 *   · README.md — `"log": { "path": "../../50-resources/hermes-log" }` 를 그대로 넣으면
 *     `path.resolve(DATA_ROOT, …)` 가 **저장소 두 단계 위**를 가리킨다. 로그가 git 밖에
 *     쌓이는데 에러가 안 난다 (2026-08-31 실측: `C:\\Users\\TY\\50-resources\\hermes-log`).
 *   · scripts/*.ps1 — 파일 위치에서 **깊이를 세어** 뿌리를 잡던 자리. 이사로 깊이가
 *     달라져 `.claude/skills/…` 를 못 찾고 `SKIP` 한 줄만 남기고 끝났다.
 *     (그 파일은 2026-09-02 에 걷어냈다 — 아래 「지금 .ps1 은 0개다」 참조.)
 *
 * 셋 다 **시끄럽게 죽지 않는다.** 그래서 문자열로 못 박는다.
 *
 * 두 가지를 본다:
 *
 *   ① 이사 전 경로가 한 줄도 없나 (아래 `TARGETS` 전부).
 *   ② SKILL.md 가 **어느 저장소에서 도는지 말하나.** ①만 고치면 반만 고친 것이다 —
 *      경로를 새 것으로 바꿔 놓아도, 두 저장소 중 어디서 치는지가 없으면 여전히
 *      엉뚱한 곳에서 돈다. 스킬 다섯은 코드 저장소와 자료 저장소 양쪽을 건드린다.
 *
 * ── `src/` 도 본다. 다만 **주석은 빼고 실제로 나가는 것만** (2026-09-03 추가) ──
 *
 * 2026-08-31 에는 「`src/` 의 옛 경로 문자열은 전부 이력 주석이라 일부러 안 센다」고 적고
 * `src/` 를 통째로 뺐다. **그 전제가 한 줄에서 거짓이었다** — `documents.js` 의
 * `BRIEF_HEADER` 첫 줄 `# 문서 아카이브 (50-resources/documents)` 는 주석이 아니라
 * `buildDocumentsBrief` 가 **매 질문의 시스템 프롬프트에 싣는 문자열**이었고, 그 폴더는
 * 이사 때 지워졌다. 검사는 그동안 rc=0 으로 통과했다.
 *
 * 그래서 「`src/` 를 안 본다」가 아니라 **「주석을 지운 뒤에 본다」** 로 바꿨다.
 * 지우는 것은 `//` 줄 주석과 `/* *\/` 덩어리 주석뿐이고, 남는 것(문자열 리터럴·식별자)은
 * 전부 살아 있는 코드다. 실측 2026-09-03: `src/` 의 옛 경로 7줄 중 **6줄이 주석, 1줄이
 * 그 헤더**였다 — 주석을 안 가르면 6줄이 매번 함께 빨개져서 아무도 안 보게 된다.
 *
 * 문자열 안의 `//`(URL) 과 정규식 안의 `/` 를 주석으로 잘못 읽으면 그 뒤가 통째로 사라져
 * **거짓 통과**가 난다. 그래서 아래 `stripComments` 는 상태를 들고 훑고, 그 자신을
 * 가짜 조각으로 먼저 시험한다(③).
 *
 * ── 여기서 안 보는 것 ──
 *
 * 스킬 **스크립트**(`.py`)와 `scripts/*.js` 는 아직 안 본다. 살아 있는 코드가 어느 자리를
 * 보는지는 `check-roots.js` 가 실물 값을 대서 본다 — 문자열 검색이 그 자리를 대신하지
 * 못한다.
 *
 * 그래서 **md 대상 파일에는 예외를 두지 않는다.** 이력을 적어야 하면 경로 문자열 대신
 * 「이사 전 워크스페이스」처럼 말로 적는다. 예외 표시를 만들면 다음 사람이 그것을 붙여
 * 통과시키고, 그게 이 결함이 처음 생긴 방식이다. `src/` 만 주석을 봐주는데, 거기서는
 * 「예전에는 이랬다」를 경로로 적는 것이 실제로 쓸모가 있고(코드 옆이라야 읽힌다)
 * 주석은 사람이 그대로 치는 값이 아니기 때문이다.
 *
 * ── 자료 저장소도 본다 (2026-09-05 추가) ──
 *
 * 색인 둘(`slack-export/index.md` · `documents/index.md`)과 자료 저장소 **뿌리의 md**.
 * 색인은 봇 시스템 프롬프트에 전문이 실리고(`src/archive.js` 의 `buildArchiveBrief`), 뿌리 md 는 사람이 쓴
 * 운영 문서다 — 원래 워크스페이스 핸드오프였던 것이 옮겨와 있어 옛 경로가 남기 쉽다.
 *
 * **채널 md·문서 md 는 안 본다.** 슬랙 원문과 그 변환물이라 여기서 고칠 수 없다.
 * 대상에 넣으면 못 고치는 실패가 매번 나고, 그러면 이 검사 전체가 안 읽히게 된다.
 * 그래서 여기서 「깨끗하다」는 **색인과 운영 문서가 깨끗하다**는 뜻이지 자료 저장소
 * 전체가 깨끗하다는 뜻이 아니다.
 *
 * ── 못 잡는 구간 (적어 두지 않으면 「다 확인됐다」로 읽힌다) ──
 *
 * **이것은 글자 검사다.** 옛 수집 래퍼(2026-09-02 삭제)에서 실제로 고장 났던 것은 주석이
 * 아니라 **파일 위치에서 뿌리까지 몇 단계를 올라가느냐**였는데, 여기서 보는 것은 그 옆
 * 주석의 글자뿐이었다. 깊이만 바꾸고 주석을 그대로 두면 **이 검사는 통과한다.**
 *
 * **`scripts/*.ps1` 은 지금 한 개도 없다** (같은 날 마지막 하나를 걷어냈다). 훑는 팔은
 * 남겨 둔다 — 새로 생기면 자동으로 대상이 된다. 다만 **0개를 실패로 세지 않으므로**
 * (SKILL.md 쪽과 다르다) 이 팔이 지금 무엇을 지키고 있다고 읽지 말 것. **이 검사가
 * 실제로 지키는 갈래 전체는 `targets()` 가 원본이다** — 코드 저장소 쪽은 위 두 절과
 * `targets()` 안의 인라인 주석(CLAUDE.md·씨앗 색인 둘은 거기에만 있다)에, 자료
 * 저장소 쪽은 「자료 저장소도 본다」 절에 적어 두었다. 여기 다시 세어 적지 않는다 —
 * 같은 목록이 두 자리에 있으면 한쪽만 고쳤을 때 갈리고, 그 갈림이 2026-09-06 검토에서
 * 실제로 이 자리에서 두 번 잡혔다(자료 저장소를 더했을 때, 그리고 「위 두 절」이라고
 * 가리켰지만 그 절엔 CLAUDE.md·씨앗 근거가 없었을 때).
 *
 * 마찬가지로 README 의 `log.path` 도 **값이 저장소 밖을 가리키는지**가 아니라 옛 경로
 * 문자열인지만 본다. `"../보관"` 같은 새로운 밖은 못 잡는다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DATA_ROOT, ARCHIVE_DIR, DOCS_DIR } from '../src/config.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SKILLS = path.join(ROOT, '.claude', 'skills');

/** 이사 전 자리를 가리키는 문자열. 하나라도 대상 파일에 있으면 ✗. */
const STALE = [
  '50-resources',
  '20-operations',
  '21-hermes',
  'do-better-workspace',
  '/opt/hermes/workspace',
  'whk_workspace',
  // 워크스페이스 최상위 폴더 나머지 (2026-09-05, WHK 「무조건 막아야 함」).
  // 위 여섯만으로는 `40-personal/46-todos/active-todos.md` 같은 줄이 그대로 통과했다 —
  // README.md 에 실제로 한 줄 있었고, 팀은 그 경로를 못 연다.
  // 하위 폴더(46-todos·47-handoff·00-wiki)는 여기 안 적는다 — 위 여섯에 덮인다.
  '00-inbox',
  '00-system',
  '10-projects',
  '30-knowledge',
  '40-personal',
  '90-archive',
];

/** ② 어느 저장소에서 도는지 말하고 있나. 둘 다 있어야 한다. SKILL.md 에만 건다. */
const MUST_SAY = ['코드 저장소', '자료 저장소'];

let bad = 0;
// 대상 파일 안에서 **실제로 찾은 위반**만 센다 (①·② 두 자리에서만 올린다). `bad` 는
// 준비 실패(스킬 폴더를 못 읽음 등)·probe 실패도 함께 세므로, 「낡았습니다」 배너를
// 찍을지는 이 값으로 가른다 — 아래 파일 끝 절 참조.
let found = 0;
const fail = (m) => {
  console.log(`  ✗ ${m}`);
  bad += 1;
};
const ok = (m) => console.log(`  ✓ ${m}`);

/**
 * JS 소스에서 **주석만** 공백으로 지운다. 줄 수와 줄마다의 글자 수는 그대로 두므로
 * 지운 뒤에도 줄 번호가 원문과 맞는다.
 *
 * 남기는 것: 문자열 리터럴(`'` `"` 백틱) 안의 내용 · 정규식 리터럴 · 식별자.
 * 전부 **실제로 도는 코드**다. 여기 옛 경로가 있으면 그것은 이력이 아니라 지금 나가는 값이다.
 *
 * 문자열·정규식 안의 `/` 를 주석으로 잘못 읽으면 **그 뒤가 통째로 지워져** 진짜 옛 경로가
 * 사라진다 — 거짓 통과다. 그래서 상태를 들고 훑는다. `/` 가 정규식의 시작인지 나눗셈인지는
 * 바로 앞의 뜻있는 글자로 가른다(그것이 `)]}` 나 글자·숫자면 나눗셈, 아니면 정규식).
 * 이 판정이 흔한 방식이고, 이 저장소가 쓰는 모양(`/^## /` · `line.replace(/^>\s?/, '')`)에는
 * 충분하다.
 */
function stripComments(src) {
  let out = '';
  let state = 'code';          // code | line | block | sq | dq | tpl | re
  let prev = '';               // 바로 앞의 뜻있는 글자 (공백 아님)
  const tplStack = [];         // 템플릿 안 `${ }` 의 중괄호 깊이
  for (let i = 0; i < src.length; i += 1) {
    const c = src[i];
    const n = src[i + 1];
    const keep = () => { out += c; if (!/\s/.test(c)) prev = c; };
    // 주석 안: 줄바꿈만 남기고 나머지는 공백으로 — 줄 번호가 안 밀린다.
    const blank = () => { out += c === '\n' ? '\n' : ' '; };

    if (state === 'line') { if (c === '\n') state = 'code'; blank(); continue; }
    if (state === 'block') {
      if (c === '*' && n === '/') { state = 'code'; out += '  '; i += 1; continue; }
      blank();
      continue;
    }
    if (state === 'sq' || state === 'dq' || state === 're') {
      if (c === '\\') { out += src.slice(i, i + 2); i += 1; continue; }
      const close = { sq: "'", dq: '"', re: '/' }[state];
      // 줄바꿈에서도 닫는다 — 이 셋은 줄을 넘지 못한다. 안 닫으면 한 번 잘못 열린
      // 상태가 파일 끝까지 이어져 그 뒤가 통째로 안 걸린다.
      if (c === close || c === '\n') state = 'code';
      keep();
      continue;
    }
    if (state === 'tpl') {
      if (c === '\\') { out += src.slice(i, i + 2); i += 1; continue; }
      if (c === '$' && n === '{') { tplStack.push(0); state = 'code'; out += '${'; i += 1; prev = '{'; continue; }
      if (c === '`') state = 'code';
      keep();
      continue;
    }
    // state === 'code'
    if (c === '/' && n === '/') { state = 'line'; blank(); continue; }
    if (c === '/' && n === '*') { state = 'block'; blank(); continue; }
    if (c === '/' && !(/[)\]}\w$]/.test(prev))) { state = 're'; keep(); continue; }
    if (c === "'") { state = 'sq'; keep(); continue; }
    if (c === '"') { state = 'dq'; keep(); continue; }
    if (c === '`') { state = 'tpl'; keep(); continue; }
    if (tplStack.length) {
      if (c === '{') tplStack[tplStack.length - 1] += 1;
      else if (c === '}') {
        if (tplStack[tplStack.length - 1] === 0) { tplStack.pop(); state = 'tpl'; keep(); continue; }
        tplStack[tplStack.length - 1] -= 1;
      }
    }
    keep();
  }
  return out;
}

/** 볼 파일을 모은다. 하나라도 못 모으면 「없다」가 아니라 실패다. */
function targets() {
  const out = [];

  let skills = [];
  try {
    skills = fs.readdirSync(SKILLS, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name)
      .filter((n) => fs.existsSync(path.join(SKILLS, n, 'SKILL.md')))
      .sort();
  } catch (err) {
    fail(`스킬 폴더를 못 읽었습니다 — ${SKILLS}: ${String(err && err.message).slice(0, 150)}`);
  }
  // 0개를 통과로 셈하지 않는다. 이 검사가 막는 것이 조용한 통과다.
  if (skills.length === 0) fail(`SKILL.md 가 한 개도 없습니다 — ${SKILLS}`);
  for (const n of skills) {
    out.push({ label: `${n}/SKILL.md`, file: path.join(SKILLS, n, 'SKILL.md'), mustSay: true });
  }

  const readme = path.join(ROOT, 'README.md');
  if (fs.existsSync(readme)) out.push({ label: 'README.md', file: readme, mustSay: false });
  else fail('README.md 가 없습니다');

  // 저장소 뿌리의 CLAUDE.md 도 사람이 그대로 읽고 치는 문서다 — 세션마다 자동으로
  // 읽히므로 낡은 경로가 있으면 그 자리에서 바로 잘못 짚힌다.
  const claudemd = path.join(ROOT, 'CLAUDE.md');
  if (fs.existsSync(claudemd)) out.push({ label: 'CLAUDE.md', file: claudemd, mustSay: false });
  else fail('CLAUDE.md 가 없습니다');

  let ps1 = [];
  try {
    ps1 = fs.readdirSync(path.join(ROOT, 'scripts'))
      .filter((f) => f.endsWith('.ps1')).sort();
  } catch (err) {
    fail(`scripts/ 를 못 읽었습니다 — ${String(err && err.message).slice(0, 150)}`);
  }
  for (const f of ps1) {
    out.push({ label: `scripts/${f}`, file: path.join(ROOT, 'scripts', f), mustSay: false });
  }

  /* `src/` 의 `.js` 전부. **주석을 지운 뒤에** 본다 (`strip: true`) — 위 「주석은 빼고」 참조.
   * 하위 폴더(`src/ingest/`)까지 훑으므로 파일이 늘어도 자동으로 대상이 된다.
   * 0개면 실패다 — 봇 소스가 없을 리 없고, 못 읽은 것을 통과로 세는 것이 이 검사가
   * 막으려는 모양이다. */
  const srcRoot = path.join(ROOT, 'src');
  const jsFiles = [];
  const walk = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const abs = path.join(dir, e.name);
      if (e.isDirectory()) walk(abs);
      else if (e.name.endsWith('.js')) jsFiles.push(abs);
    }
  };
  try {
    walk(srcRoot);
  } catch (err) {
    fail(`src/ 를 못 읽었습니다 — ${String(err && err.message).slice(0, 150)}`);
  }
  if (jsFiles.length === 0) fail(`src/ 아래 .js 가 한 개도 없습니다 — ${srcRoot}`);
  for (const f of jsFiles) {
    const rel = path.relative(ROOT, f).replace(/\\/g, '/');
    out.push({ label: rel, file: f, mustSay: false, strip: true });
  }

  // 씨앗은 팀마다 복사되므로, 여기 이사 전 경로가 있으면 **팀마다 퍼진다.**
  // 실제로 자료 저장소의 slack-export/index.md 에 `21-hermes/...` 가 남아 있었고,
  // 그 파일이 자료 저장소에 있어서 이 검사가 못 봤다 (2026-09-01).
  const TPL = path.join(ROOT, 'archive-template');
  for (const rel of ['slack-export/index.md', 'documents/index.md']) {
    const f = path.join(TPL, ...rel.split('/'));
    if (fs.existsSync(f)) out.push({ label: `archive-template/${rel}`, file: f, mustSay: false });
    else fail(`archive-template/${rel} 이 없습니다`);
  }

  /* 자료 저장소. **팀이 나눠 쓰고, push 하면 15분 안에 봇이 인용한다.**
   *
   * 특히 `slack-export/index.md` 는 `src/archive.js` 의 `buildArchiveBrief` 가 「시스템 프롬프트에 항상
   * 실리는 색인」으로 **전문**을 싣는다(`:437` 의 `getIndexText()`). 여기 워크스페이스
   * 경로가 있으면 봇이 매 질문마다 읽는다 — 2026-09-05 에 실제로 `:64` 에 있었다.
   *
   * **채널 md 와 문서 md 는 안 본다.** `slack-export/channels/*.md` 는 사람이 슬랙에 쓴
   * 원문이라 여기서 고칠 수 없고(자리 선언표: 「원문 메시지는 안 고친다」), `documents/**`
   * 는 변환물이다. 대상에 넣으면 **못 고치는 실패가 매번 나서 아무도 안 보게 된다.**
   * 보는 것은 색인 둘과 자료 저장소 뿌리의 md 다 — 뿌리 md 는 사람이 쓴 운영 문서이고,
   * 원래 워크스페이스 핸드오프였던 것이 옮겨와 있다. */
  for (const [label, f] of [
    ['자료 slack-export/index.md', path.join(ARCHIVE_DIR, 'index.md')],
    ['자료 documents/index.md', DOCS_DIR ? path.join(DOCS_DIR, 'index.md') : null],
  ]) {
    if (!f) continue;
    if (fs.existsSync(f)) out.push({ label, file: f, mustSay: false });
    else fail(`${label} 이 없습니다 — ${f}`);
  }

  let dataMd = [];
  try {
    dataMd = fs.readdirSync(DATA_ROOT, { withFileTypes: true })
      .filter((d) => d.isFile() && d.name.endsWith('.md'))
      .map((d) => d.name)
      .sort();
  } catch (err) {
    fail(`자료 저장소 뿌리를 못 읽었습니다 — ${DATA_ROOT}: ${String(err && err.message).slice(0, 150)}`);
  }
  for (const n of dataMd) {
    out.push({ label: `자료 ${n}`, file: path.join(DATA_ROOT, n), mustSay: false });
  }

  return out;
}

/* ③ 주석 지우개를 **먼저 시험한다.** 이것이 틀리면 위 ①이 조용히 헛돈다 —
 * 문자열 안의 `//` 를 주석으로 읽으면 그 뒤가 사라져 진짜 옛 경로가 안 걸리고,
 * 주석을 안 지우면 이력 6줄이 매번 빨개져 아무도 안 보게 된다. */
{
  const S = '50-resources';
  const probe = [
    ['줄 주석은 지운다', `// 예전에는 ${S} 였다\nconst a = 1;`, false],
    ['덩어리 주석은 지운다', `/* 예전에는\n * ${S}\n */\nconst a = 1;`, false],
    ['문자열 리터럴은 남긴다', `const a = '${S}/documents';`, true],
    ['템플릿 리터럴도 남긴다', 'const a = `' + S + '/x${y}`;', true],
    ['문자열 안의 `//` 를 주석으로 안 읽는다', `const u = 'https://x/'; const a = '${S}';`, true],
    ['정규식 안의 `/` 를 주석으로 안 읽는다', `const r = /^##\\s/; const a = '${S}';`, true],
    ['주석 뒤 같은 줄의 코드는 안 지운다', `const a = '${S}'; // 옛 자리`, true],
  ];
  let broken = 0;
  for (const [label, code, want] of probe) {
    const got = stripComments(code).includes(S);
    if (got !== want) { fail(`주석 지우개 — ${label} (기대 ${want}, 받은 값 ${got})`); broken += 1; }
  }
  // 줄 번호가 안 밀려야 아래 보고가 원문을 가리킨다.
  const nl = (s) => (s.match(/\n/g) || []).length;
  for (const [, code] of probe) {
    if (nl(stripComments(code)) !== nl(code)) { fail('주석 지우개 — 줄 수가 달라졌습니다'); broken += 1; break; }
  }
  if (!broken) ok(`주석 지우개가 주석만 지운다 (${probe.length}가지)`);
}

/* ④ STALE 목록이 실제로 걸리나. 대상 파일이 깨끗하면 ①은 **한 번도 안 밟히므로**,
 * 항목에 오타가 있어도 초록이다. 그래서 가짜 줄로 항목마다 한 번씩 밟는다. */
{
  let broken = 0;
  /* **여기 왼쪽 값은 STALE 에서 만들어 쓰면 안 된다.** 시험 줄을 `s` 로 지으면
   * `line.includes(s)` 가 늘 참이라 무엇을 넣어도 통과하는 항진명제가 된다
   * (2026-09-06 검토에서 실제로 그렇게 썼다가 잡혔다 — 오타를 잡으려고 넣은
   * probe 가 오타를 하나도 못 잡고 있었다). 오타를 잡는 힘은 **목록과 따로
   * 손으로 적은 이 두 번째 사본**에서 나온다. */
  const MUST_CATCH = [
    ['00-inbox', '`00-inbox/2026-08-10_주간미팅_요약.pdf` 를 보세요'],
    ['00-system', '`00-system/01-templates/handoff-template.md` 를 읽어'],
    ['10-projects', '경위는 `10-projects/260902_두저장소-운영_설계.md`'],
    ['30-knowledge', '위키 `30-knowledge/00-wiki/거짓-통과.md` 참조'],
    ['40-personal', '백로그: `40-personal/46-todos/active-todos.md`'],
    ['90-archive', '끝난 것은 `90-archive/` 로 옮긴다'],
    ['50-resources', '옛 자리 `50-resources/documents` 를 보던 줄'],
    ['20-operations', '`20-operations/21-hermes/README.md` 접속 절'],
    ['21-hermes', 'VM 의 `21-hermes/logs/*.jsonl` 이 원본'],
    ['do-better-workspace', 'do-better-workspace-v2 를 클론한 뒤'],
    ['/opt/hermes/workspace', '옛 클론 /opt/hermes/workspace 가 남아 있다'],
    ['whk_workspace', 'whk_workspace 라는 옛 이름'],
  ];
  // 짝 수가 STALE 과 다르면, 항목을 늘리면서 시험을 안 늘린 것이다.
  if (MUST_CATCH.length !== STALE.length) {
    fail(`STALE ${STALE.length}개 중 ${MUST_CATCH.length}개만 시험합니다 — 짝을 함께 늘리세요`);
    broken += 1;
  }
  for (const [entry, line] of MUST_CATCH) {
    // ㉮ 오타 잡기. 이 왼쪽 값은 손으로 적은 것이라 STALE 이 틀리면 여기서 갈린다.
    if (!STALE.includes(entry)) {
      fail(`STALE 에 '${entry}' 가 없습니다 — 오타이거나 지워졌습니다`);
      broken += 1;
      continue;
    }
    // ㉯ 시험 줄이 그 항목을 실제로 담고 있나 (시험 자체가 헛돌지 않게).
    if (!line.includes(entry)) {
      fail(`시험 줄이 '${entry}' 를 안 담고 있습니다 — 시험이 헛돕니다`);
      broken += 1;
      continue;
    }
    // ㉰ 매칭 기계가 실제로 잡나. ①이 쓰는 것과 같은 판정이다.
    if (!STALE.some((x) => line.includes(x))) {
      fail(`매칭이 '${entry}' 를 못 잡습니다`);
      broken += 1;
    }
  }
  // 안 걸려야 하는 줄도 본다. 전부 걸리는 목록은 초록이어도 쓸모가 없다.
  for (const clean of ['src/archive.js', 'deploy/README.md', '자료 저장소의 config.json']) {
    if (STALE.some((x) => clean.includes(x))) {
      fail(`STALE 이 멀쩡한 줄을 잡습니다 — ${clean}`);
      broken += 1;
    }
  }
  if (!broken) ok(`STALE ${STALE.length}개가 실제로 걸리고, 멀쩡한 줄 3가지는 안 걸린다`);
}

for (const t of targets()) {
  let raw;
  try {
    raw = fs.readFileSync(t.file, 'utf8');
  } catch (err) {
    fail(`${t.label} 을 못 읽었습니다 — ${String(err && err.message).slice(0, 150)}`);
    continue;
  }
  // md·ps1 은 통째로, `src/` 의 js 는 주석을 지운 뒤에 본다.
  const lines = (t.strip ? stripComments(raw) : raw).split(/\r?\n/);
  const rawLines = raw.split(/\r?\n/);

  /* ① 이사 전 경로 */
  const hits = [];
  lines.forEach((line, i) => {
    for (const s of STALE) {
      if (line.includes(s)) {
        // 한 줄에 둘이 있어도 줄 하나로 센다 — 「고칠 줄이 몇 개인가」가 알고 싶은 값이다.
        // 보이는 것은 **원문 줄**이다 — 주석을 지운 줄을 보이면 사람이 파일에서 못 찾는다.
        hits.push({ no: i + 1, text: (rawLines[i] ?? line).trim().slice(0, 90) });
        break;
      }
    }
  });
  if (hits.length) {
    fail(`${t.label} — 이사 전 경로 ${hits.length}줄${t.strip ? ' (주석 아님 — 실제로 나가는 값입니다)' : ''}`);
    found += 1;
    for (const h of hits.slice(0, 5)) console.log(`      ${h.no}: ${h.text}`);
    if (hits.length > 5) console.log(`      … 그 외 ${hits.length - 5}줄`);
  } else {
    ok(`${t.label} — 이사 전 경로 없음`);
  }

  /* ② 어느 저장소에서 도는지 (SKILL.md 만) */
  if (t.mustSay) {
    const missing = MUST_SAY.filter((w) => !raw.includes(w));
    if (missing.length) {
      fail(`${t.label} — 어느 저장소에서 도는지 안 적혀 있습니다 (없는 말: ${missing.join(' · ')})`);
      found += 1;
    }
  }
}

if (bad) {
  if (found) {
    console.log('\n낡았습니다. 이 파일들은 그대로 실행되거나 그대로 설정에 붙는 값이라,');
    console.log('없는 경로를 대면 에러 대신 **빈 결과·종료코드 0·저장소 밖 쓰기** 로 나타납니다.');
  } else {
    // 위반이 아니라 검사 자체가 덜 돌았다 — 대상을 못 모았거나 자기 검증이 깨진 것.
    // 이 갈림이 필요한 이유: .githooks/pre-commit 이 「낡았습니다」 유무로 위반/불능을
    // 가른다. 갈라 찍지 않으면 준비 실패가 「워크스페이스를 가리킵니다」로 오분류된다
    // (2026-09-06 재검토가 스킬 폴더를 숨겨 실측으로 잡았다).
    console.log('\n검사를 끝내지 못했습니다 — 위 ✗ 는 위반이 아니라 대상을 못 모았거나');
    console.log('자기 검증이 깨진 것입니다. 고치기 전까지 「깨끗하다」로 읽으면 안 됩니다.');
  }
  process.exit(1);
}
console.log('\n문서와 스크립트가 지금 저장소를 가리킵니다.');
