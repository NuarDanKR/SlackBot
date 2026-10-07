#!/usr/bin/env node
/**
 * 두 언어에 나뉘어 있는 **같은 판정**이 실제로 같은 답을 내나.
 *
 *   node scripts/check-shared-rules.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * 이 파이프라인은 JS(봇·위생 점검)와 파이썬(스킬 스크립트) 둘로 갈려 있어서, 같은 판정을
 * 두 곳에 둘 수밖에 없는 자리가 있다. 지금까지 그것을 **주석으로만** 지켰다 —
 * 「여기와 저기는 같은 판정이어야 한다」가 네 군데 적혀 있었는데, 2026-08-12 감사에서
 * **그중 셋이 사실이 아니었다**:
 *
 *   · `isBotMessage`  — `archive-health.js` 가 내용을 베껴 써서 `selfId` 를 안 봤고,
 *                       `index.js` 는 아예 다른 규칙이라 첨부가 딸린 DM 질문을 버렸다
 *   · `excluded`      — 한쪽은 파일 ID, 한쪽은 이름이라 같은 자료를 다시 올리면 갈렸다
 *   · `board-docs.js` — 「모양이 바뀌면 board.py 의 시험이 잡는다」고 적었지만 그 시험이 없었다
 *
 * 갈려도 **에러가 안 난다.** 한쪽은 「N건 남음」, 한쪽은 「0건」이라고 말할 뿐이다.
 * 그래서 주석 대신 **두 구현에 같은 입력을 먹여 결과를 대조하는 검사**를 둔다.
 *
 * **슬랙에 붙지 않는다.** 판정 함수만 부른다.
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { isBotMessage } from '../src/slack-live.js';
import { fold as jsFold, needles as jsNeedles, quotesIn as jsQuotes } from '../src/ingest/summary.js';
import {
  nameKey as jsNameKey, docFilters, classifyDoc,
  DOC_EXTS as jsDocExts, PREFER as jsPrefer,
} from '../src/archive-health.js';
import { archiveChannelNames as jsArchiveNames } from '../src/archive.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// 코드 저장소 뿌리. 이 파일은 <뿌리>/scripts/ 에 있고 스킬은 <뿌리>/.claude/skills/ 에 있다.
// 전에는 `../../..`(워크스페이스)였다 — 저장소가 갈리면서 한 단계가 됐다.
// **어긋나면 판정이 갈렸다고 알리는 게 아니라 `ModuleNotFoundError` 로 죽는다.**
// 즉 이 검사가 「JS 와 파이썬이 갈렸나」를 아예 못 보는 상태가 된다.
const ROOT = path.resolve(HERE, '..');
const SRC_DIR = path.join(ROOT, 'src');
const PY_DIR = path.join(ROOT, '.claude/skills/doc-archive/scripts');
const INBOX_PY_DIR = path.join(ROOT, '.claude/skills/archive-inbox/scripts');

let ok = true;
const fail = (msg) => { ok = false; console.error(`✗ ${msg}`); };

/* ── ① isBotMessage ─────────────────────────────────────────────────────
 * JS 는 `slack-live.js`, 파이썬은 `fetch_slack_files.is_bot_message`.
 * 파이썬은 `selfId` 를 안 보므로(봇 토큰으로 올린 글에는 `bot_id` 가 늘 붙는다)
 * 그 인자를 안 주는 경우로만 대조한다. */
const BOT_CASES = [
  { label: '봇 메시지 (bot_id)', m: { bot_id: 'B1', text: 'x' } },
  { label: '봇 메시지 (subtype)', m: { subtype: 'bot_message', text: 'x' } },
  { label: '사람 메시지', m: { user: 'U1', text: 'x' } },
  { label: '앱이 사람 이름으로 올린 글 (app_id 만)', m: { user: 'U1', app_id: 'A1', text: 'x' } },
  { label: '첨부가 딸린 사람 메시지', m: { user: 'U1', subtype: 'file_share', text: 'x' } },
  { label: '참여 로그', m: { user: 'U1', subtype: 'channel_join' } },
  { label: '빈 것', m: {} },
];

/* `python` 이라는 이름이 없는 곳이 있다 — 리눅스(VM)에는 `python3` 만 깔린 경우가 흔하다.
 * 2026-08-13 부터 이 검사가 `npm run check` 에 묶여 VM 에서도 도므로, 이름 하나만 보면
 * 거기서는 늘 「파이썬 쪽을 부르지 못했습니다」로 끝난다 — 닫히는 방향이지만 그 실패는
 * 진짜 어긋남과 구별되지 않아서, 곧 이 검사 전체를 안 읽게 된다.
 * 훅(`.githooks/pre-commit`)과 같은 순서로 되짚는다. */
const PY_NAMES = process.env.PYTHON ? [process.env.PYTHON] : ['python', 'python3'];
const PY_SNIPPET = ['-c', [
  'import json,sys',
  `sys.path.insert(0, ${JSON.stringify(PY_DIR)})`,
  'from fetch_slack_files import is_bot_message',
  'print(json.dumps([is_bot_message(m) for m in json.load(sys.stdin)]))',
].join('\n')];

let py = null;
let pyErr = '';
for (const name of PY_NAMES) {
  try {
    py = JSON.parse(execFileSync(name, PY_SNIPPET, {
      input: JSON.stringify(BOT_CASES.map((c) => c.m)), encoding: 'utf-8',
    }));
    break;
  } catch (err) {
    pyErr = `${name}: ${err.message.split('\n')[0]}`;
  }
}
if (!py) {
  // **조용히 넘어가지 않는다.** 대조를 못 한 것은 「같다」가 아니다.
  fail(`파이썬 쪽 판정을 부르지 못했습니다 — ${pyErr}`);
}

if (py) {
  BOT_CASES.forEach((c, i) => {
    const js = isBotMessage(c.m);
    if (js !== py[i]) fail(`isBotMessage 가 갈립니다 — ${c.label}: JS ${js} · 파이썬 ${py[i]}`);
  });
  // 첨부가 딸린 사람 메시지를 봇으로 보면 DM 질문이 버려진다 (2026-08-12 의 그 사고).
  if (isBotMessage({ user: 'U1', subtype: 'file_share' })) {
    fail('첨부가 딸린 사람 메시지를 봇으로 봅니다 — DM 질문이 버려집니다.');
  }
  /* 위 대조는 **양쪽이 똑같이 틀려도 통과한다.** `app_id` 를 두 언어에서 함께 빼면 둘 다
   * `false` 라 어긋남이 없기 때문이다. 그래서 정책 자체를 여기서 한 번 더 못박는다.
   * 앱이 사람 이름으로 올린 글에는 `bot_id` 도 `bot_message` 도 없고 `app_id` 만 붙는다 —
   * 그것을 사람 글로 보면 **AI 가 만든 요약이 사람 원문으로** 아카이브에 들어간다
   * (WHK 결정 2026-08-26). */
  if (!isBotMessage({ user: 'U1', app_id: 'A1' })) {
    fail('앱이 사람 이름으로 올린 글을 사람으로 봅니다 — AI 요약이 원문으로 아카이브에 들어갑니다.');
  }
}

/* ── ①-b 근거 대조의 정규화가 두 언어에서 같나 ──────────────────────────
 * 이쪽(`summary.js` 의 fold)은 **관문**이다 — 근거가 원문에 없으면 항목을 아예 안 내보낸다.
 * 저쪽(`archive-inbox/scripts/review_work.py` 의 `_fold`)은 그 항목의 원문을 사람에게
 * 찾아 준다. 관문이 더 너그러우면 **통과시킨 항목을 화면이 「원문에서 찾지 못했습니다」**로
 * 보여주고, 스킬 문서는 그 문구를 「대개 빼」로 읽으라고 한다 — 멀쩡한 근거가 버려진다.
 * 2026-08-13 까지 파이썬 쪽은 공백만 지웠고, 아카이브 전량 실측으로 백틱 440건 ·
 * 별표 141건 · 따옴표 25건이 그 상태였다.
 *
 * **에러가 안 나는 종류라 이렇게 대보는 것 말고 알아챌 방법이 없다.** */
const FOLD_CASES = [
  '가 나  다',                       // 공백
  '*강조* `코드` _밑줄_ ~물결~',      // 강조 기호
  '"큰따옴표" \'작은따옴표\'',        // 곧은 따옴표 — 양쪽 다 지운다
  '“둥근따옴표”',                    // 둥근 것은 양쪽 다 남긴다
  'Tr.A 318억 상환',                 // 소문자화
  '• 항목 ◦ 하위 ∙ 가운데',           // 글머리표 — 양쪽 다 지운다 (2026-08-20)
  '가·나 다·라',                     // 가운뎃점은 양쪽 다 남긴다 — 글머리표가 아니라 문장 구분자
  // NBSP(U+00A0) — 슬랙 복사-붙여넣기에 섞이는 공백. `\s` 를 ASCII 로 좁히는 어긋남은
  // 이 줄만 가른다 (전 문장은 ASCII 공백뿐이라 첫 줄이 잡는 것 외에 잡는 게 0이었다 — 2026-09-05).
  '만기\u00a0연장 요청시에는 취급수수료 중 일부는',
  '회신에 \\"직접 수령\\" 이라고 적혀 있었다',   // 백슬래시 이스케이프 따옴표 — 다섯째 원인
  '',
];

