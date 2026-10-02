/**
 * ingest 공용 — 워크스페이스 경로와 파이썬 헬퍼 호출.
 *
 * 포맷 규칙이 박혀 있는 스킬 스크립트(insert_messages.py 등)를 **다시 만들지 않고 부른다.**
 * 그 스크립트들에는 손으로 겪어서 알아낸 규칙이 들어 있다 — 예를 들어
 * insert_messages.py 의 find_body_start 는 상단 요약 섹션을 건너뛴다. 이걸 다시 구현하면
 * 2026-08-02 사고(요약표가 사람 발언 본문으로 딸려 들어감)를 그대로 다시 낸다.
 */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { ROOT, DATA_ROOT, config } from '../config.js';

const execFileP = promisify(execFile);

/**
 * 만기가 안 지난 보류 (`.doc-state.json`·`.sync-state.json` 의 `deferred`).
 *
 * `excluded`(영구 제외)와 달리 **만기가 있다.** 만기가 지나면 여기서 안 걸러져 후보로
 * 돌아오고, 그러면 점검이 다시 알린다 — 만기 없는 보류는 삭제와 같아서(위키
 * `[[적어둔-일은-돌아오지-않는다]]`) 만기를 필수로 뒀다.
 *
 * **여기 한 곳에 둔다.** 거르는 자리가 셋이다 — 09:00 위생 점검(`archive-health.js`),
 * 07:00 자동 반영 보고(`ingest/slack-archive.js`), 그리고 사람이 쓰는
 * `apply_edits.py`·`decide.py`(파이썬 쪽 `fetch_slack_files.py` 의 `active_deferred`).
 * 옮겨 적으면 갈리고, 갈리면 한쪽은 「N건 남음」 한쪽은 「0건」이라고 알린다.
 * 2026-08-10 에 실제로 갈려 있었다: 09:00 은 걸렀는데 07:00 은 안 걸러서, 「미뤄 두면
 * 만기까지 조용합니다」라고 안내해 놓고 다음 날 아침에 그 건의 이름을 다시 불렀다.
 *
 * 만기를 못 읽으면 살아 있지 않은 것으로 본다 — 영원히 걸러 어디에도 안 보이는 쪽보다
 * 후보로 돌려 사람이 다시 정하게 하는 쪽이 낫다.
 *
 * @returns {Array<[string, object]>} `Object.entries` 모양
 */
export function activeDeferred(state, now = Date.now()) {
  // 'sv-SE' 로케일이 YYYY-MM-DD 를 준다 (`claude.js` 의 `answerQuestion` 안 오늘 날짜 계산과 같은 방식).
  // UTC 로 재면 안 된다 — 09:00 KST 는 00:00 UTC 라 자정 언저리에서 하루가 어긋난다.
  const today = new Intl.DateTimeFormat('sv-SE', { timeZone: config.timezone }).format(new Date(now));
  return Object.entries(state?.deferred || {}).filter(([, v]) => {
    const until = String(v?.until || '');
    return /^\d{4}-\d{2}-\d{2}$/.test(until) && until >= today;
  });
}

/**
 * 코드 저장소 뿌리. 스킬 스크립트(`.claude/skills/`)가 여기 있다.
 *
 * 예전에는 `WORKSPACE = ROOT/../..` 하나뿐이었고 자료도 코드도 그 아래 있었다.
 * 저장소가 갈린 뒤로는 **둘을 반드시 구별해야 한다** — 섞으면 에러가 아니라
 * 「담긴 문서 없음」·「0건」으로 조용히 통과한다.
 */
export const CODE_ROOT = ROOT;

/** 자료 저장소 뿌리. 커밋·push 는 **여기에만** 건다. */
export { DATA_ROOT };

export const SKILL_SCRIPTS = {
  insertMessages: path.join(CODE_ROOT, '.claude', 'skills', 'slack-sync', 'scripts', 'insert_messages.py'),
  syncIndex: path.join(CODE_ROOT, '.claude', 'skills', 'slack-sync', 'scripts', 'sync_index.py'),
  verifyArchive: path.join(CODE_ROOT, '.claude', 'skills', 'slack-sync', 'scripts', 'verify_archive.py'),
  fetchSlackFiles: path.join(CODE_ROOT, '.claude', 'skills', 'doc-archive', 'scripts', 'fetch_slack_files.py'),
  insertEntry: path.join(CODE_ROOT, '.claude', 'skills', 'doc-archive', 'scripts', 'insert_entry.py'),
  verifyFormat: path.join(CODE_ROOT, '.claude', 'skills', 'doc-archive', 'scripts', 'verify_format.py'),
};

