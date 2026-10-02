#!/usr/bin/env node
/**
 * 「전사 종합 카드」 킬 스위치가 **정말로 통째로 끄는지** — `companyWideDocProjects` 를
 * 비웠을 때 카드만 안 나가는 게 아니라, 그걸 계산하려고 도는 로컬 문서 재스캔
 * (`src/documents/search.js` 의 `shouldOfferCards` ②)도 같이 안 도는지 잰다.
 *
 *   node scripts/check-doc-card-kill-switch.js
 *
 * 종료코드: 0 통과 / 1 실패 있음
 *
 * ── 왜 필요한가 (2026-09-11 성능·비용 검토 Important 2) ──
 *
 * `config.json` 의 그 키 주석은 「비우면 카드 기능이 통째로 꺼진다」고 적는다. 그런데
 * 고침 전에는 `searchDocuments` 래퍼가 `shouldOfferCards` 를 먼저 부르고
 * `companyWideCards` 안의 킬 스위치는 그 **다음**이라, 목록을 비워도 `shouldOfferCards`
 * 의 로컬 재스캔(그 사업장 문서 전부를 소문자로 복사해 낱말을 찾는 부분, +42~47ms)은
 * 계속 돌았다. 적힌 것과 실제가 달랐다. 고침은 래퍼 맨 앞에
 * `if (!companyWideDocProjects.length) return r;` 를 추가해 `shouldOfferCards` 호출
 * 자체를 건너뛰게 한 것이다(`src/documents/search.js` 의 `searchDocuments` 래퍼 맨 앞).
 *
 * ── 어떻게 재나, 그리고 무엇을 포기했나 ──
 *
 * `shouldOfferCards` 는 exported 함수가 아니라 같은 모듈 안의 지역 함수라 밖에서 spy 로
 * 갈아끼울 수 없다(ESM import 바인딩은 읽기 전용이고, 내부 호출은 export 바인딩을 안
 * 거친다). **처음에는 `fs.readdirSync` 호출 횟수로 「재스캔이 listDocuments 를 한 번 더
 * 부르는지」를 세려 했다** — `documents.js` 가 `import fs from 'node:fs'`(default import)라
 * 이 스크립트와 같은 객체를 공유하므로 `fs.readdirSync` 를 갈아 끼우면 잡힐 것으로
 * 봤다. **실제로 돌려 보니 '현장나' 경로로 `readdirSync` 가 요청 하나에 10회 걸렸다**
 * (`listProjects()`·`resolveProjectFor`·`canSeeDoc` 등 여러 경로가 같은 디렉터리를 이미
 * 여러 번 훑는다) — 재스캔이 더하는 "+1"이 이 잡음(10) 안에 묻혀 신뢰할 수 있는 신호가
 * 안 됐다. **그래서 이 방식은 버렸다** — 코디네이터가 미리 허락한 대로("잴 방법이
 * 지저분해지면 카드 없음만 재고 그 한계를 보고에 적어라") 여기서 그 한계를 남긴다.
 *
 * [1/3]는 **행동**(카드가 실제로 안 나가는지)을, [2/3]는 **소스 순서**(킬 스위치 가드가
 * `shouldOfferCards` 호출보다 코드 상 먼저 오는지, `check-open-before-deny.js` 의
 * 「코드와 대보기」와 같은 종류)를 잰다 — 순서가 바뀌면(가드가 뒤로 가거나 지워지면)
 * [2/3]가 빨개진다. **[2/3]가 증명하는 것은 "재스캔 함수 자체가 안 불린다"이지 "그
 * 함수 안의 CPU(toLowerCase 사본·termHits·sectionsOf)를 안 쓴다"를 직접 재는 것은
 * 아니다** — 다만 함수가 안 불리면 그 안의 CPU 도 쓸 수가 없다.
 *
 * **[2/3]은 주석·문자열을 지운 사본에서, 가드의 모양 전체를 잰다** (2026-09-11 이월
 * Minor 고침 + 같은 날 회의적 검증의 지적 반영).
 *
 * 그전에는 원본 소스에서 `indexOf` 로 이름의 **위치만** 비교했다. 그래서 가드를 지우고
 * 그 이름이 든 주석을 대신 남기면 초록이 된다 — **오늘 이 저장소가 그렇다는 뜻이 아니다.**
 * 실물 `documents.js` 의 가드 위 주석에는 검사가 찾던 두 문자열이 그대로 들어 있지 않아
 * 옛 방식도 지금은 빨갛다. 여기서 막는 것은 **앞으로 그렇게 될 수 있는 모양**이고,
 * 그것을 일부러 만들어(가드 자리에 두 이름이 정확히 든 주석) 옛 방식 ✓ · 새 방식 ✗ 인
 * 것을 확인했다 (위키 [[거짓-통과]] 계열).
 *
 * [3/3]은 **이웃한 되돌리기 스위치**를 본다 — 안전망(`outsideWhenNarrowedMaxHits`)을 0
 * 이하로 내리면 카드가 **반쯤** 죽고(자동 추론 경로에서만 버려진다), 그 사실을 화면에
 * 내는 `[못잼]` 한 줄이 2026-09-11 까지 **자동 검사 없이 수동 확인만** 이었다. 문구를
 * `_card-warnings.js` 로 빼서 세 상태를 동작으로 재고, `check-setup.js` 가 그것을 정말
 * 부르는지(배선)까지 함께 본다.
 *
 * **실물 아카이브를 안 건드린다** — `check-doc-cards.js` 와 같은 가짜 자료 저장소 방식.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { codeOnly } from './_code-only.js';
import { reportCardWarnings } from './_card-warnings.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

/* ── 가짜 자료 저장소 — companyWideDocProjects 를 **비운다** ─────────────── */

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-doc-card-kill-'));
const DATA = path.join(TMP, 'data');
fs.mkdirSync(DATA, { recursive: true });

