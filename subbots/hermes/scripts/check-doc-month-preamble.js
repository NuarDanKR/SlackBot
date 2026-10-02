#!/usr/bin/env node
/**
 * `read_document` 에 `month` 를 지정해도 문서 맨 위 머리말(사람이 쓴 정리)이 오나 —
 * fixture 로 돈다 (`check-excel-sheets.js`·`check-doc-outline.js` 와 같은 규칙, 실제
 * 아카이브에 그 문서가 없어도 돌아야 한다).
 *
 *   node scripts/check-doc-month-preamble.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 *
 * ── 왜 있나 ──
 *
 * 커밋 7446962 가 「전문 대신 목차만 오는 큰 문서」에는 목차 앞에 머리말을 붙였다
 * (`outlineWithPreamble`). 그런데 그 커밋의 「대조군」 절이 이미 적어 뒀듯, **`month` 를
 * 지정한 호출은 범위 밖으로 남겨졌다** — 머리말은 첫 월 헤딩보다 위라 월로 자르면
 * `outlineOf` 갈래를 타지도 않고 그냥 사라진다. 실측(2026-09-03): 머리말 있는 문서 12개
 * 중 **10개가 월이 둘 이상**이라 봇이 그 문서를 읽을 때 `month` 를 지정해 부르는 경우가
 * 오히려 흔하다. WHK 결정 2026-09-03 으로 이 축도 고쳤다 — 같은 함수
 * (`outlineWithPreamble`) 를 그대로 재사용한다.
 *
 * 진짜 사업장·문서 이름을 여기 적지 않는다(`check-business-names.js`) — 임시 폴더에
 * 합성 문서를 만들어 `HERMES_DOCS_DIR` 로 가리킨 별도 프로세스에서 돈다.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PREAMBLE_NEEDLE = '시험머리말표식-금액12345';

const docWrap = (pre, body) => [
  '# [업무보고] 시험 문서', '',
  '> **사업장**: 시험 · **종류**: 업무보고',
  '> **열람**: 공개', '',
  '---', '',
  ...(pre ? [pre, '', '---', ''] : []),
  body,
].join('\n');

const preamble = ['## 회차 요약', '', `이 문서를 훑은 사람 정리다 — ${PREAMBLE_NEEDLE}.`].join('\n');

const twoMonths = ['## 2026-07', '', '**2026-07-05 · 보고.hwp**', '', '칠월본문내용', '',
  '## 2026-08', '', '**2026-08-05 · 보고.hwp**', '', '팔월본문내용', ''].join('\n');

const FIXTURE_WITH_PREAMBLE = docWrap(preamble, twoMonths);
const FIXTURE_NO_PREAMBLE = docWrap(null, twoMonths);

function withFixture(fixtureText, run) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-doc-month-pre-'));
  const proj = path.join(tmp, 'projects', '시험');
  fs.mkdirSync(proj, { recursive: true });
  fs.writeFileSync(path.join(proj, '20260805-시험-업무보고.md'), fixtureText, 'utf8');
  fs.writeFileSync(path.join(tmp, 'index.md'), '# 문서 아카이브\n', 'utf8');
  try {
    return run(tmp, proj);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

function readViaSubprocess(tmp, calls) {
  // Windows: 절대경로를 그대로 import specifier 로 주면 ERR_UNSUPPORTED_ESM_URL_SCHEME 로
  // 죽는다("C:/..." 가 file:// URL 이 아니라서다) — pathToFileURL 로 감싼다.
  const probe = `
    process.env.HERMES_DOCS_DIR = ${JSON.stringify(tmp)};
    const { readDocument } = await import(${JSON.stringify(
      pathToFileURL(path.join(HERE, '..', 'src', 'documents.js')).href,
    )});
    const { FULL_ACCESS } = await import(${JSON.stringify(
      pathToFileURL(path.join(HERE, '..', 'src', 'config.js')).href,
    )});
    const out = {};
    for (const [key, args] of Object.entries(${JSON.stringify(calls)})) {
      out[key] = readDocument({ ...args, access: FULL_ACCESS });
    }
    console.log(JSON.stringify(out));
  `;
  const r = spawnSync(process.execPath, ['--input-type=module', '-e', probe], { encoding: 'utf-8' });
  if (r.status !== 0) {
    throw new Error(`probe 가 실패했습니다: ${(r.stderr || '').trim().split('\n')[0]}`);
  }
  return JSON.parse(r.stdout.trim().split('\n').pop());
}

console.log('[1/3] month 를 지정해도 머리말이 온다 (이번에 고친 축)');
withFixture(FIXTURE_WITH_PREAMBLE, (tmp) => {
  let out;
  try {
    out = readViaSubprocess(tmp, {
      july: { project: '시험', document: '업무보고', month: '2026-07' },
      august: { project: '시험', document: '업무보고', month: '2026-08' },
    });
  } catch (e) {
    bad(e.message);
    return;
  }
  if (out.july.error) bad(`month:"2026-07" → ${out.july.error}`);
  else if (out.july.text.includes(PREAMBLE_NEEDLE)) ok('7월 호출에 머리말이 온다');
  else bad(`7월 호출에 머리말이 없습니다: ${out.july.text.slice(0, 200)}`);

  if (out.august.error) bad(`month:"2026-08" → ${out.august.error}`);
  else if (out.august.text.includes(PREAMBLE_NEEDLE)) ok('8월 호출에도 머리말이 온다 (매번 붙는다)');
  else bad(`8월 호출에 머리말이 없습니다: ${out.august.text.slice(0, 200)}`);

  // 월로 좁힌 본문은 그대로 좁혀져야 한다 — 머리말을 붙이다가 다른 달 내용까지
  // 새 들어오면 안 된다.
  if (out.july.text && out.july.text.includes('칠월본문내용') && !out.july.text.includes('팔월본문내용')) {
    ok('머리말이 붙어도 월 범위는 그대로 좁혀져 있다');
  } else {
    bad(`월 범위가 흐트러졌습니다: ${(out.july.text || '').slice(0, 300)}`);
  }
});

console.log('[2/3] 머리말이 없는 문서는 month 를 지정해도 아무것도 안 붙는다 (반대 방향)');
withFixture(FIXTURE_NO_PREAMBLE, (tmp) => {
  let out;
  try {
    out = readViaSubprocess(tmp, { july: { project: '시험', document: '업무보고', month: '2026-07' } });
  } catch (e) {
    bad(e.message);
    return;
  }
  if (out.july.error) bad(`month:"2026-07" → ${out.july.error}`);
  else if (!out.july.text.includes('문서 md 상단 정리') && out.july.text.trim().startsWith('##')) {
    ok('머리말 표식이 안 붙고 월 섹션 그대로 시작한다');
  } else {
    bad(`머리말이 없는데도 뭔가 붙었습니다: ${out.july.text.slice(0, 200)}`);
  }
});

console.log('[3/3] month 없이 부르면 (기존 동작) 전문에 머리말이 자연히 포함된다 — 안 흔들렸다');
withFixture(FIXTURE_WITH_PREAMBLE, (tmp) => {
  let out;
  try {
    out = readViaSubprocess(tmp, { full: { project: '시험', document: '업무보고' } });
  } catch (e) {
    bad(e.message);
    return;
  }
  if (out.full.error) bad(`전문 호출 → ${out.full.error}`);
  else if (out.full.text.includes(PREAMBLE_NEEDLE)) ok('전문 호출에는 원래도 머리말이 포함된다');
  else bad(`전문 호출에 머리말이 없습니다: ${out.full.text.slice(0, 200)}`);
});

console.log(failed ? `\n[check-doc-month-preamble] FAIL — ${failed}건` : '\n[check-doc-month-preamble] OK — month 지정 호출에도 머리말이 실립니다.');
process.exit(failed ? 1 : 0);
