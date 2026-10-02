/**
 * 문서 아카이브 계층 — 50-resources/documents/ 를 읽기 전용으로 다룬다.
 *
 * 슬랙 아카이브(archive.js)와 나란히 두되 섞지 않는다.
 * 저쪽은 "사람이 한 말", 이쪽은 "문서에 적힌 것" 이라 신뢰도도 수명주기도 다르다.
 * 한쪽 포맷을 고칠 때 다른 쪽이 조용히 깨지는 일을 막으려고 파일을 나눴다.
 *
 * 파싱은 archive.js 의 계약을 그대로 빌려 쓴다 —
 * 회차 헤더(**YYYY-MM-DD · 파일명**), 월 헤딩(## YYYY-MM), 상단 > 메타 블록.
 * 그래서 여기에는 새 파서가 없고 경로 계산과 필터만 있다.
 */
import fs from 'node:fs';
import path from 'node:path';
import {
  DOCS_DIR, DOC_PROJECTS_DIR, config, canSeePrivateChannel, isPrivateChannel, normalizeChannel,
  redactPrivateMentions, matchesHiddenPrivate, BLOCKED_NOTE, PUBLIC_ACCESS, companyWideDocProjects,
  TRUNC_PHRASE, truncMarker, currentChannelNames,
} from './config.js';
import {
  readCached, fold, splitMessages, metaBlock, preambleOf, preambleOutline,
  pickSpread, spreadNote, byScoreThenDate,
} from './archive.js';
import {
  splitTerms, scoreTerms, termHits, PARTIAL_MIN_TERMS, clipPartial,
} from './search-terms.js';

import { createDocumentRead } from './documents/read.js';
import { createDocumentSearch } from './documents/search.js';
import { createDocumentBrief } from './documents/brief.js';
import { createDocumentAccess } from './documents/access.js';
import { createDocumentStore } from './documents/store.js';
import { SHEET_RE, entryFileName, sheetsOf, sectionsOf } from './documents/parse.js';
export { SHEET_RE, entryFileName, sheetsOf, sectionsOf };

const limits = config.limits || {};
const DOC_SEARCH_MAX_HITS = limits.docSearchMaxHits ?? 12;
const DOC_HIT_MAX_CHARS = limits.docHitMaxChars ?? 4000;
// 부분 일치(아래 partials 분기)는 "열어볼 값어치가 있나" 만 보이면 되고, 열면 read_document 가
// 전문을 준다. 확정 히트와 같은 예산을 주면 헛도는 질의가 비싸진다 — 실측 47,203자/호출
// (2026-08-17). 확정 히트(DOC_HIT_MAX_CHARS)는 그대로 두고 이 값만 따로 쓴다.
const PARTIAL_HIT_MAX_CHARS = limits.partialHitMaxChars ?? 1000;
/* 재보기(scripts/run-log-measure.js)가 「6만 자에 걸렸나」를 이 값으로 가른다 —
 * 거기 숫자를 또 적으면 상한을 바꾸는 날 조용히 갈린다. */
export const DOC_READ_MAX_CHARS = limits.documentReadMaxChars ?? 60000;
// 검색이 0건일 때 대신 보여 줄 그 사업장의 문서 제목 개수. 상한을 두는 것은 정기보고가
// 쌓인 사업장에서 제목만으로 답 하나를 다 먹지 않게 하려는 것이다.
const DOC_LIST_MAX = limits.docListMaxTitles ?? 25;
// 색인은 매 질문의 시스템 프롬프트에 통째로 실린다. 넘치면 오래된 단발부터 접는다.
const DOC_BRIEF_MAX_CHARS = limits.docBriefMaxChars ?? 6000;
// 추가 열람분(비공개 채널 문서)의 몫. 공개 몫과 **따로** 두는 것이 요점이다 —
// 한 예산을 나눠 쓰면 비공개 채널 멤버는 공개 문서가 더 접힌 색인을 보게 되고,
// 그러면 공개 부분이 사람마다 달라져 프롬프트 캐시가 안 걸린다 (buildDocumentsBriefSplit).
const DOC_BRIEF_PRIVATE_MAX_CHARS = limits.docBriefPrivateMaxChars ?? 1500;

