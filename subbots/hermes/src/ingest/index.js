import { usageFields } from '../llm/usage.js';
import { append as logConversation } from '../convo-log.js';
/**
 * 아카이브 자동 반영 — 전체 순서.
 *
 *   ① git pull (작업 트리가 더러우면 멈춘다)
 *   ② 대화 원문 수집·삽입 + Hermes 대화 로그 렌더
 *   ③ 상단 요약 대조 (찾기만 — 고치지 않는다)
 *   ③-2 파생값 재계산 (헤더 건수·index.md 표 — 판단이 없는 값이라 그냥 다시 센다)
 *   ④ 관문 — 하나라도 실패하면 되돌린다
 *   ⑤ commit + push
 *   ⑥ DM 보고 (알릴 것이 있을 때만 — 중간에 멈췄으면 그것도 알린다)
 *
 * 문서(첨부) 반영은 아직 여기 없다. 계획상 대화가 며칠 안정적으로 도는 것을 본 뒤에 붙인다.
 */
import { ARCHIVE_DIR, DOCS_DIR, LOG_DIR, config } from '../config.js';
import { renderAll as renderConversationLog } from '../convo-log.js';
import { ingestConversations } from './slack-archive.js';
import { checkSummaries } from './summary.js';
import { runDerive } from './derive.js';
import { runPendingWork } from './pending-work.js';
import { runGate } from './verify.js';
import { head, syncBeforeWork, hasChanges, commitAndPush, rollback } from './git.js';
import { compose, send } from './report.js';
import { clearTemp, DATA_ROOT } from './util.js';
import path from 'node:path';
import { assertMayWriteArchive } from '../mode.js';

/* 실패한 회차가 어디까지 갔나.
 *
 * `result` 는 단계를 지날 때마다 키가 하나씩 차므로(아래 ①②③…) **그 순서가 곧 발자국이다.**
 * 새로 잴 것이 없다. 단계를 늘리면 여기 한 줄을 함께 늘린다. */
const STEPS = [
  ['conversations', '대화 반영'],
  ['log', '로그 렌더'],
  ['summary', '요약 대조'],
  ['derive', '파생값'],
  ['work', '할 일'],
  ['gate', '관문'],
  ['push', '커밋·push'],
];
function stepsReached(result) {
  return STEPS.map(([k, label]) => `${label} ${result[k] ? '✓' : '✗'}`).join(' · ');
}

/**
 * 던지지 않고 정상 반환하는 실패가 셋이라 하나로 가른다 (2026-08-28 F1).
 *
 *   fatal        — 위 try 블록 자체가 죽었다 (catch 가 `result.fatal` 을 채운다)
 *   gate_failed  — 관문(runGate)이 막아 되돌렸다 (`result.gate.passed === false`)
 *   push_failed  — 커밋은 됐는데 push 만 안 됐다 (`commitAndPush` 의 `pushed === false`)
 *
 * 셋 다 throw 하지 않으므로 scheduler 의 catch 도, 예전의 `result.fatal` 단독 판정도
 * 안 걸렸다 — 그래서 관문에서 막힌 회차가 로그에 한 줄도 안 남았다. 셋이 겹치면 더 이른
 * 단계가 근본 원인이므로 fatal → gate_failed → push_failed 순으로 앞의 것을 쓴다.
 */
function classifyFailure(result) {
  if (result.fatal) {
    return { reason: 'fatal', errorType: 'ingest_fatal', error: String(result.fatal).slice(0, 500) };
  }
  if (result.gate && !result.gate.passed) {
    const checks = result.gate.failures.map((f) => f.check).join(' · ');
    return { reason: 'gate_failed', errorType: 'gate_failed', error: `관문 실패 (${checks})` };
  }
  if (result.push?.committed && !result.push.pushed) {
    return { reason: 'push_failed', errorType: 'push_failed', error: `push 실패 — ${result.push.reason}` };
  }
  if (result.secondaryErrors?.length) {
    return { reason: 'fatal', errorType: 'ingest_finalize_failed', error: '자동 반영 후속 처리 실패' };
  }
  return null;
}

