/**
 * 아카이브 커밋·push. **관문(verify.js)을 통과한 뒤에만 부른다.**
 *
 * VM 이 저장소에 쓰는 유일한 자리다. main 에만 올리고 force push 는 쓰지 않는다.
 */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { DATA_ROOT } from './util.js';
import { assertMayWriteArchive } from '../mode.js';

const execFileP = promisify(execFile);

// 전역 git 설정이 없는 기계(새로 만든 VM)에서도 커밋이 되도록 매번 실어 보낸다.
const IDENTITY = [
  '-c', 'user.name=Hermes',
  '-c', 'user.email=hermes@noreply.local',
];

async function git(args, { ok = false, cwd = DATA_ROOT, timeout } = {}) {
  try {
    const opts = { cwd, maxBuffer: 64 * 1024 * 1024, encoding: 'utf8' };
    if (timeout) Object.assign(opts, { timeout, killSignal: 'SIGKILL' });
    const { stdout, stderr } = await execFileP('git', args, opts);
    return { ok: true, stdout: stdout || '', stderr: stderr || '' };
  } catch (err) {
    // **상한에 걸려 죽인 회차는 git 이 stderr 를 한 글자도 안 남긴다** (2026-09-07 실측).
    // 그대로 두면 사유가 Node 의 `Command failed: git pull --ff-only --quiet` 가 되어,
    // 잠금·인증 실패처럼 stderr 없이 죽는 다른 실패와 **글자가 똑같아진다** — DM 을 받은
    // 사람이 「원격이 안 받아준 것」인지 「기다리다 끊은 것」인지 구별할 수 없다.
    // 상한으로 죽은 것은 `err.killed` 로만 구별된다(`signal: 'SIGKILL'`, `code: null`).
    const detail = err.killed && timeout
      ? [`${Math.round(timeout / 1000)}초 안에 응답 없음`, `git ${args[0]}`, err.stderr].filter(Boolean).join(' · ')
      : (err.stderr || err.message);
    if (ok) return { ok: false, stdout: err.stdout || '', stderr: detail };
    throw new Error(`git ${args.join(' ')} 실패: ${String(detail || '').trim()}`);
  }
}

/** 현재 커밋 해시 — 실패 시 여기로 되돌린다. */
export async function head() {
  return (await git(['rev-parse', 'HEAD'])).stdout.trim();
}

/**
 * 시작 전 원격과 맞춘다. 작업 트리가 더러우면 **멈춘다** — 남의 작업 위에 쓰지 않는다.
 */
export async function syncBeforeWork() {
  const dirty = (await git(['status', '--porcelain'])).stdout.trim();
  if (dirty) {
    throw new Error(
      `작업 트리에 반영되지 않은 변경이 있습니다. 손으로 정리한 뒤 다시 돌리세요:\n${dirty.slice(0, 1000)}`,
    );
  }
  const r = await git(['pull', '--ff-only'], { ok: true });
  if (!r.ok) throw new Error(`git pull --ff-only 실패: ${r.stderr.trim()}`);
  return r.stdout.trim();
}

/** 지정한 경로에 바뀐 것이 있나 */
export async function hasChanges(paths) {
  const r = await git(['status', '--porcelain', '--', ...paths]);
  return r.stdout.trim().length > 0;
}

/**
 * 커밋하고 push. push 가 거절되면 rebase 로 한 번만 다시 시도한다.
 * 두 번째도 실패하면 **자동으로 풀지 않고** 그대로 알린다 — 무인으로 히스토리를 손대지 않는다.
 */
