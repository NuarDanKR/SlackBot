/**
 * Hermes 와 오간 대화 로그 — 유일한 입출력 자리.
 *
 * 왜 있나: 봇 답변은 대화 아카이브에 **일부러** 넣지 않고(자기 요약을 원본 근거로 삼는 것을
 * 막으려는 규칙), DM 은 아카이브 수집 대상 자체가 아니다. 그래서 지금까지 Hermes 와 오간
 * 문답은 systemd journal 링버퍼에 잘린 한 줄로만 남고 답변 본문은 어디에도 없었다.
 *
 * 자리가 둘이다.
 *   ① 원본  logs/qa-YYYY-MM.jsonl        — 답변 직후 즉시 append. git 밖.
 *   ② 읽는 자리  50-resources/hermes-log/ — ①을 렌더한 md. 저장소 안, 매일 07:00 커밋.
 *
 * 나눈 이유는 config.js 의 LOG_DIR 주석에 있다 (작업 트리를 더럽히면 자동 반영이 멈춘다).
 *
 * **이 트리는 봇의 근거가 아니다.** archive.js 는 CHANNELS_DIR, documents.js 는 DOCS_DIR 만
 * 읽는다. 여기를 읽는 코드는 없고, 앞으로도 넣으면 안 된다 — 넣는 순간 봇이 자기 답변을
 * 원본 근거로 삼는다.
 *
 * 렌더는 **멱등**이다. 같은 jsonl 이면 몇 번을 돌려도 같은 md 가 나온다 (렌더 시각 같은
 * 매번 바뀌는 값을 md 에 넣지 않는다). 그래야 활동이 없는 날 헛커밋이 생기지 않는다.
 */
import { createLogStore } from './convo-log/store.js';
import { createLogRenderer } from './convo-log/render.js';
import { createLogStats } from './convo-log/stats.js';
import { createLogCodec } from './convo-log/codec.js';
import fs from 'node:fs';
import path from 'node:path';
import { config, LOG_ENABLED, LOG_DIR, LOG_RAW_DIR } from './config.js';

const TZ = config.timezone || 'Asia/Seoul';

/* 렌더된 회차 헤더에는 kind 대신 이 한글 이름만 남는다. **내보내는 이유**: 재보기
 * (`scripts/run-log-measure.js`)가 「정기 발송인가」를 그 이름으로 가려야 하는데,
 * 목록을 거기 베끼면 여기 하나 늘 때 조용히 갈린다 — BROADCAST 를 내보내는 것과 같은 이유. */
export const KIND_LABEL = {
  qa: '문답',
  empty: '빈 질문',
  daily: '일일 요약',
  weekly: '주간 요약',
  health: '위생 점검',
  ingest: '자동 반영',
};

/* 정기 발송(사람 질문이 아닌 것).
 *
 * **자동 반영도 여기 든다.** 실패만 기록하지만 사람이 물은 것이 아니라서, 빼면
 * renderIndex 가 문답으로 세고(`!BROADCAST.has(e.kind)`) 「누가 물었나」 표에
 * `(알 수 없음)` 행이 생긴다. 에러는 안 난다 — 숫자만 틀린다.
 *
 * export 하는 이유: scheduler.js 의 JOBS → LOG_KIND 사슬이 여기로 이어지는지를
 * check-failure-log.js 의 검사 ⑨ 가 직접 먹여 본다 — 값을 검사 쪽에 따로 베끼면
 * 여기 하나 늘 때 거기서 조용히 갈린다. */
export const BROADCAST = new Set(['daily', 'weekly', 'health', 'ingest']);

