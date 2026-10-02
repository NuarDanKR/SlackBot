#!/usr/bin/env node
/**
 * `config.limits.liveFetchMaxDays` 의 **기본값**이 한 곳(`slack-live.js` 의
 * `DEFAULT_LIVE_FETCH_MAX_DAYS`)에만 있고, 그 값을 쓰는 세 자리(`archive-health.js`·
 * `slack-live.js` 의 `recentWindow`·`llm/tools.js` 의 도구 설명)가 전부 그 상수로 도나.
 *
 *   node scripts/check-live-fetch-max-days.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 (2026-09-02 전수조사) ──
 *
 * 전에는 `archive-health.js` 만 `config.limits?.liveFetchMaxDays ?? 14` 로 기본값을 두고
 * `slack-live.js`(`recentWindow`)·`llm/tools.js`(도구 설명 문구)는 기본값 없이
 * `config.limits.liveFetchMaxDays` 를 그대로 읽었다. config.json 에 그 키가 없으면
 * `recentWindow` 는 `Math.min(…, undefined)` = `NaN` 이 되고, `llm/tools.js` 의 도구 설명은
 * "1~undefined" 로 모델에게 나갔다 — **지금 config.json 에 값(14)이 있어 우연히 안
 * 갈렸을 뿐**이라 실물 대조로는 안 잡힌다. 그래서 여기서는 그 키를 **일부러 지운
 * config** 을 만들어 세 자리가 정말 같은 값(기본값)으로 떨어지는지 합성 입력으로 본다.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

console.log('[1/2] DEFAULT_LIVE_FETCH_MAX_DAYS 를 세 자리가 실제로 쓰나 (소스 모양)');
{
  const files = {
    'src/slack-live.js': [/export const DEFAULT_LIVE_FETCH_MAX_DAYS\s*=\s*(\d+)/, /createSlackLiveWindow\(\{[\s\S]*?DEFAULT_LIVE_FETCH_MAX_DAYS/],
    'src/slack/live-window.js': [/DEFAULT_LIVE_FETCH_MAX_DAYS/, /liveFetchMaxDays\s*\?\?\s*DEFAULT_LIVE_FETCH_MAX_DAYS/],
    'src/archive-health.js': [/DEFAULT_LIVE_FETCH_MAX_DAYS/, /liveFetchMaxDays\s*\?\?\s*DEFAULT_LIVE_FETCH_MAX_DAYS/],
    'src/llm/tools.js': [/DEFAULT_LIVE_FETCH_MAX_DAYS/, /liveFetchMaxDays\s*\?\?\s*DEFAULT_LIVE_FETCH_MAX_DAYS/],
  };
  let bad = false;
  const values = new Set();
  for (const [rel, [defRe, useRe]] of Object.entries(files)) {
    const text = fs.readFileSync(path.join(ROOT, rel), 'utf-8');
    const defM = text.match(defRe);
    if (rel === 'src/slack-live.js') {
      if (!defM) { fail(`${rel} 에 DEFAULT_LIVE_FETCH_MAX_DAYS 정의가 없습니다`); bad = true; }
      else values.add(defM[1]);
    }
    if (!useRe.test(text)) {
      fail(`${rel} 이 DEFAULT_LIVE_FETCH_MAX_DAYS 로 안 떨어집니다 (\`?? 14\` 처럼 다시 리터럴을 박았을 수 있습니다)`);
      bad = true;
    }
  }
  if (!bad) pass(`정본 값(${[...values][0]})을 세 자리가 전부 참조한다`);
}

console.log('\n[2/2] 실제 동작 — liveFetchMaxDays 가 없는 config 에서도 세 자리가 같은 기본값으로 떨어지나');
{
  // config.js 는 process.env.HERMES_DATA_ROOT 의 config.json 을 읽어 모듈 로드 시점에
  // **한 번만** 굳는다 — 여기서 값을 지운 가짜 config 를 만들어 그 자리로 새 프로세스를
  // 하나 띄운다 (임포트 캐시를 못 속이므로 이 방법뿐이다).
  // **이 가짜 아카이브에는 개명 지도(`slack-export/.sync-state.json`)가 없다.** 지금은
  // 아카이브 자료를 안 읽어서 괜찮지만, 이 검사가 나중에 자료를 읽게 되면 `src/config.js`
  // 가 지도를 「죽음」으로 보고 **전체 권한이 아닌 접근을 전부 닫는다**(fail-closed).
  // 그러면 검사는 초록인데 그 초록은 **닫힘이 만든 것**이라 아무것도 안 잰 것이 된다.
  // 자료를 읽게 고칠 때는 지도도 함께 쓴다 — `check-doc-card-kill-switch.js` 가 원본이다.
  const tmp = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'hermes-livefetch-'));
  try {
    const dataRoot = tmp;
    const realConfig = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf-8'));
    delete realConfig.limits.liveFetchMaxDays;   // 일부러 뺀다 — 이것이 이 검사의 핵심
    fs.writeFileSync(path.join(dataRoot, 'config.json'), JSON.stringify(realConfig));
    fs.mkdirSync(path.join(dataRoot, 'slack-export', 'channels'), { recursive: true });
    fs.mkdirSync(path.join(dataRoot, 'documents', 'projects'), { recursive: true });

    const probe = [
      `import { recentWindow, DEFAULT_LIVE_FETCH_MAX_DAYS } from ${JSON.stringify(pathToFileURL(path.join(ROOT, 'src/slack-live.js')).href)};`,
      `import { config } from ${JSON.stringify(pathToFileURL(path.join(ROOT, 'src/config.js')).href)};`,
      'const r = recentWindow(999);',
      'console.log(JSON.stringify({ days: r.days, def: DEFAULT_LIVE_FETCH_MAX_DAYS, hasKey: config.limits.liveFetchMaxDays !== undefined }));',
    ].join('\n');
    const probeFile = path.join(tmp, 'probe.mjs');
    fs.writeFileSync(probeFile, probe);

    const r = spawnSync(process.execPath, [probeFile], {
      encoding: 'utf-8',
      env: { ...process.env, HERMES_DATA_ROOT: dataRoot },
    });
    if (r.status !== 0) {
      fail(`liveFetchMaxDays 없는 config 에서 슬랙 조회 쪽이 죽습니다 — ${(r.stderr || '').split('\n').slice(0, 4).join(' / ')}`);
    } else {
      const out = JSON.parse(r.stdout.trim().split('\n').pop());
      if (out.hasKey) {
        fail('가짜 config 에서도 liveFetchMaxDays 키가 남아 있습니다 — 이 검사가 아무것도 안 잽니다');
      } else if (!Number.isFinite(out.days) || out.days !== out.def) {
        fail(`recentWindow 가 기본값(${out.def})이 아니라 ${out.days} 를 냅니다 (NaN 이면 옛 버그가 재현된 것)`);
      } else {
        pass(`liveFetchMaxDays 키가 없어도 recentWindow 가 기본값(${out.def}) 그대로 캡핑한다`);
      }
    }
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

if (ok) {
  console.log('\n[check-live-fetch-max-days] OK — liveFetchMaxDays 의 기본값이 한 곳에만 있고 세 자리가 그것을 쓴다.');
} else {
  console.error('\n고칠 곳: src/slack-live.js 의 DEFAULT_LIVE_FETCH_MAX_DAYS 와 그것을 쓰는 자리(archive-health.js·slack-live.js·llm/tools.js).');
  process.exitCode = 1;
}
