import { createQuestionAnswerer } from './llm/qa.js';
import { assertOwnsRole, ROLES } from './mode.js';
/**
 * Claude 계층 — 툴 루프(Q&A)와 요약 생성.
 *
 * 프롬프트 캐싱 전제: system 블록은 "지시문 + 아카이브 색인" 만 담는다.
 * 오늘 날짜·질문자·출처 채널처럼 매번 바뀌는 값은 user 메시지로 보낸다.
 * (변하는 값이 앞에 끼면 그 뒤 캐시가 통째로 무효화된다)
 */
import { createPromptContext } from './llm/prompts.js';
import Anthropic from '@anthropic-ai/sdk';
import { createDigestGenerator } from './llm/digest.js';
import { createTextModel, createJsonModel, createToolSession } from './llm/provider.js';
import { createSummaryChecker } from './llm/summary-check.js';
import { createToolBuilder } from './llm/tools.js';
import { lazyTerms as domainTerms } from './domain.js';
import {
  ROOT, DATA_ROOT, ARCHIVE_DIR, DOCS_DIR, config, accessLabel, canSeePrivateChannel, canSee,
  isPrivateChannel, matchesHiddenPrivate, BLOCKED_NOTE, BOT_ANSWER_MARK, truncMarker, DOMAIN,
} from './config.js';
import {
  buildArchiveBriefSplit, searchArchive, readChannel, resolveChannel, listArchivedChannels,
  listReadableChannels,
} from './archive.js';
import {
  buildDocumentsBriefSplit, searchDocuments, readDocument, hasDocuments, markArchivedAttachments,
  resolveProject, narrowableProjects,
} from './documents.js';
import { detectPlace } from './search-terms.js';
import {
  fetchWindow, formatTranscript, recentWindow, listBotChannels, DEFAULT_LIVE_FETCH_MAX_DAYS,
} from './slack-live.js';
import { logUsage, estimateCost } from './format.js';

// 지연 생성 — 모듈을 import 하는 것만으로 API 키를 요구하지 않게 한다.
// (키가 없을 때는 requireEnv 가 먼저 친절한 안내를 띄운다)
let _client;
const anthropic = () => (_client ??= new Anthropic());

// 도구 왕복 창구 — Q&A 가 쓴다. answerQuestion 배선보다 **위**여야 한다.
const toolSession = (spec) => createToolSession(spec, { client: anthropic });

// One cache owner shared by Q&A and the public compatibility exports.
const promptContext = createPromptContext({
  ROOT, DATA_ROOT, ARCHIVE_DIR, DOCS_DIR, config, accessLabel, canSeePrivateChannel,
  buildArchiveBriefSplit, buildDocumentsBriefSplit, hasDocuments, listArchivedChannels,
  // 환경변수가 설정 파일을 이기는 판정은 `config.js` 하나다.
  domain: DOMAIN,
});
export const { renderPrompt, lastSyncedAt, briefStampPaths } = promptContext;
const { systemBlocks, privateQuoteLine } = promptContext;
const promptFile = renderPrompt;

export const { autoNarrow, buildTools } = createToolBuilder({
  terms: domainTerms(DOMAIN),
  config, canSee, canSeePrivateChannel, isPrivateChannel, matchesHiddenPrivate,
  BLOCKED_NOTE, truncMarker, searchArchive, readChannel, resolveChannel,
  listReadableChannels, searchDocuments, readDocument, hasDocuments,
  markArchivedAttachments, narrowableProjects, resolveProject, detectPlace,
  fetchWindow, formatTranscript, recentWindow, listBotChannels, DEFAULT_LIVE_FETCH_MAX_DAYS,
});

// Keep the public facade and a single cross-question diagnostics owner.
const answerWithLocalArchive = createQuestionAnswerer({
  terms: domainTerms(DOMAIN),
  config, toolSession, buildTools, systemBlocks, lastSyncedAt, privateQuoteLine,
  BOT_ANSWER_MARK, logUsage, estimateCost,
});