const exampleConfig = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf8'));
exampleConfig.search = { companyWideDocProjects: [] }; // 킬 스위치 — 비워 둔다
fs.writeFileSync(path.join(DATA, 'config.json'), JSON.stringify(exampleConfig, null, 2), 'utf8');

/** fixture 가 만든 자리 이름 — 마지막에 개명 지도로 한 번에 적는다 (writeSyncState). */
const projectNames = new Set();

function writeDoc(project, file, lines) {
  projectNames.add(project);
  const dir = path.join(DATA, 'documents', 'projects', project);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, file), lines.join('\n'), 'utf8');
}

/* 개명 지도(`slack-export/.sync-state.json`). **없으면 안 된다** — `src/config.js` 는
 * 아카이브에 자료(대화 md 또는 문서 폴더)가 있는데 지도를 못 읽으면 「죽음」으로 보고
 * 전체 권한이 아닌 접근을 전부 닫는다(fail-closed). 이 검사는 「카드 없음」을 재므로
 * 지도가 죽어도 초록이 뜨지만, 그건 킬 스위치가 아니라 닫힘이 만든 초록이다 —
 * 지도를 제대로 둬야 킬 스위치를 실제로 재게 된다. 빈 지도로 때우면 모든 이름이
 * 「고아」가 되어 마찬가지로 닫힌다. */
function writeSyncState() {
  const channels = {};
  let i = 0;
  for (const name of projectNames) {
    i += 1;
    channels[`C${String(i).padStart(9, '0')}`] = { name, file: name };
  }
  fs.mkdirSync(path.join(DATA, 'slack-export'), { recursive: true });
  fs.writeFileSync(
    path.join(DATA, 'slack-export', '.sync-state.json'),
    JSON.stringify({ channels }, null, 2), 'utf8',
  );
}

function entries(word, count, startDay) {
  const out = [];
  for (let i = 0; i < count; i += 1) {
    const day = String(startDay + i).padStart(2, '0');
    out.push(`**2026-08-${day} · 잡보${startDay + i}.pdf**`, '', `${word} 관련 잡보 내용입니다.`, '');
  }
  return out;
}