export async function commitAndPush(message, paths) {
  // 자료 저장소에 **실제로 쓰는 유일한 자리**. 위쪽 관문을 전부 우회해
  // 여기까지 와도 여기서 막힌다.
  assertMayWriteArchive('아카이브 커밋·push(commitAndPush)');
  await git(['add', '--', ...paths]);

  const staged = (await git(['diff', '--cached', '--name-only'])).stdout.trim();
  if (!staged) return { committed: false, reason: 'no-changes' };

  await git([...IDENTITY, 'commit', '-m', message]);

  let push = await git(['push'], { ok: true });
  if (!push.ok) {
    const rebase = await git(['pull', '--rebase'], { ok: true });
    if (rebase.ok) push = await git(['push'], { ok: true });
  }
  // `git pull --rebase` 가 성공하면 로컬 커밋을 새 해시로 다시 만든다(replay) — 커밋 직후
  // 잰 해시는 그 순간부터 **어느 브랜치 끝에도 없는 값**이 된다. 그래서 push 시도가 전부
  // 끝난 **지금**의 HEAD 를 마지막에 한 번만 읽는다. 안 그러면 rebase 가 있었던 회차의
  // DM 이 "없는 커밋 해시"를 알린다 — 그 해시로 GitHub 에서 찾아도 없다.
  const sha = (await git(['rev-parse', '--short', 'HEAD'])).stdout.trim();
  if (!push.ok) {
    return { committed: true, pushed: false, sha, reason: push.stderr.trim() };
  }
  return { committed: true, pushed: true, sha, files: staged.split('\n').length };
}

/**
 * 관문 실패 시 되돌리기. 시작 시점의 커밋으로 되돌리고, 새로 생긴 파일도 지운다.
 * `git clean` 은 **아카이브 경로에만** 건다 — 저장소 전체에 걸면 남의 작업물을 지운다.
 */
export async function rollback(startSha, paths) {
  // A failed reset must not be followed by deletion, or reported as recovery.
  await git(['reset', '--hard', startSha]);
  await git(['clean', '-fd', '--', ...paths]);
}

/**
 * `git rev-list --count` 가 내놓은 글자를 뒤처진 수로 읽는다. 못 읽으면 **`null`**.
 *
 * **`Number()` 를 쓰면 안 된다.** `Number('')` 는 **0** 이고 `Number.isInteger(0)` 은
 * **true** 라, 빈 출력이 「안 뒤처졌다」로 읽혀 **원격에 닿지도 못한 상태가 ok:true 가
 * 된다** — `syncForRead` 가 없애려는 고장 그 자체다. `Number('abc')` 의 NaN 도
 * `behind===null` 도 `behind===0` 도 아니라 어느 판정에도 안 걸리고 「NaN개 뒤처짐」이
 * 그대로 사람에게 간다. 그래서 **숫자로 바꾸기 전에 숫자인지 글자로 먼저 본다.**
 *
 * 밖으로 뗀 이유: `git rev-list --count` 는 늘 숫자를 내므로 **함수를 통째로 돌려서는
 * 이 입력을 만들 수 없다.** 검사에 직접 먹이려고 export 한다 (`check-sync-for-read.js` E).
 */
export function parseCount(stdout) {
  const raw = String(stdout ?? '').trim();
  return /^\d+$/.test(raw) ? Number(raw) : null;
}

/** 여러 줄 에러에서 쓸 만한 첫 줄만. DM 한 줄에 들어가야 한다. */
function firstLine(s) {
  return String(s || '').split('\n').map((x) => x.trim()).filter(Boolean)[0] || '';
}

/**
 * **읽기 전에 맞춰본다. 절대 던지지 않는다.**
 *
 * `syncBeforeWork` 와 형제지만 성격이 반대다 — 그쪽은 쓰기 전에 부르므로 이상하면
 * 멈춰야 하고, 이쪽은 **점검이 죽으면 안 되므로** 무슨 일이 있어도 결과를 돌려준다.
 *
 * `ok` 는 「pull 이 성공했나」가 아니라 **「HEAD 가 origin 보다 뒤처지지 않음을
 * 확인했나」**다. 잠금 때문에 pull 이 실패해도 이미 최신이면 잰 값은 옳으므로 ok 다.
 * 반대로 pull 을 건너뛴 손 실행이라도 뒤처져 있으면 ok 가 아니다.
 *
 * `behind` 를 못 구하면 **`null` 이다. 0 으로 적지 않는다** — 「안 뒤처졌다」와
 * 「모른다」가 같은 값이 되면 화면이 조용히 거짓말한다.
 */
