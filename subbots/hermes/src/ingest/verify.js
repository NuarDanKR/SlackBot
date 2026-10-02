/**
 * 커밋 전 관문. **하나라도 실패하면 커밋하지 않는다.**
 *
 * 아카이브 포맷은 계약이고, 깨져도 **에러가 나지 않는다** — 검색이 조용히 0건이 되고
 * 봇은 "아카이브에 없습니다"라고 정상적으로 답한다. 그래서 "넣었다"로 끝내면 안 되고
 * **"찾아진다"까지** 봐야 한다. 마지막 검사가 그것이다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { CHANNELS_DIR, DOCS_DIR, FULL_ACCESS } from '../config.js';
import { splitMessages } from '../archive.js';
import { BOT_AUTHOR_LABEL, BOT_BLOCK_BODY } from '../slack-live.js';
import { searchDocuments } from '../documents.js';
import { SKILL_SCRIPTS, runScript, DATA_ROOT, readJson } from './util.js';

/**
 * 슬랙 아닌 출처가 섞였는지.
 *
 * Hermes 의 근거는 슬랙으로만 한정한다(WHK 결정, 2026-08-03). 슬랙 파일은 채널이 곧 사업장이고
 * 채널의 비공개 여부가 곧 문서의 공개 여부라, 분류와 공개 판정이 **추측이 아니라 사실**이 된다.
 * 다른 출처가 들어오면 그 근거가 사라지고 그때 사람이 적은 `열람:` 한 줄만 남는다.
 *
 * `.doc-state.json` 의 `local_files` 가 그 흔적이다. 비어 있어야 한다.
 */
export function checkNonSlackSources() {
  if (!DOCS_DIR) return [];
  const state = readJson(path.join(DOCS_DIR, '.doc-state.json'), {});
  return Object.values(state?.local_files || {}).map(
    (v) => `슬랙 아닌 출처: ${v.path || '(경로 미상)'} → ${v.doc || '(문서 미상)'}`,
  );
}

const execFileP = promisify(execFile);

/* 아카이브에 봇이 **작성자로** 등장하는 줄. 메시지 헤더와 스레드 답글 줄 두 모양이다.
 * `> 💬 스레드 N건 (봇 답변 — 미수록)` 은 건수 표시라 여기 안 걸린다 — 걸리면 안 된다. */
const SELF_HEADER_RE = new RegExp(`^\\*\\*\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2} · ${BOT_AUTHOR_LABEL}\\*\\*`);
const SELF_REPLY_RE = new RegExp(`^>\\s*\\*\\*└\\s*\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2} · ${BOT_AUTHOR_LABEL}\\*\\*`);

/* 봇 이름이 그대로 적힌 줄 — `**… · 주간 체크인 (봇)**` · `> **└ … · Hermes**` 같은 모양.
 * 2026-08-12 이전 아카이브에는 다른 봇의 글이 본문째 들어가 있었고(주간 체크인 9건 등),
 * 그때는 그게 정상이었다. 이제는 어느 봇이든 본문이 들어가면 안 되므로 **예외 없이 실패**다.
 * 위 자리표시 판정과 달리 여기는 봇 이름을 모르니, `(봇)` 꼬리와 `Hermes` 두 가지로 잡는다.
 * (자리표시 헤더는 이름 없이 `· 봇` 하나뿐이라 이 정규식에 안 걸린다.) */
const NAMED_BOT_RE = new RegExp(
  '^(?:>\\s*\\*\\*└\\s*|\\*\\*)\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2} · (?:.+ \\(봇\\)|Hermes)\\*\\*',
);

/* 스레드가 시작되는 줄. 여기서부터 아래는 **사람이 쓴 답글 본문**이라 무엇이 적혀 있어도 된다
 * (여러 줄짜리 답글의 이어지는 줄은 `>` 로 시작하지도 않는다). 봇의 문장이 들어올 수 있는
 * 자리는 헤더와 이 줄 **사이**뿐이고, 검사도 거기만 본다. */
const THREAD_START_RE = /^>\s*(💬|\*\*└)/;

