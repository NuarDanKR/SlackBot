#!/usr/bin/env node
/* 검색 건수 상한의 생존률 측정 — 설계: 워크스페이스
 * docs/superpowers/specs/2026-09-12-검색부피-측정-design.md
 * 재생 방식(buildTools run)·분모 원칙(출신별)·편향 두 방향은 그 문서가 원본이다. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseEntryHeader, parseToolLine } from '../src/convo-log.js';

export function parseQaEntries(mdText) {
  const entries = [];
  let day = null;
  let cur = null;
  let inAnswer = false;
  // 이 PC 의 자료 저장소는 autocrlf 로 CRLF 체크아웃이다 — `\r` 를 안 벗기면
  // `line === '**답변**'` 와 날짜 헤더 정규식이 전부 지고, 근거·도구 줄 파서만 관대해서
  // 살아남아 163/66 게이트를 통과한 채 출처 149건이 조용히 전량 유실된다 (검증 실측).
  // src/archive.js:24-27 의 readCached 가 같은 이유로 같은 정규화를 한다.
  for (const line of mdText.replace(/\r\n/g, '\n').split('\n')) {
    const d = /^## (\d{4}-\d{2}-\d{2})$/.exec(line);
    if (d) { day = d[1]; continue; }
    const h = parseEntryHeader(line);
    if (h) {
      cur = { date: day, ...h, answerLines: [], sources: null, evidence: [], calls: [], privateAccess: null };
      entries.push(cur);
      inAnswer = false;
      continue;
    }
    if (!cur) continue;
    if (line === '**답변**') { inAnswer = true; continue; }
    if (line.startsWith('> **근거** ')) {
      const v = line.slice('> **근거** '.length).trim();
      cur.evidence = v.startsWith('(') ? [] : v.split(' · ');
      inAnswer = false;
      continue;
    }
    const t = parseToolLine(line);
    if (t) { cur.calls = t; inAnswer = false; continue; }
    if (line.startsWith('> ')) {
      const m = /비공개 근거 (.+?)(?: · |$)/.exec(line);
      if (m) cur.privateAccess = m[1].trim();
      inAnswer = false;
      continue;
    }
    if (inAnswer) {
      cur.answerLines.push(line);
      if (line.startsWith('— 출처:')) {
        cur.sources = splitSources(line.slice('— 출처:'.length).trim());
      }
    }
  }
  return entries.filter((e) => e.kind === '문답');
}

/* 출처 줄 분리 — 실물 149줄 중 75줄(50%)이 「📄 제목(2026-08-24, 시트 3/5)」처럼
 * 괄호 안에 `, ` 를 품는다 (검증 실측). 괄호 깊이 0 에서만 나누고,
 * `#a·#b·#c` 채널 나열(실물 2줄)은 낱개로 편다. */
export function splitSources(s) {
  const out = [];
  let depth = 0;
  let cur = '';
  for (let i = 0; i < s.length; i += 1) {
    const c = s[i];
    if (c === '(') depth += 1;
    if (c === ')') depth = Math.max(0, depth - 1);
    if (depth === 0 && c === ',' && s[i + 1] === ' ') { out.push(cur.trim()); cur = ''; i += 1; continue; }
    cur += c;
  }
  if (cur.trim()) out.push(cur.trim());
  return out.flatMap((x) => (!x.startsWith('📄') && x.includes('·') ? splitTop(x, '·') : [x]));
}

/** 괄호 깊이 0 에서만 한 글자 sep 로 나눈다 — splitSources 의 `,` 규칙과 같은 것. */
function splitTop(s, sep) {
  const out = [];
  let depth = 0;
  let cur = '';
  for (const c of s) {
    if (c === '(') depth += 1;
    if (c === ')') depth = Math.max(0, depth - 1);
    if (depth === 0 && c === sep) { out.push(cur.trim()); cur = ''; continue; }
    cur += c;
  }
  if (cur.trim()) out.push(cur.trim());
  return out.filter(Boolean);
}

/**
 * `LOG_DIR`(src/config.js export)의 `YYYY-MM.md` 전부를 읽어 문답 항목을 모은다.
 * `measure-hit-cap.js:36-54` 의 LOG_ENABLED 가드·파일 나열과 같은 모양.
 *
 * config.js 는 여기서 **동적 import** 로 가져온다 — 다음 Task 들이 「import 전 mutation」
 * 구조(env 를 먼저 바꾸고 그 뒤에 config 를 읽는 것)를 쓰므로, 이 파일 맨 위에서
 * 정적으로 import 해 버리면 그 구조가 안 된다. Task 1~3 은 순수 함수 + 픽스처만이라
 * 이 함수 자체는 아직 아무도 안 부른다(실물 대조는 Step 5 에서 `node -e` 로 따로 한다).
 */
export async function loadAllEntries() {
  const { LOG_DIR, LOG_ENABLED } = await import('../src/config.js');
  if (!LOG_ENABLED || !LOG_DIR) {
    console.error('대화 로그가 꺼져 있습니다 (config.json 의 log.enabled · log.path).');
    process.exit(2);
  }
  let files = [];
  try {
    files = fs.readdirSync(LOG_DIR).filter((n) => /^\d{4}-\d{2}\.md$/.test(n));
  } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }
  if (!files.length) {
    console.error(`재생할 로그가 없습니다: ${LOG_DIR}`);
    process.exit(2);
  }
  const entries = [];
  for (const f of files) {
    const text = fs.readFileSync(path.join(LOG_DIR, f), 'utf-8');
    entries.push(...parseQaEntries(text));
  }
  return entries;
}

const ONLY_VALUES = new Set(['archive', 'documents']);

/* 도구 줄은 입력의 값만 `, ` 로 잇는다(키 소실 — convo-log.js:257-258). query 가 맨
 * 앞이라는 것만 믿고, 뒤에서부터 where/document/only 를 알아보는 값만 걷어낸다.
 * 못 알아보는 토큰이 나오면 거기까지가 query 다 — query 속 쉼표를 where 로 오인하는
 * 쪽보다, where 를 query 에 남기는 쪽이 안전하다(후자는 기준 재생 겹침 검사에 걸린다). */
export function decomposeArgs(args, deps) {
  const { resolveChannelFor, resolveProject, resolveDocument, FULL_ACCESS } = deps;
  const tokens = args.split(', ');
  const input = { query: null };
  let end = tokens.length;
  while (end > 1) {
    const t = tokens[end - 1];
    if (!input.only && ONLY_VALUES.has(t)) { input.only = t; end -= 1; continue; }
    if (!input.where && (resolveChannelFor(t, FULL_ACCESS).ok || resolveProject(t).ok)) {
      input.where = t; end -= 1; continue;
    }
    /* 실물 키 순서(query→where→document)에서는 document 가 **마지막 토큰**이라, 그 앞
     * 토큰이 project 로 풀리고 마지막이 그 문서로 풀리면 둘을 짝으로 함께 걷는다.
     * (v1 은 where 를 먼저 안 것을 전제해 이 정상 순서가 안 풀렸다 — 검증 치명 ②) */
    if (!input.where && !input.document && end > 2) {
      const prev = tokens[end - 2];
      const pj = resolveProject(prev);
      if (pj.ok && resolveDocument(pj.name, t).ok) {
        input.where = prev; input.document = t; end -= 2; continue;
      }
    }
    break;
  }
  input.query = tokens.slice(0, end).join(', ');
  return { input, leftoverComma: input.query.includes(', ') };
}

/* 출처 제목과 slug 는 어순·접두어가 갈린다(설계서 §3.3 실물 예). 날짜 앵커로 먼저
 * 거르고, 정규화한 제목 조각의 포함 관계로 잰다. 유일할 때만 성공 — 둘 이상 걸리면
 * 고르는 것이 아니라 「못 쟀다」다.
 *
 * [수정 라운드 1 — 2026-09-12, 회의적 검수 오매칭] 원래 구현은 날짜가 안 맞으면
 * 후보를 그냥 버렸다. 그런데 slug 에 날짜가 아예 없는 후보는 그 필터를 **면제**받아
 * 겹침 50% 만으로 유일 후보가 될 수 있었고, 그 사이 진짜 후보는 날짜 오탈자
 * (출처 0813 vs slug 0806) 때문에 걸러져 사라졌다 — 겹침이 더 낮은 쪽이 이겼다
 * (실물: 2026-08-17 회차, 실명은 보고서에만 — 이하 전부 모양으로 설명). 아래 두 규칙을 더한다.
 * ① 날짜가 안 맞아 걸러진 후보의 겹침이 승자보다 크거나 같으면 승자를 못 믿는다 → null.
 * ② slug 에 날짜가 없어 날짜 검사를 면제받은 후보는 문턱을 50%→75% 로 올린다
 *    (면제는 공짜가 아니라 감점이어야 한다). 방향 원칙: 오매칭보다 「못 쟀다」가 낫다.
 *
 * [수정 라운드 2 — 2026-09-12, ①의 과잉교정] 재검수가 601건 기여도를 갈라 재니
 * 날짜 규칙(①+②)만으로 −32(503→471, fold 보강 +8 은 별도) — 그중 22건이 **원래
 * 올바른 매칭을 죽인 것**이었다. 원인은 ①이 방해 후보의 겹침을 **원시값 그대로,
 * 그 후보가 스스로 자격(문턱·fullMatch)을 갖췄는지 안 보고** 비교한 것 — 후보도
 * 못 되는 잡음(겹침 0.09·0.20)이나 **같은 제목의 다른 회차**가, 날짜까지 정확히
 * 맞은 승자를 거부했다. 두 가지를 좁힌다.
 * ①-a 승자의 날짜가 출처 날짜와 **정확히 일치**하면 ①을 아예 안 건다 — 날짜 앵커가
 *      이미 제 일을 했고, 원래 오매칭은 승자가 날짜 **면제**(slug 에 날짜 없음)인
 *      경우였다(위 ② 대상). 날짜가 맞은 승자를 다른 날짜의 후보가 거부할 이유가 없다.
 * ①-b ①을 걸 때도 방해 후보가 **스스로 자격을 갖춘 것**(fullMatch 이거나 겹침
 *      50% 이상)만 승자를 거부할 수 있다 — 후보도 못 되는 것이 승자를 거부하면 안 된다.
 *      비교 지표는 `score`(fullMatch 면 1, 아니면 ratio)로 통일한다 — fullMatch 는
 *      겹침 계산 방식이 달라 ratio 값이 낮게 나올 수 있어서다.
 *
 * [수정 라운드 3 — 2026-09-12, ①의 잔여 과잉교정 — 「동점 거부」] 라운드2 이후에도
 * 601건 중 6건이 여전히 죽어 있었다 — 전부 **누적 문서**(여러 회차를 품은, 날짜 없는
 * slug)가 승자인데, 같은 제목의 **단일 회차 문서**가 `score` **동점**(둘 다 fullMatch
 * ⇒ 1=1)으로 거부권을 행사한 모양이었다(실물 사례 6건, 보고서에 이름 있음). 검수가
 * 실물 확인 — 출처 날짜가 그 누적 문서의 실제 회차 목록 안에 있어
 * **승자가 맞았다.** 비교를 `>=`(동점도 거부) 에서 **`>`**(엄격히 우세할 때만 거부)로
 * 바꾼다 — 동점은 "더 나은 근거가 있다"고 볼 수 없으니 거부할 이유가 없다. */
function scoreDocMatch(s, evidence, fold) {
  const date = (/(\d{4})-(\d{2})-(\d{2})/.exec(s) || []).slice(1).join('');
  const titleBody = s.slice(2).replace(/\(?\d{4}-\d{2}-\d{2}\)?/, '');
  const title = fold(titleBody);
  const parts = titleBody.split(/[\s()\/·,]+/).map(fold).filter(Boolean);
  const scored = evidence
    .filter((e) => e.startsWith('📄'))
    .map((e) => {
      const slug = e.slice(2).trim().split('/').pop();
      const slugDate = (/^(\d{8})-/.exec(slug) || [])[1];
      const body = fold(slug.replace(/^\d{8}-/, ''));
      // 토큰 단위 부분합치: 제목의 정규화 조각이 slug 에 얼마나 들어 있는지 비율로 잰다.
      const inBody = parts.filter((p) => body.includes(p)).length;
      const ratio = parts.length > 0 ? inBody / parts.length : 0;
      const fullMatch = body.includes(title) || title.includes(body);
      // 'match'/'mismatch' 는 둘 다 날짜가 있어 대볼 수 있는 경우, 'unverifiable' 은
      // 출처엔 날짜가 있는데 slug 엔 없어 면제되는 경우(②의 감점 대상), 'no-signal' 은
      // 출처 자체에 날짜가 없어 애초에 대볼 게 없는 경우(기존 문턱 그대로).
      let dateStatus = 'no-signal';
      if (date && slugDate) dateStatus = date === slugDate ? 'match' : 'mismatch';
      else if (date && !slugDate) dateStatus = 'unverifiable';
      return { e, ratio, fullMatch, dateStatus, score: fullMatch ? 1 : ratio };
    });
  const dateOk = scored.filter((c) => c.dateStatus !== 'mismatch');
  const dateBad = scored.filter((c) => c.dateStatus === 'mismatch');
  const eligible = dateOk.filter((c) => c.fullMatch
    || c.ratio >= (c.dateStatus === 'unverifiable' ? 0.75 : 0.5));
  if (eligible.length !== 1) return { result: null, loose: false };
  const winner = eligible[0];
  // ①-a: 날짜가 정확히 맞은 승자는 상호검증을 안 받는다.
  // [라운드4] 방해 후보의 자격 검사는 두지 않는다 — score 정규화(fullMatch=1) 때문에
  // 자격 미달 후보(겹침<0.5, fullMatch 아님 ⇒ score<0.5)는 승자(항상 score>=0.5)를
  // 이길 수 없다(대수·601건 실증 둘 다 확인). 라운드3: 동점(score 같음)은 거부 못
  // 한다 — 엄격히 우세할 때만(>) 거부한다.
  if (winner.dateStatus !== 'match') {
    if (dateBad.some((c) => c.score > winner.score)) {
      return { result: null, loose: false };
    }
  }
  return { result: winner.e, loose: !winner.fullMatch };
}

