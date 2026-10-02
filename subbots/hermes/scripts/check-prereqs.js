#!/usr/bin/env node
/**
 * **이 기계에 준비물이 있나.** 설치를 시작하기 전에 돌린다.
 *
 *     npm run doctor
 *
 * 종료코드: 0 지금 갈 수 있음 / 1 지금 막힌 것이 있음
 *
 * ── check-bootstrap.js 와 무엇이 다른가 ──
 *
 * 그쪽은 「**우리 문서·템플릿이 맞나**」를 우리 기계에서 흉내 내는 검사다.
 * 이쪽은 「**이 기계에 준비물이 있나**」를 받는 팀 기계에서 실제로 재는 검사다.
 * 섞으면 실패 한 줄의 뜻이 둘이 되어, 새 팀이 자기 기계 문제인지 우리 저장소
 * 문제인지 못 가른다. 그래서 파일을 나눴다.
 *
 * ── 등급이 셋인 이유 ──
 *
 *   ✗ 지금 못 간다        → 종료코드 1
 *   ⚠ N단계에서 막힌다    → 종료코드 0 (그전까지는 아무 문제 없다)
 *   ? 못 쟀다             → **✗ 로 센다**
 *
 * 여덟 개를 한꺼번에 던지면 새 팀이 전부 고쳐야 하는 줄 알고 6단계 도구를
 * 1단계에서 깐다. 그래서 ⚠ 를 따로 뒀다.
 * 셋째는 `check-business-names.js` 와 같은 결이다 — **안 셈을 통과로 읽으면
 * 검사가 있는 것이 더 위험하다.**
 *
 * ── 이 검사가 안 보는 것 ──
 *
 *   · 슬랙 워크스페이스 권한 · GitHub 계정 · VM — 사람 몫이다
 *   · **kordoc 실물** — 받아오는 데 네트워크가 필요하다. npx 가 있나까지만 본다
 *   · Claude Code 자체 — 이 검사는 그 안에서 불리므로 그때 이미 있다
 *   · **빈 슬랙에 새 팀으로 실제로 설치되나** — 사람만 할 수 있고 그대로 남아 있다
 *
 * 네트워크도 토큰도 안 쓴다. `src/config.js` 를 안 부르고(자료 저장소가 아직 없다)
 * node_modules 도 안 쓴다(`npm install` 전에도 돌아야 한다).
 *
 * ── `npm run doctor` 가 `--no-deprecation` 을 붙이는 이유 ──
 *
 * 이 파일은 윈도우에서 `shell:true` + 인자 배열을 쓴다(quoteForShell 주석 참고) —
 * 그때마다 node 가 DEP0190 경고를 낸다. `지금 막힌 것 0` 바로 다음 줄에 그 경고가
 * 찍히면 새 팀이 첫 명령에서 **마지막으로 보는 문장**이 그것이 된다. `package.json`
 * 의 `doctor` 스크립트가 `--no-deprecation` 으로 가린다. **다른 experimental·
 * deprecation 경고도 함께 가려진다** — node 옵션이라 DEP0190 만 골라 끄지 못한다.
 * 이 파일이 새 경고를 낼 만한 다른 실험적 API 를 쓰기 시작하면 그것도 조용히 묻힌다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const WIN = process.platform === 'win32';

/** 등급. `unknown` 은 화면에 `?` 로 나오고 ✗ 와 같이 센다. */
const rows = [];
const add = (level, name, detail, fix) => rows.push({ level, name, detail, fix });

