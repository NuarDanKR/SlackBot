#!/usr/bin/env node
/**
 * 시간대 기준이 JS 와 파이썬 넷에서 같은 값(config.json 의 `timezone`)을 보나.
 *
 *   node scripts/check-timezone.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 (2026-09-02 전수조사) ──
 *
 * JS 13곳은 `config.js` 의 `config.timezone`(config.json 에서 읽는다)을 쓴다. 파이썬 넷
 * (`board.py`·`fetch_slack_files.py`·`sync_index.py`·`decide_work.py`)은 각자 "Asia/Seoul"
 * 을 따로 하드코딩해 두고 있었고, `decide_work.py:107` 은 그마저도 없이 **기계 로컬
 * 시간대**(`datetime.now().astimezone()`)로 쟀다. 한국 안에서는 지금 다 같은 값이 나와서
 * **우연히 안 갈렸다** — config.json 의 timezone 이 바뀌는 날, 또는 기계 로컬이 KST 가
 * 아닌 날(예: VM 을 UTC 로 세팅) 조용히 갈린다.
 *
 * 2026-09-03 에 파이썬 넷을 `_shared/tz.py` 하나로 모았다(config.json 의 timezone 을
 * 읽고, 못 읽는 기계에서만 Asia/Seoul 전용 고정 폴백). 이 검사는 그 통합이 유지되는지
 * ① 소스 모양(하드코딩이 다시 안 들어왔나) ② 실제 동작(넷이 재는 오프셋이 같나)
 * ③ JS 와 값이 같나, 세 가지로 잰다.
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { config } from '../src/config.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const SKILLS = path.join(ROOT, '.claude', 'skills');

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

const PY_NAMES = process.env.PYTHON ? [process.env.PYTHON] : ['python', 'python3'];
function runPy(snippet, input) {
  let lastErr = '';
  for (const name of PY_NAMES) {
    try {
      return { out: execFileSync(name, ['-c', snippet], { input, encoding: 'utf-8' }) };
    } catch (err) {
      lastErr = `${name}: ${err.message.split('\n')[0]}`;
    }
  }
  return { err: lastErr };
}

/* ── ① 소스 모양 — 네 스크립트가 시간대를 다시 하드코딩하지 않았나 ──────────
 * 걷어내는 방식은 check-shared-rules.js 의 codeOf 와 같다 (파이썬 `#` 주석·JS 주석 제거,
 * 줄 맨 앞만). 실물 위반이 있는지와 함께, **이 검사에 이빨이 있는지**도 합성 입력으로 잰다. */
