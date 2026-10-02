import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

dotenv.config({ path: path.join(ROOT, '.env'), quiet: true });

/**
 * 자료 저장소 뿌리 — slack-export·documents·config.json 이 있는 곳.
 *
 * **config.json 이 아니라 .env 에서 온다.** config.json 자체가 자료 저장소 안에 있어서,
 * 그 파일이 자료 저장소의 위치를 알려줄 수는 없다 (순환). 같은 방식의 전례가
 * 아래 HERMES_DOCS_DIR 다.
 *
 * 환경변수가 .env 를 이긴다 — VM 은 systemd 가 환경변수를 주고 PC 는 .env 만 있다.
 * (dotenv 는 이미 있는 환경변수를 덮지 않으므로 이 한 줄로 그 순서가 된다.)
 * 스킬 쪽의 같은 값은 .claude/skills/_shared/paths.py 의 DATA_ROOT 이고,
 * 둘이 같은 곳을 가리키는지는 scripts/check-roots.js 가 본다.
 */
export const DATA_ROOT = process.env.HERMES_DATA_ROOT
  ? path.resolve(process.env.HERMES_DATA_ROOT)
  : path.resolve(ROOT, '..', 'hermes-archive');

/**
 * config.json 을 읽는다. **못 읽으면 무엇을 해야 하는지 말하고 멈춘다.**
 *
 * 여기서 막히는 사람은 대개 처음 까는 사람이다. 우리 기계에는 파일이 이미 있어서
 * 우리는 이 자리를 영영 안 밟는다. 2026-08-31 전수감사 전까지 이 줄은 그냥
 * `readFileSync` 였고, 그러면 첫 화면이 `Error: ENOENT ... at Object.readFileSync`
 * 스택 트레이스다 — `npm run check` 는 `[1/6]` 한 줄도 못 찍고 죽는다.
 *
 * **원인이 둘인데 증상이 하나다.** ① 자료 저장소는 맞게 잡혔는데 config.json 이 아직
 * 없거나 ② `HERMES_DATA_ROOT` 가 비어 폴백 경로(코드 옆 `hermes-archive`)로 떨어졌거나.
 * 그래서 **어디를 봤는지와 그 값이 어디서 왔는지를 함께** 적는다. 하나만 적으면
 * 폴백으로 떨어진 사람이 없는 폴더에 config.json 을 만들려고 한다.
 */
function loadConfig() {
  const file = path.join(DATA_ROOT, 'config.json');
  let raw;
  try {
    raw = fs.readFileSync(file, 'utf8');
  } catch (err) {
    const fromEnv = !!process.env.HERMES_DATA_ROOT;
    const lines = [
      '',
      '설정 파일을 읽지 못했습니다.',
      '',
      `  찾은 곳 : ${file}`,
      `  그 자리는 ${fromEnv ? 'HERMES_DATA_ROOT 로 지정된 값' : 'HERMES_DATA_ROOT 가 비어 있어 쓰는 폴백(코드 저장소 옆 hermes-archive)'}입니다.`,
      `  사유    : ${err.code === 'ENOENT' ? '그 경로에 파일이 없습니다' : err.message}`,
      '',
      '  둘 중 하나입니다.',
      '',
      '  ① 자료 저장소 자리가 틀렸다',
      '     코드 저장소의 .env 에서 HERMES_DATA_ROOT 를 자료 저장소 뿌리의',
      '     절대경로로 맞추세요. (VM 은 deploy/setup.sh 가 자동으로 적습니다)',
      '',
      '  ② 자료 저장소에 아직 config.json 이 없다',
      '     코드 저장소의 config.example.json 을 그 자리에 config.json 으로',
      '     복사하고 <> 자리를 채우세요. 자료 저장소에 둡니다 — 코드가 아닙니다.',
      '',
    ];
    process.stderr.write(`${lines.join('\n')}\n`);
    process.exit(1);
  }
  try {
    return JSON.parse(raw);
  } catch (err) {
    // 파싱 실패는 위와 원인이 다르다 — 파일은 있는데 JSON 이 깨진 것이다.
    // 같은 안내를 내면 멀쩡한 HERMES_DATA_ROOT 를 고치러 간다.
    process.stderr.write(
      `\n설정 파일이 JSON 이 아닙니다.\n\n  파일 : ${file}\n  사유 : ${err.message}\n\n` +
      '  쉼표나 따옴표가 빠진 자리를 찾으세요. 되돌릴 것이 없으면\n' +
      '  코드 저장소의 config.example.json 에서 다시 시작하면 됩니다.\n\n');
    process.exit(1);
  }
}

export const config = loadConfig();

/** 아카이브 루트 (자료 저장소의 slack-export) 절대경로 */
export const ARCHIVE_DIR = path.resolve(DATA_ROOT, config.archivePath);
export const CHANNELS_DIR = path.join(ARCHIVE_DIR, 'channels');

/**
 * 문서 아카이브 루트 (자료 저장소의 documents). 슬랙 첨부를 md 로 변환해 둔 곳.
 * config.json 에 documentsPath 가 없으면 null 이고, 그러면 문서 검색 도구와
 * 색인이 통째로 꺼진다 — 문제가 생겼을 때 한 줄로 되돌리는 킬 스위치다.
 */
export const DOCS_DIR = process.env.HERMES_DOCS_DIR
  ? path.resolve(process.env.HERMES_DOCS_DIR)
  : config.documentsPath
    ? path.resolve(DATA_ROOT, config.documentsPath)
    : null;
export const DOC_PROJECTS_DIR = DOCS_DIR ? path.join(DOCS_DIR, 'projects') : null;

