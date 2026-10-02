#!/usr/bin/env node
/**
 * 선택 의존성이 없어 **못 잰** 시험이, 깨진 시험과 다른 신호를 내나.
 *
 *     node scripts/check-optional-dep-signal.js
 *
 * 종료코드: 0 통과 / 1 실패·못 잼
 *
 * ── 왜 있나 ──
 *
 * `openpyxl` 은 **VM 에 일부러 안 깐다** (`deploy/setup.sh` 의 「3/9 기본 패키지」 절과
 * 그것을 받치는 `deploy/test-setup-packages.sh` ③). SKILL_SCRIPTS 의 파이썬(VM 이
 * 부르는 넷 + 사람이 부르는 스킬 전용 둘, 2026-09-22 정정 — setup.sh 주석 참조)은 표준
 * 라이브러리만 쓰고, openpyxl 을 쓰는 것은 엑셀 변환뿐이며 그것은 사람이 노트북에서
 * 돌린다. 데비안 12 는 시스템 파이썬에 pip install 을 막는다(PEP 668).
 *
 * 그런데 그 라이브러리를 쓰는 시험 넷은 **없을 때도 종료코드 1** 을 냈다. `check-setup.js`
 * 는 파이썬 시험을 0/비0 두 갈래로만 보므로 **「안 깔기로 한 것이 없다」와 「시험이
 * 깨졌다」가 같은 ✗ 로 나왔고**, VM 의 `npm run check` 는 **영구히 「점검 실패」**였다.
 * 2026-09-06 에 VM 에서 실제로 그 상태를 봤다(넷 다 rc=1).
 *
 * 영구히 빨간 검사는 검사가 아니다 — 진짜 고장이 나도 화면이 그대로라 구별되지 않는다.
 *
 * ── 규약 ──
 *
 * **종료코드 2 = 못 쟀음.** 이 저장소가 이미 쓰는 규약이다
 * (`.claude/skills/doc-archive/scripts/check_against_libreoffice.py` — LibreOffice 가
 * 없으면 2 를 내고, SKILL.md 가 「2 를 0 과 가르는 이유: 안 돌아간 검사를 통과로 읽으면
 * 안 된다」고 적어 두었다). 같은 규약을 선택 의존성에도 쓴다.
 *
 * ── 이 검사가 재는 것 ──
 *
 *  ① openpyxl 을 가린 채 돌리면 넷 다 **종료코드 2** 를 낸다 (0 도 1 도 아니다)
 *  ② 그때 화면에 **무엇을 어떻게 깔라**는 말이 나온다
 *  ③ 대조군 — 안 가리면 넷 다 **종료코드 0** 이다. 이게 없으면 「늘 2 를 내는 시험」도
 *     ①을 통과한다. 노트북에는 openpyxl 이 있으므로 여기서 진짜로 갈린다.
 *     **openpyxl 이 없는 기계(VM)에서는 ③ 을 잴 수 없다** — 그때는 「못 쟀음」이라고
 *     적고 넘어간다. 없는 것을 통과로도, 실패로도 읽지 않는다.
 *
 * ── 못 잡는 구간 ──
 *
 *  · 가리는 방법은 `PYTHONPATH` 앞에 `import` 하면 ImportError 를 던지는 모듈을 두는
 *    것이다. 진짜로 안 깔린 기계와 **같은 예외**를 만들지만 같은 상황은 아니다.
 *  · `check-setup.js` 가 그 2 를 실제로 「못 쟀음」으로 세는지는 여기서 안 본다 —
 *    그쪽은 `scripts/check-setup-skip.js` 가 본다.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };
const 못쟀음 = (m) => console.log(`  · ${m}`);

/** 대상은 **모든** 스킬 시험이다. 「어느 시험이 openpyxl 을 쓰나」를 import 줄로
 *  가려내려 했다가 두 번 놓쳤다 (2026-09-06) — 처음에는 `from X import` 를 안 봐서 둘,
 *  고친 뒤에도 **두 단계 건너 닿는 시험**을 하나. 정적 분석은 사슬이 길어지는 만큼
 *  계속 어긋난다.
 *
 *  그래서 **가려내지 않고 전부 돌린다.** 지키는 것은 한 줄이다 —
 *  **openpyxl 을 가린 채 돌렸을 때 종료코드 1 을 내는 시험이 하나도 없어야 한다.**
 *  안 쓰는 시험은 0 을 내고, 쓰는 시험은 2(못 쟀음)를 낸다. 새 시험이 늘어도,
 *  import 사슬이 깊어져도 이 판정은 안 어긋난다. */
const SKILL_DIR = path.join(ROOT, '.claude', 'skills');

function findPyTests(dir) {
  const out = [];
  if (!fs.existsSync(dir)) return out;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory()) {
      if (['node_modules', 'venv', '.venv', '__pycache__', '.git'].includes(e.name)) continue;
      out.push(...findPyTests(path.join(dir, e.name)));
    } else if (/^test_.*\.py$/.test(e.name)) out.push(path.join(dir, e.name));
  }
  return out;
}

