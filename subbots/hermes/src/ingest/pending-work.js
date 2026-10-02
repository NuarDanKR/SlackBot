/**
 * 07:00 회차가 찾아낸 「사람이 손대야 하는 것」을 파일로 남긴다.
 *
 * **DM 은 알림이고 이 파일이 목록이다.** 전에는 DM 본문이 어디에도 안 남아서, 사람이
 * 손으로 복사해 Claude Code 에 붙여넣는 것 말고 들어올 길이 없었다. 붙여넣고 나면
 * 건별로 멈추는 자리도 없어서, 「상단 요약과 다른 것」처럼 **원문을 다시 봐야 하는
 * 후보**까지 한꺼번에 반영됐다 (2026-08-12 에 5건 중 2건이 DM 문구와 원문이 달랐다).
 *
 * **`.pending-edits.json`(슬랙에서 고쳐지거나 지워진 것)은 여기 담지 않는다.** 그쪽은
 * 이미 도는 장치가 있고(`apply_edits.py`), 매일 도는 경로를 건드리면 깨질 때 매일 깨진다.
 * 사람이 보는 화면에서만 두 목록을 나란히 낸다 (`archive-inbox` 스킬).
 *
 * 이 파일은 봇이 읽지 않는다 — `archive.js`·`documents.js` 어느 쪽도 열지 않는다.
 * 운영 상태이지 아카이브 자료가 아니다 (`.pending-edits.json` 과 같다).
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { ARCHIVE_DIR, CHANNELS_DIR } from '../config.js';
import { readJson, writeJson, activeDeferred } from './util.js';
import { extractSummary } from './summary.js';

export const WORK_FILE = path.join(ARCHIVE_DIR, '.pending-work.json');
const STATE_FILE = path.join(ARCHIVE_DIR, '.sync-state.json');

const h16 = (s) => crypto.createHash('sha256').update(String(s ?? ''), 'utf8').digest('hex').slice(0, 16);

/**
 * 그 채널 요약 자리의 지금 해시 — 「뺌」을 언제 풀지 판정하는 값.
 *
 * 「뺌」은 "이 후보는 틀렸고 요약은 지금 이대로가 맞다"는 판정이다. 요약이 나중에
 * 고쳐지면 그 판정의 전제가 사라지므로 후보로 돌려보내야 한다. 만기를 붙이는 대신
 * 해시를 보는 이유는, 안 바뀐 요약을 만기마다 다시 묻는 것은 그냥 소음이기 때문이다.
 */
export function summaryHash(file) {
  const p = path.join(CHANNELS_DIR, `${file}.md`);
  if (!fs.existsSync(p)) return '';
  return h16(extractSummary(p).text);
}

/**
 * 회차 결과 → 할 일 항목 배열.
 *
 * **id 는 회차가 바뀌어도 같아야 한다** — 며칠째인지가 여기에 달려 있다.
 * 특히 `summary` 의 id 에 **`now`(바뀐 값)를 넣지 않는다.** 모델이 매일 조금씩 다르게
 * 쓰면 id 가 매일 새로 생겨 「며칠째」가 영영 1일로 남는다.
 *
 * 그 대가로 **한 회차 안에서 서로 다른 후보 둘이 같은 id 를 받는 일**이 생긴다. 전에는
 * 뒤엣것이 앞엣것을 조용히 덮었는데, 2026-08-12 첫 실물 회차에서 바로 났다 — DM 은
 * `#사업장다` 을 5건 중 2건으로 적었으나 목록에는 1건만 남았고, 사라진 쪽이 만기
 * (8/25) 대응이라는 더 큰 건이었다. 화면에도 DM 에도 「합쳐졌다」는 표시가 없어서 세어
 * 보지 않으면 모른다.
 *
 * 그래서 이제 **한 항목이 후보를 여럿 담는다**(`more`). 항목을 쪼개지 않는 이유는 같은
 * id = 같은 채널·같은 자리이고, 사람이 고칠 요약 줄도 결국 하나이기 때문이다 — 실제로
 * 그날 두 후보는 `핵심 쟁점` 한 줄에 함께 반영됐다. 나이도 그대로 지켜진다.
 */