// check-doc-cards.js [8] 과 같은 모양 — '알파 베타' 로 부분일치 8건(>3)을 만들어
// shouldOfferCards 의 ② 갈래(로컬 재스캔)에 닿을 조건을 갖춘다. 킬 스위치가 안 먹으면
// 이 조건에서 카드가 계산됐어야 한다(원래 companyWideDocProjects 가 있었다면).
writeDoc('현장나', '20260802-현장잡보.md', [
  '# [잡보] 현장나 현장잡보', '',
  '> **사업장**: 현장나 · **종류**: 잡보',
  '> **열람**: 공개', '',
  '---', '',
  '## 2026-08', '',
  ...entries('알파', 5, 2),
  ...entries('베타', 3, 7),
]);

fs.mkdirSync(path.join(DATA, 'slack-export', 'channels'), { recursive: true });
fs.writeFileSync(path.join(DATA, 'slack-export', 'index.md'), '# 색인\n', 'utf8');
writeSyncState();

process.env.HERMES_DATA_ROOT = DATA;

const { searchDocuments } = await import('../src/documents.js');
const { PUBLIC_ACCESS } = await import('../src/config.js');

const r = searchDocuments({ query: '알파 베타', project: '현장나', access: PUBLIC_ACCESS });

console.log('[1/3] 목록이 비어 있으면 카드가 없다 (행동)');
if (r.cards?.length) bad(`카드가 붙었습니다(킬 스위치가 안 먹습니다): ${JSON.stringify(r.cards)}`);
else ok('카드 없음');

/* 주석·문자열을 지운 사본은 `_code-only.js` 가 만든다 — 이 검사와
 * `check-reexport-binding.js` 가 함께 쓰므로 두 벌로 적지 않는다. */

console.log('[2/3] searchDocuments 소스에서 킬 스위치 가드가 shouldOfferCards 호출보다 먼저다 (소스 순서 · 주석 제외)');
const src = codeOnly(fs.readFileSync(path.join(ROOT, 'src', 'documents', 'search.js'), 'utf8'));
const fnMatch = src.match(/  function searchDocuments\(opts\) \{[\s\S]*?\n  \}/);
if (!fnMatch) {
  bad('src/documents/search.js 에서 function searchDocuments(opts) 를 못 찾았습니다 — 검사 전제가 깨졌습니다');
} else {
  const body = fnMatch[0];
  /* **가드의 모양 전체를 본다** — 이름만 있는지가 아니라 「그 조건 하나로 곧바로
   * 돌아가는지」를 본다. 2026-09-11 회의적 검증이 이름만 보던 판에서 초록으로 빠져나가는
   * 길 셋을 찾았다: ① `return` 만 빠짐(`if (…) { }`) ② `if (false && …)` ③ 조건 뒤집기.
   * 셋 다 **카드 검사 전 계열이 초록**이었다 — `companyWideCards` 안에 같은 킬 스위치가
   * 또 있어(`documents.js` 의 그 함수 첫 줄) 결과가 한 글자도 안 바뀌기 때문이다.
   * 이 가드의 유일한 효과는 `shouldOfferCards` 의 로컬 재스캔을 **아예 안 부르는 것**이라,
   * 그것을 지키는 검사는 여기뿐이다. */
  const GUARD_RE = /if\s*\(\s*!\s*companyWideDocProjects\.length\s*\)\s*return\b/;
  const guardMatch = body.match(GUARD_RE);
  const guardIdx = guardMatch ? guardMatch.index : -1;
  const callIdx = body.indexOf('shouldOfferCards(');
  /** 함수 본문에서 그 자리의 **중괄호 깊이**. 함수 몸통 바로 아래면 1이다.
   * 깊이를 보는 이유(2026-09-11 2차 검증): 가드를 `if (false) { … }` 나 죽은 화살표
   * 함수로 감싸면 문장은 그대로 있는데 **실행되지 않는다.** 프로브로 재 보니 그때 로컬
   * 재스캔이 실제로 돌았는데도 검사는 초록이었다. 깊이 1을 요구하면 그 길이 막힌다. */
  const depthAt = (idx) => {
    let depth = 0;
    for (let i = body.indexOf('{'); i >= 0 && i < idx; i += 1) {
      if (body[i] === '{') depth += 1;
      else if (body[i] === '}') depth -= 1;
    }
    return depth;
  };
  if (guardIdx === -1) {
    bad('searchDocuments 안에 `if (!companyWideDocProjects.length) return …` 모양의 가드가 없습니다 — '
      + '이름만 남고 곧바로 돌아가지 않으면(조건이 바뀌었거나 return 이 빠졌으면) 로컬 재스캔이 계속 돕니다');
  } else if (depthAt(guardIdx) !== 1) {
    bad(`가드가 함수 몸통 바로 아래가 아니라 중괄호 ${depthAt(guardIdx)}겹 안에 있습니다 — `
      + '죽은 코드(`if (false) { … }`·안 부르는 함수) 안으로 들어가면 문장은 남아도 실행되지 않습니다');
  } else if (callIdx === -1) {
    bad('searchDocuments 안에 shouldOfferCards( 호출이 없습니다 — 카드 판정 배선 자체가 빠진 것으로 보입니다');
  } else if (guardIdx > callIdx) {
    bad('companyWideDocProjects.length 가드가 shouldOfferCards( 호출보다 뒤에 있습니다 — '
      + '목록을 비워도 shouldOfferCards 의 로컬 재스캔이 먼저 돌고 난 뒤에야 끊깁니다');
  } else {
    ok('킬 스위치 가드가 shouldOfferCards( 호출보다 소스 상 먼저입니다 — 목록이 비면 재스캔 자체가 안 불립니다');
  }
}

