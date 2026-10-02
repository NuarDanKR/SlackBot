#!/usr/bin/env node
/**
 * 「질의에 자리 이름이 있으면 검색이 스스로 좁힌다」 점검.
 *
 * 왜 있나: `search` 는 자리 이름이 질의 안에 있어도 그것을 **낱말 하나로만** 다룬다.
 * 그러면 상한을 자리들이 나눠 갖는 `pickSpread` 가 지목한 자리에 한 칸만 주고 나머지를
 * 다른 자리로 채운다. 실측(2026-09-03, 팀 로그의 실제 질의 78개) — 봇이 안 좁혀 부른
 * 질의에서 돌아온 히트 중 **지목한 자리 것이 대화 22% · 문서 13%** 였다. 나머지는 묻지
 * 않은 자리의 자료이고, 그것이 답에 실리면 **원문 그대로인데 답이 틀린다.**
 *
 * 보는 것 아홉:
 *  ① 자리가 하나만 걸리면 그 자리로 좁힌다
 *  ② 서로 다른 자리가 둘 걸리면 안 좁힌다 (두 자리를 견주는 질문이다)
 *  ③ 띄어 쓴 이름·줄여 쓴 이름도 푼다 (붙어 있는 두 낱말까지 본다)
 *  ④ 목록에 없는 이름은 안 좁힌다
 *  ⑤ 한 글자 낱말은 안 본다 (아무 데나 걸린다)
 *  ⑥ **볼 수 없는 비공개 채널 이름으로는 안 좁혀진다 — 대화(`channel`)도 문서(`project`)도.**
 *     좁혀지면 `BLOCKED_NOTE` 가 돌아와서, 질문자가 이름을 댄 자리에 그 자리가 있다는
 *     것이 드러난다 (qa.md 의 「이름을 댄 자리에는 늘 같은 한 문장으로」). 문서 축은
 *     `narrowableProjects`(documents.js)가 후보를 권한으로 거르는지를 재는 자리다 —
 *     `got.channel` 만 보고 `got.project` 를 안 보면 이 축의 결함이 초록으로 통과한다.
 *  ⑦ 좁혀서 아무것도 못 받으면 **안 좁힌 결과로 되돌아간다** — 이름을 잘못 짚었을 때
 *     있는 자료가 통째로 사라지지 않게
 *  ⑧ `limits.autoNarrow` 를 끄면 예전 동작 그대로다 (되돌리기가 실제로 되나)
 *  ⑨ **좁힘 정보가 호출당 정확히 한 칸씩 쌓인다** — 던진 호출도 칸을 남기고, 값이
 *     제 호출의 칸에 실린다 (convo-log.js 의 `[좁힘:]` 꼬리 · claude.js 의 attachNarrows —
 *     짝은 tool_use id, 칸을 미는 자리는 buildTools 끝의 도구 래퍼)
 *
 * ①~⑤ 는 **합성 목록**으로 재므로 아카이브가 없어도 돈다. ⑥~⑨ 는 실물이 필요해서
 * 없으면 못 잰다고 말하고 건너뛴다.
 *
 * 실행: node scripts/check-auto-narrow.js
 */
import fs from 'node:fs';
import path from 'node:path';
import {
  config, PUBLIC_ACCESS, FULL_ACCESS, canSee, isPrivateChannel, CHANNELS_DIR,
} from '../src/config.js';
import {
  assertArchive, resolveChannel, listReadableChannels,
} from '../src/archive.js';
import { hasDocuments, narrowableProjects, searchDocuments } from '../src/documents.js';
import { detectPlace } from '../src/search-terms.js';
import { autoNarrow, buildTools } from '../src/claude.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

/* 합성 목록. **실제 사업장 이름을 쓰지 않는다** — 이 저장소는 팀끼리 나눠 쓴다
 * (`check-business-names.js`). 여기서 재는 것은 규칙이지 그 팀의 자료가 아니라서
 * 합성으로 충분하다. */
const NAMES = ['사업장가', '사업장나', '다른자리'];
const D = (q) => detectPlace(q, NAMES, resolveChannel);

