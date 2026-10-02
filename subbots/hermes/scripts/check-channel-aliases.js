#!/usr/bin/env node
/**
 * 개명 되짚기가 **실제로 도나**, 그리고 되짚을 근거가 없는 자리가 있나.
 *
 * 권한 판정은 개명 지도로 이름을 한쪽으로 모은다(config.js 의 canonicalChannel).
 * 지도는 `.sync-state.json` 에서 오므로 거기 없는 채널은 **되짚을 근거가 없다.**
 * 그런 채널에 대화 md 나 문서 폴더가 있으면 가려야 할 것을 못 가릴 수 있다.
 *
 * 내용은 수집이 만들고 수집은 기록을 남기므로 대개 0건이다. 예외는 셋이다 —
 * **등록 전에 이미 쌓인 md · 개명으로 설정 줄이 죽은 사이에 쌓인 md · 사람이 손으로 넣은
 * md** (archive.js 의 `listReadableChannels` 주석이 원본이다). 0건인 것을 여기서 확인한다.
 *
 * **[3/6] 는 실물이 아니라 가짜 아카이브로 잰다.** 이 워크스페이스에는 개명된 비공개
 * 채널이 0개라 [1/6] 가 「못 잼」으로 끝나는데, 그러면 **되짚기를 통째로 꺼도 이 저장소의
 * 관문이 전부 초록이다**(2026-09-16 전체 검토에서 실측). `CLAUDE.md` 는 기여자에게 고쳤으면
 * `npm run check` 하나라고 말하므로, 그 약속이 이 판정까지 덮어야 한다.
 *
 * **이름은 화면에 안 적는다** — 비공개 채널 이름이다. 건수만 낸다.
 * (가짜 이름 `ch-was`·`ch-now` 는 여기서 지어낸 것이라 적어도 된다.)
 *
 * 실행: node scripts/check-channel-aliases.js
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import {
  config, CHANNELS_DIR, PUBLIC_ACCESS, normalizeChannel, isPrivateChannel, canSee,
  currentChannelNames, archiveChannelNames, canonicalChannel, channelMapStatus,
} from '../src/config.js';
import { listProjects } from '../src/documents.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

const priv = (config.privateChannels || []).map(normalizeChannel);
const aliases = currentChannelNames();              // 아카이브 이름 → 현재 이름 (개명된 것만)
const inState = new Set(archiveChannelNames().keys());  // 수집 기록이 아는 현재 이름 전부

/* 아카이브에 내용이 있는 이름들 — 대화 md 파일명과 문서 폴더 최상위. */
const withContent = new Set();
if (fs.existsSync(CHANNELS_DIR)) {
  for (const f of fs.readdirSync(CHANNELS_DIR)) {
    if (f.endsWith('.md')) withContent.add(normalizeChannel(f.slice(0, -3)));
  }
}
for (const p of listProjects()) withContent.add(normalizeChannel(p.split('/')[0]));

/* ① 되짚기가 실제로 먹히나 — **성질을 직접 잰다.**
 *
 * 프록시 조건(「기록에 있나」 따위)으로 재면 정작 위험한 경우를 놓친다. 재야 하는 것은
 * 하나다: **비공개 채널의 옛 이름 자료가 공개 권한으로 열리면 안 된다.** */
console.log('[1/6] 개명된 비공개 채널의 옛 이름이 공개 권한으로 안 열리나');
const renamedPrivate = [...aliases].filter(([, now]) => isPrivateChannel(now));
if (!renamedPrivate.length) {
  // **조용히 통과시키지 않는다.** 잴 것이 없으면 없다고 말한다. ([3/6] 이 가짜 자료로 잰다)
  console.log('  - 못 잼: 개명된 비공개 채널이 없습니다 (지금 이 워크스페이스가 그렇습니다)');
} else {
  let leaked = 0;
  for (const [was] of renamedPrivate) if (canSee(PUBLIC_ACCESS, was)) leaked += 1;
  if (leaked) bad(`개명된 비공개 채널 ${leaked}개의 옛 이름이 공개 권한으로 열립니다`);
  else ok(`개명된 비공개 채널 ${renamedPrivate.length}개 — 옛 이름도 막힘`);
}

