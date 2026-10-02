#!/usr/bin/env node
/**
 * `deploy/hermes.service` — StartLimit* 가 systemd 가 실제로 읽는 절([Unit])에 있나.
 *
 *   node scripts/check-service-unit.js
 *
 * 종료코드: 0 통과 / 1 실패
 *
 * ── 왜 필요한가 ──
 *
 * systemd 는 `StartLimitIntervalSec`·`StartLimitBurst` 를 **[Unit] 절에서만** 읽는다.
 * [Service] 절에 두면 문법 오류는 안 나고 `Unknown key 'StartLimitIntervalSec' in section
 * [Service], ignoring` 경고만 journal 에 남긴 채 무시된다 — 그러면 「5분에 5번 넘게 죽으면
 * 멈춘다」는 주석과 달리 **systemd 기본값(10초에 5번)** 이 적용되어, 설정 오류로 무한
 * 재시작하며 API 를 두드리는 것을 막으려던 창이 훨씬 좁아진다.
 *
 * VM 의 journalctl 에 이 경고가 실물로 6건 있었다(2026-09-03 확인). 파일만 읽고 파싱하므로
 * 네트워크도, 실제 systemd 도 필요 없다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FILE = path.join(ROOT, 'deploy', 'hermes.service');

const text = fs.readFileSync(FILE, 'utf8');

/** 섹션 이름 → 그 섹션의 본문 줄 배열. */
function sections(unitText) {
  const out = {};
  let cur = null;
  for (const line of unitText.split('\n')) {
    const m = line.match(/^\[(\w+)\]\s*$/);
    if (m) {
      cur = m[1];
      out[cur] = out[cur] || [];
      continue;
    }
    if (cur) out[cur].push(line);
  }
  return out;
}

const sec = sections(text);
const KEYS = ['StartLimitIntervalSec', 'StartLimitBurst'];

for (const key of KEYS) {
  const inUnit = (sec.Unit || []).some((l) => l.trim().startsWith(`${key}=`));
  const inService = (sec.Service || []).some((l) => l.trim().startsWith(`${key}=`));
  if (inService) {
    fail(`${key} 가 [Service] 절에 있습니다 — systemd 가 이 절에서는 이 키를 안 읽습니다 (Unknown key … ignoring). [Unit] 절로 옮기세요.`);
  }
  if (!inUnit) {
    fail(`${key} 가 [Unit] 절에 없습니다 — 재시작 상한이 안 걸립니다 (systemd 기본값 10초/5번이 적용됩니다).`);
  }
}

if (!ok) process.exit(1);
console.log('  ✓ StartLimitIntervalSec·StartLimitBurst 가 [Unit] 절에 있습니다');