/**
 * 봇이 쓴 글이 아카이브에 들어갔는지 — **넘지 않는 선**.
 *
 * Hermes 의 답변·요약은 아카이브를 읽어 만든 2차 가공물이다. 그게 아카이브에 들어가면 다음번에
 * 봇이 자기 요약을 원본 근거로 삼고, 한 번 잘못 요약한 숫자가 원문처럼 굳는다. 되돌릴 방법도
 * 없다 — 그 시점부터의 모든 답변이 그 숫자를 인용한다. **다른 봇의 글도 함께 막는다**
 * (WHK 결정 2026-08-12) — 전에는 `주간 체크인` 같은 알림 봇의 글을 원문이라며 넣었다.
 *
 * 규칙을 문서로만 두면 코드를 고칠 때마다 다시 지켜야 한다. 여기서 기계가 지킨다.
 *
 * **딱 하나 허용되는 것이 자리표시 블록이다** (2026-08-09). 봇이 올린 글에 달린 사람 답글을
 * 담을 자리라, **헤더와 스레드 시작 줄 사이**가 `BOT_BLOCK_BODY` 표식 한 줄
 * (뒤에 원문 링크가 붙는 것까지)일 때만 통과시킨다. 봇의 문장이 들어올 수 있는 자리가 정확히
 * 거기다. 그 아래는 사람이 쓴 답글
 * 본문이라 무엇이 적혀 있어도 되고, 봐서도 안 된다 — 여러 줄짜리 답글의 이어지는 줄은 `>` 로
 * 시작하지도 않아서, 거기까지 모양을 따지면 멀쩡한 정정이 관문에 걸려 반영이 통째로 되돌아간다.
 *
 * 그래도 이 검사는 예전보다 **엄격하다** — 줄 하나만 보던 것이 이제 블록의 본문 자리가 비어
 * 있는지를 본다.
 */
export function checkNoSelfAuthored() {
  const bad = [];
  if (!fs.existsSync(CHANNELS_DIR)) return bad;
  for (const f of fs.readdirSync(CHANNELS_DIR).filter((x) => x.endsWith('.md'))) {
    const text = fs.readFileSync(path.join(CHANNELS_DIR, f), 'utf8');

    text.split(/\r?\n/).forEach((line, i) => {
      // 답글 자리에 봇이 등장하는 것은 예외 없이 실패다 (봇 답변 본문 금지, 예전 그대로)
      if (SELF_REPLY_RE.test(line)) bad.push(`${f}:${i + 1} → ${line.trim().slice(0, 80)}`);
      // 봇 이름이 그대로 적힌 헤더·답글도 실패다 — 자리표시는 이름 없이 `· 봇` 하나뿐이다
      else if (NAMED_BOT_RE.test(line)) bad.push(`${f}:${i + 1} → 봇 글: ${line.trim().slice(0, 80)}`);
    });

    for (const b of splitMessages(text)) {
      const lines = b.text.split(/\r?\n/);
      if (!SELF_HEADER_RE.test(lines[0])) continue;

      const rest = lines.slice(1);
      const at = rest.findIndex((l) => THREAD_START_RE.test(l));
      // 스레드가 없으면 담을 것이 없었다는 뜻이다 — 자리표시를 만들 이유가 없다
      if (at === -1) {
        bad.push(`${f} → 스레드 없는 봇 자리표시 블록: ${lines[0].trim()}`);
        continue;
      }
      const body = rest.slice(0, at).filter((l) => l.trim());
      /* 본문 자리는 표식 한 줄, 뒤에 원문 링크가 붙어도 된다 — 그 둘 말고는 무엇도 안 된다.
       * 정규식 대신 조각으로 나눠 본다: `BOT_BLOCK_BODY` 자체에 괄호가 들어 있어 정규식으로
       * 쓰려면 이스케이프가 필요하고, 이스케이프를 빠뜨리면 **검사가 조용히 헐거워진다.** */
      const parts = (body[0] || '').split(' · ');
      const shape =
        body.length === 1
        && parts[0] === BOT_BLOCK_BODY
        && parts.length <= 2
        && (parts.length === 1 || /^\[원문\]\(https:\/\/[^\s()]+\)$/.test(parts[1]));
      if (!shape) {
        bad.push(`${f} → 봇 블록에 본문이 있음: ${lines[0].trim()} / ${body.join(' ').slice(0, 60)}`);
        continue;
      }
      // 사람 답글 없이 봇 답변 건수만 있는 자리표시도 만들 이유가 없다
      if (!rest.slice(at).some((l) => /^>\s*\*\*└/.test(l))) {
        bad.push(`${f} → 답글 없는 봇 자리표시 블록: ${lines[0].trim()}`);
      }
    }
  }
  return bad;
}

