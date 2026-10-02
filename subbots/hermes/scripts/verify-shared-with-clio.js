/**
 * 두 저장소가 나눠 쓰는 파일이 어긋났나 — **고치는 쪽에서** 보이게 한다.
 *
 * 같은 판정이 Clio 의 tests/shared-read-layer.test.js 에도 있다. 거기만 있으면 Hermes 를
 * 고친 사람은 어긋난 줄 모르고, Clio 시험을 누가 돌릴 때까지 그 차이는 에러 없이 흐른다.
 * 목록은 **한 벌뿐이어야 하므로** 저쪽 파일에서 읽어 온다 — 두 곳에 적으면 목록이 어긋난다.
 *
 * **npm run check 에는 안 들어간다 (WHK 결정 2026-09-19).** Hermes 는 팀이 나눠 쓰는
 * 저장소고 Clio 는 소유자 개인 것이라, 대부분의 Hermes 설치에는 Clio 가 아예 없다. 거기서
 * 「못 잼」(종료 2)을 내면 npm run check 가 영원히 0 이 아닌 값으로 끝나고, 그러면 사람이
 * 그 숫자를 무시하게 된다 — 관문 하나를 통째로 죽이는 대가다. 그래서 이것은 **공유 파일을
 * 고칠 때 손으로 돌리는 검사**이고, 그 사실을 src/llm/provider.js 머리 주석에 적어 둔다.
 * 자동으로 지키는 자리는 Clio 쪽 npm test 다.
 */
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const CLIO = path.resolve(ROOT, '../../clio');
const LIST_FILE = path.join(CLIO, 'tests/shared-read-layer.test.js');

if (!fs.existsSync(CLIO)) {
  console.log(`Clio 저장소가 없어 잴 것이 없습니다 — ${CLIO}`);
  process.exit(0);
}

if (!fs.existsSync(LIST_FILE)) {
  throw new Error(`Clio 저장소에서 공유 파일 목록을 찾지 못했습니다 — ${LIST_FILE}`);
}

/* 목록의 원본은 Clio 시험 파일이다. 여기에 베껴 적으면 파일이 하나 늘거나 줄 때
 * 두 목록이 갈리고, 갈린 쪽은 조용히 덜 잰다. */
const source = fs.readFileSync(LIST_FILE, 'utf8');
const literal = source.match(/for \(const file of (\[[^\]]*\])\)/);
assert.ok(literal, 'Clio 시험에서 공유 파일 목록을 찾지 못했습니다 — 저쪽 모양이 바뀌었습니다');
const files = new Function('return ' + literal[1])();
assert.ok(files.length >= 12, `공유 파일이 ${files.length}개뿐입니다 — 목록이 줄었는지 확인하세요`);

const drifted = [];
for (const file of files) {
  const mine = path.join(ROOT, 'src', file);
  const theirs = path.join(CLIO, 'src', file);
  if (!fs.existsSync(mine)) { drifted.push(`${file} — Hermes 에 없습니다`); continue; }
  if (!fs.existsSync(theirs)) { drifted.push(`${file} — Clio 에 없습니다`); continue; }
  if (!fs.readFileSync(mine).equals(fs.readFileSync(theirs))) drifted.push(`${file} — 내용이 다릅니다`);
}

assert.deepEqual(drifted, [],
  '두 저장소가 나눠 쓰는 파일이 어긋났습니다 — 고친 쪽을 상대 저장소에도 그대로 옮기세요:\n  '
  + drifted.join('\n  '));

console.log(`[verify-shared-with-clio] OK — 공유 파일 ${files.length}개가 Clio 와 같습니다`);