export async function syncForRead({ cwd = DATA_ROOT, pull = true } = {}) {
  const checkedAt = new Date().toTimeString().slice(0, 8);
  let reason = null;

  /**
   * **이번 실행에서 원격에 실제로 닿았나.**
   *
   * 이 값이 이 함수의 핵심이다. `origin/main` 은 **로컬에 캐시된 참조**라 원격이
   * 끊겨도 그대로 남아 있고, 그걸로 `rev-list` 를 돌리면 **닿지도 못했는데 «안
   * 뒤처졌다»가 나온다.** 그러면 이 함수가 낡은 상태를 «최신»이라고 보고하게 되는데,
   * 그게 바로 이 작업이 없애려는 고장이다. 그래서 **닿은 것을 확인했을 때만** 잰다.
   */
  let reached = false;

  // 네트워크 호출에는 반드시 상한을 건다. 반쯤 끊긴 연결은 예외를 던지지 않고 그냥
  // 영영 걸려 있는다 — `fetch_slack_files.py` 가 예전에 겪은 「대기 상한이 없어 영영
  // 서 있던 것」과 같은 고장이다. 이 함수는 「절대 안 던진다」가 계약인데, 멈춰 버리면
  // 스케줄러의 catch-and-DM 도 못 걸려 아무 신호 없이 죽는다.
  if (pull) {
    const p = await git(['pull', '--ff-only', '--quiet'], { cwd, ok: true, timeout: 60000 });
    if (p.ok) reached = true;
    else reason = firstLine(p.stderr) || 'git pull 실패';
  }

  // pull 을 안 했거나(손 실행) 실패했으면 fetch 로라도 닿아 본다. pull 이 병합 단계에서
  // 실패했어도 fetch 는 되는 경우가 흔하다(잠금 등) — 그때 뒤처짐은 잴 수 있다.
  if (!reached) {
    const f = await git(['fetch', '--quiet'], { cwd, ok: true, timeout: 60000 });
    if (f.ok) reached = true;
    else if (!reason) reason = firstLine(f.stderr) || 'git fetch 실패';
  }

  // HEAD 는 닿았든 아니든 읽는다 — 실패해도 «무엇을 기준으로 쟀나»는 말해야 한다.
  // 이름을 `headSha` 로 둔다 — 이 모듈은 `head()` 를 내보내므로 여기서 `head` 를 쓰면
  // 그 함수 이름을 가린다. 지금은 죽은 코드가 아니지만, 나중에 이 줄 위에서 누가
  // `await head()` 를 추가하면 TDZ `ReferenceError` 가 난다.
  const h = await git(
    ['log', '-1', '--format=%h|%cd', '--date=format-local:%H:%M:%S', 'HEAD'],
    { cwd, ok: true },
  );
  const [headSha = null, headAt = null] = h.ok ? h.stdout.trim().split('|') : [];

  // 닿았을 때만 잰다. 추적 브랜치가 없으면 origin/main 으로 물러선다 (board.py 와 같은 대비).
  let behind = null;
  if (reached) {
    for (const ref of ['@{upstream}', 'origin/main']) {
      const c = await git(['rev-list', '--count', `HEAD..${ref}`], { cwd, ok: true });
      if (c.ok) {
        const n = parseCount(c.stdout);
        // 못 읽으면 이 ref 는 못 쓰는 것으로 치고 다음 ref 로 넘어간다.
        if (n !== null) { behind = n; break; }
      }
    }
  }

  // `reached && behind === 0` 은 지금은 항상 같이 성립한다 — behind 는 `reached` 안에서만
  // 대입되기 때문이다. 그래도 남겨 둔다: 나중에 이 rev-list 블록이 `reached` 밖으로
  // 옮겨지거나 behind 의 기본값이 0 으로 바뀌면, 이 조건이 캐시로 잰 값을 「최신」이라고
  // 보고하는 것을 막는 마지막 방어선이 된다.
  if (reached && behind === 0) return { ok: true, head: headSha, headAt, checkedAt, behind: 0, reason: null };
  return {
    ok: false,
    head: headSha, headAt, checkedAt, behind,
    // 실패한 git 명령이 있을 때만 사유를 채운다. 여기서 `origin 보다 N개 뒤처짐` 을
    // 지어내지 않는다 — 그건 실패가 아니라 «닿았고 쟀다»이고, 그 문장은 compose() 가
    // (안 낡은) 상태를 놓고 판단해 붙일 몫이다.
    reason: reason || (behind === null ? '원격 확인 실패' : null),
  };
}