/** 커밋 대상 경로 (자료 저장소 상대) */
function targetPaths() {
  const rel = (p) => path.relative(DATA_ROOT, p).split(path.sep).join('/');
  return [rel(ARCHIVE_DIR), ...(DOCS_DIR ? [rel(DOCS_DIR)] : []), ...(LOG_DIR ? [rel(LOG_DIR)] : [])];
}

/**
 * 자동 반영 커밋의 메시지. **순수 함수로 떼어 둔 이유는 확인할 방법이 그것뿐이라서다** —
 * 이 문구는 위생 점검이 아니라 자동 반영이 만들므로 `npm run health -- --dry` 로는 안 보인다.
 *
 * **새 대화가 0건이어도 무엇을 했는지 말해야 한다.** CLAUDE.md 는 「자동 반영이 돌았는지는
 * DM 유무가 아니라 `git log --author=Hermes` 로 판정하라」고 하는데, 예전에는 derive 가
 * `index.md` 를 16곳 고쳐도 메시지가 `동기화 시각만 갱신` 이었다 — 그 로그를 보고
 * 「아무것도 안 했다」로 읽게 된다.
 *
 * @param {{added:number, replies:number, fixes:number, botReplies:number,
 *          logged:number, derived:number, channels:Array}} counts
 */
/**
 * 중간에 멈춘 채널을 커밋 본문에 알리는 줄들. 갈래가 둘이라 문장이 둘이다.
 *
 * · **셈에 든 채널** — 위 목록에 줄이 있고 합계에 건수를 보탰다. 그 합계가 「md 에 들어간
 *   것」과 다를 수 있다는 것이 할 말이다.
 * · **건수 0 인 채널** — 위 목록에 줄이 없다. 합계를 오염시키지 않았으므로 합계를 걸고
 *   넘어지면 안 되고, 그래도 **그 채널이 오늘 통째로 빠졌다**는 것은 말해야 한다.
 *   바깥 catch 가 만드는 결과(`{ added: 0, error }`)가 여기 오므로 이쪽이 오히려 흔하다.
 */
function failureLines(channels) {
  const names = (list) => list.map((c) => `#${c.channel}`).join(', ');
  const counted = channels.filter(
    (c) => c.error && (c.added || c.threadReplies || c.selfCorrections || c.botReplies),
  );
  const empty = channels.filter(
    (c) => c.error && !(c.added || c.threadReplies || c.selfCorrections || c.botReplies),
  );
  const out = [];
  if (counted.length) {
    out.push(`중간에 멈춘 채널이 있어 위 건수가 다음 회차에 또 찍힐 수 있습니다 — ${names(counted)}.`);
  }
  if (empty.length) {
    out.push(`한 건도 못 읽고 멈춘 채널 — ${names(empty)}. 오늘 이 채널은 통째로 안 들어왔습니다.`);
  }
  if (out.length) out.push('실패 사유는 아침 DM 의 「❌ 실패」 절에 있습니다.', '');
  return out;
}