/* 접힘 줄에 적는 종류의 개수 상한.
 *
 * 전부 적으면 이 줄이 **문서를 넣을 때마다** 자란다 — 새 종류 하나에 약 7자이고,
 * 이 줄은 접히지 않으므로 그만큼 바닥이 영구히 오른다 (2026-08-25 실측 건당 8.4자).
 * 문서를 넣는 것이 이 시스템의 본업이므로, 본업이 한도를 갉아먹으면 안 된다.
 * `scripts/check-doc-fold-line.js` 가 이 성질을 지킨다. */
const FOLD_KINDS_MAX = 3;
const DOC_BRIEF_RECENT_MONTHS = limits.docBriefRecentMonths ?? 3;

/* 절 목차를 내밀 최소 덮음률. 첫 절보다 위에 있는 구간은 어느 절에도 안 들기 때문에,
 * 이 값이 낮으면 봇이 문서의 일부만 보고 전부로 읽는다 (아래 outlineOf 주석의 8%·15% 실측). */
const SECTION_OUTLINE_MIN_COVER = 0.7;


/** 문서 아카이브가 켜져 있고 실제로 존재하는지 */
export function hasDocuments() {
  return Boolean(DOC_PROJECTS_DIR && fs.existsSync(DOC_PROJECTS_DIR));
}

/**
 * 사업장 목록. 폴더 한 겹 아래까지만 본다 ('_공통/정기보고' 처럼).
 * 더 깊이 파지 않는 것은 의도다 — 깊어지면 이름이 길어져 봇이 지정하기 어려워진다.
 */
export function listProjects() {
  if (!hasDocuments()) return [];
  const out = [];
  for (const top of fs.readdirSync(DOC_PROJECTS_DIR, { withFileTypes: true })) {
    if (!top.isDirectory()) continue;
    const topPath = path.join(DOC_PROJECTS_DIR, top.name);
    const children = fs.readdirSync(topPath, { withFileTypes: true });
    const subDirs = children.filter((c) => c.isDirectory());
    const hasOwnMd = children.some((c) => c.isFile() && c.name.endsWith('.md'));
    if (hasOwnMd || !subDirs.length) out.push(top.name);
    for (const sub of subDirs) out.push(`${top.name}/${sub.name}`);
  }
  return out.sort((a, b) => a.localeCompare(b, 'ko'));
}

function projectDir(project) {
  return path.join(DOC_PROJECTS_DIR, ...project.split('/'));
}

// Access construction is lazy: listDocuments may use the store initialized below.
const { projectIsPrivate, projectPrivateChannel, maskProject, realProjects, documentsFor, canSeeDoc, visibleProjects, resolveProjectFor, resolveDocumentFor } = createDocumentAccess({
  isPrivateChannel, normalizeChannel, canSeePrivateChannel, listProjects, listDocuments, resolveProject, resolveDocument, matchesHiddenPrivate, BLOCKED_NOTE
});

// One parsed-document cache for this facade; creation performs no reads.
// channelMap: 캐시 키에 개명 지도의 지금 판을 섞는다 — store.js 의 docCache 주석이 원본.
const { loadDocument } = createDocumentStore({
  fs, path, projectDir, projectPrivateChannel, projectIsPrivate, readCached, metaBlock, splitMessages, preambleOf, channelMap: currentChannelNames
});

/** 사업장(생략 시 전체)의 문서 목록 */
export function listDocuments(project) {
  if (!hasDocuments()) return [];
  const projects = project ? [project] : listProjects();
  const out = [];
  for (const p of projects) {
    const dir = projectDir(p);
    if (!fs.existsSync(dir)) continue;
    for (const f of fs.readdirSync(dir)) {
      if (f.endsWith('.md')) out.push(loadDocument(p, f));
    }
  }
  return out;
}