/* **바이트로 주고받는다.** 윈도우 파이썬은 stdin 을 cp949 로 읽어서, 한글이 든 입력을
 * `json.load(sys.stdin)` 로 받으면 글자가 깨진 채 비교돼 **늘 「갈립니다」가 뜬다.**
 * 위 봇 판정 검사가 이 문제를 안 겪은 것은 불리언만 오갔기 때문이다.
 * 내보낼 때도 `ensure_ascii` 로 아스키만 내보내 stdout 인코딩에 기대지 않는다. */
const PY_FOLD_SNIPPET = ['-c', [
  'import json,sys',
  `sys.path.insert(0, ${JSON.stringify(INBOX_PY_DIR)})`,
  'from review_work import _fold, _needles, QUOTE_RE',
  "data = json.loads(sys.stdin.buffer.read().decode('utf-8'))",
  'out = {"fold": [_fold(s) for s in data["fold"]],',
  '       "needles": [_needles(s) for s in data["needles"]],',
  '       "quotes": [QUOTE_RE.findall(s) for s in data["quotes"]]}',
  "sys.stdout.buffer.write(json.dumps(out).encode('ascii'))",
].join('\n')];

/* ── ①-d 근거에서 **인용을 뽑는 문자군**이 두 언어에서 같나 ──────────────
 * `summary.js` 의 `quotesIn` ↔ `review_work.py` 의 `QUOTE_RE`. 위 fold·needles 와 같은
 * 관문/화면 짝이고, 여기서 하나도 못 뽑으면 관문이 그 항목을 `근거에 원문 인용이 없음` 으로
 * 통째로 버린다 — 인용이 원문에 멀쩡히 있어도 그렇다.
 *
 * **2026-08-31 감사에서 이미 갈려 있었다.** JS 쪽 문자군은 `["""']` 인데 저 큰따옴표 셋은
 * 전부 같은 글자(U+0022)라 실제로는 `["']` 다 — 넓어 보이는데 곧은 것만 본다. 파이썬 쪽은
 * 만들 때부터 `"\"“”‘’'"` 로 둥근 것까지 봤다. 방향은 **관문이 더 엄격한** 쪽이라
 * 화면이 찾아낼 수 있는 근거를 관문이 먼저 버린다.
 *
 * **가르는 입력이 곧 둥근 따옴표다.** 곧은 것만 넣으면 갈려도 검사가 통과한다 —
 * 이 파일이 needles 에서 이미 한 번 겪은 실수다(위 NEEDLE_CASES 주석). */
const QUOTE_CASES = [
  '원문에 “여덟자가넘는인용문” 이라고 적혀 있다',   // 둥근 큰따옴표 — 이 쌍을 가른다
  "원문에 ‘여덟자가넘는인용문’ 이라고 적혀 있다",   // 둥근 작은따옴표 — 이것도 가른다
  '원문에 "여덟자가넘는인용문" 이라고 적혀 있다',   // 곧은 큰따옴표 — 양쪽 다 뽑아야 한다
  "원문에 '여덟자가넘는인용문' 이라고 적혀 있다",   // 곧은 작은따옴표
  '“앞의인용문입니다” 그리고 “뒤의인용문입니다”',   // 한 문장에 둘
  '"짧다" 는 여덟자가 안 되어 안 뽑힌다',           // 최소 길이 8 을 가른다
  '따옴표가 아예 없는 근거 문장이다',               // 둘 다 빈 배열
  '',
];

/* 위 fold 와 같은 이유로 **조각 내는 방식도** 두 언어에 하나씩 있다 (`summary.js` 의
 * `needles` / `review_work.py` 의 `_needles`). 이쪽이 더 엄격하면 **관문이 버린 항목을
 * 화면은 찾아내는** 반대 방향의 어긋남이 된다 — 2026-08-13 에 화면만 고쳐 실제로 그랬고,
 * 그날 회차의 실제 인용으로 재현됐다. 이것도 에러가 안 나는 종류다. */
/* **입력이 경계를 갈라야 검사가 뜻이 있다.** 처음 쓴 입력은 짧은 조각이 2자여서
 * 최소 길이를 8 → 4 로 바꿔도 양쪽 다 버려 **검사가 통과했다.** 아래 세 값(최소 길이 8 ·
 * 자르기 30 · 생략부호는 점 셋부터)을 하나씩 어긋내면 각각 잡히는 것을 확인하고 넣었다.
 *
 * **2026-08-31 감사에서 그 확인이 반쪽이었던 것이 드러났다.** 올리는 방향(8 → 9)만 잡히고
 * **낮추는 방향은 8 → 7·6·5·0 어디로 바꿔도 통과했다.** 가장 짧은 조각이 `다섯자다요`(5자)
 * 뿐이었는데 5자는 접어도 5자라 뒤의 `MIN_FOLDED`(6)가 어차피 버려서, 앞 필터를 아무리
 * 낮춰도 결과가 안 바뀌었기 때문이다. 처음 겪은 것과 **같은 모양의 실수**다.
 *
 * 그래서 아래 두 줄을 넣었다 — 7자 조각(접으면 6자)이 `8 → 7` 을, 6자 조각이 `7 → 6` 을
 * 가른다. 넣고 나서 **한쪽만 8 → 9·7·6·5·4·0 으로 바꿔 여섯 번 다 빨개지는 것을 봤다.**
 *
 * 두 가지는 여전히 못 잡는다. 적어 두지 않으면 다음 사람이 「다 확인됐다」로 읽는다:
 *   · **6 아래끼리는 구별되지 않는다.** `fold` 는 글자를 지우기만 해서 6자 미만 조각은
 *     접어도 6자가 안 되고 `MIN_FOLDED`(6)가 전부 버린다 — 실측으로 6·5·4 의 결과가
 *     글자까지 같았다. 그 자리는 `MIN_FOLDED` 쪽 검사가 대신 지킨다.
 *   · **양쪽을 함께 바꾸면 조용하다** (실측: 둘 다 7·6·5 로 바꾸면 통과). 이건 설계다 —
 *     이 검사가 보는 것은 「값이 옳은가」가 아니라 「두 언어가 갈렸나」다. */
/* **원문은 가짜다** — 이 저장소는 팀끼리 나눠 쓰므로 실제 팀 메시지를 픽스처에 두지
 * 않는다(WHK 결정 2026-09-04). 아래에서 뜻이 있는 것은 글자가 아니라 **길이와 기호**라
 * 각 줄 뒤에 무엇을 가르는지 적어 둔다. `check-pending-work.js` 의 TR·TR_BULLET 과 같은
 * 가짜 문장에서 떼어 왔다 — 접은 뒤 길이(6·27·35)는 옛 실물과 같다. */
const NEEDLE_CASES = [
  '명의의 회신을 확인했고 ... 그러면 절차 끌지 말고',   // 생략 인용 (조각 둘 다 8자 이상)
  '앞부분도 여덟자가 넘는다…뒷부분도 여덟자가 넘는다',   // 홑 생략부호
  '다섯자다요...뒷부분은 여덟자가 넘는다',             // 5자 조각 — 최소 길이 8 을 가른다
  '일곱자 입니다...뒷부분은 여덟자가 넘는다',          // 7자 조각(접으면 6자) — 8 → 7 을 가른다
  '여섯자입니다...뒷부분은 여덟자가 넘는다',           // 6자 조각(접어도 6자) — 7 → 6 을 가른다
  '앞부분 여덟자이상..뒷부분',                          // 점 둘은 생략부호가 아니다
  '약정 체결 업무',                                    // 8자는 접기 **전**에 센다 (접으면 6자 — 바닥에 딱 걸린다)
  '<면담과 별도 내용> • 커뮤니티 위탁관리계약에 따라 갑PFV는',  // 글머리표가 30자 자르기 전에 빠지나
  '**회신을** 확인...뒷부분은 여덟자가 넘는다',        // 앞 조각 원본 10자 → 접으면 5자 — 바닥 6 을 가른다
  '만기 연장 요청시에는 취급수수료 중 일부는 감면하고 접수하는 것이 내부 여신지침과',  // 접어서 35자 — 30 자르기를 가른다
  '대출 잔액 약 40억 감소 (900억 → 860억) / 실수금 약 7억이며',  // ` / ` 이음매 — 조각 둘 다 8자 이상
  '실수금 약 7억이며 채권/채무 상계는 별건으로',   // 붙여 쓴 / 는 원문의 글자라 안 쪼갠다
  '준공일은 10/23 이고 상장예정일은 11/30 이다',        // 날짜의 / 도 안 쪼갠다
  '회신에 \\"직접 수령\\" 이라고 적혀 있었다',   // 백슬래시 이스케이프 따옴표 — 다섯째 원인
  '',
];

let pyFold = null;
let pyFoldErr = '';
for (const name of PY_NAMES) {
  try {
    pyFold = JSON.parse(execFileSync(name, PY_FOLD_SNIPPET, {
      input: JSON.stringify({ fold: FOLD_CASES, needles: NEEDLE_CASES, quotes: QUOTE_CASES }),
      encoding: 'utf-8',
    }));
    break;
  } catch (err) {
    pyFoldErr = `${name}: ${err.message.split('\n')[0]}`;
  }
}
if (!pyFold) {
  // 대조를 못 한 것은 「같다」가 아니다 (위 ① 과 같은 이유).
  fail(`파이썬 쪽 fold 를 부르지 못했습니다 — ${pyFoldErr}`);
} else {
  FOLD_CASES.forEach((s, i) => {
    const js = jsFold(s);
    if (js !== pyFold.fold[i]) {
      fail(`근거 대조 정규화가 갈립니다 — ${JSON.stringify(s)}: JS ${JSON.stringify(js)}`
        + ` · 파이썬 ${JSON.stringify(pyFold.fold[i])}`);
    }
  });
  NEEDLE_CASES.forEach((s, i) => {
    const js = JSON.stringify(jsNeedles(s));
    const py = JSON.stringify(pyFold.needles[i]);
    if (js !== py) {
      fail(`인용 조각 내는 방식이 갈립니다 — ${JSON.stringify(s)}: JS ${js} · 파이썬 ${py}`);
    }
  });
  QUOTE_CASES.forEach((s, i) => {
    const js = JSON.stringify(jsQuotes(s));
    const py = JSON.stringify(pyFold.quotes[i]);
    if (js !== py) {
      fail(`근거에서 인용 뽑는 문자군이 갈립니다 — ${JSON.stringify(s)}: JS ${js} · 파이썬 ${py}`);
    }
  });
}