export function buildCommitMessage({
  added = 0, replies = 0, fixes = 0, botReplies = 0,
  logged = 0, derived = 0, channels = [],
} = {}) {
  const logLine = logged ? `Hermes 대화 로그 ${logged}개 파일 갱신.` : '';
  const replyLine = replies ? `나중에 달린 스레드 답글 ${replies}건을 부모 메시지에 덧붙였습니다.` : '';
  /* 자리표시 블록에는 봇 글의 본문이 없다 — 사람이 쓴 것만 들어간다. 두 줄로 나누는
   * 이유는 담긴 것이 다르기 때문이다: Hermes 것은 `[정정]` 만, 다른 봇 것은 답글 전부.
   * 합쳐 세면 커밋 메시지가 남의 인사 답글을 「Hermes 글에 달린 [정정]」으로 기록한다
   * (2026-08-28 커밋 cffb772 가 그랬다 — 실제로는 Calendar Bot 글의 답글이었다). */
  const fixLine = fixes ? `Hermes 글에 달린 [정정] ${fixes}건을 자리표시 블록에 담았습니다.` : '';
  const botReplyLine = botReplies
    ? `봇 글에 달린 답글 ${botReplies}건을 자리표시 블록에 담았습니다.` : '';
  const deriveLine = derived ? `색인 ${derived}곳 갱신.` : '';

  if (added || replies || fixes || botReplies) {
    return [
      `아카이브 자동 반영 — 대화 ${added}건`
        + (replies ? ` · 답글 ${replies}건` : '')
        + (fixes ? ` · 정정 ${fixes}건` : '')
        + (botReplies ? ` · 봇글답글 ${botReplies}건` : ''),
      '',
      /* **중간에 멈춘 채널은 그렇다고 적는다.**
       *
       * 이 숫자들은 「슬랙에서 읽어 온 것」이다. 회차가 중간에 멈추면 그 채널의 상태가
       * 저장되지 않아 **다음 회차가 같은 것을 다시 읽고**, md 쪽 중복 판정이 조용히
       * 걸러 낸다. 그래서 파일은 한 글자도 안 바뀌었는데 커밋 메시지에는 매일 같은
       * 건수가 찍힌다 — 2026-09-22 에 어느 채널이 그랬고, 그 줄만 보면 매일 반영되는
       * 것처럼 읽혔다.
       *
       * **「반영된 것이 아니다」라고는 적지 않는다.** 월별 삽입(④)은 답글 덧붙이기
       * (④-b·④-c)보다 **먼저** 돌아 md 에 실제로 쓰고, 그 변경은 바로 이 커밋에 담겨
       * 나간다. 커밋이 자기가 담은 것을 안 담았다고 말하면 사람이 손으로 다시 넣는다.
       * 사실은 「또 찍힐 수 있다」쪽이다.
       *
       * 숫자를 지우지도 않는다 — 지우면 그 채널이 아예 조용했던 날과 구별이 안 된다. */
      channels
        .filter((c) => c.added || c.threadReplies || c.selfCorrections || c.botReplies)
        .map((c) => `  #${c.channel} ${c.added}건`
          + (c.threadReplies ? ` (+답글 ${c.threadReplies})` : '')
          + (c.selfCorrections ? ` (+정정 ${c.selfCorrections})` : '')
          + (c.botReplies ? ` (+봇글답글 ${c.botReplies})` : '')
          + (c.error ? ' — 중간에 멈췄습니다. 상태가 저장되지 않아 다음 회차에 또 찍힐 수 있습니다' : ''))
        .join('\n'),
      '',
      /* **멈춘 채널을 여기서 한 번 더 부르되, 셈에 든 것만 부른다.**
       *
       * 처음에는 실패한 채널을 전부 불렀다. 그런데 가장 흔한 실패는 바깥 catch 가 만드는
       * `{ added: 0, error }`(조회 실패·레이트리밋)라, 위 목록에 줄도 없고 합계에 한 건도
       * 안 보탠 채널을 「합계를 오염시켰다」고 지목했다 — 멀쩡한 숫자를 못 믿게 만든다.
       * 건수 0 인 실패는 셈이 아니라 **실패했다는 사실**만 말한다. */
      ...failureLines(channels),
      '요약 섹션은 건드리지 않았습니다 (사람이 고치는 자리).',
      ...(replyLine ? [replyLine] : []),
      ...(fixLine ? [fixLine] : []),
      ...(botReplyLine ? [botReplyLine] : []),
      ...(deriveLine ? [deriveLine] : []),
      ...(logLine ? [logLine] : []),
    ].join('\n');
  }

  // 새 대화가 0건인 날. 한 줄이라 무엇을 했는지 제목에 다 적는다.
  const did = [
    ...(derived ? [`색인 ${derived}곳 갱신`] : []),
    ...(logged ? [`Hermes 대화 로그 ${logged}개 파일 갱신`] : []),
  ];
  const head = did.length
    ? `아카이브 자동 반영 — ${did.join(' · ')} (새 대화 없음)`
    : '아카이브 자동 반영 — 동기화 시각만 갱신 (새 대화 없음)';

  /* **조용한 날에도 실패는 말한다.**
   *
   * 이 갈래는 「합계가 전부 0」이라 한 줄로 끝났는데, 가장 흔한 실패(조회 실패·레이트리밋)가
   * 만드는 결과가 바로 `added 0` 이다. 그래서 **채널이 통째로 안 들어온 날의 커밋 메시지가
   * 「새 대화 없음」 한 줄**이었다 — 조용한 날과 글자가 같다. */
  const failed = failureLines(channels);
  return failed.length ? [head, '', ...failed].join('\n').trimEnd() : head;
}

