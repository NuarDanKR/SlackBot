#!/usr/bin/env node
/**
 * `pf-archiver` 전환 **관문** — 채널 공개 여부 manifest 와 정본을 전수 대조한다.
 *
 *   HERMES_MODE=pf-archiver node scripts/check-archiver-privacy.js [manifest 경로]
 *
 * 경로는 인자 · `HERMES_PRIVACY_MANIFEST` · `config.json` 의 `archiver.privacyManifest`
 * 순으로 찾는다.
 *
 * 종료코드: 0 전환 가능 / 1 **전환 금지** / 2 대조 자체를 못 했다
 *
 * ── 권위가 어디 있나 ──
 *
 * `archive_channel_mode.is_private` 다. 그 값은 Slack 의 `conversations.list` 가 주고
 * `channel_membership.sync` 가 적는다. TYBot 쪽 읽기 전용 exporter
 * (`tybot.archive.privacy_manifest`)가 그것만 JSON 한 장으로 내보낸다.
 *
 * **정본의 `visibility` 는 권위가 아니다.** 수집기가 raw 에 그 값을 안 넘겨서 전 채널이
 * writer 기본값 `private` 로 박힌다. 첨부 정본에만 실제 값이 실리고, 첨부 없는 채널에는
 * 아무 신호가 없다. 그래서 그 값은 **경고용 대조**에만 쓴다.
 *
 * ── Hermes 는 DB 에 안 붙는다 ──
 *
 * 이 스크립트는 **파일만 읽는다.** DB 자격증명을 Hermes 쪽에 두면 PF 에 운영 DB 로 가는
 * 길이 하나 더 생기고, 그 길은 읽기 전용이라는 보장이 없다.
 *
 * ── 막는 쪽이 기본값 ──
 *
 * | 갈래 | 동작 |
 * |---|---|
 * | manifest 가 없다·못 읽는다·형식이 다르다 | **2** — 대조를 못 했다 |
 * | manifest 의 workspace 가 설정과 다르다 | **2** — 남의 워크스페이스 파일이다 |
 * | 정본에 있는데 manifest 에 없다(미확인) | **1** — 공개 여부를 모른다 |
 * | manifest 가 비공개인데 선언에 없다 | **1** — Hermes 가 공개로 다룬다 |
 * | manifest 가 공개인데 선언은 비공개 | 경고. 닫히는 쪽이라 막지 않는다 |
 * | manifest 에 있는데 정본에 없다 | 경고. 아직 수집이 안 됐을 뿐이다 |
 * | 첨부 정본의 visibility 가 manifest 와 다르다 | 경고. 수집 뒤 Slack 에서 바뀐 것일 수 있다 |
 *
 * 「미확인」 을 통과시키지 않는 것이 이 관문의 핵심이다. 모르는 채널을 공개로 다루면
 * 오류가 아니라 **평범한 답변**으로 내용이 나가고, 내용을 아는 사람만 알아챈다.
 */
import fs from 'node:fs';
import path from 'node:path';
import {
  ARCHIVE_SOURCE, archiveSource, archiverConfigProblem, config, normalizeChannel,
} from '../src/config.js';

/** manifest 형식 이름. TYBot 쪽 `privacy_manifest.SCHEMA` 와 같아야 한다. */
const SCHEMA = 'channel-privacy-manifest/v1';

function refuseHard(message) {
  console.error(message);
  process.exit(2);
}

if (ARCHIVE_SOURCE !== 'archiver') {
  refuseHard('HERMES_MODE 가 pf-archiver 가 아닙니다 — 대조할 정본이 없습니다.');
}
const problem = archiverConfigProblem();
if (problem) refuseHard(problem);

const manifestPath = process.argv[2]
  || process.env.HERMES_PRIVACY_MANIFEST
  || (config.archiver?.privacyManifest
    ? path.resolve(config.archiver.privacyManifest)
    : '');
if (!manifestPath) {
  refuseHard(
    '공개 여부 manifest 경로가 없습니다.\n'
    + '  config.json 의 archiver.privacyManifest 또는 HERMES_PRIVACY_MANIFEST 를 적으세요.\n'
    + '  만드는 쪽(사내): python -m tybot.archive.privacy_manifest --workspace <ws> --out <경로>',
  );
}

let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
} catch (e) {
  // **「없음」 을 「비공개 채널이 없음」 으로 읽지 않는다.** 그렇게 읽으면 파일이 사라진
  // 날 전 채널이 공개로 판정된다.
  refuseHard(`manifest 를 읽지 못했습니다: ${manifestPath}\n  ${e.message}`);
}
if (!manifest || manifest.schema !== SCHEMA) {
  refuseHard(
    `manifest 형식이 다릅니다: ${JSON.stringify(manifest?.schema)} (기대: ${SCHEMA})\n`
    + '  모르는 형식을 짐작해 읽으면 공개 여부를 잘못 판정합니다.',
  );
}
if (manifest.workspace !== archiveSource.workspace) {
  refuseHard(
    `manifest 의 workspace 가 다릅니다: ${manifest.workspace} (정본: ${archiveSource.workspace})\n`
    + '  남의 워크스페이스 manifest 로 대조하면 이름이 겹치는 채널만 우연히 맞습니다.',
  );
}