// 파이썬 docstring(삼중따옴표)도 걷어낸다 — 이 파일 스스로가 `.now().astimezone()` ·
// `ZoneInfo(` 같은 낱말을 **설명하는 docstring**에 그대로 쓰는데, `#` 만 걷어내면(check-
// shared-rules.js 의 codeOf 와 같은 한계) 그 설명 자체가 「하드코딩」으로 오탐된다.
const codeOf = (text) => text
  .replace(/("""[\s\S]*?"""|'''[\s\S]*?''')/g, '')
  .split('\n').filter((l) => !/^\s*(\/\/|\*|\/\*|#)/.test(l)).join('\n');
const HARDCODE_RE = /ZoneInfo\(|timezone\(timedelta|timedelta\(hours=9\)/;

const OWNED_PY = [
  '.claude/skills/archive-run/scripts/board.py',
  '.claude/skills/doc-archive/scripts/fetch_slack_files.py',
  '.claude/skills/slack-sync/scripts/sync_index.py',
  '.claude/skills/archive-inbox/scripts/decide_work.py',
];

console.log('[1/3] 소스 모양 — 시간대 만들기가 네 스크립트에 다시 하드코딩되지 않았나');
{
  // 이빨 확인 — 하드코딩된 가짜 소스를 먹여서 잡히는지 먼저 본다.
  const bogusCases = [
    ['ZoneInfo 직접 생성', 'return datetime.now(ZoneInfo("Asia/Seoul"))', true],
    ['고정 오프셋 직접 생성', 'return datetime.now(timezone(timedelta(hours=9)))', true],
    ['주석 안 (오탐이면 안 된다)', '# ZoneInfo("Asia/Seoul") 은 이제 tz.py 가 한다', false],
    ['_tz 로 위임 (정상)', 'return _tz.now()', false],
  ];
  let bad = false;
  for (const [label, src, want] of bogusCases) {
    const got = HARDCODE_RE.test(codeOf(src));
    if (got !== want) { fail(`이 검사가 스스로를 못 지킵니다 — ${label}`); bad = true; }
  }
  if (!bad) pass('합성 입력 네 가지에서 정확히 갈린다 (이 검사에 이빨이 있다)');

  let realBad = false;
  for (const rel of OWNED_PY) {
    const p = path.join(ROOT, rel);
    let text;
    try { text = fs.readFileSync(p, 'utf-8'); } catch { fail(`${rel} 을 읽지 못했습니다`); realBad = true; continue; }
    if (HARDCODE_RE.test(codeOf(text))) {
      fail(`${rel} 이 시간대를 다시 하드코딩하고 있습니다 — _shared/tz.py 를 쓰세요`);
      realBad = true;
    }
  }
  if (!realBad) pass('네 스크립트 다 _shared/tz.py 로 위임한다 (직접 하드코딩 없음)');
}

/* ── ②-b decide_work.py 가 기계 로컬로 돌아가지 않았나 ─────────────────────
 * `datetime.now().astimezone()` (인자 없는 astimezone) 은 시스템 시간대를 쓴다.
 * 이것만은 이름 규칙(ZoneInfo/timedelta)에 안 걸려서 따로 본다. */
console.log('\n[2/3] decide_work.py 가 기계 로컬 시간대로 돌아가지 않나');
{
  const NAIVE_RE = /\.now\(\)\.astimezone\(\)/;
  for (const [label, src, want] of [
    ['기계 로컬 (전에 실제로 이랬다)', 'datetime.now().astimezone().isoformat()', true],
    ['tz 인자를 준 것 (정상)', 'datetime.now(KST).isoformat()', false],
    ['_tz 위임 (정상)', '_tz.now().isoformat(timespec="seconds")', false],
    ['docstring 안 (오탐이면 안 된다)', '"""전에는 datetime.now().astimezone() 을 썼다"""', false],
  ]) {
    if (NAIVE_RE.test(codeOf(src)) !== want) fail(`이 검사가 스스로를 못 지킵니다 — ${label}`);
  }
  const p = path.join(ROOT, '.claude/skills/archive-inbox/scripts/decide_work.py');
  const text = fs.readFileSync(p, 'utf-8');
  if (NAIVE_RE.test(codeOf(text))) {
    fail('decide_work.py 가 아직 기계 로컬 시간대(.now().astimezone())로 잽니다');
  } else {
    pass('decide_work.py 가 기계 로컬 시간대를 안 씁니다');
  }
}

/* ── ③ 실제 동작 — 넷이 같은 오프셋을 내나, 그리고 JS 의 config.timezone 과 같나 ──── */
console.log('\n[3/3] 실제 동작 — 파이썬 넷의 tzinfo 오프셋이 서로 같고 JS 의 config.timezone 과 맞나');
{
  const snippet = [
    'import json, sys',
    `sys.path.insert(0, ${JSON.stringify(path.join(SKILLS, '_shared'))})`,
    `sys.path.insert(0, ${JSON.stringify(path.join(SKILLS, 'archive-run', 'scripts'))})`,
    `sys.path.insert(0, ${JSON.stringify(path.join(SKILLS, 'doc-archive', 'scripts'))})`,
    `sys.path.insert(0, ${JSON.stringify(path.join(SKILLS, 'slack-sync', 'scripts'))})`,
    `sys.path.insert(0, ${JSON.stringify(path.join(SKILLS, 'archive-inbox', 'scripts'))})`,
    'import tz',
    'import board',
    'import fetch_slack_files as fsf',
    'import sync_index as si',
    'from datetime import datetime',
    'out = {',
    '  "tz_name": tz.TZ_NAME,',
    '  "board_offset": board.now_kst().utcoffset().total_seconds(),',
    '  "fetch_offset": datetime.now(tz.TZINFO).utcoffset().total_seconds(),',  // today_kst()는 date라 오프셋이 없음 — 같은 tzinfo로 잰다
    '  "sync_offset": si.KST.utcoffset(datetime.now()).total_seconds(),',
    '}',
    'sys.stdout.write(json.dumps(out))',
  ].join('\n');
  const r = runPy(snippet);
  if (r.err) {
    fail(`파이썬 쪽을 부르지 못했습니다 — ${r.err}`);
  } else {
    let data;
    try { data = JSON.parse(r.out); } catch (e) { fail(`파이썬 출력을 못 읽었습니다: ${e.message} — ${r.out}`); data = null; }
    if (data) {
      if (data.tz_name !== config.timezone) {
        fail(`파이썬이 읽은 시간대(${data.tz_name})가 JS 의 config.timezone(${config.timezone})과 다릅니다`);
      } else {
        pass(`파이썬·JS 가 같은 시간대(config.json 의 timezone=${config.timezone})를 본다`);
      }
      const offsets = [data.board_offset, data.fetch_offset, data.sync_offset];
      if (new Set(offsets).size !== 1) {
        fail(`board·fetch_slack_files·sync_index 가 서로 다른 오프셋을 냅니다 — ${JSON.stringify(offsets)}`);
      } else {
        pass(`board·fetch_slack_files·sync_index 가 같은 오프셋(${offsets[0]}초)을 낸다`);
      }
      if (config.timezone === 'Asia/Seoul' && offsets[0] !== 9 * 3600) {
        fail(`config.timezone 이 Asia/Seoul 인데 오프셋이 +09:00 이 아닙니다 (${offsets[0]}초)`);
      }
    }
  }
}

if (ok) {
  console.log('\n[check-timezone] OK — JS·파이썬 넷이 config.json 의 timezone 하나로 같이 움직입니다.');
} else {
  console.error('\n두 갈래가 다시 갈리면 config.json 을 바꾸는 날(또는 기계 로컬이 KST 가 아닌 날)에만 드러납니다.');
  process.exitCode = 1;
}
