/**
 * 아카이브 위생 점검 — "밀리고 있다"는 사실을 사람에게 알린다.
 *
 * 봇은 아카이브를 읽기만 한다. 아카이브에 새 내용을 넣는 일(slack-sync·doc-archive)은
 * 전부 사람 손이라, 며칠 안 돌리면 봇의 근거가 조용히 낡는다. **그 사실이 아무 데도
 * 드러나지 않는 것**이 진짜 문제다 — 봇은 "아카이브에 없습니다"라고 정상적으로 답한다.
 *
 * 세 가지만 본다.
 *   ① 대화 동기화가 며칠 밀렸나        (slack-export/.sync-state.json)
 *   ② 미변환 첨부가 몇 건인가          (슬랙 첨부 목록 ↔ documents/.doc-state.json)
 *   ③ `[공개]` 승인이 아카이브에 반영됐나 (슬랙 스레드 댓글 ↔ 문서 md 의 메타)
 *
 * ②③ 에서 **사람이 「이번엔 아니다」로 정한 것**(`.doc-state.json` 의 `deferred`)은 뺀다.
 * 안 빼면 줄지 않는 숫자가 매일 오고, 그러면 곧 알림 전체를 안 읽게 되어 진짜 밀린 것도
 * 같이 묻힌다. 대신 **만기가 지나면 저절로 다시 세어진다** — 미뤄 둔 것을 잊을 수 없게.
 *
 * ③ 을 여기 둔 이유: 승인은 문서가 아카이브에 들어간 **뒤에** 나오는데, 수집 스크립트는
 * 이미 변환된 파일을 태그를 보기 전에 건너뛴다(`fetch_slack_files.py` 342·347행).
 * 그래서 `doc-archive` 를 아무리 돌려도 그 승인은 화면에 안 뜬다 — 팀은 댓글을 달았고
 * 봇은 계속 막는 상태가 조용히 이어진다 (2026-08-07 발견: 워크스페이스의 `[공개]` 1건이
 * 8/5 부터 이 상태였고, 그 사이 doc-archive 를 두 번 돌렸다).
 * 이 점검은 **어차피 매일 전 채널의 스레드를 훑으므로**(첨부를 세려고) 슬랙 호출이 늘지 않는다.
 *
 * **git 에 쓰지는 않는다** — `syncForRead()` 로 `pull`/`fetch` 는 하지만 커밋·push 는
 * 없다. 두 상태 파일은 그렇게 받은 것을 읽기만 하고, 슬랙 쪽도 조회 API 만 쓴다.
 * 그래서 VM 의 읽기 전용 deploy key 로 그대로 동작한다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { compose } from './archive-health/report.js';
import { createHealthStatus } from './archive-health/status.js';
import { createHealthDocFilters } from './archive-health/doc-filters.js';
import { createHealthAttachments } from './archive-health/attachments.js';
import { createHealthDocuments } from './archive-health/documents.js';
export { compose };
import { config, ARCHIVE_DIR, DOCS_DIR, isPrivateChannel } from './config.js';
import { archiveChannelNames, archiveChannelOf } from './archive.js';
import { listBotChannels, fetchAllReplies, isBotMessage, dropSkippedChannels, DEFAULT_LIVE_FETCH_MAX_DAYS } from './slack-live.js';
import { chunkForSlack } from './format.js';
import { append as logConversation } from './convo-log.js';
import { SKILL_SCRIPTS, runScript, activeDeferred } from './ingest/util.js';
// 07:00 회차와 **같은 판정**으로 「빼·나중에」를 거른다 (아래 `stalePendingWork`).
import { suppress } from './ingest/pending-work.js';
import { syncForRead } from './ingest/git.js';

// doc-archive 가 다루는 확장자. **같은 목록이 세 자리에 있다** — 여기,
// `.claude/skills/doc-archive/scripts/fetch_slack_files.py` 의 `DOC_EXTS`,
// 같은 폴더 `apply_approvals.py` 의 `DOC_EXTS`. 갈리면 에러 없이 숫자만 달라진다.
// 갈렸는지는 `scripts/check-shared-rules.js` 의 절 ④ 가 본다.
export const DOC_EXTS = new Set(['pdf', 'hwp', 'hwpx', 'docx', 'doc', 'pptx', 'xlsx', 'xlsm']);
// 같은 자료를 hwpx 와 pdf 로 함께 올리면 한 건으로 센다.
// `fetch_slack_files.py` 의 `PREFER_EXTS` 와 순서까지 같아야 한다 (--prefer 기본값과 같은 순서).
// 갈렸는지는 `check-shared-rules.js` 의 절 ④ 가 본다.
export const PREFER = ['hwpx', 'hwp', 'docx', 'pdf'];

const DEFAULTS = { scanDays: 60, syncWarnDays: 7, docWarnCount: 1, pendingEditDays: 3 };

/**
 * 비공개 채널 문서를 공개로 여는 승인 태그.
 *
 * **전용 태그만 본다** — `scripts/fetch_slack_files.py` 의 `PUBLIC_TAG` 와 같은 판정이어야 한다.
 * '공개' 라는 낱말을 찾으면 "공개해도 되나요?"·"공개 불가" 같은 질문·부정문까지 걸린다.
 * `g` 플래그를 붙이지 않는다 — `lastIndex` 가 남아 같은 정규식이 한 번씩 걸러 무는다.
 */