let pythonCmd = null;

/** 이 기계의 파이썬 실행 이름. 리눅스는 python3, 윈도우는 python 인 경우가 많다. */
export async function python() {
  if (pythonCmd) return pythonCmd;
  for (const cmd of ['python3', 'python']) {
    try {
      await execFileP(cmd, ['--version']);
      pythonCmd = cmd;
      return cmd;
    } catch {
      /* 다음 후보 */
    }
  }
  throw new Error('python3 도 python 도 찾지 못했습니다 — 헬퍼 스크립트를 돌릴 수 없습니다.');
}

/**
 * 스킬 스크립트를 부른다.
 *
 * **cwd 기본값은 자료 저장소다.** 다만 지금 스킬 스크립트는 이 값에 안 기댄다 —
 * 2026-08-31 부터 경로가 전부 `_shared/paths.py` 에서 오는 절대경로이고, git 도
 * `cwd=ROOT`(=자료 저장소)를 스스로 실어 부른다. 코드 저장소·자료 저장소·`/tmp`
 * 세 곳에서 `verify_archive.py --all`·`verify_format.py --all` 을 돌려 결과가 같은 것을
 * 확인했다. 그래도 자료 저장소를 기본값으로 두는 것은, 상대경로나 `Path.cwd()` 를 쓰는
 * 스크립트가 생기는 날 **자료 쪽에 떨어지는 것이 맞기 때문**이다
 * (`doc-archive/scripts/decide.py` 가 이미 사람이 준 파일명을 `Path.cwd()` 기준으로 찾는다).
 *
 * @returns {Promise<{ok:boolean, stdout:string, stderr:string}>}
 */
export async function runScript(scriptPath, args, { cwd = DATA_ROOT, timeout = 300000 } = {}) {
  const py = await python();
  try {
    const { stdout, stderr } = await execFileP(py, [scriptPath, ...args], {
      cwd,
      timeout,
      maxBuffer: 32 * 1024 * 1024,
      encoding: 'utf8',
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    });
    return { ok: true, stdout: stdout || '', stderr: stderr || '' };
  } catch (err) {
    return {
      ok: false,
      stdout: err.stdout || '',
      stderr: err.stderr || err.message || '',
    };
  }
}

/**
 * 임시 파일 자리 — **저장소 밖.**
 *
 * 예전에는 `21-hermes/.ingest-tmp/` 에 썼다. 정상 종료 때는 clearTemp 가 지우지만,
 * 프로세스가 중간에 죽으면(OOM·systemd 재시작) md 조각이 남는다. 그러면 다음 실행의
 * syncBeforeWork 가 "작업 트리에 반영되지 않은 변경이 있습니다" 로 던지고,
 * **사람이 VM 에 들어가 지울 때까지 자동 반영이 영구히 멈춘다.** 저장소 밖에 두면
 * 이 실패 종류가 아예 없어진다.
 */
const TEMP_DIR = path.join(os.tmpdir(), 'hermes-ingest');

/** 임시 파일에 내용을 쓰고 경로를 돌려준다 (스크립트의 --content-file 용) */
export function writeTemp(name, content) {
  fs.mkdirSync(TEMP_DIR, { recursive: true });
  const p = path.join(TEMP_DIR, name);
  fs.writeFileSync(p, content, 'utf8');
  return p;
}

/** 임시 디렉터리 비우기. 옛 자리에 남아 있을 수 있는 것도 함께 치운다. */
export function clearTemp() {
  fs.rmSync(TEMP_DIR, { recursive: true, force: true });
  fs.rmSync(path.join(ROOT, '.ingest-tmp'), { recursive: true, force: true });
}

export function readJson(file, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

/** JSON 을 원자적으로 쓴다 — 쓰는 도중 죽어도 상태 파일이 깨지지 않게. */
export function writeJson(file, data) {
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n', 'utf8');
  fs.renameSync(tmp, file);
}