export function buildItems(r) {
  const out = [];
  const byId = new Map();
  const push = (kind, id, o) => {
    const prev = byId.get(id);
    if (prev) {
      (prev.more ||= []).push(o);
      return;
    }
    const item = { id, kind, ...o };
    byId.set(id, item);
    out.push(item);
  };

  /* 채널 이름과 md 파일명은 다를 수 있다 — 채널을 개명해도 md 파일명은 그대로 두기
   * 때문이다 (report.js 의 「채널 개명」 블록). `summary.js` 도 `t.file || t.channel` 로
   * 파일을 연다. 이름으로 md 를 찾으면 개명된 채널의 요약 해시가 늘 빈 값이 되어
   * 「뺌」이 안 걸린다. */
  const fileOf = new Map(
    (r.conversations?.channels || []).map((c) => [c.channel, c.file || c.channel]),
  );

  for (const f of r.summary?.findings || []) {
    push('summary', `${f.channel}|summary|${f.type}|${f.where || ''}`, {
      channel: f.channel,
      file: fileOf.get(f.channel) || f.channel,
      type: f.type,
      where: f.where,
      was: f.was,
      now: f.now,
      evidence: f.evidence,
    });
  }

  /* 근거를 원문에서 못 찾아 요약 대조가 스스로 뺀 후보. 조용히 버리지 않고 남긴다 —
   * 모델이 지어낸 것일 수도 있고, 검색이 어긋난 것일 수도 있어 사람이 봐야 갈린다.
   *
   * **그래서 `evidence` 를 함께 담는다.** 갈리라고 남기면서 갈릴 재료를 빼면 남기는 뜻이
   * 없다. 화면(`review_work.py` 의 `context_for`)이 이 값으로 채널 md 에서 원문을 찾고,
   * 못 찾으면 모델이 적은 문장을 그대로 보여준다 — 없으면 「적힌 것: 없음」이 떠서
   * **모델은 적었는데 화면이 「없음」이라고 말한다**(2026-08-13 실물 확인).
   *
   * 찾아질 여지가 실제로 있다 — 관문이 뒤지는 것은 `verifyEvidence(found, t.transcript)`
   * 로 **그 회차에 들어온 대화**뿐인데 화면은 **채널 md 전체**를 뒤지고, 생략부호가 든
   * 인용은 화면(`_needles`)만 쪼개어 찾는다. 즉 「뺐다」가 「원문에 없다」와 같은 말이 아니다. */
  for (const d of r.summary?.dropped || []) {
    push('dropped', `${d.channel}|dropped|${d.type}|${d.where || d.now || ''}`, {
      channel: d.channel,
      file: fileOf.get(d.channel) || d.channel,
      type: d.type,
      where: d.where,
      now: d.now,
      evidence: d.evidence,
      why: d.why,
    });
  }

  for (const u of r.derive?.unresolved || []) {
    push('derive', `${u.where}|derive|${u.kind}`, { where: u.where, type: u.kind, detail: u.detail });
  }

  for (const c of r.conversations?.newChannels || []) {
    push('new-channel', `${c}|new-channel`, { channel: c });
  }

  for (const x of r.conversations?.renamed || []) {
    push('renamed', `${x.from}|renamed|${x.to}`, { channel: x.from, detail: `${x.from} → ${x.to}` });
  }

  for (const c of r.conversations?.channels || []) {
    if (c.undeclaredPrivate) {
      push('undeclared-private', `${c.channel}|undeclared-private`, { channel: c.channel });
    }
    /* 채널별 반영 실패(스레드 덧붙이기 실패 등). 전에는 DM 「❌ 실패」 절 글자로만
     * 나가서 상황판(`board.py`)도 `archive-inbox` 도 못 봤다 — 2026-09-18 에
     * 어느 사업장 채널의 스레드 덧붙이기 실패가 래치로 매일 재시도·재실패하며 DM 에만
     * 나오다, 사람이 DM 을 직접 보고서야 손으로 붙였다. id 의 해시 덕에 같은 실패는
     * 이월돼 「며칠째」가 쌓인다. 종결은 다른 비요약 항목과 같이 「빼」다 — 그래서
     * **같은 문구의 실패가 나중에 재발하면 목록으로 안 돌아온다**(해시가 같아 계속
     * 빠진다). 그때도 DM 「❌ 실패」 절에는 나오므로 이 항목을 넣기 전보다 나빠지는
     * 자리는 없고, 그 재발의 뿌리(같은-분 중복)는 원장의 「수집 래치」 건이 따로 든다. */
    if (c.error) {
      push('error', `${c.channel}|error|${h16(c.error)}`, { channel: c.channel, detail: c.error });
    }
    for (const n of c.notes || []) {
      // report.js 와 같은 제외 — 시스템·봇 메시지 건너뜀은 알릴 것이 아니다
      if (n.includes('시스템·봇 메시지')) continue;
      push('note', `${c.channel}|note|${h16(n)}`, { channel: c.channel, detail: n });
    }
  }

  return out;
}

