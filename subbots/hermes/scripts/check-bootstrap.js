#!/usr/bin/env node
/**
 * **빈 손으로 시작하는 사람이 문서대로 하면 되나.**
 *
 *     node scripts/check-bootstrap.js
 *
 * 종료코드: 0 통과 / 1 실패 있음
 *
 * ── 왜 남겨 두는가 ──
 *
 * 이 저장소를 처음 받는 사람은 우리가 아니다. 우리 기계에는 `.env` 도 `config.json` 도
 * 이미 있어서, **설치 경로가 깨져도 우리는 영영 모른다.** 2026-08-31 전수감사에서 셋이
 * 나왔고 세 번 다 실패 메시지가 원인을 안 알려줬다:
 *
 *   · 문서가 저장소 주소를 「인자로 넘긴다」고 해놓고 정작 명령에 인자가 없어서,
 *     남의 private 저장소를 clone 하려다 실패하고 **남의 키 등록 화면**을 가리켰다.
 *   · `config.json` 을 어디서 얻는지 아무 데도 없고 템플릿도 없어서,
 *     `npm run check` 가 `[1/6]` 한 줄도 못 찍고 **ENOENT 스택 트레이스**로 죽었다.
 *   · `.env.example` 에 `HERMES_DATA_ROOT` 가 없어서 폴백 경로로 떨어지는데,
 *     그 증상이 위와 **글자까지 같아** 원인이 갈리지 않았다.
 *
 * 네 가지를 본다:
 *
 *   ① `.env.example` 이 `HERMES_DATA_ROOT` 를 들고 있나.
 *   ② `config.example.json` 이 있고, 실물 `config.json` 의 최상위 키를 다 덮나.
 *   ③ 자료 저장소를 못 찾았을 때 **설명하는 실패**를 내나 — 실제로 없는 뿌리를 주고
 *      `src/config.js` 를 불러 본다. 스택 트레이스가 아니라 무엇을 해야 하는지가 나와야 한다.
 *   ④ 설치 문서와 `setup.sh` 의 재실행 안내가 **저장소 주소를 인자로** 넘기나.
 *
 * ── 못 잡는 구간 ──
 *
 * ④ 는 글자 검사다. 명령 모양이 바뀌면 못 본다. ②도 **최상위 키**만 대므로 안쪽 값이
 * 비거나 틀린 것은 안 본다 — 거기는 `npm run check` 의 나머지 절이 실물로 본다.
 * 그리고 이 검사는 **우리 기계에서 도는 흉내**다. 진짜 확인은 빈 VM 에서 한 번 밟아
 * 보는 것이고, 그건 사람만 할 수 있다.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const read = (p) => {
  try {
    return fs.readFileSync(p, 'utf8');
  } catch {
    return null;
  }
};

// 자료 저장소 자리. src/config.js 와 같은 순서(환경변수 → .env)로 직접 읽는다.
// config.js 를 임포트하면 config.json 이 없을 때 그쪽이 process.exit(1) 하는데,
// 그게 이 검사가 지키려는 바로 그 상황이다.
const DATA_ROOT_FOR_CHECK = (() => {
  if (process.env.HERMES_DATA_ROOT) return path.resolve(process.env.HERMES_DATA_ROOT);
  const env = read(path.join(ROOT, '.env')) || '';
  const m = env.match(/^\s*HERMES_DATA_ROOT\s*=\s*(.+?)\s*$/m);
  if (m) return path.resolve(m[1].replace(/^["']|["']$/g, ''));
  return path.resolve(ROOT, '..', 'hermes-archive');
})();

let bad = 0;
const fail = (m) => {
  console.log(`  ✗ ${m}`);
  bad += 1;
};
const ok = (m) => console.log(`  ✓ ${m}`);
const note = (m) => console.log(`      · ${m}`);

/* ── ① .env.example 이 자료 저장소 자리를 들고 있나 ───────────────────────── */

const envExample = read(path.join(ROOT, '.env.example'));
if (envExample === null) {
  fail('.env.example 이 없습니다 — 처음 받는 사람이 무엇을 채워야 하는지 알 길이 없습니다');
} else if (!/^\s*#?\s*HERMES_DATA_ROOT=/m.test(envExample)) {
  fail('.env.example 에 HERMES_DATA_ROOT 가 없습니다');
  note('없으면 폴백 경로로 조용히 떨어지고, 증상이 「config.json 이 없다」와 똑같아집니다');
} else {
  ok('.env.example 이 HERMES_DATA_ROOT 를 들고 있습니다');
}