/* 「이 항목을 집계의 분모에 넣나」. **한 곳에서만 정한다.**
 *
 * `accountingOnly` 는 **회차가 아니라 기록**이다 — 사람이 물은 것도 발송한 것도 아니고,
 * 모델 사용량을 적으러 온 줄이다(`ingest/index.js` 의 「모델 사용량 기록 — 발송 아님」).
 * 그래서 어느 집계에도 분모로 들어가면 안 된다.
 *
 * 전에는 그 문장을 **네 자리가 각자 기억**했고, `cacheStats` 한 곳이 빠뜨렸다. 지금은
 * `accountingOnly` 가 전부 `kind:'ingest'` 라 BROADCAST 검사에 먼저 걸려 안 드러나는데,
 * **가려져 있는 것이지 맞게 적힌 것이 아니다.** broadcast 아닌 kind 로 한 번만 쓰면
 * 그 회차가 캐시 분모에 들어가 「쟀다」로 세어지고, 에러 없이 비율만 묽어진다
 * (2026-09-16). 같은 판정이 여러 곳에 있으면 그중 하나는 반드시 안 따라온다. */
export const isOperational = (e) => !e?.accountingOnly;
export const isQaRound = (e) => isOperational(e) && !BROADCAST.has(e?.kind);
export const isBroadcastRound = (e) => isOperational(e) && BROADCAST.has(e?.kind);

/* 캐시가 **안 맞은 것**과 캐시가 맞았는지를 **못 잰 것**을 가른다.
 *
 * `previous_message_not_found` 는 미스가 아니다 — API 에게 대조하라고 준 앞 요청을
 * 못 찾았다는 뜻이라, 그 회차는 캐시가 맞았는지 안 맞았는지를 **모른다**. 그래서
 * `cache_missed_input_tokens` 도 0으로 온다. 그 0을 「손해가 없었다」로 읽으면 안 된다.
 *
 * 새 사유는 기본이 「미스」다 — 모르는 것을 「모름」쪽에 넣으면 조용히 묻히고,
 * 이 표는 묻히면 안 되는 것을 보여주려고 있는 자리다. */
const DIAG_FAILED = new Set(['previous_message_not_found']);

/* 대조 기준 자체가 없던 회차. **API 사유가 아니라 우리가 붙이는 이름이라 괄호로 적는다.**
 *
 * 봇을 다시 켜면 기준(직전 응답 id)이 프로세스 메모리에만 있어 사라지고, 그러면 API 는 첫
 * 회차에 사유를 **아예 안 보낸다.** 사유 줄이 없는 것이 「맞았다」로 읽히는데, 그 첫 회차가
 * 바로 색인을 다시 쓰는 자리다. `claude.js` 가 `cacheBaseline: false` 로 남긴다. */
const NO_BASELINE = '(대조 기준 없음)';

const num = (v) => Number(v || 0).toLocaleString('en-US');