/**
 * 지난 목록과 이번 것을 합친다.
 *
 * **아무 항목도 「다시 안 잡혔다」는 이유로 치우지 않는다.** 종결은 사람의 결정 하나뿐이고
 * (`suppress` 가 거른다), 여기서는 이월과 갱신만 한다.
 *
 * 전에는 요약 계열만 달랐다 — 대조한 회차에서 다시 안 잡히면 「사람이 고쳐서 해소됐다」로
 * 읽고 치웠다. **그 전제가 틀렸다.** 대조에 들어가는 원문은 그 회차에 **새로 들어온
 * 메시지뿐**이라(`index.js` 의 touched → `summary.js` 의 `checkSummaries` 가 `t.transcript`
 * 를 넘긴다), 지난 회차의 후보가 거기서 다시 잡힐 이유가 애초에 없다. 그러니 그 판정이
 * 실제로 잰 것은 「해소됐나」가 아니라 **「그 채널에 아무 메시지나 들어왔나」**였다 —
 * 상관없는 한 줄만 들어와도 미결 요약 항목이 통째로 사라졌다. 요약은 틀린 채 봇
 * 프롬프트에 실려 남고 목록에서만 없어진다. 에러는 안 난다.
 *
 * 2026-09-04 에 이 판정을 회차 단위에서 채널 단위로 좁혔지만 구멍은 채널 안에 그대로
 * 남아 있었고, 2026-09-07 에 WHK 가 그것을 짚어 걷어냈다. 그때 실물은 아직 안 터진
 * 잠복 상태였다 — 항목이 걸린 두 채널에 09-04 이후 메시지가 안 들어왔다.
 *
 * 그 대신 **「반영」이 저장소에 남는다**(`.sync-state.json` 의 `applied`). 자동 치움이
 * 메우던 자리가 그것이다 — 안 옮기면 반영한 항목이 영영 안 사라진다.
 */
export function mergeItems({ prev = [], fresh = [], today, now }) {
  const before = new Map(prev.map((it) => [it.id, it]));
  const merged = new Map(prev.map((it) => [it.id, it]));
  for (const it of fresh) {
    merged.set(it.id, {
      ...it,
      firstSeen: before.get(it.id)?.firstSeen || today,
      /* **이번 회차에 실제로 검출된 것에만 찍는다.** 이월만 된 것은 앞 값을 그대로 둔다 —
       * 그래야 「반영」한 항목을 감출 때 목록 전체의 `generated` 가 아니라 **그 항목을
       * 마지막으로 다시 본 시각**과 견줄 수 있다 (`review_work.py` 의 `confirmed_at`,
       * 아래 `suppress` 의 `applied`). 목록 단위로 견주면 이월만 된 항목이 되살아난다 —
       * 07:00 회차는 대조를 했으므로 목록 도장을 정당하게 앞당기기 때문이다. */
      lastSeen: now,
    });
  }
  // 이월된 것 중 firstSeen 이 없는 것(옛 형식)에도 날짜를 준다 — 없으면 나이가 NaN 이 된다
  for (const [id, it] of merged) if (!it.firstSeen) merged.set(id, { ...it, firstSeen: today });
  return [...merged.values()];
}

/**
 * 「반영」이 그 항목을 **마지막으로 다시 본 시각보다 뒤**인가.
 *
 * **`review_work.py` 의 `applied_after` 와 같은 판정이어야 한다** — 이쪽은 VM 이 07:00 에
 * 목록을 다시 쓸 때 쓰고, 저쪽은 사람이 정한 직후 다음 07:00 전까지 화면에서 쓴다.
 * 갈리면 한쪽에서만 사라진다.
 *
 * **못 읽으면 감추지 않는다.** 시각이 깨졌거나 없을 때 감추는 쪽으로 물러서면 사람이
 * 영영 못 본다 — 보이는 쪽으로 틀린다 (저쪽도 같은 방향이다).
 */
const appliedAfter = (rec, at) => {
  const a = Date.parse(rec?.at ?? '');
  const b = Date.parse(at ?? '');
  return Number.isFinite(a) && Number.isFinite(b) && a > b;
};

