#!/usr/bin/env node
/**
 * 과거 대화를 채운다.
 *
 *   npm run backfill -- --dry-run
 *   npm run backfill -- --channel 사업장가
 *   npm run backfill -- --since 2025-01
 *   npm run backfill -- --all-channels     ← 전 채널을 실제로 쓸 때만
 *
 * 종료코드: 0 전부 됨 / 1 한 채널이라도 실패·건너뜀
 *
 * **커밋·push 는 안 한다.** 무엇이 커밋되는지는 사람이 봐야 한다.
 *
 * ── 돌린 뒤에 잰다 ──
 *
 * **이 도구의 진짜 목적은 한 번도 검증된 적이 없다.** 42채널 시험은 「지운 달을 되채워
 * 원본과 대는」 방식이라 정답지가 원본 md 인데, 백필을 만든 이유는 **원본이 한 번도 안
 * 가졌던 과거**를 채우는 것이고 그 경로는 대볼 정답지가 없다 (2026-09-03).
 *
 * 그래서 **슬랙을 정답지로 삼는** 검증기를 따로 두었다. 일부러 파이썬으로 썼고
 * `conversations.history` 를 하루 단위 창으로 다시 훑는다 — 같은 코드로 재면 같은 버그를
 * 함께 갖는다.
 *
 *   python .claude/skills/slack-sync/scripts/verify_backfill.py --channel 사업장가 \
 *       --from 2026-06-01 --to 2026-09-04
 *   python .claude/skills/slack-sync/scripts/verify_backfill.py --all --from … --to …
 *
 * 보는 것 — ① 슬랙과 md 헤더가 **양방향**으로 같나(「md 에만 있는 것」이 원본 대조로는
 * 구조적으로 못 보던 방향이다) ② 헤더 자리인데 파싱 안 되는 줄이 있나. 실제로 돌리기
 * **전에 한 번, 후에 한 번** 재서 경계 위 구간이 안 변했는지 보는 것이 쓰는 법이다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { WebClient } from '@slack/web-api';
import { config, CHANNELS_DIR, isPrivateChannel, normalizeChannel } from '../src/config.js';
import { listBotChannels, getUserMap, getSelfId } from '../src/slack-live.js';
import { listAllChannels } from '../src/archive.js';
import {
  monthChunks, writeMonth, channelSkeleton, stripMissingMarks,
  loadBackfillState, saveBackfillState, advance, archiveFloor,
  backfillTargets, mdFileFor,
} from '../src/ingest/backfill.js';
import { runDerive } from '../src/ingest/derive.js';

// --- TYBot 연동 모드 관문 -----------------------------------------------------
// CLI 는 사람이 직접 치는 자리다. 깊은 곳에서 던지면 스택만 보이고 무엇을 해야
// 하는지 안 보이므로, **여기서 먼저** 사람 말로 멈춘다.
import { isTybotMode, ArchiveWriteBlocked } from '../src/mode.js';
if (isTybotMode()) {
  const blocked = new ArchiveWriteBlocked('npm run backfill');
  console.error(blocked.message);
  process.exit(2);
}

/* 이 아래는 함수로 감싼다 — 조기 종료를 `process.exit()` 이 아니라
 * `process.exitCode` + `return` 으로 하기 때문이다. 최상위(모듈 스코프)에서는
 * `return` 을 못 쓰므로(ESM 이라 top-level `return` 은 문법 오류) 함수 몸통이
 * 있어야 조기 종료가 된다.
 *
 * **`process.exit()` 을 안 쓰는 이유** (2026-09-02): 윈도우에서 슬랙 호출이
 * 성공한 뒤 `process.exit()` 을 부르면 `@slack/web-api` 의 `WebClient` 가 열어
 * 둔 비동기 핸들 위로 이벤트 루프가 즉시 끊겨
 * `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)` 로 죽고, 종료코드가
 * 넘긴 값이 아니라 **127** 로 뭉개진다. 마법사 5단계가 이 종료코드로 성공·실패를
 * 가리므로 127 은 실패를 성공처럼 보이게 할 수 있다. `process.exitCode = n` 은
 * 값만 정하고 루프가 스스로 비워지길 기다려 핸들이 정상적으로 닫힌다. */