/* ② 되짚을 근거가 아예 없는 선언 — 내용이 있으면 위험하고, 없으면 그냥 확인 불가다.
 *
 * 내용은 수집이 만들고 수집은 기록을 남기므로 대개 「기록에 없다 = 내용도 없다」이다.
 * 예외 셋은 위 헤더에 적었고, 그 경우만 빨갛게 낸다.
 *
 * **대보는 이름을 되짚는다.** config 에는 사람이 어느 철자로든 적는데(옛 이름을 적어도
 * 살게 만든 것이 이 작업의 요점이다) `inState` 에는 **현재 이름만** 들어 있다. 그대로
 * 대면 옛 이름을 적은 워크스페이스에서 **멀쩡한 설정이 빨갛게 난다** — 실제로 그랬다
 * (2026-09-16 전체 검토). 진짜 모르는 이름은 되짚어도 그대로라 여전히 잡힌다. */
console.log('[2/6] 수집 기록에 없는 비공개 선언');
/* **지도가 죽었거나 비어 있으면 「못 잼」이 아니라 빨갛다.** 예전에는 죽은 지도(빈 집합)와
 * 선언을 그대로 대봤는데, 그 둘은 원리상 절대 안 맞아서 전부 「기록에 없음」으로 흘렀고
 * 내용이 없으면 초록이었다 — 지도가 통째로 죽어도 이 관문이 눈멀었다 (2026-09-16). */
const mapStatus = channelMapStatus();
if (mapStatus.dead) {
  bad(`개명 지도를 읽지 못했습니다(${mapStatus.reason}) — 선언과 수집 기록을 댈 수 없습니다. `
    + '지도가 죽어 있는 동안 권한 판정은 전체 권한이 아닌 접근을 전부 닫습니다');
} else if (!inState.size && withContent.size) {
  bad(`개명 지도가 비어 있는데 아카이브에는 내용이 ${withContent.size}건 있습니다 — `
    + '수집 기록 없이 쌓인 아카이브라 개명되면 되짚을 근거가 없습니다');
} else {
  /* 선언은 사람이 어느 철자로든 적는다 — canonicalChannel 이 `file`·`aka`·현재 이름
   * **철자 전부**를 현재 이름으로 모으므로(2026-09-16 부터 aka 포함), 어느 철자를 적어도
   * 기록에 있는 채널은 여기서 「기록에 있음」으로 잡힌다. 진짜 모르는 이름만 남는다. */
  const unverifiable = priv.filter((p) => !inState.has(canonicalChannel(p)));
  const dangerous = unverifiable.filter((p) => withContent.has(p));
  if (dangerous.length) {
    bad(`수집 기록에 없는데 아카이브에 내용이 있는 비공개 채널 ${dangerous.length}개 — `
      + '개명되면 되짚을 근거가 없어 못 가립니다');
    console.error('    (이름은 적지 않습니다. 그 채널이 `.sync-state.json` 에 있는지 — 즉 수집을 한 번이라도'
      + ' 거쳤는지 — 부터 보세요. 손으로 넣은 md 라면 그 채널을 수집에 태우거나 md 를 빼야 합니다)');
  } else if (unverifiable.length) {
    ok(`비공개 ${priv.length}개 중 기록에 없는 것 ${unverifiable.length}개 — 전부 내용이 없어 가릴 것이 없음`);
  } else {
    ok(`비공개 ${priv.length}개 전부 수집 기록에 있음`);
  }
}

/* ③ 되짚기가 **이 저장소 안에서** 도나 — 가짜 아카이브를 세워 자식 프로세스로 잰다.
 *
 * `config.js` 는 모듈을 읽을 때 환경변수로 아카이브 경로를 굳히므로, 이 프로세스 안에서는
 * 다른 아카이브를 세울 수 없다. 그래서 자식으로 띄운다 — `check-shared-rules.js` 가
 * 파이썬을 띄우는 것과 같은 수법이다. config 철자를 새 이름·옛 이름 **양쪽으로** 한 번씩
 * 돌린다. 이 작업이 만든 성질이 「어느 철자를 적어도 둘 다 산다」라서다. */
console.log('[3/6] 되짚기가 실제로 도나 (가짜 아카이브로 직접 잼)');
const OLD = 'ch-was';         // 비공개 채널의 아카이브 md 이름 (맨 처음 이름)
const MID = 'ch-mid';         // 그 채널의 가운데 이름 — x→y→z 두 번 개명의 y. `aka` 사슬에만 남는다
const NOW = 'ch-now';         // 그 채널의 슬랙 현재 이름 (개명 후)
const SKIP_OLD = 'sk-was';    // **안 다루기로 한** 채널의 아카이브 md 이름 (개명 전)
const SKIP_NOW = 'sk-now';    // 그 채널의 슬랙 현재 이름
const OTHER = 'ch-unrelated'; // 관계 없는 공개 채널 — 넓게 막지 않았나 (수집 기록에는 있다)
const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'src');
/** 화면 문구가 아니라 **실제로 몇 개를 쟀는지** 대보는 기대값 (아래 프로브의 줄 수). */
const PROBE_PROPS = 20;