/**
 * 사람이 이미 정한 것을 뺀다.
 *
 *   · `deferred`  — 만기 전이면 뺀다. 판정은 `util.js` 의 activeDeferred 한 곳
 *   · `dismissed` — 「이 후보는 틀렸다」. **그때의 요약 해시와 같을 때만** 뺀다
 *   · `applied`   — 「반영했다」. **그 뒤에 다시 잡히지 않았을 때만** 뺀다
 *
 * 뺀 것은 파일에서도 빠진다. 결정은 `.sync-state.json` 에 남아 있으므로 잃어버리지 않고,
 * 목록에 안 보이는 것이 「정해졌다」는 뜻이다.
 *
 * **셋이 다 저장소에 들어가는 것이 요점이다.** 「반영」만 `.decision-stamp.json`(로컬
 * 전용)에 있던 동안은 VM 이 그것을 못 봤고, 그 자리를 `mergeItems` 의 자동 치움이
 * 메우고 있었다 — 그 전제가 틀린 것이었으므로(위 `mergeItems`) 셋을 한자리에 모았다
 * (2026-09-07). 도장(`--show` 가 찍는 `files`)은 그대로 로컬이다: 그건 「이 내용을
 * 사람이 화면에서 봤다」는 커밋 관문의 값이라 아카이브 자료가 아니다.
 *
 * **이 판정을 부르는 곳이 둘이다** — 07:00 회차(여기서 파일을 다시 쓴다)와 09:00·16:00
 * 위생 점검(`archive-health.js` 의 `stalePendingWork`, 파일을 읽기만 한다). 뒤엣것이
 * 자기 나름대로 거르던 때는 **정한 시점부터 다음 07:00 까지 「빼」가 계속 세어졌다**
 * (2026-09-04). 판정은 이 함수 하나로 둔다.
 *
 * @param {number} [now]  만기 판정 기준 시각. 위생 점검이 자기 기준 시각을 넘긴다.
 * @param {string} [generated]  `lastSeen` 이 없는 옛 형식 항목이 물러설 값.
 */
export function suppress(items, state, now = Date.now(), generated = '') {
  const held = new Set(activeDeferred(state, now).map(([id]) => id));
  const dismissed = state?.dismissed || {};
  const applied = state?.applied || {};
  return items.filter((it) => {
    if (held.has(it.id)) return false;
    if (appliedAfter(applied[it.id], it.lastSeen || generated)) return false;
    const d = dismissed[it.id];
    if (!d) return true;
    /* 요약이 아닌 항목(새 채널·개명 등)에는 해시가 없다 — 되돌아올 근거가 없으니 계속 뺀다.
     *
     * **알려진 한계이고, 2026-09-22 에 고치려다 물렀다.** `error` 항목은 id 에 실패 문구
     * 해시가 들어가므로 같은 실패가 몇 달 뒤 재발해도 id 가 같아 **영영 목록으로 안
     * 돌아온다**(DM 의 「❌ 실패」 절에는 나온다). 그때 `firstSeen` 으로 「사라졌다 다시
     * 났나」를 재려 했는데 두 군데서 깨졌다.
     *   · **`firstSeen` 의 기억 장소가 `.pending-work.json` 하나인데 뺀 항목은 그 파일에서
     *     빠진다**(`runPendingWork` 가 거른 결과만 쓴다). 그래서 다음 회차엔 앞 기록이 없어
     *     그날로 새로 찍히고, 안 사라진 실패도 한 회차 뒤에 되살아난다 — 「빼」가 하루짜리가
     *     된다. 3회차 시뮬레이션으로 재현했다.
     *   · 화면 쪽 판정(`review_work.py` 의 `_still_dismissed`)은 그대로라, 되살아난 항목이
     *     상황판·위생점검에는 세어지는데 **화면에는 안 보여** 손댈 방법이 없어진다.
     *
     * 제대로 고치려면 **뺀 항목의 기억을 어디에 둘지**부터 정해야 하고(파일에 남기되 화면
     * 에서만 빼기 등) 두 언어의 판정을 함께 옮겨야 한다. 원장에 별건으로 있다. */
    if (d.summaryHash === null || d.summaryHash === undefined || d.summaryHash === '') return false;
    // 요약이 그때 그대로면 계속 뺀다. 고쳐졌으면 판정의 전제가 사라졌으니 돌려보낸다.
    return d.summaryHash !== summaryHash(it.file || it.channel);
  });
}

