#!/usr/bin/env node
/**
 * `digest.skipChannels` 가 **봇이 읽는 자리에서도** 걸리나 — 슬랙에 안 붙는다.
 *
 *   node scripts/check-skip-channels.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 *
 * ── 왜 있나 ──
 *
 * `skipChannels` 는 설정 주석이 「봇이 아예 다루지 않는 채널」이라고 적어 둔 목록인데,
 * 2026-09-03 까지 거르는 코드가 **쓰기 경로 다섯 곳에만** 있었다 (`slack-live.js` 의
 * `fetchWindow` · `ingest/slack-archive.js` 의 `ingestConversations` · `ingest/backfill.js` 의
 * `backfillTargets` · `archive-health.js` 의 첨부 후보 · doc-archive 의 `fetch_slack_files.py`).
 * **봇이 읽는 쪽에는 한 곳도 없었다.**
 *
 * 그래서 그 채널의 md 가 한 번이라도 생기면 — 목록에 넣기 전에 이미 쌓였거나, 개명으로
 * 설정 줄이 죽은 사이에 쌓였거나(2026-08-10 사고) — 봇은 채널을 디스크의 md 목록으로
 * 세므로(`archive.js` 의 `listArchivedChannels`) 그 즉시 **공개 시스템 프롬프트 색인과
 * 공개 권한 검색에 실렸다.** 실측 2026-09-03: 목록 9개 중 md 가 있는 2개가 둘 다 공개
 * 색인에 있었고 공개 권한 검색에도 걸렸다.
 *
 * 이 검사는 **실물 아카이브를 상대로** 돈다. 사업장·채널 이름을 여기 적지 않는다 —
 * 이 저장소는 팀끼리 나눠 쓰고 `check-business-names.js` 가 그것을 막는다. 이름도
 * 검색 낱말도 전부 그 팀의 `config.json` 과 md 에서 **실행할 때** 꺼내 쓴다.
 */
import fs from 'node:fs';
import path from 'node:path';
import {
  config, PUBLIC_ACCESS, FULL_ACCESS, normalizeChannel, CHANNELS_DIR,
  BLOCKED_NOTE, OUT_OF_SCOPE_NOTE,
} from '../src/config.js';
import { dropSkippedChannels } from '../src/slack-live.js';
import {
  assertArchive, listArchivedChannels, listReadableChannels, withoutSkipped,
  buildArchiveBrief, buildArchiveBriefSplit,
  searchArchive, readChannel, readCached, splitMessages,
} from '../src/archive.js';

let ok = true;
const check = (label, cond, extra = '') => {
  ok &&= !!cond;
  console.log((cond ? '  PASS ' : '  FAIL ') + label + (extra ? `  ${extra}` : ''));
};
const section = (name) => console.log(`\n${name}`);

/* ── ① 거르는 규칙 자체 — 가짜 이름으로 돈다 ──────────────────────────
 *
 * 아래 ② 는 실물을 상대로 돌아서, 그 팀의 `skipChannels` 가 비어 있거나 그 채널 md 가
 * 아직 없으면 **아무것도 못 잰다.** 규칙이 통째로 죽어도 조용히 지나가는 그 상태를
 * 막으려고, 규칙만 따로 여기서 잰다 (`check-backfill.js` 의 `backfillTargets` 절과 같은 꼴). */
