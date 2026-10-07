#!/usr/bin/env node
/**
 * Archiver 정본에서 투영한 **채널 본문과 좌표표**를 JSON 으로 낸다.
 *
 *   node scripts/archiver-channel.js <채널 이름 또는 채널 ID>
 *   node scripts/archiver-channel.js --list
 *
 * 종료코드: 0 성공 / 1 모드가 아니거나 못 찾음
 *
 * ── 왜 이 스크립트가 있나 ──
 *
 * `archive-inbox` 스킬(파이썬)이 결정에 **원문 좌표**를 적어야 한다. `pf-archiver` 에서
 * 채널 본문은 디스크에 그 모양으로 없고 정본에서 투영된 것이라, 파이썬이 파일을 열 수
 * 가 없다.
 *
 * 파이썬에서 정본을 다시 파싱하지 않는다. 그러면 투영이 **두 벌**이 되고, 두 벌은
 * 갈린다 — 갈리면 화면이 보여 준 원문과 기록에 적힌 좌표가 다른 메시지를 가리킬 수
 * 있고 그건 조용히 틀린다. `summary_hash` 가 같은 이유로 JS 를 부르는 것과 같은 자리다
 * (`review_work.summary_hash` 주석).
 *
 * ── 쓰기는 하지 않는다 ──
 *
 * 읽기 전용이다. `HERMES_MODE=pf` 에서는 투영이 없으므로 **거절한다** — 그때 파이썬은
 * 지금까지처럼 `slack-export/channels/*.md` 를 직접 읽는다.
 */
import { ARCHIVE_SOURCE, archiveSource } from '../src/config.js';

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

const arg = process.argv[2];
if (!arg) fail('채널 이름 또는 채널 ID 를 주세요. (--list 로 목록)');

if (ARCHIVE_SOURCE !== 'archiver' || !archiveSource) {
  fail(
    'HERMES_MODE 가 pf-archiver 가 아닙니다 — 투영할 Archiver 정본이 없습니다.\n'
    + 'pf 모드에서는 slack-export/channels/*.md 를 직접 읽으세요.',
  );
}

if (arg === '--list') {
  process.stdout.write(JSON.stringify({
    workspace: archiveSource.workspace,
    root: archiveSource.root,
    channels: archiveSource.channelNames().map((name) => {
      const meta = archiveSource.channelMeta(name);
      return { name, channelId: meta.channelId, firstName: meta.firstName, dirName: meta.dirName };
    }),
  }));
  process.exit(0);
}

const meta = archiveSource.channelMeta(arg);
if (!meta) fail(`Archiver 정본에서 채널을 찾지 못했습니다: ${arg}`);

process.stdout.write(JSON.stringify({
  workspace: archiveSource.workspace,
  channelId: meta.channelId,
  name: meta.name,
  firstName: meta.firstName,
  // 투영된 본문. 파이썬의 `evidence_block()` 이 **이 글자에서** 인용을 찾는다.
  text: archiveSource.readKey(archiveSource.channelKeyOf(arg)),
  // 투영 줄 범위 → 정본 좌표. 1-based, 양끝 포함.
  coords: archiveSource.coordinates(arg),
}));