/* ── [3/3] 안전망을 끄면 화면에 `[못잼]` 이 뜨는가 ──────────────────
 *
 * 킬 스위치(`companyWideDocProjects` 비우기)는 카드를 **통째로** 끄고, 안전망
 * (`outsideWhenNarrowedMaxHits` 0 이하)은 **반쯤** 끈다 — 봇이 자리를 안 짚어 검색이
 * 스스로 추론한 경로에서만 카드가 버려진다. 둘 다 「설정 한 줄로 카드가 조용히 죽는」
 * 자리라 같은 검사에 둔다.
 *
 * **무엇이 화면에 찍히는지를 가짜 기록계로 직접 잰다.** 「문구를 만드나」가 아니라
 * 「내보내나」를 재는 것이 요점이다 — 2026-09-11 회의적 검증이, 만들기만 재던 판에서
 * **부르고 안 찍는** 모양 넷(찍는 줄 삭제 · `if (false && …)` · 아무 데도 안 가는 곳으로
 * 보내기 · 인자 뒤바꾸기)이 전부 초록으로 지나가는 것을 보였다. 그래서 찍는 일을
 * `_card-warnings.js` 안으로 들였고(`reportCardWarnings`), 남는 빈틈인 **인자 자리**만
 * 소스로 대본다. 2026-09-11 까지 이 자리는 **자동 검사 없이 수동 확인만** 이었다. */