/* ── ①-c 같은 자료의 다른 포맷 판을 알아보는 키가 두 언어에서 같나 ────────
 *
 * `archive-health.js` 의 `nameKey` 와 `fetch_slack_files.py` 의 `name_key` 다.
 * 파이썬 쪽 머리말이 2026-08-04 부터 「같은 판정이어야 한다」고 적고 있었는데
 * **지키는 것이 그 주석뿐이었다** — 이 파일 머리말이 말하는 바로 그 모양이다.
 *
 * 실제로 갈려 있었다 (2026-08-15 실측): 파이썬이 `Path(name).stem` 을 써서 이름을
 * **경로로** 읽었다. `dir/file.pdf` 가 `file` 이 되고, 더 나쁘게는 **답이 기계마다
 * 달랐다** — `C:file.pdf` 가 WindowsPath 에서 `file`, PosixPath 에서 `C:file` 이다.
 * 수집은 사람 PC(윈도우)에서 돌고 위생 점검은 VM(리눅스)에서 돈다.
 * 그때 실제 파일명 471건으로는 갈림이 0 이라 **잠복해 있었다.**
 *
 * 갈리면 에러가 안 나고 한쪽은 「N건 남음」, 한쪽은 「0건」이라고 말한다.
 *
 * **입력이 경계를 갈라야 한다** (위 needles 와 같은 이유). 아래에서 경로 구분자를 뺀
 * 채로는 `Path` 판과 지금 판이 같은 답을 내서 검사가 그대로 통과한다. */
const NAMEKEY_CASES = [
  ['사업장나', '260812_잔금수금 업무보고(VAT별도).pdf'],  // 평범한 것
  ['ch', 'dir/file.pdf'],        // 슬래시 — Path 판은 앞을 버린다
  ['ch', 'dir\\file.pdf'],       // 역슬래시 — 윈도우에서만 버린다 (기계마다 다른 답)
  ['ch', 'C:file.pdf'],          // 드라이브 접두사 — 윈도우에서만 버린다
  ['ch', 'a.b.pdf'],             // 점이 둘
  ['ch', '.hidden'],             // 맨 앞 점은 확장자가 아니다
  ['ch', 'name.'],               // 끝이 점
  ['ch', 'noext'],
  ['ch', 'STRASSE.PDF'],         // 소문자화
  ['ch', '보고서 (사내).hwp'],
  ['', ''],
];

const PY_NAMEKEY_SNIPPET = ['-c', [
  'import json,sys',
  `sys.path.insert(0, ${JSON.stringify(PY_DIR)})`,
  'from fetch_slack_files import name_key',
  "data = json.loads(sys.stdin.buffer.read().decode('utf-8'))",
  "sys.stdout.buffer.write(json.dumps([name_key(c, n) for c, n in data]).encode('ascii'))",
].join('\n')];

let pyNameKey = null;
let pyNameKeyErr = '';
for (const name of PY_NAMES) {
  try {
    pyNameKey = JSON.parse(execFileSync(name, PY_NAMEKEY_SNIPPET, {
      input: JSON.stringify(NAMEKEY_CASES), encoding: 'utf-8',
    }));
    break;
  } catch (err) {
    pyNameKeyErr = `${name}: ${err.message.split('\n')[0]}`;
  }
}
if (!pyNameKey) {
  fail(`파이썬 쪽 name_key 를 부르지 못했습니다 — ${pyNameKeyErr}`);
} else {
  NAMEKEY_CASES.forEach(([c, n], i) => {
    const js = jsNameKey(c, n);
    if (js !== pyNameKey[i]) {
      fail(`같은 자료 판정 키가 갈립니다 — ${JSON.stringify(n)}: JS ${JSON.stringify(js)}`
        + ` · 파이썬 ${JSON.stringify(pyNameKey[i])}`);
    }
  });
}

/* ── ①-d 개명 지도 (사업장 이름) ────────────────────────────────────────
 * `archive.js` 의 `archiveChannelNames` 와 `fetch_slack_files.py` 의
 * `archive_channel_names` 다. 채널을 개명하면 대화 md 파일명은 안 바뀌므로 아카이브가
 * 아는 이름과 슬랙의 현재 이름이 갈리는데, 두 구현이 그 되짚기를 다르게 하면
 * **에러 없이** 갈래가 둘로 벌어진다 — 한쪽은 옛 사업장에 넣고 한쪽은 새 이름으로
 * 미변환을 센다. 2026-09-16 에 그 어긋남으로 사업장 폴더가 두 벌이 될 뻔했다.
 *
 * **입력이 경계를 갈라야 한다** — `file` 이 없는 옛 기록, `#` 접두, 빈 이름, 그리고
 * 개명 안 한 평범한 채널(항등이어야 한다)을 함께 먹인다. */
const ALIAS_STATE = {
  channels: {
    C1: { name: 'ch-now', file: 'ch-was' },        // 개명된 것
    C2: { name: 'ch-same', file: 'ch-same' },      // 이름이 그대로
    C3: { name: 'ch-old-record' },                 // `file` 칸이 생기기 전 기록
    C4: { name: '#ch-hash', file: '#ch-hash-was' },// 접두 `#` 는 떼고 본다
    C5: { name: '' },                              // 빈 이름은 지도에 안 넣는다
    C6: 'not-an-object',                           // 모양이 깨진 줄은 건너뛴다
  },
};
const ALIAS_LOOKUPS = ['ch-now', 'ch-same', 'ch-old-record', 'ch-hash', '없는채널', ''];

const PY_ALIAS_SNIPPET = ['-c', [
  'import json,sys',
  `sys.path.insert(0, ${JSON.stringify(PY_DIR)})`,
  'from fetch_slack_files import archive_channel_names, archive_channel',
  "data = json.loads(sys.stdin.buffer.read().decode('utf-8'))",
  'amap = archive_channel_names(data["state"])',
  'out = {"map": amap, "lookups": [archive_channel(n, amap) for n in data["lookups"]]}',
  "sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode('utf-8'))",
].join('\n')];

let pyAlias = null;
let pyAliasErr = '';
for (const name of PY_NAMES) {
  try {
    pyAlias = JSON.parse(execFileSync(name, PY_ALIAS_SNIPPET, {
      input: JSON.stringify({ state: ALIAS_STATE, lookups: ALIAS_LOOKUPS }), encoding: 'utf-8',
    }));
    break;
  } catch (err) {
    pyAliasErr = `${name}: ${err.message.split('\n')[0]}`;
  }
}
if (!pyAlias) {
  fail(`파이썬 쪽 archive_channel_names 를 부르지 못했습니다 — ${pyAliasErr}`);
} else {
  const js = jsArchiveNames(Object.values(ALIAS_STATE.channels)
    .filter((v) => v && typeof v === 'object'));
  const jsMap = Object.fromEntries(js);
  const pyMap = pyAlias.map;
  const keys = new Set([...Object.keys(jsMap), ...Object.keys(pyMap)]);
  for (const k of keys) {
    if (jsMap[k] !== pyMap[k]) {
      fail(`개명 지도가 갈립니다 — ${JSON.stringify(k)}: JS ${JSON.stringify(jsMap[k])}`
        + ` · 파이썬 ${JSON.stringify(pyMap[k])}`);
    }
  }
  ALIAS_LOOKUPS.forEach((n, i) => {
    const got = jsMap[n] ?? n;
    if (got !== pyAlias.lookups[i]) {
      fail(`개명 되짚기가 갈립니다 — ${JSON.stringify(n)}: JS ${JSON.stringify(got)}`
        + ` · 파이썬 ${JSON.stringify(pyAlias.lookups[i])}`);
    }
  });
}

/* ── ①-e 비공개 선언 대조 (개명 되짚기 포함) ────────────────────────────
 * JS 는 `config.js` 의 `isPrivateChannel`, 파이썬은 `fetch_slack_files.is_declared_private`.
 * 둘이 갈리면 **에러가 안 난다** — 한쪽은 내려받고 한쪽은 거부할 뿐이다. 내려받는 쪽으로
 * 갈리면 비공개 채널 문서가 공개로 아카이브되고(2026-08-05 사고), 거부하는 쪽으로
 * 갈리면 그 채널 자료가 조용히 안 쌓인다.
 *
 * **개명 케이스를 반드시 먹인다.** config 에 옛 이름을 적어 둔 비공개 채널이 개명되는
 * 순간이 정확히 갈리는 자리다. */