/** ISO 시각 → 설정 표준시 기준 { ym, date, time } */
function localParts(at) {
  const d = at instanceof Date ? at : new Date(at);
  // sv-SE 는 'YYYY-MM-DD HH:MM' 로 나온다.
  const s = new Intl.DateTimeFormat('sv-SE', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(d);
  return { ym: s.slice(0, 7), date: s.slice(0, 10), time: s.slice(11, 16) };
}

/** 슬랙 메시지 링크. ts 는 '1785796447.269769' 형식. */
export function permalink(channelId, ts, threadTs) {
  if (!channelId || !ts) return null;
  const base = `https://${config.workspace}/archives/${channelId}/p${String(ts).replace('.', '')}`;
  if (threadTs && threadTs !== ts) return `${base}?thread_ts=${threadTs}&cid=${channelId}`;
  return base;
}

/* ── ① 원본 기록 ──────────────────────────────────────────────── */

// Codex R3d: bind the store once; source guards and orchestration remain here.
const logStore = createLogStore({
  fs, path, LOG_ENABLED, LOG_RAW_DIR, LOG_DIR, localParts, console,
});
export const { append } = logStore;
const { readRaw, months, renderedMonths, writeIfChanged } = logStore;

/**
 * **원본이 사라진 것과 기록이 0건인 것을 가른다.**
 *
 * 렌더는 「원본이 유일한 진실」이라 md 를 통째로 다시 쓴다. 그 전제가 깨지는 자리가 하나 있다 —
 * 원본(`LOG_RAW_DIR`)은 git 밖이라 **클론에 안 딸려온다.** 그때 `months()` 는 빈 배열을 내는데,
 * 그것을 「0건」으로 읽으면 렌더본을 빈 표로 덮어쓴다. 원본이 없을 때 우리가 아는 것은
 * 「0건이다」가 아니라 **「모른다」**이다.
 *
 * 2026-08-31 저장소를 가르며 실제로 났다. 코드 저장소를 새로 복제했고 `logs/` 는 git 밖이라
 * 안 따라왔다. 17:00 회차가 거의 빈 원본을 읽어 `hermes-log/2026-08.md` 에서 6,743줄을 지운 채
 * 커밋·push 했다. 그때 `months()` 에 가드를 넣었지만 `renderIndex` 는 그대로였고, 그래서
 * **월별 md 는 지켜지는데 `index.md` 는 여전히 빈 표로 덮이는** 절반만 고친 상태가 남았다.
 *
 * 「렌더본이 있는데 원본이 0개」면 멈춘다. 첫 실행(둘 다 0)은 정상이라 통과시킨다 —
 * 없는 상태를 실패로 세면 이 가드가 곧 꺼진다.
 *
 * 순수 함수로 둔 이유: `scripts/check-log-render.js` 가 **이 판정을 그대로 불러** 양쪽을
 * 대본다. 검사가 같은 조건을 따로 적으면 여기가 바뀌어도 둘이 사이좋게 틀린다.
 *
 * @param {string[]} rawMonths 원본에서 찾은 달
 * @param {string[]} renderedMonths 이미 렌더돼 있는 달
 */
export function sourceGone(rawMonths, renderedMonths) {
  return rawMonths.length === 0 && renderedMonths.length > 0;
}

// Codex R3a: preserve public bindings and use the same labels/number formatter.
export const { toolLine, TOOL_LINE_PREFIX, parseToolLine, parseEntryHeader, parseCostLine } = createLogCodec({ KIND_LABEL, num });

// Codex R3b: preserve the public API and the original policy objects.
export const { failureStats, failureSectionLines, cacheStats } = createLogStats({
  BROADCAST, DIAG_FAILED, NO_BASELINE, isQaRound, isBroadcastRound,
});

// Codex R3c: keep public entry points and storage ownership in this facade.
const logRenderer = createLogRenderer({
  localParts, BROADCAST, KIND_LABEL, num, toolLine,
  readRaw, path, LOG_DIR, writeIfChanged, months,
  cacheStats, failureSectionLines, NO_BASELINE,
  isOperational, isQaRound, isBroadcastRound,
});
export const { renderEntry, renderMonth } = logRenderer;
const { renderIndex } = logRenderer;

/**
 * 원본이 있는 달을 전부 다시 렌더한다. 멱등이라 여러 번 돌려도 안전하고,
 * 바뀐 것이 없으면 파일을 건드리지 않아 헛커밋이 생기지 않는다.
 *
 * @returns {{months:object[], qa:number, broadcast:number, broken:number, changed:number}|null}
 */
export function renderAll({ write = true } = {}) {
  if (!LOG_ENABLED) return null;

  /* 원본이 사라졌으면 **아무것도 쓰지 않는다.** 자세한 것은 sourceGone 의 주석.
   * 조용히 넘기지 않는다 — 이 상태는 사람이 원본을 옮겨 와야 풀린다. */
  const raw = months();
  const rendered = renderedMonths();
  if (sourceGone(raw, rendered)) {
    return {
      noSource: true,
      rawDir: LOG_RAW_DIR,
      rendered,
      months: [],
      index: null,
      qa: 0,
      broadcast: 0,
      broken: 0,
      changed: 0,
    };
  }

  const stats = raw.map((ym) => renderMonth(ym, { write }));
  const idx = renderIndex(stats, write);
  return {
    months: stats,
    index: idx,
    qa: stats.reduce((a, s) => a + s.qa, 0),
    broadcast: stats.reduce((a, s) => a + s.broadcast, 0),
    broken: stats.reduce((a, s) => a + s.broken, 0),
    changed: stats.filter((s) => s.changed).length + (idx.changed ? 1 : 0),
  };
}