/* 전사 종합 성격의 문서 폴더 — 사업장-좁힘 검색에서 로컬 확정 0건일 때 카드로 안내한다.
 * 사업장 이름은 코드에 못 쓰므로(check-business-names) 목록은 자료 저장소 config.json 에.
 * 이 목록이 낡는 것이 곧 재발 경로다 — 전사 보고 폴더가 늘면 여기부터 본다. */
export const companyWideDocProjects = config.search?.companyWideDocProjects || [];

/**
 * 대화 로그. 자리가 둘로 나뉜다.
 *
 *   LOG_RAW_DIR — 원본 JSONL. git 밖(.gitignore 에 등록됨). 답변 직후 바로 append 한다.
 *                 **코드 저장소 기준**이다 — VM 이 봇을 돌리는 자리 옆에 쌓인다.
 *   LOG_DIR     — 사람이 읽는 md. **자료 저장소 안.** 매일 07:00 자동 반영이 렌더해서 커밋한다.
 *
 * 나눈 이유: 답변 직후 저장소 안에 바로 쓰면 작업 트리가 더러워지고,
 * 다음 자동 반영의 syncBeforeWork() 가 거기서 멈춰 대화 반영이 통째로 죽는다.
 * config.json 의 log.enabled 를 false 로 두면 기록이 통째로 꺼진다 (킬 스위치).
 */
export const LOG_ENABLED = !!(config.log && config.log.enabled !== false && config.log.path);
export const LOG_DIR = LOG_ENABLED ? path.resolve(DATA_ROOT, config.log.path) : null;
export const LOG_RAW_DIR = LOG_ENABLED ? path.resolve(ROOT, config.log.rawPath || 'logs') : null;

/**
 * 필수 환경변수를 확인한다. 없으면 무엇이 빠졌는지 알려주고 종료.
 * @param {string[]} names
 */
export function requireEnv(names) {
  const missing = names.filter((n) => !process.env[n] || process.env[n].trim() === '');
  if (missing.length) {
    console.error(`\n환경변수가 비어 있습니다: ${missing.join(', ')}`);
    console.error(`  → ${path.join(ROOT, '.env')} 를 확인하세요.`);
    console.error('  → .env 가 없으면: copy .env.example .env\n');
    process.exit(1);
  }
  return Object.fromEntries(names.map((n) => [n, process.env[n]]));
}

/** 채널명이 비공개로 지정되어 있는지. **양쪽을 다 되짚어** 댄다 — 대보는 이름도,
 * config 에 적힌 이름도. 한쪽만 되짚으면 config 에 옛 이름을 적은 경우의 멀쩡하던
 * 가리기까지 죽는다. 지도가 비어도 판정이 옛 코드와 글자 그대로 같아지지는 않는다 —
 * 옛 코드(`config.privateChannels.includes(normalizeChannel(name))`)는 입력만 다듬어
 * 댔다. 판정이 넓어지는 방향으로만 달라진다 — 옛 코드가 못 맞히던 지저분한 줄을
 * 이제 맞힌다. **지도가 죽었으면(아카이브는 있는데 `.sync-state.json` 을 못 읽으면)
 * 모든 이름을 비공개로 답한다** — 비공개 여부를 확정할 수 없어서다 (fail-closed). */
export function isPrivateChannel(name) {
  return isPrivateWith(name, currentChannelNames());
}

/**
 * 채널 이름을 다듬어 비교 가능한 형태로 만든다 — 소문자로, 공백·밑줄·붙임표·`#` 를 지운다.
 * `matchesHiddenPrivate`(아래)와 `redactPrivateMentions` 가 여기서 가져다 쓴다 —
 * **한 곳에서 정의해 여럿이 가져다 쓴다** (WHK 지시 2026-09-03. 전에는 `matchesHiddenPrivate`
 * 안에 지역 함수로 같은 줄이 또 있었고, `redactPrivateMentions` 는 이 다듬기 없이 설정
 * 문자열 그대로 대봐서 사람이 띄어 쓴 이름과 안 맞았다).
 *
 * `archive.js` 의 `export function fold` 도 **글자 그대로 같은 규칙**을 별도로 정의해 두고
 * 있다 — `config.js` 가 `archive.js` 를 import 하면 순환이라 베낀 것이다(`matchesHiddenPrivate`
 * 의 예전 주석과 같은 사정). `archive.js` 는 이 조가 못 고치는 파일이라 그대로 남았다.
 * 자리를 진짜 하나로 모으려면 `archive.js` 도 이 export 를 쓰게 바꿔야 한다.
 */
