#!/usr/bin/env node
/**
 * **VM 에서 도는 것이 지금 원격에 있는 것과 같은가.**
 *
 *   npm run deployed
 *
 * 종료코드: 0 이상 없음 / 1 어긋남 · 못 잼
 *
 * ── 읽기만 한다 ──
 *
 * pull 도 restart 도 push 도 하지 않는다. 배포를 명령 하나로 묶으면 사람이 멈추는
 * 자리가 없어지고, 잘못 돌린 것이 그대로 봇에 닿는다. 이 검사는 **보이기만** 한다.
 *
 * 판정은 `src/deploy-judge.js` 에 따로 있다 — 거기 「왜 해시 대조로는 모자란가」가
 * 적혀 있고, 시험은 `scripts/check-deploy-judge.js` 가 네트워크 없이 돌린다.
 * 그래서 `npm run check` 에는 **판정 시험만** 걸려 있고 이 파일은 안 걸려 있다.
 *
 * ── VM 값은 .env 에서 온다 ──
 *
 * `GCP_VM`·`GCP_PROJECT`·`GCP_ZONE`. 소스에 우리 팀 값을 박지 않는다 —
 * 그 셋은 `deploy/README.md` 「먼저」 절의 표와 같은 것이다. 없으면 이 검사는
 * 「못 쟀습니다」라고 적고 로컬 몫(안 나간 커밋)만 잰다.
 */
import { execFileSync } from 'node:child_process';
import { ROOT, DATA_ROOT } from '../src/config.js';
import { judgeDeploy } from '../src/deploy-judge.js';

/** git 한 줄. 실패하면 null. */
function git(repo, args) {
  try {
    return execFileSync('git', ['-C', repo, ...args], { encoding: 'utf8' }).trim();
  } catch {
    return null;
  }
}

/** 안 나간 커밋 수. 못 세면 0 이 아니라 -1 로 두어 「못 쟀다」가 드러나게 한다. */
function unpushed(repo) {
  const n = git(repo, ['rev-list', '--count', '@{u}..HEAD']);
  return n === null ? -1 : Number(n);
}

/** `.env` 한 줄. `src/config.js` 가 이미 dotenv 로 읽어 두었다. */
const envOf = (k) => (process.env[k] || '').trim();

/**
 * VM 을 SSH 한 번으로 훑는다. 못 재면 `[null, 사유]`.
 *
 * `--tunnel-through-iap` 를 빼지 말 것 — 22번 포트가 IAP 대역만 열려 있어 직접 SSH 는
 * 원래 안 된다. 빼면 「Connection timed out」이 나고 그 메시지는 이유를 안 알려준다.
 */