/** 메시지 블록 안에 있어도 정상인 헤딩 — 월 헤딩과 참여 기록. */
const OK_HEADING = /^##\s+(\d{4}-\d{2}\s*$|참여 기록)/;

/**
 * 요약 섹션이 메시지 블록으로 딸려 들어갔는지.
 *
 * 2026-08-02 에 실제로 났다 — 새 월 헤딩이 상단 요약 위에 삽입되는 바람에 `## 자금 구조 요약`
 * 표가 첫 메시지 헤더 아래로 밀렸고, 봇이 그 표를 "2026-08-02 WHK 발언"으로 인용했다.
 *
 * 월 헤딩과 `## 참여 기록` 이 블록에 딸려 오는 것은 **정상**이다. splitMessages 는 메시지
 * 헤더에서만 자르므로 각 달 마지막 메시지가 다음 헤딩을 품는다. 이 둘을 안 거르면
 * 45개 채널 전부가 오탐한다(2026-08-03 에 실제로 그랬다).
 */
export function checkSummaryLeak() {
  const bad = [];
  if (!fs.existsSync(CHANNELS_DIR)) return bad;
  for (const f of fs.readdirSync(CHANNELS_DIR).filter((x) => x.endsWith('.md'))) {
    const text = fs.readFileSync(path.join(CHANNELS_DIR, f), 'utf8');
    for (const b of splitMessages(text)) {
      const heads = b.text
        .split('\n')
        .filter((l) => l.startsWith('## ') && !OK_HEADING.test(l));
      if (heads.length) bad.push(`${f} → ${heads[0].trim()}`);
    }
  }
  return bad;
}

/**
 * 문서 트리에 md 아닌 원본이 섞여 들어갔는지.
 *
 * `core.quotepath=false` 를 빠뜨리면 안 된다. git 이 한글 경로를 `"\354\262\255…"` 로 감싸
 * 출력해서 끝이 `.md"` 가 되고, 그러면 멀쩡한 md 가 전부 바이너리로 오탐된다.
 * 이 아카이브는 파일명이 거의 다 한글이다.
 */
export async function checkBinaryLeak() {
  if (!DOCS_DIR) return [];
  const rel = path.relative(DATA_ROOT, DOCS_DIR).split(path.sep).join('/');
  const { stdout } = await execFileP(
    'git',
    ['-c', 'core.quotepath=false', 'ls-files', '--', rel],
    { cwd: DATA_ROOT, maxBuffer: 32 * 1024 * 1024, encoding: 'utf8' },
  );
  return stdout
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .filter((l) => !l.endsWith('.md') && !l.endsWith('.doc-state.json'));
}

/**
 * 이번에 넣은 것이 봇에게 **한 건으로 보이는지**.
 *
 * 검색어로 확인하지 않는다 — 토큰화에 좌우되어 통과·실패가 흔들린다. 대신 `splitMessages` 가
 * 그 메시지를 블록 하나로 잘라내는지 직접 본다. 포맷 계약이 깨지는 방식이 정확히 여기라서,
 * 이 검사만이 "넣었다"가 아니라 "찾아진다"를 보증한다.
 *
 * @param {Array<{file:string, header:string}>} channels  header 예: '**2026-08-04 09:12 · 박민수**'
 * @param {Array<{file:string, header:string, reply:string}>} threadReplies
 *   나중에 덧붙인 답글. 부모 블록 **안에** 있어야 한다 — 밖으로 새면 봇이 그 정정을
 *   부모와 무관한 남의 발언으로 읽고, 원문에 걸린 검색에 정정이 안 딸려 온다.
 */
/**
 * 프로브와 견줄 판으로 채널 md 를 읽는다 — **줄 끝을 LF 로 맞춘 뒤에.**
 *
 * 프로브의 `reply` 는 `slack-archive.js` 의 `renderReply` 가 `'\n> '` 로 이어 만든 값인데,
 * 작업 트리의 채널 md 는 윈도우에서 클론하면 CRLF 다(2026-09-03 실측: 채널 42개 전부).
 * 맞춰 놓지 않으면 **여러 줄 답글은 원리상 절대 안 맞는다** — md 안에는 `\r\n> ` 이고
 * 프로브는 `\n> ` 이라서다. 그러면 관문이 「부모 블록 밖에 있음」으로 실패하고
 * `index.js` 의 `rollback` 이 그날 반영을 통째로 되돌리는데, `.sync-state.json` 도 함께
 * 돌아가므로 **다음 회차가 같은 답글을 다시 잡아 같은 자리에서 또 실패한다.**
 * (실측: 답글 436건 중 여러 줄이 72건.)
 *
 * 같은 함정을 `slack-archive.js` 의 `parseBlocks` 는 `split(/\r?\n/)` 로 이미 피하고 있고,
 * 읽는 자리 전체의 규칙은 `archive.js` 의 `readCached`(+`scripts/check-line-endings.js`)다.
 * 여기서 `readCached` 를 안 쓰는 것은 **관문은 캐시가 아니라 지금 디스크에 있는 것**을
 * 봐야 하기 때문이다 — 같은 회차 안에서 md 를 쓰고 곧바로 재는 자리다.
 */
