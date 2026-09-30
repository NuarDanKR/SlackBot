# 기존 TYBot 아카이브 오프라인 이관 — 계획·dry-run·검증

작성: 2026-09-30
대상: TYBot 운영자, Claude Code / Codex 구현 담당
상태: **계획 도구만 구현됨.** 한 건도 옮기지 않았다

코드: `src/tybot/archive/legacy_migrate.py` · CLI: `scripts/plan_legacy_migration.py`
시험: `tests/test_legacy_migrate.py`
관련: [Master 수집 분리](master-collection-handover.md) ·
[단일 Supervisor·소급 수집](archiver-supervisor-backfill-console-2026-09-29.md)

## 0. 이 단계가 하는 일

운영 아카이브(`/var/lib/tybot/archive`)를 **읽기만** 해서, 새 채널별 구조로 옮기면
어떻게 되는지 세고 보고서를 낸다.

옮기지 않는다. `--apply` 같은 스위치가 **없는 것이 의도다** — 계획을 세우는 명령과
옮기는 명령이 같으면, 보기만 하려던 실행이 옮겨 버리는 날이 온다.

`legacy_migrate.py` 에는 파일을 만드는 코드가 하나도 없고, 시험이 그것을 구문
나무로 확인한다(`test_the_module_never_writes`).

## 1. 두 구조

### 지금 (운영)

```text
archive/workspaces/<ws>/channels/<ID>__<이름>/raw/<날짜>.md   ← 사람 원문
archive/../objects/workspaces/<ws>/channels/<ID>/attachments/  ← 첨부 정본(형제)
archive/channels/<ws>/<이름>.md                                ← v1 평면 구조
```

### 새 구조 (`shadow_paths`, per-channel-v1)

```text
<root>/<ws>/<채널ID>__<이름>/archive/raw/<날짜>.md
<root>/<ws>/<채널ID>__<이름>/objects/
<root>/<ws>/<채널ID>__<이름>/staging/
```

첨부가 채널 디렉터리 **안**으로 들어온다. 지금은 archive 의 형제라, 채널 하나를
떼어 내거나 지우려면 두 자리를 같이 다뤄야 한다.

## 2. 무엇을 검증하나 — 보고서 네 절

| 절 | 무엇을 세나 | 왜 |
|---|---|---|
| **내용** | 문서 수, 원문 줄 수, 새로 옮길 줄 / 소급과 겹치는 줄, 못 읽은 문서 | 옮긴 뒤 「몇 줄이 어디로 갔나」 를 말할 수 있어야 한다 |
| **권한** | 공개·비공개, ACL 이 빈 문서, 타 워크스페이스 공유, DM 문서 수 | 옮기면서 **넓어지는 것**이 없어야 한다(절대 원칙 3·4) |
| **출처** | 좌표 있는 줄 / 없는 줄, 채널 ID 없는 문서, schema v1·v2 | 좌표가 곧 대조 가능성이다 |
| **첨부** | 첨부 표시 줄, 파일 ID 로 이어진 것, 이름으로만 이어진 줄 | ID 없는 줄은 옮긴 뒤 잘못 이어질 수 있다 |

보고서에는 **본문이 들어가지 않는다.** 들어가면 그 보고서가 또 하나의 아카이브가
되고, 권한이 안 붙은 사본이 하나 더 생긴다. 시험이 이것도 고정한다.

## 3. 좌표가 없으면 합치지 않는다

소급 수집이 이미 가져온 메시지와 옛 아카이브의 같은 메시지를 대조해야 한다. 기준은
**`(workspace, channel_id, message_ts)` 세 값**뿐이다.

셋을 다 쓰는 이유가 각각 있다.

- `workspace` 를 빼면 남의 워크스페이스 자료가 우리 중복으로 세어진다(원칙 4)
- `channel_id` 를 빼면 **다른 채널의 같은 시각**이 같은 메시지가 된다
- `message_ts` 는 Slack 이 준 좌표다. 이것이 없으면 짝지을 근거가 없다

옛 줄에는 `message_ts` 가 없는 것이 많다(좌표를 남기기 시작하기 전 자료). 그런
줄은 **어느 것과도 짝짓지 않는다.** 시각과 화자가 비슷하다고 같은 메시지로 보는
순간 둘 중 하나가 일어난다.

- 다른 메시지를 같은 것으로 보고 **지운다**
- 같은 메시지를 다른 것으로 보고 **둘로 남긴다**