section('① 거르는 규칙 (순수 함수)');
{
  const names = ['사업장가', 'z_안다루는채널', '사업장나'];
  const left = withoutSkipped(names, ['z_안다루는채널']);
  check('목록에 적힌 채널을 뺀다', !left.includes('z_안다루는채널'), left.join(','));
  check('나머지는 그대로 둔다', left.length === 2 && left[0] === '사업장가' && left[1] === '사업장나');
  check('목록이 비면 전부 남는다', withoutSkipped(names, []).length === 3);
  check('설정이 아예 없어도 안 죽는다', withoutSkipped(names, undefined ?? []).length === 3);
  /* 설정에는 `#` 를 붙여 적기도 한다. 그대로 대보면 한 글자 차이로 아무것도 안 막는데
   * **에러가 안 난다** — 2026-08-10 개명 사고와 같은 모양이다.
   *
   * **거르는 다섯 자리와 똑같이 `normalizeChannel` 하나만 쓴다.** 여기서만 더 세게 다듬으면
   * 읽기와 쓰기의 판정이 갈리고, 갈리는 쪽이 늘 새는 쪽이다.
   * 그래서 그 함수가 못 다듬는 모양(`' #이름 '` 처럼 `#` **앞에** 공백이 있는 줄. `^#` 를
   * 먼저 떼고 나중에 trim 하므로 `#` 가 남는다)은 여기서도 안 막힌다 — 여섯 자리 공통
   * 한계이고, 고치려면 `config.js` 의 `normalizeChannel` 을 고쳐야 한다. */
  check('설정 쪽 # 를 무시한다', !withoutSkipped(names, ['#z_안다루는채널']).includes('z_안다루는채널'));
  check('설정 쪽 뒤 공백을 무시한다', !withoutSkipped(names, ['z_안다루는채널 ']).includes('z_안다루는채널'));
  check('이름 쪽 # 도 무시한다', !withoutSkipped(['#z_안다루는채널'], ['z_안다루는채널']).length);
  check('부분 일치로 과하게 안 막는다', withoutSkipped(['z_안다루는채널_2'], ['z_안다루는채널']).length === 1);
}

/* ── ①-b 쓰기 자리도 개명을 되짚는다 (2026-09-16) ────────────────────────
 *
 * 읽기 쪽(`withoutSkipped`)만 되짚고 쓰기 자리는 슬랙이 준 **현재** 이름과 글자
 * 그대로 대면, 옛 철자를 config 에 둔 순간 안 다루기로 한 공개 채널이 아카이브에
 * 다시 쌓이고 일일 요약에 실린다 — 2026-08-10 에 21일간 실제로 벌어진 모양이다.
 * `dropSkippedChannels`(자동 반영·요약 수집 공용)를 **가짜 지도**로 잰다 —
 * 실물 `.sync-state.json` 없이도, 진짜 채널 이름 없이도 돈다. */
section('①-b 쓰기 자리의 되짚기 (순수 함수, 가짜 지도)');
{
  const chans = [
    { id: 'C1', name: '사업장가' },
    { id: 'C2', name: 'z_안다루는채널_새이름' },
  ];
  // 개명 지도: 아카이브(=개명 전) 이름 → 슬랙의 현재 이름
  const map = new Map([['z_안다루는채널', 'z_안다루는채널_새이름']]);
  const names = (r) => r.map((c) => c.name).join(',');

  check('옛 철자를 적어 둔 skip 줄이 개명 뒤에도 산다',
    names(dropSkippedChannels(chans, ['z_안다루는채널'], map)) === '사업장가');
  check('새 철자로 고쳐 적어도 산다',
    names(dropSkippedChannels(chans, ['z_안다루는채널_새이름'], map)) === '사업장가');
  check('# 와 뒤 공백도 여전히 무시한다',
    names(dropSkippedChannels(chans, ['#z_안다루는채널 '], map)) === '사업장가');
  check('관계 없는 채널까지 막지는 않는다',
    dropSkippedChannels(chans, ['z_안다루는채널'], map).some((c) => c.name === '사업장가'));
  check('skip 이 비면 전부 남는다',
    dropSkippedChannels(chans, [], map).length === 2);
  check('지도가 비면 글자 그대로 판정과 같다 (개명 없을 때의 동작 보존)',
    names(dropSkippedChannels(chans, ['z_안다루는채널_새이름'], new Map())) === '사업장가'
    && dropSkippedChannels(chans, ['z_안다루는채널'], new Map()).length === 2);

  /* 함수만 맞고 부르는 쪽이 옛 문장을 그대로 두면 위 검사는 전부 통과한 채 아무것도
   * 안 고쳐진다 — `check-backfill.js` ⑧ 절이 실행기를 재는 것과 같은 수법이다. */
  const liveSrc = fs.readFileSync(new URL('../src/slack-live.js', import.meta.url), 'utf8');
  const ingestSrc = fs.readFileSync(new URL('../src/ingest/slack-archive.js', import.meta.url), 'utf8');
  check('fetchWindow 가 dropSkippedChannels 로 거른다',
    /targets = dropSkippedChannels\(targets\)/.test(liveSrc));
  check('fetchWindow 에 옛 필터(skip.has)가 안 남아 있다', !/skip\.has\(ch\.name\)/.test(liveSrc));
  check('ingestConversations 가 dropSkippedChannels 로 거른다',
    /dropSkippedChannels\(await listBotChannels\(client\)\)/.test(ingestSrc));
  check('ingestConversations 에 옛 필터(skip.has)가 안 남아 있다', !/skip\.has\(ch\.name\)/.test(ingestSrc));
}

