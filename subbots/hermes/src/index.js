import { createSlackQuestion } from './slack/question.js';
import { createSlackAccess } from './slack/access.js';
import { usageFields } from './llm/usage.js';
/**
 * Hermes — 진입점.
 *
 *   npm start
 *
 * Socket Mode 로 슬랙에 붙으므로 공개 URL·포트포워딩·방화벽 설정이 필요 없다.
 * 이 프로세스가 떠 있는 동안에만 동작한다 (PC 절전/재부팅 시 중단).
 */
import pkg from '@slack/bolt';
import { config, requireEnv, normalizeChannel, canonicalChannel, accessFor, accessLabel, PUBLIC_ACCESS } from './config.js';
import { assertArchive } from './archive.js';
import { answerQuestion, lastSyncedAt } from './claude.js';
import { fetchThreadContext, isBotMessage, SKIP_SUBTYPES, resolveChannelMentions } from './slack-live.js';
import { toSlackMrkdwn, chunkForSlack } from './format.js';
import { append as logConversation, permalink } from './convo-log.js';
import { startScheduler } from './scheduler.js';
import { assertOwnsRole, ROLES } from './mode.js';

const { App } = pkg;

// TYBot 연동은 이 Slack 프로세스를 띄우는 방식이 아니다. 계약 프롬프트를 TYBot의
// 권한 도구로 호출한다. 여기까지 열어 두면 같은 질문에 두 봇이 답하고, 이 프로세스는
// 자기 로컬 아카이브를 읽어 TYBot 권한 경계를 우회한다.
try {
  assertOwnsRole(ROLES.ANSWER, 'Hermes 독립 Slack 서비스(npm start)');
} catch (err) {
  console.error(err.message);
  process.exit(2);
}

const env = requireEnv(['SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'ANTHROPIC_API_KEY']);
assertArchive();

const app = new App({
  token: env.SLACK_BOT_TOKEN,
  appToken: env.SLACK_APP_TOKEN,
  socketMode: true,
});

/* ── 비공개 접근 판정 ──────────────────────────────────────────
 * 원칙: "이미 그 채널을 읽을 수 있는 사람에게만, 그 채널 것만."
 *  - 공개 채널     → 비공개 전부 금지 (답변이 그 채널 전원에게 보이므로 질문자가 누구든 무관)
 *  - 비공개 채널 안 → **그 채널 하나만.** 다른 비공개 채널은 멤버가 다르므로 금지
 *  - DM            → 그 사람이 **실제로 멤버인** 비공개 채널만
 *
 * 비공개 채널마다 따로 판정하는 게 핵심이다. 예전에는 멤버를 한 집합으로 합쳐 두고
 * "비공개 하나라도 멤버면 전부 허용" 이었다. 어느 비공개 채널 멤버가 **다른**
 * 비공개 채널 내용을 받아볼 수 있었다 (WHK 지시로 2026-08-05 수정).
 * 그 실물은 자료 저장소 `사고기록.md` 의
 * 「비공개 채널이 서로도 막혀야 하는 이유」 절에 있다.
 */
const memberCache = { at: 0, byChannel: new Map() };

// Codex R3e: preserve the singleton cache and existing authorization policy.
const { resolveAccess } = createSlackAccess({
  config, normalizeChannel, canonicalChannel, accessFor, PUBLIC_ACCESS, memberCache, console,
});

/* ── 질문 처리 ────────────────────────────────────────────────── */

// Codex R3f: preserve question delivery and failure accounting order.
const { handleQuestion } = createSlackQuestion({
  resolveAccess, accessLabel, fetchThreadContext, answerQuestion,
  toSlackMrkdwn, chunkForSlack, logConversation, permalink, usageFields, console,
  resolveChannelMentions,
});

app.event('app_mention', async ({ event, client }) => {
  // DM 안에서 @Hermes 를 붙이면 슬랙이 app_mention 과 message.im 을 둘 다 보낸다.
  // 그대로 두면 같은 질문에 두 번 답하고 API 비용도 두 배가 된다. DM 은 아래 message 핸들러에 맡긴다.
  if (event.channel_type === 'im') return;
  await handleQuestion({ client, event, text: event.text || '' });
});

/* DM 만 받는다.
 *
 * **`subtype` 이 붙었다고 다 버리면 안 된다.** 슬랙은 첨부가 딸린 메시지에 `file_share` 를
 * 붙이므로, 그러면 **파일을 붙여 DM 으로 물은 질문이 통째로 버려진다** — 응답도 에러도
 * 로그도 없다. 같은 문장을 채널에 쓰면 답이 오니 사람 쪽에서는 재현 조건을 찾을 수 없다.
 * (`app_mention` 은 DM 이면 먼저 빠지므로 우회로도 없다 — 위 핸들러.)
 *
 * 그래서 **버릴 것만 이름으로 적는다.** 참여·이름변경 같은 로그성 subtype 은
 * `slack-live.js` 의 `SKIP_SUBTYPES` 가 이미 갖고 있으니 그것을 쓴다 — 여기서 새 목록을
 * 만들면 실시간 경로와 DM 경로가 「subtype 이란 무엇인가」에 서로 다른 답을 갖게 된다.
 * 수정·삭제 이벤트(`message_changed`·`message_deleted`)는 질문이 아니라 편집 알림이다.
 * 봇 판정도 마찬가지로 `isBotMessage` 한 곳에서 온다 (2026-08-12).
 */
const DM_SKIP_SUBTYPES = new Set([...SKIP_SUBTYPES, 'message_changed', 'message_deleted']);

app.message(async ({ message, client }) => {
  if (message.channel_type !== 'im') return;
  if (isBotMessage(message)) return;
  if (message.subtype && DM_SKIP_SUBTYPES.has(message.subtype)) return;
  await handleQuestion({ client, event: message, text: message.text || '' });
});

app.error(async (error) => {
  console.error('Bolt 오류:', error);
});

/* ── 기동 ─────────────────────────────────────────────────────── */

(async () => {
  await app.start();
  const auth = await app.client.auth.test();

  console.log('\n' + '='.repeat(52));
  console.log(`Hermes 실행 중`);
  console.log(`  워크스페이스 : ${auth.team}`);
  console.log(`  봇           : @${auth.user}`);
  console.log(`  아카이브 동기화: ${lastSyncedAt()}`);
  console.log(`  비공개 채널  : ${config.privateChannels.map((c) => `#${c}`).join(', ')} (각 채널 멤버에게 그 채널만)`);
  console.log(`  모델         : Q&A ${config.models.qa.id}(${config.models.qa.effort}) / 주간 ${config.models.weekly.effort}`);
  console.log('='.repeat(52));
  console.log('  중지: 이 창에서 Ctrl+C\n');

  startScheduler(app.client);
})().catch((err) => {
  console.error('\n기동 실패:', err.data?.error || err.message);
  console.error('→ npm run check 로 설정을 먼저 점검하세요.\n');
  process.exit(1);
});