/**
 * 자동 좁히기(질의에 자리 이름이 있으면 검색을 그 자리로 좁히는 기능)가 쓰는 후보 목록.
 *
 * **가려질 이름은 아예 후보에 넣지 않는다.** `maskProject(p, access) === p` 로 거른다 —
 * 볼 수 없는 비공개 채널의 폴더는 그 안에 `[공개]` 승인 문서가 섞여 있어도 후보에서
 * 뺀다. 그 승인 문서 자체는 가상 이름(`_승인자료`)으로 여전히 읽을 수 있다 — 그게
 * 설계된 길이다(「문서는 살리고 이름만 가린다」, 2026-08-10). `listDocuments(p).some(...)`
 * 만으로 거르면 승인 문서 한 건 때문에 가려야 할 폴더 이름이 후보로 새는데, 아래 계층
 * (`resolveProjectFor`)이 가려진 목록으로 다시 막아 주리라 기대하는 것은 이번에 고치는
 * 결함의 형태다 — 볼 수 없는 이름은 아래에서 막는 게 아니라 **후보 단계에서** 없앤다.
 *
 * `listProjects()` 를 그대로 넘기면 디스크의 폴더가 전부 후보가 되어, 볼 수 없는
 * 비공개 폴더로도 좁혀진다 — 검색 자체는 `canSeeDoc`/`BLOCKED_NOTE` 가 막아도,
 * **좁히기가 발동했다는 사실 자체**가 「그 이름의 자리가 있다」를 알려주는 신호가
 * 된다(존재 확인 오라클, 2026-09-16 외부 검토). 이 함수가 없으면 자동 좁히기가
 * `access` 를 조용히 버리고 전체 폴더를 후보로 삼는 것이 그 사고다.
 *
 * `visibleProjects` 와 달리 **가상 이름(`_승인자료`)으로 뭉치지 않고 실명을 낸다** —
 * 좁히기는 검색 함수에 실제 폴더 이름을 넘겨야 해서, 가상 이름을 주면 애초에 안 풀린다.
 */
export function narrowableProjects(access) {
  return listProjects().filter((p) => maskProject(p, access) === p
    && listDocuments(p).some((d) => canSeeDoc(access, d)));
}

/* ── 첨부 「본문 수록」 표시 ───────────────────────────────────── */

/**
 * 채널 md 의 첨부 줄에 붙는 표시. 검사(check-attachment-marks.js)가 이 상수를 쓴다.
 *
 * 짧게 둔 이유는 토큰이다 — 대화 검색 결과의 6.7%(30자짜리 꼬리)가 아니라 1% 아래로
 * 들어간다. 여기서 알려야 하는 것은 「본문이 있다」 한 가지뿐이고, 어느 문서인지는
 * 봇이 `search` 나 `read_document` 로 찾으면 된다.
 */
export const ARCHIVED_MARK = '📄수록';

/**
 * 본문이 문서 아카이브에 들어 있는 **원본 첨부 파일명**들. 권한을 반영한다.
 *
 * 회차 헤더 `**YYYY-MM-DD · 원본파일명**` 에 슬랙에 올라온 그대로의 파일 이름이 남아 있어
 * 채널 md 의 `📎 첨부: \`파일명\`` 과 **글자까지 그대로** 맞춰볼 수 있다 (2026-08-18 실측
 * 626건 중 394건이 이 방식으로 맞았다). 사람이 고쳐 쓴 문서 제목(slug)이 아니라 원본
 * 이름이라는 점이 요점이다 — slug 로는 못 맞춘다.
 *
 * **이름을 뽑는 일은 `entryFileName` 에 맡긴다.** 여기 정규식을 직접 적었을 때 엑셀 헤더의
 * 시트 꼬리를 몰라서 엑셀 첨부 50자리가 통째로 표시를 잃었다 (2026-09-03 · SHEET_TAIL 주석).
 *
 * **`canSeeDoc` 을 거치는 것이 이 함수의 절반이다.** 표시 자체가 「그런 자료가 있다」는
 * 사실이라, 볼 수 없는 비공개 문서에 표시가 붙으면 내용을 한 글자도 안 보여주고도 존재가
 * 샌다. 비공개 채널 규칙(qa.md)이 이름조차 밝히지 말라고 하는 것과 같은 자리다.
 *
 * **이름은 NFC 로 맞춰서 담는다** (2026-09-03). 한글은 같은 글자를 두 가지로 적을 수
 * 있어서(완성형 한 글자 / 자모 셋을 이어 붙인 조합형), **눈으로는 똑같은데 `Set.has` 가
 * false 다.** 채널 md 이름은 슬랙에서 온 것이라 완성형인데, 문서 md 의 회차 헤더에는
 * 맥에서 만든 파일명이 섞여 조합형이 들어온다. 실측 2026-09-03: 채널 md 의 첨부 언급
 * 772자리 중 **1자리**가 이것 하나로 「수록」 표시를 잃고 있었다 — 에러는 안 난다.
 * 붙이는 쪽(`markArchivedAttachments`)도 **같이** 맞춰야 한다. 한쪽만 고치면 그대로다.
 */