// 아카이브가 없는 새 클론에서 스택 트레이스가 npm run check 화면에 끼지 않게 한다
// (check-outside-hits.js·check-partial-hits.js 와 같은 이유·같은 처리).
// **① 의 실패는 여기서 삼키지 않는다** — 아카이브가 없는 것과 규칙이 깨진 것은 다르다.
try {
  assertArchive();
} catch (e) {
  console.log(`\n  - ② 는 건너뜁니다: ${e.message.split('\n')[0]}`);
  console.log(ok ? '\n전부 통과' : '\n실패 있음');
  process.exit(ok ? 0 : 1);
}

const skip = (config.digest?.skipChannels || []).map(normalizeChannel);
const archived = listArchivedChannels();
/* **재는 대상은 「목록에 있으면서 md 가 실재하는 채널」이다.** md 가 없는 줄은 지금
 * 샐 것이 없다 — 그러나 막는 코드가 없다는 사실은 그대로이므로, 없다고 통과로 적지
 * 않고 「못 쟀다」로 적는다. */
const leaky = skip.filter((c) => archived.includes(c));

section('② 실물 아카이브 — 봇이 읽는 자리에 skipChannels 가 안 남나');

if (!skip.length) {
  console.log('  - 못 쟀습니다: config.json 의 digest.skipChannels 가 비어 있습니다 (샐 것이 없습니다)');
} else if (!leaky.length) {
  console.log(`  - 못 쟀습니다: skipChannels ${skip.length}줄 중 채널 md 가 실재하는 것이 없습니다`);
  console.log('    (지금은 샐 것이 없지만, 막는 코드가 도는지는 이 상태로 잴 수 없습니다)');
} else {
  /* 검색 낱말은 **그 채널에만 있는 말**을 골라 쓴다. 흔한 말을 고르면 상한(`pickSpread`)에
   * 걸려 그 채널이 결과에 안 나올 수 있고, 그것을 「안 샜다」로 읽으면 검사가 헛돈다. */
  const wordsOf = (name) => {
    const set = new Set();
    for (const msg of splitMessages(readCached(path.join(CHANNELS_DIR, `${name}.md`)))) {
      for (const w of msg.text.toLowerCase().match(/[가-힣a-z0-9]{3,}/g) || []) set.add(w);
    }
    return set;
  };
  const others = new Set();
  for (const c of archived) if (!leaky.includes(c)) for (const w of wordsOf(c)) others.add(w);

  for (const name of leaky) {
    const mine = [...wordsOf(name)].filter((w) => !others.has(w));

    check('색인(공개)에 그 채널 요약이 없다',
      !buildArchiveBriefSplit({ access: PUBLIC_ACCESS }).common.includes(`## #${name}`));
    check('색인(전체)에도 그 채널 요약이 없다', (() => {
      const b = buildArchiveBriefSplit({ access: FULL_ACCESS });
      return !b.common.includes(`## #${name}`) && !b.extra.includes(`## #${name}`);
    })());
    check('쪼개기 전 색인(buildArchiveBrief)에도 없다',
      !buildArchiveBrief({ access: FULL_ACCESS }).includes(`## #${name}`));

    if (!mine.length) {
      console.log(`  - 검색은 못 쟀습니다: 그 채널에만 있는 낱말을 못 찾았습니다 (채널 ${leaky.indexOf(name) + 1})`);
    } else {
      const term = mine[0];
      for (const [label, access] of [['공개', PUBLIC_ACCESS], ['전체', FULL_ACCESS]]) {
        const hits = searchArchive({ query: term, access }).hits.filter((h) => h.channel === name);
        check(`검색(${label})에 그 채널 메시지가 안 나온다`, hits.length === 0, `히트 ${hits.length}건`);
      }
    }

    const r = readChannel({ channel: name, access: FULL_ACCESS });
    check('전문 읽기가 그 채널을 안 내준다', !!r.error && !r.text);

    /* ── 막을 때 **왜** 막는지가 사실이어야 한다 (WHK 지시 2026-09-03) ──
     *
     * 처음에는 권한 차단 문구(`BLOCKED_NOTE`, "비공개 자리에 있어…")를 그대로 썼는데,
     * 안 다루기로 한 채널은 **비공개가 아니다.** 봇은 이 문구를 그대로 읽고 사람에게
     * 옮기므로, 공개 채널을 「비공개라서 못 본다」고 답하게 된다 — 막히는 것은 맞지만
     * 이유가 틀리고, 사람은 권한을 달라고 요청하러 간다. 안 막히는 것 다음으로 나쁜 것이
     * **틀린 이유로 막히는 것**이다. */
    check('막는 문구가 「범위 밖」이다 (「비공개」가 아니다)',
      r.error === OUT_OF_SCOPE_NOTE, r.error === BLOCKED_NOTE ? '지금은 권한 차단 문구가 나간다' : '');
    check('검색으로 짚어 물어도 같은 문구로 닫힌다',
      searchArchive({ query: mine[0] || '자료', channel: name, access: FULL_ACCESS }).note === OUT_OF_SCOPE_NOTE);
    /* `BLOCKED_NOTE` 와 같은 규율을 지킨다 — 이름을 안 담고, 「없다」고도 안 한다.
     * 담으면 봇이 그대로 옮겨 적어, 막아 놓고 이름을 흘리는 꼴이 된다. */
    check('문구에 채널 이름이 안 담긴다', !OUT_OF_SCOPE_NOTE.includes(name));
    check('문구가 「없다」로 닫지 말라고 못박는다', OUT_OF_SCOPE_NOTE.includes('없다'));
  }

  /* 대조군 — 진짜 비공개 채널은 **여전히** 권한 차단 문구로 닫혀야 한다. 새 문구를 넣다가
   * 비공개까지 「범위 밖」으로 바꾸면, 못 보는 이유가 권한인 자리에서 권한 얘기가 사라진다. */
  const hiddenPrivate = (config.privateChannels || []).map(normalizeChannel)
    .filter((c) => !PUBLIC_ACCESS.channels.has(c));
  if (!hiddenPrivate.length) {
    console.log('  - 대조군은 못 쟀습니다: 공개 권한에서 가려지는 비공개 채널이 설정에 없습니다');
  } else {
    const p = readChannel({ channel: hiddenPrivate[0], access: PUBLIC_ACCESS });
    check('대조군 — 비공개 채널은 그대로 권한 차단 문구다', p.error === BLOCKED_NOTE);
  }

  /* 부작용 — 거르기가 **딱 그만큼만** 걷어냈나. 한 개라도 더 사라지면 정상 채널이
   * 봇에게서 통째로 없어진 것이고, 그 실패는 에러 없이 「자료가 없습니다」로만 보인다. */
  const readable = listReadableChannels();
  check('걷어낸 것이 skipChannels 뿐이다 (정상 채널은 그대로)',
    readable.length === archived.length - leaky.length
    && archived.every((c) => readable.includes(c) === !leaky.includes(c)),
    `md ${archived.length}개 → 읽기 대상 ${readable.length}개 (skip ${leaky.length}개)`);

  /* 점검 화면은 **실물을 세야** 한다. 여기까지 걸러 버리면 md 가 멀쩡히 있는 채널을
   * 「아카이브 md 없음」이라고 찍는다 (`check-setup.js` 의 미초대 목록). */
  check('listArchivedChannels 는 디스크 그대로다 (점검 화면이 실물을 센다)',
    leaky.every((c) => archived.includes(c)));
}

console.log(ok ? '\n전부 통과' : '\n실패 있음');
process.exit(ok ? 0 : 1);