const TESTS = findPyTests(SKILL_DIR).sort();

const PY = ['python', 'python3'].find((n) => {
  const p = spawnSync(n, ['--version'], { encoding: 'utf-8' });
  return !p.error && p.status === 0;
});

if (!PY) {
  bad('python/python3 를 찾지 못해 **재지 못했습니다**');
} else if (!TESTS.length) {
  // 0개는 「없다」가 아니라 「못 찾았다」다 — 찾기가 깨지면 이 검사가 조용히 통과한다.
  bad('스킬 시험(test_*.py)을 하나도 못 찾았습니다 — 찾기가 깨졌습니다');
} else {
  // openpyxl 을 가리는 자리. import 하면 ImportError 를 던진다.
  const shim = fs.mkdtempSync(path.join(os.tmpdir(), 'no-openpyxl-'));
  fs.writeFileSync(path.join(shim, 'openpyxl.py'), 'raise ImportError("가림 — check-optional-dep-signal.js")\n');

  const env = { ...process.env };
  delete env.HERMES_DATA_ROOT; // check-setup.js 와 같은 이유 (시험이 진짜 아카이브를 보면 안 된다)
  env.PYTHONPATH = shim + path.delimiter + (process.env.PYTHONPATH || '');
  env.PYTHONDONTWRITEBYTECODE = '1';

  // 이 기계에 openpyxl 이 실제로 있나 — ③ 을 잴 수 있는지 정한다.
  const 있나 = spawnSync(PY, ['-c', 'import openpyxl'], { encoding: 'utf-8' }).status === 0;

  const 못잼 = [];   // 가리면 2 를 내는 것 = openpyxl 에 닿는 시험
  let 무관 = 0;      // 가려도 0 = openpyxl 을 안 쓰는 시험

  for (const t of TESTS) {
    const rel = path.relative(ROOT, t).replace(/\\/g, '/');

    // ① 가린 채 돌린다. 1 이 나오면 「안 깔기로 한 것」이 「깨졌다」와 같은 신호가 된다.
    const r = spawnSync(PY, [t], { encoding: 'utf-8', env });
    const out = `${r.stdout || ''}${r.stderr || ''}`;
    if (r.status === 0) { 무관 += 1; continue; }
    if (r.status !== 2) {
      bad(`${rel} — openpyxl 을 가리면 종료코드 ${r.status} 입니다 (0 이거나 2 여야 합니다). 「안 깔기로 한 것이 없다」가 「시험이 깨졌다」와 같은 ✗ 가 됩니다`);
      const 첫줄 = out.trim().split('\n').filter((l) => l.trim()).pop() || '';
      console.error(`      ${첫줄.slice(0, 140)}`);
      continue;
    }
    못잼.push(rel);
    ok(`${rel} — openpyxl 이 없으면 종료코드 2 (못 쟀음)`);

    // ② 무엇을 어떻게 깔라고 말하나
    if (/openpyxl/.test(out) && /requirements\.txt/.test(out)) ok(`${rel} — 무엇을 깔아야 하는지 말한다`);
    else bad(`${rel} — 못 쟀다고만 하고 깔 방법을 안 말합니다: ${JSON.stringify(out.trim().slice(0, 120))}`);

    // ③ 대조군 — 안 가리면 0. 없으면 「늘 2 를 내는 시험」도 ①을 통과한다.
    if (!있나) continue;
    const c = spawnSync(PY, [t], { encoding: 'utf-8', env: { ...env, PYTHONPATH: process.env.PYTHONPATH || '' } });
    if (c.status === 0) ok(`${rel} — 가리지 않으면 통과한다 (대조군)`);
    else bad(`${rel} — openpyxl 이 있는데 종료코드 ${c.status} 입니다. 위 2 가 「늘 2」인지 구별되지 않습니다`);
  }

  // **0건은 통과가 아니다.** 가려도 아무 시험이 안 반응했다면 가림이 안 먹은 것이고,
  // 그러면 이 검사는 아무것도 안 재고 초록을 낸 것이다.
  if (!못잼.length) bad(`시험 ${TESTS.length}개를 openpyxl 없이 돌렸는데 반응한 것이 하나도 없습니다 — 가림(PYTHONPATH shim)이 안 먹었습니다`);
  else console.log(`      openpyxl 에 닿는 시험 ${못잼.length}개 · 무관한 시험 ${무관}개`);

  if (!있나) 못쟀음('이 기계에는 openpyxl 이 없어 대조군(③)을 재지 못했습니다 — openpyxl 이 있는 기계에서 돌리세요');

  fs.rmSync(shim, { recursive: true, force: true });
}

console.log(failed ? `\n실패 ${failed}건` : `\n통과 — 시험 ${TESTS.length}개`);
process.exit(failed ? 1 : 0);