function archivedAttachmentNames(access) {
  const names = new Set();
  for (const d of listDocuments()) {
    if (!canSeeDoc(access, d)) continue;
    for (const e of d.entries) {
      const name = entryFileName(e);
      if (name) names.add(name.normalize('NFC'));
    }
  }
  return names;
}

/**
 * 채널 md 텍스트의 `📎 첨부:` 줄에서, 본문이 아카이브에 있는 파일 뒤에 표시를 붙인다.
 *
 * 없으면 아무 표시도 안 붙인다 — 「미수록」이라고 적지 않는다. 아직 안 변환된 것과 영영
 * 못 넣는 것(엑셀·이미지)을 여기서 가를 수 없는데, 적어 두면 봇이 그 차이를 지어낸다.
 * 표시가 없는 것에 대해서는 qa.md 가 「확인하지 않았으면 아무 말도 붙이지 마라」로 받는다.
 *
 * 첨부 줄만 건드린다. 대화 본문에도 백틱이 흔해서(파일명·코드·인용) 줄을 안 가리면
 * 엉뚱한 곳에 표시가 붙는다.
 *
 * **맞춰보기 전에 NFC 로 맞춘다** — 이유는 위 `archivedAttachmentNames` 주석에 있다.
 * 두 곳이 **같은 정규화**를 써야 한다. 한쪽만 고치면 아무것도 안 달라진다.
 */
