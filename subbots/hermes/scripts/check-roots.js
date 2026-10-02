/**
 * 봇과 스킬이 **같은 자리를 가리키나.**
 *
 * 저장소가 둘로 갈린 뒤, 경로를 정하는 코드가 두 언어에 하나씩 있다 —
 * JS 는 `src/config.js` 의 DATA_ROOT, 파이썬은 `.claude/skills/_shared/paths.py` 의 DATA_ROOT.
 * 한쪽만 고치면 나머지가 조용히 옛 자리를 본다. 그 고장은 에러가 아니라 **오답**이다.
 *
 *   node scripts/check-roots.js
 *
 * 종료코드: 0 같음 / 1 갈림 · 못 잼
 *
 * ── 뿌리만 대보면 반쯤만 보는 것이다 ──
 *
 * 갈리는 자리가 뿌리 말고 하나 더 있다. **봇은 하위 경로를 `config.json` 에서 읽고
 * (`config.archivePath`·`config.documentsPath`), 스킬은 `paths.py` 에 박아 두었다**
 * (`DATA_ROOT / "slack-export"`). 뿌리가 같아도 `config.json` 의 그 값을 바꾸면
 * 봇만 따라가고 스킬은 옛 폴더에 남는다 — 뿌리 비교로는 안 잡힌다.
 * 그래서 대화 아카이브·문서 아카이브·설정 파일까지 다섯 쌍을 본다.
 *
 * ── 스킬이 자료 뿌리를 알아내는 길이 둘이다 ──
 *
 * `paths.py` 는 **환경변수 → `.env` → 이웃 폴더** 순으로 찾는다. VM 은 systemd 가
 * 환경변수를 주고, PC 에서 사람이 Claude Code 로 스킬을 돌릴 때는 `.env` 만 있다.
 * 이 검사를 환경변수가 있는 채로만 돌리면 **`.env` 를 읽는 길은 한 번도 안 밟힌다.**
 * 그래서 환경변수가 있으면 그것을 뺀 채로 한 번 더 돌려 둘 다 봇과 맞는지 본다.
 */
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { ROOT, DATA_ROOT, ARCHIVE_DIR, DOCS_DIR, config } from '../src/config.js';

/* `python` 이라는 이름이 없는 곳이 있다 — 리눅스(VM)에는 `python3` 만 깔린 경우가 흔하다.
 * check-shared-rules.js 와 **같은 방식**이다. 여기서 하드코딩하면 이 검사만
 * VM 에서 죽고, 그러면 정작 갈림을 봐야 할 자리에서 안 돈다. */
const PY_NAMES = process.env.PYTHON ? [process.env.PYTHON] : ['python', 'python3'];

const SHARED = path.join(ROOT, '.claude', 'skills', '_shared');
const SNIPPET = ['-c', `
import sys, json
sys.path.insert(0, ${JSON.stringify(SHARED)})
import paths
print(json.dumps({
  "code": str(paths.CODE_ROOT),
  "data": str(paths.DATA_ROOT),
  "archive": str(paths.ARCHIVE),
  "docs": str(paths.DOCS_DIR),
  "config": str(paths.CONFIG),
}))
`];

/** 파이썬 쪽 경로를 받아 온다. 못 돌리면 null 과 사유. */
function askSkill(env) {
  let lastErr = null;
  for (const name of PY_NAMES) {
    try {
      // 캐시가 옛 .pyc 를 물고 오면 고친 값을 못 본다 (2026-08-31 돌연변이 검사에서 실제로 겪음).
      const e = { ...env, PYTHONDONTWRITEBYTECODE: '1' };
      return [JSON.parse(execFileSync(name, SNIPPET, { encoding: 'utf8', env: e })), null];
    } catch (err) {
      lastErr = err;
    }
  }
  return [null, lastErr];
}

const norm = (p) => path.resolve(p);
let bad = 0;

/* ── ① 환경변수가 있는 그대로 (VM 에서 봇이 스킬을 부르는 길) ───────────────── */

const [skill, err] = askSkill(process.env);
if (!skill) {
  console.log(`  ✗ 파이썬을 돌리지 못했습니다 (${PY_NAMES.join(' · ')})`);
  console.log(`      ${String(err && err.message).slice(0, 300)}`);
  console.log('\n못 쟀습니다 — 「갈리지 않았다」가 아닙니다.');
  process.exit(1);
}

const pairs = [
  ['코드 뿌리', norm(ROOT), norm(skill.code)],
  ['자료 뿌리', norm(DATA_ROOT), norm(skill.data)],
  ['대화 아카이브', norm(ARCHIVE_DIR), norm(skill.archive)],
  ['설정 파일', norm(path.join(DATA_ROOT, 'config.json')), norm(skill.config)],
];

/* 문서 기능은 `documentsPath` 를 지우면 통째로 꺼진다 (킬 스위치). 그때 봇의 DOCS_DIR 은
 * null 인데 스킬은 여전히 `DATA_ROOT/documents` 를 본다 — 그건 **갈림이 아니라 그 스위치의
 * 뜻**이므로 ✗ 로 세지 않는다. 대신 안 댔다는 것을 적는다. 안 적으면 「다 봤다」로 읽힌다. */
let docsNote = null;
if (!config.documentsPath) {
  docsNote = '문서 기능이 꺼져 있어(documentsPath 없음) 문서 아카이브는 안 댔습니다';
} else if (process.env.HERMES_DOCS_DIR) {
  docsNote = 'HERMES_DOCS_DIR 로 봇만 다른 곳을 봅니다 — 스킬은 그 값을 안 읽습니다';
  pairs.push(['문서 아카이브', norm(DOCS_DIR), norm(skill.docs)]);
} else {
  pairs.push(['문서 아카이브', norm(DOCS_DIR), norm(skill.docs)]);
}

for (const [name, js, py] of pairs) {
  if (js === py) {
    console.log(`  ✓ ${name} — ${js}`);
  } else {
    console.log(`  ✗ ${name} 갈림\n      봇(JS):   ${js}\n      스킬(PY): ${py}`);
    bad += 1;
  }
}
if (docsNote) console.log(`      · ${docsNote}`);

/* ── ② 환경변수를 뺀 채로 (사람이 PC 에서 스킬만 돌리는 길) ─────────────────── */

if (process.env.HERMES_DATA_ROOT) {
  const stripped = { ...process.env };
  delete stripped.HERMES_DATA_ROOT;
  const [plain, err2] = askSkill(stripped);
  if (!plain) {
    console.log('  ✗ 환경변수 없이 파이썬을 돌리지 못했습니다');
    console.log(`      ${String(err2 && err2.message).slice(0, 300)}`);
    bad += 1;
  } else if (norm(plain.data) === norm(DATA_ROOT)) {
    console.log(`  ✓ 환경변수 없이도 같은 곳 — .env 로 찾아간다`);
  } else {
    console.log('  ✗ 환경변수를 빼면 스킬이 다른 곳을 봅니다');
    console.log(`      봇(JS):        ${norm(DATA_ROOT)}`);
    console.log(`      스킬(.env만):  ${norm(plain.data)}`);
    console.log('      사람이 Claude Code 에서 스킬만 돌리면 이 자리를 봅니다.');
    console.log('      .env 의 HERMES_DATA_ROOT 와 환경변수가 다릅니다.');
    bad += 1;
  }
}

if (bad) {
  console.log('\n갈렸습니다. 봇과 스킬이 다른 자료를 봅니다 — 에러 없이 오답이 납니다.');
  process.exit(1);
}
console.log('\n같습니다.');