const PUBLIC_TAG = /\[공개\]/;

/** health 설정 (없는 값은 기본값). syncCriticalDays 는 실시간 조회 한계에서 역산한다. */
function settings() {
  const h = config.digest?.health || {};
  const live = config.limits?.liveFetchMaxDays ?? DEFAULT_LIVE_FETCH_MAX_DAYS;
  return {
    scanDays: h.scanDays ?? DEFAULTS.scanDays,
    syncWarnDays: h.syncWarnDays ?? DEFAULTS.syncWarnDays,
    // 한계에 닿은 뒤 알리면 늦다. 이틀 남았을 때 경고한다.
    syncCriticalDays: h.syncCriticalDays ?? Math.max(1, live - 2),
    docWarnCount: h.docWarnCount ?? DEFAULTS.docWarnCount,
    // 프로덕션 VM 에서 git 을 만지는 동작이라 코드 배포 없이 되돌릴 자리를 둔다.
    // 끄면 pull 만 멈추고 낡음 표시는 그대로 돈다 — 손잡이가 알림을 통째로 끄면
    // 끈 사실을 아무도 모르는 상태로 돌아간다.
    syncBeforeCheck: h.syncBeforeCheck ?? true,
    liveFetchMaxDays: live,
    // 반영 안 된 슬랙 수정·삭제를 며칠째부터 여기서도 알릴지.
    // 07:00 자동 반영 보고는 매일 오지만 09:00 은 이상할 때만 온다 — 무게가 다르다.
    pendingEditDays: h.pendingEditDays ?? DEFAULTS.pendingEditDays,
    archiveCheck: h.archive?.enabled !== false,
  };
}

/**
 * 요약 전 마감 점검이 훑는 기간. 기본 2일 — 어제 저녁에 올라온 것까지 든다
 * (일일 요약의 창이 24시간이라 어제 저녁 자료도 오늘 요약 대상이다).
 */
