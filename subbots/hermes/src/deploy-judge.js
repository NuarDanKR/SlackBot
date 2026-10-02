/**
 * **VM 에서 도는 것이 지금 원격에 있는 것과 같은가** — 재본 값으로 판정만 한다.
 *
 * 재는 쪽은 `scripts/check-deployed.js`, 시험은 `scripts/check-deploy-judge.js` 다.
 * 여기를 따로 둔 것은 판정을 **파일도 네트워크도 없이** 시험하기 위해서다 —
 * `npm run check` 는 네트워크 없이 도는 것이 성질이라, VM 조회가 딸려 오면 깨진다.
 *
 * ── 왜 해시 대조로는 모자란가 ──
 *
 * 코드 배포는 사람이 두 줄로 한다 — `git pull` 다음 `systemctl restart`. 받아만 놓고
 * 재시작을 안 하면 **해시는 맞는데 옛 코드가 돈다.** 2026-08-13 에 실제로 그랬고,
 * `git log` 도 pull 도 정상이라 「배포됐다」로 읽혔다. 그래서 시각을 함께 본다:
 * VM 이 그 커밋으로 옮겨간 시각이 봇 기동 시각보다 뒤면 아직 안 붙은 것이다.
 *
 * `config.json` 은 자료 저장소에 있어 15분 안에 저절로 들어오지만, 봇이 기동할 때
 * 한 번만 읽으므로 같은 함정이 있다. 그래서 그 파일 시각도 함께 본다.
 */

/**
 * @param {object} o 재본 값
 * @param {number} o.codeUnpushed 코드 저장소에 안 나간 커밋 수
 * @param {number} o.dataUnpushed 자료 저장소에 안 나간 커밋 수
 * @param {string} o.codeRemote  로컬이 아는 코드 origin/main 해시
 * @param {string} o.dataRemote  로컬이 아는 자료 origin/main 해시
 * @param {?object} o.vm  VM 에서 읽은 값. 못 쟀으면 null
 * @param {string} o.vm.codeHead     VM 의 /opt/hermes/code HEAD
 * @param {string} o.vm.dataHead     VM 의 /opt/hermes/archive HEAD
 * @param {number} o.vm.codeMovedAt  VM 이 그 커밋으로 옮겨간 시각 (epoch 초)
 * @param {number} o.vm.botStartedAt 봇 기동 시각 (epoch 초)
 * @param {number} o.vm.configMtime  VM 의 config.json 파일 시각 (epoch 초)
 * @param {string} o.vm.active       systemctl is-active 결과
 * @param {string} o.vmWhy  vm 이 null 일 때 왜 못 쟀는지
 * @returns {{level: 'ok'|'info'|'bad'|'unknown', text: string}[]}
 */
export function judgeDeploy(o) {
  const out = [];
  const bad = (t) => out.push({ level: 'bad', text: t });

  if (o.codeUnpushed > 0) {
    bad(`코드 저장소에 안 나간 커밋 ${o.codeUnpushed}개 — push 하지 않으면 VM 이 받을 것이 없습니다.`);
  }
  if (o.dataUnpushed > 0) {
    bad(`자료 저장소에 안 나간 커밋 ${o.dataUnpushed}개 — push 하지 않으면 봇이 옛 자료로 답합니다.`);
  }

  /* **못 쟀으면 거기서 멈춘다.** 통과와 못 잰 것은 다르다 — 0 을 채워 넣고 이어서
   * 판정하면 「다 붙었습니다」가 나온다. */
  if (!o.vm) {
    out.push({ level: 'unknown', text: `VM 을 못 쟀습니다 — ${o.vmWhy}` });
    return out;
  }

  const v = o.vm;
  let codeOk = true;

  if (v.codeHead !== o.codeRemote) {
    codeOk = false;
    bad(`VM 이 옛 코드로 돕니다 — VM ${v.codeHead.slice(0, 7)} / 원격 ${o.codeRemote.slice(0, 7)}. `
      + 'VM 에서: sudo -u hermes git -C /opt/hermes/code pull --ff-only 다음 sudo systemctl restart hermes');
  } else if (v.codeMovedAt > v.botStartedAt) {
    codeOk = false;
    bad('코드는 받았는데 재시작 전입니다 — 지금 도는 것은 옛 코드입니다. '
      + 'VM 에서: sudo systemctl restart hermes');
  }

  if (v.configMtime > v.botStartedAt) {
    codeOk = false;
    bad('설정(config.json)이 바뀌었는데 재시작 전입니다 — 봇은 기동할 때 한 번만 읽습니다. '
      + 'VM 에서: sudo systemctl restart hermes');
  }

  /* 자료는 15분마다 저절로 붙으므로 어긋난 것 자체는 고장이 아니다. 다만 방금 push 하고
   * 바로 물어본 사람이 「왜 아직이지」를 알 수 있게 알려는 준다. */
  if (v.dataHead !== o.dataRemote) {
    out.push({
      level: 'info',
      text: '자료가 VM 에 아직 안 붙었습니다 — 15분마다 저절로 받아갑니다. '
        + '지금 붙이려면 VM 에서: sudo systemctl start hermes-archive-pull',
    });
  }

  if (v.active !== 'active') {
    codeOk = false;
    bad(`봇이 안 돌고 있습니다 — systemctl 상태가 ${v.active} 입니다.`);
  }

  if (codeOk && o.codeUnpushed === 0 && o.dataUnpushed === 0) {
    out.push({ level: 'ok', text: `VM 이 지금 원격의 코드로 붙었습니다 — ${o.codeRemote.slice(0, 7)}.` });
  }
  return out;
}