async function main() {
  const arg = (name) => {
    const i = process.argv.indexOf(name);
    return i >= 0 ? process.argv[i + 1] : undefined;
  };
  const DRY = process.argv.includes('--dry-run');
  const SINCE = arg('--since');
  const ONLY = arg('--channel');

  const client = new WebClient(process.env.SLACK_BOT_TOKEN);
  const tz = config.timezone;
  const started = Date.now();
  /** 오늘 날짜 (설정의 시간대 기준). 이 저장소가 쓰는 방식 그대로다 — `src/llm/qa.js` 의 today 참조. */
  const TODAY = new Intl.DateTimeFormat('sv-SE', { timeZone: tz }).format(new Date());
  /** 메시지 헤더에서 날짜만 뽑는다. `archive.js` 의 splitMessages 와 같은 모양을 본다. */
  const HDR_DATE = /^\*\*(\d{4}-\d{2}-\d{2}) \d{2}:\d{2} · /gm;

  const [selfId, userMap, channels] = await Promise.all([
    getSelfId(client), getUserMap(client), listBotChannels(client),
  ]);

  /* **`digest.skipChannels` 를 뺀다 — 자동 반영과 같은 규칙이다** (최종 검토 F1, 2026-09-02).
   * 판정은 `backfillTargets` 한 자리에 있고 시험은 `check-backfill.js` 의 ⑧ 절이 본다. */
  const skipChannels = config.digest?.skipChannels || [];
  const targets = backfillTargets(channels, { only: ONLY, skip: skipChannels });
  if (ONLY && !targets.length) {
    const isSkipped = skipChannels.some((n) => normalizeChannel(n) === normalizeChannel(ONLY));
    console.error(isSkipped
      ? `«${ONLY}» 는 config.json 의 digest.skipChannels 에 있어 «다루지 않는 채널»입니다.\n`
        + '  넣으려면 그 목록에서 먼저 지우세요 — 그래야 자동 반영도 함께 다뤄 두 경로가 안 갈립니다.'
      : `봇이 든 채널 중에 «${ONLY}» 가 없습니다. 4단계(채널 참여)를 먼저 보세요.\n`
        + '  개명된 채널이면 md 파일명이 아니라 **지금 슬랙 이름**으로 부르세요.');
    process.exitCode = 1;
    return;
  }
  /* 몇 개를 왜 뺐는지 밝힌다 — 조용히 줄이면 「전부 훑었다」로 읽힌다. */
  if (!ONLY && targets.length < channels.length) {
    console.log(`[백필] 안 다루기로 한 채널 ${channels.length - targets.length}개를 뺐습니다`
      + ' (config.json 의 digest.skipChannels)');
  }

  /* **전 채널을 쓰려면 말로 시켜야 한다.**
   * 인자 없이 부르면 봇이 든 채널 전부를 몇 년치 되짚어 md 에 쓴다 — 되돌리려면 아직
   * 커밋 전일 때 `git restore` 를 해야 하고, 커밋 뒤라면 훨씬 성가시다. 실수로 한 번
   * 도는 것을 막을 값이 이 한 줄이라 둔다.
   * (2026-09-01: 이 스크립트를 `import()` 로 「문법만 보려던」 명령이 인자 없이 최상위를
   *  돌려 47채널 백필이 시작됐다. 10채널까지 진행됐고 커밋 전이라 되돌렸다.) */
  if (!ONLY && !process.argv.includes('--all-channels') && !DRY) {
    console.error(`봇이 든 채널 ${targets.length}개 전부를 백필하려 합니다.`);
    console.error('실수를 막으려고 이 경우에는 확인을 요구합니다. 둘 중 하나로 다시 부르세요:');
    console.error('  npm run backfill -- --channel <채널이름>      한 채널만');
    console.error('  npm run backfill -- --all-channels            전부 (되돌리기 어렵습니다)');
    console.error('  npm run backfill -- --dry-run                 세기만 (md 를 안 건드립니다)');
    process.exitCode = 1;
    return;
  }

  const state = loadBackfillState();
  const failed = [];
  const report = [];
  let total = 0;
  /* **못 읽은 스레드의 합계.** `monthChunks` 가 청크마다 `missedThreads` 를 실어 보내는데
   * (`src/ingest/backfill.js`), 그 값이 닿는 곳이 여기밖에 없다 — 청크마다 찍는 `⚠` 줄은
   * `--all-channels` 몇 시간짜리 로그 한가운데에 묻힌다. 백필이 넣는 부모 글은 자동 반영의
   * 되돌아보기 구간(30일) 밖이라 **다시 안 읽힌다**(그 파일의 flush 주석 참조) — 그래서
   * 「넣은 것 N건」 옆에 못 읽은 수가 안 붙으면 그 유실은 어디에도 안 남는다. */
  let missedThreads = 0;

  /* 개명 화석 — `.sync-state.json` 의 `file`. **채널 ID 로 맞춘다** (최종 검토 F2, 2026-09-02).
   * 이게 없으면 개명된 채널마다 없는 파일을 가리켜 「아카이브가 비어 있음」을 찍고
   * 전체 이력을 새 이름으로 다시 넣는다 — 같은 사업장이 두 이름으로 봇에 실린다. */
  const known = listAllChannels();

  for (const [i, ch] of targets.entries()) {
    const saved = state.channels[ch.id];
    const file = mdFileFor(ch, { savedFile: saved?.file, known });
    const mdPath = path.join(CHANNELS_DIR, `${file}.md`);

    /* **경계 계산도 실패할 수 있다 — 그 채널만 건너뛰고 나머지는 계속한다.**
     * `archiveFloor` 안의 `minuteToEpoch` 는 md 의 헤더 시각을 되짚어 못 맞추면
     * (예: `2026-02-30` 처럼 실존하지 않는 날짜가 헤더 모양으로 박혀 있으면) 조용히
     * 틀리는 대신 던진다. 이 계산이 아래 본문 `try` **앞**에 있어서 여기서 던지면
     * `catch (err) { failed.push(...) }` 를 못 만나고 프로세스가 그대로 죽는다 —
     * 파일 자신의 설계(138행 「한 채널이 실패해도 나머지는 계속한다」)와 어긋나고,
     * `--all-channels` 로 42채널을 몇 시간 돌리다 서른 번째에서 이러면 그 회차의
     * report 표 전체가 사라진다 (2026-09-02 리뷰가 `**2026-02-30 10:00 · 테스터**`
     * 로 재현). 그래서 이 계산만 따로 감싸 실패하면 그 채널을 건너뛴다. `mdPath` 는
     * 이 try 밖에 그대로 둔다 — 아래 본문 `finally` 가 쓴다. */
    let floor;
    try {
      floor = archiveFloor(mdPath, tz);
    } catch (err) {
      failed.push({ name: ch.name, why: err.message });
      console.log(`[백필] #${ch.name}  경계를 못 잡았습니다 — 건너뜁니다  (${err.message})`);
      continue;
    }

    /* **시작점은 md 에서 읽는다 — 상태 파일의 `latest` 를 쓰지 않는다.**
     * 예전에는 `Date.now()` 로 **오늘부터** 거꾸로 읽어서, 아카이브가 이미 갖고 있는 몇 달
     * 치를 매번 다시 읽어 들였다. 그 겹침을 지워내려고 짝짓기가 필요했고 거기서 조용한 유실이
     * 났다. 경계 아래만 읽으면 겹침 자체가 없다.
     * md 를 진실로 삼는 것이 요점이다 — 중간에 죽어도 그때까지 들어간 만큼 경계가 내려가
     * 있어서, 다시 돌리면 그 아래부터 이어간다. */
    const prev = {
      ...(saved || { months: [], oldest: null, done: false }),
      file,
      latest: floor ? floor.latest : String(Math.floor(Date.now() / 1000)),
    };
    console.log(floor
      ? `[백필] #${ch.name}  경계 ${floor.minute} — 이 분보다 오래된 것만 읽습니다`
      : `[백필] #${ch.name}  경계 없음 (아카이브가 비어 있음) — 전부 읽습니다`);
    let cur = prev;
    let added = 0;
    let missedHere = 0;

    try {
      if (!DRY && !fs.existsSync(mdPath)) {
        // **네 값을 다 넘긴다.** channelId·today 를 빠뜨리면 헤더에 `undefined` 가 그대로
        // 찍히는데, 에러가 아니라 **글자**라 아무도 안 막는다. 그리고 새 채널 md 를 만드는
        // 이 경로가 곧 마법사가 새 팀에게 시키는 경로다 (2026-09-01 사본 시험에서 잡혔다).
        fs.writeFileSync(mdPath, channelSkeleton({
          name: ch.name,
          isPrivate: isPrivateChannel(ch.name),
          channelId: ch.id,
          today: TODAY,
        }), 'utf8');
        console.log(`  만듦  ${prev.file}.md`);
      }

      for await (const chunk of monthChunks(client, ch.id, {
        latest: cur.latest, since: SINCE, tz, selfId, userMap,
      })) {
        /* **`chunk.kept` 는 찍어낸 블록 수이지, 실제로 들어간 수가 아니다.**
         * 죽고 다시 돌리면 이미 md 에 있는 달을 또 렌더해 다시 넣으려 하는데,
         * `insert_messages.py` 는 이미 있는 것을 걸러 그만큼만 안 쓴다. 「이미
         * 반영됨」 문구의 유무로 이진 판정(전부 반영됨/아님)하면, 청크 안에서
         * **일부만** 이미 있는 경우를 놓친다 — 그때 파이썬은 새것만 써서 성공하고
         * 평범한 「OK 삽입」을 돌려주므로, 문구만 보면 그 부분 성공을 「청크 전체를
         * 새로 썼다」로 과대 집계한다 (2026-09-02, 이진 판정으로 시험했다가 이
         * 세 번째 경우를 못 잡는다고 지적받았다 — 「넣음」이 거짓말하지 않게
         * 하려던 이 과제 자체의 목적이 깨지는 구멍이었다). 그래서 `writeMonth` 가
         * md 의 헤더 수를 넣기 전/후로 세어 돌려주는 `added` 를 그대로 쓴다 —
         * 파이썬이 뭐라고 말했는지가 아니라 md 에 실제로 늘어난 줄 수다.
         * `--dry-run` 은 실제로 안 넣으므로 파이썬을 부르지 않고(셀 것이 없다)
         * 추정치(`chunk.kept`)를 「넣을 것」이라고 밝혀 찍는다. */
        const res = (!DRY && chunk.blocks.length)
          ? await writeMonth(mdPath, chunk.month, chunk.blocks)
          : { notes: [], added: 0 };
        const written = DRY ? chunk.kept : res.added;
        const dropped = chunk.seen - chunk.kept;
        console.log(
          `[백필] 채널 ${i + 1}/${targets.length}  #${ch.name}  ${chunk.month}`
          + `  ${DRY ? '넣을 것' : '넣음'} ${written}건`
          + (dropped ? ` (걸러짐 ${dropped}건)` : '')
          + `  경과 ${Math.round((Date.now() - started) / 60000)}분`,
        );
        // 그 달에 조용히 넘어간 실패(스레드 읽기 등). 안 찍으면 「넣음 N건」만 보이고
        // 빠진 답글이 있다는 사실이 어디에도 안 남는다.
        for (const n of chunk.notes) console.log(`         ⚠ ${n}`);
        /* 파이썬이 찍은 줄도 함께 올린다 — 버리면 `insert_messages.py` 가 일부러 찍는
         * 신호가 화면 어디에도 안 닿는다. 「이미 반영됨」은 셈이 이미 0으로 말해 주지만
         * 그래도 왜 0인지 보여주는 데 값이 있어 남긴다 — 매번 나오는 평범한 「OK 삽입」
         * 확인 줄만 걸러 화면이 안 늘어지게 한다. */
        for (const n of res.notes) {
          if (n.startsWith('OK 삽입') && !n.includes('이미 반영됨')) continue;
          console.log(`         · ${n}`);
        }
        cur = advance(cur, chunk);
        added += written;
        total += written;
        missedHere += chunk.missedThreads || 0;
        missedThreads += chunk.missedThreads || 0;
        if (!DRY) { state.channels[ch.id] = cur; saveBackfillState(state); }
      }

      if (!DRY && added) {
        const { text, removed, unmatched } = stripMissingMarks(fs.readFileSync(mdPath, 'utf8'));
        if (removed) { fs.writeFileSync(mdPath, text, 'utf8'); console.log(`  「미수집 구간」 마커 ${removed}줄 지움  ${prev.file}.md`); }
        // 마커 모양이 아닌데 그 낱말이 든 줄. 지우지 않았으니 관문은 계속 막는다 —
        // 왜 막히는지 여기서 안 알리면 사람이 「지운 줄 0」만 보고 헤맨다.
        for (const u of unmatched) console.log(`  ⚠ 마커 모양이 아니라 안 지웠습니다  ${prev.file}.md: ${u}`);
      }

      report.push({
        name: ch.name, added, missed: missedHere, oldest: cur.oldest, floorMinute: floor ? floor.minute : null,
      });
    } catch (err) {
      // 한 채널이 실패해도 나머지는 계속한다. 다만 그 채널은 「채웠다」로 안 적는다.
      failed.push({ name: ch.name, why: err.data?.error || err.message });
    } finally {
      /* 뼈대가 적어 둔 `**기간**: (백필 중)` 을 실제 범위로 바꾼다.
       *
       * **`sync_index.py` 가 못 고친다.** 그 스크립트의 `PERIOD_END_RE`(88행)는
       * `**기간**:` 뒤에 **날짜**가 오는 모양만 찾으므로 `(백필 중)` 은 애초에 안 걸린다.
       * 그대로 두면 다 채운 채널이 봇 프롬프트 색인에 영영 「백필 중」으로 실린다 —
       * 에러가 아니라 **틀린 사실**로 나간다 (2026-09-01 사본 시험에서 잡혔다).
       * 한 번 진짜 날짜로 바꿔 두면 그 뒤로는 sync_index 가 끝날짜를 따라간다.
       *
       * **「넣은 것이 있을 때」 안에 두면 안 된다.** 뼈대는 읽기 **전에** 만들어지므로,
       * 넣을 것이 0건이거나 중간에 던지면 그 거짓 표시가 파일에 남은 채 끝난다 — 고치려던
       * 그 사고가 다른 문으로 들어오는 자리다 (2026-09-01 최종 검토). 그래서 예외가 나도
       * 도는 `finally` 에 둔다. 헤더가 하나도 없으면 `(대화 없음)` 으로 적는다 — 그것도
       * 사실이고 「백필 중」으로 굳는 것보다 낫다. */
      if (!DRY && fs.existsSync(mdPath)) {
        const md = fs.readFileSync(mdPath, 'utf8');
        if (md.includes('**기간**: (백필 중)')) {
          const dates = [...md.matchAll(HDR_DATE)].map((m) => m[1]).sort();
          const span = dates.length ? `${dates[0]} ~ ${dates[dates.length - 1]}` : '(대화 없음)';
          fs.writeFileSync(mdPath, md.replace('**기간**: (백필 중)', `**기간**: ${span}`), 'utf8');
          console.log(`  기간 적음  ${span}  ${prev.file}.md`);
        }
      }
    }
  }

  /* 파생값을 다시 센다 — **안 하면 커밋이 막힌다.**
   * `verify_archive.py` 가 ③ 헤더의 `**실제 메시지**: N건` 과 ④ 기간 끝날짜를 보고,
   * `pre-commit` 이 그것을 부른다. 백필은 md 에 메시지를 넣기만 하고 헤더는 안 고치므로
   * 여기서 맞추지 않으면 사람이 커밋하려 할 때 관문에서 멈춘다.
   * 계산은 여기 두지 않는다 — 자동 반영이 회차마다 부르는 `runDerive` 를 그대로 쓴다. */
  let deriveFailed = false;
  if (!DRY && total) {
    const d = await runDerive();
    if (d.failed) {
      /* **종료코드에 넣는다.** 이게 없으면 파생값이 안 맞는데도 0 으로 끝나서, 부르는 쪽
       * (마법사 5단계)이 성공으로 읽는다. 화면 출력은 몇 시간짜리라 사람이 못 보고 지나간다
       * (2026-09-01 최종 검토). */
      deriveFailed = true;
      console.log(`\n⚠ 파생값 재계산 실패 — ${d.failed}`);
      console.log('  이대로는 `pre-commit` 이 커밋을 막습니다.');
      console.log('  python .claude/skills/slack-sync/scripts/sync_index.py --all --dry-run 으로 보세요.');
    } else {
      console.log(`\n파생값 재계산: 고친 곳 ${d.changed.length}개`
        + (d.unresolved.length ? ` · 기계가 못 고친 것 ${d.unresolved.length}건` : ''));
      for (const u of d.unresolved) console.log(`  ⚠ ${u.where} — ${u.detail}`);
    }
  }

  /* 꼬리는 **0 이면 안 붙인다.** 늘 붙으면 「0건」이 매 회차의 배경이 되어 안 읽히고,
   * 이 값은 0 이 정상이라 붙어 있을 때만 뜻이 있다. */
  console.log(`\n넣은 것 ${total}건 · 채널 ${report.length}개 · 경과 ${Math.round((Date.now() - started) / 60000)}분`
    + (missedThreads ? ` · 스레드 못 읽음 ${missedThreads}건` : ''));

  /* 「가장 오래된 날짜」 표. 슬랙 요금제에 따라 과거가 90일치만 남기도 하는데,
   * 그때도 백필은 **정상으로 끝난다.** 이 표가 없으면 그 상태가 「다 채웠다」로 읽힌다. */
  console.log('\n채널별 가장 오래된 대화');
  for (const r of report) {
    /* **비어 있어도 「없음」이 아닐 수 있다.** 경계 아래에서 아무것도 못 찾았다는 것은
     * 「이 채널이 안 채워졌다」가 아니라 「경계가 곧 슬랙에 남은 가장 오래된 대화」라는
     * 뜻이다 — md 가 이미 그 경계까지 갖고 있고, 그 아래를 다 훑었는데도 더 없었다는
     * 뜻이기 때문이다. 예전 씨앗(`Date.now()`)은 늘 청크를 하나는 만들어서 이 모양이
     * 드러난 적이 없었다(2026-09-02 리뷰가 지적). 경계 자체가 없는(새 채널) 채로
     * 못 찾았으면 정말 아무것도 모르는 것이라 그때는 「없음」이 맞다. */
    const d = r.oldest
      ? new Date(Number(r.oldest) * 1000).toISOString().slice(0, 10)
      : (r.floorMinute ? r.floorMinute.slice(0, 10) : '없음');
    console.log(`  ${d}  #${r.name}  (${r.added}건${r.missed ? ` · 스레드 못 읽음 ${r.missed}건` : ''})`);
  }
  console.log('\n이 날짜보다 앞은 슬랙에 남아 있지 않습니다 — 요금제의 보존 기간을 확인하세요.');

  if (failed.length) {
    console.log(`\n못 채운 채널 ${failed.length}개 — **0건이 아닙니다**`);
    for (const f of failed) {
      console.log(`  #${f.name}  ${f.why}`
        + (f.why === 'not_in_channel' ? '  ← 봇이 그 채널에 없습니다. 4단계로 돌아가세요' : ''));
    }
  }

  console.log(DRY
    ? '\n--dry-run 이라 md 를 건드리지 않았습니다.'
    : '\n커밋은 사람이 합니다. 채널 단위로 나눠 커밋하시길 권합니다 (되돌릴 때 한 채널만 되돌립니다).');

  process.exitCode = failed.length || deriveFailed ? 1 : 0;
}

await main();