const rows = Array.isArray(manifest.channels) ? manifest.channels : null;
if (!rows) refuseHard('manifest 에 channels 배열이 없습니다.');

const byId = new Map();
for (const row of rows) {
  const id = String(row?.channel_id || '');
  if (!id) continue;
  if (typeof row.is_private !== 'boolean') {
    // 값이 불리언이 아니면 **모르는 것**이다. 문자열 'false' 를 참으로 읽거나 그 반대로
    // 읽는 쪽이 둘 다 조용히 틀린다.
    refuseHard(`manifest 의 is_private 가 불리언이 아닙니다: ${id} = ${JSON.stringify(row.is_private)}`);
  }
  byId.set(id, { name: String(row.channel_name || ''), isPrivate: row.is_private });
}

let audit;
try {
  audit = {
    channels: archiveSource.channelNames().map((name) => {
      const meta = archiveSource.channelMeta(name);
      return { name: normalizeChannel(name), id: meta.channelId, canonical: meta.visibility };
    }),
    // 첨부 정본의 visibility — **경고용**이다. 권위가 아니다.
    signalled: Object.fromEntries(
      archiveSource.privateByCanonical()
        .filter((r) => r.signalled)
        .map((r) => [normalizeChannel(r.name), r.visibility]),
    ),
  };
} catch (e) {
  refuseHard(`정본을 읽지 못해 대조하지 못했습니다:\n${e.message}`);
}

const declared = new Set(
  (config.privateChannels || []).map((n) => normalizeChannel(n)).filter(Boolean),
);

const unverified = [];      // 정본에 있는데 manifest 에 없다 → 거부
const undeclared = [];      // manifest 가 비공개인데 선언에 없다 → 거부
const overDeclared = [];    // manifest 가 공개인데 선언은 비공개 → 경고
const disagreed = [];       // 첨부 정본의 값과 manifest 가 다르다 → 경고

for (const channel of audit.channels) {
  const row = byId.get(channel.id);
  if (!row) {
    unverified.push(`${channel.name} (${channel.id})`);
    continue;
  }
  if (row.isPrivate && !declared.has(channel.name)) {
    undeclared.push(`${channel.name} (${channel.id})`);
  }
  if (!row.isPrivate && declared.has(channel.name)) {
    overDeclared.push(`${channel.name} (${channel.id})`);
  }
  const signal = audit.signalled[channel.name];
  if (signal && (signal === 'private') !== row.isPrivate) {
    disagreed.push(`${channel.name} (${channel.id}): 첨부=${signal} · manifest=${row.isPrivate ? 'private' : 'public'}`);
  }
}

const seenIds = new Set(audit.channels.map((c) => c.id));
const notCollected = [...byId.keys()].filter((id) => !seenIds.has(id));

console.log(
  `정본 채널 ${audit.channels.length}개를 manifest ${byId.size}개와 대조했습니다.`
  + `\nmanifest: ${manifestPath} (생성 ${manifest.generated_at || '?'})`,
);

if (unverified.length) {
  console.error(`\n✗ manifest 에 없는 채널 ${unverified.length}개 — **전환 금지**`);
  for (const line of unverified) console.error(`    ${line}`);
  console.error(
    '\n  공개 여부를 모르는 채널입니다. 모르는 것을 공개로 다루면 오류가 아니라\n'
    + '  평범한 답변으로 내용이 나가고, 내용을 아는 사람만 알아챕니다.\n'
    + '  사내에서 멤버십 동기화를 돌린 뒤 manifest 를 다시 내보내세요.',
  );
}
if (undeclared.length) {
  console.error(`\n✗ manifest 가 비공개인데 privateChannels 에 없는 채널 ${undeclared.length}개 — **전환 금지**`);
  for (const line of undeclared) console.error(`    ${line}`);
  console.error('\n  config.json 의 privateChannels 에 위 이름을 그대로 적은 뒤 다시 돌리세요.');
}
if (overDeclared.length) {
  console.log(`\n· manifest 는 공개인데 선언은 비공개인 채널 ${overDeclared.length}개`);
  for (const line of overDeclared) console.log(`    ${line}`);
  console.log('  닫히는 쪽이라 막지 않습니다. 둘 중 하나는 낡았으니 확인하세요.');
}
if (disagreed.length) {
  console.log(`\n· 첨부 정본의 visibility 와 manifest 가 다른 채널 ${disagreed.length}개`);
  for (const line of disagreed) console.log(`    ${line}`);
  console.log('  수집 뒤 Slack 에서 공개 여부가 바뀌면 이렇게 보입니다. manifest 가 권위입니다.');
}
if (notCollected.length) {
  console.log(`\n· manifest 에 있는데 정본에 아직 없는 채널 ${notCollected.length}개`);
  for (const id of notCollected) console.log(`    ${byId.get(id).name || '(이름 없음)'} (${id})`);
  console.log('  수집이 아직 그 채널에 닿지 않았을 뿐입니다. 막지 않습니다.');
}
if (!unverified.length && !undeclared.length && !overDeclared.length
    && !disagreed.length && !notCollected.length) {
  console.log('\n✓ 엇갈림 없음.');
}

process.exit(unverified.length || undeclared.length ? 1 : 0);