console.log('[1/9] 자리가 하나만 걸리면 그 자리로 좁힌다');
{
  const r = D('사업장가 잔액 얼마');
  if (r === '사업장가') ok(`'사업장가 잔액 얼마' → ${r}`);
  else bad(`'사업장가 잔액 얼마' → ${r} (사업장가 여야 합니다)`);
}

console.log('[2/9] 서로 다른 자리가 둘 걸리면 안 좁힌다');
{
  const r = D('사업장가 사업장나 잔액 비교');
  if (r === null) ok('두 자리가 걸리면 null');
  else bad(`두 자리가 걸렸는데 '${r}' 로 좁혔습니다 — 견주는 질문에서 한쪽이 사라집니다`);
}

console.log('[3/9] 띄어 쓴 이름도 푼다 (붙어 있는 두 낱말)');
{
  const r = D('사업장 가 잔액');
  if (r === '사업장가') ok(`'사업장 가 잔액' → ${r}`);
  else bad(`'사업장 가 잔액' → ${r} (붙어 있는 두 낱말을 안 보고 있습니다)`);
}

console.log('[4/9] 목록에 없는 이름은 안 좁힌다');
{
  const r = D('없는이름 잔액 얼마');
  if (r === null) ok('없는 이름이면 null');
  else bad(`없는 이름인데 '${r}' 로 좁혔습니다`);
}

console.log('[5/9] 한 글자 낱말은 안 본다');
{
  // '가' 하나만으로 사업장가 를 집으면, 조사·한 글자 낱말이 아무 자리나 집게 된다.
  const r = D('가 잔액');
  if (r === null) ok("'가 잔액' → null");
  else bad(`한 글자 '가' 로 '${r}' 를 집었습니다`);
}

/* ── 여기부터는 실물이 필요하다 ─────────────────────────────────── */
let live = true;
try {
  assertArchive();
} catch (e) {
  live = false;
  console.log(`[못잼]  - [6/9]~[9/9] 건너뜀: ${e.message.split('\n')[0]}`);
}