/* 자식 안에서 돌 코드. 성질마다 참/거짓 하나씩만 내보낸다 — 이름은 안 싣는다.
 *
 * **`archive.js` 까지 부른다.** `config.js` 의 판정만 재면 `withoutSkipped` 의 개명
 * 되짚기가 안 걸린다 — 실제로 그 되짚기를 되돌려 놓고 이 저장소 검사 109개를 전부
 * 돌렸더니 **하나도 안 빨개졌다**(2026-09-16 회의적 검토). 그래서 아카이브를 읽는
 * 자리(`listReadableChannels`)까지 함께 잰다. fixture 의 md 파일들이 그래서 있다. */
const probeSource = `(async () => {
  const { pathToFileURL } = require('node:url');
  const url = (f) => pathToFileURL(${JSON.stringify(SRC)} + '/' + f).href;
  const cfg = await import(url('config.js'));
  const arch = await import(url('archive.js'));
  const OLD = ${JSON.stringify(OLD)}, NOW = ${JSON.stringify(NOW)}, OTHER = ${JSON.stringify(OTHER)};
  const MID = ${JSON.stringify(MID)};
  const SKIP_OLD = ${JSON.stringify(SKIP_OLD)};
  const skipSpelled = cfg.config.digest.skipChannels[0];
  const pub = cfg.PUBLIC_ACCESS, member = cfg.accessFor([NOW]);
  const text = ['| ' + OLD + ' | a |', '| ' + MID + ' | m |', '| ' + NOW + ' | b |', '| ' + OTHER + ' | c |'].join('\\n');
  const out = cfg.redactPrivateMentions(text, pub);
  const readable = arch.listReadableChannels();
  process.stdout.write(JSON.stringify({
    '아카이브 이름이 현재 이름으로 되짚어진다': cfg.canonicalChannel(OLD) === NOW,
    '가운데 이름(aka)이 현재 이름으로 되짚어진다': cfg.canonicalChannel(MID) === NOW,
    '옛 이름이 비공개로 잡힌다': cfg.isPrivateChannel(OLD) === true,
    '가운데 이름이 비공개로 잡힌다': cfg.isPrivateChannel(MID) === true,
    '새 이름이 비공개로 잡힌다': cfg.isPrivateChannel(NOW) === true,
    '공개 권한에 옛 이름이 안 열린다': cfg.canSee(pub, OLD) === false,
    '공개 권한에 가운데 이름이 안 열린다': cfg.canSee(pub, MID) === false,
    '공개 권한에 새 이름이 안 열린다': cfg.canSee(pub, NOW) === false,
    '멤버가 옛 이름 자료를 읽는다': cfg.canSee(member, OLD) === true,
    '멤버가 가운데 이름 자료를 읽는다': cfg.canSee(member, MID) === true,
    '멤버가 자기 채널을 읽는다': cfg.canSee(member, NOW) === true,
    '옛 이름이 적힌 줄이 지워진다': !out.includes(OLD),
    '가운데 이름이 적힌 줄이 지워진다': !out.includes(MID),
    '새 이름이 적힌 줄이 지워진다': !out.includes(NOW),
    '관계 없는 줄은 남는다': out.includes(OTHER),
    '관계 없는 채널은 공개다': cfg.canSee(pub, OTHER) === true,
    '안 다루기로 한 채널이 되짚어져 걸러진다': arch.withoutSkipped([SKIP_OLD], [skipSpelled]).length === 0,
    '안 다루기로 한 채널이 읽기 목록에서 빠진다': !readable.includes(SKIP_OLD),
    '관계 없는 채널은 읽기 목록에 남는다': readable.includes(OTHER),
    '비공개 채널은 읽기 목록에 남는다': readable.includes(OLD),
  }));
})();`;

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-alias-'));
try {
  const write = (rel, text) => {
    const dest = path.resolve(tmp, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, text, 'utf8');
  };
  write('slack-export/index.md', '# fixture\n');
  write(`slack-export/channels/${OLD}.md`, `# ${OLD}\n\n## 2026-01\n\nalpha\n`);
  write(`slack-export/channels/${SKIP_OLD}.md`, `# ${SKIP_OLD}\n\n## 2026-01\n\ngamma\n`);
  write(`slack-export/channels/${OTHER}.md`, `# ${OTHER}\n\n## 2026-01\n\nbeta\n`);
  /* OTHER 도 기록에 넣는다 — 실물 `.sync-state.json` 은 수집한 채널 전부의 줄을 가지며,
   * 기록에 없는 이름은 이제 「고아」로 닫힌다 ([6/6] 이 그 성질을 따로 잰다). */
  write('slack-export/.sync-state.json', JSON.stringify({
    channels: {
      C1: { name: NOW, file: OLD, aka: [MID] },
      C2: { name: SKIP_NOW, file: SKIP_OLD },
      C3: { name: OTHER, file: OTHER },
    },
  }));
  write('documents/index.md', '# fixture\n');

  let broken = 0;
  // 두 목록에 **같은 쪽 철자**를 적는다 — 사람이 개명 보고를 보고 한꺼번에 고치는 모양이다.
  // 셋째 회차는 가운데 이름(aka) 철자다 — y 시절에 적어 둔 config 줄도 살아야 한다.
  for (const [label, spelled, skipSpelled] of [
    ['새 이름', NOW, SKIP_NOW], ['옛 이름', OLD, SKIP_OLD], ['가운데 이름', MID, SKIP_NOW],
  ]) {
    write('config.json', JSON.stringify({
      timezone: 'UTC', workspace: 'fixture', archivePath: 'slack-export', documentsPath: 'documents',
      privateChannels: [spelled], digest: { skipChannels: [skipSpelled] },
      owner: { slackUserId: 'FIXTURE', name: 'Fixture' }, limits: {},
    }));
    let got;
    try {
      got = JSON.parse(execFileSync(process.execPath, ['-e', probeSource], {
        encoding: 'utf-8',
        env: { ...process.env, HERMES_DATA_ROOT: tmp, HERMES_DOCS_DIR: path.join(tmp, 'documents') },
      }));
    } catch (e) {
      bad(`config 에 ${label}을 적은 경우를 재지 못했습니다: ${e.message.split('\n')[0]}`);
      broken += 1;
      continue;
    }
    /* **몇 개를 쟀는지부터 센다.** 화면 문구의 개수를 손으로 적어 두면, 프로브가 줄어들어도
     * 「전부 산다」가 그대로 초록으로 나간다 — 이 저장소가 「못 잼」 표시까지 만들어 막으려던
     * 그 모양이다 (2026-09-16 회의적 검토에서 실측: 성질을 지워도·빈 객체를 내보내도 초록). */
    if (Object.keys(got).length !== PROBE_PROPS) {
      broken += 1;
      bad(`config 에 ${label}을 적은 회차가 성질 ${Object.keys(got).length}개만 쟀습니다 `
        + `(${PROBE_PROPS}개여야 합니다) — 프로브가 줄었거나 중간에 죽었습니다`);
      continue;
    }
    const wrong = Object.entries(got).filter(([, v]) => v !== true).map(([k]) => k);
    if (wrong.length) {
      broken += 1;
      bad(`config 에 ${label}을 적으면 ${wrong.length}개가 깨집니다: ${wrong.join(' · ')}`);
    }
  }
  if (!broken) ok(`config 에 새 이름·옛 이름·가운데 이름 어느 쪽을 적어도 ${PROBE_PROPS}개 성질이 전부 산다 (세 회차)`);
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}