/**
 * 이 회차가 목록에 찍을 시각 도장.
 *
 * **대조를 안 한 회차는 앞 회차의 도장을 그대로 쓴다.** 도장은 "언제 파일을 썼나"가
 * 아니라 **"이 목록의 항목들을 언제 새 증거로 봤나"**라서다. `archive-inbox` 의
 * `review_work.py` 가 「반영」한 항목을 감출 때 **반영 시각 > `generated`** 로 판정하는데,
 * 안 보고 온 회차가 도장만 새로 찍으면 그 판정이 뒤집혀 **이미 끝낸 일이 되살아난다.**
 *
 * 2026-08-12 에 실제로 그랬다 — 12:51 목록의 요약 후보 4건을 12:58 에 반영했는데,
 * 17:00 회차(`skipSummary`)가 항목은 그대로 두고 `generated` 만 12:51 → 17:00 으로
 * 바꾸자(그 커밋의 이 파일 diff 는 한 줄이었다) 4건이 상황판에 다시 떴다. 에러는 안 난다.
 *
 * `.pending-edits.json` 은 같은 함정을 이미 밟고 고쳤다 — 그쪽은 파일 쓰기 전체가
 * `if (editsEnabled)` 안이라 대조를 안 한 회차는 파일도 도장도 안 건드린다
 * (`slack-archive.js` 의 `ingestConversations` 안 `PENDING_FILE` 쓰기). 여기는 요약이 아닌 항목(새 채널·파생값)이 그 회차에도
 * 새로 생길 수 있어 파일 쓰기 자체를 막을 수 없으므로, 도장만 묶는다.
 *
 * 앞 도장이 없으면 새로 찍는다 — **감추는 쪽으로 틀리지 않는다.** 같은 이유로
 * `applied_after` 도 못 읽으면 감추지 않는다.
 */
export function nextGenerated({ prev, summaryChecked, now }) {
  return !summaryChecked && prev ? prev : now;
}

/**
 * 파일에 반영하고 보고용 숫자를 돌려준다.
 *
 * @param {object} r        회차 결과 (index.js 의 `result`)
 * @param {object} opts     `{dry, today, summaryChecked}` — today 는 KST YYYY-MM-DD
 */
export function runPendingWork(r, { dry = false, today, summaryChecked = true } = {}) {
  const state = readJson(STATE_FILE) || {};
  const prevFile = readJson(WORK_FILE) || {};
  const prev = prevFile.items || [];
  // 회차 시각은 하나여야 한다 — 항목의 `lastSeen` 과 목록의 `generated` 를 같은 값으로 찍는다.
  const now = new Date().toISOString();
  const merged = mergeItems({ prev, fresh: buildItems(r), today, now });
  const items = suppress(merged, state, Date.now(), prevFile.generated || '');

  if (!dry) {
    // 남은 것이 없으면 파일을 지운다 — 빈 목록을 남겨 두면 "볼 것이 있다"로 읽힌다
    // (`.pending-edits.json` 과 같은 규칙, `slack-archive.js` 의 `ingestConversations` 안 `PENDING_FILE` 쓰기).
    if (items.length) {
      const generated = nextGenerated({ prev: prevFile.generated, summaryChecked, now });
      writeJson(WORK_FILE, { generated, items });
    } else if (fs.existsSync(WORK_FILE)) fs.unlinkSync(WORK_FILE);
  }

  const age = (it) => Math.round((Date.parse(today) - Date.parse(String(it.firstSeen))) / 86400000);
  const ages = items.map(age).filter(Number.isFinite);
  const kept = new Set(items);

  return {
    file: WORK_FILE,
    total: items.length,
    fresh: items.filter((it) => it.firstSeen === today).length,
    carried: items.filter((it) => it.firstSeen !== today).length,
    oldestDays: ages.length ? Math.max(...ages) : 0,
    /* 사람이 「뺌·나중에」로 정한 요약 후보의 id. DM 의 📌 블록에서도 빼려고 index.js 가
     * 쓴다 — 안 빼면 「미뤄 두면 만기까지 조용합니다」라고 안내해 놓고 다음 아침에 같은
     * 건의 이름을 다시 부르게 된다 (2026-08-10 에 수정·삭제 쪽에서 실제로 그랬다). */
    suppressedFindings: new Set(merged.filter((x) => !kept.has(x)).map((x) => x.id)),
  };
}
