#!/usr/bin/env node
/**
 * **실제 슬랙 ID 가 코드 저장소에 적혀 있나** — 이름 축(`check-business-names.js`)의 사각.
 *
 * 왜 새 축인가
 * ------------
 * 이 저장소는 팀끼리 나눠 쓴다. 그래서 사업장·비공개 채널 **이름**이 들어가는 것을
 * `check-business-names.js` 가 막는다. 그런데 **ID 는 이름이 아니라 안 걸린다** —
 * 2026-09-03 전수로 훑으니 예시 JSON·주석·README 에 **실제 채널 ID 다섯 · 파일 ID 셋**이
 * 이름 대신(또는 이름과 함께) 적혀 있었다. 채널 이름은 이미 가짜(`비공개가`·`사업장타`)로
 * 바꿔 둔 자리에서도 **ID 만 진짜**로 남아 있었다 — 한쪽만 가린 것이다.
 *
 * ID 는 이름보다 구체적이다. 이름은 사람이 읽고 알아보는 것이지만, ID 는 토큰이 있으면
 * 그 채널·파일을 **바로 열 수 있는 좌표**다.
 *
 * `check-channel-ids.js` 와 다르다 — 그쪽은 슬랙 **본문**의 `<#C…>` 링크를 봇이 어떻게
 * 푸는지 보고, 여기는 **저장소 파일에 적힌 글자**를 본다.
 *
 * 재는 법
 * -------
 * 모양(`C0…`·`F0…`)만 보면 예시 ID 까지 잡아 헛걸림이 된다. 그래서 **자료 저장소의 실물
 * 상태 파일과 대본다** — `.sync-state.json` 의 채널 ID 와 `.doc-state.json` 의 파일 ID.
 * 거기 있으면 우리 워크스페이스의 진짜 좌표이고, 없으면 예시다. 네트워크를 안 쓴다.
 *
 * 자료 저장소가 없는 새 클론에서는 **건너뛴다고 화면에 적고** 통과한다 — 조용한 통과가
 * 이 저장소가 가장 경계하는 것이라 건너뜀임을 반드시 말한다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ARCHIVE_DIR, DOCS_DIR } from '../src/config.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/** 실물 상태 파일에서 「우리 것인 ID」를 모은다. 없으면 null (= 못 쟀다). */
function realIds() {
  const out = new Set();
  let read = 0;
  const sync = path.join(ARCHIVE_DIR, '.sync-state.json');
  if (fs.existsSync(sync)) {
    for (const id of Object.keys(JSON.parse(fs.readFileSync(sync, 'utf8')).channels || {})) out.add(id);
    read += 1;
  }
  const doc = DOCS_DIR && path.join(DOCS_DIR, '.doc-state.json');
  if (doc && fs.existsSync(doc)) {
    const d = JSON.parse(fs.readFileSync(doc, 'utf8'));
    for (const k of ['slack_files', 'skipped', 'excluded', 'deferred', 'superseded']) {
      for (const id of Object.keys(d[k] || {})) out.add(id);
    }
    read += 1;
  }
  return read ? out : null;
}

const ids = realIds();
if (!ids) {
  console.log('  - 건너뜀: 자료 저장소의 상태 파일이 없어 「진짜 ID」를 모을 수 없습니다');
  process.exit(0);
}

/* 훑을 곳 — 사람이 읽고 따라 하는 자리와 코드. `node_modules`·`.git` 은 뺀다.
 *
 * **`.gitignore` 된 곳도 훑는다** (예: `.superpowers/` 의 앞선 회차 보고서). 커밋되지
 * 않으니 「저장소에 들어간다」는 판정에는 과하지만, 실제 ID 가 기계에 굴러다니는 것도
 * 위생 문제이고 그 파일이 나중에 추적되기 시작하면 그대로 들어간다. 이 저장소는
 * 이런 자리에서 **넉넉하게 자르는 쪽**으로 정해 왔다. 대신 그래서 **기계마다 결과가
 * 다를 수 있다** — 없는 파일은 안 잡히므로 거짓 실패는 안 난다. */
const SKIP_DIR = new Set(['node_modules', '.git', 'logs']);
const EXT = new Set(['.md', '.js', '.py', '.json', '.sh', '.service', '.timer']);
const files = [];
(function walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory()) {
      if (!SKIP_DIR.has(e.name)) walk(path.join(dir, e.name));
    } else if (EXT.has(path.extname(e.name))) {
      files.push(path.join(dir, e.name));
    }
  }
})(ROOT);

const SELF = path.join(ROOT, 'scripts', 'check-real-ids.js');
const ID_RE = /\b[CFDG]0[A-Z0-9]{9}\b/g;
const hits = [];
for (const f of files) {
  if (f === SELF) continue;            // 이 파일이 자기를 잡지 않게 (설명에 모양이 적혀 있다)
  const text = fs.readFileSync(f, 'utf8');
  text.split('\n').forEach((line, i) => {
    for (const m of line.match(ID_RE) || []) {
      if (ids.has(m)) hits.push({ f: path.relative(ROOT, f), no: i + 1, id: m });
    }
  });
}

if (!hits.length) {
  console.log(`  ✓ 실제 슬랙 ID 가 저장소에 없습니다 (대본 진짜 ID ${ids.size}개 · 파일 ${files.length}개)`);
  process.exit(0);
}

console.error(`  ✗ 실제 슬랙 ID 가 ${hits.length}곳에 적혀 있습니다 — 이 저장소는 팀끼리 나눠 씁니다.`);
console.error('    ID 는 이름보다 구체적입니다(토큰이 있으면 그 채널·파일을 바로 엽니다).');
console.error('    예시로 바꾸세요 — 모양만 지키면 됩니다 (C0EXAMPLE01 · F0EXAMPLE01 …).');
for (const h of hits) console.error(`      ${h.f}:${h.no}  ${h.id}`);
process.exitCode = 1;