/**
 * 명령을 한 번 불러 본다.
 *
 * **「없다」와 「못 쟀다」를 가른다.**
 *
 * **윈도우에서는 실제로 부르기 전에 `where.exe` 로 먼저 잰다.** 이 PC 에서 실측한 결과,
 * `shell:true` 로 없는 명령을 부르면 종료코드가 **1** 로 온다(9009 가 아니다 — cmd.exe
 * 버전마다 다르게 낸다는 뜻이다). 종료코드 1 만으로는 "없다"와 "있는데 다른 이유로
 * 실패했다"를 못 가른다. `where.exe` 는 존재 여부만 재는 것이 문서화된 계약이다 —
 * **0 찾음 / 1 못 찾음 / 2 오류**. 그래서 실제 명령을 부르기 **전에** `where.exe` 로
 * 먼저 있는지 없는지를 가르고, 없다고 나오면(1) 그 자리에서 `missing` 으로 확정한다 —
 * 애매한 shell 종료코드를 아예 안 보게 된다. `where.exe` 도 못 가르면(2 · 그 밖) 판단을
 * 미루고 아래 실제 호출 결과로 넘어간다.
 * (2026-09-02, 이 PC 에서 없는 명령 이름으로 직접 재현: `shell:true` 호출도 status 1 ·
 * `where.exe` 호출도 status 1 — 문서화된 계약대로 동작하는 것을 확인했다.)
 * `where.exe` 는 진짜 실행 파일이라(`C:\Windows\System32\where.exe`) shell 없이 바로
 * 부를 수 있다 — quoteForShell·DEP0190 문제가 여기엔 없다. **윈도우가 아니면 이 사전
 * 검사를 안 한다** — 그 밖의 플랫폼은 ENOENT 가 그대로 잡혀 애매함이 없다.
 *
 * 그 다음 기본 규칙은 ENOENT·127·9009 만 없는 것이고(윈도우 cmd.exe 가 "찾을 수
 * 없음"을 127·9009 로도 낸다), 그 밖의 실패(권한 등)는 못 잰 것이다. 둘을 합치면 권한
 * 때문에 못 연 python 이 「없음」으로 보고된다.
 *
 * **세 번째 인자 `{ status1Means: 'missing' }` 을 넘긴 호출부만 예외다.** 종료코드
 * 1 이 "없다"는 뜻이라고 **그 명령 자신이 문서화**한 경우에만 붙인다 — 지금은 둘이다:
 *   · `git config --get <key>` (④) — git-config(1): 매칭되는 키가 없으면 정상적으로
 *     종료코드 1 로 끝난다(에러가 아니다)
 *   · `python -c "import X"` (⑥) — 모듈이 없으면 ModuleNotFoundError 로 죽어 1 을
 *     낸다(파이썬이 잡히지 않은 예외를 종료코드로 알리는 일반 규약)
 * 둘 다 "명령 자체가 없다"가 아니라 "명령은 있는데 그 안에서 찾는 대상이 없다"는
 * 뜻이라 위 `where.exe` 사전 검사와는 다른 자리다 — 이 호출들은 그 명령(git·python)이
 * 이미 `found` 로 확인된 뒤에만 일어난다.
 * **`soffice --version` 은 더는 이 예외에 없다** — 전에는 윈도우 shell 우회로 나는
 * 종료코드 1 을 이 옵션으로 억지로 "없다"로 묶었지만, 이제 위 `where.exe` 사전 검사가
 * 그 자리를 정확하게 가르므로 필요 없어졌다(2026-09-02, `⑦ LibreOffice` 항목에서 뗐다).
 * **이 옵션 없이 status===1 을 통째로 missing 으로 묶으면 안 된다** — 그렇게 묶으면
 * 설치는 돼 있는데 다른 이유로 1 을 낸 경우까지 "없다"로 잘못 읽는다.
 * **③ git·⑥ python·⑧ npx 셋 다 전에는 이 구멍(윈도우에서 없는 명령이 status 1 로 와서
 * `missing` 대신 `unknown` 에 걸리는 것)에 노출돼 있었다** — 지난 라운드 주석은 ⑧ npx
 * 만 적었지만 실제로는 셋 다 같은 문제였다. 위 `where.exe` 사전 검사가 셋을 한 번에
 * 닫는다 — python 은 `unknown` 에 걸려 `python3` 시도 없이 루프가 끊기던 것도 함께
 * 고쳐진다(이 파일 ⑥ 항목의 for 문은 `missing` 이면 원래도 다음 후보로 넘어가게 돼
 * 있었지만, 실제로 오는 값이 `unknown` 이라 그 분기를 못 탔다).
 *
 * 윈도우에서 `npx`·`soffice` 는 `.cmd`·`.exe` 껍데기라 execFile 이 그냥 ENOENT 를
 * 낸다. 그래서 윈도우에서만 shell 을 거친다. **「인자가 전부 고정값」이라는 말은
 * 정확하지 않다** — 커밋·push 관문 검사의 `ROOT`(새 팀이 어디에 clone 했느냐로 정해지는
 * 값)가 인자에 실제로 섞인다. 다만 지금 쓰는 명령 다섯은 전부 맨 이름(`git`·`python`·
 * `soffice`·`npx`)이고, **`cmd` 자체는 한 번도 안 감싸진다**(quoteForShell 은 인자에만
 * 걸린다) — 그래서 위험하지 않다. 틀리는 방향도 안전한 쪽이다: `ROOT` 를 잘못 감싸도
 * 결과는 명령 실패(거짓 ✗)일 뿐 임의 명령 실행으로 번지지 않는다. (2026-09-02, 검토에서
 * 「인자가 전부 고정값」 주장이 사실과 다르다는 지적을 받고 고쳤다 — 동작은 그대로다.)
 *
 * WORKAROUND: shell:true 인 윈도우에서는 node 가 인자를 따옴표 없이 그냥 이어붙인다
 * (실행마다 뜨는 DEP0190 경고가 그 얘기다). `python -c "import sys; ..."` 처럼 공백이
 * 든 인자가 이어붙으면 cmd.exe 가 공백에서 다시 쪼개 python 에 여러 토큰으로 들어가고,
 * `-c` 뒤엔 그중 첫 토큰만 남아 `SyntaxError` 로 죽는다 — 이 PC 에서 python 검사가
 * 그렇게 실제로 깨지는 것을 보고 여기서 직접 따옴표를 씌운다. 2026-09-02.
 */