export function fold(s) {
  return String(s).toLowerCase().replace(/[\s_\-#]/g, '');
}

/** '#사업장나 ' → '사업장나' */
export function normalizeChannel(name) {
  return String(name || '').replace(/^#/, '').trim();
}

/* ── 채널 개명 되짚기 ──────────────────────────────────────────────
 *
 * 채널을 개명해도 대화 md 파일명과 문서 폴더 이름은 **안 바꾼다**
 * (`ingest/slack-archive.js` 의 `fileName`). 그래서 아카이브는 개명 전 이름을, 슬랙은
 * 개명 후 이름을 쓴다. 그 화석이 `.sync-state.json` 의 `channels[<id>].file` 이다.
 *
 * 권한 판정이 이것을 안 보면 `privateChannels` 에 **어느 철자를 적어도 한쪽이 깨진다** —
 * 새 이름이면 옛 이름 폴더의 가리기가 죽고, 옛 이름이면 멤버 권한이 죽는다. 그런데
 * 개명 보고는 **새 이름으로 고치라고** 한다(`ingest/report.js`). 위험한 쪽을 시키고 있었다.
 *
 * 지도를 여기 두는 이유: `config.js` 는 프로젝트 안에서 아무것도 import 하지 않는 뿌리라
 * `archive.js` 에서 못 가져온다. 그렇다고 같은 판정을 여기 새로 적으면 **두 곳으로
 * 갈라진다.** `archive.js` 가 이것을 다시 내보내 쓰던 곳은 그대로 둔다 — 바로 위 `fold`
 * 주석이 「자리를 진짜 하나로 모으려면 archive.js 도 이 export 를 쓰게 바꿔야 한다」고
 * 적어 둔 그 방향이다.
 */

const fileCache = new Map(); // abs path -> { mtimeMs, text }

/** 파일 수정 시각이 그대로면 다시 안 읽는다. 줄 끝은 LF 로 맞춘다. */
export function readCached(absPath) {
  const stat = fs.statSync(absPath);
  const hit = fileCache.get(absPath);
  if (hit && hit.mtimeMs === stat.mtimeMs) return hit.text;
  // 줄 끝을 LF 로 맞춰 담는다. 저장소의 md 는 전부 LF 인데 윈도우에서 클론하면
  // core.autocrlf 가 작업 트리 사본을 CRLF 로 바꿔, 원문을 그대로 프롬프트에 싣는
  // 자리(documents.js 의 missingSection 등)에 `\r` 이 함께 실린다. 자세한 것은
  // scripts/check-line-endings.js.
  const text = fs.readFileSync(absPath, 'utf8').replace(/\r\n/g, '\n');
  fileCache.set(absPath, { mtimeMs: stat.mtimeMs, text });
  return text;
}

/* ── 지도 죽음 = 닫힘 (fail-closed) ─────────────────────────────────
 *
 * 되짚기의 근거는 `.sync-state.json` **하나**다. 그 파일이 없거나(①) JSON 이 깨졌거나(②)
 * `channels` 키가 없거나(③) 읽기가 실패하면(④) 지도가 통째로 빈다. 예전에는 그때 조용히
 * 빈 지도로 물러섰는데, 빈 지도에서는 **비공개 채널의 옛 이름이 공개로 판정된다** —
 * `isPrivateWith` 가 config 철자와만 대보게 되어서다. 파일 하나가 사라지는 것만으로 공개
 * 권한 지점들이 전부 새는 모양이라(2026-09-16 검증), 이제는 「죽음」을 상태로 들고 다니며
 * **전체 권한이 아닌 접근을 닫는다.** 조용히 열리는 것보다 시끄럽게 닫히는 쪽이 맞다.
 *
 * **예외 하나 — 신규 설치.** `slack-export/channels/` 에 대화 md 가 하나도 없으면 보호할
 * 옛 이름 자체가 없다. 그때의 「파일 없음」은 죽음이 아니라 그냥 시작 전이고, 닫으면
 * 첫 화면부터 전부 막혀 새 팀이 빨간 줄을 무시하는 법부터 배운다. */

/** 판정 회차 단위 경고 — 같은 회차(지도 하나로 여러 줄을 거르는 반복)에서는 반복하지 않되,
 * 며칠 떠 있는 봇에서도 계속 보이도록 모듈 1회 플래그 대신 시간으로 조인다. */
const closedWarnedAt = new Map(); // kind -> 마지막으로 알린 시각
function warnClosed(kind, message) {
  const t = Date.now();
  if (t - (closedWarnedAt.get(kind) || 0) < 60 * 1000) return;
  closedWarnedAt.set(kind, t);
  console.warn(message);
}

/** 보호할 아카이브가 실제로 있나 — 「죽음」과 「신규 설치」를 가르는 기준.
 *
 * 대화 md 와 **문서 폴더 둘 다** 본다. 처음에는 대화 md 만 봤는데, 그러면 문서만 있고
 * 대화 md 가 0장인 상태에서 지도가 죽었을 때 「신규 설치」로 새어 문서 폴더가 열린다.
 * 실물에서는 문서가 수집 뒤에만 생겨 그 상태가 잘 안 나오지만, **잘 안 나오는 것과
 * 막혀 있는 것은 다르다** — 여기서 한 줄로 닫는다. */
function archiveHasContent() {
  try {
    if (fs.readdirSync(CHANNELS_DIR).some((f) => f.endsWith('.md'))) return true;
  } catch { /* channels/ 가 없으면 대화 쪽은 비어 있는 것이다 — 문서 쪽을 마저 본다 */ }
  if (!DOC_PROJECTS_DIR) return false; // 문서 기능 자체가 꺼진 설치
  try {
    return fs.readdirSync(DOC_PROJECTS_DIR, { withFileTypes: true }).some((d) => d.isDirectory());
  } catch {
    return false; // 둘 다 없으면 신규 설치다
  }
}

/** 지도를 못 읽었을 때 — 아카이브가 있으면 죽음, 없으면 신규 설치(닫지 않되 알린다). */
function deadOrFresh(reason) {
  if (archiveHasContent()) return { rows: [], dead: true, fresh: false, reason };
  warnClosed('map-broken-fresh',
    `개명 지도를 못 읽었습니다(${reason}) — 아카이브가 비어 있어 닫지는 않습니다.`);
  return { rows: [], dead: false, fresh: true, reason };
}

/** `.sync-state.json` 을 상태와 함께 읽는다. dead 면 rows 는 늘 빈 배열이다. */
function loadSyncState() {
  const p = path.join(ARCHIVE_DIR, '.sync-state.json');
  if (!fs.existsSync(p)) {
    return archiveHasContent()
      ? { rows: [], dead: true, fresh: false, reason: '지도 파일이 없는데 아카이브에는 자료(대화 md 또는 문서 폴더)가 있습니다' }
      : { rows: [], dead: false, fresh: true, reason: null };
  }
  let raw;
  try {
    raw = readCached(p);
  } catch (e) {
    return deadOrFresh(`읽기 실패: ${e.message}`);
  }
  try {
    const parsed = JSON.parse(raw);
    const channels = parsed && typeof parsed === 'object' ? parsed.channels : null;
    // `channels` 키 없음도 죽음이다 — 예전 `.channels || {}` 가 조용히 삼키던 갈래.
    if (!channels || typeof channels !== 'object') return deadOrFresh('channels 키가 없습니다');
    return { rows: Object.values(channels), dead: false, fresh: false, reason: null };
  } catch (e) {
    return deadOrFresh(`JSON 이 아닙니다: ${e.message}`);
  }
}

/** `.sync-state.json` 의 채널 줄들. 없거나 깨졌으면 빈 배열 (상태는 loadSyncState 가 든다). */
function syncStateChannels() {
  return loadSyncState().rows;
}

/**
 * 슬랙의 **현재** 채널 이름 → 아카이브가 그 채널에 쓰는 이름.
 * 수집·위생 점검이 쓴다(`archive-health.js`). `fetch_slack_files.py` 의
 * `archive_channel_names` 와 같은 판정이어야 한다 — `scripts/check-shared-rules.js` 가 본다.
 *
 * @param {Array<{name:string,file?:string}>} [known] 생략하면 `.sync-state.json`
 * @returns {Map<string,string>}
 */
export function archiveChannelNames(known) {
  const rows = known || syncStateChannels();
  const m = new Map();
  for (const c of rows) {
    const now = normalizeChannel(c?.name || '');
    if (!now) continue;
    m.set(now, normalizeChannel(c?.file || now) || now);
  }
  return m;
}

/** 개명을 되짚은 사업장 이름. 지도에 없으면 그대로 — 개명 안 한 채널이 그쪽이다. */
export function archiveChannelOf(name, map) {
  return map.get(name) ?? name;
}

/** 지도 `Map` 에 몰래 얹는 상태 — 죽음 여부와, 고아 판정용 「기록이 아는 철자 전부」.
 * `Map` 의 이터레이션에는 안 잡히므로 기존 소비처(`for...of` · `[...map]`)는 그대로 돈다. */
const MAP_META = Symbol('channelMapMeta');

function buildCurrentChannelNames(rows, state) {
  const m = new Map();
  const known = new Set();
  for (const c of rows) {
    const now = normalizeChannel(c?.name || '');
    if (!now) continue;
    known.add(now);
    /* `file`(맨 처음 이름)과 `aka`(사이 이름들) 전부가 이 채널의 철자다 — x→y→z 로
     * 두 번 개명하면 `file` 은 x 만 들고, y 는 수집이 남긴 `aka` 사슬에만 있다
     * (쓰기 쪽 계약: `channels[<ID>].aka`, `fetch_slack_files.py` 의
     * `current_channel_names` 와 같은 판정 — `check-shared-rules.js` 가 본다). */
    for (const s of [c?.file, ...(Array.isArray(c?.aka) ? c.aka : [])]) {
      const then = normalizeChannel(s || '');
      if (!then) continue;
      known.add(then);
      if (then !== now) m.set(then, now);
    }
  }
  m[MAP_META] = {
    dead: Boolean(state?.dead),
    reason: state?.reason || null,
    /* 고아 판정은 지도가 **정상 적재**됐을 때만 켠다. `known: null` = 끔 —
     * 신규 설치(fresh)와 rows 를 직접 넘긴 호출(옛 계약 그대로)이 그쪽이다. */
    known: state && !state.dead && !state.fresh ? known : null,
    declared: null, // declaredSetFor 가 지도당 한 번 만들어 여기 얹는다
  };
  return m;
}

let currentChannelNamesCache = null; // { mtimeMs, at, map } — known 없이 부를 때만 쓴다

/**
 * **아카이브 이름 → 슬랙의 현재 이름.** 위 지도의 반대 방향이고, 읽기·권한 판정이 쓴다.
 * 개명 안 한 채널은 넣지 않는다 — 지도가 항등이면 줄이 없는 것과 같다.
 *
 * `.sync-state.json` 의 수정 시각(`readCached` 가 쓰는 것과 같은 신호)으로 캐시한다.
 * `canonicalChannel` 을 거쳐 `redactPrivateMentions` 처럼 줄마다 도는 자리까지 이걸
 * 부르므로, 매번 다시 파싱하고 `Map` 을 새로 만들면 비용이 쌓인다. 파일이 그대로면
 * 지난 `Map` 을 그대로 돌려주고, 수정 시각이 바뀌면(봇은 며칠씩 떠 있다) 다시 만든다.
 *
 * **돌려주는 `Map` 을 밖에서 고치지 마라 — 캐시된 그 물건 자체다.** 고치면 다음 무효화까지
 * 권한 판정이 오염된다. 방어 복사는 줄마다 도는 자리라 비용이 되살아나므로 안 한다.
 * 지금 호출부는 전부 읽기만 한다(`.get` · `for...of` · `[...map]`).
 */
export function currentChannelNames(known) {
  if (known) return buildCurrentChannelNames(known);
  const p = path.join(ARCHIVE_DIR, '.sync-state.json');
  let mtimeMs = null;
  try {
    mtimeMs = fs.statSync(p).mtimeMs;
  } catch {
    mtimeMs = null;
  }
  const c = currentChannelNamesCache;
  /* 파일이 없을 때(mtimeMs null)는 60초만 캐시한다 — 그 상태는 「신규 설치」와 「죽음」
   * 사이라, md 가 나중에 생기는 것을 stat 신호 없이도 봐야 한다. 파일이 있으면 예전처럼
   * 수정 시각으로만 무효화한다 (「같은 프로세스 안에서 개명을 본다」는 시험이 지킨다). */
  if (c && c.mtimeMs === mtimeMs && (mtimeMs !== null || Date.now() - c.at < 60 * 1000)) {
    return c.map;
  }
  const state = loadSyncState();
  const map = buildCurrentChannelNames(state.rows, state);
  currentChannelNamesCache = { mtimeMs, at: Date.now(), map };
  return map;
}

/** 지도의 지금 상태 — 관문(`check-channel-aliases.js` [2/6])과 시험이 본다. */
export function channelMapStatus() {
  const meta = currentChannelNames()[MAP_META] || {};
  return { dead: Boolean(meta.dead), fresh: !meta.dead && meta.known === null, reason: meta.reason || null };
}

/**
 * 아래 `*With` 넷은 **지도를 인자로 받는다.**
 *
 * 지도를 뜨는 데 드는 `fs.statSync` 가 이 판정들의 비용 거의 전부다(실측: `canonicalChannel`
 * 5만 회 2,062ms 중 statSync 가 2,000ms). 그런데 `redactPrivateMentions` 는 **줄마다**
 * 불리고(`archive.js` 의 revived. `documents/brief.js` 의 revived 는 같은 이유로
 * 2026-09-16 부터 전체를 권한별 한 번씩만 거른다), 그 안에서 다시
 * 비공개 채널 수만큼 `canSeePrivateChannel`·`channelSpellings` 를 부른다. 함수마다 지도를
 * 새로 뜨면 한 줄에 스무 번 넘게 stat 을 치게 된다 — 검색 한 번이 36ms → 680ms 였다.
 *
 * 그래서 **공개 함수가 지도를 한 번만 뜨고** 그 하나를 이 아래로 넘긴다. 판정 자체는
 * 그대로다(같은 지도, 같은 식). 캐시를 더 오래 붙잡는 방법도 있지만, 그러면 「같은
 * 프로세스 안에서 개명을 본다」가 깨진다 — 그 성질은 시험이 지키고 있다.
 */
function canonWith(name, map) {
  const n = normalizeChannel(name);
  return map.get(n) ?? n;
}

function isPrivateWith(name, map) {
  const target = canonWith(name, map);
  if (!target) return false;
  if (config.privateChannels.some((c) => canonWith(c, map) === target)) return true;
  /* 지도가 죽었으면 어느 이름이 비공개 채널의 옛 이름인지 **확정할 수 없다** —
   * 전부 비공개 취급한다 (아래 canSeePrivateWith 가 전체 권한 아닌 접근을 닫는다). */
  if (map[MAP_META]?.dead) {
    warnClosed('map-dead', `개명 지도를 못 읽었습니다(${map[MAP_META].reason}) — `
      + '비공개 여부를 확정할 수 없어 전체 권한이 아닌 접근을 전부 닫습니다.');
    return true;
  }
  return false;
}

function canSeePrivateWith(access, name, map) {
  if (!access) return false;
  if (access.full) return true;
  // 지도가 죽으면 전체 권한만 산다 — 멤버 권한도 이름 대조가 근거인데 그 근거가 없다.
  if (map[MAP_META]?.dead) return false;
  const target = canonWith(name, map);
  for (const c of access.channels) if (canonWith(c, map) === target) return true;
  return false;
}

/**
 * 지도는 정상인데 **어느 채널 줄에도 없는 이름** — 「비공개 채널의 옛 이름인지 확인 불가」.
 * `canSee` 가 이것도 전체 권한이 아닌 접근에는 닫는다 (2026-09-16 실측으로 지금 이런
 * 채널·문서는 0개다 — 회귀가 아니라 울타리다). 고아가 **아닌** 것:
 *   · 기록의 철자(현재 이름 · `file` · `aka`)와 canon 으로 이어지는 이름
 *   · config 에 선언된 이름 (privateChannels · skipChannels · companyWideDocProjects)
 *   · `_` 로 시작하는 가상 이름 (`_공통` · `_승인자료` — 채널이 아닌 자리라는 표기 규약)
 */
function isOrphanWith(name, map) {
  const meta = map[MAP_META];
  if (!meta || meta.dead || !meta.known) return false; // 죽음은 위에서, 신규 설치는 안 닫는다
  const n = normalizeChannel(name);
  if (!n || n.startsWith('_')) return false;
  const target = canonWith(name, map);
  if (meta.known.has(n) || meta.known.has(target)) return false;
  const declared = declaredSetFor(map, meta);
  return !declared.has(n) && !declared.has(target);
}

/** config 가 선언한 이름들(+canon). 지도당 한 번만 만든다 — canSee 는 줄마다 돈다. */
function declaredSetFor(map, meta) {
  if (meta.declared) return meta.declared;
  const s = new Set();
  for (const c of [
    ...(config.privateChannels || []),
    ...(config.digest?.skipChannels || []),
    ...companyWideDocProjects,
  ]) {
    const n = normalizeChannel(c);
    if (!n) continue;
    s.add(n);
    s.add(canonWith(n, map));
  }
  meta.declared = s;
  return s;
}

function spellingsWith(name, map) {
  const canon = canonWith(name, map);
  const out = new Set([normalizeChannel(name), canon]);
  for (const [archiveName, currentName] of map) {
    if (currentName === canon) out.add(archiveName);
  }
  return [...out].filter(Boolean);
}

/**
 * 권한 판정이 쓰는 한 이름. 지도에 없으면 다듬기만 한 그대로.
 *
 * `map` 을 넘기면 그것을 쓴다 — 여러 이름을 한 번에 되짚는 자리(`archive.js` 의
 * `withoutSkipped`)가 이름마다 지도를 다시 뜨지 않게. 되짚는 **규칙은 한 곳**이다.
 */
export function canonicalChannel(name, map = currentChannelNames()) {
  assertNameMap(map, 'canonicalChannel');
  return canonWith(name, map);
}

/**
 * 넘어온 것이 **지도 모양이기라도** 한지 본다.
 *
 * 이 인자는 틀려도 조용히 판정을 **좁히는** 쪽이라 위험하다 — 빈 `Map` 을 넘기면
 * 되짚기가 꺼진 것과 같아서 옛 이름 줄이 안 지워지는데 에러는 안 난다. 방향을 거꾸로 든
 * `archiveChannelNames()` 도 `Map` 이라 이름이 헷갈리기 쉽다.
 *
 * `instanceof` 로는 그 둘까지 못 가른다(둘 다 진짜 `Map` 이다). 여기서 막는 것은
 * `null`·`{}`·숫자 같은 **모양부터 틀린 것**이고, 그때 `map.get is not a function` 이라는
 * 엉뚱한 자리의 에러 대신 **어느 인자가 틀렸는지**를 말해 준다. 나머지는 호출부가
 * `currentChannelNames()` 를 그대로 넘기는지로만 지켜진다 — 지금은 전부 그렇다.
 */
function assertNameMap(map, who) {
  if (!(map instanceof Map)) {
    throw new TypeError(`${who}: map 인자는 currentChannelNames() 가 내주는 Map 이어야 합니다`);
  }
}

/**
 * 그 채널이 **쓴 적 있는 철자 전부.** 언급 가리기가 쓴다 — 옛 대화 본문에는 옛 이름이
 * 글자로 적혀 있어서, 되짚어 한 이름으로 모으면 그 줄이 안 지워진다.
 *
 * 두 번 개명한 경우의 가운데 이름도 나온다 — 수집이 `channels[<ID>].aka` 에 남기고
 * `buildCurrentChannelNames` 가 그것도 지도에 넣으므로, 여기 반복이 그대로 줍는다.
 * 받은 철자는 늘 넣는다. 기록 어디에도 없는 이름이 와도 최소한 그 철자만큼은 막히고,
 * 그런 이름의 **읽기**는 `canSee` 의 고아 판정이 따로 닫는다.
 */
export function channelSpellings(name) {
  return spellingsWith(name, currentChannelNames());
}

/**
 * 이 채널 이름을 가리키는 설정 목록 이름들 (`['privateChannels', 'skipChannels']`).
 *
 * 개명 보고가 「옛 이름이 설정에 있습니다」를 적을 때 쓴다(`ingest/report.js`). 설정은 채널을
 * 이름으로 가리키는데 거르는 자리는 슬랙의 현재 이름과 대므로, 개명하면 그 줄이 조용히 죽는다.
 *
 * **두 목록을 다 본다.** 한쪽만 돌려주면 두 목록에 다 적힌 채널(`비공개-바` 가 그렇다)에서
 * 사람이 한 줄만 고치고 닫게 되고, 나머지가 조용히 남는다.
 */
export function channelRefsIn(name) {
  const k = normalizeChannel(name);
  const out = [];
  if ((config.privateChannels || []).map(normalizeChannel).includes(k)) out.push('privateChannels');
  if ((config.digest?.skipChannels || []).map(normalizeChannel).includes(k)) out.push('skipChannels');
  return out;
}

/* ── 열람 권한 ────────────────────────────────────────────────────
 * 비공개 채널은 **하나하나 따로** 열린다. 예전에는 allowPrivate 불린 하나였고,
 * 비공개 채널 어디서든 질문이 오면 비공개 채널 **전부**가 열렸다. 멤버가 서로 다르므로
 * 그건 유출이다 — 어느 비공개 채널 멤버가 그 채널에서 물었을 때 **다른** 비공개
 * 채널 내용이 인용됐다 (WHK 지시로 2026-08-05 수정). 그 실물은 자료 저장소
 * `사고기록.md` 의 「비공개 채널이 서로도 막혀야 하는 이유」 절에 있다.
 *
 * 권한은 세 가지 모양뿐이다.
 *   PUBLIC_ACCESS — 공개 채널만. 공개 채널에서 온 질문 (답이 그 채널 전원에게 보인다)
 *   accessFor([…]) — 적어 준 비공개 채널만. 비공개 채널에서 온 질문(그 채널 하나)·DM(그 사람이 멤버인 채널)
 *   FULL_ACCESS   — 전부. 본인 DM 요약과 점검 스크립트처럼 사람이 하나뿐인 자리
 *
 * 권한 인자를 빼먹고 부르면 undefined 가 되는데, 그때는 아무것도 안 열린다 (fail-closed).
 */

/** @param {string[]} channelNames 열어 줄 비공개 채널 이름 */
export function accessFor(channelNames) {
  return Object.freeze({
    full: false,
    channels: new Set((channelNames || []).map(normalizeChannel)),
  });
}

export const PUBLIC_ACCESS = accessFor([]);
export const FULL_ACCESS = Object.freeze({ full: true, channels: new Set() });

/**
 * 사람 글의 스레드에 봇이 단 답글 자리 — 본문 없이 건수만 적는다.
 *
 * 아카이브(`ingest/slack-archive.js`)·스레드 맥락(`fetchThreadContext`, slack-live.js)·
 * 반향 판정(`isEchoEntry`, archive.js)·프롬프트 설명(`claude.js`)이 같은 문자열을 봐야
 * 하므로 여기서 한 번만 정한다.
 *
 * **정본 자리가 slack-live.js 가 아니라 여기인 이유(2026-09-11)**: `isEchoEntry` 가
 * archive.js 에서 이 상수를 써야 하는데, slack-live.js 는 이미 `listAllChannels` 를
 * archive.js 에서 가져다 쓰고 있어(반대 방향) archive.js 가 slack-live.js 를 import 하면
 * 순환이 된다. slack-live.js 는 기존 import 처(`ingest/slack-archive.js` 등)가 안 깨지게
 * 재수출(`export { BOT_ANSWER_MARK } from './config.js'`)만 남긴다.
 */
export const BOT_ANSWER_MARK = '(봇 답변 — 미수록)';

/**
 * **비공개** 채널 하나를 이 권한으로 볼 수 있나.
 * 공개 여부를 여기서 판정하지 않는 이유: 슬랙에서는 비공개인데 config 의 privateChannels 에
 * 안 적힌 채널이 있을 수 있고(CLAUDE.md 에 적힌 어긋남), 그 경우 슬랙의 실제 플래그로
 * 판정해야 한다. 실시간 조회(slack-live.js)가 그래서 이 함수를 직접 쓴다.
 */
export function canSeePrivateChannel(access, name) {
  return canSeePrivateWith(access, name, currentChannelNames());
}

/** 채널(공개·비공개 무엇이든)을 이 권한으로 볼 수 있나.
 * 지도가 죽었거나(isPrivateWith 안) 이름이 고아면(isOrphanWith) 비공개 여부를 확정할 수
 * 없으므로 전체 권한이 아닌 접근에는 닫는다 — fail-closed. */
export function canSee(access, name) {
  const map = currentChannelNames();
  if (isOrphanWith(name, map)) {
    if (access && access.full) return true;
    warnClosed('orphan', '수집 기록 어디에도 없는 채널 이름이 권한 판정에 들어왔습니다 — '
      + '비공개 채널의 옛 이름인지 확인할 수 없어 전체 권한이 아닌 접근을 닫습니다 (이름은 적지 않습니다).');
    return false;
  }
  if (!isPrivateWith(name, map)) return true;
  return canSeePrivateWith(access, name, map);
}

/**
 * 사람이 쓴 색인·설명에서 **볼 수 없는 비공개 채널을 언급한 줄을 뺀다.**
 *
 * 채널·문서 하나하나를 막는 것만으로는 안 된다. 색인(`slack-export/index.md`,
 * `documents/index.md`)은 사람이 쓴 문서라 표 칸과 분석 문장 안에 비공개 채널 **내용**이
 * 섞여 들어온다. 실제로 **공개 채널 행**에 비공개 채널의 논의와 금액·거래상대가 문장으로
 * 적혀 있었고, 그 줄이 공개 채널 답변의 프롬프트에도 그대로 실렸다 (2026-08-05 발견).
 * 그 실물은 자료 저장소 `사고기록.md` 의
 * 「`redactPrivateMentions` 가 줄을 통째로 지우는 이유」 절에 있다.
 *
 * 표는 한 줄에 한 채널, 분석은 한 줄에 한 항목이라 줄 단위로 자르면 맞아떨어진다.
 * 채널 **이름**을 감추려는 게 아니라 그 줄에 **딸려 오는 내용**을 막는 것이다.
 * 이름이 공개 문맥에 우연히 들어간 줄까지 지우는 쪽으로 넉넉하게 자른다 (fail-closed).
 *
 * **`fold` 로 다듬어 맞댄다** (WHK 지시 2026-09-03). 전에는 설정 문자열 그대로
 * `line.includes(c)` 로 대봐서, 설정에는 붙여 적힌 이름을 사람이 줄에 띄어 쓰면
 * (`matchesHiddenPrivate`·`archive.js` 는 이미 다듬어 맞대는데 여기만 안 그랬다) 안 맞아
 * 그 줄이 안 지워졌다 — 방침(fail-closed)과 반대 방향이었다. 다듬은 뒤 대보므로 이제
 * **줄 안의 아무 자리**에 이름이 섞여도(공백·붙임표가 있어도) 걸린다.
 */
export function redactPrivateMentions(text, access, map = currentChannelNames()) {
  /* 이 함수 자체가 **줄마다·메시지마다** 불린다(`archive.js` 의 revived·searchArchive).
   * 지도를 인자로 받는 이유가 그것이다 — 부르는 쪽이
   * 반복문 밖에서 한 번 떠 넘기면 `fs.statSync` 가 반복 횟수만큼 사라진다.
   * 안 넘기면 여기서 뜬다. **가리는 규칙은 그대로 이 함수 하나다.** */
  assertNameMap(map, 'redactPrivateMentions');
  const hidden = config.privateChannels.filter((c) => !canSeePrivateWith(access, c, map));
  if (!hidden.length) return text;
  /* **되짚지 않고 철자를 다 막는다.** 옛 대화 본문에 적힌 것은 옛 이름이라, 한 이름으로
   * 모으면 그 줄이 안 지워진다. 줄마다 도는 자리라 철자 집합은 여기서 한 번만 만든다. */
  const hiddenFolded = hidden.flatMap((c) => spellingsWith(c, map).map(fold)).filter(Boolean);
  return text
    .split('\n')
    .filter((line) => {
      const foldedLine = fold(line);
      return !hiddenFolded.some((c) => foldedLine.includes(c));
    })
    .join('\n');
}

/**
 * 입력이 **볼 수 없는 비공개 채널을 가리키나.** 아카이브에 파일이 없어도 이름으로 판정한다.
 *
 * 이게 없으면 "찾지 못했습니다. 후보: …" 쪽으로 떨어지는데, 그 문구는 물어본 이름을
 * 그대로 되돌려 준다 — 봇이 그걸 답변에 옮기면 이름이 나간다. 게다가 아카이브에 md 가 없는
 * 비공개 채널(#비공개나·#사업장카)은 "그런 채널 없습니다" 로 답하게 되어 사실도 아니다.
 *
 * fold 규칙은 archive.js 와 같다 (config 가 archive 를 import 하면 순환이라 여기 다시 적는다).
 * 넉넉하게 잡는다 — 짧은 조각이 걸려 과하게 막히는 쪽이 이름이 새는 쪽보다 낫다.
 *
 * 다듬기 자체는 위 `fold` export 하나를 쓴다 (전에는 이 함수 안에 지역 함수로 같은
 * 줄이 또 있었다 — WHK 지시 2026-09-03 로 `redactPrivateMentions` 도 같은 것을 쓰게
 * 맞추면서 한 곳으로 모았다).
 */
export function matchesHiddenPrivate(input, access) {
  const target = fold(normalizeChannel(input));
  if (!target) return false;
  const map = currentChannelNames();
  return config.privateChannels.some((c) => {
    if (canSeePrivateWith(access, c, map)) return false;
    // 철자 전부와 댄다 — 옛 이름으로 물어도 막혀야 한다.
    return spellingsWith(c, map).some((s) => {
      const f = fold(s);
      return Boolean(f) && (f === target || f.includes(target) || target.includes(f));
    });
  });
}

/**
 * 권한이 없을 때 도구가 돌려주는 문구.
 *
 * **채널명·문서명을 담지 않는다** (WHK 지시 2026-08-05). 이름만으로도 어떤 자리가 있고
 * 거기서 무엇을 다루는지가 드러나서다. 봇이 이름을 알고 물어본 경우까지 있으므로
 * "언급하지 말라"를 문구에 함께 넣는다 — 안 넣으면 봇이 되짚어 말해 버린다.
 *
 * "없다"고 답하지 말라는 것도 같이 못박는다. 없는 것과 못 보는 것은 다르고,
 * 없다고 답하면 있는 자료를 없는 것으로 만든다.
 */
export const BLOCKED_NOTE =
  '요청하신 자료는 비공개 자리에 있어 이 자리에서는 제공할 수 없습니다. '
  + '어느 채널·어느 문서인지도 답변에 밝히지 마세요. '
  + '"자료가 없다"고 하지 말고, 열람 권한이 없어 확인해 드릴 수 없다고만 하세요.';

/**
 * `digest.skipChannels` — **봇이 다루지 않기로 정해 둔 자리**를 짚었을 때의 문구.
 *
 * `BLOCKED_NOTE` 와 갈라 쓴다 (WHK 지시 2026-09-03). 2026-09-03 에 읽기 경로에도
 * skipChannels 필터를 넣으면서 처음에는 권한 차단 문구를 그대로 썼는데, **그 채널들은
 * 비공개가 아니다.** 봇은 이 문구를 그대로 읽고 사람에게 옮기므로, 공개 채널을 두고
 * "비공개라서 못 본다"고 답하게 된다 — 막히는 것은 맞지만 **이유가 틀리고**, 그 말을
 * 들은 사람은 있지도 않은 권한을 달라고 요청하러 간다. 안 막히는 것 다음으로 나쁜 것이
 * 틀린 이유로 막히는 것이다.
 *
 * **`BLOCKED_NOTE` 의 규율 둘은 그대로 지킨다.** ① 채널·문서 이름을 담지 않는다 —
 * `skipChannels` 에는 **비공개이면서 안 다루는 자리도 섞여 있어서**(설정 두 목록에 다
 * 적힌 채널), 이름을 담으면 막아 놓고 이름을 흘리는 꼴이 된다. ② "자료가 없다"고
 * 답하지 말라고 못박는다 — md 는 실제로 있고 안 읽기로 정한 것뿐이라, 없다고 하면
 * 있는 자료를 없는 것으로 만든다.
 *
 * **차단(BLOCKED_NOTE)과 마찬가지로 상수와의 동일성으로 판정한다** — `quietNotFoundNote`
 * (claude.js)가 "후보:" 가 든 문구만 손대므로 이 문구는 그 자리를 그냥 지나간다.
 */
export const OUT_OF_SCOPE_NOTE =
  '요청하신 자리는 이 봇이 다루지 않기로 정해 둔 곳이라 확인해 드릴 수 없습니다. '
  + '어느 채널·어느 문서인지도 답변에 밝히지 마세요. '
  + '"자료가 없다"고 하지 말고, 비공개라서가 아니라 이 봇이 다루는 범위 밖이라고만 하세요.';

/** 프롬프트 캐시 키·콘솔·대화 로그에 쓰는 짧은 표기 */
export function accessLabel(access) {
  if (!access) return '없음';
  if (access.full) return '전체';
  if (!access.channels.size) return '공개만';
  return [...access.channels].sort().map((c) => `#${c}`).join(', ');
}

/* search·read_document 가 본문을 자를 때 붙는 마커의 원본. 생산자(documents.js ·
 * search-terms.js · claude.js)와 소비자(archive.js · documents.js · check-*-cap*.js)가
 * 전부 여기서 가져간다 — 두 벌이던 시절 한쪽만 바뀌면 read_channel 안내가 조용히
 * 사라지고 검사도 초록이었다 (active-todos 2026-09-10). 문구를 바꾸면 봇에게 가는
 * 문서 히트 텍스트가 바뀐다 — 바꿀 일이 생기면 그것은 동작 변경이다. */
export const TRUNC_PHRASE = '길이 제한으로 잘림';
export const truncMarker = (hint) => `…(${TRUNC_PHRASE}${hint ? `. ${hint}` : ''})`;