console.log('[3/3] 안전망이 꺼져 있으면 [못잼] 한 줄이 화면에 나오고, check-setup 이 그것을 부른다');
{
  /** 가짜 기록계로 **무엇이 찍혔는지**를 직접 잰다 — 「만들기」가 아니라 「내보내기」를
   * 재는 것이 요점이다(2026-09-11 회의적 검증: 부르고 안 찍는 모양이 전부 통과했다). */
  const printed = (state) => {
    const lines = [];
    const ret = reportCardWarnings(state, (l) => lines.push(l));
    return { lines, ret };
  };

  const off = printed({ companyWideCount: 1, outsideMax: 0 });
  const on = printed({ companyWideCount: 1, outsideMax: 3 });
  const noCards = printed({ companyWideCount: 0, outsideMax: 0 });

  if (off.lines.length !== 1) bad(`안전망이 꺼졌는데 화면에 낸 줄이 ${off.lines.length}개입니다 — 카드가 조용히 반쯤 죽습니다`);
  else if (!off.lines[0].startsWith('[못잼]')) bad(`[못잼] 표시로 시작하지 않습니다(화면에 안 올라옵니다): ${off.lines[0]}`);
  else if (!off.lines[0].includes('outsideWhenNarrowedMaxHits')) bad(`어느 설정인지 안 적혀 있습니다: ${off.lines[0]}`);
  else if (off.ret !== true) bad('낸 것이 있는데 false 를 돌려줍니다');
  else ok('안전망 꺼짐 → [못잼] 한 줄이 실제로 찍힘');

  if (on.lines.length) bad(`안전망이 켜져 있는데 찍었습니다: ${on.lines[0]}`);
  else ok('안전망 켜짐 → 조용');

  if (noCards.lines.length) bad(`카드 기능 자체가 꺼져 있는데 안전망 얘기를 찍었습니다: ${noCards.lines[0]}`);
  else ok('목록이 비면(카드 기능 꺼짐) → 조용');

  /* 배선 — `check-setup.js` 가 그 함수를 부르나. 찍는 일이 함수 안으로 들어갔으므로
   * 「부르기만 하고 안 찍기」는 이제 만들 수 없다. 남는 빈틈은 **인자를 엉뚱하게 넘기는
   * 것**뿐이라 호출 모양도 함께 본다(주석·문자열은 지운 사본이라 안 세어진다). */
  const setup = codeOnly(fs.readFileSync(path.join(ROOT, 'scripts', 'check-setup.js'), 'utf8'));
  /* 호출 **전체**를 본다 — 첫 `{…}` 까지만 보면 둘째 인자가 사각이 된다. 2026-09-11 2차
   * 검증이 `reportCardWarnings({…}, () => {})` 로 **화면 출력만 끄고** 초록을 받아냈다.
   * 그 자리(`log`)는 시험이 갈아 끼우라고 연 것이지 실사용에서 넘길 자리가 아니다. */
  const callStart = setup.indexOf('reportCardWarnings(');
  let callText = '';
  if (callStart >= 0) {
    let depth = 0;
    for (let i = setup.indexOf('(', callStart); i < setup.length; i += 1) {
      if (setup[i] === '(') depth += 1;
      else if (setup[i] === ')') { depth -= 1; if (!depth) { callText = setup.slice(callStart, i + 1); break; } }
    }
  }
  const args = callText.replace(/^reportCardWarnings\(|\)$/g, '');
  if (!callText) {
    bad('check-setup.js 가 reportCardWarnings(…) 를 안 부릅니다 — 문구는 멀쩡한데 화면엔 아무것도 안 뜹니다');
  } else if (!/companyWideCount:\s*companyWideDocProjects\.length/.test(args) || !/outsideMax\s*[,}]/.test(args)) {
    bad(`check-setup.js 의 호출 인자가 어긋납니다(뒤바뀌면 영영 조용해집니다): ${callText.trim()}`);
  } else if (/\}\s*,/.test(args)) {
    bad(`check-setup.js 가 기록계를 따로 넘깁니다 — 그 자리는 시험용이고, 넘기면 화면에 안 뜹니다: ${callText.trim()}`);
  } else {
    ok('check-setup.js 가 목록 개수와 안전망 값만 넘겨 부른다 (배선 살아 있음 · 기록계 안 넘김)');
  }
}

fs.rmSync(TMP, { recursive: true, force: true });

console.log(failed ? `\n실패 ${failed}건` : '\n전부 통과');
process.exit(failed ? 1 : 0);