function quoteForShell(a) {
  if (WIN && /[\s"^&|<>()%!]/.test(a)) return `"${a.replace(/"/g, '""')}"`;
  return a;
}
/**
 * 윈도우에서 `where.exe` 로 명령이 있는지 먼저 잰다 (probe() 머리말 참고).
 * true = 없음(확정) · false = 있음 · null = where 도 못 갈랐음(판단 보류).
 */
function winMissing(cmd) {
  try {
    execFileSync('where', [cmd], { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 15000 });
    return false;
  } catch (e) {
    if (e.status === 1) return true;
    return null;
  }
}
function probe(cmd, args, opts = {}) {
  if (WIN) {
    if (winMissing(cmd) === true) return { state: 'missing' };
  }
  try {
    const out = execFileSync(cmd, args.map(quoteForShell), {
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 15000,
      shell: WIN,
    });
    const first = (out || '').trim().split('\n')[0];
    // 종료코드 0 인데 출력이 비면 「있다」가 아니라 「못 쟀다」다 — 무엇을 찾았는지
    // 못 밝히면 found 로 못 올린다. (2026-09-02, 검토에서 ③·⑥·⑦·⑧ 이 이 자리를
    // 빈 출력이면 그대로 found 로 읽는 것을 지적받고 일반화했다. ④는 이미 호출부에서
    // `cfg.state === 'found' && cfg.out` 로 이 경우를 막고 있었다.)
    if (!first) return { state: 'unknown', why: '종료코드 0 인데 출력이 비었습니다' };
    return { state: 'found', out: first };
  } catch (e) {
    if (e.code === 'ENOENT' || e.status === 127 || e.status === 9009) return { state: 'missing' };
    if (opts.status1Means === 'missing' && e.status === 1) return { state: 'missing' };
    // stderr 를 함께 돌려준다 — "not a git repository" 같은 문구가 e.message 첫 줄이
    // 아니라 stderr 둘째 줄에 오는 경우가 있어(④ ZIP 분기 참고), why(첫 줄)만으로는
    // 못 가르는 호출부가 있다.
    return { state: 'unknown', why: String(e.message || e).split('\n')[0], stderr: String(e.stderr || '') };
  }
}

/* ── ① node 버전 ── 필요한 값을 package.json 에서 읽는다 (손으로 안 베낀다) ── */
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf-8'));
// 숫자만 남기고 이어붙이면 ">=20.11" 이 2011 이 된다 — 첫 숫자 덩어리(메이저)만 뽑는다.
const needMatch = String(pkg.engines?.node || '').match(/(\d+)/);
const need = needMatch ? Number(needMatch[1]) : 0;
const have = Number(process.versions.node.split('.')[0]);
if (!need) add('unknown', 'node 버전', 'package.json 의 engines.node 를 못 읽었습니다', null);
else if (have >= need) add('ok', 'node', `${process.versions.node} (필요 >=${need})`, null);
else add('fail', 'node', `${process.versions.node} — ${need} 이상이 필요합니다`, 'https://nodejs.org 에서 LTS 를 받으세요');

/* ── ② 패키지 설치 ── */
if (fs.existsSync(path.join(ROOT, 'node_modules', '@slack', 'web-api', 'package.json'))) {
  add('ok', '패키지', 'node_modules 에 설치됨', null);
} else {
  add('fail', '패키지', '아직 설치 안 됨 — 2단계부터 전부 막힙니다', 'npm install');
}

/* ── ③ git ── */
const git = probe('git', ['--version']);
if (git.state === 'found') add('ok', 'git', git.out, null);
else if (git.state === 'missing') add('fail', 'git', '못 찾았습니다', 'https://git-scm.com 에서 받으세요');
else add('unknown', 'git', `못 쟀습니다 — ${git.why}`, null);

/* ── ④ 커밋·push 관문 ──
 *
 * 훅 파일은 저장소 안에 있지만 **git 은 훅을 복제하지 않는다.** 그래서 clone 직후에는
 * 꺼져 있고, 꺼져 있다는 신호가 어디에도 안 난다 — 새 팀은 승인 관문이 도는 줄 알고
 * 일하게 된다.
 *
 * **판정은 `git rev-parse --git-path hooks` 로 한다 — `git config --get core.hooksPath`
 * 값을 문자열로 그대로 대보지 않는다.** 문자열 대조는 절대경로·역슬래시·`~` 확장 같은
 * 표기 차이를 놓쳐, 실제로는 켜져 있는 것을 「꺼짐」으로 오탐할 수 있다. `rev-parse
 * --git-path` 는 그 표기를 git 이 흡수한 **실제 경로**를 준다(걸려 있으면 그 경로,
 * 아니면 `.git/hooks`). **`.claude/skills/archive-run/scripts/board.py` 의
 * `probe_hooks()` 가 원본이다** — 2026-08-13 검토에서 문자열 대조 방식을 버리고 이
 * 방법으로 바꾼 경위가 거기 적혀 있다. 이 파일은 그 버린 방법을 다시 쓰고 있었다
 * (2026-09-02 에 board.py 와 같은 방법으로 맞췄다). 둘은 **다른 저장소**를 본다 —
 * 이 파일은 코드 저장소, board.py 는 자료 저장소. 같은 판정의 쌍둥이지 중복이 아니다.
 *
 * **상대경로 `.githooks` 는 이 저장소(코드)에서는 실제로 동작한다** — cwd 가 이
 * 저장소 뿌리로 고정돼 있어서다. 그래서 ✗ 로 만들지 않는다. 다만 **같은 값을 자료
 * 저장소에 걸면 거기엔 `.githooks/` 가 없어 아무것도 안 막는다** — 그래서 절대경로를
 * 권하는 한 줄을 덧붙인다.
 */
const HOOKS = path.join(ROOT, '.githooks');
if (git.state === 'found') {
  const rp = probe('git', ['-C', ROOT, 'rev-parse', '--git-path', 'hooks']);
  // "not a git repository" 는 e.message 첫 줄(rp.why)이 아니라 stderr 둘째 줄에 온다 —
  // rp.why 만 보던 전 라운드는 이 분기가 한 번도 안 걸려 ZIP 으로 받은 팀이 「? 못
  // 쟀습니다」만 받고 고치기 줄을 못 받았다(2026-09-02, 실제 non-git 폴더로 재현해
  // 발견). probe() 가 이제 함께 돌려주는 rp.stderr 로 가른다.
  if (rp.state === 'unknown' && /not a git repository/i.test(rp.stderr || '')) {
    // ZIP 으로 받으면 .git 이 없다 — 이 경우는 「꺼져 있습니다」와 다르다. 고치기 줄도
    // git 명령이라 그대로는 통하지 않으므로 다른 안내를 낸다.
    add('fail', '커밋·push 관문', '이 폴더가 git 저장소가 아닙니다 (ZIP 으로 받으셨을 수 있습니다)',
      'git clone 으로 다시 받으세요 — ZIP 다운로드에는 .git 이 없어 훅을 걸 수 없습니다');
  } else if (rp.state !== 'found') {
    add('unknown', '커밋·push 관문', `못 쟀습니다 — ${rp.why || '원인 미상'}`, null);
  } else {
    const actual = path.resolve(ROOT, rp.out);
    // 화면에 보여줄 값(사람이 무엇을 고쳐야 하는지)은 원문 설정값이 낫다 — 판정에는
    // 안 쓴다(판정은 위 rev-parse 결과로 이미 났다).
    // git-config(1): --get 은 키가 없으면 정상 종료코드 1 (위 probe() 의 status1Means 규칙 참고)
    const cfg = probe('git', ['-C', ROOT, 'config', '--get', 'core.hooksPath'], { status1Means: 'missing' });
    const raw = cfg.state === 'found' ? cfg.out : null;
    // 고치기 줄에 -C 를 넣는다 — 이 문서는 다른 폴더에서 붙여넣힐 수 있고, -C 가 없으면
    // 그때 열려 있는 엉뚱한 저장소에 걸린다.
    const fixCmd = `git -C "${ROOT}" config core.hooksPath "${HOOKS.split(path.sep).join('/')}"`;
    // 가리키는 곳만 보면 안 된다 — **그 자리에 훅이 실제로 있어야 관문이다.**
    // git 은 hooksPath 의 파일이 없어도 아무 말 없이 그냥 지나가므로, 사본에서 훅을
    // 전부 지우고 돌려도 「켜져 있음」이 나왔다 (2026-09-02 발견 · 2026-09-04 고침).
    // 위 185행대의 선언(「꺼져 있다는 신호가 어디에도 안 나는 것을 막는다」)이 값
    // 대조만으로는 지켜지지 않던 자리다.
    const gateMissing = ['pre-commit', 'pre-push', 'push-approve']
      .filter((f) => !fs.existsSync(path.join(actual, f)));
    if (actual === HOOKS && gateMissing.length) {
      add('fail', '커밋·push 관문',
        `hooksPath 는 걸려 있는데 그 자리에 훅 파일이 없습니다: ${gateMissing.join(' · ')} — git 은 없는 훅을 조용히 건너뛰므로 꺼진 것과 같습니다`,
        `git -C "${ROOT}" checkout -- .githooks`);
    } else if (actual === HOOKS) {
      if (raw && !path.isAbsolute(raw)) {
        add('ok', '커밋·push 관문',
          `켜져 있음 (${raw}) — 값이 상대경로입니다. 절대경로로 두는 것이 낫습니다: 같은 값을 자료 저장소에 걸면 거기엔 .githooks/ 가 없어 아무것도 안 막습니다`,
          fixCmd);
      } else {
        add('ok', '커밋·push 관문', `켜져 있음 (${raw || actual})`, null);
      }
    } else {
      add('fail', '커밋·push 관문', raw ? `다른 곳을 가리킵니다 (${raw})` : '꺼져 있습니다', fixCmd);
    }
  }
} else {
  add('unknown', '커밋·push 관문', 'git 이 없어 못 쟀습니다', null);
}

/* ── ⑤ .env ── 키 목록을 .env.example 에서 뽑는다. **값은 한 글자도 안 찍는다.** ── */
const exEnv = fs.readFileSync(path.join(ROOT, '.env.example'), 'utf-8');
const exEnvLines = exEnv.split(/\r?\n/);
const keys = [...exEnv.matchAll(/^([A-Z][A-Z0-9_]*)=/gm)].map((m) => m[1]);

/**
 * 「없어도 됩니다」로 적힌 키는 안 채워도 ⚠ 로 세지 않는다.
 *
 * `.env.example` 의 `GCP_VM`·`GCP_PROJECT`·`GCP_ZONE` 은 주석에 「없어도 됩니다」라고
 * 스스로 적혀 있는데, 이 검사가 이름 모양으로만 뽑아 똑같이 세면 새 팀은 7단계(VM)
 * 전까지 **영영 안 꺼지는 ⚠** 를 본다 — 그러면 「⚠ 는 무시하는 것」을 배우고, 그게 이
 * 검사 설계의 뿌리를 갉는다. 손으로 키 이름을 베끼지 않는다 — `.env.example` 이 바뀌면
 * 조용히 어긋난다. 대신 그 파일의 「없어도 됩니다」 표시를 읽어서 가른다.
 *
 * **가르는 규칙**: 어떤 키로 시작해 주석 없이 곧바로 이어지는 키들(예: GCP 셋)은 같은
 * 안내 문단을 공유한다고 보고, 그 문단의 첫 키 위로 comment 줄을 거슬러 올라가며
 * 「없어도 됩니다」를 찾는다. 빈 줄이나 comment 가 아닌 줄을 만나면 그 문단은 끝이다.
 *
 * **이 규칙이 못 보는 것**: 표시 문구가 정확히 「없어도 됩니다」일 때만 걸린다 — 다른
 * 말로 적히면(예: 「선택 사항입니다」) 그냥 못 읽은 채로 「채워야 할 값」으로 되돌아간다.
 * 억지로 넓게 잡지 않고 **못 읽으면 안전한 쪽(필수로 취급)으로 떨어지게** 뒀다 — 잘못
 * 넓히면 진짜 필수 값이 조용히 열려 있어도 되는 것처럼 보일 수 있다.
 */
const keyLines = exEnvLines
  .map((line, i) => ({ i, m: /^([A-Z][A-Z0-9_]*)=/.exec(line) }))
  .filter((x) => x.m)
  .map((x) => ({ i: x.i, key: x.m[1] }));
const groups = [];
for (const kl of keyLines) {
  const last = groups[groups.length - 1];
  if (last && kl.i === last.lines[last.lines.length - 1].i + 1) last.lines.push(kl);
  else groups.push({ lines: [kl] });
}
const optionalKeys = new Set();
for (const g of groups) {
  let optional = false;
  for (let j = g.lines[0].i - 1; j >= 0; j--) {
    const line = exEnvLines[j];
    if (/^\s*#/.test(line)) {
      if (line.includes('없어도 됩니다')) optional = true;
      continue;
    }
    break; // 주석이 아닌 줄(빈 줄 포함)을 만나면 이 안내 문단은 끝
  }
  if (optional) for (const { key } of g.lines) optionalKeys.add(key);
}

if (!keys.length) {
  add('unknown', '.env', '.env.example 에서 키를 못 읽었습니다', null);
} else if (!fs.existsSync(path.join(ROOT, '.env'))) {
  add('warn', '.env', `아직 없습니다 (${keys.length}개 값) — 1단계에서 만듭니다`,
    WIN ? 'copy .env.example .env' : 'cp .env.example .env');
} else {
  const cur = fs.readFileSync(path.join(ROOT, '.env'), 'utf-8');
  // `\s` 는 줄바꿈도 포함한다 — `=` 뒤 값이 비어 있는 줄에서 `\s*` 가 그 줄의 개행까지
  // 먹어 버리면 `(.*)` 가 **다음 줄**(다음 키=값)을 이 키의 값으로 잘못 캡처한다.
  // 연속으로 빈 키가 여럿이면(GCP_* 셋이 그 예) 실제로 재현된다 — A5 검증 중 실측했다.
  // 가로 공백만 매치하는 `[ \t]` 로 좁혀 줄을 못 넘게 막는다. (2026-09-02)
  const empty = keys.filter((k) => {
    const m = new RegExp(`^[ \\t]*${k}[ \\t]*=[ \\t]*(.*)$`, 'm').exec(cur);
    return !m || !m[1].trim();
  });
  const requiredEmpty = empty.filter((k) => !optionalKeys.has(k));
  const optionalEmpty = empty.filter((k) => optionalKeys.has(k));
  const optionalNote = optionalEmpty.length
    ? `없어도 되는 값이라 셈에서 뺀 것 ${optionalEmpty.length}개: ${optionalEmpty.join(' · ')}`
    : '없어도 되는 값 없음';
  if (requiredEmpty.length) {
    add('warn', '.env', `아직 안 채운 값 ${requiredEmpty.length}개: ${requiredEmpty.join(' · ')} (${optionalNote})`, '.env 를 열어 채우세요');
  } else {
    add('ok', '.env', `채워야 할 값은 다 채워져 있음 (${keys.length}개 중 · ${optionalNote})`, null);
  }
}

/* ── ⑥ python + openpyxl ──
 *
 * **둘의 막히는 자리가 다르다.** openpyxl 은 6단계(엑셀 변환)에서만 쓰지만, **python
 * 자체는 5단계부터 쓴다** — `npm run backfill` 이 끝에 파생값을 다시 세면서 파이썬
 * 스크립트를 부르고(`src/ingest/util.js` 의 `python()` 이 못 찾으면 던진다), 설치가 끝난
 * 뒤에도 매일 07:00·17:00 자동 반영과 09:00 위생 점검이 같은 함수를 부른다.
 * 2026-09-03 까지 이 절이 통째로 「6단계(엑셀)에서만 쓴다」고 적혀 있었고, 그 말을 믿은
 * 설치 마법사가 python 을 6단계까지 미뤄도 된다고 안내했다.
 *
 * **등급은 `warn` 그대로 둔다** — 이 파일의 기호 약속이 ✗ 는 「지금 막힘」이고 python 이
 * 막는 것은 1단계가 아니라 5단계라서다. 그래서 종료코드는 0 으로 나간다. 그 구멍은
 * 마법사가 0단계에서 **⚠ 목록을 사람에게 보이는 것**으로 메운다
 * (`.claude/skills/hermes-install/SKILL.md` 의 0단계 절). 등급을 올릴지는 사람 판단이다. */
const REQ = '.claude/skills/doc-archive/scripts/requirements.txt';
let py = null;
for (const cand of ['python', 'python3']) {
  const r = probe(cand, ['-c', 'import sys; print(sys.version.split()[0], sys.executable)']);
  if (r.state === 'found') { py = { cand, out: r.out }; break; }
  if (r.state === 'unknown') { py = { cand, unknown: r.why }; break; }
}
if (!py) add('warn', 'python', '못 찾았습니다 — 5단계(백필)부터 막히고, 설치 뒤엔 자동 반영이 매일 멈춥니다', 'https://python.org 에서 받으세요');
else if (py.unknown) add('unknown', 'python', `못 쟀습니다 — ${py.unknown}`, null);
else {
  // python -c "import X": 모듈이 없으면 ModuleNotFoundError 로 정상 종료코드 1 (위 probe() 의 status1Means 규칙 참고)
  const mod = probe(py.cand, ['-c', 'import openpyxl; print(openpyxl.__version__)'], { status1Means: 'missing' });
  if (mod.state === 'found') add('ok', 'python + openpyxl', `${py.out} · openpyxl ${mod.out}`, null);
  else if (mod.state === 'unknown') add('unknown', 'python + openpyxl', `못 쟀습니다 — ${mod.why}`, null);
  else add('warn', 'python + openpyxl', `${py.out} — openpyxl 이 없어 6단계(엑셀)에서 막힙니다`,
    `${py.cand} -m pip install -r ${REQ}`);
}

/* ── ⑦ LibreOffice ── hwp·doc·pptx 변환이 이것을 거친다 ── */
const SOFFICE_WIN = [
  'C:/Program Files/LibreOffice/program/soffice.exe',
  'C:/Program Files (x86)/LibreOffice/program/soffice.exe',
];
// 윈도우에서 PATH 에 없으면 probe() 안의 where.exe 사전 검사가 실제 호출 전에 바로
// missing 을 확정한다(위 probe() 머리말 참고) — status1Means 는 이제 필요 없어 뗐다
// (2026-09-02). soffice --version 자체는 "1 = 없음"이라는 문서화된 계약이 없어서,
// 이 옵션을 계속 걸면 설치는 됐는데 다른 이유로 1 을 낸 경우까지 missing 으로 잘못
// 읽을 수 있었다.
let office = probe('soffice', ['--version']);
// `!== 'found'` 로 두면 unknown(못 쟀다)까지 파일 폴백을 타 ✓ 로 승격된다 — 이 파일에서
// `?` 가 ✓ 로 바뀌는 유일한 자리였고 머리말의 「? 는 ✗ 로 센다」와 어긋났다. `missing`
// (못 찾았다)일 때만 파일 존재로 대신 확인한다. (2026-09-02, 검토 지적으로 좁혔다.)
if (office.state === 'missing' && WIN) {
  for (const p of SOFFICE_WIN) {
    if (fs.existsSync(p)) { office = { state: 'found', out: `설치됨 (${p})` }; break; }
  }
}
if (office.state === 'found') add('ok', 'LibreOffice', office.out, null);
else if (office.state === 'unknown') add('unknown', 'LibreOffice', `못 쟀습니다 — ${office.why}`, null);
else add('warn', 'LibreOffice', '못 찾았습니다 — 6단계(hwp·doc·pptx)에서 막힙니다', 'https://libreoffice.org 에서 받으세요');

/* ── ⑧ npx ── kordoc 을 받아 쓰는 통로. **kordoc 실물은 네트워크라 안 본다.** ── */
const npx = probe('npx', ['--version']);
if (npx.state === 'found') add('ok', 'npx', `${npx.out} (kordoc 실물은 6단계에서 드러납니다)`, null);
else if (npx.state === 'unknown') add('unknown', 'npx', `못 쟀습니다 — ${npx.why}`, null);
else add('warn', 'npx', '못 찾았습니다 — 6단계(문서 변환)에서 막힙니다', 'node 를 다시 설치하면 함께 들어옵니다');

/* ── 화면 ──
 *
 * **이빨**: 재료가 이 아래면 「다 통과」가 아니라 실패다. 위 항목이 여덟이라 바닥을
 * 여덟로 둔다. 항목을 늘리면 이 숫자도 함께 올린다.
 * (`check-install-wizard.js` 의 FLOOR 와 같은 결 — 0개를 뽑고 초록을 내는 것이
 * 이 시스템에서 가장 비싼 실패다.)
 */
const FLOOR = 8;
const MARK = { ok: '✓', warn: '⚠', fail: '✗', unknown: '?' };

console.log('\nHermes 준비물 점검\n');
for (const r of rows) {
  console.log(`  ${MARK[r.level]} ${r.name} — ${r.detail}`);
  if (r.fix) console.log(`      ${r.level === 'fail' ? '고치기' : '나중에'}: ${r.fix}`);
}

const blocked = rows.filter((r) => r.level === 'fail' || r.level === 'unknown').length;
const later = rows.filter((r) => r.level === 'warn').length;
console.log(`\n  지금 막힌 것 ${blocked} · 나중에 막힐 것 ${later}\n`);

let bad = blocked > 0;
if (rows.length < FLOOR) {
  console.log(`  ✗ 잰 항목이 ${rows.length}개뿐입니다 (바닥 ${FLOOR}) — 검사가 도중에 빠진 것으로 봅니다\n`);
  bad = true;
}
process.exitCode = bad ? 1 : 0;
