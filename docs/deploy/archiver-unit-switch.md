# Archiver 유닛 전환 — `tybot-archiving-shadow@` → `tybot-archiver@` (B-99 2단계)

기준: [BACKLOG B-99](../../BACKLOG.md#b-99) 2단계 설계 결정 D1–D7.
이 문서가 [그림자 수집 준비](archiving-shadow.md)를 대체한다. 그 문서는 기록으로만 남긴다.

## 무엇이 바뀌나

| 전 | 후 |
|---|---|
| 유닛 `tybot-archiving-shadow@<ws>` | `tybot-archiver@<ws>` (`Conflicts=` 로 옛 유닛과 동시에 돌지 않는다) |
| 섀도 루트(`ARCHIVER_SHADOW_DIR`)와 운영 루트 둘 | 운영 루트 `ARCHIVE_DIR` 하나 |
| `LOCK_DIR=/var/lib/tybot/archiver-shadow/<ws>/.locks` | `/var/lib/tybot/archiver/<ws>/.locks` |
| Archiver DM 인계 소비자, Master DM 인계 거울 | 없음. DM 원문은 Master 가 운영 루트에 직접 쓴다(B-99 1단계) |
| 채널 모드 `off·shadow·active·paused` | `off·active·paused`. 옛 `shadow` 행은 수집하지 않는다 |

**지우지 않는 것.** `/var/lib/tybot/archiver-shadow` 데이터, DM 인계 대기열
(`/var/lib/tybot/state/dm-handoff`)은 그대로 둔다. 보존 기한 집행(`retention`)이 대기열을
계속 정리한다.

## 순서

서버 명령은 venv 와 서비스 계정 기준이다(CLAUDE.md). 한 단계씩 실행하고 결과를 확인한 뒤
다음으로 간다.

1. **1단계 배포가 먼저다.** `ls /var/lib/tybot/archive/workspaces` 결과가 「없음」 이어야 한다.
   있으면 B-99 임시 조치대로 격리 폴더로 **옮긴다**(삭제 금지).
2. 코드를 반영한다(평소 `update.sh` 절차).
3. 유닛·디렉터리를 설치한다. `tybot-archiver@.service` 가 깔리고
   `/var/lib/tybot/archiver` 가 만들어진다.

   ```bash
   sudo /opt/tybot/deploy/install.sh
   ```

4. 스키마를 적용한다. `shadow` 행이 남아 있으면 CHECK 축소는 건너뛰고 NOTICE 만 남긴다.

   ```bash
   sudo /opt/tybot/deploy/apply-schema.sh
   ```

5. 부팅 때 옛 유닛 이름으로 띄우는 링크가 있는지 본다. 결과가 있으면 그 링크를 새 이름으로
   바꿔야 재부팅 뒤에도 같은 상태로 뜬다.

   ```bash
   ls -l /etc/systemd/system/*.wants/ | grep -i archiving-shadow
   ```

6. **워크스페이스 하나씩** 새 유닛으로 띄운다. `Conflicts=` 때문에 새 유닛을 시작하면 옛
   유닛이 멈춘다.

   ```bash
   sudo systemctl start tybot-archiver@tyit
   systemctl is-active tybot-archiver@tyit tybot-archiving-shadow@tyit   # active / inactive
   journalctl -u tybot-archiver@tyit --since "-5min" --no-pager | tail -40
   ```

   로그에 `retired setting ignored (B-99 stage 2): …` 가 나오면 그 설정을
   `/etc/tybot/archiver.env` 에서 지울 대상으로 적어 둔다(지금 지우지 않아도 동작은 같다).
   `ARCHIVER_LIVE_WRITES_ENABLED is off` 로 기동이 거부되면 그 값을 지운다 — 이제 운영
   쓰기가 아닌 모드는 없다.

7. 나머지 워크스페이스(civil·mgmt·archi·invest)도 6을 반복한다.
8. 다섯 개가 모두 `active` 이면 옛 유닛 파일을 지우고 대조한다.

   ```bash
   sudo rm /etc/systemd/system/tybot-archiving-shadow@.service
   sudo systemctl daemon-reload
   sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_unit_drift.py
   ```

   옛 유닛이 남아 있으면 `retired-present` 로 나온다.

## 복구 스크립트

`scripts/recover_archiver_files.py --apply` 는 수집기와 같은 잠금(`archiving-bot-<ws>`)을 잡는다.
잠금은 `LOCK_DIR` 기준이므로 새 유닛과 같은 값으로 실행해야 실행 중인 수집기를 막는다.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env LOCK_DIR=/var/lib/tybot/archiver/tyit/.locks \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/recover_archiver_files.py ...
```

## 되돌리기

새 유닛을 멈추고 옛 유닛을 시작하면 **옛 코드가 필요하다** — 이 코드에는 섀도 루트 경로가
없다. 되돌릴 때는 코드도 이전 커밋으로 함께 되돌린다.