export async function runIngest(client, { dry = false, skipSummary = false, scanEdits } = {}) {
  // 연동 모드에서는 원문을 쓰지 않는다. **맨 앞이어야** 한다 — 아래에서
  // git sync 가 먼저 돌면 트리를 건드린 뒤에 막는 꼴이 된다.
  assertMayWriteArchive('아카이브 자동 반영(ingest)');
  const started = Date.now();
  const paths = targetPaths();
  console.log('\n[ingest] 아카이브 자동 반영…');

  const result = { dry };
  let summaryAccounting;
  let startSha = null;
  // syncBeforeWork 를 통과했는지. 통과 **전** 실패에 rollback 을 걸면 안 된다 —
  // `reset --hard` 는 트리 전체에 걸리므로, 트리를 더럽혀 둔 사람의 작업을 지운다.
  let synced = false;
  let commitStarted = false;
  const errorText = (err) => String(err?.data?.error || err?.message || err).slice(0, 500);
  const secondary = (stage, err) => {
    (result.secondaryErrors ||= []).push({ stage, error: errorText(err) });
    console.error(`[ingest] ${stage} 실패 —`, errorText(err));
  };
  const recover = async () => {
    if (result.rollback) return; // Attempt at most once, including failed attempts.
    if (!synced || !startSha || dry) return;
    if (commitStarted) {
      result.rollback = { attempted: false, reason: 'commit-started' };
      result.commitOutcomeUnknown = !result.push;
      return;
    }
    result.rollback = { attempted: true, ok: false, sha: startSha };
    try {
      await rollback(startSha, paths);
      result.rollback.ok = true;
    } catch (err) {
      result.rollback.error = errorText(err);
      secondary('rollback', err);
    }
  };

  try {
    /* ① 원격과 맞추기 */
    if (!dry) {
      const pulled = await syncBeforeWork();
      // Recovery must preserve commits received by the successful pull.
      startSha = await head();
      synced = true;
      console.log(`[ingest] git pull — ${pulled.split('\n')[0]}`);
    }

    /* ② 대화 원문 */
    result.conversations = await ingestConversations(client, { dry, scanEdits });
    console.log(
      `[ingest] 대화 ${result.conversations.totalAdded}건` +
        (result.conversations.totalReplies ? ` · 나중에 달린 답글 ${result.conversations.totalReplies}건` : '') +
        (result.conversations.totalSelfCorrections ? ` · Hermes 글에 달린 정정 ${result.conversations.totalSelfCorrections}건` : '') +
        (result.conversations.totalBotReplies ? ` · 봇 글에 달린 답글 ${result.conversations.totalBotReplies}건` : '') +
        (result.conversations.totalEdited ? ` · 슬랙에서 고쳐진 것 ${result.conversations.totalEdited}건` : '') +
        (result.conversations.totalDeleted ? ` · 지워진 것 ${result.conversations.totalDeleted}건` : '') +
        (result.conversations.newChannels.length ? ` · 새 채널 ${result.conversations.newChannels.length}개` : '') +
        (result.conversations.errors.length ? ` · 실패 ${result.conversations.errors.length}건` : ''),
    );

    /* ②-2 Hermes 대화 로그 — 원본 jsonl(git 밖)을 읽어 md 로 렌더한다.
     * 여기서 하는 이유: syncBeforeWork 를 이미 지난 뒤라 작업 트리를 더럽혀도 안전하고,
     * 커밋·push 경로가 아래 하나로 유지된다. dry 에서는 쓰지 않는다 —
     * 쓰고 커밋하지 않으면 다음 실행이 더러운 트리에서 멈춘다. */
    result.log = renderConversationLog({ write: !dry });
    if (result.log?.noSource) {
      console.log(
        `[ingest] 대화 로그 — ⚠️ 원본이 없어 아무것도 쓰지 않았습니다 (${result.log.rawDir}). ` +
          `렌더본 ${result.log.rendered.length}개는 그대로입니다.`,
      );
    } else if (result.log) {
      console.log(
        `[ingest] 대화 로그 — 문답 ${result.log.qa}건 · 정기 발송 ${result.log.broadcast}건 · ` +
          `바뀐 파일 ${result.log.changed}개` +
          (result.log.broken ? ` · ⚠️ 읽지 못한 줄 ${result.log.broken}개` : ''),
      );
    }

    const touched = result.conversations.channels.filter((c) => c.added && c.transcript);

    /* ③ 요약 대조 — 새 메시지가 들어온 채널만 */
    if (!skipSummary && touched.length) {
      result.summary = await checkSummaries(touched, {
        onUsage: (accounting) => { summaryAccounting = accounting; },
      });
      console.log(`[ingest] 요약 대조 — 후보 ${result.summary.findings.length}건`);
    }

    /* ③-2 파생값 재계산 — 헤더 건수·index.md 표.
     * `if (!dry)` 밖에 둔다: `npm run ingest -- --dry` 로 "지금 얼마나 어긋나 있나" 를 볼 수
     * 있어야 하고, dry 면 스크립트도 --dry-run 으로 읽기만 한다.
     * 관문 **앞**에 둔다: 여기서 쓴 뒤에 관문이 돌아야 파일이 깨졌을 때 잡힌다. */
    result.derive = await runDerive({ dry });
    console.log(
      `[ingest] 파생값 — 맞춘 곳 ${result.derive.changed.length}건` +
        (result.derive.unresolved.length ? ` · 못 고친 것 ${result.derive.unresolved.length}건` : '') +
        (result.derive.failed ? ` · ⚠️ 실패` : ''),
    );

    /* ③-3 할 일 목록 — DM 은 알림이고 이 파일이 목록이다.
     *
     * 파생값 **뒤**에 둔다: `derive.unresolved`(기계가 못 고친 것)가 항목으로 들어가야 한다.
     * 관문 **앞**에 두는 이유는 파생값과 같다 — 여기서 쓴 파일이 관문의 검사를 받아야 한다. */
    const today = new Intl.DateTimeFormat('sv-SE', { timeZone: config.timezone }).format(new Date());
    result.work = runPendingWork(result, {
      dry,
      today,
      // 이 회차가 요약 대조를 했는지. **목록의 시각 도장에만 쓴다** (`nextGenerated`) —
      // 항목을 치우는 데는 안 쓴다. 대조가 보는 원문은 그 회차의 새 메시지뿐이라
      // 「다시 안 잡힘」이 「해소됨」을 뜻하지 않는다 (pending-work.js 의 mergeItems).
      summaryChecked: !skipSummary && touched.length > 0,
    });

    /* 사람이 이미 「뺌·나중에」로 정한 요약 후보는 DM 에서도 부르지 않는다.
     * 안 빼면 「미뤄 두면 만기까지 조용합니다」라고 안내해 놓고 다음 아침에 같은 건의
     * 이름을 다시 부르게 된다 — 2026-08-10 에 수정·삭제 쪽에서 실제로 그랬다. */
    if (result.summary?.findings?.length) {
      result.summary.findings = result.summary.findings.filter(
        (f) => !result.work.suppressedFindings.has(`${f.channel}|summary|${f.type}|${f.where || ''}`),
      );
    }
    console.log(
      `[ingest] 할 일 — ${result.work.total}건` +
        (result.work.fresh ? ` (오늘 새로 ${result.work.fresh}건)` : ''),
    );

    /* ④ 관문 */
    if (!dry) {
      result.gate = await runGate({
        probes: {
          channels: touched
            .filter((c) => c.probeHeader)
            .map((c) => ({ file: c.file, header: c.probeHeader })),
          // 덧붙인 답글은 새 메시지가 없는 채널에서도 생기므로 touched 가 아니라 전체에서 모은다
          threadReplies: (result.conversations.channels || []).flatMap((c) => c.replyProbes || []),
        },
      });

      if (!result.gate.passed) {
        console.error('[ingest] 관문 실패 — 되돌립니다.');
        for (const f of result.gate.failures) console.error(`  ✗ ${f.check}: ${f.detail.slice(0, 3).join(' | ')}`);
        await recover();
      } else if (await hasChanges(paths)) {
        /* ⑤ 커밋·push — **새 대화가 0건이어도 바뀐 것이 있으면 커밋한다.**
         * `.sync-state.json` 의 `last_sync` 는 새 메시지가 없어도 매번 새로 쓰인다
         * (slack-archive.js). 조용한 날 그것을 안 남기면 작업 트리가 더러운 채로 남고,
         * 다음 실행의 syncBeforeWork 가 거기서 멈춰 자동 반영이 영영 안 돈다. */
        const added = result.conversations.totalAdded;
        const replies = result.conversations.totalReplies || 0;
        const fixes = result.conversations.totalSelfCorrections || 0;
        const botReplies = result.conversations.totalBotReplies || 0;
        const msg = buildCommitMessage({
          added,
          replies,
          fixes,
          botReplies,
          logged: result.log?.changed || 0,
          derived: result.derive?.changed?.length || 0,
          channels: result.conversations.channels,
        });
        commitStarted = true;
        result.push = await commitAndPush(msg, paths);
        // 이 커밋은 사람에게 알릴 것이 아니다 (report.js 가 이 표시를 보고 조용히 넘어간다).
        result.push.stateOnly = !added && !replies && !fixes && !botReplies;
        console.log(
          result.push.pushed
            ? `[ingest] 커밋·push 완료 (${result.push.sha})`
            : `[ingest] push 실패 — ${result.push.reason || result.push.reason}`,
        );
      }
    }
  } catch (err) {
    result.fatal = errorText(err);
    console.error('[ingest] 중단 —', result.fatal);
    await recover();
  } finally {
    // Preserve the in-memory usage independently of the logging outcome.
    // append has no durable acknowledgement; never retry an ambiguous write
    // or pass the same accounting to the scheduler for another cost record.
    if (summaryAccounting?.records.length) {
      result.summaryAccounting = summaryAccounting;
      result.accountingLog = dry ? 'not-persisted' : 'attempted';
      try {
        if (dry) {
          console.log('[ingest] --dry 요약 대조 비용 (저장 안 함): ' + JSON.stringify(usageFields(summaryAccounting)));
        } else {
          logConversation({
            kind: 'ingest', accountingOnly: true, origin: '요약 대조 비용', target: '(모델 사용량 기록 — 발송 아님)',
            ok: !result.fatal && !(result.summary?.failed?.length),
            ...usageFields(summaryAccounting), elapsedMs: Date.now() - started,
          });
        }
      } catch (err) {
        result.accountingLog = 'unknown';
        secondary('accounting', err);
      }
    }
    try { clearTemp(); } catch (err) { secondary('cleanup', err); }
  }

  console.log(`[ingest] 끝 (${((Date.now() - started) / 1000).toFixed(1)}s)`);

  const finish = (sent, text) => {
    const classified = classifyFailure(result);
    const additional = (result.secondaryErrors || []).map((e) => `[${e.stage}] ${e.error}`);
    if (result.reportDelivery?.status === 'unknown') additional.push('보고는 일부 전달됐을 수 있습니다. 원문을 자동 재전송하지 않았습니다.');
    return {
      ...result, ...sent, ...(text === undefined ? {} : { text }),
      ...(classified ? {
        ...classified,
        error: [classified.error, ...additional].filter(Boolean).join(' · ').slice(0, 500),
        context: stepsReached(result),
        origin: skipSummary ? '요약 전 회차' : '아침 회차',
      } : {}),
    };
  };

  // Report failures are data, not a second exception hiding the first failure.
  let text;
  try { text = compose(result); } catch (err) {
    secondary('compose', err);
    result.reportDelivery = { status: 'not-sent', confirmedParts: 0, attemptedParts: 0 };
    return finish({ sent: false });
  }
  if (!text) {
    result.reportDelivery = { status: 'not-sent', confirmedParts: 0, attemptedParts: 0 };
    return finish({ sent: false, reason: 'nothing-to-report' });
  }
  try {
    const sent = await send(client, text, { dry });
    result.reportDelivery = sent.reportDelivery || {
      status: dry ? 'dry-run' : sent.sent ? 'sent' : 'not-sent',
    };
    return finish(sent, text);
  } catch (err) {
    secondary('send', err);
    result.reportDelivery = err?.hermesReport || { status: dry ? 'dry-run' : 'unknown' };
    return finish({ sent: false }, text);
  }
}