export function markArchivedAttachments(text, access) {
  if (!text || !text.includes('📎 첨부:') || !hasDocuments()) return text;
  const names = archivedAttachmentNames(access);
  if (!names.size) return text;
  return text
    .split('\n')
    .map((line) => (line.includes('📎 첨부:')
      ? line.replace(/`([^`]+)`/g, (whole, name) => (names.has(name.trim().normalize('NFC')) ? `${whole} ${ARCHIVED_MARK}` : whole))
      : line))
    .join('\n');
}

/* ── 이름 풀기 ────────────────────────────────────────────────── */

/**
 * 줄여 적은 이름 → 실제 사업장 이름, '정기보고' → '_공통/정기보고'.
 * archive.js 의 resolveChannel 과 같은 fold 규칙을 쓴다.
 */
export function resolveProject(input, all = listProjects()) {
  const raw = normalizeChannel(input);
  if (all.includes(raw)) return { ok: true, name: raw };

  const target = fold(raw);
  if (!target) return { ok: false, candidates: [] };

  const exact = all.filter((p) => fold(p) === target || fold(p.split('/').pop()) === target);
  if (exact.length === 1) return { ok: true, name: exact[0] };

  const partial = all.filter((p) => fold(p).includes(target) || target.includes(fold(p)));
  if (partial.length === 1) return { ok: true, name: partial[0] };
  return { ok: false, candidates: partial.length ? partial : all };
}

/** 문서 하나를 부를 수 있는 이름 후보 전부 — 슬러그 · H1 제목 · **회차 원본 파일명**(확장자 제거).
 *
 * 파일명을 더한 이유(2026-09-11): 봇은 검색 발췌 첫 줄(회차 헤더)의 원본 파일명을
 * read_document 에 그대로 넣는데, 그 이름만 후보에 없어 첫 호출을 매번 버렸다.
 * 파일명 추출은 entryFileName 하나로 — 정규식 사본을 두면 붙이는 쪽과 재는 쪽이
 * 똑같이 틀려 검사가 초록으로 남는다(2026-09-03 SHEET_TAIL 사고).
 *
 * NFC: 파일명은 슬랙(맥 업로드)에서 조합형으로 올 수 있다 — 눈으로 같아도
 * 문자열 비교가 어긋난다(archivedAttachmentNames 의 2026-09-03 사고와 같은 자리). */
function resolveNames(d) {
  const names = [fold(String(d.slug).normalize('NFC')), fold(String(d.title).normalize('NFC'))];
  for (const e of d.entries || []) {
    const f = entryFileName(e);
    if (f) names.push(fold(f.normalize('NFC').replace(/\.[A-Za-z0-9]+$/, '')));
  }
  return names;
}

/** 사업장 안에서 문서 이름 풀기 */
export function resolveDocument(project, input, docs = listDocuments(project)) {
  const target = fold(String(input).normalize('NFC'));
  if (!target) return { ok: false, candidates: docs.map((d) => d.slug) };

  // 이름 후보는 문서마다 **한 번만** 만든다 — `resolveNames` 가 회차를 전부 도는데(문서당
  // 회차 수만큼 `entryFileName`·`normalize`·`fold`), exact·partial 두 곳에서 각각 부르면
  // 그 일이 통째로 두 번 돈다.
  const named = docs.map((d) => ({ d, names: resolveNames(d) }));

  const exact = named.filter(({ names }) => names.some((n) => n === target));
  if (exact.length === 1) return { ok: true, doc: exact[0].d };

  const partial = named.filter(({ names }) => names.some((n) => n.includes(target))).map(({ d }) => d);
  if (partial.length === 1) return { ok: true, doc: partial[0] };
  return { ok: false, candidates: (partial.length ? partial : docs).map((d) => d.slug) };
}

/* ── 색인 ─────────────────────────────────────────────────────── */

/** 문서의 가장 최근 회차 날짜 */
function latestDate(entries) {
  return entries.reduce((m, e) => (e.date > m ? e.date : m), '');
}

/** 시트로 나뉜 문서인가. 날짜가 같다는 식으로 짐작하지 않고 메타 한 줄로 가른다.
 *
 * **내보내는 이유는 판정을 두 벌 만들지 않기 위해서다** — `doc-index-audit.js` 가
 * 「엑셀 몇 건」을 여기서 가져다 쓴다. 거기서 다시 정의하면 색인이 세는 엑셀과
 * index.md 를 대보는 엑셀이 조용히 갈린다. */
export function isSheetDoc(doc) {
  return Boolean(doc.meta && doc.meta['시트']);
}

/** 시리즈 문서인가 — 엑셀이 아니면서 회차 2건 이상. 엑셀은 회차가 여럿이어도
 * 시리즈가 아니다 — 시트를 세는 것이라 추이가 아니다.
 * 이걸 안 가르면 엑셀 한 건마다 절대 안 접히는 줄이 하나씩 생긴다(`foldRank` 의
 * rank 0 은 가장 마지막에 접히므로 — 아래 buildDocumentsBrief 참조).
 *
 * **내보내는 이유는 판정을 두 벌 만들지 않기 위해서다** — `doc-index-audit.js` 의
 * `countDocs` 가 여기서 가져다 쓴다. 거기서 다시 정의하면 색인이 접는 시리즈와
 * index.md 를 대보는 시리즈가 조용히 갈린다. */
export function isSeriesDoc(doc) {
  return !isSheetDoc(doc) && doc.entries.length >= 2;
}

/**
 * 머리말을 **검색 결과**에 실을 때 앞에 붙이는 줄. 대화 쪽 `PREAMBLE_MARK` 와 짝이지만
 * **문구가 일부러 다르다** — 저쪽이 막는 것은 「사람 발언으로 읽히는 것」이고, 여기서
 * 막는 것은 「바로 뒤 회차의 원본 파일 내용으로 읽히는 것」이다. 문서 검색 결과의 다른
 * 히트는 전부 `**YYYY-MM-DD · 원본파일명**` 으로 시작하는데 이 덩어리만 그렇지 않아서,
 * 표시가 없으면 봇이 이것을 어느 한 회차의 원문이라고 출처를 붙인다.
 */
const DOC_PREAMBLE_MARK = '**문서 md 상단 정리 — 사람이 회차들을 훑어 손으로 적어 둔 것입니다 (특정 원본 파일의 내용이 아닙니다)**';

// One lazy index builder; preserve the existing public facade and limit snapshots.
const { foldedLine, floorParts, indexGauge, buildDocumentsBrief, buildDocumentsBriefSplit } = createDocumentBrief({
  path, DOCS_DIR, readCached, hasDocuments, listProjects, listDocuments, canSeeDoc, maskProject, projectPrivateChannel, canSeePrivateChannel, redactPrivateMentions, PUBLIC_ACCESS, preambleOutline, latestDate, isSheetDoc, isSeriesDoc, DOC_BRIEF_MAX_CHARS, DOC_BRIEF_PRIVATE_MAX_CHARS, DOC_BRIEF_RECENT_MONTHS, FOLD_KINDS_MAX
});
export { foldedLine, floorParts, indexGauge, buildDocumentsBrief, buildDocumentsBriefSplit };

/* ── 검색 ─────────────────────────────────────────────────────── */

function clip(text, max) {
  return text.length > max ? `${text.slice(0, max)}\n${truncMarker()}` : text;
}

// Codex R2e: keep public search bindings and the shared store in this facade.
export const { searchDocuments, companyWideCards } = createDocumentSearch({
  limits, DOC_SEARCH_MAX_HITS, DOC_HIT_MAX_CHARS, PARTIAL_HIT_MAX_CHARS, DOC_LIST_MAX, DOC_PREAMBLE_MARK, TRUNC_PHRASE, companyWideDocProjects, hasDocuments, splitTerms, scoreTerms, termHits, PARTIAL_MIN_TERMS, clipPartial, resolveProjectFor, resolveDocumentFor, documentsFor, listDocuments, canSeeDoc, redactPrivateMentions, maskProject, clip, pickSpread, spreadNote, byScoreThenDate, fold, readCached, sectionsOf, isSeriesDoc, latestDate
});


/* ── 요약 재료 ─────────────────────────────────────────────────── */

// 요약 한 번에 실을 문서 발췌의 총 상한. 넘치면 오래된 것부터 요지만 남긴다.
// 코드 폴백은 config.json 에 값이 없을 때만 쓰인다 — 2026-08-10 사고(문서 5건이 8,000자
// 상한을 다 먹어 업무일지가 제목만 실린 것)로 15,000/3,000 으로 올렸고, config.example.json·
// 실물 config.json 은 이미 그 값이다. 이 코드 폴백만 사고 이전 값 그대로 남아 있었다
// (check-doc-digest-limits.js 가 2026-09-02 전수조사로 찾음 — WHK 결정 2026-09-03 로 맞춘다).
const DIGEST_DOC_MAX_CHARS = limits.digestDocMaxChars ?? 15000;
// 문서 한 건이 그 상한을 다 먹지 못하게 하는 몫 (searchMaxPerChannel 과 같은 취지).
const DIGEST_DOC_PER_DOC_CHARS = limits.digestDocPerDocChars ?? 3000;
// 이보다 적게 남았으면 본문을 싣지 않고 요지만 둔다. 몇 백 자짜리 조각은 문서의 첫 줄만
// 보여주고 나머지를 감춰서, 없느니만 못하게 오해를 부른다.
const DIGEST_DOC_MIN_CHARS = 400;

// Codex R2f: public API, original limits and shared loader remain facade-owned.
export const { outlineOf, readDocument, documentsUploadedIn } = createDocumentRead({
  fs, path, DOCS_DIR, DOC_READ_MAX_CHARS, DOC_HIT_MAX_CHARS, SECTION_OUTLINE_MIN_COVER, DOC_PREAMBLE_MARK, DIGEST_DOC_MAX_CHARS, DIGEST_DOC_PER_DOC_CHARS, DIGEST_DOC_MIN_CHARS, hasDocuments, resolveProjectFor, resolveDocumentFor, canSeeDoc, BLOCKED_NOTE, readCached, redactPrivateMentions, preambleOf, splitMessages, SHEET_RE, sectionsOf, fold, loadDocument, maskProject, clip
});