const PRIV_STATE = { channels: { C1: { name: 'pc-now', file: 'pc-was' } } };
// [대보는 이름, config 에 적힌 철자, 기대]
const PRIV_CASES = [
  ['현재 이름 · 현재 철자', 'pc-now', 'pc-now', true],
  ['현재 이름 · 옛 철자', 'pc-now', 'pc-was', true],
  ['옛 이름 · 현재 철자', 'pc-was', 'pc-now', true],
  ['옛 이름 · 옛 철자', 'pc-was', 'pc-was', true],
  ['관계 없는 채널', 'pc-other', 'pc-now', false],
  ['`#` 접두는 떼고 본다', '#pc-now', 'pc-now', true],
  // JS `normalizeChannel` 은 선행 `#` 을 **한 글자만** 뗀다. 파이썬이 `lstrip("#")` 로
  // 전부 떼면 여기서 갈린다 (2026-09-16 검토가 실측으로 잡았다).
  // 이름·config 철자 둘 다 한 글자만 떼면 `#pc-now` vs `pc-now` 라 안 갈린다(원한 것 false).
  // 파이썬이 `lstrip("#")` 로 전부 떼면 둘 다 `pc-now` 가 되어 true 로 갈린다 — 그 회귀를
  // 잡는 것이 이 케이스의 목적이라 want 는 true 가 아니라 false 다(2026-09-16 실측으로 고침).
  ['`#` 은 한 글자만 뗀다', '##pc-now', '#pc-now', false],
  ['`#` 을 전부 떼면 안 된다', '##pc-now', 'pc-now', false],
  ['빈 이름', '', 'pc-now', false],
];

// **몇 가지를 쟀는지부터 확인한다.** 뒤의 `pyPriv.length !== PRIV_CASES.length` 는 파이썬이
// 돌려준 개수와 **이 배열 자기 길이**를 대는 것이라, 표를 통째로 비워도(둘 다 0) 잡히지
// 않는다 — `check-channel-aliases.js` 의 `PROBE_PROPS` 와 같은 수법으로 독립된 상수와 댄다
// (2026-09-16 검토가 `PRIV_CASES = []` 실측으로 잡았다).
const PRIV_CASES_COUNT = 9;
if (PRIV_CASES.length !== PRIV_CASES_COUNT) {
  fail(`비공개 선언 케이스가 ${PRIV_CASES_COUNT}가지여야 하는데 ${PRIV_CASES.length}가지입니다`
    + ' — 표가 줄었을 수 있습니다.');
}

const PY_PRIV_SNIPPET = ['-c', [
  'import json,sys',
  `sys.path.insert(0, ${JSON.stringify(PY_DIR)})`,
  'from fetch_slack_files import current_channel_names, is_declared_private',
  "d = json.loads(sys.stdin.buffer.read().decode('utf-8'))",
  'cmap = current_channel_names(d["state"])',
  'out = [is_declared_private(n, {s}, cmap) for n, s in d["cases"]]',
  "sys.stdout.buffer.write(json.dumps(out).encode('utf-8'))",
].join('\n')];

let pyPriv = null;
let pyPrivErr = '';
for (const name of PY_NAMES) {
  try {
    pyPriv = JSON.parse(execFileSync(name, PY_PRIV_SNIPPET, {
      input: JSON.stringify({
        state: PRIV_STATE, cases: PRIV_CASES.map(([, n, s]) => [n, s]),
      }),
      encoding: 'utf-8',
    }));
    break;
  } catch (err) {
    pyPrivErr = `${name}: ${err.message.split('\n')[0]}`;
  }
}

// JS 쪽은 자식 프로세스에 임시 아카이브를 물려 부른다 — `config.js` 가 모듈 로드 때
// 경로를 굳히므로 이 프로세스 안에서는 합성 config 를 먹일 수 없다.
const PRIV_PROBE = `(async () => {
  const cfg = await import(${JSON.stringify(pathToFileURL(path.join(SRC_DIR, 'config.js')).href)});
  const cases = JSON.parse(process.env.PRIV_CASES);
  process.stdout.write(JSON.stringify(cases.map(([n]) => cfg.isPrivateChannel(n))));
})();`;

function jsPrivAnswers(spelled, names) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-priv-'));
  try {
    const write = (rel, text) => {
      const dest = path.resolve(tmp, rel);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, text, 'utf8');
    };
    write('slack-export/index.md', '# fixture\n');
    write('slack-export/.sync-state.json', JSON.stringify(PRIV_STATE));
    write('documents/index.md', '# fixture\n');
    write('config.json', JSON.stringify({
      timezone: 'UTC', workspace: 'fixture', archivePath: 'slack-export',
      documentsPath: 'documents', privateChannels: [spelled], digest: { skipChannels: [] },
      owner: { slackUserId: 'FIXTURE', name: 'Fixture' }, limits: {},
    }));
    return JSON.parse(execFileSync(process.execPath, ['-e', PRIV_PROBE], {
      encoding: 'utf-8',
      env: {
        ...process.env,
        // This probe owns a synthetic slack-export and rename map. Operational
        // pf-archiver variables would make it read the live Archiver instead,
        // so the test would compare different inputs across JS and Python.
        HERMES_MODE: 'pf',
        HERMES_DATA_ROOT: tmp,
        HERMES_DOCS_DIR: path.join(tmp, 'documents'),
        PRIV_CASES: JSON.stringify(names.map((n) => [n])),
      },
    }));
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

// ①-e-c(스스로 지키는지 재는 자기검사)가 **다시 계산하지 않고** 여기서 구한 답을 재사용할
// 수 있도록 바깥 스코프에 둔다 — 자식 프로세스를 또 띄우면 그만큼 느려진다(아래 참조).
let jsPriv = null;
if (!pyPriv) {
  fail(`파이썬 쪽 is_declared_private 를 부르지 못했습니다 — ${pyPrivErr}`);
} else if (pyPriv.length !== PRIV_CASES.length) {
  fail(`비공개 선언 대조를 ${PRIV_CASES.length}가지 물었는데 ${pyPriv.length}가지만 돌아왔습니다.`);
} else {
  // **같은 `privateChannels` 철자를 쓰는 케이스는 한 번에 묶어 부른다** — 케이스마다 자식
  // 프로세스를 새로 띄우면 이 관문이 느려진다. 정확성이 먼저라 철자가 갈리는 경계에서만
  // 나눈다: `spelled` 값으로 그룹을 짓고, 그룹 안의 `name` 을 한 번의 호출에 함께 먹인다.
  const bySpelled = new Map();
  PRIV_CASES.forEach(([, , spelled], i) => {
    if (!bySpelled.has(spelled)) bySpelled.set(spelled, []);
    bySpelled.get(spelled).push(i);
  });
  jsPriv = new Array(PRIV_CASES.length);
  let jsPrivBroke = false;
  for (const [spelled, idxs] of bySpelled) {
    const names = idxs.map((i) => PRIV_CASES[i][1]);
    let answers;
    try {
      answers = jsPrivAnswers(spelled, names);
    } catch (e) {
      fail(`JS 쪽 isPrivateChannel 을 재지 못했습니다 (철자 ${JSON.stringify(spelled)}): `
        + `${e.message.split('\n')[0]}`);
      jsPrivBroke = true;
      continue;
    }
    idxs.forEach((i, k) => { jsPriv[i] = answers[k]; });
  }
  if (!jsPrivBroke) {
    PRIV_CASES.forEach(([label, , , want], i) => {
      const js = jsPriv[i];
      if (js !== want) {
        fail(`JS 비공개 판정이 기대와 다릅니다 — ${label}: ${js} (원한 것 ${want})`);
      }
      if (pyPriv[i] !== want) {
        fail(`파이썬 비공개 판정이 기대와 다릅니다 — ${label}: ${pyPriv[i]} (원한 것 ${want})`);
      }
      if (js !== pyPriv[i]) {
        fail(`비공개 선언 대조가 두 언어에서 갈립니다 — ${label}: JS ${js} · 파이썬 ${pyPriv[i]}`);
      }
    });
  }
}

/* ── ①-e-b 파이썬 수집이 거부를 **실제로 쓰나** ─────────────────────────
 * 판정 함수가 있어도 `main()` 이 반환값을 안 받으면 아무 일도 안 일어난다 —
 * 이 결함이 정확히 그 모양이었다(`warn_private_mismatch` 의 반환값을 버렸다).
 *
 * **「받나」와 「쓰나」는 다른 질문이다.** `rejected` 를 받아만 두고 아무 데도 안 넘기면
 * 거부는 되는데 **아무도 모르는** 상태가 된다 — 이 항목의 첫 두 정규식은 「받나」만
 * 봤고, 그래서 `report_rejected_private(rejected, …)` 호출을 통째로 지워도(2026-09-16
 * 검토가 실측) 초록이었다. 아래 세 번째 정규식이 「쓰나」를 본다. */