/* ── ② config.example.json 이 있고 실물의 최상위 키를 덮나 ────────────────── */

const examplePath = path.join(ROOT, 'config.example.json');
const exampleRaw = read(examplePath);
if (exampleRaw === null) {
  fail('config.example.json 이 없습니다 — 새 자료 저장소에는 config.json 이 없습니다');
  note('어디서 얻는지가 어느 문서에도 없으면 그 사람은 여기서 멈춥니다');
} else {
  let example = null;
  try {
    example = JSON.parse(exampleRaw);
  } catch (err) {
    fail(`config.example.json 이 JSON 이 아닙니다 — ${String(err && err.message).slice(0, 120)}`);
  }
  if (example) {
    ok('config.example.json 이 있습니다');
    // 실물과 대본다. 실물이 없는 기계(외부 팀)에서는 못 대므로 그 사실을 적는다 —
    // 「안 댔다」와 「대봤더니 같다」가 화면에서 구별돼야 한다.
    //
    // **자리는 봇에게 물어서 받는다.** 여기서 환경변수만 보고 폴백을 직접 적으면
    // `.env` 로만 값을 주는 기계(= PC 설치 전부)에서 엉뚱한 폴더를 보고 「안 댔다」로
    // 빠진다 — 그러면 이 갈래가 영영 안 돌면서 화면은 ✓ 로 보인다.
    let liveRoot = null;
    try {
      liveRoot = execFileSync(
        process.execPath,
        ['--input-type=module', '-e', "import('./src/config.js').then(m=>console.log(m.DATA_ROOT))"],
        { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
      ).trim();
    } catch { /* 설정을 못 읽는 기계다 — 아래에서 「안 댔다」로 적는다 */ }
    const liveRaw = liveRoot ? read(path.join(liveRoot, 'config.json')) : null;
    if (liveRaw === null) {
      note(`실물 config.json 이 없어 키를 안 댔습니다 (${liveRoot || '자료 저장소 자리를 못 알아냄'})`);
    } else {
      try {
        const live = JSON.parse(liveRaw);
        const missing = Object.keys(live).filter((k) => !(k in example));
        if (missing.length) {
          fail(`config.example.json 에 없는 최상위 키 ${missing.length}개: ${missing.join(' · ')}`);
          note('실물에 키를 더할 때 템플릿을 안 고치면, 새로 까는 사람만 그 기능이 꺼진 채 씁니다');
        } else {
          ok(`실물 config.json 의 최상위 키 ${Object.keys(live).length}개를 다 덮습니다`);
        }
      } catch (err) {
        fail(`실물 config.json 을 못 읽었습니다 — ${String(err && err.message).slice(0, 120)}`);
      }
    }

    /* ②-b 질의 뭉치도 같은 모양으로 본다 (2026-09-01).
     *
     * `check-fixtures.json` 은 검사 셋이 실물 아카이브에 던지는 질의를 담는데, 우리가
     * 실물에서 절·열쇠 이름을 바꿔도 `check-fixtures.example.json` 이 낡은 채 남으면
     * **새 팀은 안 맞는 틀을 채우게 된다.** 그러면 그 팀 검사는 「질의 뭉치는 있는데
     * 열쇠가 없다」로 죽거나 조용히 건너뛴다.
     *
     * 없는 것은 실패가 아니다 — 새 팀에는 실물이 없는 것이 정상이고, 예시 파일이
     * 없으면 그건 아래에서 따로 센다. */
    const fxExample = read(path.join(ROOT, 'check-fixtures.example.json'));
    if (fxExample === null) {
      fail('check-fixtures.example.json 이 없습니다 — 새 팀이 질의 뭉치를 어떻게 만드는지 알 길이 없습니다');
    } else {
      const fxLiveRaw = liveRoot ? read(path.join(liveRoot, 'check-fixtures.json')) : null;
      if (fxLiveRaw === null) {
        note('실물 check-fixtures.json 이 없어 열쇠를 안 댔습니다');
      } else {
        try {
          const ex = JSON.parse(fxExample);
          const live = JSON.parse(fxLiveRaw);
          const gaps = [];
          for (const sec of Object.keys(live)) {
            if (sec.startsWith('_')) continue;
            if (!(sec in ex)) { gaps.push(sec); continue; }
            for (const k of Object.keys(live[sec] || {})) {
              if (!k.startsWith('_') && !(k in (ex[sec] || {}))) gaps.push(`${sec}.${k}`);
            }
          }
          if (gaps.length) {
            fail(`check-fixtures.example.json 에 없는 열쇠 ${gaps.length}개: ${gaps.join(' · ')}`);
            note('실물에 열쇠를 더할 때 예시를 안 고치면, 새 팀은 안 맞는 틀을 채웁니다');
          } else {
            ok('check-fixtures.example.json 이 실물의 절·열쇠를 다 덮습니다');
          }
        } catch (err) {
          fail(`질의 뭉치를 못 읽었습니다 — ${String(err && err.message).slice(0, 120)}`);
        }
      }
    }
  }
}

/* ── ③ 자료 저장소를 못 찾았을 때 설명하는 실패를 내나 ────────────────────── */

// 임시 폴더 만들기도 실패할 수 있다 (권한·디스크·TMPDIR 이 없는 곳). try 밖에 두면
// 그때 사람이 보는 것이 스택 트레이스라, 이 검사가 하려던 말과 정반대가 된다.
let empty = null;
try {
  empty = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-bootstrap-'));
} catch (err) {
  fail(`임시 폴더를 못 만들어 자료 저장소 실패 안내를 **재지 못했습니다** — ${String(err && err.message).slice(0, 120)}`);
  note('재지 못한 것은 「안내가 있다」가 아닙니다');
}
let out = '';
let died = false;
if (empty) try {
  execFileSync(process.execPath, ['--input-type=module', '-e', "import('./src/config.js')"], {
    cwd: ROOT,
    env: { ...process.env, HERMES_DATA_ROOT: empty },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
} catch (err) {
  died = true;
  out = `${err.stdout || ''}${err.stderr || ''}`;
} finally {
  try {
    fs.rmSync(empty, { recursive: true, force: true });
  } catch { /* 지우기 실패는 판정과 무관하다 */ }
}

if (!empty) {
  /* 위에서 이미 ✗ 를 냈다. 여기서 또 세면 원인 하나가 실패 둘로 보인다. */
} else if (!died) {
  fail('자료 저장소가 비어 있는데 config.js 가 그냥 통과했습니다 — 멈춰야 합니다');
} else {
  const hasTrace = /at Object\.readFileSync|node:fs:\d+|at ModuleJob/.test(out);
  const saysWhat = out.includes('HERMES_DATA_ROOT');
  const saysHow = out.includes('config.example.json');
  if (hasTrace) {
    fail('자료 저장소를 못 찾았을 때 스택 트레이스만 냅니다');
    note('처음 받는 사람이 보는 첫 화면입니다 — 무엇을 해야 하는지가 없습니다');
  } else if (!saysWhat || !saysHow) {
    fail(`실패 안내가 모자랍니다 (HERMES_DATA_ROOT: ${saysWhat ? '있음' : '없음'} · config.example.json: ${saysHow ? '있음' : '없음'})`);
  } else {
    ok('자료 저장소를 못 찾으면 무엇을 해야 하는지 말하고 멈춥니다');
  }
}

/* ── ④ 설치 문서가 저장소 주소를 인자로 넘기나 ────────────────────────────── */

const RUN_LINE = /(^|\s)(sudo\s+)?bash\s+\S*setup\.sh(?<args>[^\n`]*)/g;

function checkRunLines(label, text) {
  if (text === null) {
    fail(`${label} 을 못 읽었습니다`);
    return;
  }
  const naked = [];
  let m;
  RUN_LINE.lastIndex = 0;
  while ((m = RUN_LINE.exec(text)) !== null) {
    const args = (m.groups.args || '').trim();
    // 인자가 없거나, `<코드저장소>` 같은 자리표시가 아니라 아무것도 없으면 벗은 명령이다.
    if (!args) {
      const line = text.slice(0, m.index).split('\n').length;
      naked.push(line);
    }
  }
  if (naked.length) {
    fail(`${label} — 저장소 주소 없이 setup.sh 를 시키는 줄 ${naked.length}개 (행 ${naked.join(' · ')})`);
    note('기본값은 원저자 저장소입니다. 남의 private 저장소를 clone 하려다 실패하는데,');
    note('그 실패 안내가 「주소가 틀렸다」고 말하지 않습니다');
  } else {
    ok(`${label} — setup.sh 를 부르는 곳마다 저장소 주소를 넘깁니다`);
  }
}

/* ── ⑤ 자료 저장소 씨앗이 온전한가 ────────────────────────────────────────── */
//
// 코드 저장소만 받은 팀은 자료 저장소를 **빈 채로** 만든다. 빈 저장소로 돌리면
// `assertArchive()` 가 막고, 그 전에 `.gitignore` 가 없어 첫 `git add` 에 원본
// pdf·hwp 와 `.env` 가 들어간다. 그래서 씨앗을 코드 저장소가 들고 있어야 한다.

const TEMPLATE = path.join(ROOT, 'archive-template');
// 씨앗의 `.gitignore` 는 `gitignore.tpl` 이라는 이름으로 둔다 — `.gitignore` 로 두면
// **코드 저장소 안에서도 진짜로 무시가 걸린다** (`init-archive.js` 의 FILES 주석 참조).
const TEMPLATE_FILES = ['gitignore.tpl', 'slack-export/index.md', 'documents/index.md'];

const missingTpl = TEMPLATE_FILES.filter((f) => read(path.join(TEMPLATE, f)) === null);
if (missingTpl.length) {
  fail(`archive-template/ 에 ${missingTpl.join(' · ')} 이(가) 없습니다`);
  note('빈 자료 저장소를 받은 팀이 무엇을 넣어야 하는지 알 길이 없습니다');
} else {
  ok(`archive-template/ 이 씨앗 ${TEMPLATE_FILES.length}개를 들고 있습니다`);
}

/* ── ⑥ 씨앗의 무시 규칙이 자료 저장소에 살아 있나 ──────────────────────────── */
//
// **글자 하나까지 같은지를 보지 않는다** (2026-09-01 고침). 전에는 바이트로 대봤는데,
// 그러면 자기 사정으로 한 줄만 더한 팀은 그때부터 매번 ✗ 이고 문구가 「한쪽만 고친
// 것입니다」라고 **틀린 말**을 한다. 빠져나갈 길이 팀끼리 나눠 쓰는 코드 저장소를 고치는
// 것뿐이라, 이 변경이 지키려는 바로 그 팀이 영구히 빨간 줄을 보게 된다.
//
// 대신 **씨앗의 줄이 전부 살아 있나**만 본다(부분집합). 지켜야 할 방향은 한쪽이다 —
// 팀이 규칙을 **빼면** 첨부 원본 pdf·hwp 나 `.env` 가 커밋에 섞이고, 팀이 규칙을
// **더하는** 것은 그 팀 사정이라 막을 이유가 없다.
//
// 우리 쪽이 씨앗보다 앞서 나간 것(자료 저장소에만 있는 줄)은 실패가 아니라 **알림**이다.
// 그게 없으면 우리가 새 규칙을 만들고 씨앗에 안 옮긴 것을 아무도 모른다.
//
// 자료 저장소를 못 읽으면 「없음」이 아니라 「못 쟀다」로 실패시킨다.

const rulesOf = (t) => t.replace(/\r\n/g, '\n').split('\n')
  .map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));

const tplIgnore = read(path.join(TEMPLATE, 'gitignore.tpl'));
const liveIgnore = read(path.join(DATA_ROOT_FOR_CHECK, '.gitignore'));
if (tplIgnore === null) {
  // ⑤ 가 이미 알렸다. 여기서 또 세지 않는다.
} else if (liveIgnore === null) {
  fail(`자료 저장소의 .gitignore 를 못 읽어 **대보지 못했습니다** — ${DATA_ROOT_FOR_CHECK}`);
  note('검사를 못 한 것은 「같다」가 아닙니다');
} else {
  const seed = rulesOf(tplIgnore);
  const live = new Set(rulesOf(liveIgnore));
  const dropped = seed.filter((l) => !live.has(l));
  if (dropped.length) {
    fail(`씨앗의 무시 규칙 ${dropped.length}줄이 자료 저장소에 없습니다`);
    for (const l of dropped.slice(0, 5)) note(`빠진 줄: ${l}`);
    if (dropped.length > 5) note(`… 그 외 ${dropped.length - 5}줄`);
    note('규칙이 빠지면 첨부 원본 pdf·hwp 나 .env 가 커밋에 섞입니다');
  } else {
    const extra = [...live].filter((l) => !seed.includes(l)).length;
    ok(`씨앗의 무시 규칙 ${seed.length}줄이 자료 저장소에 다 살아 있습니다`
      + (extra ? ` (자료 저장소에만 있는 줄 ${extra}개 — 씨앗에 옮길지 보세요)` : ''));
  }
}

/* ── ⑦ 빈 손으로 실제로 밟아 본다 ────────────────────────────────────────── */
//
// ⑤⑥ 은 파일이 있나만 본다. 이 저장소가 여러 번 다친 모양은 **문서에 적었는데
// 안 돌려 본 것**이라, 여기서는 빈 폴더를 만들어 init-archive 를 실제로 돌리고
// 그 결과로 봇 설정과 아카이브 판정이 서는지까지 본다.
//
// git init 은 하지 않는다 — [1/6] 은 네트워크도 git 도 안 타는 것들만 모은 목록이다.

// ③ 과 같은 이유로 try 안에서 만든다 — 임시 폴더를 못 만들면 이 검사가 하려던 말
// 대신 스택 트레이스가 나온다.
let tmp = null;
try {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-init-'));
  const r = execFileSync(process.execPath, [path.join(ROOT, 'scripts', 'init-archive.js'), tmp], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
  });
  // 씨앗 이름이 아니라 **놓일 이름**으로 찾는다 — `gitignore.tpl` 은 `.gitignore` 로 간다.
  const WANT = ['.gitignore', 'slack-export/index.md', 'documents/index.md'];
  const made = WANT.filter((f) => fs.existsSync(path.join(tmp, f)));
  if (made.length !== WANT.length) {
    fail(`init-archive 가 ${WANT.length}개 중 ${made.length}개만 만들었습니다`);
    note(`안 만들어진 것: ${WANT.filter((f) => !made.includes(f)).join(' · ')}`);
  } else {
    ok(`init-archive 가 빈 폴더에 씨앗 ${made.length}개를 만듭니다`);
  }
  if (!/init-archive|다음|npm/.test(r)) {
    fail('init-archive 가 다음에 무엇을 해야 하는지 안 알려줍니다');
    note('만들고 끝내면 사람이 config.json 복사와 커밋을 빠뜨립니다');
  }

  // 봇이 실제로 설 수 있나 — config.example.json 을 복사한 뒤 설정과 아카이브 판정을 부른다.
  fs.copyFileSync(path.join(ROOT, 'config.example.json'), path.join(tmp, 'config.json'));
  const probe = [
    "import('./src/archive.js')",
    '  .then((m) => { m.assertArchive(); console.log("OK"); })',
    '  .catch((e) => { console.log("ERR " + String(e.message).split("\\n")[0]); });',
  ].join('\n');
  const out = execFileSync(process.execPath, ['--input-type=module', '-e', probe], {
    cwd: ROOT, encoding: 'utf8', env: { ...process.env, HERMES_DATA_ROOT: tmp },
  }).trim();
  if (out.startsWith('OK')) ok('만든 자료 저장소로 봇 설정과 아카이브 판정이 섭니다');
  else fail(`만든 자료 저장소로 봇이 서지 못합니다 — ${out}`);

  // 빈 손이 마주하는 두 상황에서 **다른 말**을 하나. 같은 말을 하면 새 팀이
  // 멀쩡한 archivePath 를 고치러 간다 (2026-09-01 실측: 폴더가 없는데
  // "config.json 의 archivePath 를 확인하세요" 라고 했다).
  const bare = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-bare-'));
  try {
    fs.copyFileSync(path.join(ROOT, 'config.example.json'), path.join(bare, 'config.json'));
    const say = (dataRoot) => execFileSync(process.execPath, ['--input-type=module', '-e',
      'import("./src/archive.js").then((m)=>{try{m.assertArchive();console.log("OK")}'
      + 'catch(e){console.log(String(e.message).split("\\n").join(" "))}})'],
    { cwd: ROOT, encoding: 'utf8', env: { ...process.env, HERMES_DATA_ROOT: dataRoot } }).trim();

    const noFolder = say(bare);                       // slack-export/ 자체가 없다
    fs.mkdirSync(path.join(bare, 'slack-export'));    // 폴더만 있고 index.md 가 없다
    const noIndex = say(bare);

    if (!/만들지 않았|init-archive/.test(noFolder)) {
      fail('아카이브 폴더가 없을 때 「아직 만들지 않았다」고 말하지 않습니다');
      note(`실제 문구: ${noFolder.slice(0, 120)}`);
    } else if (noFolder === noIndex) {
      fail('「폴더가 없다」와 「index.md 가 없다」가 같은 말을 합니다');
      note('둘은 할 일이 다릅니다 — 하나는 init-archive, 하나는 archivePath 확인입니다');
    } else {
      ok('빈 아카이브의 두 상황이 다른 안내를 냅니다');
    }
  } finally {
    fs.rmSync(bare, { recursive: true, force: true });
  }
} catch (err) {
  fail(`init-archive 를 돌려 보지 못했습니다 — ${String(err && err.message).slice(0, 200)}`);
  note('돌려 보지 못한 것은 「된다」가 아닙니다');
} finally {
  if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
}

checkRunLines('deploy/README.md', read(path.join(ROOT, 'deploy', 'README.md')));
checkRunLines('deploy/setup.sh', read(path.join(ROOT, 'deploy', 'setup.sh')));

/* ── ⑧ 문서가 자료 저장소를 만들라고 말하나 ──────────────────────────────── */
//
// 2026-09-01 실측: 「만들라」는 말이 grep 0건이었다. 준비물이 「저장소 둘 다 private」
// 이라고 **있다고 전제**만 하고, README 의 「처음 설치」에는 자료 저장소가 한 번도
// 안 나왔다. 명령을 만들어 놓고 문서가 안 부르면 아무도 안 돌린다.
for (const [label, rel] of [['README.md', 'README.md'], ['deploy/README.md', 'deploy/README.md']]) {
  const t = read(path.join(ROOT, rel));
  if (t === null) { fail(`${label} 을 못 읽었습니다`); continue; }
  if (!t.includes('init-archive')) {
    fail(`${label} 이 init-archive 를 한 번도 안 부릅니다`);
    note('빈 자료 저장소를 채우는 길이 문서에 없으면 새 팀은 여기서 멈춥니다');
  } else {
    ok(`${label} 이 자료 저장소 만드는 절차를 데려갑니다`);
  }
}

/* ── ⑨ 프롬프트의 우리 이름이 자료 저장소에서 오나 ────────────────────────── */
//
// 프롬프트는 실행 중 봇 시스템 메시지에 실린다. 여기 우리 이름을 박아 두면 **남의 팀
// 봇이 우리 이름을 물고 돈다.** 그렇다고 지우면 우리 쪽 문장이 바뀌므로, 자리표시자로
// 두고 값은 자료 저장소의 config.json 에서 읽는다.
//
// 사업장 예시(`{{EX_A}}`…)로 시작했는데 2026-09-01 에 **회사·팀 이름과 받는 사람
// 표기**가 같은 모양으로 남아 있는 것을 찾았다 — 프롬프트 세 개의 첫 줄이었다.
// `check-business-names.js` 는 자료 저장소의 채널·사업장 이름과 대볼 뿐이라 회사
// 이름은 목록에 아예 없어서, 그동안 계속 초록이었다.
const EX_TOKENS = ['{{EX_A}}', '{{EX_B}}', '{{EX_C}}', '{{ORG}}', '{{OWNER}}'];
const promptDir = path.join(ROOT, 'src', 'prompts');
let promptFiles = [];
try {
  promptFiles = fs.readdirSync(promptDir).filter((f) => f.endsWith('.md'));
} catch (err) {
  fail(`src/prompts/ 를 못 읽었습니다 — ${String(err && err.message).slice(0, 120)}`);
}
if (promptFiles.length === 0) fail('src/prompts/ 에 프롬프트가 하나도 없습니다');
const usesToken = promptFiles.some((f) => {
  const t = read(path.join(promptDir, f)) || '';
  return EX_TOKENS.some((k) => t.includes(k));
});
if (!usesToken) {
  fail('프롬프트가 자리표시자를 하나도 안 씁니다');
  note('이름을 박아 두면 남의 팀 봇이 우리 이름을 물고 돕니다');
} else {
  ok('프롬프트가 자리표시자로 되어 있습니다');
}

/* 자리표시자가 있다는 것과 **이름이 안 박혀 있다는 것은 다르다.** 새로 문단을 쓰면서
 * 회사 이름을 그대로 적으면 위 검사는 여전히 초록이다. 그래서 자료 저장소가 들고 있는
 * 우리 이름들을 프롬프트 원문(치환 전)과 직접 대본다 — 거기 있으면 박힌 것이다.
 *
 * 자료 저장소를 못 읽거나 값이 하나도 없으면 **잴 것이 없다**고 적고 넘어간다. 새 팀은
 * 아직 아무것도 안 채운 것이 정상이고, 여기서 막으면 이 검사가 지키려는 팀이 막힌다. */
const cfgRaw = read(path.join(DATA_ROOT_FOR_CHECK, 'config.json'));
let ourNames = [];
if (cfgRaw !== null) {
  try {
    const c = JSON.parse(cfgRaw);
    ourNames = [c.org, c.owner?.label, c.owner?.name, ...Object.values(c.promptExamples || {})]
      // `<…>` 는 예시를 복사만 하고 안 채운 자리다. 2자 미만은 일상 낱말과 겹친다.
      .filter((v) => typeof v === 'string' && v.trim().length >= 2 && !/^<.*>$/.test(v.trim()))
      .map((v) => v.trim());
  } catch { /* 못 파싱하면 아래에서 「잴 것이 없음」으로 나간다 */ }
}
if (!ourNames.length) {
  note(`프롬프트에 우리 이름이 박혔는지는 재지 못했습니다 — 자료 저장소에 댈 이름이 없습니다 (${DATA_ROOT_FOR_CHECK})`);
} else {
  const stuck = [];
  for (const f of promptFiles) {
    const t = read(path.join(promptDir, f)) || '';
    for (const n of ourNames) if (t.includes(n)) stuck.push(`${f} — ${n}`);
  }
  if (stuck.length) {
    fail(`프롬프트에 우리 이름이 박혀 있습니다 ${stuck.length}곳`);
    for (const s of stuck.slice(0, 6)) note(`  ${s}`);
    if (stuck.length > 6) note(`  … 그 외 ${stuck.length - 6}곳`);
    note('config.json 으로 빼고 {{ORG}}·{{OWNER}}·{{EX_A}} 같은 자리표시자를 쓰세요');
  } else {
    ok(`프롬프트에 우리 이름이 안 박혀 있습니다 (이름 ${ourNames.length}개로 프롬프트 ${promptFiles.length}개를 대봄)`);
  }
}

// 치환 뒤에 자리표시자가 남으면 안 된다 — 빠뜨리면 봇이 `{{EX_A}}` 를 그대로 읽는다.
// **프롬프트 전부**를 본다. 한 파일만 보면 나머지는 조용히 안 채워진 채 남는다.
if (promptFiles.length) {
  try {
    const probe = [
      "import('./src/claude.js').then((m) => {",
      `  const names = ${JSON.stringify(promptFiles.map((f) => f.replace(/\.md$/, '')))};`,
      '  const left = names.filter((n) => /\\{\\{EX_[A-Z]\\}\\}/.test(m.renderPrompt(n)));',
      '  console.log(left.length ? "LEFT " + left.join(",") : "CLEAN");',
      '}).catch((e) => console.log("ERR " + String(e.message).split("\\n")[0]));',
    ].join('\n');
    const out = execFileSync(process.execPath, ['--input-type=module', '-e', probe], {
      cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
    if (out === 'CLEAN') ok('치환 뒤 프롬프트에 자리표시자가 남지 않습니다');
    else if (out.startsWith('LEFT')) fail(`치환 뒤에도 {{EX_…}} 가 남습니다 — ${out.slice(5)}`);
    else fail(`치환을 확인하지 못했습니다 — ${out}`);
  } catch (err) {
    fail(`치환을 확인하지 못했습니다 — ${String(err && err.message).slice(0, 200)}`);
    note('확인하지 못한 것은 「채워졌다」가 아닙니다');
  }
}

if (bad) {
  console.log('\n처음 받는 사람이 막힙니다. 우리 기계에는 이미 다 있어서 우리는 안 밟는 자리입니다.');
  process.exit(1);
}
console.log('\n빈 손으로 시작해도 문서가 데려갑니다.');