/**
 * PF 독립 런타임의 Q&A. TYBot 연동은 이 함수를 실행하지 않고 계약 프롬프트와
 * TYBot ToolBox 를 쓴다. 직접 모듈 호출도 그 경계를 우회하지 못하게 여기서 막는다.
 */
export async function answerQuestion(args) {
  assertOwnsRole(ROLES.ANSWER, 'Hermes 독립 Q&A(answerQuestion)');
  return answerWithLocalArchive(args);
}

/** 시도 이력·실패 집계 표에 적을 사유 짧은 이름. **판정은 여기 한 곳뿐이다** — scheduler.js 의
 * catch 도 이걸 쓴다(2026-08-28). 전에는 scheduler.js 가 `err?.type` 만 봐서, type 이 없는
 * 연결 오류(APIConnectionError)가 이름을 잃고 집계 표에서 `(기타)`로 묻혔다(실측 재현).
 *
 * 우선순위는 「가변 문구에 안 흔들리는 값」부터다 — type(Anthropic API) → code(슬랙 SDK,
 * `slack_webapi_platform_error` 등 — 실제로 그런지 확인함, node_modules/@slack/web-api/dist/errors.js)
 * → status(HTTP) → constructor.name(APIConnectionError 등). **err.name 은 안 쓴다** — 실제로
 * 찍어 보면 Anthropic SDK 에러는 전부 'Error' 로 뭉개져 있다(SDK 가 this.name 을 안 덮어씀).
 * constructor.name 은 클래스별로 정확한 값을 준다.
 *
 * 마지막 보루는 메시지인데, **그대로 60자를 자르지 않는다.** git.js 가 던지는
 * `git push 실패: <stderr>` 처럼 우리가 던지는 에러는 고정된 앞부분 뒤에 매번 바뀌는 텍스트가
 * 붙는데, 그대로 자르면 같은 종류의 실패가 회차마다 다른 이름이 되어 집계 표에서 행이
 * 쪼개진다(이번 작업의 사유). 그래서 콜론·줄바꿈·줄표 중 처음 나오는 자리에서 끊어 고정된
 * 부분만 쓴다. `new Error(...)` 는 constructor.name 도 'Error' 라 이 갈래까지 내려온다.
 *
 * 길이 상한 40자는 실측한 구조화 값 중 가장 긴 것(슬랙 rate_limited 코드 31자)도 안 잘리게
 * 넉넉히 뒀다 — 더 긴 슬랙 코드(파일 업로드 전용, 43·45자)는 이 코드베이스가 슬랙 파일
 * 업로드를 쓰지 않아 실제로 나올 일이 없다. */
export function errLabel(err) {
  if (err?.type) return err.type;
  if (err?.code) return String(err.code);
  if (err?.status != null) return `http_${err.status}`;
  const ctorName = err?.constructor?.name;
  if (ctorName && ctorName !== 'Error') return ctorName;
  const msg = String(err?.message || 'unknown');
  const stable = msg.split(/[:\n—]/)[0].trim();
  return (stable || msg).slice(0, 40);
}

// 모델 제공사 창구 — Q&A 와 같은 지연 클라이언트를 물려 만든다.
// 검사(check-llm-summary.js)가 createTextModel 을 같은 이름으로 주입한다.
const textModel = (spec) => createTextModel(spec, { client: anthropic });
const jsonModel = (spec) => createJsonModel(spec, { client: anthropic });

// 기존 호출자는 이 진입점을 계속 쓴다. 모델 클라이언트도 Q&A와 같은 지연 생성 인스턴스다.
export const compareSummary = createSummaryChecker({ config, jsonModel, promptFile, logUsage, estimateCost });
export const generateDigest = createDigestGenerator({
  config, textModel, promptFile, logUsage, estimateCost, errLabel,
});
