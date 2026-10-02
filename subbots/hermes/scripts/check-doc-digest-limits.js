#!/usr/bin/env node
/**
 * 일일 요약 문서 상한의 **코드 폴백**(config.json 에 값이 없을 때 쓰는 값)이 지금
 * 맞는 값(config.example.json·실물 config.json·README 가 다 같이 말하는 15000/3000)과
 * 같나 — 읽기만 한다. **`src/documents.js` 는 이 조의 파일이 아니라 여기서 고치지
 * 않는다.**
 *
 *   node scripts/check-doc-digest-limits.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (아직 못 고침 — 다른 조에 알리는 자리)
 *
 * ── 왜 필요한가 (2026-09-02 전수조사) ──
 *
 * `README.md` 「자주 바꾸는 설정 — `config.json`」 절은 2026-08-10 에 실제로 벌어진 사고를 적어 두고 있다 — 그날 문서
 * 5건이 8,000자 상한을 다 먹어 한 업무일지가 제목만 실렸고, 2,500자 `perDoc` 상한은
 * 다른 문서의 표를 합계 행 앞에서 끊었다. 그래서 그날 값을 8,000/2,500 → 15,000/3,000
 * 으로 올렸고, `config.example.json`·실물 `config.json` 은 지금 둘 다 15000/3000 이다.
 *
 * 그런데 `src/documents.js` 의 코드 폴백(`config.json` 에 그 키가 없을 때 쓰는 값)은
 * 아직 **8000/2500** — 사고 이전 값 그대로다. 지금은 config.json 에 값이 있어 그
 * 폴백을 영영 안 밟지만, 새 팀이 `digestDocMaxChars`·`digestDocPerDocChars` 를 안 채운
 * config.json 으로 시작하면 **그날 사고를 그대로 재현한다.**
 *
 * 이 검사는 고치지 않는다 — `documents.js` 는 다른 조 것이라 여기서 손댈 수 없다.
 * 값이 갈려 있는 동안은 **일부러 빨갛게 둬서** 다음에 그 파일을 손보는 사람이 보게 한다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

console.log('[1/2] 정본 값 — config.example.json·실물 config.json 이 같은 값을 말하나');
let canonical = null;
{
  const example = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf-8'));
  const exVals = {
    max: example.limits?.digestDocMaxChars,
    per: example.limits?.digestDocPerDocChars,
  };
  if (!exVals.max || !exVals.per) {
    fail('config.example.json 에 digestDocMaxChars·digestDocPerDocChars 가 없습니다');
  } else {
    canonical = exVals;
    pass(`config.example.json = ${exVals.max}/${exVals.per}`);
  }
  // 실물 자료 저장소가 이 PC 에 있으면 함께 본다 (없으면 못 잰 것 — 실패로 안 셈).
  try {
    const { DATA_ROOT } = await import(pathToFileURL(path.join(ROOT, 'src/config.js')).href);
    const live = JSON.parse(fs.readFileSync(path.join(DATA_ROOT, 'config.json'), 'utf-8'));
    const liveVals = { max: live.limits?.digestDocMaxChars, per: live.limits?.digestDocPerDocChars };
    if (canonical && (liveVals.max !== canonical.max || liveVals.per !== canonical.per)) {
      fail(`실물 config.json(${liveVals.max}/${liveVals.per})이 config.example.json 과 다릅니다`);
    } else if (canonical) {
      pass(`실물 config.json 도 같은 값(${liveVals.max}/${liveVals.per})`);
    }
  } catch (e) {
    console.log(`  · 실물 config.json 을 못 봤습니다 (${e.message.split('\n')[0]}) — 못 잰 것으로 둡니다`);
  }
}

console.log('\n[2/2] src/documents.js 의 코드 폴백이 정본과 같나 (읽기만)');
{
  const text = fs.readFileSync(path.join(ROOT, 'src/documents.js'), 'utf-8');
  const maxM = text.match(/DIGEST_DOC_MAX_CHARS\s*=\s*limits\.digestDocMaxChars\s*\?\?\s*(\d+)/);
  const perM = text.match(/DIGEST_DOC_PER_DOC_CHARS\s*=\s*limits\.digestDocPerDocChars\s*\?\?\s*(\d+)/);
  if (!maxM || !perM) {
    console.log('  · documents.js 의 폴백 모양을 못 찾았습니다 (이미 고쳐졌거나 코드가 바뀌었을 수 있습니다) — 못 잰 것으로 둡니다');
  } else if (!canonical) {
    console.log('  · 정본 값을 못 구해 대조를 건너뜁니다');
  } else {
    const code = { max: Number(maxM[1]), per: Number(perM[1]) };
    if (code.max === canonical.max && code.per === canonical.per) {
      pass(`documents.js 의 폴백(${code.max}/${code.per})이 정본과 같습니다`);
    } else {
      fail(
        `documents.js:${text.slice(0, maxM.index).split('\n').length} 의 코드 폴백이 `
        + `${code.max}/${code.per} — 정본(${canonical.max}/${canonical.per})보다 낮습니다. `
        + '2026-08-10 사고 이전 값 그대로입니다 (README.md 참조). '
        + '이 파일은 다른 조 것이라 여기서 못 고칩니다 — DIGEST_DOC_MAX_CHARS·'
        + 'DIGEST_DOC_PER_DOC_CHARS 의 `??` 뒤 리터럴을 15000/3000 으로 맞춰야 합니다.',
      );
    }
  }
}

if (ok) {
  console.log('\n[check-doc-digest-limits] OK — 요약 문서 상한 폴백이 정본과 같습니다.');
} else {
  console.error('\n이 검사는 documents.js 를 못 고칩니다 — 그 파일을 맡은 조가 고쳐야 닫힙니다.');
  process.exitCode = 1;
}