/* ④ 개명으로 **비워진 옛 이름을 지금 다른 채널이 다시 쓰고 있나.**
 *
 * 되짚기는 이름을 열쇠로 쓰므로, 그런 채널이 생기면 두 채널이 한 이름으로 접힌다 —
 * **권한이 서로 열린다.** 공개 채널이 옛 이름을 물려받으면 그 채널 질문자가 비공개
 * 본문을 받고, 비공개끼리면 서로의 자료가 열린다 (2026-09-16 전체 검토에서 재현).
 *
 * **WHK 판정 2026-09-16: 「그대로 두고 감지기를 단다」.** 고치는 쪽이 더 위험해서다 —
 * 그 줄을 지도에서 빼면 **그 채널의 옛 이름 아카이브가 공개로 풀린다.** 제대로 닫으려면
 * 이름 대신 채널 ID 로 갈아타야 하고 그것은 별건 설계다(설계서 11장이 일부러 뺐다).
 * 그래서 고치는 대신 **그 상황이 생기는 순간 빨갛게** 만든다. 지금은 0건이다.
 *
 * **한계 — 수집 기록에 없는 재사용은 여기서 못 본다.** `aliases`(file·aka)와 `inState`
 * (현재 이름) 둘 다 `.sync-state.json` 에서 오므로, 비워진 옛 이름을 **한 번도 수집되지
 * 않은 새 채널**이 물려받으면 이 대조에 안 잡힌다. 다만 그 채널의 아카이브 읽기는
 * canSee 의 고아 판정이 닫고([6/6]), 수집이 그 채널을 처음 태우는 순간 inState 에 올라
 * 여기서 빨갛게 잡힌다 — 눈머는 기간은 「재사용 개설 ~ 첫 수집」 사이뿐이다. */