function readVm() {
  const vm = envOf('GCP_VM');
  const project = envOf('GCP_PROJECT');
  const zone = envOf('GCP_ZONE');
  const missing = [['GCP_VM', vm], ['GCP_PROJECT', project], ['GCP_ZONE', zone]]
    .filter(([, v]) => !v).map(([k]) => k);
  if (missing.length) return [null, `${missing.join('·')} 이 .env 에 없습니다 (.env.example 참고)`];

  /* 여섯 줄이 순서대로 온다. 시각은 전부 **VM 자기 시계의 epoch 초**라 서로 바로 댄다.
   *
   * **git 마다 `sudo -u hermes` 를 붙인다.** 저장소 소유자가 `hermes` 라 그냥 부르면
   * `dubious ownership` 으로 막힌다 (접속 사용자는 `hermes` 가 아니다). 그러면 해시
   * 자리에 빈 줄이 와서 「VM 응답이 짧습니다」로 떨어진다 — 2026-09-02 실측.
   *
   * 3번째 줄이 「VM 이 그 커밋으로 옮겨간 시각」이다. reflog 의 `%gd` 는 `--date=unix`
   * 에서 `HEAD@{1756800000}` 꼴로 오므로 숫자만 남긴다.
   *
   * 4번째 줄은 `ActiveEnterTimestamp`(사람이 읽는 시각)를 epoch 으로 바꾼 것이다.
   * **`ActiveEnterTimestampMonotonic` 을 쓰지 말 것** — 부팅 기준 마이크로초라
   * `date +%s` 와 바로 뺄 수 없고, 빼면 조용히 엉뚱한 시각이 나온다.
   * `date -d "$(…)"` 대신 `date -f -` 로 stdin 에서 읽는다 — **이 문자열에 큰따옴표와
   * `$` 가 들어가면 윈도우에서 명령이 통째로 깨진다**(아래 주석). */
  const remote = [
    'sudo -u hermes git -C /opt/hermes/code rev-parse HEAD',
    'sudo -u hermes git -C /opt/hermes/archive rev-parse HEAD',
    "sudo -u hermes git -C /opt/hermes/code reflog --date=unix -1 --format=%gd | tr -dc '0-9'; echo",
    'systemctl show hermes -p ActiveEnterTimestamp --value | date -f - +%s',
    'stat -c %Y /opt/hermes/archive/config.json',
    'systemctl is-active hermes || true',
  ].join('; ');

  /* **부르는 방법이 OS 마다 다르다.**
   *
   * 리눅스·맥은 인자 배열을 그대로 넘기면 된다 — 쉘을 안 거치므로 따옴표 걱정이 없다.
   *
   * 윈도우는 `gcloud` 가 `gcloud.cmd` 라 Node 가 직접 실행하지 못한다
   * (`spawnSync gcloud.cmd EINVAL`). `shell: true` 로 우회하면 Node 가 인자를
   * **escape 없이 이어 붙여** 공백에서 쪼개지고, `cmd.exe` 에 배열로 넘기면 Node 가
   * 제 나름의 따옴표를 덧붙여 `--command` 의 묶음이 풀린다. 셋 다 2026-09-02 에
   * 실제로 재봤고, 통한 것은 **한 문자열 + `windowsVerbatimArguments`** 뿐이었다.
   * 그래서 위 `remote` 에 큰따옴표를 쓰면 안 된다 — 여기서 그것으로 묶는다. */
  let raw = null;
  let lastErr = '';
  try {
    if (process.platform === 'win32') {
      const line = `gcloud compute ssh ${vm} --project=${project} --zone=${zone}`
        + ` --tunnel-through-iap --command="${remote}"`;
      raw = execFileSync('cmd.exe', ['/d', '/s', '/c', line], {
        encoding: 'utf8', timeout: 180000, windowsVerbatimArguments: true,
      });
    } else {
      raw = execFileSync('gcloud', [
        'compute', 'ssh', vm, `--project=${project}`, `--zone=${zone}`,
        '--tunnel-through-iap', '--command', remote,
      ], { encoding: 'utf8', timeout: 180000 });
    }
  } catch (e) {
    lastErr = String(e.message).split('\n')[0];
  }
  if (raw === null) return [null, `gcloud 로 VM 에 붙지 못했습니다 — ${lastErr}`];

  const L = raw.trim().split('\n').map((s) => s.trim()).filter((s) => s !== '');
  if (L.length < 6) return [null, `VM 응답이 짧습니다 (${L.length}줄) — 명령이 중간에 끊겼습니다`];

  /* 못 읽은 것을 0 으로 넘기면 판정이 「다 붙었다」로 조용히 넘어간다.
   * 통과와 못 잰 것은 다르다. */
  const num = (s, what) => {
    const n = Number(s);
    if (!Number.isFinite(n) || n <= 0) throw new Error(`${what} 를 숫자로 못 읽었습니다 — "${s}"`);
    return n;
  };

  try {
    return [{
      codeHead: L[0],
      dataHead: L[1],
      codeMovedAt: num(L[2], '코드가 옮겨간 시각'),
      botStartedAt: num(L[3], '봇 기동 시각'),
      configMtime: num(L[4], 'config.json 파일 시각'),
      active: L[5],
    }, ''];
  } catch (e) {
    return [null, e.message];
  }
}

const codeUnpushed = unpushed(ROOT);
const dataUnpushed = unpushed(DATA_ROOT);
const [vm, vmWhy] = readVm();

const results = judgeDeploy({
  codeUnpushed: Math.max(0, codeUnpushed),
  dataUnpushed: Math.max(0, dataUnpushed),
  codeRemote: git(ROOT, ['rev-parse', 'origin/main']) || '',
  dataRemote: git(DATA_ROOT, ['rev-parse', 'origin/main']) || '',
  vm,
  vmWhy,
});

/* 안 나간 커밋을 못 센 경우(원격 추적 가지가 없는 등)는 판정이 아니라 여기서 말한다 —
 * 0 으로 넘어갔으므로 판정 화면만 보면 「깨끗하다」로 읽힌다. */
if (codeUnpushed < 0) results.push({ level: 'unknown', text: '코드 저장소의 안 나간 커밋을 못 셌습니다 (원격 추적 가지가 없나요?)' });
if (dataUnpushed < 0) results.push({ level: 'unknown', text: '자료 저장소의 안 나간 커밋을 못 셌습니다 (원격 추적 가지가 없나요?)' });

const mark = { ok: '✓', info: '·', bad: '✗', unknown: '?' };
for (const r of results) console.log(`  ${mark[r.level]} ${r.text}`);
if (results.some((r) => r.level === 'bad' || r.level === 'unknown')) process.exitCode = 1;