const FETCH_PY = fs.readFileSync(path.join(PY_DIR, 'fetch_slack_files.py'), 'utf-8');
if (!/chans,\s*rejected\s*=\s*select_channels\(/.test(FETCH_PY)) {
  fail('fetch_slack_files.py 의 main 이 select_channels 의 거부 목록을 받지 않습니다'
    + ' — 판정이 있어도 내려받기가 안 막힙니다.');
}
if (/def\s+warn_private_mismatch\b/.test(FETCH_PY)) {
  fail('경고만 하던 옛 함수 warn_private_mismatch 가 남아 있습니다.');
}
/* **글자 매치는 호출이 아니다.** `report_rejected_private(rejected` 문자열 규칙은 세 번
 * 틀린 이유로 걸리거나 놓쳤다 — ① 매개변수 이름이 우연히 `rejected` 인 **정의 줄**에도
 * 걸리고(2026-09-16 1차 검토), ② **주석 처리**(`# report_rejected_private(...)`)나 **다른
 * 함수 독스트링**에 그 이름이 든 문장만 남아도 걸리고(2026-09-16 2차 검토), ③ 멀쩡한 호출을
 * **여러 줄로 나누면**(`report_rejected_private(\n    rejected, …)`) 못 잡는다(같은 2차
 * 검토). 정규식을 더 조여도 셋을 동시에 못 막는다 — **파이썬 `ast` 로 `main()` 함수 본문
 * 안에 `Call(func=Name('report_rejected_private'))` 가 실제로 있는지** 본다. 주석은 파스
 * 트리에 안 잡히고, 독스트링은 `Call` 이 아닌 문자열이고, 줄바꿈은 `Call` 하나로 합쳐진다.
 * 이미 파이썬 자식 프로세스를 띄우는 항목이라 추가 비용이 사실상 없다. */
const PY_MAIN_CALL_SNIPPET = ['-c', [
  'import ast, json, sys',
  `src = open(${JSON.stringify(path.join(PY_DIR, 'fetch_slack_files.py'))}, encoding="utf-8").read()`,
  'tree = ast.parse(src)',
  'main = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"), None)',
  'def calls(node, name):',
  '    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name'
    + ' for n in ast.walk(node))',
  'out = {"has_main": main is not None,'
    + ' "calls_report_rejected": bool(main) and calls(main, "report_rejected_private")}',
  "sys.stdout.buffer.write(json.dumps(out).encode('utf-8'))",
].join('\n')];

let pyMainCall = null;
let pyMainCallErr = '';
for (const name of PY_NAMES) {
  try {
    pyMainCall = JSON.parse(execFileSync(name, PY_MAIN_CALL_SNIPPET, { encoding: 'utf-8' }));
    break;
  } catch (err) {
    pyMainCallErr = `${name}: ${err.message.split('\n')[0]}`;
  }
}
if (!pyMainCall) {
  // 파이썬을 못 부르는 환경이면 「같다」로 조용히 넘기지 않는다 (위 ① 과 같은 이유).
  fail(`fetch_slack_files.py 의 main 이 report_rejected_private 를 부르는지 재지 못했습니다`
    + ` — ${pyMainCallErr}`);
} else if (!pyMainCall.has_main) {
  fail('fetch_slack_files.py 에서 main() 함수를 찾지 못했습니다.');
} else if (!pyMainCall.calls_report_rejected) {
  fail('fetch_slack_files.py 의 main 이 거부 목록을 받고도 report_rejected_private 를'
    + ' 부르지 않습니다 — 거부는 되는데 아무도 모릅니다.');
}

/* ── ①-e-c 이 검사가 실제로 잡는가 (합성 입력) ─────────────────────────
 * 「오늘 돌렸더니 0건」은 검사가 맞다는 증거가 아니다. 되짚기를 뺀 가짜 판정을 먹여
 * 갈림이 실제로 잡히는지 본다. 파일은 건드리지 않는다.
 *
 * **위 PRIV_CASES 에서 이미 구한 답을 재사용한다.** 아래 두 케이스는 각각
 * `[name='pc-now', spelled='pc-was']`·`[name='pc-now', spelled='pc-now']` 로 위
 * PRIV_CASES 의 항목과 글자까지 같다 — 새 자식 프로세스를 또 띄우면 순수 재계산이라
 * 이 관문만 느려진다(2026-09-16 검토 실측: +45%). `jsPriv` 를 못 구했을 때만(그 그룹의
 * 호출이 실패했을 때만) 개별로 다시 띄운다. */
const FAKE_DECLARED = (name, spelled) => String(name || '').replace(/^#/, '').trim() === spelled;
const SELF_CASES = [
  ['되짚기 없는 판정은 「옛 철자」에서 갈려야 한다', 'pc-now', 'pc-was', true],
  ['되짚기 없는 판정도 「같은 철자」에서는 안 갈린다', 'pc-now', 'pc-now', false],
];
for (const [label, name, spelled, shouldDiffer] of SELF_CASES) {
  const reuseIdx = PRIV_CASES.findIndex(([, n, s]) => n === name && s === spelled);
  let js;
  if (jsPriv && reuseIdx !== -1 && jsPriv[reuseIdx] !== undefined) {
    js = jsPriv[reuseIdx];
  } else {
    try {
      [js] = jsPrivAnswers(spelled, [name]);
    } catch (e) {
      fail(`①-e-c 를 재지 못했습니다 (${label}): ${e.message.split('\n')[0]}`);
      continue;
    }
  }
  if ((js !== FAKE_DECLARED(name, spelled)) !== shouldDiffer) {
    fail(`①-e 가 스스로를 못 지킵니다 — ${label}`);
  }
}

// **다음 `def` 까지만** 이 함수 본문으로 본다 — 고정 글자 수 창은 한글 독스트링이 길면
// 코드가 멀쩡해도 창을 넘겨 틀린 이유로 빨개진다(2026-09-16 검토 실측: 실제 거리 315자,
// 옛 창 400자와 여유 85자뿐이었다).
const isDeclaredStart = FETCH_PY.search(/def\s+is_declared_private\b/);
let isDeclaredBody = '';
if (isDeclaredStart !== -1) {
  const rest = FETCH_PY.slice(isDeclaredStart);
  const nextDefRel = rest.slice(1).search(/\ndef\s/);
  isDeclaredBody = nextDefRel === -1 ? rest : rest.slice(0, nextDefRel + 1);
}
if (!/canon_channel\(/.test(isDeclaredBody)) {
  fail('파이썬 is_declared_private 가 canon_channel 로 되짚지 않습니다'
    + ' — config 에 옛 철자를 적은 비공개 채널의 문서가 조용히 안 쌓입니다.');
}

/* ── ② isBotMessage 를 베껴 쓴 곳이 없나 ────────────────────────────────
 * 같은 JS 안에서 판정을 손으로 옮겨 적으면 한쪽만 고쳤을 때 조용히 갈린다.
 * `archive-health.js` 가 실제로 그랬다.
 *
 * **2026-08-13 에 두 군데를 넓혔다** (리뷰 지적):
 *
 *   · 전에는 `bot_id … subtype === 'bot_message'` 라는 **그때 그 순서**만 잡았다.
 *     순서를 바꿔 쓰면(`subtype === 'bot_message' || m.bot_id`) 안 걸렸다.
 *     지금은 코드에 `'bot_message'` 라는 **문자열이 있는지**만 본다 — 순서와 무관하다.
 *     실측(2026-08-13)으로 그 문자열은 정본 `slack-live.js` 한 곳뿐이고, 남은 `bot_id`
 *     세 곳은 전부 작성자 이름에 `(봇)` 을 붙이는 표기용이라 오탐이 안 난다.
 *   · 전에는 대상 파일이 셋으로 **하드코딩**돼 있어 새 파일이 베껴 쓰면 못 봤다.
 *     지금은 `src/` 아래 모든 js 를 순회한다.
 *
 * 문자열 규칙만으로는 `if (m.bot_id) return;` 처럼 **한쪽 조건만 옮겨 적은 것**을 못 잡는다.
 * 그건 아래 ②-b(호출하고 있나)가 받는다. */
function jsFilesUnder(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...jsFilesUnder(p));
    else if (e.name.endsWith('.js')) out.push(p);
  }
  return out;
}

/** 주석을 걷어낸 코드. 설명에는 그 낱말이 나올 수 있어서 본문만 본다.
 *
 * **`#` 도 걷어낸다 (2026-09-03).** 이 함수는 JS 뿐 아니라 **파이썬 파일에도** 쓰인다 —
 * 아래 ③(`apply_approvals.py` 의 `in excluded_names`)과 ④-b(`fetch_slack_files.py` 의
 * `--exts`·`--prefer` 기본값) 세 단언이 그것이다. 그런데 걷어내는 것이 JS 주석뿐이라,
 * 파이썬 쪽은 **`#` 주석 한 줄이 세 단언을 전부 통과시켰다** — 거르는 코드를 지우고
 * 주석에 그 문장을 적어 두기만 해도 초록이 났다. 이 파일의 머리말이 말하는 「주석이
 * 지킨다고 적혀 있는데 아무것도 안 지키는」 바로 그 상태다.
 *
 * JS 에서 `#` 로 시작하는 줄은 셔뱅(`#!/usr/bin/env node`)과 비공개 클래스 필드뿐이고
 * 실측(2026-09-03) 로 `src/` 아래에는 둘 다 없다. 셔뱅을 지우는 것은 결과를 안 바꾼다.
 *
 * **줄 맨 앞의 주석만 걷어낸다** — 꼬리 주석(`x = 1  # …`)은 그대로 남는다. JS 쪽
 * 꼬리 `//` 도 마찬가지라 두 언어가 같은 한계를 갖는다. 문자열 안의 `#`·`//` 를 안
 * 건드리려면 이 자리가 파서가 되어야 하는데, 그건 이 검사가 지키려는 것보다 크다. */
const codeOf = (text) => text.split('\n').filter((l) => !/^\s*(\/\/|\*|\/\*|#)/.test(l)).join('\n');

/* 이 걷어내기가 실제로 듣는가 (합성 입력 — 아래 ②-c·④ 와 같은 뜻).
 * 「오늘 돌렸더니 초록」은 검사가 맞다는 증거가 아니다. */
for (const [label, src, want] of [
  ['파이썬 주석 (걷어내야 함)', '# if fid in excluded_names: pass', ''],
  ['들여쓴 파이썬 주석 (걷어내야 함)', '    # in excluded_names', ''],
  ['JS 한 줄 주석 (걷어내야 함)', "// 'bot_message'", ''],
  ['블록 주석 속 줄 (걷어내야 함)', " * in excluded_names", ''],
  ['셔뱅 (걷어내도 결과가 같다)', '#!/usr/bin/env python3', ''],
  ['진짜 코드 (남아야 함)', 'if key in excluded_names:', 'if key in excluded_names:'],
  ['문자열 안의 # (남아야 함)', 'sep = "#"', 'sep = "#"'],
]) {
  if (codeOf(src) !== want) {
    fail(`주석 걷어내기가 스스로를 못 지킵니다 — ${label}: ${JSON.stringify(codeOf(src))} (원한 것 ${JSON.stringify(want)})`);
  }
}

/**
 * 판정을 베껴 쓴 파일 목록. **읽는 함수를 인자로 받는다** — 그래야 아래에서 합성 입력으로
 * 이 함수 자체를 시험할 수 있다(실제 파일을 건드리지 않고 red-green).
 */
export function findInlineCopies(files, read) {
  const bad = [];
  for (const f of files) {
    if (f.replace(/\\/g, '/').endsWith('src/slack-live.js')) continue;   // 정본
    let code;
    try {
      code = codeOf(read(f));
    } catch {
      continue;                                                          // 없는 파일은 건너뛴다
    }
    if (code.includes("'bot_message'") || code.includes('"bot_message"')) bad.push(f);
  }
  return bad;
}

const SRC = path.join(HERE, '..', 'src');
const rel = (p) => path.relative(path.join(HERE, '..'), p).replace(/\\/g, '/');
for (const f of findInlineCopies(jsFilesUnder(SRC), (p) => fs.readFileSync(p, 'utf-8'))) {
  fail(`${rel(f)} 가 봇 판정을 베껴 쓰고 있습니다 — slack-live.js 의 isBotMessage 를 부르세요.`);
}

/* ── ②-b 판정이 필요한 곳이 실제로 **부르고 있나** ──────────────────────
 * 문자열을 안 쓰고 조건 하나만 옮겨 적으면 위 검사가 못 본다. 그런 파일은 호출이
 * 사라지는 것이 특징이라, 여기서 반대쪽에서 받는다. 이쪽이 문자열 규칙보다 강하다. */
export const callsJudgment = (code) => /\bisBotMessage\s*\(/.test(codeOf(code));

const MUST_CALL = ['src/archive-health/attachments.js', 'src/index.js', 'src/ingest/slack-archive.js'];
for (const f of MUST_CALL) {
  const p = path.join(HERE, '..', f);
  let text;
  try {
    text = fs.readFileSync(p, 'utf-8');
  } catch {
    fail(`${f} 를 읽지 못했습니다 — 봇 판정을 부르는지 확인할 수 없습니다.`);
    continue;
  }
  if (!callsJudgment(text)) {
    fail(`${f} 가 isBotMessage 를 부르지 않습니다 — 판정을 인라인으로 옮겨 적었을 수 있습니다.`);
  }
}

/* ── ②-c 이 검사가 실제로 잡는가 (합성 입력) ───────────────────────────
 * 「오늘 돌렸더니 0건」은 검사가 맞다는 증거가 아니다 — 아무것도 안 보는 검사도 0건을 낸다.
 * 실제 파일을 건드리지 않고 세 경우를 먹여 본다. */
const CASES = [
  ['순서를 바꿔 쓴 것', "if (m.subtype === 'bot_message' || m.bot_id) return;", true],
  ['옛 순서 그대로', "if (m.bot_id || m.subtype === 'bot_message') return;", true],
  ['표기용 (오탐이면 안 된다)', "const author = m.bot_id && !m.user ? `${n} (봇)` : n;", false],
  ['주석 안 (오탐이면 안 된다)', "// subtype === 'bot_message' 는 slack-live.js 가 본다", false],
];
for (const [label, src, shouldFlag] of CASES) {
  const got = findInlineCopies(['src/fake.js'], () => src).length > 0;
  if (got !== shouldFlag) {
    fail(`이 검사가 스스로를 못 지킵니다 — ${label}: ${got ? '잡았다' : '못 잡았다'} (원한 것: ${shouldFlag ? '잡아야 함' : '넘겨야 함'})`);
  }
}
// 정본은 자기 자신을 베낌으로 세지 않는다 (거르지 않으면 늘 실패한다).
if (findInlineCopies([path.join(SRC, 'slack-live.js')], (p) => fs.readFileSync(p, 'utf-8')).length) {
  fail('정본 slack-live.js 를 베낌으로 셌습니다 — 제외 규칙이 깨졌습니다.');
}
// ②-b 도 같은 방식으로 스스로를 본다.
for (const [label, src, want] of [
  ['부르는 코드', 'if (isBotMessage(m, selfId)) return;', true],
  ['안 부르는 코드', 'if (m.bot_id) return;', false],
  ['주석에만 있는 것', '// isBotMessage(m) 를 부르면 된다', false],
]) {
  if (callsJudgment(src) !== want) {
    fail(`②-b 가 스스로를 못 지킵니다 — ${label}`);
  }
}

/* ── ③ excluded 를 ID 로만 보는 곳이 없나 ───────────────────────────────
 * 같은 자료를 다시 올리면 파일 ID 가 새로 생긴다. 한쪽만 ID 로 보면 그 순간
 * 「미변환 1건」과 「미반영 0건」으로 갈리고, 수집은 제외 결정을 무효화한다. */
/* **JS 쪽도 2026-08-26 부터 동작으로 잰다.** 그전에는 `archive-health.js` 의
 * `excludedNames.has(` 개수를 셌는데, 그 재는 법으로는 **이름만 바꾼 것과 진짜로 지운
 * 것이 같은 신호**를 낸다. 그날 실측: `excludedNames` → `exNames` 로 바꾸기만 해도
 * (동작은 그대로) 이 검사가 실패했고, `excludedNames.has(` 한 곳을 지운 것(동작이 깨짐)
 * 도 똑같이 실패했다. 구별이 안 되면 곧 안 읽게 된다 — 9차가 파이썬 쪽에서 겪은 그 모양.
 *
 * 게다가 **`deferredNames` 는 어느 검사도 안 봤다** — 거르는 줄 넷을 통째로 지워도
 * 검사 14종이 전부 통과했다(같은 날 실측). 개수 세기는 자기가 아는 이름만 지킨다.
 *
 * 그래서 판정을 `docFilters`·`classifyDoc` 으로 꺼내 **파이썬과 같은 후보를 먹여 답을
 * 대본다.** 아래 ③-b 가 그 자리다 — 이름을 어떻게 적든 안 깨지고, 거르는 자리가
 * 사라지면 갈래마다 잡힌다. */

/* 파이썬 쪽은 **두 파일**이다 (2026-08-13 에 하나 늘었다).
 *
 * `apply_approvals.py` 가 `excluded` 를 ID 로만 보고 있었다. 같은 자료를 다시 올리면 ID 가
 * 새로 생기므로, 영구 제외한 등기부등본 스레드에 `[공개]` 가 달리면 그 도구가 「아직 변환
 * 전」으로 보고해 **제외한 자료를 변환하라고 권했다.** 옆줄의 `deferred` 는 이름까지 보는데
 * 이 줄만 빠져 있었고, 이 검사가 그 파일을 안 봐서 조용했다 — 검사의 대상 목록이 곧
 * 「무엇이 지켜지나」이므로, 판정을 쓰는 파일이 늘면 여기도 늘려야 한다.
 *
 * **재는 법이 둘로 갈린다.** `fetch_slack_files.py` 는 `load_filters`·`classify` 라는
 * 순수 함수가 있어 **실제로 먹여 보고**, `apply_approvals.py` 는 그런 함수가 없어 코드
 * 모양으로 본다. */

/* ── ③-b classify() 의 다섯 갈래가 실제로 상의되나 ──────────────────────
 *
 * 2026-08-26 까지는 `fetch_slack_files.py` 도 `in excluded_names` 라는 **글자 모양**으로
 * 찾았다. 그날 집합을 `filters` 딕셔너리로 묶어 `filters["excluded_names"]` 가 되자 그
 * 모양이 사라져, **동작은 그대로인데 이 검사만 실패**했다. 그 실패는 진짜 어긋남과
 * 구별되지 않는다(위 55~59행과 같은 이유) — 이름 필터를 정말로 지워도 결과가 똑같다.
 * 그래서 정규식을 넓히는 대신 판정을 불러 답을 본다. 이름을 어떻게 적든 안 깨지고,
 * 걸러는 자리가 사라지면 잡힌다.
 *
 * **그때는 `excluded` 한 갈래만 먹였다.** 2026-08-26 에 나머지를 하나씩 지워 재 보니
 * `known`·`deferred`·`superseded`·`other_format` 은 **분기를 통째로 지워도 이 검사가
 * 초록**이었다(✗0. 이름 경로만 죽인 `deferred_names`·`superseded_names` 도 ✗0).
 * 그 갈래들을 지키는 `test_fetch_filters.py` 는 **손으로만 도는 시험**이고
 * (`SKILL.md` 「시험」 — `npm run check` 도 pre-commit 훅도 안 부른다), 그래서
 * **자동으로 도는 관문은 다섯 중 하나만 보고 있었다.**
 *
 * 갈래가 하나 죽으면 에러가 아니라 숫자로 나타난다 — `load_filters` 주석이 못박은
 * 「세 곳(여기 · `pendingDocuments` · `unconvertedAmong`)이 같아야 한다」가 깨져서,
 * 위생 점검은 「N건 남음」인데 요약 꼬리말은 「0건」이 된다. `superseded` 만은 JS 쪽에
 * 대응이 없는 것이 알려진 상태다(할일 목록의 `archive-health.js` 항목) — 여기서는
 * 파이썬 쪽이 그 갈래를 계속 거르는지만 지킨다.
 *
 * **후보는 한 경로씩만 맞게 만든다.** ID 와 이름이 동시에 맞는 후보 하나만 재면 두 경로
 * 중 하나가 죽어도 통과한다(2026-08-26 에 `test_fetch_filters.py` 첫 판이 실제로 그랬다).
 * 아래 `idOnly` 대조군이 그 성질을 매번 확인한다. */

// 날짜를 박아 두면 그날이 지나는 순간 「만기 전」이 아니라 「만기 후」를 재게 된다.
// 30일 여유라 KST·UTC 차이는 결과를 못 바꾼다.
const DEFER_UNTIL = new Date(Date.now() + 30 * 86400 * 1000).toISOString().slice(0, 10);

const FILTER_STATE = {
  slack_files: {
    F_KNOWN: {
      channel: '사업장가', name: '산정 내역.xlsx', doc: '사업장가/산정-내역.md', date: '2026-08-23',
    },
  },
  excluded: {
    F_EXCLUDED: {
      channel: '사업장나', name: '등기부등본.pdf', reason: '영구 제외', decided: '2026-08-26',
    },
  },
  deferred: {
    F_DEFERRED: { channel: '사업장라', name: '요약본-260824.xlsx', until: DEFER_UNTIL },
  },
  superseded: {
    F_SUPERSEDED: {
      channel: '비공개나', name: '일보 260710.xlsx', kept: '일보 260714.xlsx',
      kept_archived: false, at: '2026-08-26',
    },
    // 아래 「겹친 것」과 짝 — 같은 파일이 두 칸에 들어 있을 때 어느 쪽으로 세느냐를 가른다.
    F_BOTH: {
      channel: '사업장가', name: '겹친 것.pdf', kept: '새 판.pdf',
      kept_archived: true, at: '2026-08-26',
    },
  },
};
// `excluded` 가 먼저다. 두 언어 중 한쪽만 순서를 바꾸면 여기서 갈린다.
FILTER_STATE.excluded.F_BOTH = {
  channel: '사업장가', name: '겹친 것.pdf', reason: 'x', decided: '2026-08-26',
};

/* [라벨, 후보, 기대 갈래, 이름 경로인가]
 * 마지막 칸이 `true` 인 줄이 **이름으로만 맞는 후보**다 — 아래 가짜 판정에 안 걸려야 한다. */
const FILTER_CASES = [
  ['변환 끝난 것', { id: 'F_KNOWN', channel: '사업장가', name: '산정 내역.xlsx' }, 'known', false],
  ['같은 자료의 다른 포맷 판',
    { id: 'F_OTHER', channel: '사업장가', name: '산정 내역.pdf' }, 'other_format', true],
  ['제외 — ID 만 맞는 후보',
    { id: 'F_EXCLUDED', channel: '사업장나', name: '다른자료.pdf' }, 'excluded', false],
  ['제외 — 이름만 맞는 후보 (다시 올려 ID 가 새로 생긴 것)',
    { id: 'F_NEW1', channel: '사업장나', name: '등기부등본.hwp' }, 'excluded', true],
  ['보류 — ID 만 맞는 후보',
    { id: 'F_DEFERRED', channel: '사업장라', name: '다른자료.xlsx' }, 'deferred', false],
  ['보류 — 이름만 맞는 후보',
    { id: 'F_NEW2', channel: '사업장라', name: '요약본-260824.pdf' }, 'deferred', true],
  ['물림 — ID 만 맞는 후보',
    { id: 'F_SUPERSEDED', channel: '비공개나', name: '다른자료.xlsx' }, 'superseded', false],
  ['물림 — 이름만 맞는 후보',
    { id: 'F_NEW3', channel: '비공개나', name: '일보 260710.pdf' }, 'superseded', true],
  // 거르는 순서 — 제외와 물림에 함께 든 파일은 **제외**로 세어야 한다. 순서가 바뀌면
  // 같은 자료를 한쪽은 「제외」, 한쪽은 「물림」으로 세어 화면 숫자가 조용히 갈린다.
  ['제외와 물림에 함께 든 것 (제외가 이긴다)',
    { id: 'F_BOTH', channel: '사업장가', name: '겹친 것.pdf' }, 'excluded', false],
];

/* 한글이 오가므로 바이트로 주고받는다 (위 fold 와 같은 이유).
 * `id_only` 는 **일부러 ID 만 보는 가짜 판정**이다. 이 검사가 두 경로를 실제로 구별하는지
 * 재는 대조군으로, 후보를 잘못 만들면 여기서 드러난다 (위 ②-c 와 같은 뜻). */
const PY_FILTER_SNIPPET = ['-c', [
  'import json,sys',
  `sys.path.insert(0, ${JSON.stringify(PY_DIR)})`,
  'from fetch_slack_files import load_filters, classify',
  "data = json.loads(sys.stdin.buffer.read().decode('utf-8'))",
  'filters = load_filters(data["state"])',
  'ID_SETS = ("known", "excluded", "deferred", "superseded")',
  'def id_only(rec):',
  '    return next((c for c in ID_SETS if rec["id"] in filters[c]), None)',
  'out = {"real": [classify(r, filters) for r in data["recs"]],',
  '       "idOnly": [id_only(r) for r in data["recs"]]}',
  "sys.stdout.buffer.write(json.dumps(out).encode('ascii'))",
].join('\n')];

let pyFilters = null;
let pyFiltersErr = '';
for (const name of PY_NAMES) {
  try {
    pyFilters = JSON.parse(execFileSync(name, PY_FILTER_SNIPPET, {
      input: JSON.stringify({ state: FILTER_STATE, recs: FILTER_CASES.map(([, r]) => r) }),
      encoding: 'utf-8',
    }));
    break;
  } catch (err) {
    pyFiltersErr = `${name}: ${err.message.split('\n')[0]}`;
  }
}
if (!pyFilters) {
  // 대조를 못 한 것은 「같다」가 아니다 (위 ① 과 같은 이유).
  fail(`fetch_slack_files.py 의 후보 거르기를 부르지 못했습니다 — ${pyFiltersErr}`);
} else {
  // JS 쪽(`archive-health.js`)에 같은 상태·같은 후보를 먹인다. 여기가 갈리면 수집과
  // 위생 점검·요약 꼬리말이 서로 다른 숫자를 말한다.
  const jsFilters = docFilters(FILTER_STATE);
  FILTER_CASES.forEach(([label, rec, want, nameOnly], i) => {
    if (pyFilters.real[i] !== want) {
      fail(`fetch_slack_files.py 가 후보를 ${want} 로 안 거릅니다 — ${label}: `
        + `${JSON.stringify(pyFilters.real[i])}. 갈래가 하나 죽으면 에러가 아니라 `
        + '위생 점검과 요약 꼬리말의 숫자가 갈립니다.');
    }
    const js = classifyDoc(rec, jsFilters);
    if (js !== pyFilters.real[i]) {
      fail(`후보 거르기가 두 언어에서 갈립니다 — ${label}: `
        + `JS ${JSON.stringify(js)} · 파이썬 ${JSON.stringify(pyFilters.real[i])}. `
        + 'archive-health.js 와 fetch_slack_files.py 는 같은 판정이어야 합니다.');
    }
    // 가짜 판정에도 잡히면 그 후보는 두 경로를 안 가른 것이고, 그런 검사는 이름 경로를
    // 지워도 늘 통과한다 — 지금 막으려는 「눈 없는 관문」과 같은 상태가 된다.
    if (nameOnly && pyFilters.idOnly[i] !== null) {
      fail(`이 검사가 ID 경로와 이름 경로를 구별하지 못합니다 — ${label} 가 ID 만 보는 `
        + `가짜 판정에도 ${JSON.stringify(pyFilters.idOnly[i])} 로 잡혔습니다. `
        + '후보를 다시 만드세요.');
    }
  });

  /* **파일 ID 가 없는 자리도 재야 한다.** `unconvertedAmong` 의 입력은 채널·이름뿐이라
   * ID 경로가 통째로 죽어 있다. 여기를 안 재면 「ID 로만 거르게」 축소해도 위 대조가
   * 전부 통과한다 — 요약 꼬리말만 조용히 틀린다. */
  FILTER_CASES.filter(([, , , nameOnly]) => nameOnly).forEach(([label, rec, want]) => {
    const noId = classifyDoc({ channel: rec.channel, name: rec.name }, jsFilters);
    if (noId !== want) {
      fail(`ID 를 빼면 안 걸립니다 — ${label}: ${JSON.stringify(noId)} `
        + `(${want} 여야 합니다). 요약 꼬리말(unconvertedAmong)이 그 상태로 셉니다.`);
    }
  });
}

{
  const f = 'apply_approvals.py';
  const src = execFileSync('node', ['-e',
    `process.stdout.write(require('fs').readFileSync(${JSON.stringify(path.join(PY_DIR, f))},'utf8'))`,
  ], { encoding: 'utf-8' });
  // 주석에서 이름만 말하는 것과 실제로 거르는 것을 구별한다 (이 검사를 만들며 실제로 샜다).
  const code = codeOf(src);
  if (!/in\s+excluded_names/.test(code)) {
    fail(`${f} 가 excluded_names 로 거르지 않습니다 — excluded 를 ID 로만 보고 있습니다.`);
  }
}

/* ── ④ 확장자 목록과 우선순위가 세 자리에서 같나 ────────────────────────
 * `doc-archive` 가 다루는 확장자 목록은 **세 자리**에 있다 —
 * `fetch_slack_files.py` 의 `DOC_EXTS`(수집), `apply_approvals.py` 의 `DOC_EXTS`
 * (`[공개]` 승인 반영), `archive-health.js` 의 `DOC_EXTS`(미변환 집계).
 * 셋이 갈리면 **에러가 안 나고 숫자만 달라진다** — 수집은 「0건」인데 점검은
 * 「N건 남음」이라고 DM 을 보내고, 그 어긋남은 매일 오는 숫자로만 드러난다.
 *
 * 2026-08-28 까지 이 검사는 다섯 갈래 필터(`classify` ↔ `classifyDoc`)는 대봤지만
 * **확장자 목록은 안 봤다.** 그날 엑셀이 상황판에서 안 세어지고 있던 것을 사람이
 * 물어봐서 알았다 — 목록이 여섯 확장자뿐이었고 넷(상황판·09:00·16:00·요약 하단)이
 * 함께 조용했다.
 *
 * 짝이 되는 우선순위(`PREFER` ↔ `PREFER_EXTS`)도 함께 본다. 같은 두 파일의 같은
 * 종류 버그이고, 갈리면 한쪽이 pdf 를 고르고 한쪽이 hwpx 를 골라 **어느 포맷을 내려받아
 * 아카이브에 넣을지(수집)와 위생 점검이 「최근 `<이름>`」으로 보고하는 이름이 서로
 * 달라진다** — 건수 자체는 이름 키로 한 건씩만 세므로(양쪽 다 `nameKey`/`name_key`) 안 갈린다.
 * **순서가 곧 우선순위라 순서까지 본다.** */

/** 두 목록을 집합으로 비교. 순서에 뜻이 없는 값(확장자)에 쓴다. */
function sameSet(a, b) {
  const A = new Set(a);
  const B = new Set(b);
  return {
    ok: A.size === B.size && [...A].every((x) => B.has(x)),
    onlyA: [...A].filter((x) => !B.has(x)).sort(),
    onlyB: [...B].filter((x) => !A.has(x)).sort(),
  };
}

/** 두 목록을 순서까지 비교. 순서가 곧 뜻인 값(우선순위)에 쓴다. */
function sameOrder(a, b) {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/* 이 검사가 실제로 잡는가 (합성 입력 — 위 ②-c 와 같은 뜻).
 * 「오늘 돌렸더니 초록」은 검사가 맞다는 증거가 아니다. 아무것도 안 보는 검사도 초록이다. */
for (const [label, a, b, wantOk] of [
  ['확장자 — 같은 것', ['pdf', 'hwp'], ['hwp', 'pdf'], true],
  ['확장자 — 한쪽에만 있는 것', ['pdf', 'hwp', 'xlsx'], ['pdf', 'hwp'], false],
  ['확장자 — 한쪽에만 있는 것 (반대 방향)', ['pdf', 'hwp'], ['pdf', 'hwp', 'xlsx'], false],
  ['확장자 — 서로 다른 것', ['pdf', 'xlsx'], ['pdf', 'xlsm'], false],
]) {
  if (sameSet(a, b).ok !== wantOk) {
    fail(`④ 가 스스로를 못 지킵니다 — ${label}`);
  }
}
for (const [label, a, b, wantOk] of [
  ['우선순위 — 같은 것', ['hwpx', 'pdf'], ['hwpx', 'pdf'], true],
  ['우선순위 — 순서만 다른 것', ['hwpx', 'pdf'], ['pdf', 'hwpx'], false],
  ['우선순위 — 길이가 다른 것', ['hwpx', 'pdf'], ['hwpx'], false],
  ['우선순위 — 짧은 쪽이 앞 (반대 방향)', ['hwpx'], ['hwpx', 'pdf'], false],
]) {
  if (sameOrder(a, b) !== wantOk) {
    fail(`④ 가 스스로를 못 지킵니다 — ${label}`);
  }
}

/* 한글이 없는 데이터지만 기존 스니펫과 같은 형태로 바이트로 주고받는다. */
const PY_CONST_SNIPPET = ['-c', [
  'import json,sys',
  `sys.path.insert(0, ${JSON.stringify(PY_DIR)})`,
  'import fetch_slack_files as F, apply_approvals as A',
  'out = {"fetch_exts": sorted(F.DOC_EXTS),',
  '       "fetch_prefer": list(F.PREFER_EXTS),',
  '       "approvals_exts": sorted(A.DOC_EXTS)}',
  "sys.stdout.buffer.write(json.dumps(out).encode('ascii'))",
].join('\n')];

let pyConst = null;
let pyConstErr = '';
for (const name of PY_NAMES) {
  try {
    pyConst = JSON.parse(execFileSync(name, PY_CONST_SNIPPET, { encoding: 'utf-8' }));
    break;
  } catch (err) {
    pyConstErr = `${name}: ${err.message.split('\n')[0]}`;
  }
}
if (!pyConst) {
  // 대조를 못 한 것은 「같다」가 아니다.
  fail(`파이썬 쪽 확장자 목록을 읽지 못했습니다 — ${pyConstErr}`);
} else {
  const js = [...jsDocExts].sort();
  for (const [label, py] of [
    ['fetch_slack_files.py', pyConst.fetch_exts],
    ['apply_approvals.py', pyConst.approvals_exts],
  ]) {
    const r = sameSet(js, py);
    if (!r.ok) {
      fail(`확장자 목록이 갈립니다 — archive-health.js 와 ${label}:`
        + ` JS 에만 [${r.onlyA.join(', ')}] · 파이썬에만 [${r.onlyB.join(', ')}]`);
    }
  }
  if (!sameOrder(jsPrefer, pyConst.fetch_prefer)) {
    fail(`중복 포맷 우선순위가 갈립니다 (순서까지 같아야 합니다) —`
      + ` JS [${jsPrefer.join(', ')}] · 파이썬 [${pyConst.fetch_prefer.join(', ')}]`);
  }
}

/* ── ④-b 수집이 실제로 도는 기본값이 상수에서 나오나 (코드 모양) ────────
 * 위 대조는 `DOC_EXTS`·`PREFER_EXTS` **상수 자체**를 읽는다. 하지만 수집
 * (`fetch_slack_files.py`)은 그 상수가 아니라 `args.exts`/`args.prefer`(argparse 기본값)로
 * 돈다. 그 기본값이 `",".join(sorted(DOC_EXTS))`/`",".join(PREFER_EXTS)` 로 상수를
 * 참조하는 동안은 위 대조와 같은 값이지만, 누가 리터럴 문자열로 되돌리면 위 대조는
 * 계속 초록인 채 실제 수집만 다른 목록을 쓴다 — `default=` 는 상수를 안 거치고 읽으므로
 * 그 어긋남은 대조로 못 잡는다. `apply_approvals.py` 에 불러볼 순수 함수가 없어 코드
 * 모양으로 보는 위 ③ 과 같은 이유로, 여기도 값을 부르는 대신 모양을 본다. */
{
  const f = 'fetch_slack_files.py';
  let src = '';
  try {
    src = fs.readFileSync(path.join(PY_DIR, f), 'utf-8');
  } catch (err) {
    fail(`${f} 를 읽지 못했습니다 — --exts/--prefer 기본값이 상수에서 나오는지 확인할 수 없습니다.`);
  }
  if (src) {
    const code = codeOf(src);
    if (!/"--exts",\s*default=",".join\(sorted\(DOC_EXTS\)\)/.test(code)) {
      fail(`${f} 의 --exts 기본값이 DOC_EXTS 에서 안 나옵니다 — 리터럴로 바뀌었을 수 있습니다.`);
    }
    if (!/"--prefer",\s*default=",".join\(PREFER_EXTS\)/.test(code)) {
      fail(`${f} 의 --prefer 기본값이 PREFER_EXTS 에서 안 나옵니다 — 리터럴로 바뀌었을 수 있습니다.`);
    }
  }
}

if (ok) {
  console.log(`[check-shared-rules] OK — 봇 판정 ${BOT_CASES.length}가지와 근거 대조 정규화 ${FOLD_CASES.length}가지`
    + ` · 인용 조각 내기 ${NEEDLE_CASES.length}가지 · 인용 뽑는 문자군 ${QUOTE_CASES.length}가지`
    + ` · 같은 자료 판정 키 ${NAMEKEY_CASES.length}가지`
    + ` · 개명 지도 ${Object.keys(ALIAS_STATE.channels).length}줄과 되짚기 ${ALIAS_LOOKUPS.length}가지가`
    + ' 두 언어에서 같습니다.'
    + ` 문서 후보 거르기는 다섯 갈래를 후보 ${FILTER_CASES.length}건으로 **양쪽에 먹여**`
    + ' 답까지 대봤습니다 (archive-health.js 의 classifyDoc ↔ fetch_slack_files.py 의 classify'
    + `, 이름 경로 ${FILTER_CASES.filter(([, , , n]) => n).length}건은 ID 를 빼도 걸리고 ID 만 보는`
    + ' 가짜 판정에는 안 걸림). apply_approvals 만 순수 함수가 없어 코드 모양으로 봅니다.'
    + ' 베낀 곳 없음(src js 순회) · 판정이 필요한 세 곳이 부르고 있음.'
    + ` 확장자 목록 ${jsDocExts.size}개가 세 자리에서 같고`
    + ' 중복 포맷 우선순위가 두 자리에서 순서까지 같습니다.'
    + ` 비공개 선언 대조 ${PRIV_CASES.length}가지(자기검사 ${SELF_CASES.length}가지 포함)가`
    + ' 두 언어에서 같고 거부가 실제로 report_rejected_private 로 전달됩니다.');
} else {
  console.error('\n두 구현이 갈리면 에러 없이 숫자만 달라집니다. 고칠 때는 반드시 함께.');
  process.exitCode = 1;
}