console.log('[4/6] 개명으로 비워진 옛 이름을 다른 채널이 다시 쓰고 있나');
const recycled = [...aliases.keys()].filter((was) => inState.has(was));
if (recycled.length) {
  bad(`개명 전 이름을 지금 다른 채널이 다시 쓰고 있습니다 ${recycled.length}개 — `
    + '두 채널이 한 이름으로 접혀 권한이 서로 열립니다');
  console.error('    (이름은 적지 않습니다. 그 채널의 이름을 바꾸거나, 채널 ID 로 갈아타는 설계가 필요합니다.'
    + ' 판정 근거는 sdd 원장의 park 항목에 있습니다)');
} else {
  ok(`개명 기록 ${aliases.size}개 — 옛 이름을 다시 쓰는 채널 없음`);
}

/* ── [5/6]·[6/6] 도 가짜 아카이브 + 자식 프로세스로 잰다 ([3/6]과 같은 수법·같은 이유) ──
 * 프로브마다 성질 개수를 **독립된 기대값과 먼저 댄다** — PROBE_PROPS 와 같은 수법. */
function runProbe(label, files, configJson, probeBody, expectedProps) {
  const tmp2 = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-alias-'));
  try {
    for (const [rel, text] of files) {
      const dest = path.resolve(tmp2, rel);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, text, 'utf8');
    }
    fs.writeFileSync(path.join(tmp2, 'config.json'), JSON.stringify(configJson), 'utf8');
    const source = `(async () => {
      const { pathToFileURL } = require('node:url');
      const cfg = await import(pathToFileURL(${JSON.stringify(SRC)} + '/config.js').href);
      ${probeBody}
    })();`;
    let got;
    try {
      got = JSON.parse(execFileSync(process.execPath, ['-e', source], {
        encoding: 'utf-8',
        env: { ...process.env, HERMES_DATA_ROOT: tmp2, HERMES_DOCS_DIR: path.join(tmp2, 'documents') },
      }));
    } catch (e) {
      bad(`${label} 를 재지 못했습니다: ${e.message.split('\n')[0]}`);
      return;
    }
    if (Object.keys(got).length !== expectedProps) {
      bad(`${label} 가 성질 ${Object.keys(got).length}개만 쟀습니다 (${expectedProps}개여야 합니다)`);
      return;
    }
    const wrong = Object.entries(got).filter(([, v]) => v !== true).map(([k]) => k);
    if (wrong.length) bad(`${label} 에서 ${wrong.length}개가 깨집니다: ${wrong.join(' · ')}`);
    else ok(`${label} — ${expectedProps}개 성질이 전부 산다`);
  } finally {
    fs.rmSync(tmp2, { recursive: true, force: true });
  }
}

const FIXTURE_CFG = {
  timezone: 'UTC', workspace: 'fixture', archivePath: 'slack-export', documentsPath: 'documents',
  privateChannels: [NOW], digest: { skipChannels: [SKIP_NOW] },
  owner: { slackUserId: 'FIXTURE', name: 'Fixture' }, limits: {},
};
const MD = (name) => [`slack-export/channels/${name}.md`, `# ${name}\n\n## 2026-01\n\nalpha\n`];

/* 지도가 죽으면(파일 없음·JSON 깨짐) 판정이 **닫혀야** 한다. 예전에는 빈 지도로 물러서
 * 비공개 채널의 옛 이름이 공개로 판정됐다 — 이 저장소가 이번에 고친 1급 결함이다.
 * 신규 설치(아카이브 md 없음)는 보호할 옛 이름이 없으니 닫히면 안 된다. */