export function matchSource(item, evidence, fold) {
  const s = item.trim();
  if (!s.startsWith('📄')) {
    const name = s.replace(/^#/, '').replace(/\s*\(.*\)\s*$/, '').trim();
    const hit = evidence.filter((e) => !e.startsWith('📄') && e.replace(/^#/, '') === name);
    return hit.length === 1 ? hit[0] : null;
  }
  return scoreDocMatch(s, evidence, fold).result;
}

/** 문서 매칭이 전체 제목 포함(fullMatch)이 아니라 토큰 겹침 문턱으로만 성공했는지 —
 * `--audit` 표본을 위험 지대(느슨한 매칭)로 좁히는 데만 쓴다. 채널 매칭은 항상
 * 완전 일치이므로 loose 가 될 수 없다(false). */
export function isLooseDocMatch(item, evidence, fold) {
  const s = item.trim();
  if (!s.startsWith('📄')) return false;
  return scoreDocMatch(s, evidence, fold).loose;
}

/* 잴 수 있는 축 — config.limits 의 키와 **글자 그대로** 같아야 한다. 오타를 그냥 넣으면
 * `config.limits` 에 없는 키가 하나 생길 뿐이라 **에러 없이 기준이 한 번 더 돈다**
 * (격자 표가 전부 같은 숫자로 차고 아무도 못 알아챈다). 그래서 `applyCap` 이
 * 「목록에 있나 · 지금 config 에 있나 · 지금 값보다 낮나」 셋을 다 본다.
 *
 * **이 두 줄이 파일 앞쪽에 있는 이유**: 아래 `--selftest` 블록이 이 둘을 부르는 함수를
 * 실행한다. `const` 를 뒤에 선언하면 그때는 아직 TDZ 라 자가검사가 ReferenceError 로
 * 죽는다 (`buildFold` 주석과 같은 이유). */
export const CAP_AXES = [
  'searchMaxHits', 'searchMaxPerChannel',
  'docSearchMaxHits', 'docSearchMaxPerProject',
  'partialHitMaxHits',
];

/* 격자 — 축별로 하나씩만 움직인다(설계서 §3.4). 전 조합 곱이 아니다. */
export const GRID = [
  ['searchMaxHits', 30], ['searchMaxHits', 20], ['searchMaxHits', 12],
  ['searchMaxPerChannel', 5], ['searchMaxPerChannel', 3],
  ['docSearchMaxHits', 8], ['docSearchMaxHits', 6], ['docSearchMaxHits', 4],
  ['docSearchMaxPerProject', 2],
  ['partialHitMaxHits', 8], ['partialHitMaxHits', 4],
];

const FIXTURE = `# 대화 로그
---

## 2026-09-02

### 15:58 · 담당자 · DM (1:1 대화) · 문답

**질문**

그 채널에 올라온 자료야

**답변**

말씀하신 자료는 이런 것이 있습니다.

— 출처: #예시채널, 📄 현안보고(예시몰) 2026-09-02

> **근거** #예시채널 · #보조채널 · 📄 예시몰/20260902-예시몰-할인분양-현안보고
> **도구** search(자료 공유, archive) 5,758자
> **비용** claude-opus-5 · 17.3초 · in 260 / out 617 · 약 $0.092
> 비공개 근거 공개만 · [슬랙에서 열기](https://example.com/p1)
`;

if (process.argv.includes('--selftest')) {
  const es = parseQaEntries(FIXTURE);
  assert.equal(es.length, 1);
  assert.equal(es[0].date, '2026-09-02');
  assert.equal(es[0].kind, '문답');
  assert.deepEqual(es[0].evidence, ['#예시채널', '#보조채널', '📄 예시몰/20260902-예시몰-할인분양-현안보고']);
  assert.equal(es[0].calls.length, 1);
  assert.equal(es[0].calls[0].name, 'search');
  assert.equal(es[0].privateAccess, '공개만');
  assert.deepEqual(es[0].sources, ['#예시채널', '📄 현안보고(예시몰) 2026-09-02']);

  assert.deepEqual(splitSources('#예시채널, 📄 [현황표] 요약본(2026-08-24, 시트 3/5 「요약」), 📄 현안보고(2026-09-02)'),
    ['#예시채널', '📄 [현황표] 요약본(2026-08-24, 시트 3/5 「요약」)', '📄 현안보고(2026-09-02)']);
  assert.deepEqual(splitSources('#예시채널·#보조채널'), ['#예시채널', '#보조채널']);
  assert.deepEqual(splitSources('#예시채널(8/5·8/13 담당자), 📄 현안보고(2026-09-02)'),
    ['#예시채널(8/5·8/13 담당자)', '📄 현안보고(2026-09-02)']);

  console.log('selftest OK');

  {
    // 이름은 전부 가짜 — Task 1 픽스처 주석과 같은 이유 (실물 이름은 pre-commit 이 막는다).
    const fakeDeps = {
      FULL_ACCESS: { full: true },
      resolveChannelFor: (t) => (t === '예시채널' ? { ok: true, name: '예시채널' } : { ok: false }),
      resolveProject: (t) => (t === '예시몰' ? { ok: true, name: '예시몰' } : { ok: false }),
      resolveDocument: (p, t) => (t === '현안보고' ? { ok: true } : { ok: false }),
    };
    const d = (s) => decomposeArgs(s, fakeDeps);
    assert.deepEqual(d('대출 만기 연장').input, { query: '대출 만기 연장' });
    assert.deepEqual(d('할인분양 보고, 예시채널').input, { query: '할인분양 보고', where: '예시채널' });
    assert.deepEqual(d('자료 공유, archive').input, { query: '자료 공유', only: 'archive' });
    assert.deepEqual(d('관리단집회 의안, 예시몰, documents').input,
      { query: '관리단집회 의안', where: '예시몰', only: 'documents' });
    assert.deepEqual(d('잔금 일정, 예시몰, 현안보고').input,
      { query: '잔금 일정', where: '예시몰', document: '현안보고' });
    assert.equal(d('예산 4,600억 증액, 모르는말').leftoverComma, true);
    console.log('selftest decompose OK');
  }

  {
    const fold = (s) => String(s).toLowerCase().replace(/[\s\-_()·.,/]/g, ''); // 자가검사용 근사 — 실행 시엔 archive.js 의 fold 주입
    // 이름은 가짜, 모양(어순 뒤바뀜·괄호 수식·접두어 차이)은 실물 대조 쌍 그대로 유지.
    const ev = ['#보조채널', '📄 예시몰/20260902-예시몰-꿈공장-할인분양-현안보고',
      '📄 보조몰/20260821-공사도급변경계약-3차-체결의-건'];
    assert.equal(matchSource('#보조채널', ev, fold), '#보조채널');
    assert.equal(matchSource('📄 현안보고(예시몰 꿈공장) 2026-09-02', ev, fold),
      '📄 예시몰/20260902-예시몰-꿈공장-할인분양-현안보고');
    assert.equal(matchSource('📄 보조 지식산업센터 공사도급변경계약(3차) 체결의 건(2026-08-21)', ev, fold),
      '📄 보조몰/20260821-공사도급변경계약-3차-체결의-건');
    assert.equal(matchSource('📄 없는 문서(2026-01-01)', ev, fold), null);

    // [수정 라운드 2·3 회귀 픽스처 — 2026-09-12] 이름·제목 전부 가짜
    // (예시업무·예시업무2·예시안건철·예시나머지철·예시목록철 — 라운드3 지적으로 실물
    // 문서 제목을 그대로 쓰던 것도 가짜 제목으로 바꿨다).
    //
    // ⓐ [라운드3 교정] 날짜 면제 승자(slug 에 날짜 없음, 겹침 0.75 로 겨우 자격)가,
    //    **엄격히 더 강한**(score 1, fullMatch) 날짜불일치 후보에게 거부당해 null —
    //    라운드1 이 고친 원래 오매칭의 모양. (라운드2 판은 둘 다 score 1 로 **동점**이라
    //    "동점도 거부"였던 당시 코드에만 맞았고, 라운드3 의 "엄격 우세만 거부"에서는
    //    안 죽어야 하는데 죽지 않는 걸 확인 못 시키는 잘못된 픽스처였다 — 방해 후보가
    //    엄격히 우세한 모양으로 다시 짰다, 완화 아님.)
    const evA = ['📄 예시업무/20260101-절차-안내',
      '📄 예시업무2/예시업무-준비-절차-요약'];
    assert.equal(matchSource('📄 예시업무 준비 절차 안내(2026-08-13)', evA, fold), null);
    // ⓑ [①-a] 날짜가 정확히 일치한 승자는, **방해 후보가 엄격히 더 강해도**(score 1 >
    //    승자의 0.75) 살아남는다 — ①-a 가 없으면(veto 를 무조건 건다면) 이 경우 죽는다.
    const evB = ['📄 예시업무/20260101-절차-안내',
      '📄 예시업무/20260813-예시업무-준비-절차-요약'];
    assert.equal(matchSource('📄 예시업무 준비 절차 안내(2026-08-13)', evB, fold),
      '📄 예시업무/20260813-예시업무-준비-절차-요약');
    // ⓒ [라운드3 핵심 — 동점 거부 금지] 누적 문서(회차 여럿을 품은, 날짜 없는 승자)와
    //    같은 제목의 단일 회차 문서(날짜 다름)가 둘 다 fullMatch 로 **동점**(score 1=1)
    //    이면 승자가 살아남는다 — 실물 사례 두 건(보고서에 이름 있음)과 같은 모양.
    //    `>=` 로 되돌리면(라운드2 상태) 이 assert 가 빨개진다.
    const evTie = ['📄 예시목록철/예시목록-정기-안내',
      '📄 예시목록철2/20260608-예시목록-정기-안내-v3'];
    assert.equal(matchSource('📄 예시목록 정기 안내(2026-08-24)', evTie, fold),
      '📄 예시목록철/예시목록-정기-안내');
    // ⓓ [score 정규화] 승자가 fullMatch 로 이겼지만(score 1) raw 겹침 비율 자체는
    //    낮은 경우(실물 사례처럼, 부속 설명이 길어 낱말 겹침은 낮아도 slug 전체가
    //    제목 안에 그대로 들어 있는 경우) — 자격은 있지만(겹침 0.75) fullMatch 는 아닌
    //    날짜불일치 후보가 raw 겹침만으로 승자를 거부하면 안 된다(score 비교라 0.75>1
    //    이 안 됨). 이 assert 는 `score` 정규화(fullMatch 를 1로 침) 자체를 잡는다 —
    //    라운드4 에서 지운 방해 후보 자격 게이트는(수학적으로 죽은 코드라) 이 assert
    //    로도 못 잡는다(위 scoreDocMatch 의 라운드4 주석 참조).
    const evD = ['📄 예시안건철/초안',
      '📄 예시안건철2/20260101-예시안건-세부-내역-요약'];
    assert.equal(matchSource('📄 예시안건 세부 내역 초안(2026-08-13)', evD, fold),
      '📄 예시안건철/초안');
    // ⓔ [②] slug 에 날짜가 없어 면제되는 후보는 문턱이 0.75 — 겹침 0.667(2/3) 로는
    //    유일 후보라도 문턱 미달로 null. 0.75→0.5 로 되돌리면 이 assert 가 빨개진다.
    const evE = ['📄 예시나머지철/예시나머지-항목-요약'];
    assert.equal(matchSource('📄 예시나머지 항목 정리(2026-08-13)', evE, fold), null);

    console.log('selftest match OK');
  }

  {
    /* 권한 라벨 풀기. **구분자가 `', '` 인 것이 요점이다** — `'·'` 로 나누면 세 이름이
     * 통째로 한 이름이 되어 비공개가 하나도 안 열리는데, **에러는 안 난다.** 그 상태로
     * 재생하면 비공개 근거가 전부 사망으로 찍히고 생존률이 조용히 틀린다.
     * 채널 이름은 전부 가짜다 (실물 이름은 pre-commit 이 막는다). */
    const fakeCfg = {
      FULL_ACCESS: { full: true, channels: new Set() },
      PUBLIC_ACCESS: { full: false, channels: new Set() },
      accessFor: (names) => ({ full: false, channels: new Set(names) }),
    };
    const ch = (label) => [...accessForLabel(label, fakeCfg).channels].sort();
    assert.equal(accessForLabel('전체', fakeCfg).full, true);
    assert.equal(accessForLabel('허용', fakeCfg).full, true);
    assert.equal(accessForLabel('공개만', fakeCfg).full, false);
    // `.full` 을 **같이** 봐야 한다 — 채널 목록만 보면 `'금지'` 를 FULL 로 바꾸는 변이가
    // 초록으로 지나간다(FULL_ACCESS 도 channels 가 빈 Set 이라서다, config.js).
    assert.equal(accessForLabel('금지', fakeCfg).full, false);
    assert.equal(accessForLabel(null, fakeCfg).full, false);
    assert.deepEqual(ch('공개만'), []);
    assert.deepEqual(ch('금지'), []);
    assert.deepEqual(ch(null), []);
    assert.deepEqual(ch('#예시채널'), ['예시채널']);
    // 이 한 줄이 `', '` 를 지킨다 — `'·'` 로 나누면 ['#예시채널, #보조채널, #셋째채널']
    // 하나가 되어 길이 1 이 나온다.
    assert.deepEqual(ch('#예시채널, #보조채널, #셋째채널'), ['보조채널', '셋째채널', '예시채널']);
    console.log('selftest access OK');
  }

  {
    // 기준 라벨 모양 검사 — 어긋난 것은 **던져야** 한다(조용한 폴백 금지).
    assert.equal(assertOriginsShape([{ a: 'hits' }, {}], 2), true);
    assert.throws(() => assertOriginsShape(null, 2), /배열이어야/);
    assert.throws(() => assertOriginsShape({ 0: {} }, 2), /배열이어야/);
    assert.throws(() => assertOriginsShape([{}], 2), /길이가/);
    assert.throws(() => assertOriginsShape([{}, {}, {}], 2), /길이가/);
    assert.throws(() => assertOriginsShape([{}, undefined], 2), /라벨 객체가 아닙니다/);
    assert.throws(() => assertOriginsShape([{}, []], 2), /라벨 객체가 아닙니다/);
    assert.throws(() => assertOriginsShape([{}, 'hits'], 2), /라벨 객체가 아닙니다/);
    console.log('selftest origins-shape OK');
  }

  {
    // 상한 주입 — **오타가 조용히 기준으로 돌아가는 것**을 막는 게 이 검사의 전부다.
    const mk = () => ({ limits: { searchMaxHits: 40, docSearchMaxHits: 12 } });
    const c1 = mk();
    assert.deepEqual(applyCap(c1, 'searchMaxHits', 12), { axis: 'searchMaxHits', value: 12, before: 40 });
    assert.equal(c1.limits.searchMaxHits, 12);
    assert.equal(applyCap(mk(), null, null), null);
    assert.equal(applyCap(mk(), 'baseline', 0), null);
    // 오타·모르는 축 → 던진다 (안 던지면 그 지점이 기준과 같은 숫자로 조용히 찬다).
    assert.throws(() => applyCap(mk(), 'searchMaxHit', 12), /모르는 축/);
    // 목록엔 있는데 지금 config 에 없는 축 → 코드 기본값과 갈릴 수 있으니 안 넣는다.
    assert.throws(() => applyCap(mk(), 'partialHitMaxHits', 4), /config.limits 에/);
    assert.throws(() => applyCap(mk(), 'searchMaxHits', 40), /낮지 않습니다/);
    assert.throws(() => applyCap(mk(), 'searchMaxHits', 60), /낮지 않습니다/);
    assert.throws(() => applyCap(mk(), 'searchMaxHits', 1.5), /양의 정수가 아닙니다/);
    assert.throws(() => applyCap(mk(), 'searchMaxHits', 0), /양의 정수가 아닙니다/);
    assert.throws(() => applyCap({}, 'searchMaxHits', 12), /객체가 아닙니다/);
    // 격자는 전부 잴 수 있는 축이어야 한다 — 오타가 하나 있으면 그 지점만 조용히 빠진다.
    for (const [a, v] of GRID) {
      assert.ok(CAP_AXES.includes(a), `격자의 축 ${a} 가 CAP_AXES 에 없습니다`);
      assert.ok(Number.isInteger(v) && v > 0);
    }
    assert.equal(GRID.length, 11);
    console.log('selftest apply-cap OK');
  }

  {
    /* 기준에서 얼려 덮어쓰기. **어긋나면 던져야 한다** — 첨자가 밀린 채 돌면 다른 회차의
     * 표본 판정이 붙고, 에러는 안 난다. */
    const mkEntry = (id, over) => ({
      id,
      measurable: true,
      baselineOk: true,
      overlap: 1,
      citations: [{
        item: 'A', survived: true, echoOnly: true, afterOnly: false,
      }],
      ...over,
    });
    const point = [mkEntry('r1', { measurable: true, baselineOk: true, overlap: 0.2 })];
    const base = [mkEntry('r1', { measurable: false, baselineOk: null, overlap: null })];
    base[0].citations[0] = {
      item: 'A', survived: false, echoOnly: false, afterOnly: true,
    };
    assert.equal(freezeFromBaseline(point, base), true);
    // 표본을 가르는 세 칸은 기준 것으로 덮이고, 지점 값은 `point` 에 남는다.
    assert.equal(point[0].measurable, false);
    assert.equal(point[0].baselineOk, null);
    assert.equal(point[0].overlap, null);
    assert.deepEqual(point[0].point, { measurable: true, baselineOk: true, overlap: 0.2 });
    // 인용의 생존 관련 칸 셋은 **안 언다** — 지금 이 생존이 진짜인가를 말하는 칸이라서다.
    // 기준 값은 baseline* 옆칸으로 들어간다(버리지 않는다).
    assert.equal(point[0].citations[0].afterOnly, false);
    assert.equal(point[0].citations[0].baselineAfterOnly, true);
    assert.equal(point[0].citations[0].echoOnly, true);
    assert.equal(point[0].citations[0].baselineEchoOnly, false);
    assert.equal(point[0].citations[0].survived, true);
    assert.equal(point[0].citations[0].baselineSurvived, false);

    assert.throws(() => freezeFromBaseline([mkEntry('r1')], null), /배열이어야/);
    assert.throws(() => freezeFromBaseline([mkEntry('r1')], []), /회차 수가 다릅니다/);
    assert.throws(() => freezeFromBaseline([mkEntry('r1')], [mkEntry('r2')]), /회차가 다릅니다/);
    assert.throws(() => freezeFromBaseline([mkEntry('r1')], [null]), /회차 객체가 아닙니다/);
    assert.throws(
      () => freezeFromBaseline([mkEntry('r1')], [mkEntry('r1', { citations: [] })]),
      /인용 수가 다릅니다/,
    );
    assert.throws(
      () => freezeFromBaseline([mkEntry('r1')], [mkEntry('r1', { citations: [{ item: 'B' }] })]),
      /인용 0 가 다릅니다/,
    );
    console.log('selftest freeze OK');
  }
}

/* Step 5(브리프) — 실물 로그 손 대조용. 실행 경로는 archive.js 의 진짜 fold 를 쓴다
 * (자가검사의 근사 fold 와 다르다 — 파일 맨 위 주석 참조). 무작위 없이 고정 간격으로
 * 20건을 뽑는다: pool[Math.floor(i * pool.length / 20)].
 *
 * [수정 라운드 1 — 2026-09-12]
 * ① fold 보강: archive.js 의 fold 는 공백·`_`·`-`·`#` 만 지우고 `.`·`(`·`)`·`,` 는
 *    남긴다. 실측(archive fold 503매칭 vs 근사 fold 514매칭, 차이 11건 전부 근사 쪽만
 *    성공한 올바른 매칭)으로 확인된 결함이라, 주입 fold 위에 그 네 문자 제거를 한 겹
 *    더 얹는다 (archive.js 자체는 안 건드린다 — src/ 무수정 제약).
 * ② 표본 모집단 교체: 이전 감사는 채널 매칭(항상 완전일치, 오매칭 불가) 12/20 을 표본에
 *    섞어 위험 지대를 못 찔렀다. 문서 매칭 중에서도 「전체 제목 포함」이 아니라 토큰
 *    겹침 문턱으로만 성공한 것(`isLooseDocMatch`)만 모아, 그 안에서 고정 간격 20건을
 *    다시 뽑는다.
 *
 * [수정 라운드 2 — 2026-09-12] fold 보강이 11건 중 3건을 못 잡았다 — 전부 가운뎃점
 * `·` 때문(실물 제목 셋에 가운뎃점이 들어 있었다, 보고서에 이름 있음). 자가검사
 * 근사 fold 는 `·`·`/` 도 지우므로 보강 fold 에도 그 둘을 더한다. */
if (process.argv.includes('--audit')) {
  const { fold: archiveFold } = await import('../src/archive.js');
  const fold = buildFold(archiveFold);
  const entries = await loadAllEntries();
  const matched = [];
  let attempted = 0;
  for (const e of entries) {
    if (!e.sources || !e.sources.length || !e.evidence.length) continue;
    for (const item of e.sources) {
      attempted += 1;
      const hit = matchSource(item, e.evidence, fold);
      if (hit) matched.push({ date: e.date, item, hit, evidence: e.evidence });
    }
  }
  const unmatched = attempted - matched.length;
  const failRate = attempted ? unmatched / attempted : 0;
  console.log(`[audit] 시도 ${attempted}건 · 매칭 ${matched.length}건 · 못 쟀다 ${unmatched}건 (${(failRate * 100).toFixed(1)}%)`);
  if (failRate > 0.3) {
    console.log('[audit] 못 쟀다 비율이 30%를 넘습니다 — 실패 표본 10건:');
    const fails = [];
    for (const e of entries) {
      if (!e.sources || !e.sources.length || !e.evidence.length) continue;
      for (const item of e.sources) {
        if (!matchSource(item, e.evidence, fold)) fails.push({ date: e.date, item, evidence: e.evidence });
      }
    }
    for (let i = 0; i < Math.min(10, fails.length); i += 1) {
      const f = fails[Math.floor(i * fails.length / 10)];
      console.log(`  ${i + 1}. [${f.date}] 출처: ${f.item}  |  근거: ${f.evidence.join(' · ')}`);
    }
  }
  const looseMatched = matched.filter((p) => isLooseDocMatch(p.item, p.evidence, fold));
  console.log(`[audit] 문서 매칭 중 완전 포함이 아닌(느슨한) 것: ${looseMatched.length}건 — 표본은 여기서 뽑는다.`);
  console.log(`[audit] 느슨한 매칭 중 고정 간격 20건 (또는 전체 ${looseMatched.length}건 중):`);
  const n = Math.min(20, looseMatched.length);
  for (let i = 0; i < n; i += 1) {
    const p = looseMatched[Math.floor(i * looseMatched.length / 20)];
    console.log(`  ${i + 1}. [${p.date}] 출처: ${p.item}  →  근거: ${p.hit}`);
  }
}

/* ─────────────────────────────────────────────────────────────────────────
 * 기준 재생 — 과거 회차의 search 호출을 모델 없이 지금 코드로 다시 돌린다.
 * ───────────────────────────────────────────────────────────────────────── */

/**
 * 근거 줄과 `touched` 를 같은 모양으로 만든다.
 *
 * `touched` 의 채널은 **맨이름**이고(claude.js 의 `touched.add(h.channel)`), 근거 줄의
 * `#` 는 렌더가 붙인 것이다(convo-log.js 의 `**근거**` 조립). 문서(`📄 project/slug`)만
 * 양쪽이 같은 모양으로 들어온다. 정규화를 빼면 채널 인용이 **전부 사망으로 찍힌다.**
 */
export const identityKey = (e) => (e.startsWith('📄') ? e : e.replace(/^#/, ''));

/**
 * 로그 꼬리의 `비공개 근거 …` 라벨을 그때의 access 로 되돌린다.
 *
 * 실물 라벨(두 달 전수, 문답·요약 포함 198건): `공개만` 96 · `#a, #b, #c` 60 ·
 * `#a` 29 · `허용` 5 · `금지` 3 (`전체` 는 실물에 없지만 `accessLabel` 이 내는 값이라
 * 함께 받는다). `허용`·`금지` 는 2026-08-05 전의 불린 렌더다 — `허용` 은 당시 의미가
 * 「비공개 전부」라 FULL 에 준하고 `금지` 는 공개만이다.
 *
 * **구분자는 `', '` 다** (config.js 의 `accessLabel` 이 그렇게 잇는다). `·` 로 나누면
 * 통째로 한 이름이 되어 비공개가 하나도 안 열린 채 에러 없이 조용히 틀린다.
 */
export function accessForLabel(label, mods) {
  const { FULL_ACCESS, PUBLIC_ACCESS, accessFor } = mods;
  if (label === '전체' || label === '허용') return FULL_ACCESS;
  if (!label || label === '공개만' || label === '금지') return PUBLIC_ACCESS;
  return accessFor(label.split(', ').map((s) => s.trim().replace(/^#/, '')));
}

/**
 * 출처 매칭용 fold. `src/archive.js` 의 fold 는 `.`·`(`·`)`·`,`·`·`·`/` 를 남기는데
 * 출처 제목에는 그것들이 들어 있어 slug 와 안 맞는다 (Task 3 라운드 1·2 실측).
 *
 * **`--audit` 과 재생이 같은 한 벌을 쓴다** — 규칙이 두 벌이면 한쪽만 고쳤을 때 감사와
 * 재생이 서로 다른 매칭을 쓰게 된다. `function` 선언인 것이 요점이다(끌어올려져서
 * 위쪽 `--audit` 블록에서도 부를 수 있다 — `const` 로 두면 TDZ 로 죽는다).
 */
export function buildFold(archiveFold) {
  return (s) => archiveFold(s).replace(/[().,·/]/g, '');
}

/**
 * 회차가 **증명할 수 있게** search 밖에서 건드린 키 — 로그 자신의 도구 인자로 만든다.
 *
 * 왜 필요한가: 겹침의 분모를 「재생이 낸 것」으로 정의하면 분모가 정의상 재생 결과의
 * 부분집합이 되어 **겹침이 항상 1.0** 이 된다(수정 라운드 1 전의 결함). 분모는 재생과
 * 무관한 자료 — 로그에 적힌 도구 인자 — 로 만들어야 한다.
 *
 * `parseToolLine` 은 search 아닌 호출의 인자도 값만 `', '` 로 이어 준다.
 *  - `read_channel(채널[, 월])`   → 첫 토큰을 `resolveChannelFor` 로 푼다 (archive.js 의
 *    `readChannel` 이 `{ channel: r.name }` 을 돌려주고 claude.js 가 그것을 touched 에 넣는다)
 *  - `read_document(사업장, 문서[, 월/시트/절])` → `📄 ${사업장}/${slug}` (documents.js 의
 *    `readDocument` 가 `{ project: p.name, document: d.doc.slug }` 를 돌려준다)
 *
 * **못 푼 인자는 빼지 않는다** — 증명이 안 된 것을 빼면 재생 실패를 「검색밖」으로 숨기게
 * 된다. 못 푼 횟수는 별도 칸으로 센다. 권한을 본 이름 풀기(`resolveProjectFor` 등)는
 * src/ 에서 내보내지 않아 쓸 수 없으므로, ① 공개 해석기로 풀고 ② 그래도 안 되면 로그
 * 토큰이 근거 줄의 키와 **글자 그대로** 같은 경우만 받는다(가상 사업장 이름이 그렇다).
 */
function offSearchKeys(entry, access, mods, evidenceKeys) {
  const { resolveChannelFor, resolveProject, resolveDocument } = mods;
  const keys = new Set();
  let unresolved = 0;
  let inferred = 0;
  for (const call of entry.calls) {
    if (!call.args) continue;
    const tokens = call.args.split(', ');
    if (call.name === 'read_channel') {
      const r = resolveChannelFor(tokens[0], access);
      if (r.ok) keys.add(r.name);
      else if (evidenceKeys.includes(tokens[0])) keys.add(tokens[0]);
      else unresolved += 1;
      continue;
    }
    if (call.name === 'read_document') {
      const p = resolveProject(tokens[0]);
      let slug = null;
      // 문서 제목 안에도 `, ` 가 들어 있을 수 있다(키가 소실돼 어디까지가 문서명인지
      // 모른다) — 짧은 것부터 이어 붙여 가며 풀어 본다.
      for (let n = 1; p.ok && n < tokens.length && !slug; n += 1) {
        const d = resolveDocument(p.name, tokens.slice(1, n + 1).join(', '));
        if (d.ok) slug = d.doc.slug;
      }
      if (slug) { keys.add(`📄 ${p.name}/${slug}`); continue; }
      /* 권한을 본 이름 풀기로만 잡히는 가상 사업장은 공개 해석기로 못 푼다. 그 호출도
       * 무언가를 열긴 열었으므로 근거 줄에 그 사업장 문서가 **딱 하나**면 그것이다.
       * 둘 이상이면 고르지 않는다. 이 갈래는 추정이므로 따로 센다 — 호출이 에러로
       * 끝났으면(아무것도 안 열었으면) 잘못 빼는 것이 된다. */
      const under = evidenceKeys.filter((e) => e.startsWith(`📄 ${tokens[0]}/`));
      if (under.length === 1) { keys.add(under[0]); inferred += 1; } else unresolved += 1;
    }
  }
  return { keys, unresolved, inferred };
}

/**
 * 넘겨받은 기준 출신 라벨의 모양을 검사한다. **어긋나면 던진다 — 조용히 되돌아가지 않는다.**
 *
 * 길이가 모자라거나 원소가 객체가 아니면 `origins[idx]` 가 `undefined` 가 되고, 그러면
 * 그 회차만 **지점에서 라벨을 다시 매기는 쪽으로 조용히 되돌아간다.** 그 회차의 생존률은
 * 구조상 100% 가 되는데 **에러는 안 난다** — 이 Task 내내 잡아 온 바로 그 모양이다.
 *
 * @param {unknown} origins 기준 실행이 낸 `result.origins`
 * @param {number} n 표본 회차 수 (`entries` 와 첨자가 같아야 한다)
 */
export function assertOriginsShape(origins, n) {
  if (!Array.isArray(origins)) {
    throw new Error(`origins 는 배열이어야 합니다 — 받은 것: ${typeof origins}`);
  }
  if (origins.length !== n) {
    throw new Error(`origins 길이가 표본 회차 수와 다릅니다 — origins ${origins.length} vs 회차 ${n}. `
      + '첨자가 어긋나면 라벨이 다른 회차에 붙습니다.');
  }
  for (let i = 0; i < origins.length; i += 1) {
    const o = origins[i];
    if (!o || typeof o !== 'object' || Array.isArray(o)) {
      throw new Error(`origins[${i}] 가 라벨 객체가 아닙니다 — 받은 것: ${Array.isArray(o) ? 'array' : typeof o}`);
    }
  }
  return true;
}

/**
 * 인용 하나의 근거 히트 묶음을 한 줄로 접는다 — 지점끼리 **같은 근거를 받았나**를 대는 값.
 *
 * 건수만 대면 안 된다: `searchMaxPerChannel`·`docSearchMaxPerProject` 는 건수를 안 바꾸고
 * **구성만** 바꾸므로(pickSpread 가 남는 예산을 최근순으로 채운다) 건수 비교로는 「아무
 * 일도 없었다」로 보인다. 그래서 히트의 신원(날짜·자리·길이)까지 넣는다.
 * 본문 전문을 넣지 않는 것은 값만 커지고 판별력이 안 늘어서다 — 같은 자리·같은 날짜·같은
 * 길이인데 내용만 다른 히트는 이 재생에서 나올 수 없다(같은 아카이브를 같은 질의로 읽는다).
 *
 * **[규칙판] 이 함수를 고치면 산출의 `poolSig` 값이 달라져 지점끼리 댈 수 없다.**
 * `ENTRY_RULES` 를 올리고 격자를 다시 돌려야 한다 — 무엇을 서명에 넣을지(날짜·자리·길이)를
 * 바꾸는 것이 특히 그렇다. 안 올려도 `entryFingerprint()` 가 잡는다.
 */
export function poolSignature(pool) {
  if (!pool.length) return 'empty';
  const line = pool
    .map((h) => [h.date, h.channel, h.project, h.document, (h.text || '').length].join('|'))
    .join(';');
  return crypto.createHash('sha1').update(line).digest('hex').slice(0, 12);
}

/**
 * 회차 하나를 재생한다. **`buildTools` 가 만든 `search` 도구의 `run` 을 부른다** —
 * `searchArchive` 를 직접 부르면 자동 좁힘·폴백·밖 결과·카드·부분일치 안내가 전부
 * 빠진다. 같은 관례를 `scripts/check-outside-hits.js` 의 [7/8] 이 쓴다.
 *
 * **[규칙판] 이 함수를 고치면 `toolChars`·`touched`·`inputs` 의 뜻이 달라진다** — 절감률과
 * 생존 판정의 뿌리다. `ENTRY_RULES` 를 올리고 격자를 다시 돌려야 한다(어느 호출을 빼는지,
 * 어떤 인자로 돌리는지를 바꾸는 것이 특히 그렇다). 안 올려도 `entryFingerprint()` 가 잡는다.
 */
async function replayEntry(entry, mods) {
  const { buildTools } = mods;
  const access = accessForLabel(entry.privateAccess, mods);
  const touched = new Set();
  const narrows = [];
  const tools = buildTools({ access, touched, sizes: [], narrows });
  const search = tools.find((t) => t.name === 'search');
  let toolChars = 0;
  let heldChars = 0;
  let held = 0;
  const inputs = [];
  const calls = [];
  for (const call of entry.calls) {
    if (call.name !== 'search' || !call.args) continue;
    const { input, leftoverComma } = decomposeArgs(call.args, mods);
    /* **분해 보류 호출은 재생하지 않는다** (설계서 §3.2 「따로 세고 뺀다」).
     * 실물 4건은 전부 `where` 없이 `document` 만 온 호출이라 분해기가 `document` 를
     * 못 들어올린다. 그대로 돌리면 재생이 원본과 **두 군데** 달라진다 —
     * ① 원본은 `document` 가 있어 `autoNarrow` 를 안 탔는데 재생은 탄다
     *    (claude.js 의 `auto = (where || document) ? … : autoNarrow(...)`)
     * ② 문서 제목이 `query` 에 남아 검색 낱말 자체가 달라진다.
     * 결과 글자도 절감률 분모에서 뺀다 — 안 돌린 호출의 크기를 세면 안 된다. */
    if (leftoverComma) {
      held += 1;
      if (call.chars != null) heldChars += call.chars;
      inputs.push({ input, leftoverComma, replayed: false });
      continue;
    }
    inputs.push({ input, leftoverComma, replayed: true });
    const text = await search.run(input);
    toolChars += text.length;
    // 로그가 적어 둔 결과 크기와 지금 크기 — 아카이브가 얼마나 달라졌나의 직접 증거다.
    // `chars` 가 `null` 인 것은 0자가 아니라 **크기를 모르는 것**이다(convo-log.js).
    calls.push({ loggedChars: call.chars, replayChars: text.length });
  }
  return {
    access, touched, narrows, inputs, toolChars, calls, held, heldChars,
  };
}

/**
 * 근거 줄 항목의 **출신**을 가르려고, 재생과 **같은 유효 인자**로 두 검색을 구조 호출해
 * 확정 히트 / 부분 일치 / 밖 결과를 키별로 모은다.
 *
 * 유효 인자 계산은 `claude.js` 의 `search.run` 첫머리와 같은 순서다 — 좁힘은 `narrows`
 * 문자열을 되파싱하지 않고 **`autoNarrow(query, access)` 를 직접 다시 부른다**(그 배열
 * 원소는 사람이 읽는 표시 문자열뿐이라 구조 값이 없다). 봇이 스스로 준 `where` 가 있으면
 * 그것이 우선이다 — 그 호출은 애초에 autoNarrow 를 안 탄다.
 *
 * 확정과 부분 일치는 **`score` 가 숫자인가**로 가른다 — 부분 일치에만 붙는다
 * (`archive.js` 의 `partials`, `documents.js` 의 `partials`). `check-outside-hits.js`
 * 의 `shape()` 가 쓰는 것과 같은 판정이다.
 *
 * **[규칙판] 이 함수를 고치면 출신 라벨(hits/partial/outside)과 `poolSig` 가 함께 달라진다**
 * — 축별 분모가 이 라벨로 갈리므로 표 전체가 흔들린다. `ENTRY_RULES` 를 올리고 격자를
 * 다시 돌려야 한다. 특히 위 `score` 판정을 뒤집는 변경이 그렇다(검수가 실제로 뒤집어
 * 보였고, 이름표만으로는 안 잡혀서 `entryFingerprint()` 를 넣었다).
 */
function originSets(inputs, access, mods) {
  const {
    autoNarrow, searchArchive, searchDocuments, hasDocuments,
  } = mods;
  const hits = new Map();
  const partial = new Map();
  const outside = new Map();
  const add = (m, k, h) => { if (!m.has(k)) m.set(k, []); m.get(k).push(h); };
  for (const { input, replayed } of inputs) {
    if (!replayed) continue;
    const {
      query, where, document, only,
    } = input;
    const auto = (where || document)
      ? { channel: null, project: null }
      : autoNarrow(query, access);
    const chWhere = where || auto.channel || undefined;
    const pjWhere = where || auto.project || undefined;
    if (only !== 'documents' && !(document && where)) {
      let r = searchArchive({ query, channel: chWhere, access });
      if (auto.channel && ![...r.hits, ...(r.outside || [])].length) {
        r = searchArchive({ query, access });
      }
      for (const h of r.hits) add(typeof h.score === 'number' ? partial : hits, h.channel, h);
      for (const h of r.outside || []) add(outside, h.channel, h);
    }
    if (only !== 'archive' && hasDocuments()) {
      let r = searchDocuments({ query, project: pjWhere, document, access });
      if (auto.project && ![...r.hits, ...(r.outside || [])].length) {
        r = searchDocuments({ query, document, access });
      }
      for (const h of r.hits) {
        add(typeof h.score === 'number' ? partial : hits, `📄 ${h.project}/${h.document}`, h);
      }
      for (const h of r.outside || []) add(outside, `📄 ${h.project}/${h.document}`, h);
    }
  }
  return { hits, partial, outside };
}

/**
 * 메모리의 `config.limits` 를 한 축만 낮춘다. **config.json 파일은 안 건드린다.**
 *
 * 부르는 자리가 `src/archive.js`·`src/documents.js`·`src/claude.js` 를 import 하기
 * **전**이어야 한다 — `documents.js` 의 `DOC_SEARCH_MAX_HITS` 같은 상수는 import 시점에
 * 고정되므로, import 뒤에 고치면 그 축만 조용히 안 먹는다(다른 축은 먹어서 표가 반쯤
 * 맞는 채로 나온다 — 가장 알아채기 어려운 모양이다). 한 프로세스에서 두 지점을 돌리면
 * 두 번째 import 는 캐시라 같은 일이 벌어진다. 그래서 `--all` 은 **지점마다 프로세스를
 * 새로 띄운다.**
 *
 * 실측 확인(2026-09-12, 축 5개 전부 별도 프로세스로 대조):
 * searchMaxHits 40→12 은 히트 수가 40→12, docSearchMaxHits 12→4 는 12→4,
 * partialHitMaxHits 12→4 는 부분일치 12→4 로 준다. `searchMaxPerChannel`·
 * `docSearchMaxPerProject` 는 **건수가 아니라 구성만** 바꾼다 — pickSpread 가 라운드
 * 로빈으로 자리마다 perGroup 건까지 고른 뒤 **남는 자리를 최근순으로 채우기** 때문이다
 * (`archive.js` 의 pickSpread ②). 그래서 이 두 축은 절감이 0에 가깝고 생존 구성만 흔든다.
 */
export function applyCap(config, axis, value) {
  if (!axis || axis === 'baseline') return null;
  if (!CAP_AXES.includes(axis)) {
    throw new Error(`모르는 축입니다: ${axis} — 잴 수 있는 축은 ${CAP_AXES.join(' · ')}`);
  }
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${axis} 의 값이 양의 정수가 아닙니다 — 받은 것: ${value}`);
  }
  const limits = config && config.limits;
  if (!limits || typeof limits !== 'object') {
    throw new Error('config.limits 가 객체가 아닙니다 — 주입할 자리가 없습니다.');
  }
  if (!(axis in limits)) {
    throw new Error(`config.limits 에 ${axis} 가 없습니다 — 넣으면 코드 기본값과 갈릴 수 있어 `
      + '기준값을 확인하기 전에는 주입하지 않습니다.');
  }
  const before = limits[axis];
  if (!(value < before)) {
    throw new Error(`${axis}=${value} 는 지금 값 ${before} 보다 낮지 않습니다 — 격자는 낮추는 쪽만 잽니다.`);
  }
  limits[axis] = value;
  return { axis, value, before };
}

/**
 * 아카이브가 그때와 같은 상태인지 재는 서명 — 채널·문서 파일의 **경로와 크기**를 이어
 * 붙여 해시한다.
 *
 * 왜: 지점은 기준 산출(baseline.json)에서 라벨과 표본 칸을 얼려 온다. 그런데
 * `freezeFromBaseline` 이 대는 것은 회차 수·`id`·인용 `item` 뿐이고, 그 셋은 **로그에서만**
 * 오는 값이라 **아카이브만 자란 경우를 못 잡는다.** 로그가 그대로면 낡은 기준이 폴더에
 * 남아 있어도 그대로 얼려 써지고, 그러면 「오늘 아카이브로 돌린 지점」을 「지난주
 * 아카이브로 돌린 기준」에 대는 셈이 된다 — 상한 탓으로 보이는 죽음이 사실은 아카이브
 * 변화다. **에러는 안 난다.**
 *
 * **파일 내용까지 읽는다.** 처음엔 경로+크기만 썼는데, 이 저장소의 아카이브는 자라기만
 * 하는 것이 아니라 **제자리에서 고쳐진다** — `slack-sync` 가 슬랙에서 고쳐지거나 지워진
 * 메시지를 md 에 반영하고, `doc-archive` 가 `[공개]` 승인을 문서 메타에 옮긴다. 크기가
 * 같은 채로 내용만 바뀌는 일이 실제로 일어나고(검수 실증: 같은 크기로 내용만 바꾸니
 * 서명이 같았다), 그러면 낡은 기준이 조용히 통과한다. 실측 15.4MB·516개를 다 읽어
 * 해시하는 데 **93ms** 라 아낄 이유가 없다.
 */
export function archiveSignature(cfg) {
  const h = crypto.createHash('sha1');
  const walk = (dir) => {
    let ents = [];
    try {
      ents = fs.readdirSync(dir, { withFileTypes: true });
    } catch (e) {
      if (e.code !== 'ENOENT') throw e;
      return;
    }
    // 이름순으로 못 박는다 — 읽는 순서가 OS 마다 다르면 같은 자료에서 다른 서명이 난다.
    for (const ent of [...ents].sort((a, b) => (a.name < b.name ? -1 : 1))) {
      const p = path.join(dir, ent.name);
      if (ent.isDirectory()) { walk(p); continue; }
      // 경로도 함께 넣는다 — 내용만 이으면 파일 둘의 이름이 뒤바뀐 것을 못 잡는다.
      h.update(`${p}\n`);
      h.update(fs.readFileSync(p));
    }
  };
  if (cfg.CHANNELS_DIR) walk(cfg.CHANNELS_DIR);
  if (cfg.DOCS_DIR) walk(cfg.DOCS_DIR);
  return h.digest('hex').slice(0, 16);
}

/**
 * **재생·얼리기 규칙판.** `replayEntry`·`originSets`·`freezeFromBaseline` 이 내는 값의
 * 뜻이 바뀌면 이 문자열을 올린다 (예: 어떤 칸을 얼릴지 바꾸거나, 인용에 칸을 더하거나).
 *
 * 왜: `--summary` 는 **재생을 다시 안 돌고** 이미 있는 산출의 `summary` 만 다시 만든다.
 * 규칙이 바뀐 뒤에 그걸 돌리면 `entries` 는 낡은 채 표만 새 규칙으로 나오는데 **에러가
 * 안 난다.** 실제로 이 Task 안에서 `afterOnly` 를 얼릴지 한 번 되돌렸다 — 그때 낡은
 * 산출에 `--summary` 를 돌렸다면 두 규칙이 섞인 표가 나왔을 것이다.
 *
 * 표(`summarize`) 규칙만 바뀐 것은 여기 해당 없다 — 그건 다시 계산하면 되는 값이다.
 *
 * **이 문자열만으로는 아무것도 강제되지 않는다** — 손으로 올리는 이름표라, 규칙을 고치고
 * 이 값을 안 올리면 `--summary` 가 조용히 통과한다(검수 실증: `originSets` 의 확정/부분
 * 판정을 뒤집고 값을 그대로 뒀더니 통과). 그래서 아래 `entryFingerprint()` 가 **함수의
 * 실제 소스**를 해시해 함께 찍는다 — 이름표는 사람이 읽는 용도이고, 강제는 지문이 한다.
 */
export const ENTRY_RULES = '2026-09-12-r3';

/**
 * 재생·얼리기 함수 넷의 **소스 자체**를 해시한다. 손으로 올리는 `ENTRY_RULES` 와 달리
 * 잊어버릴 수가 없다 — 함수 본문이 한 글자라도 바뀌면 값이 바뀌고, 그러면 `--summary` 가
 * 낡은 산출을 거부한다.
 *
 * `Function.prototype.toString()` 은 **함수 본문의 소스 그대로**를 돌려주므로 파서가 필요
 * 없다. 함수 **위**의 주석은 본문 밖이라 안 들어간다 — 설명만 고치는 것으로 28분짜리
 * 재실행이 강요되지 않게 하려는 것이고, 본문 안 주석은 들어간다(그건 대개 판정 근거를
 * 적는 자리라 함께 묶이는 편이 맞다).
 *
 * **한계가 넓다 — 이 지문은 「바뀌면 잡는다」가 아니라 「이 넷이 바뀌면 잡는다」뿐이다.**
 * 검수가 변이로 실증한 네 가지(2026-09-12):
 *  - 본문 **주석 한 줄**만 더해도 값이 바뀐다 → **헛된 재실행**을 부른다 (거짓 양성).
 *    주석만 지우고 해시하려면 문자열·정규식 안의 `//` 를 가려낼 토큰 분석이 필요한데,
 *    엉성하게 지우면 `'http://a'` → `'http://b'` 같은 **진짜 변경을 못 잡는** 쪽으로
 *    틀어진다. 거짓 음성이 더 위험해서 **안 한다.**
 *  - `runPoint` 안의 인용 생성 블록(`survived`·`echoOnly`·`origin` 을 실제로 만드는
 *    자리)과 `scoreDocMatch`(출처↔근거 매처) 변경은 **못 잡는다** (거짓 음성).
 *    이 둘을 넣는 것이 옳지만, 넣으면 지문 값이 달라져 **이미 나온 산출 12개가 전부
 *    무효**가 되고 격자를 20분 다시 돌려야 한다. 다음에 격자를 돌릴 일이 생기면 그때
 *    함께 넣는다 (2026-09-12 판단 — 표 숫자는 안 바뀌므로 그것만으로 재실행하지 않는다).
 *  - `src/` 쪽 변경도 못 잡는다. 그건 이 측정의 **대상**이라 바뀌면 어차피 다시 재야 한다.
 *
 * 줄바꿈은 정규화한다 — 이 저장소는 `autocrlf` 라 다시 clone/checkout 하면 코드가 한 글자도
 * 안 바뀌어도 CRLF 로 내려와 지문이 갈린다 (검수 실증). 그건 순수한 거짓 양성이다.
 */
export function entryFingerprint() {
  const src = [replayEntry, originSets, freezeFromBaseline, poolSignature]
    .map((f) => f.toString().replace(/\r\n/g, '\n'))
    .join('\n---\n');
  return crypto.createHash('sha1').update(src).digest('hex').slice(0, 12);
}

/**
 * **기준에서 얼려 오는 칸**을 지점 결과에 덮어쓴다.
 *
 * 왜: `measurable`·`baselineOk`·`overlap` 은 지점마다 다시 계산된다. 상한을 낮추면
 * 재생이 근거를 덜 내므로 `overlap` 이 떨어지고, 그러면 `baselineOk` 가 false 로 뒤집혀
 * **그 회차가 지점 표본에서 빠진다.** 지점마다 표본이 달라지면 절감률·생존률을 지점끼리
 * 댈 수 없다 — 「상한을 낮췄더니 생존률이 올랐다」 같은 것이 나온다(어려운 회차가 표본에서
 * 빠져서). 그래서 표본을 가르는 세 칸은 기준 것으로 못 박고, 지점이 계산한 값은
 * `point` 에 따로 남긴다(버리지 않는다 — 지점 겹침 자체가 상한 영향의 직접 지표다).
 *
 * 인용 칸 둘(`echoOnly`·`afterOnly`)은 **안 얼린다 — 지점 값 그대로 둔다.** 둘 다
 * 「지금 이 상한에서 이 생존이 진짜인가」를 말하는 칸이라, 생존과 같은 지점에서 재야 한다.
 *  - `echoOnly`: 기준 값으로 얼리면, 상한을 낮춰 진짜 히트가 잘려 나가고 반향만 남아 산
 *    채널이 **진짜 생존으로 세어진다** — 생존률이 위로 틀어진다.
 *  - `afterOnly`: 설계서 §3.5 의 문면이 「인용 문서의 **살아남은** 히트가 전부 질문 날짜
 *    이후 회차뿐인 건」이다 — 살아남은 히트는 지점마다 다르다. 실측도 얼리는 쪽이 위험한
 *    방향임을 보였다: `docSearchMaxHits=4` 에서 기준 값은 5건인데 지점에서 다시 재면
 *    8건이다(상한이 옛 회차를 잘라내 새 회차만 남은 인용이 3건 늘었다). 얼리면 **가장
 *    공격적인 지점에서 편향을 3건 적게** 보고하게 되고, 그건 「낮춰도 되겠다」쪽으로
 *    기우는 안전하지 않은 방향이다.
 *
 * 대신 기준 값을 `baselineEchoOnly`·`baselineAfterOnly`·`baselineSurvived` 로 함께 실어
 * 둔다 — 버리지 않으므로 Task 6 이 두 잣대를 다 볼 수 있다.
 *
 * 어긋나면 **던진다.** 조용히 넘어가면 다른 회차의 라벨이 붙는다.
 *
 * **[규칙판] 이 함수를 고치면 「어느 칸이 기준 것이고 어느 칸이 지점 것인가」가 달라진다**
 * — 얼리는 칸을 더하거나 빼면 같은 표에 두 규칙이 섞인다(이 Task 안에서 `afterOnly` 를
 * 얼릴지 실제로 한 번 되돌렸다). `ENTRY_RULES` 를 올리고 격자를 다시 돌려야 한다.
 * 안 올려도 `entryFingerprint()` 가 잡는다.
 */
export function freezeFromBaseline(entries, frozen) {
  if (!Array.isArray(frozen)) {
    throw new Error(`기준 entries 는 배열이어야 합니다 — 받은 것: ${typeof frozen}`);
  }
  if (frozen.length !== entries.length) {
    throw new Error(`기준 회차 수와 지점 회차 수가 다릅니다 — 기준 ${frozen.length} vs 지점 ${entries.length}. `
      + '로그가 자랐으면 기준을 다시 재야 합니다(첨자가 어긋나면 라벨이 다른 회차에 붙습니다).');
  }
  for (let i = 0; i < entries.length; i += 1) {
    const f = frozen[i];
    const e = entries[i];
    if (!f || typeof f !== 'object' || Array.isArray(f)) {
      throw new Error(`기준 entries[${i}] 가 회차 객체가 아닙니다.`);
    }
    if (f.id !== e.id) {
      throw new Error(`첨자 ${i} 의 회차가 다릅니다 — 기준 「${f.id}」 vs 지점 「${e.id}」`);
    }
    e.point = { measurable: e.measurable, baselineOk: e.baselineOk, overlap: e.overlap };
    e.measurable = f.measurable;
    e.baselineOk = f.baselineOk;
    e.overlap = f.overlap;
    if (!Array.isArray(f.citations) || f.citations.length !== e.citations.length) {
      throw new Error(`첨자 ${i}(${e.id}) 의 인용 수가 다릅니다 — 기준 `
        + `${Array.isArray(f.citations) ? f.citations.length : '(배열 아님)'} vs 지점 ${e.citations.length}`);
    }
    for (let j = 0; j < e.citations.length; j += 1) {
      const fc = f.citations[j];
      const ec = e.citations[j];
      if (!fc || fc.item !== ec.item) {
        throw new Error(`첨자 ${i}(${e.id})의 인용 ${j} 가 다릅니다 — 기준 「${fc && fc.item}」 vs 지점 「${ec.item}」`);
      }
      // 지점 값(`survived`·`echoOnly`·`afterOnly`)은 그대로 두고, 기준 값을 옆칸에 싣는다.
      ec.baselineAfterOnly = fc.afterOnly;
      ec.baselineEchoOnly = fc.echoOnly;
      ec.baselineSurvived = fc.survived;
    }
  }
  return true;
}

/**
 * 지점 하나를 잰다. `axis` 없이 부르면 기준(지금 상한 그대로)이다.
 *
 * **동적 import 가 이 함수 안에 있는 것이 요점이다** — 다음 Task 의 상한 주입이
 * 「config.limits 를 먼저 고치고 그 뒤에 읽는다」 순서에 기댄다. 파일 맨 위의 정적
 * import 는 node 내장과 `src/convo-log.js` 까지다.
 */
export async function runPoint({
  axis = null, value = null, origins = null, frozen = null, baseSignature = null,
} = {}) {
  const cfg = await import('../src/config.js');
  /* **이 한 줄의 자리가 전부다** — 아래 세 import 보다 앞이어야 import 시점에 고정되는
   * 상수(documents.js 의 DOC_SEARCH_MAX_HITS 류)에도 먹는다. 뒤로 옮기면 그 축만 조용히 기준값으로 돈다. */
  const injected = applyCap(cfg.config, axis, value);
  const claude = await import('../src/claude.js');
  const archive = await import('../src/archive.js');
  const documents = await import('../src/documents.js');
  const mods = {
    FULL_ACCESS: cfg.FULL_ACCESS,
    PUBLIC_ACCESS: cfg.PUBLIC_ACCESS,
    accessFor: cfg.accessFor,
    buildTools: claude.buildTools,
    autoNarrow: claude.autoNarrow,
    searchArchive: archive.searchArchive,
    resolveChannelFor: archive.resolveChannelFor,
    isEchoEntry: archive.isEchoEntry,
    searchDocuments: documents.searchDocuments,
    resolveProject: documents.resolveProject,
    resolveDocument: documents.resolveDocument,
    hasDocuments: documents.hasDocuments,
  };
  const fold = buildFold(archive.fold);

  /* 얼려 올 기준이 **지금과 같은 아카이브**에서 나온 것인지 본다. 로그만 대는 검사는
   * 「로그는 그대로인데 아카이브만 자란」 경우를 통과시킨다 (archiveSignature 주석). */
  const signature = archiveSignature(cfg);
  if (frozen) {
    if (!baseSignature) {
      throw new Error('기준 산출에 아카이브 서명이 없습니다 — 이 검사가 생기기 전에 만든 '
        + '기준입니다. `--point baseline` 을 다시 돌려 기준부터 새로 만드세요.');
    }
    if (baseSignature !== signature) {
      throw new Error(`아카이브가 기준을 잰 때와 다릅니다 — 기준 ${baseSignature} vs 지금 ${signature}. `
        + '이 상태로 대면 아카이브가 자란 것이 상한 탓으로 보입니다. 기준부터 다시 재세요.');
    }
  }

  const all = await loadAllEntries();
  // 재생 표본 = search 호출이 있는 문답. 나머지는 재생할 것이 없다.
  const sample = all.filter((e) => e.calls.some((c) => c.name === 'search' && c.args));
  // 라벨을 받았으면 **돌기 전에** 모양을 본다. 어긋난 채 돌면 그 회차만 조용히 지점
  // 재계산으로 되돌아가 생존률이 100% 로 찍힌다 (assertOriginsShape 주석).
  if (origins) assertOriginsShape(origins, sample.length);

  const counts = {
    분해보류: 0, 못쟀다: 0, 기준불일치: 0, 검색밖: 0, 검색밖전용: 0, 못잼: 0,
  };
  const diagnostics = {
    근거줄없음: 0,
    출처줄없음: 0,
    근거항목: 0,
    미재현근거: 0,
    잴수있는회차: 0,
    못잼_실시간조회: 0,
    못잼_분해보류: 0,
    못푼읽기호출: 0,
    추정으로뺀읽기호출: 0,
    읽기키인데재생도냄: 0,
    보류한도구글자: 0,
    크기대조: [],
  };
  const entries = [];
  const originsOut = [];

  for (let idx = 0; idx < sample.length; idx += 1) {
    const entry = sample[idx];
    const replay = await replayEntry(entry, mods);
    const sets = originSets(replay.inputs, replay.access, mods);

    const touchedKeys = new Set([...replay.touched].map(identityKey));
    const evidenceKeys = entry.evidence.map(identityKey);
    if (!evidenceKeys.length) diagnostics.근거줄없음 += 1;
    diagnostics.근거항목 += evidenceKeys.length;

    /* **분모는 로그 자신의 도구 인자로 만든다.** 재생 결과로 분모를 정의하면 분모가
     * 정의상 재생 결과의 부분집합이 되어 겹침이 항상 1.0 이 된다(수정 라운드 1 전의 결함). */
    const off = offSearchKeys(entry, replay.access, mods, evidenceKeys);
    diagnostics.못푼읽기호출 += off.unresolved;
    diagnostics.추정으로뺀읽기호출 += off.inferred;

    /* 출신 라벨은 **기준에서 한 번만** 매긴다. 지점마다 다시 매기면 상한을 낮출 때
     * touched 와 sets 가 함께 줄어 그 지점에서도 `survived ≡ (origin≠검색밖)` 이 되고
     * 축별 생존률이 구조상 100% 가 된다 (설계서 §3.3 — 라벨링은 기준 재생에서). */
    /* **두 겹으로 막는다.** 위 `assertOriginsShape` 가 길이·모양을 미리 보지만, 그 줄
     * 하나가 없어지면 여기서 `undefined` 가 되어 **이 회차만 조용히 지점 재계산으로
     * 되돌아간다**(생존률이 그 회차만 100%, 에러는 없음). 꺼내는 자리에서도 던진다. */
    /* 이름이 `frozenLabel` 인 것이 요점이다 — 매개변수 `frozen`(기준 회차 배열)과 다른
     * 것이다. 예전엔 이 변수도 `frozen` 이라 매개변수를 가렸고, 지금은 블록 스코프라
     * 동작이 맞지만 **호출을 이 루프 안으로 옮기는 순간 에러 없이 다른 값**이 들어간다. */
    let frozenLabel = null;
    if (origins) {
      frozenLabel = origins[idx];
      if (!frozenLabel || typeof frozenLabel !== 'object' || Array.isArray(frozenLabel)) {
        throw new Error(`origins[${idx}] 가 없거나 라벨 객체가 아닙니다 — 조용히 재계산으로 `
          + '되돌아가면 그 회차만 생존률 100% 가 됩니다.');
      }
    }
    const originOf = (k) => {
      if (frozenLabel) return frozenLabel[k] ?? '미재현';
      if (sets.hits.has(k)) return 'hits';
      if (sets.partial.has(k)) return 'partial';
      if (sets.outside.has(k)) return 'outside';
      // 로그의 read_channel·read_document 인자로 **증명되는** 것만 검색밖이다.
      if (off.keys.has(k)) return '검색밖';
      // 증명 안 된 미출현 = 재생이 못 낸 것. 이것이 기준 불일치의 재료다.
      return '미재현';
    };
    const label = {};
    for (const k of evidenceKeys) label[k] = originOf(k);
    originsOut.push(label);

    const hitsFor = (k) => [
      ...(sets.hits.get(k) || []), ...(sets.partial.get(k) || []), ...(sets.outside.get(k) || []),
    ];

    /* evidence″ = 근거 − 증명 가능한 읽기 호출 키.
     *
     * **읽기 호출로 열린 것을 재생이 검색으로도 냈을 수 있다.** 그래도 분모에서 뺀다 —
     * 그때 봇이 그것을 검색으로 봤는지 읽기로 봤는지 로그가 안 가르므로, 재생이 오늘
     * 냈다는 사실이 「그때 검색이 냈다」의 근거가 못 된다. 뺀 쪽이 보수적이다
     * (성공 한 칸을 분자·분모에서 함께 뺀다).
     *
     * 그래서 이 칸(`counts.검색밖`)과 출신 라벨 `검색밖` 은 **수가 다르다** — 라벨은
     * 히트/부분/밖을 먼저 보므로, 재생이 검색으로도 낸 것은 `hits` 로 남는다. 차이를
     * 따로 세어 둔다(안 세면 두 숫자가 안 맞는 이유를 다음 사람이 못 찾는다). */
    const evPrime = evidenceKeys.filter((k) => !off.keys.has(k));
    counts.검색밖 += evidenceKeys.length - evPrime.length;
    diagnostics.읽기키인데재생도냄 += evidenceKeys
      .filter((k) => off.keys.has(k) && touchedKeys.has(k)).length;
    const missed = evPrime.filter((k) => !touchedKeys.has(k));
    diagnostics.미재현근거 += missed.length;

    /* 「못 잼」 — 겹침을 아예 못 재는 회차. 「통과」로 세면 안 된다.
     *  ① fetch_recent_slack 을 부른 회차: 실시간 창의 임의 채널이 touched 에 들어가는데
     *     인자로 되살릴 수 없다(claude.js 의 fetch_recent_slack run).
     *  ② 분해 보류 호출이 있는 회차: 그 호출을 안 돌렸으므로 재생이 불완전하다. */
    const liveFetch = entry.calls.some((c) => c.name === 'fetch_recent_slack');
    const unmeasurable = liveFetch || replay.held > 0;
    if (liveFetch) diagnostics.못잼_실시간조회 += 1;
    if (!liveFetch && replay.held > 0) diagnostics.못잼_분해보류 += 1;

    const overlap = (unmeasurable || !evPrime.length)
      ? null
      : evPrime.filter((k) => touchedKeys.has(k)).length / evPrime.length;
    const baselineOk = overlap === null ? null : overlap >= 0.5;
    if (unmeasurable) counts.못잼 += 1;
    else if (!evPrime.length) counts.검색밖전용 += 1;
    else {
      diagnostics.잴수있는회차 += 1;
      if (baselineOk === false) counts.기준불일치 += 1;
    }

    const citations = [];
    if (!entry.sources || !entry.sources.length) diagnostics.출처줄없음 += 1;
    for (const item of entry.sources || []) {
      const ev = matchSource(item, entry.evidence, fold);
      if (!ev) {
        // 0 으로 접지 않는다 — 「인용이 없다」와 「어느 근거인지 못 쟀다」는 다른 칸이다.
        counts.못쟀다 += 1;
        citations.push({
          item, evidence: null, origin: null, survived: null, echoOnly: null, afterOnly: null,
        });
        continue;
      }
      const key = identityKey(ev);
      const origin = originOf(key);
      const pool = hitsFor(key);
      citations.push({
        item,
        evidence: ev,
        origin,
        survived: touchedKeys.has(key),
        /* **이 인용의 근거가 이 지점에서 실제로 달라졌나**를 재는 재료. 기준의 것과
         * 다르면 그 인용은 이 상한에 **닿은** 것이고, 같으면 상한이 건드리지도 않은
         * 것이다(= 살아남은 것이 아니라 위험에 노출된 적이 없는 것).
         * 건수만 재면 **구성만 바뀐 축**(perChannel·perProject)을 통째로 놓치므로
         * 히트의 신원까지 넣어 해시한다. */
        poolSize: pool.length,
        poolSig: poolSignature(pool),
        // 반향(질문 자신이 아카이브에 남아 자기 질의에 걸리는 것)만으로 산 채널은
        // 거짓 생존이다. 문서에는 해당이 없어 false 로 둔다.
        echoOnly: key.startsWith('📄')
          ? false
          : pool.length > 0 && pool.every((h) => mods.isEchoEntry(h.text)),
        // 질문 **뒤에** 그 문서에 붙은 회차만 남아 산 것 — 위쪽 편향이다.
        afterOnly: key.startsWith('📄')
          ? pool.length > 0 && pool.every((h) => h.date && h.date > entry.date)
          : false,
      });
    }

    counts.분해보류 += replay.held;
    diagnostics.보류한도구글자 += replay.heldChars;
    for (const c of replay.calls) {
      if (c.loggedChars != null) diagnostics.크기대조.push([c.loggedChars, c.replayChars]);
    }

    entries.push({
      // 같은 분·같은 사람 회차가 실물에 여럿 있어 날짜·시각만으로는 안 갈린다 —
      // 표본 안 순번을 붙인다. `origins` 배열의 첨자와 같은 순번이다.
      id: `${entry.date} ${entry.time} #${idx}`,
      date: entry.date,
      baselineOk,
      overlap,
      measurable: !unmeasurable && evPrime.length > 0,
      unmeasurableReason: liveFetch ? '실시간조회' : (replay.held > 0 ? '분해보류' : null),
      evidenceCount: evidenceKeys.length,
      // 로그 인자로 증명된 검색밖 / 증명 안 된 미재현 — 두 칸을 가른다.
      offSearchEvidence: evidenceKeys.length - evPrime.length,
      missedEvidence: missed.length,
      offSearchCalls: entry.calls.filter((c) => c.name && c.name !== 'search').length,
      citations,
      toolChars: replay.toolChars,
      // 위쪽 편향(새 회차가 옛 인용을 살리는 것) 확인용 — 채널은 citations 의
      // survived 가 들고 있다.
      touchedDocs: [...touchedKeys].filter((k) => k.startsWith('📄')).sort(),
    });
  }

  /* 표본을 가르는 칸은 기준 것으로 못 박는다 (freezeFromBaseline 주석 — 지점마다 다시
   * 계산하면 표본이 흔들려 지점끼리 댈 수 없다). 기준 실행에는 `frozen` 이 없다. */
  if (frozen) freezeFromBaseline(entries, frozen);

  /* 기준에서 매긴 출신 라벨을 그대로 싣는다 — Task 5 가 지점마다 이것을 `origins` 로
   * 되돌려 넣어야 축별 생존률이 구조상 100% 가 되는 것을 막는다. 배열 첨자가 곧
   * `entries` 의 첨자다. */
  return {
    axis,
    value,
    injected,
    // 아카이브 상태 서명 — 지점이 이 기준을 얼려 써도 되는지 대는 값(archiveSignature).
    signature,
    // 이 `entries` 가 어느 규칙판으로 찍혔나. 이름표(사람용)와 지문(강제용) 둘 다 찍는다 —
    // `--summary` 가 둘을 대고 지금 코드와 다르면 던진다(낡은 재생 위에 새 표 금지).
    entryRules: ENTRY_RULES,
    entryFingerprint: entryFingerprint(),
    // 얼린 칸이 무엇인지 산출에 남긴다 — 「이 표의 measurable 은 어느 실행 것인가」는
    // 몇 주 뒤에 반드시 다시 묻게 되는 질문이다.
    frozenFields: frozen
      ? { entry: ['measurable', 'baselineOk', 'overlap'], citation: [] }
      : null,
    entries,
    counts,
    diagnostics,
    origins: origins ?? originsOut,
  };
}

/**
 * 지점 결과를 **기준으로 얼린 표본** 위에서 센다.
 *
 * 표본: `baselineOk === true` 회차(= 기준 재생이 그때의 근거 줄을 절반 넘게 되살린 회차).
 * 기준 불일치·못 잼 회차는 뺀다 — 그 죽음은 상한 탓이 아니다(설계서 §3.3).
 *
 * 분모는 **그 축이 실제로 자르는 출신만**이어야 하므로(§3.3) 여기서는 한 칸으로 접지 않고
 * 출신×종류(채널/문서)로 **갈라서만** 낸다. 축을 어느 칸에 걸지는 `AXIS_BUCKET` 이 정한다.
 */

/**
 * 축이 **실제로 자르는** 출신 칸. 코드에서 읽은 것이고, 짐작이 아니다.
 *
 *  - `searchMaxHits` 는 `scanArchive` 의 확정 히트 상한이다(`archive.js` 의 `maxHits`).
 *    `outside`(밖 결과)는 **좁히지 않고 한 번 더 돌린 그 scan 결과에서 잘라 오므로**
 *    (`archive.js` 의 wide → `.slice(0, max)`) 같은 상한에 걸린다.
 *  - `searchMaxPerChannel` 은 확정 히트의 `pickSpread` 뿐 아니라 **부분 일치의
 *    `pickSpread` 에도 그대로 넘어간다**(`archive.js` 의 partialGroups 호출) — 즉
 *    `partial.ch` 도 자른다. wide 를 거치는 `outside.ch` 도 마찬가지다.
 *  - 문서 쪽도 같은 모양이다(`documents.js` 의 partialGroups 호출에 `perProject`).
 *  - `partialHitMaxHits` 도 **`outside` 를 자를 수 있다.** 발동 가드가 보는 것은 **좁힌
 *    쪽(`base`)에 확정 히트가 없나**이고, 잘라 오는 것은 **넓게 다시 돌린 `wide.hits`** 다.
 *    `wide` 는 보통 확정 히트고(안전망의 존재 이유가 「밖에는 확정이 있다」이다), 그때는
 *    `searchMaxHits`·`perChannel` 이 자른다. 다만 `wide` 의 확정이 2건 이하이면
 *    `partialAlsoWhenAtMost` 가 **부분 일치를 `hits` 뒤에 붙이므로**(`archive.js` 의
 *    `few`/`extra`, `documents.js` 의 같은 블록) 그 몫은 `partialHitMaxHits` 가 자른다.
 *    산출만으로는 어느 쪽인지 못 가르므로 **자를 수 있는 축 전부**에 넣는다.
 *
 * [수정 라운드 2 — 2026-09-12] 그전 판은 다섯 축 전부 `hits.*` 만 봤다. `perChannel`·
 * `perProject` 가 partial 도 자른다는 것을 놓쳐서 분모가 작았다.
 *
 * [수정 라운드 3 — 2026-09-12] 라운드 2 는 `outside` 를 `partialHitMaxHits` 분모에서
 * **뺐고**, 이유를 「그 outside 가 부분이었나를 가를 수 없다 · 넣으면 생존률이 위로
 * 부푼다」고 적었다. **둘 다 틀렸다.**
 *  - 가를 수 있다: 라운드 2 에 넣은 `poolSig` 가 인용별로 「이 상한이 이 근거를
 *    건드렸나」를 직접 말한다. 실측에서 `outside.ch` 노출은 `partialHitMaxHits=4` 에서 1건,
 *    `=8` 에서 0건, `searchMaxHits`·`searchMaxPerChannel` 의 모든 지점에서 0건이다 —
 *    이 표본에서 이 출신을 실제로 자른 것은 부분 일치 상한뿐이었다(위 `partialAlsoWhenAtMost`
 *    경로). 「원리상 이 축만 자른다」는 뜻이 아니다 — 그래서 분모에는 셋 다 넣는다.
 *  - 방향이 반대다: 빼면 생존률이 **위로** 부푼다. 뺀 판에서 `12→4` 는 81.1%(30/37)인데,
 *    넣으면 **79.5%(31/39)** 다. 빼는 쪽이 **실제로 죽은 인용 하나를 분모 밖에 숨기고**
 *    있었다 (2026-09-07 회차의 `outside.ch` — 기준 pool 1건 → `=4` 에서 0건, 사망).
 *    전 지점 죽은 인용 21건 중 유일하게 자기 축 분모 밖에서 죽던 건이다.
 */
export const AXIS_BUCKET = {
  searchMaxHits: ['hits.ch', 'outside.ch'],
  searchMaxPerChannel: ['hits.ch', 'partial.ch', 'outside.ch'],
  docSearchMaxHits: ['hits.doc', 'outside.doc'],
  docSearchMaxPerProject: ['hits.doc', 'partial.doc', 'outside.doc'],
  partialHitMaxHits: ['partial.ch', 'partial.doc', 'outside.ch', 'outside.doc'],
};

/* 건수를 안 바꾸고 **구성만** 바꾸는 축. pickSpread 가 자리마다 perGroup 건까지 고른 뒤
 * 남는 예산을 최근순으로 채우므로, 총 히트가 상한보다 적으면 원리상 못 문다
 * (`src/archive.js` 의 pickSpread ②). 노출이 0 일 때 「왜 0 인가」를 표에 적어 주려고 둔다. */
const PER_GROUP_AXES = new Set(['searchMaxPerChannel', 'docSearchMaxPerProject']);

/* 노출이 이 수 미만이면 생존률을 **읽지 말라고** 표시한다. 열 몇 건짜리 분모에서 나온
 * 100% 는 「안전하다」가 아니라 「이 표본으로는 못 쟀다」다. */
const EXPOSED_MIN = 10;

export function summarize(r, base) {
  /* **노출** = 이 상한이 **실제로 건드린** 인용. 이 칸이 없으면 「한 번도 안 물린 축」이
   * 생존률 100% 로 찍혀 **「낮춰도 안전하다」로 읽힌다.** 그건 측정 결과가 아니라 미측정이다.
   *
   * 두 가지를 따로 센다.
   *  - `exposed`(인용 단위, **정확**): 그 인용의 근거 히트 묶음(`poolSig`)이 기준과 다른가.
   *    구성만 바뀐 것도 잡고, 같은 회차의 다른 인용이 잘렸다고 해서 딸려 세어지지 않는다.
   *  - `exposedMax`(회차 단위, **최대치**): 그 회차의 도구 글자가 달라졌나. 라운드 1 이
   *    쓰던 값이고, 같은 회차의 모든 분모 인용을 노출로 세므로 **과대**다.
   *    `poolSig` 가 없는 낡은 산출에서도 셀 수 있어 함께 남긴다.
   */
  const exposedEntry = new Set();
  if (base && Array.isArray(base.entries)) {
    if (base.entries.length !== r.entries.length) {
      throw new Error(`기준·지점 회차 수가 다릅니다 — 기준 ${base.entries.length} vs 지점 ${r.entries.length}`);
    }
    for (let i = 0; i < r.entries.length; i += 1) {
      if (base.entries[i].id !== r.entries[i].id) {
        throw new Error(`첨자 ${i} 의 회차가 다릅니다 — 기준 「${base.entries[i].id}」 vs 지점 「${r.entries[i].id}」`);
      }
      if (base.entries[i].citations.length !== r.entries[i].citations.length) {
        throw new Error(`첨자 ${i}(${r.entries[i].id}) 의 인용 수가 다릅니다 — `
          + `기준 ${base.entries[i].citations.length} vs 지점 ${r.entries[i].citations.length}`);
      }
      if (base.entries[i].toolChars !== r.entries[i].toolChars) exposedEntry.add(i);
    }
  }
  const bucket = {};
  const add = (k, c, exposed, exposedMax) => {
    if (!bucket[k]) {
      bucket[k] = {
        n: 0,
        baseAlive: 0,
        exposed: 0,
        exposedMax: 0,
        survived: 0,
        survivedReal: 0,
        echoOnly: 0,
        afterOnly: 0,
        afterOnlyBase: 0,
      };
    }
    const b = bucket[k];
    b.n += 1;
    if (c.baselineSurvived) b.baseAlive += 1;
    // 노출은 **분모(기준에서 살아 있던 인용) 안에서만** 센다 — 생존률의 분모와 같은 집합이라야
    // 「분모 18 중 16 은 아무 일도 안 겪었다」를 그대로 읽을 수 있다.
    if (c.baselineSurvived && exposed) b.exposed += 1;
    if (c.baselineSurvived && exposedMax) b.exposedMax += 1;
    if (c.survived) b.survived += 1;
    if (c.survived && !c.echoOnly) b.survivedReal += 1;
    if (c.survived && c.echoOnly) b.echoOnly += 1;
    // 위쪽 편향은 **이 지점에서** 잰 것이 본값이고(설계서 §3.5 「살아남은 히트」),
    // 기준 것도 함께 센다 — 둘이 벌어지는 폭 자체가 그 축을 얼마나 못 믿을지를 말한다.
    if (c.afterOnly) b.afterOnly += 1;
    if (c.baselineAfterOnly) b.afterOnlyBase += 1;
  };
  let poolSigMissing = 0;
  for (let i = 0; i < r.entries.length; i += 1) {
    const e = r.entries[i];
    if (e.baselineOk !== true) continue;
    for (let j = 0; j < e.citations.length; j += 1) {
      const c = e.citations[j];
      if (!c.evidence || !c.origin) continue;
      const bc = base && base.entries ? base.entries[i].citations[j] : null;
      /* 인용 단위 노출 — 근거 묶음이 기준과 다른가. 한쪽에 `poolSig` 가 없으면(라운드 2
       * 전 산출) **셀 수 없는 것**이라 세지 않고 따로 헤아린다. 0 으로 접으면 「아무것도
       * 노출 안 됐다」가 되어 전 축이 「못 잼」으로 찍힌다 — 조용히 틀리는 쪽이다. */
      const canPool = !!(c.poolSig && bc && bc.poolSig);
      if (!canPool) poolSigMissing += 1;
      const exposed = canPool && c.poolSig !== bc.poolSig;
      const kind = identityKey(c.evidence).startsWith('📄') ? 'doc' : 'ch';
      add(`${c.origin}.${kind}`, c, exposed, exposedEntry.has(i));
    }
  }
  // 절감률의 분자·분모는 **같은 회차 집합**이어야 한다 — 기준 쪽 회차도 얼린 칸으로 고른다.
  const okIdx = new Set(r.entries.map((e, i) => (e.baselineOk === true ? i : -1)).filter((i) => i >= 0));
  const sumOk = (list) => list.filter((_, i) => okIdx.has(i)).reduce((a, e) => a + e.toolChars, 0);
  const pointChars = sumOk(r.entries);
  const baseChars = base ? sumOk(base.entries) : null;

  /* 축 한 줄로 접은 것 — Task 6 의 표가 이 칸을 그대로 쓴다. **`잼` 이 false 면 생존률을
   * 표에 쓰면 안 된다**(미측정이지 안전 확인이 아니다). */
  const keys = AXIS_BUCKET[r.axis] || [];
  const pick = (f) => keys.reduce((a, k) => a + (bucket[k] ? bucket[k][f] : 0), 0);
  const 분모 = pick('baseAlive');
  const 노출 = pick('exposed');
  const 생존 = pick('survived');
  const 잼 = 노출 >= EXPOSED_MIN;
  const axisView = r.axis ? {
    axis: r.axis,
    value: r.value,
    buckets: keys,
    분모,
    // 인용 단위(정확). 아래 `노출_회차기준상한` 은 회차 단위라 **과대**다.
    노출,
    노출_회차기준상한: pick('exposedMax'),
    생존,
    잼,
    /* **`잼:false` 면 생존률을 아예 `null` 로 둔다.** 주석으로 「쓰지 마세요」라고만 하면
     * 표를 만드는 쪽이 그 칸을 집어 미측정 100% 를 그대로 싣는다 — 막는 것은 데이터라야
     * 한다. 숫자가 필요하면 `생존`·`분모` 원시값이 그대로 있다. */
    생존률: 잼 && 분모 ? 생존 / 분모 : null,
    // 표기가 null 이 아니면 **그 문구를 생존률 대신 쓴다.**
    표기: 잼 ? null
      : `못 잼 — 분모 ${분모}건 중 이 상한이 실제로 문 인용이 ${노출}건뿐입니다`
        + (PER_GROUP_AXES.has(r.axis)
          ? ' (이 축은 건수를 안 바꾸고 구성만 바꾼다 — pickSpread 가 남는 예산을 최근순으로 채운다)'
          : ' (이 표본에서는 상한이 거의 안 찬다)'),
  } : null;

  return {
    표본회차: r.entries.filter((e) => e.baselineOk === true).length,
    잴수있는회차: r.entries.filter((e) => e.measurable).length,
    노출회차: exposedEntry.size,
    // 0 이 아니면 인용 단위 노출을 그만큼 **못 센 것**이다 (낡은 산출에 섞여 들었다는 뜻).
    poolSig없는인용: poolSigMissing,
    axisView,
    bucket,
    도구글자_표본: pointChars,
    기준도구글자_표본: baseChars,
    절감률: baseChars ? (baseChars - pointChars) / baseChars : null,
    도구글자_전체: r.entries.reduce((a, e) => a + e.toolChars, 0),
    기준도구글자_전체: base ? base.entries.reduce((a, e) => a + e.toolChars, 0) : null,
  };
}

/**
 * 산출 하나가 **지금 코드와 같은 재생 규칙**으로 찍혔는지 댄다. 어긋나면 던진다 —
 * 표만 새 규칙으로 다시 만들면 낡은 `entries` 위에 새 표가 올라앉는데 에러가 안 난다.
 *
 * 이름표(`entryRules`)와 지문(`entryFingerprint`)을 **둘 다** 본다. 이름표는 사람이
 * 읽으라고 있는 것이고, 실제로 막는 것은 지문이다(이름표는 올리는 것을 잊을 수 있다).
 */
function assertEntryStamp(obj, what) {
  const fp = entryFingerprint();
  if (obj.entryRules !== ENTRY_RULES || obj.entryFingerprint !== fp) {
    throw new Error(`${what} 산출이 다른 재생 규칙으로 찍혔습니다 — `
      + `산출 ${obj.entryRules ?? '(없음)'}/${obj.entryFingerprint ?? '(지문 없음)'} vs `
      + `지금 코드 ${ENTRY_RULES}/${fp}. `
      + '표만 다시 만들면 낡은 재생 위에 새 규칙이 올라앉습니다 — `--all` 로 다시 재세요.');
  }
}

/**
 * 격자 산출 12개를 읽어 **각 지점의 `summary` 를 지금 코드로 다시 계산해 덮어쓰고**,
 * 축별 한 줄 표(`_grid-summary.json`)를 쓴다.
 *
 * 다시 계산하는 것이 요점이다 — 요약 규칙이 바뀌었을 때 20분짜리 재생을 다시 돌리지 않고
 * 표만 맞출 수 있다. 재생 결과(`entries`)는 **안 건드린다.** 계산은 `summarize` 한 벌이라
 * 실행 중에 낸 값과 여기서 낸 값이 갈릴 수 없다.
 */
export function writeGridSummary(outDir) {
  const baseFile = path.join(outDir, 'baseline.json');
  if (!fs.existsSync(baseFile)) throw new Error(`기준 산출이 없습니다: ${baseFile}`);
  const base = JSON.parse(fs.readFileSync(baseFile, 'utf-8'));
  /* **기준도 댄다.** 라운드 2 판은 지점 파일만 대고 기준은 `signature` 만 봤는데, 기준은
   * `origins`·얼리는 칸·`baselineSurvived`·`poolSig` 의 **출처**라 낡으면 표 전체가 섞인다
   * (검수 실증: 기준만 옛 규칙판으로 바꿔도 EXIT 0 으로 통과했다). */
  assertEntryStamp(base, '기준');
  const rows = [];
  /* 서명이 **없는** 산출을 조용히 통과시키지 않는다 — 이 검사가 생기기 전 판이라는 뜻이고,
   * 그런 지점은 「같은 아카이브에서 나왔다」가 확인된 것이 아니라 **확인 안 된 것**이다. */
  const 서명없음 = [];
  for (const [axis, value] of GRID) {
    const file = path.join(outDir, `${axis}-${value}.json`);
    if (!fs.existsSync(file)) throw new Error(`지점 산출이 없습니다: ${file}`);
    const p = JSON.parse(fs.readFileSync(file, 'utf-8'));
    if (!p.signature || !base.signature) 서명없음.push(`${axis}=${value}`);
    else if (p.signature !== base.signature) {
      throw new Error(`${axis}=${value} 는 다른 아카이브 상태에서 나왔습니다 — `
        + `지점 ${p.signature} vs 기준 ${base.signature}`);
    }
    assertEntryStamp(p, `${axis}=${value}`);
    p.summary = summarize(p, base);
    fs.writeFileSync(file, JSON.stringify(p, null, 2), 'utf-8');
    const s = p.summary;
    rows.push({
      axis,
      value,
      before: p.injected ? p.injected.before : null,
      분모: s.axisView.분모,
      노출: s.axisView.노출,
      노출_회차기준상한: s.axisView.노출_회차기준상한,
      생존: s.axisView.생존,
      // **`잼:false` 면 `null`** — 미측정 100% 가 표로 새어 나가는 길을 데이터에서 막는다.
      생존률: s.axisView.생존률,
      잼: s.axisView.잼,
      표기: s.axisView.표기,
      절감률: s.절감률,
      절감률_전체: s.기준도구글자_전체
        ? (s.기준도구글자_전체 - s.도구글자_전체) / s.기준도구글자_전체 : null,
      노출회차: s.노출회차,
      도구글자_표본: s.도구글자_표본,
    });
  }
  const out = {
    기준: {
      signature: base.signature ?? null,
      회차: base.entries.length,
      잴수있는회차: base.entries.filter((e) => e.measurable).length,
      표본회차: base.entries.filter((e) => e.baselineOk === true).length,
      도구글자_표본: base.entries.filter((e) => e.baselineOk === true)
        .reduce((a, e) => a + e.toolChars, 0),
      도구글자_전체: base.entries.reduce((a, e) => a + e.toolChars, 0),
    },
    // 비어 있어야 정상이다. 이름이 들어 있으면 그 지점은 「같은 아카이브」가 확인 안 된 것.
    서명확인안된지점: 서명없음,
    rows,
  };
  const file = path.join(outDir, '_grid-summary.json');
  fs.writeFileSync(file, JSON.stringify(out, null, 2), 'utf-8');
  return { file, out };
}

/* 자가검사 둘째 자리 — `summarize` 는 `identityKey`(파일 중간의 `const`)를 쓰므로 위쪽
 * 자가검사 블록에서 부르면 TDZ 로 죽는다. 그래서 정의가 다 끝난 여기서 한 번 더 연다. */
if (process.argv.includes('--selftest')) {
  const cite = (ev, over) => ({
    item: ev,
    evidence: ev,
    origin: 'hits',
    survived: true,
    baselineSurvived: true,
    echoOnly: false,
    afterOnly: false,
    baselineAfterOnly: false,
    poolSize: 3,
    poolSig: 'aaaa',
    ...over,
  });
  const entry = (id, over) => ({
    id, baselineOk: true, measurable: true, toolChars: 100, citations: [cite('#가짜채널')], ...over,
  });
  {
    /* 노출 = 그 인용의 **근거 묶음(poolSig)이 달라진** 것. 회차의 다른 인용이 잘렸다고
     * 딸려 세어지면 안 된다 — 아래 회차 a 는 인용 둘인데 하나만 묶음이 달라졌다. */
    const r = {
      axis: 'searchMaxHits',
      value: 12,
      entries: [
        entry('a', {
          toolChars: 90,
          citations: [
            cite('#가짜채널', { survived: false, poolSig: 'zzzz' }),
            cite('#가짜채널2'),
          ],
        }),
        entry('b'),
        entry('c', { baselineOk: false, toolChars: 50 }),
      ],
    };
    const base = {
      entries: [
        entry('a', { citations: [cite('#가짜채널'), cite('#가짜채널2')] }),
        entry('b'),
        entry('c', { baselineOk: false }),
      ],
    };
    const s = summarize(r, base);
    assert.equal(s.bucket['hits.ch'].n, 3);
    assert.equal(s.bucket['hits.ch'].baseAlive, 3);
    // 인용 단위(정확) 1건 — 회차 단위(상한)로는 2건. 이 둘이 갈리는 것이 요점이다.
    assert.equal(s.bucket['hits.ch'].exposed, 1);
    assert.equal(s.bucket['hits.ch'].exposedMax, 2);
    assert.equal(s.bucket['hits.ch'].survived, 2);
    // 표본 밖(baselineOk=false) 회차는 글자가 달라져도 축 노출로 안 센다.
    assert.equal(s.노출회차, 2);
    assert.equal(s.axisView.분모, 3);
    assert.equal(s.axisView.노출, 1);
    assert.equal(s.axisView.노출_회차기준상한, 2);
    // **여기가 요점** — 노출이 한 자릿수면 `잼:false` 라 생존률을 쓰면 안 된다고 말한다.
    assert.equal(s.axisView.잼, false);
    assert.match(s.axisView.표기, /못 잼/);
    assert.match(s.axisView.표기, /상한이 거의 안 찬다/);
    // [중요 3] 데이터가 막는다 — `잼:false` 면 생존률 칸 자체가 null 이다.
    assert.equal(s.axisView.생존률, null);
  }
  {
    /* [중요 4] **노출은 분모(기준에서 살아 있던 인용) 안에서만 센다.** 아래 인용은
     * 묶음이 달라졌지만 기준에서 이미 죽어 있었으므로 노출이 아니다 —
     * `if (c.baselineSurvived && exposed)` 에서 앞 조건을 빼면 이 assert 가 빨개진다. */
    const dead = cite('#가짜채널', { baselineSurvived: false, survived: false, poolSig: 'zzzz' });
    const r = { axis: 'searchMaxHits', value: 12, entries: [entry('a', { toolChars: 90, citations: [dead] })] };
    const s = summarize(r, { entries: [entry('a', { citations: [cite('#가짜채널', { baselineSurvived: false })] })] });
    assert.equal(s.bucket['hits.ch'].n, 1);
    assert.equal(s.bucket['hits.ch'].baseAlive, 0);
    assert.equal(s.bucket['hits.ch'].exposed, 0);
    assert.equal(s.bucket['hits.ch'].exposedMax, 0);
    assert.equal(s.axisView.분모, 0);
    assert.equal(s.axisView.노출, 0);
  }
  {
    // 건수를 안 바꾸는 축은 「왜 0 인가」를 다르게 적는다.
    const r = {
      axis: 'searchMaxPerChannel', value: 3, entries: [entry('a')],
    };
    const s = summarize(r, { entries: [entry('a')] });
    assert.equal(s.axisView.노출, 0);
    assert.match(s.axisView.표기, /구성만 바꾼다/);
    // partial·outside 도 이 축의 분모다 (라운드 2 정정 — perChannel 은 partial 도 자른다).
    assert.deepEqual(s.axisView.buckets, ['hits.ch', 'partial.ch', 'outside.ch']);
  }
  {
    /* [라운드 3] `partialHitMaxHits` 분모에 **`outside` 가 들어간다.** 뺐더니 실제로 죽은
     * 인용 하나(2026-09-07 회차의 outside.ch)가 분모 밖에 숨어 생존률이 1.6%p 위로
     * 부풀었다. 이 줄을 되돌리면(=`outside` 를 빼면) 아래 assert 가 빨개진다. */
    assert.deepEqual(
      AXIS_BUCKET.partialHitMaxHits,
      ['partial.ch', 'partial.doc', 'outside.ch', 'outside.doc'],
    );
    const dead = cite('#가짜채널', { origin: 'outside', survived: false, poolSig: 'empty' });
    const r = {
      axis: 'partialHitMaxHits',
      value: 4,
      entries: [entry('a', { toolChars: 90, citations: [dead] })],
    };
    const base = { entries: [entry('a', { citations: [cite('#가짜채널', { origin: 'outside' })] })] };
    const s = summarize(r, base);
    // outside 출신이 분모에 들고, 죽은 것이 죽은 것으로 세어진다.
    assert.equal(s.axisView.분모, 1);
    assert.equal(s.axisView.노출, 1);
    assert.equal(s.axisView.생존, 0);
  }
  {
    // 지문은 형태가 고정이고 같은 코드에서 두 번 불러도 같다 — 이름표와 달리 손이 안 탄다.
    const fp = entryFingerprint();
    assert.match(fp, /^[0-9a-f]{12}$/);
    assert.equal(fp, entryFingerprint());
  }
  {
    /* [라운드 4] **모든 축에 분모 칸이 있어야 한다.** `summarize` 는
     * `AXIS_BUCKET[axis] || []` 로 받으므로, 축이 빠지면 던지지 않고 **분모 0 · 노출 0 →
     * 「못 잼 0/0」** 으로 조용히 찍힌다. 축을 더할 때 이 줄이 없으면 그 축만 표에서
     * 사라진다. (`GRID ⊂ CAP_AXES` 는 위 apply-cap 블록이 본다.) */
    for (const a of CAP_AXES) {
      assert.ok(
        Array.isArray(AXIS_BUCKET[a]) && AXIS_BUCKET[a].length > 0,
        `${a} 에 분모 칸(AXIS_BUCKET)이 없습니다 — 표에 「못 잼 0/0」 으로 조용히 찍힙니다`,
      );
    }
    // 분모 칸 이름도 `출신.종류` 모양이어야 한다 — 오타는 그냥 0건으로 세어진다.
    for (const ks of Object.values(AXIS_BUCKET)) {
      for (const k of ks) assert.match(k, /^(hits|partial|outside)\.(ch|doc)$/);
    }
  }
  {
    // 노출이 10건 이상이면 잰 것으로 본다 — 그때만 표기가 null 이고 생존률이 숫자다.
    const many = entry('a', {
      toolChars: 90,
      citations: Array.from({ length: 10 }, (_, i) => cite(`#가짜채널${i}`, { poolSig: `z${i}` })),
    });
    const baseMany = entry('a', { citations: Array.from({ length: 10 }, (_, i) => cite(`#가짜채널${i}`)) });
    const s = summarize({ axis: 'searchMaxHits', value: 12, entries: [many] }, { entries: [baseMany] });
    assert.equal(s.axisView.노출, 10);
    assert.equal(s.axisView.잼, true);
    assert.equal(s.axisView.표기, null);
    assert.equal(s.axisView.생존률, 1);
  }
  {
    // `poolSig` 가 없는 낡은 산출은 **0 으로 접지 않고** 못 센 건수를 따로 낸다.
    const old = entry('a', { toolChars: 90, citations: [cite('#가짜채널', { poolSig: undefined })] });
    const s = summarize({ axis: 'searchMaxHits', value: 12, entries: [old] }, { entries: [entry('a')] });
    assert.equal(s.poolSig없는인용, 1);
    assert.equal(s.axisView.노출, 0);
    assert.equal(s.axisView.노출_회차기준상한, 1);
  }
  // 기준과 첨자가 어긋나면 던진다 — 노출은 첨자로 대므로 밀리면 엉뚱한 회차를 센다.
  assert.throws(() => summarize({ axis: 'searchMaxHits', entries: [entry('a')] }, { entries: [] }), /회차 수가 다릅니다/);
  assert.throws(
    () => summarize({ axis: 'searchMaxHits', entries: [entry('a')] }, { entries: [entry('b')] }),
    /회차가 다릅니다/,
  );
  // 서명은 없는 폴더에 대해 던지지 않고, 같은 입력이면 같은 값을 낸다.
  assert.equal(archiveSignature({}), archiveSignature({ CHANNELS_DIR: null, DOCS_DIR: null }));
  console.log('selftest summarize OK');
}

/**
 * 축 지점 하나를 돌린다 — **기준 산출(baseline.json)이 있어야 한다.** 출신 라벨과
 * 표본 칸을 거기서 얼려 오기 때문이다. 없으면 던지고 멈춘다(라벨 없이 돌면 그 지점의
 * 생존률은 구조상 100% 가 되는데 에러는 안 난다).
 */
async function runAxisPoint(point, outDir) {
  const eq = point.indexOf('=');
  if (eq <= 0) throw new Error(`지점 모양이 아닙니다: ${point} — 「축=값」 이어야 합니다.`);
  const axis = point.slice(0, eq);
  const value = Number(point.slice(eq + 1));
  if (!outDir) throw new Error('축 지점은 --out 폴더가 필요합니다 (거기 baseline.json 을 읽습니다).');
  const baseFile = path.join(outDir, 'baseline.json');
  if (!fs.existsSync(baseFile)) {
    throw new Error(`기준 산출이 없습니다: ${baseFile} — 먼저 --point baseline 을 돌리세요.`);
  }
  const base = JSON.parse(fs.readFileSync(baseFile, 'utf-8'));
  /* 얼려 오기 **전에** 기준의 규칙판을 댄다 — 아카이브 서명은 「같은 자료인가」만 보고
   * 「같은 규칙으로 찍힌 값인가」는 안 본다 (사소 2 와 같은 구멍의 재생 쪽). */
  assertEntryStamp(base, '기준');
  const r = await runPoint({
    axis, value, origins: base.origins, frozen: base.entries, baseSignature: base.signature,
  });
  r.summary = summarize(r, base);
  const s = r.summary;
  const pct = (x) => (x === null ? '—' : `${(x * 100).toFixed(1)}%`);
  console.log(`[지점] ${axis} ${r.injected.before} → ${value}`);
  console.log(`[지점] 표본(기준에서 얼림): 잴 수 있는 회차 ${s.잴수있는회차}건 · 기준 일치 ${s.표본회차}건`);
  for (const [k, b] of Object.entries(s.bucket).sort()) {
    console.log(`[지점] ${k.padEnd(11)} 인용 ${String(b.n).padStart(3)}건 · 기준 생존 ${String(b.baseAlive).padStart(3)}건 `
      + `· 노출 ${String(b.exposed).padStart(3)}건 `
      + `→ 지점 생존 ${String(b.survived).padStart(3)}건 (${pct(b.baseAlive ? b.survived / b.baseAlive : null)}) · `
      + `반향뿐 ${b.echoOnly} · 위쪽편향 ${b.afterOnly}(기준 ${b.afterOnlyBase})`);
  }
  const av = s.axisView;
  console.log(`[지점] 축 분모 ${av.buckets.join('+')} — 분모 ${av.분모}건 중 **노출 ${av.노출}건** `
    + `(회차 기준 상한 ${av.노출_회차기준상한}건 · 글자가 달라진 회차 ${s.노출회차}/${r.entries.length})`);
  if (s.poolSig없는인용) {
    console.log(`[지점] ⚠ 근거 묶음 서명이 없어 노출을 못 센 인용 ${s.poolSig없는인용}건 — 노출이 그만큼 적게 나옵니다.`);
  }
  if (av.표기) console.log(`[지점] ⚠ ${av.표기} — 생존률 ${pct(av.분모 ? av.생존 / av.분모 : null)} 를 표에 쓰지 마세요.`);
  console.log(`[지점] 도구 글자(표본) ${s.도구글자_표본.toLocaleString('en-US')}자 / 기준 `
    + `${(s.기준도구글자_표본 ?? 0).toLocaleString('en-US')}자 — 절감 ${pct(s.절감률)}`);
  console.log(`[지점] 지점이 스스로 센 칸(참고): 기준불일치 ${r.counts.기준불일치} · 못잼 ${r.counts.못잼} · `
    + `못쟀다 ${r.counts.못쟀다}`);
  const file = path.join(outDir, `${axis}-${value}.json`);
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(file, JSON.stringify(r, null, 2), 'utf-8');
  console.log(`[지점] ${file}`);
}

if (process.argv.includes('--point')) {
  const argv = process.argv;
  const point = argv[argv.indexOf('--point') + 1];
  const outIdx = argv.indexOf('--out');
  const outDir = outIdx >= 0 ? argv[outIdx + 1] : null;
  if (!point) {
    console.error('--point 뒤에 baseline 또는 「축=값」 이 필요합니다.');
    process.exit(2);
  }
  if (point !== 'baseline') {
    // 아래 기준 요약 블록은 기준 전용이다(기준 불일치 33건 중단 규칙 포함 — 상한을 낮추면
    // 지점의 기준 불일치는 당연히 늘어난다. 그 규칙을 지점에 걸면 격자가 중간에 선다).
    await runAxisPoint(point, outDir);
    process.exit(0);
  }
  const r = await runPoint({});
  const c = r.counts;
  const d = r.diagnostics;
  const origins = new Map();
  let echoOnly = 0;
  let afterOnly = 0;
  for (const e of r.entries) {
    for (const q of e.citations) {
      const k = q.origin ?? '못쟀다';
      origins.set(k, (origins.get(k) || 0) + 1);
      if (q.echoOnly) echoOnly += 1;
      if (q.afterOnly) afterOnly += 1;
    }
  }
  const cited = [...origins.values()].reduce((a, b) => a + b, 0);
  const measurable = r.entries.filter((e) => e.measurable);
  const mEvidence = measurable.reduce((a, e) => a + (e.evidenceCount - e.offSearchEvidence), 0);
  const mMissed = measurable.reduce((a, e) => a + e.missedEvidence, 0);
  console.log(`[기준] 재생 문답 ${r.entries.length}건 · 근거 항목 ${d.근거항목}건 · 출처 인용 ${cited}건`);
  console.log(`[기준] 잴 수 있는 회차 ${measurable.length}건 — 그 근거 ${mEvidence}건 중 미재현 ${mMissed}건 `
    + `(${mEvidence ? (mMissed / mEvidence * 100).toFixed(1) : '0.0'}%)`);
  console.log(`[기준] 기준 불일치 ${c.기준불일치}건 (겹침<0.5) · 못 잼 ${c.못잼}건 `
    + `(실시간조회 ${d.못잼_실시간조회} · 분해보류 ${d.못잼_분해보류}) · 검색밖 전용 ${c.검색밖전용}건`);
  console.log(`[기준] 분해 보류 ${c.분해보류}호출(재생에서 뺌, 로그 글자 ${d.보류한도구글자.toLocaleString('en-US')}자) · `
    + `못 쟀다 ${c.못쟀다}건 · 근거 항목 중 증명된 검색밖 ${c.검색밖}건`);
  console.log(`[기준] 인용 출신: ${[...origins.entries()].map(([k, v]) => `${k} ${v}`).join(' · ')}`);
  console.log(`[기준] 반향뿐인 생존 ${echoOnly}건 · 질문 이후 회차뿐 ${afterOnly}건`);
  console.log(`[기준] 도구 결과 글자 합계 ${r.entries.reduce((a, e) => a + e.toolChars, 0).toLocaleString('en-US')}자`);
  if (d.크기대조.length) {
    const lo = d.크기대조.reduce((a, [l]) => a + l, 0);
    const re = d.크기대조.reduce((a, [, x]) => a + x, 0);
    const same = d.크기대조.filter(([l, x]) => l === x).length;
    console.log(`[대조] 로그가 적은 결과 크기 ${lo.toLocaleString('en-US')}자 → 지금 재생 ${re.toLocaleString('en-US')}자 `
      + `(${d.크기대조.length}회 중 글자 수까지 같은 것 ${same}회) — 아카이브가 자란 만큼 재생은 그때와 다르다.`);
  }
  console.log(`[대조] 근거 항목 중 미재현 ${d.미재현근거}건 · 근거 줄 없는 회차 ${d.근거줄없음}건 · `
    + `출처 줄 없는 회차 ${d.출처줄없음}건`);
  console.log(`[대조] 읽기 호출 인자 풀기 — 못 푼 것 ${d.못푼읽기호출}회(빼지 않음) · `
    + `근거 줄로 추정해 뺀 것 ${d.추정으로뺀읽기호출}회`);
  console.log(`[대조] 분모에서 뺀 ${c.검색밖}건 중 ${d.읽기키인데재생도냄}건은 재생이 검색으로도 냈다 — `
    + '그래서 출신 라벨 「검색밖」 수와 이 칸의 수가 다르다(라벨은 히트/부분/밖을 먼저 본다).');
  /* 아래는 위 일반식의 **퇴화 사례**다 — search 밖의 도구를 한 번도 안 부른 회차는
   * offKeys 가 비어 있어 근거 전부가 분모가 된다. 일반식이 그 회차들을 같은 잣대로
   * 재고 있는지 눈으로 확인하는 자리로 남긴다. */
  const pureSearch = measurable.filter((e) => e.offSearchCalls === 0);
  const pureMissed = pureSearch.reduce((a, e) => a + e.missedEvidence, 0);
  const pureTotal = pureSearch.reduce((a, e) => a + e.evidenceCount, 0);
  console.log(`[대조] 그중 search 만 부른 회차 ${pureSearch.length}건(offKeys=∅ 퇴화 사례) — `
    + `근거 ${pureTotal}건 중 미재현 ${pureMissed}건 `
    + `(${pureTotal ? (pureMissed / pureTotal * 100).toFixed(1) : '0.0'}%)`);
  if (c.기준불일치 > 33) {
    console.log('[기준] 기준 불일치가 절반(33건)을 넘습니다 — 재생 자체가 성립하지 않습니다. 표본 5건:');
    const badIdx = r.entries.map((e, i) => i).filter((i) => r.entries[i].baselineOk === false);
    for (let i = 0; i < Math.min(5, badIdx.length); i += 1) {
      const n = badIdx[Math.floor(i * badIdx.length / 5)];
      const e = r.entries[n];
      const lab = r.origins[n];
      // 재생이 못 낸 근거를 그대로 보인다 — 원인(분해 오류 / autoNarrow 상이 /
      // 아카이브 변화)을 가르려면 「어느 키가 안 나왔나」가 출발점이다.
      const missed = Object.keys(lab).filter((k) => lab[k] === '미재현');
      console.log(`  ${i + 1}. [${e.id}] 겹침 ${(e.overlap * 100).toFixed(0)}% · `
        + `근거 ${e.evidenceCount}건(증명된 검색밖 ${e.offSearchEvidence}) · 미재현 ${e.missedEvidence}건`);
      console.log(`     touched(문서): ${e.touchedDocs.join(' · ') || '(없음)'}`);
      console.log(`     미재현 근거: ${missed.join(' · ') || '(없음)'}`);
    }
    process.exit(3);
  }
  if (outDir) {
    fs.mkdirSync(outDir, { recursive: true });
    const file = path.join(outDir, 'baseline.json');
    fs.writeFileSync(file, JSON.stringify(r, null, 2), 'utf-8');
    console.log(`[기준] ${file}`);
  }
}

/* ─────────────────────────────────────────────────────────────────────────
 * 격자 실행 — **지점마다 프로세스를 새로 띄운다.**
 *
 * 한 프로세스에서 두 지점을 돌리면 두 번째 `import` 가 캐시라 주입이 안 먹는다
 * (`applyCap` 주석). 자식이 죽으면 **거기서 멈춘다** — 반쯤 찬 폴더로 표를 만들면
 * 빠진 지점이 「측정 안 함」이 아니라 「그 값이 없다」로 읽힌다.
 * ───────────────────────────────────────────────────────────────────────── */
if (process.argv.includes('--all')) {
  const argv = process.argv;
  const outIdx = argv.indexOf('--out');
  const outDir = outIdx >= 0 ? argv[outIdx + 1] : null;
  if (!outDir) {
    console.error('--all 은 --out <폴더> 가 필요합니다 (지점마다 JSON 을 그리로 모읍니다).');
    process.exit(2);
  }
  fs.mkdirSync(outDir, { recursive: true });
  const self = fileURLToPath(import.meta.url);
  // 기준을 **먼저 다시 잰다** — 지점들이 얼려 쓸 라벨·표본이 같은 실행에서 나와야 한다.
  const points = [['baseline', null], ...GRID];
  const started = [];
  for (const [axis, value] of points) {
    const label = axis === 'baseline' ? 'baseline' : `${axis}=${value}`;
    console.log(`\n===== ${label} (${started.length + 1}/${points.length}) =====`);
    started.push(label);
    const res = spawnSync(process.execPath, [self, '--point', label, '--out', outDir], { stdio: 'inherit' });
    if (res.status !== 0) {
      console.error(`✗ ${label} 실패 (status ${res.status}) — 격자를 여기서 멈춥니다.`);
      process.exit(1);
    }
  }
  const files = ['baseline.json', ...GRID.map(([a, v]) => `${a}-${v}.json`)];
  const missing = files.filter((f) => !fs.existsSync(path.join(outDir, f)));
  console.log(`\n[격자] 지점 ${points.length}개 · JSON ${files.length - missing.length}/${files.length}개`);
  if (missing.length) {
    console.error(`✗ 없는 산출: ${missing.join(' · ')}`);
    process.exit(1);
  }
  printGridSummary(outDir);
}

/** 축별 한 줄 표를 다시 계산해 쓰고 화면에도 낸다. `--all` 끝과 `--summary` 가 같이 쓴다. */
function printGridSummary(outDir) {
  const { file, out } = writeGridSummary(outDir);
  console.log(`\n[표] 기준 표본 ${out.기준.표본회차}회차(잴 수 있는 회차 ${out.기준.잴수있는회차}) · `
    + `도구 글자 ${out.기준.도구글자_표본.toLocaleString('en-US')}자 · 아카이브 서명 ${out.기준.signature ?? '(없음)'}`);
  for (const r of out.rows) {
    const rate = r.생존률 === null ? '—' : `${(r.생존률 * 100).toFixed(0)}%`;
    /* 소수 **세** 자리로 찍는다 — 두 자리로 찍으면 −0.002% 가 `-0.00%` 가 되고, 그건
     * 보고서에서 방금 고친 그 오독(「손해는 없다」)을 화면에서 다시 만든다. */
    console.log(`[표] ${`${r.axis} ${r.before}→${r.value}`.padEnd(34)} `
      + `노출 ${String(r.노출).padStart(2)}/${String(r.분모).padStart(2)}`
      + `(회차기준 상한 ${String(r.노출_회차기준상한).padStart(2)}) · `
      + `절감 ${(r.절감률 * 100).toFixed(3)}% · `
      + (r.잼 ? `생존 ${r.생존}/${r.분모} (${rate})` : `⚠ ${r.표기}`));
  }
  if (out.서명확인안된지점.length) {
    console.log(`[표] ⚠ 아카이브 서명이 없어 「기준과 같은 자료에서 나왔나」를 확인 못 한 지점 `
      + `${out.서명확인안된지점.length}개: ${out.서명확인안된지점.join(' · ')}`);
  }
  console.log(`[표] ${file}`);
}

if (process.argv.includes('--summary')) {
  const argv = process.argv;
  const outIdx = argv.indexOf('--out');
  const outDir = outIdx >= 0 ? argv[outIdx + 1] : null;
  if (!outDir) {
    console.error('--summary 는 --out <폴더> 가 필요합니다.');
    process.exit(2);
  }
  // 재생은 다시 안 돈다 — 이미 있는 산출 12개의 `summary` 만 지금 규칙으로 다시 계산한다.
  printGridSummary(outDir);
}

/* ─────────────────────────────────────────────────────────────────────────
 * `--report` — 산출 JSON 들을 읽어 **md 표만 stdout 으로** 낸다 (보고서에 붙이는 판).
 *
 * 판정은 한 벌이다: 표의 모든 값은 `writeGridSummary`(= `--summary` 가 쓰는 그 함수)가
 * 낸 것을 그대로 옮기고, 여기서 새로 계산하는 것은 **서식뿐**이다.
 *
 * **이 모드가 막아 주는 것과 안 막아 주는 것을 갈라 둔다** (검수 2026-09-13):
 *  - 막는다: 산출 13개에 **찍힌** 아카이브 서명이 서로 다른 것 · 재생 규칙판 지문이 지금
 *    코드와 다른 것 → `writeGridSummary` 가 던진다.
 *  - **안 막는다**: 그 서명이 **지금 디스크의 아카이브와 같은지**는 안 본다. 저장된 JSON 만
 *    읽으므로 아카이브가 자라도 EXIT 0 으로 같은 표가 나온다. 그건 고장이 아니라 이 모드의
 *    일이다(과거 시점의 사진을 다시 인쇄하는 것). 대보고 싶으면 `--check-archive` 를 붙인다.
 *  - `--all` 은 기준을 **먼저 다시 재므로** 아카이브가 자랐으면 새 서명이 찍히고, 아무것도
 *    안 던진 채 **다른 숫자의 표**가 나온다. 「낡으면 멈춘다」고 읽으면 안 된다.
 *
 * 옵션은 **기본이 끔**이다 — 표만 다시 찍는 일에 `.env`·아카이브 15MB 를 읽게 하지 않는다.
 * ───────────────────────────────────────────────────────────────────────── */

/** 생존률 — `잼:false` 면 숫자를 **만들지 않는다**. 정수 반올림만 한다(소수는 못 믿는다). */
function fmtSurvival(row) {
  if (row.생존률 === null || !row.잼) return null;
  return `${Math.round(row.생존률 * 100)}%`;
}

/** 절감률 — 0 에 가깝거나 음수인 줄은 **소수·부호를 그대로** 둔다.
 *  반올림해 `0%` 로 적으면 「손해는 없다」로 읽히는데, 실제로는 글자가 **늘어난** 줄이 있다. */
function fmtSaving(x) {
  if (x === null || x === undefined) return '—';
  const v = x * 100;
  return Math.abs(v) < 0.5 ? `${v.toFixed(3)}%` : `${Math.round(v)}%`;
}

/**
 * `--check-archive` 전용 — 산출에 찍힌 서명을 **지금 디스크의 아카이브**와 댄다.
 * 막지 않고 **알려만 준다**: 여기서 던지면 「과거 시점의 표를 다시 찍는 일」자체가
 * 아카이브가 자란 다음 날부터 불가능해진다(그건 20분짜리 격자 재실행을 강요하는 것이다).
 * 확인 못 했으면 「같다」고 쓰지 않고 **「확인 못 했다」로 쓴다.**
 */
async function liveArchiveLine(baseSignature) {
  let now = null;
  try {
    /* `src/config.js` 는 아카이브 경로(CHANNELS_DIR·DOCS_DIR)를 아는 유일한 자리다.
     * **아래 catch 가 못 잡는 경우가 있다**: 설정이 아예 없거나 경로가 틀리면 그 모듈이
     * 스스로 안내를 찍고 `process.exit(1)` 한다 — 예외가 아니라 프로세스 종료라
     * 표가 통째로 안 나온다(실측: 출력 0바이트 · 종료 코드 1). 그래서 이 옵션은
     * **설정이 갖춰진 기계 전용**이고, 기본 `--report` 는 설정을 아예 안 본다. */
    const cfg = await import('../src/config.js');
    now = archiveSignature(cfg);
  } catch (e) {
    const why = String(e && e.message ? e.message : e).split('\n')[0].slice(0, 120);
    return `지금 아카이브와 같은지 **확인 못 했습니다** — ${why}`;
  }
  if (!baseSignature) return '이 산출에는 아카이브 서명이 없어 **어느 자료로 잰 것인지 확인 못 합니다.**';
  if (now === baseSignature) return `이 표를 잰 때의 아카이브와 지금 아카이브가 **같습니다** (서명 \`${now}\`).`;
  return `⚠ 지금 아카이브는 이 표를 잰 때와 **다릅니다** (잰 때 \`${baseSignature}\` vs 지금 \`${now}\`) — `
    + '이 표는 그때의 사진입니다. 지금 자료로 된 값을 알려면 격자를 다시 돌려야 합니다.';
}

export async function buildReportMarkdown(outDir, { checkArchive = false } = {}) {
  const { out } = writeGridSummary(outDir);
  const base = JSON.parse(fs.readFileSync(path.join(outDir, 'baseline.json'), 'utf-8'));

  // 인용 출신 — 전체와 표본(기준 일치 회차)을 따로 센다. 0 으로 접는 칸이 없어야 한다.
  const originAll = new Map();
  const originSample = new Map();
  for (const e of base.entries) {
    for (const c of e.citations) {
      const k = c.origin ?? '못쟀다';
      originAll.set(k, (originAll.get(k) || 0) + 1);
      if (e.baselineOk === true) originSample.set(k, (originSample.get(k) || 0) + 1);
    }
  }
  const nAll = [...originAll.values()].reduce((a, b) => a + b, 0);
  const nSample = [...originSample.values()].reduce((a, b) => a + b, 0);

  const L = [];
  L.push('| 축 (기준→값) | 분모 | 노출 | 생존 | 생존률 | 절감률(표본) | 절감률(전체 66) | 반향뿐 생존 | 질문이후 회차뿐 |');
  L.push('|---|---|---|---|---|---|---|---|---|');
  for (const row of out.rows) {
    const p = JSON.parse(fs.readFileSync(path.join(outDir, `${row.axis}-${row.value}.json`), 'utf-8'));
    const buckets = p.summary.axisView.buckets || [];
    let echoOnly = 0;
    let afterOnly = 0;
    for (const b of buckets) {
      const cell = p.summary.bucket[b];
      if (!cell) continue;
      echoOnly += cell.echoOnly;
      afterOnly += cell.afterOnly;
    }
    const rate = fmtSurvival(row);
    /* 「안 닿음」의 이유는 두 가지뿐이고, 둘의 뜻이 다르다 — 하나는 이 표본에서 상한이
     * 거의 안 찼다는 것이고(자료를 더 모으면 잴 수 있다), 하나는 그 축이 원리상 건수를
     * 안 바꾼다는 것이다(더 모아도 안 잰다). 긴 `표기` 문구를 줄여 쓰되 뜻은 그대로 둔다. */
    const why = PER_GROUP_AXES.has(row.axis)
      ? '구조상 안 물림 — 돌아오는 건수는 그대로고 구성만 바뀜'
      : '상한이 실물에서 거의 안 참';
    /* **생존 분수도 안 찍는다.** 퍼센트만 지우면 `28/30` 이 남아 암산으로 93% 가 복원되고,
     * 그 93% 야말로 이 측정이 라운드 2 에서 「미측정을 안전으로 읽던 것」이라고 뒤집은 값이다.
     * 화면 출력(`printGridSummary`)도 「못 잼」이면 생존 분수를 안 찍는다 — 같은 관례를 쓴다.
     * 원시 건수는 산출 JSON 의 `axisView.생존` 에 그대로 있다(지우는 것이 아니라 안 내는 것). */
    L.push(`| \`${row.axis}\` ${row.before}→${row.value} | ${row.분모} | ${row.노출} | `
      + `${rate ? `${row.생존}/${row.분모}` : '—'} | ${rate ?? `**안 닿음** (${why})`} | `
      + `${fmtSaving(row.절감률)} | ${fmtSaving(row.절감률_전체)} | ${echoOnly} | ${afterOnly} |`);
  }
  L.push('');
  L.push('**안 닿음** = 분모 인용 중 그 상한이 **실제로 건드린 것**(노출)이 10건 미만인 줄입니다. '
    + '생존률이 「안 죽었다」가 아니라 **미측정**이라, 생존 칸도 비웠습니다 — 분수가 남아 있으면 '
    + '암산으로 퍼센트가 복원되기 때문입니다. 원시 건수는 산출 JSON 에 그대로 있습니다.');
  L.push('');
  L.push('**절감률 두 줄을 더하면 안 됩니다** — 축을 하나씩만 움직여 잰 값이라, 둘을 함께 '
    + '내렸을 때의 값은 이 측정에 없습니다(더 클 수도 작을 수도 있습니다).');
  L.push('');
  L.push('**11줄의 분모를 합산하지 마세요** — 한 인용이 여러 축의 분모에 듭니다(축마다 '
    + '「이 상한이 이 인용을 자를 수 있나」를 따로 묻습니다).');
  L.push('');
  L.push(`표본: 문답 ${out.기준.회차}회차 중 잴 수 있는 것 ${out.기준.잴수있는회차} · `
    + `기준 재생이 맞은 것 ${out.기준.표본회차}(절감률 분모 = 그 ${out.기준.표본회차}회차의 `
    + `도구 결과 ${out.기준.도구글자_표본.toLocaleString('en-US')}자) · `
    + `아카이브 서명 ${out.기준.signature ?? '(없음)'} · 규칙판 ${base.entryRules ?? '(없음)'}`);
  L.push('');
  L.push('### 곁표 — 분모에 안 든 것 (0 으로 접지 않습니다)');
  L.push('');
  L.push('| 무엇 | 수 |');
  L.push('|---|---|');
  L.push(`| 기준 불일치 (지금 재생이 그때 근거를 절반도 못 냄 — 상한 탓이 아님) | ${base.counts.기준불일치}회차 |`);
  L.push(`| **겹침 못 잼** (그때 근거와 겹치는지를 아예 못 재는 회차) | ${base.counts.못잼}회차 `
    + `(실시간 조회 ${base.diagnostics.못잼_실시간조회} · 검색 조건 분해 보류 ${base.diagnostics.못잼_분해보류}) |`);
  L.push(`| 분해 보류 (기록에서 검색 조건을 못 가른 호출) | ${base.counts.분해보류}호출 |`);
  L.push(`| **자료를 못 맞춤** (답의 출처 제목 ↔ 아카이브 문서 이름) | ${originAll.get('못쟀다') || 0}인용 |`);
  L.push(`| 미재현 (그 근거를 재생이 아예 못 냄) | ${originAll.get('미재현') || 0}인용 |`);
  L.push(`| 검색밖 출신 (읽기·실시간 조회로 들어온 인용 — 검색 상한이 원리상 안 자름) | ${originAll.get('검색밖') || 0}인용 |`);
  L.push('');
  L.push(`인용 출신 — 전체 ${nAll}건: `
    + `${[...originAll.entries()].map(([k, v]) => `${k} ${v}`).join(' · ')}`);
  L.push(`인용 출신 — 표본 ${nSample}건: `
    + `${[...originSample.entries()].map(([k, v]) => `${k} ${v}`).join(' · ')}`);
  /* 이름이 비슷한 둘(`outside`·`검색밖`)을 여기서 갈라 준다 — 바로 옆 칸에 나란히 서
   * 있어서 풀이가 없으면 같은 것으로 읽힌다. 하나는 검색 결과이고 하나는 검색이 아니다. */
  L.push('(산출 데이터의 `못쟀다` 가 위 표의 「자료를 못 맞춤」이고, `미재현` 이 「그 근거를 '
    + '재생이 아예 못 냄」입니다. `outside` 는 **좁힌 범위 안에 낱말을 다 맞춘 것이 없을 때 '
    + '밖에서 함께 준 검색 결과**이고, `검색밖` 은 **검색이 아닌 도구**(문서·채널 직접 읽기, '
    + '실시간 조회)로 들어온 것입니다 — 이름만 비슷하고 다른 것입니다.)');
  if (out.서명확인안된지점.length) {
    L.push('');
    L.push(`⚠ 아카이브 서명이 없어 「기준과 같은 자료에서 나왔나」를 확인 못 한 지점: `
      + `${out.서명확인안된지점.join(' · ')}`);
  }
  L.push('');
  L.push(checkArchive
    ? await liveArchiveLine(out.기준.signature)
    : '이 표는 **저장된 산출만 읽어** 만듭니다 — 지금 아카이브가 이 표를 잰 때와 같은지는 '
      + '알려 주지 않습니다. 대보려면 `--check-archive` 를 붙이세요.');
  return `${L.join('\n')}\n`;
}

if (process.argv.includes('--report')) {
  const argv = process.argv;
  const outIdx = argv.indexOf('--out');
  const outDir = outIdx >= 0 ? argv[outIdx + 1] : null;
  if (!outDir) {
    console.error('--report 는 --out <폴더> 가 필요합니다.');
    process.exit(2);
  }
  process.stdout.write(await buildReportMarkdown(outDir, {
    checkArchive: argv.includes('--check-archive'),
  }));
}