function preDigestScanDays() {
  return config.digest?.healthPre?.scanDays ?? 2;
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

const { syncLag, stalePendingEdits, stalePendingWork } = createHealthStatus({
  config, path, ARCHIVE_DIR, readJson, DEFAULTS, activeDeferred, suppress,
});
export { syncLag, stalePendingEdits, stalePendingWork };

/**
 * 아카이브 md 의 **구조 결함**. 계산으로 못 고치는 것들이다.
 *
 * 스레드 머리줄이 「(4)」인데 답글 줄이 0개인 자리, 수집 실패 표식이 남은 자리, 봇이 남의
 * 발언으로 읽게 되는 한 줄 압축 메시지 같은 것. **에러가 안 나는 종류**라 아무도 안 보면
 * 영영 그대로다 — `#사업장타` 의 답글 4건이 그렇게 한 달을 남아 있었다(2026-08-10 발견).
 *
 * 검사는 여기 두지 않고 `.claude/skills/slack-sync/scripts/verify_archive.py` 를 부른다.
 * 사람이 손으로 돌릴 때와 같은 코드여야 하고, 예전에 같은 검사를 두 번 쓰고 두 번 버렸다.
 *
 * **관문(runGate)이 아니라 여기 붙인 이유**: 관문 실패는 그날 반영을 통째로 롤백한다.
 * 어제부터 있던 결함 하나로 오늘 대화가 안 들어오면 동기화가 밀리고, 14일을 넘기면
 * 실시간 조회로도 못 메운다. 이 점검은 알리기만 하고 아무것도 막지 않는다.
 */
export async function archiveIssues() {
  /* **여기서 던지지 않는다.** `runScript` 는 `python()` 을 try 밖에서 부르고, 그 함수는
   * 파이썬을 못 찾으면 던진다. 이 점검은 09:00 DM 의 **네 항목 중 하나**일 뿐인데, 예외가
   * 밖으로 나가면 scheduler 가 받아 동기화 밀림·미변환 첨부·승인 미반영까지 전부 「실행
   * 실패」 한 줄로 바꿔 버린다. 이 파일은 오늘 전까지 파이썬을 아예 안 썼다. */
  let r;
  try {
    r = await runScript(SKILL_SCRIPTS.verifyArchive, ['--all', '--json']);
  } catch (err) {
    return { total: 0, files: [], failed: String(err.message || err).slice(0, 200) };
  }

  const last = (r.stdout || '').trim().split('\n').pop() || '';
  try {
    const parsed = JSON.parse(last);
    return { total: parsed.total || 0, files: parsed.files || [] };
  } catch {
    /* **빈 문자열로 두지 않는다.** 빈 문자열은 거짓이라 compose 의 `arch?.failed` 도
     * `arch?.total`(0) 도 안 걸리고, 그러면 DM 이 「아카이브가 깨끗하다」와 똑같아 보인다.
     * 파이썬이 상한(300초)에 걸려 죽으면 두 스트림이 다 비는 일이 실제로 생긴다.
     * derive.js 가 같은 자리에 글자 폴백을 둔 것과 같은 이유다. */
    return {
      total: 0,
      files: [],
      failed: (r.stderr || r.stdout || '').trim().slice(0, 200) || '결과를 읽지 못했습니다',
    };
  }
}

/** 파일명 확장자를 먼저 본다 — 슬랙은 .hwp 의 filetype 을 'binary' 로 보고한다. */
function extOf(f) {
  const name = f.name || '';
  const dot = name.lastIndexOf('.');
  const suffix = dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  return suffix || String(f.filetype || '').toLowerCase();
}

const { nameKey, docFilters, classifyDoc } = createHealthDocFilters({ activeDeferred });
export { nameKey, docFilters, classifyDoc };

/**
 * 사람이 **「안 넣기로」 정한** 갈래. 그 자료에 `[공개]` 승인이 달려도 알리지 않는다 —
 * 반영할 문서가 아카이브에 없어서, 알리면 「변환할 때 함께 적을 것」이라는 지시가
 * 영영 안 사라진다.
 *
 * `known`·`other_format` 은 여기 없다 — 이미 들어간 자료라 승인을 **반영할 수 있다.**
 */
const DECIDED_OUT = new Set(['excluded', 'deferred', 'superseded']);

/* 개명 지도. **회차마다 새로 만든다** — 모듈 로드 때 한 번 만들면 `.sync-state.json` 이
 * 갱신돼도 프로세스가 도는 동안 옛 지도를 쓴다. 봇은 며칠씩 떠 있다. */
const archiveChannel = (name) => archiveChannelOf(name, archiveChannelNames());

const { channelAttachments } = createHealthAttachments({ fetchAllReplies, isBotMessage, extOf, PUBLIC_TAG, archiveChannel });

const { pendingDocuments, unconvertedAmong } = createHealthDocuments({
  fs, path, config, DOCS_DIR, readJson, settings, activeDeferred, isPrivateChannel, listBotChannels, channelAttachments, nameKey, docFilters, classifyDoc, DOC_EXTS, PREFER, DECIDED_OUT, extOf, archiveChannel, dropSkippedChannels,
});
export { pendingDocuments, unconvertedAmong };

/** 요약은 공개 채널로 나가지만, 위생 점검은 내부 운영 상태라 항상 본인 DM 으로 보낸다. */
async function ownerDm(client) {
  if (!config.owner?.slackUserId) throw new Error('config.json 의 owner.slackUserId 가 비어 있습니다.');
  const im = await client.conversations.open({ users: config.owner.slackUserId });
  return im.channel.id;
}

/**
 * @param {object} client Slack WebClient
 * @param {{dry?:boolean, preDigest?:boolean}} opts
 *   dry=true 면 슬랙에 보내지 않고 콘솔에만 출력.
 *   preDigest=true 면 일일 요약 직전 회차 — 첨부만 보고, 훑는 기간도 짧다.
 *   짧게 보는 이유는 이 회차가 "지금 돌리면 오늘 요약에 실린다"를 알리는 자리이기 때문이다.
 *   60일치를 다시 세어 보내면 어제까지 밀린 것과 섞여 오늘 것이 안 보인다.
 */
async function healthBody(client, { dry = false, preDigest = false, sync = false }, reached) {
  const s = settings();
  const started = Date.now();
  console.log(`\n[health] ${preDigest ? '요약 전 마감 점검' : '아카이브 위생 점검'}…`);

  // 재기 **전에** 맞춰본다. 이 줄이 없으면 최대 15분 낡은 상태를 현재 사실로 보고한다
  // (2026-08-20: 15:47 커밋을 못 받은 채 16:00 점검이 이미 처리한 1건을 다시 알렸다).
  // pull 은 예약 실행에서만 — 손 실행은 WHK PC 에서도 도므로 남의 작업 트리를 안 건드린다.
  const freshness = await syncForRead({ pull: sync && s.syncBeforeCheck });
  reached.push('git 최신화');

  // 밀림은 로컬 상태 파일 한 줄이라 회차와 무관하게 재 둔다. 요약 전 회차에서는 보고에만 안 싣는다.
  const lag = syncLag();
  reached.push('동기화 나이');
  const docs = await pendingDocuments(client, preDigest ? { scanDays: preDigestScanDays() } : undefined);
  reached.push('첨부 스캔');

  // 둘 다 요약 전 회차에서는 건너뛴다. 그 회차는 "지금 doc-archive 를 돌리면 오늘 요약에
  // 실린다"를 알리는 자리라, 다른 것이 섞이면 오늘 할 일이 안 보인다.
  const stale = preDigest ? [] : stalePendingEdits(Date.now(), s.pendingEditDays);
  // 07:00 할 일도 같은 기준일을 쓴다 — 따로 두면 두 알림이 다른 날 뜬다.
  const work = preDigest ? [] : stalePendingWork(Date.now(), s.pendingEditDays);
  const arch = preDigest || !s.archiveCheck ? null : await archiveIssues();
  reached.push('아카이브 점검');

  const lagText = lag.missing ? '상태 파일 없음' : `${lag.days}일 전(${lag.lastSync})`;
  console.log(
    `[health] ${preDigest ? '' : `동기화 ${lagText} · `}미변환 첨부 ${docs.total}건(최근 ${docs.scanDays}일)` +
      (docs.deferred?.length ? ` · 보류 ${docs.deferred.length}건` : '') +
      (docs.approvals?.length ? ` · 승인 미반영 ${docs.approvals.length}건` : '') +
      (docs.failed.length ? ` · 읽기 실패 ${docs.failed.length}개` : '') +
      (stale.length ? ` · 묵은 미반영 ${stale.length}건` : '') +
      (work.length ? ` · 묵은 할 일 ${work.length}건` : '') +
      (arch?.total ? ` · 아카이브 결함 ${arch.total}건` : '') +
      ` · 기준 ${freshness.head}${freshness.ok ? '' : `(낡음, behind ${freshness.behind ?? '모름'})`}` +
      ` (${((Date.now() - started) / 1000).toFixed(1)}s)`,
  );

  const body = compose(lag, docs, s, { preDigest, stale, work, arch, freshness });
  reached.push('본문 조립');

  // 이상이 없으면 보내지 않는다. 매일 "이상 없음" 이 오면 곧 안 읽게 되고,
  // 그때부터는 진짜 경고도 같이 안 읽힌다.
  if (!body) {
    console.log('[health] 임계 미만 — 보내지 않습니다.');
    return { sent: false, reason: 'healthy' };
  }

  if (dry) {
    console.log('\n' + '─'.repeat(52));
    console.log(body);
    console.log('─'.repeat(52));
    console.log('\n(--dry 이므로 슬랙에 보내지 않았습니다)\n');
    return { sent: false, reason: 'dry-run', text: body };
  }

  const target = await ownerDm(client);
  for (const part of chunkForSlack(body)) {
    await client.chat.postMessage({ channel: target, text: part, unfurl_links: false, unfurl_media: false });
  }
  console.log(`[health] ${config.owner.name} 에게 DM 전송 완료\n`);

  // 모델을 부르지 않으므로 비용은 없다.
  logConversation({
    kind: 'health',
    ok: true,
    target: `${config.owner.name} DM`,
    origin: preDigest
      ? `요약 전 마감 · 미변환 첨부 ${docs.total}건(최근 ${docs.scanDays}일)`
      : `동기화 ${lagText} · 미변환 첨부 ${docs.total}건`,
    answer: body,
    elapsedMs: Date.now() - started,
  });

  return { sent: true, text: body };
}

/**
 * 위생 점검. 본문은 `healthBody` 이고 여기서는 **어디까지 갔나만 붙인다** (2026-08-28).
 *
 * 껍데기를 따로 둔 이유는 본문 70여 줄을 try 안에 넣으면 들여쓰기가 전부 바뀌어
 * diff 에서 진짜 변경이 안 보이기 때문이다. 바깥에서 보는 이름과 반환은 그대로다.
 *
 * **한 단계도 못 지났으면 아무것도 안 붙인다** — 빈 발자국은 「아무 데도 못 갔다」와
 * 「못 쟀다」를 같은 글자로 만든다(convo-log.js 가 값이 없으면 줄을 안 쓴다).
 */
export async function runHealth(client, opts = {}) {
  const reached = [];
  try {
    return await healthBody(client, opts, reached);
  } catch (err) {
    if (reached.length) err.hermesContext = `${reached.join(' ✓ · ')} ✓ → 그다음에서 중단`;
    throw err;
  }
}