console.log('[5/6] 지도가 죽으면 닫히나 · 신규 설치는 안 닫히나');
runProbe('지도 없음 + 아카이브 있음 (죽음)',
  [['slack-export/index.md', '# fixture\n'], MD(OLD), MD(OTHER), ['documents/index.md', '# fixture\n']],
  FIXTURE_CFG, `process.stdout.write(JSON.stringify({
    '지도 상태가 죽음으로 잡힌다': cfg.channelMapStatus().dead === true,
    '옛 이름 md 가 비공개로 잡힌다': cfg.isPrivateChannel(${JSON.stringify(OLD)}) === true,
    '공개 권한이 전부 닫힌다': cfg.canSee(cfg.PUBLIC_ACCESS, ${JSON.stringify(OTHER)}) === false,
    '멤버 권한도 닫힌다': cfg.canSee(cfg.accessFor([${JSON.stringify(NOW)}]), ${JSON.stringify(NOW)}) === false,
    '전체 권한은 산다': cfg.canSee(cfg.FULL_ACCESS, ${JSON.stringify(OLD)}) === true,
  }));`, 5);
runProbe('지도 JSON 깨짐 (죽음)',
  [['slack-export/index.md', '# fixture\n'], MD(OLD),
    ['slack-export/.sync-state.json', 'not json {{{'], ['documents/index.md', '# fixture\n']],
  FIXTURE_CFG, `process.stdout.write(JSON.stringify({
    '지도 상태가 죽음으로 잡힌다': cfg.channelMapStatus().dead === true,
    '옛 이름 md 가 비공개로 잡힌다': cfg.isPrivateChannel(${JSON.stringify(OLD)}) === true,
    '공개 권한이 닫힌다': cfg.canSee(cfg.PUBLIC_ACCESS, ${JSON.stringify(OLD)}) === false,
    '전체 권한은 산다': cfg.canSee(cfg.FULL_ACCESS, ${JSON.stringify(OLD)}) === true,
  }));`, 4);
runProbe('신규 설치 (아카이브 md 없음)',
  [['slack-export/index.md', '# fixture\n'], ['documents/index.md', '# fixture\n']],
  FIXTURE_CFG, `process.stdout.write(JSON.stringify({
    '죽음으로 잡히지 않는다': cfg.channelMapStatus().dead === false,
    '선언된 이름은 그대로 비공개다': cfg.isPrivateChannel(${JSON.stringify(NOW)}) === true,
    '옛 이름은 안 잡힌다 (보호할 아카이브가 없다)': cfg.isPrivateChannel(${JSON.stringify(OLD)}) === false,
    '공개 채널이 닫히지 않는다': cfg.canSee(cfg.PUBLIC_ACCESS, ${JSON.stringify(OTHER)}) === true,
  }));`, 4);

/* 지도가 정상인데 어느 채널 줄에도 없는 이름 = 「비공개 채널의 옛 이름인지 확인 불가」.
 * 전체 권한이 아닌 접근에는 닫는다. config 선언·`_` 가상 이름은 고아가 아니다. */
console.log('[6/6] 수집 기록 어디에도 없는 이름(고아)이 닫히나');
runProbe('고아 이름',
  [['slack-export/index.md', '# fixture\n'], MD(OLD),
    ['slack-export/.sync-state.json', JSON.stringify({
      channels: { C1: { name: NOW, file: OLD }, C3: { name: OTHER, file: OTHER } },
    })], ['documents/index.md', '# fixture\n']],
  FIXTURE_CFG, `process.stdout.write(JSON.stringify({
    '기록에 없는 이름이 공개 권한에 닫힌다': cfg.canSee(cfg.PUBLIC_ACCESS, 'ch-ghost') === false,
    '전체 권한은 산다': cfg.canSee(cfg.FULL_ACCESS, 'ch-ghost') === true,
    '기록에 있는 공개 채널은 열린다': cfg.canSee(cfg.PUBLIC_ACCESS, ${JSON.stringify(OTHER)}) === true,
    'skipChannels 선언은 고아가 아니다': cfg.canSee(cfg.PUBLIC_ACCESS, ${JSON.stringify(SKIP_NOW)}) === true,
    '밑줄 가상 이름은 고아가 아니다': cfg.canSee(cfg.PUBLIC_ACCESS, '_승인자료') === true,
  }));`, 5);

process.exit(failed ? 1 : 0);