function readForProbe(p) {
  return fs.readFileSync(p, 'utf8').replace(/\r\n/g, '\n');
}

export function checkInserted({ channels = [], documents = [], threadReplies = [] }) {
  const misses = [];

  for (const probe of channels) {
    const p = path.join(CHANNELS_DIR, `${probe.file}.md`);
    if (!fs.existsSync(p)) {
      misses.push(`대화 ${probe.file}.md — 파일이 없음`);
      continue;
    }
    const blocks = splitMessages(readForProbe(p));
    const hit = blocks.some((b) => b.text.trimStart().startsWith(probe.header));
    if (!hit) misses.push(`대화 ${probe.file}.md — 넣은 메시지가 한 건으로 안 잘림 (${probe.header})`);
  }

  for (const probe of threadReplies) {
    const p = path.join(CHANNELS_DIR, `${probe.file}.md`);
    if (!fs.existsSync(p)) {
      misses.push(`답글 ${probe.file}.md — 파일이 없음`);
      continue;
    }
    const parent = splitMessages(readForProbe(p))
      .find((b) => b.text.trimStart().startsWith(probe.header));
    if (!parent) {
      misses.push(`답글 ${probe.file}.md — 부모 블록이 안 잡힘 (${probe.header})`);
    } else if (!parent.text.includes(probe.reply)) {
      misses.push(`답글 ${probe.file}.md — 덧붙인 답글이 부모 블록 밖에 있음 (${probe.header})`);
    }
  }

  for (const probe of documents) {
    // searchDocuments 는 `{hits, note}` 객체를 돌려준다. 예전에는 그것을 배열로 읽어
    // `hits.length` 가 늘 undefined 였고, 문서 프로브를 넘기는 순간 **관문이 매일 실패**해
    // 자동 반영이 되돌아갈 상태였다 (프로브를 아직 안 넘겨서 안 터졌을 뿐이다).
    const { hits, partial } = searchDocuments({ query: probe.query, project: probe.project, access: FULL_ACCESS, maxHits: 5 });
    // partial 이면 "낱말이 모두 든 회차" 가 아니라 다른 문서에서 일부만 겹친 것이다 —
    // 넣은 것 자체는 여전히 못 찾은 것이므로 실패로 본다.
    if (!hits.length || partial) misses.push(`문서 ${probe.project} — "${probe.query}" 가 검색되지 않음`);
  }

  return misses;
}

/**
 * 전체 관문.
 * @returns {Promise<{passed:boolean, failures:Array<{check:string, detail:string[]}>}>}
 */
export async function runGate({ probes = {} } = {}) {
  const failures = [];

  const leak = checkSummaryLeak();
  if (leak.length) failures.push({ check: '요약이 메시지 블록에 섞임', detail: leak });

  const self = checkNoSelfAuthored();
  if (self.length) failures.push({ check: '봇이 쓴 글이 아카이브에 들어감', detail: self });

  const bin = await checkBinaryLeak();
  if (bin.length) failures.push({ check: '문서 트리에 원본 파일 혼입', detail: bin });

  const nonSlack = checkNonSlackSources();
  if (nonSlack.length) failures.push({ check: '슬랙 아닌 출처가 섞임', detail: nonSlack });

  if (DOCS_DIR && fs.existsSync(DOCS_DIR)) {
    const r = await runScript(SKILL_SCRIPTS.verifyFormat, ['--all']);
    if (!r.ok) {
      failures.push({
        check: '문서 포맷 계약 (verify_format.py)',
        detail: (r.stdout + '\n' + r.stderr).split('\n').filter(Boolean).slice(0, 20),
      });
    }
  }

  const misses = checkInserted(probes);
  if (misses.length) failures.push({ check: '넣은 것이 봇에게 안 보임', detail: misses });

  return { passed: failures.length === 0, failures };
}