둘 다 오류를 내지 않는다. 그래서 「모른다」 를 그대로 들고 가고, 보고서가
`uncoordinatedLines` 로 그 수를 말한다. 같은 대화가 두 벌 남을 수 있다는 뜻이고,
그것을 합치는 판단은 사람이 원문을 보고 한다.

## 4. 채널 ID 가 없는 문서는 막는다

새 구조의 디렉터리 이름이 Slack 채널 ID 다. v1 문서에는 `channel_id` 가 없고,
`writer._stable_channel_id()` 가 채널명 해시로 `legacy-<12자리>` 를 만든다.

그 값은 새 구조의 규칙을 **통과하지 못한다** — `shadow_paths.channel_root` 는
`legacy-[a-f0-9]{16}` 를 기대하는데 실제 값은 12자리다.

```python
legacy-25f4047faf16   # writer 가 만드는 값 (12자리)
legacy-[a-f0-9]{16}   # shadow_paths 가 받는 모양
```

**맞춰 주지 않는다.** 자릿수를 늘리는 것은 쉽지만, 그러면 채널명에서 만든 가짜 ID
가 새 구조의 정식 이름이 된다. 나중에 그 채널의 실제 Slack ID 로 자료가 들어오면
같은 채널이 **두 디렉터리**로 갈리고, 그때는 누구도 둘이 같은 채널이었다는 것을
모른다.

막고, 보고서가 그 목록을 든다. 사람이 실제 채널 ID 를 찾아 주면 그때 옮긴다.

계획 쪽 정규식과 목적지 쪽 정규식이 갈리지 않게, 시험이 두 파일의 규칙이 같은지
본다(`test_the_id_rule_matches_the_destination_rule`).

## 5. 막는 다른 경우

| 무엇 | 왜 |
|---|---|
| 비공개인데 ACL 이 빈 문서 | 그대로 옮기면 권한이 조용히 넓어진다 |
| 목적지가 원본 안팎으로 겹침 | 어느 쪽이 원본인지 알 수 없게 된다 |
| 목적지가 운영 archive 와 겹침 | 이 도구는 운영 아카이브에 쓰지 않는다 |
| 상대경로 | 실행한 디렉터리에 따라 다른 자리를 가리킨다 |
| 형식 검사를 통과 못 하는 문서 | 건너뛰지 않고 **목록에 남긴다.** 조용히 빠지면 옮긴 뒤에도 모른다 |

DM 문서는 목록에 넣지 않는다. 그 사람 한 명의 기록이고 새 구조는 채널 단위다.
넣으면 이 도구가 그 파일을 열게 되고, 그 순간 보고서가 개인 기록을 읽은 셈이 된다.
**수만 세고 열지 않는다.**

## 6. 쓰는 법

```bash
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/plan_legacy_migration.py \
    --source /var/lib/tybot/archive \
    --destination /var/lib/tybot/archiver-shadow/archive \
    --live-archive /var/lib/tybot/archive \
    --collected /var/lib/tybot/archiver-shadow/archive
```

`--source` 와 `--live-archive` 가 같아도 된다. 원본은 읽기만 하고, **목적지**가 그
경로와 겹치면 거절한다.

`--collected` 를 빼면 소급 결과와 대조하지 않는다. 그때 `duplicateLines` 는 0 으로
나오는데, **겹치는 것이 없다는 뜻이 아니라 세지 않았다는 뜻**이다. 사람 출력은 그
경우 경고를 붙인다.

## 7. 아직 안 한 것

- **옮기는 실행기.** 계획과 보고서를 사람이 본 뒤의 별도 단계다. `shadow_root.py`
  처럼 계획·복사·해시 검증을 나누고, 원본은 검증 기간 동안 지우지 않는다
- **첨부 실체 대조.** 지금은 원문의 첨부 **표시 줄**만 센다. `objects/` 에 그 파일이
  실제로 있는지, 옮기면 어디로 가는지는 실행기와 같이 만든다
- **채널 ID 복구.** 막힌 v1 문서에 실제 Slack 채널 ID 를 붙이는 표. 사람이 채널명을
  보고 정하는 일이라 자동화하지 않는다
- **콘솔 화면.** CLI 먼저 만들었다. 옮기는 실행기가 생길 때 같이 붙인다
  (CLAUDE.md 「기능을 만들면 콘솔 화면과 같이 만든다」)

## 8. 이 단계에서 하지 않은 것

운영 DB 변경, 운영 아카이브 수정, Master 수집 중지, live 전환, 채널 `active` 전환.
스키마도 안 바꿨다 — release gate 지문 변화 없음.