if (live) {
  console.log('[6/9] 볼 수 없는 비공개 채널 이름으로는 안 좁혀진다 (대화·문서 둘 다)');
  const hidden = (config.privateChannels || []).filter(
    (c) => isPrivateChannel(c) && !canSee(PUBLIC_ACCESS, c),
  );
  if (!hidden.length) {
    console.log('  - 못 잼: 이 팀의 config.json 에 비공개 채널이 없습니다 (privateChannels)');
  } else {
    const name = hidden[0];
    const got = autoNarrow(`${name} 잔액 얼마`, PUBLIC_ACCESS);
    // `got.channel` 만 보고 `got.project` 를 안 보면 거짓 초록이 난다 — documents.js 의
    // listProjects 는 인자를 받지 않아 문서 후보가 access 로 안 걸러진 채 전체 폴더가
    // 후보가 됐던 사고가 실제로 여기 있었다(2026-09-16). 채널 축은 막혀도 문서 축이
    // 좁혀지면 여전히 「그 이름의 자리가 있다」가 드러난다.
    if (got.channel) bad(`볼 수 없는 비공개 채널로 좁혔습니다 — 그 자리가 있다는 것이 드러납니다`);
    else if (got.project) bad(`볼 수 없는 비공개 채널의 문서 폴더로 좁혔습니다 — 그 자리가 있다는 것이 드러납니다`);
    else ok('볼 수 없는 비공개 이름은 대화·문서 둘 다 안 걸림');
  }

  console.log('[7/9] 좁혀서 아무것도 못 받으면 안 좁힌 결과로 되돌아간다');
  /* 조건이 맞는 짝을 실물에서 찾는다 — **자기 이름이 자기 md 에는 없고 남의 md 에는 있는**
   * 채널. 그 이름 하나로 물으면 좁힌 쪽은 0건(낱말이 하나라 부분 일치 안전망도 안 켜진다)
   * 이고 안 좁힌 쪽은 1건 이상이라, 되돌아가는 길이 실제로 쓰이는 유일한 모양이다. */
  const bodies = new Map();
  for (const c of listReadableChannels().filter((x) => canSee(PUBLIC_ACCESS, x))) {
    const f = path.join(CHANNELS_DIR, `${c}.md`);
    if (fs.existsSync(f)) bodies.set(c, fs.readFileSync(f, 'utf8'));
  }
  let probe = null;
  for (const [name, own] of bodies) {
    if (own.includes(name)) continue;
    const elsewhere = [...bodies].some(([other, text]) => other !== name && text.includes(name));
    if (elsewhere) { probe = name; break; }
  }
  /* 대화 쪽에 조건이 맞는 이름이 없으면 **문서 쪽에서 찾는다.** 사업장 이름은 그 사업장
   * 문서 본문에 안 적혀 있는 경우가 흔해(파일명·폴더로만 갈린다) 이쪽이 더 잘 잡힌다. */
  /* `wide` 를 함께 재는 것은 **판정을 가르려고**다 — 넓혀도 0건이면 「되돌아갔다」는 말만
   * 확인할 수 있고, 넓혀서 뭔가 있으면 그것이 실제로 실렸는지까지 확인할 수 있다. 둘을 안
   * 가르면 「원래 아무 데도 없는 낱말」을 「되돌리기가 고장났다」로 읽는다. */
  let docProbe = null; let docWideHas = false; let docSkipNote = null;
  if (!probe && hasDocuments()) {
    /* 후보는 **자동 좁히기가 실제로 쓰는 목록**(`narrowableProjects`)에서 고른다. 전체 폴더
     * 목록에서 고르면 여기 후보와 실제 좁히기의 후보가 어긋난다 — 이 검사는 `[7/9]` 하나를
     * 재려고 쓰는 목록이 그 기능이 쓰는 목록과 같아야 한다.
     *
     * 어긋나면 이렇게 깨진다: 볼 수 없는 폴더는 `BLOCKED_NOTE` 라 **늘 0건**이라서 「좁히면
     * 빈손이 되는」 조건을 언제나 만족한다. 그런데 그 이름으로는 좁히기가 애초에 발동하지
     * 않으므로(근본 수정, 2026-09-16) 되돌아갔다는 말이 안 나오고, 멀쩡한 되돌리기가
     * **거짓으로 빨개진다.**
     *
     * **고르는 순서에 순위는 없다** — 목록 순서(`localeCompare('ko')`)대로 첫 번째를 집는다.
     * 실측 2026-09-16: 전체 39개 중 숨김 3개는 20·28·29번째였고 그 앞에 0건짜리 후보가
     * 9개 더 있어서, 옛 코드도 오늘은 숨김을 안 집었다. 보증이 아니라 순서 덕이다 — 숨김
     * 3개를 강제로 probe 에 넣으면 셋 다 `✗` 가 난다(같은 날 확인).
     *
     * 걸러지는 이유는 둘이다 — 이름이 가려지는 폴더(`maskProject`)와, 이 권한으로 볼 수
     * 있는 문서가 한 건도 없는 폴더(`canSeeDoc`). 오늘 숨김 3개는 다 앞쪽이다. */
    const docCandidates = narrowableProjects(PUBLIC_ACCESS);
    /* **후보가 통째로 비면 `[7/9]` 는 아무것도 못 잰다.** 그냥 넘어가면 문서 축 좁히기가
     * 죽어도 이 검사가 초록으로 지나간다 — `[6/9]` 는 「안 좁혀지는가」를 보는 검사라
     * 통째로 죽은 것도 통과시킨다.
     *
     * 전권으로도 0개면 `narrowableProjects` 가 죽은 것이라 빨갛게 낸다. 공개 권한으로만
     * 0개인 것은 **여기서 판정하지 않는다** — 그 팀 자료가 전부 가려지는 것일 수도, 거르기가
     * 과하게 걸러내는 것일 수도 있고, 이 검사가 가진 정보로는 둘을 못 가른다. 지어내
     * 판정하는 대신 사실만 적어 사람이 보게 한다. */
    if (!docCandidates.length) {
      if (narrowableProjects(FULL_ACCESS).length) {
        docSkipNote = '공개 권한으로 좁힐 수 있는 문서 폴더가 0개라 이 축을 못 쟀습니다 '
          + '— 자료가 전부 가려지는 것이거나, 후보 거르기가 과하게 거르는 것입니다';
      } else {
        bad('문서가 있는데 어떤 권한으로도 좁히기 후보가 0개입니다 — 문서 축 자동 좁히기가 통째로 죽었습니다');
        docSkipNote = '후보가 0개라 못 쟀습니다 (위 ✗ 가 원인입니다)';
      }
    }
    for (const p of docCandidates) {
      const own = searchDocuments({ query: p, project: p, access: PUBLIC_ACCESS });
      if ([...own.hits, ...(own.outside || [])].length) continue;
      docProbe = p;
      docWideHas = searchDocuments({ query: p, access: PUBLIC_ACCESS }).hits.length > 0;
      break;
    }
  }
  if (!probe && !docProbe) {
    // 못 재는 이유를 사실대로 적는다 — 후보가 0개였으면 「이름이 자료 안에 있다」가 아니다.
    console.log(`  - 못 잼: ${docSkipNote || '「좁히면 빈손이 되는」 이름이 이 아카이브에 없습니다 '
      + '(자기 이름이 자기 자료 안에 늘 들어 있는 아카이브입니다)'}`);
  } else {
    const search = buildTools({ access: PUBLIC_ACCESS, touched: new Set() }).find((t) => t.name === 'search');
    const q = probe || docProbe;
    const kind = probe ? '대화' : '문서';
    const empty = probe ? /^## 대화 \(사람 발언\)\n0건입니다\.$/m : /^## 문서 \(원문\)\n0건입니다\.$/m;
    // **이름은 화면에 안 적는다** — 비공개일 수 있고, 여기서 알아야 하는 것은 이름이 아니다.
    const text = await search.run({ query: q });
    if (!text.includes('좁히지 않고 다시 찾았습니다')) {
      bad(`${kind}: 좁혀서 빈손이 됐는데 되돌아갔다는 말이 결과에 없습니다 — 봇이 좁힌 결과로 읽습니다`);
    } else ok(`${kind}: 좁혀서 빈손이면 되돌아가고, 그 사실을 말함`);

    const wideHas = probe ? true : docWideHas;
    if (!wideHas) {
      console.log('  - 못 잼: 넓혀도 0건인 자리라 「넓힌 것이 실렸나」까지는 못 봅니다');
    } else if (empty.test(text)) {
      bad(`${kind}: 넓히면 있는데 결과가 0건입니다 — 되돌아간 결과가 안 실렸습니다`);
    } else ok(`${kind}: 넓혀서 찾은 것이 실제로 실림`);
  }

  console.log('[8/9] limits.autoNarrow 를 끄면 예전 동작 그대로다');
  const visible = listReadableChannels().filter((c) => canSee(PUBLIC_ACCESS, c));
  if (!visible.length) {
    console.log('  - 못 잼: 볼 수 있는 채널이 없습니다');
  } else {
    const q = `${visible[0]} 잔액`;
    const saved = config.limits.autoNarrow;
    config.limits.autoNarrow = false;
    try {
      const off = autoNarrow(q, PUBLIC_ACCESS);
      if (off.channel || off.project) bad('꺼 뒀는데 좁혔습니다 — 되돌리기가 안 됩니다');
      else ok('끄면 안 좁힘');
    } finally {
      config.limits.autoNarrow = saved;
    }
    const on = autoNarrow(q, PUBLIC_ACCESS);
    if (!on.channel) bad(`다시 켰는데 '${q}' 가 안 좁혀집니다 — [8/9] 이 앞의 시험을 망가뜨렸을 수 있습니다`);
    else ok('다시 켜면 좁힘');
  }

  /* ── ⑨ 좁힘 칸이 호출당 정확히 하나인가 ──────────────────────────────────
   *
   * 왕복 검사(check-tool-line-roundtrip.js)는 렌더↔파서만 잰다. 「래퍼가 호출당 한 칸을
   * 민다」·「본문이 그 칸을 채운다」는 거기서 안 잡힌다.
   *
   * **가장 나쁜 고장이 여기 있(었)다.** 칸이 호출과 어긋나면 안 좁혔던 질의에
   * 「사업장가로 좁혀 찾았다」가 붙는다. 에러는 안 나고 로그만 조용히 틀리는데, 그것이
   * 이 로그가 막으려던 오독 그 자체다. 2026-09-17 부터 칸은 도구 래퍼(buildTools 끝)가
   * run 본문 진입 전에 tool_use id 와 함께 밀고, qa.js 가 순서가 아니라 **id 로**
   * 짝지으므로, 칸이 모자라도 남의 칸으로 밀리지는 않는다 — 본문 진입 전에 끝난
   * 호출(예전의 「남는 구멍」)도 래퍼가 미니 닫혔다.
   *
   * 그래도 여기서 재는 것은 남는다 — **실행이 시작된 호출은 무슨 길로 끝나든 칸을 하나
   * 남기고, 값이 제 호출의 칸에 실린다.** 이른 return 갈래를 섞어 두는 것이 그 이빨이다
   * — 칸 미는 자리를 결과 조립 뒤로 되돌리면 여기서 빨개진다. */
  console.log('[9/9] 실행이 시작된 호출마다 좁힘 칸이 정확히 하나 남는다 (짝은 tool_use id)');
  const narrows = [];
  const search9 = buildTools({ access: PUBLIC_ACCESS, touched: new Set(), narrows })
    .find((t) => t.name === 'search');
  const plain = { query: '아무데도없는낱말조합zzq' };
  /* 좁힘이 **실제로 걸리는** 질의를 가운데 하나 섞는다. 빈 칸만 확인하면 값이 실리는
   * 자리를 아무것도 안 지킨다 — 채우기를 통째로 지워도 통과한다. 자리를 가운데 두는 것이
   * 이빨이다: 칸이 밀리면 그 값이 **엉뚱한 순번**에 앉아 아래 단언이 빨개진다. */
  const visible9 = listReadableChannels().filter((c) => canSee(PUBLIC_ACCESS, c));
  const narrowAt = visible9.length ? 2 : -1;
  const calls = [plain, { query: '현황', only: 'archive', where: '아무자리', document: '아무문서' }]
    .concat(visible9.length ? [{ query: `${visible9[0]} 잔액` }] : [])
    .concat([plain]);
  for (const input of calls) await search9.run(input);

  if (narrows.length !== calls.length) {
    bad(`호출 ${calls.length}회인데 좁힘 칸이 ${narrows.length}개입니다 — `
      + '뒤쪽 search 의 좁힘이 한 칸씩 앞으로 밀립니다 (로그만 조용히 틀립니다)');
  } else ok(`호출 ${calls.length}회 · 칸 ${narrows.length}개 (이른 return 갈래 포함)`);

  if (narrows.some((n) => !n || typeof n !== 'object')) bad('좁힘 칸이 객체가 아닙니다');
  if (narrowAt < 0) {
    console.log('  - 못 잼: 볼 수 있는 채널이 없어 「값이 실리는 자리」는 못 봤습니다');
  } else {
    /* 좁힘이 걸린 호출의 칸은 **비어 있으면 안 된다.** 좁혔으면 `narrowedTo`, 좁혔다가
     * 되돌아갔으면 `widenedFrom` — 둘 중 하나는 반드시 실린다(어느 쪽인지는 그 자리에
     * 자료가 있느냐에 달렸으므로 값을 못 박지 않는다). **이름은 화면에 안 적는다.** */
    const hit = narrows[narrowAt];
    if (!hit?.narrowedTo && !hit?.widenedFrom) {
      bad('좁힘이 걸린 호출인데 칸이 비었습니다 — 채우기가 안 되거나 칸이 밀렸습니다');
    } else ok(`좁힘이 걸린 호출의 칸에 값이 실림 (${hit.narrowedTo ? '좁힘' : '넓힘'})`);
    // 나머지는 **전부 빈 칸**이어야 한다 — 지어낸 값이 실리거나 칸이 밀리면 여기서 걸린다.
    const strays = narrows.filter((n, i) => i !== narrowAt && (n.narrowedTo || n.widenedFrom)).length;
    if (strays) bad(`좁힘이 안 걸린 호출 ${strays}개에 값이 실렸습니다 — 칸이 밀렸습니다`);
    else ok('좁힘이 안 걸린 호출의 칸은 빈 칸 (지어내지 않음)');
  }
}

process.exit(failed ? 1 : 0);
