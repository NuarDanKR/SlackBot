# 채널 범위 서버 검증

작성: Codex, 2026-10-16. 수신: 서버 운영자와 Claude.
Claude의 답변·권한 코드 수정과 별도로 사용하는 배포 전후 검증 절차다.
서버에서 실제 실행한 결과는 아직 없다. 아래 기준 자료는 사용자가 제공한 출력이다.
명령은 조회 전용이다. Slack 질문은 사람이 직접 실행하며 모델 비용·답변 발송이 발생한다.

## 기준 자료

대상은 `tyit`, 채널 `C0BQYAC28F3`이다. workspace 표시 이름은 `tyit=전산팀`,
`mgmt=경영본부`다. 2026-10-16 10:57:24 KST의 질문은 다음과 같이 종료됐다.

```text
이번주 CEO 주관 경영본부 주간 회의 내용이 뭐야?
intent=summary/llm reason=no_access hits=0 model=None
```

사용자가 확인한 운영 문서는 채널 ID·workspace·ACL이 일치하고 정상 파싱된다.
10월 7일 원문은 6줄, 10월 16일 원문은 9줄이었다. 10월 16일 파일의 최종 수정은
11:05이므로 이 수치로 10:57 당시 원문 내용을 소급 확정하지 않는다.
의도한 범위는 현재 채널이며, 본문에 등장한 타 조직명으로 범위를 바꾸지 않는다.

## 배포 버전과 서비스

변경 전후 각각 실행해 배포 커밋과 프로세스 상태를 기록한다. Git checkout의 HEAD가
아니라 실제 `/opt/tybot` 배포 표식을 확인한다. 서비스 재시작이나 권한 변경은 하지 않는다.

```bash
sudo cat /opt/tybot/.deployed-commit
sudo systemctl show tybot.service hermes-rpc-shared.service \
  tybot-archiving-shadow@tyit.service --no-pager \
  -p Id -p ActiveState -p SubState -p ExecMainStatus -p NRestarts -p MainPID
```

`active/running`은 프로세스 상태일 뿐 실제 근거 조회 성공을 증명하지 않는다.
실패 원인이 알려지기 전에는 설정·ACL·원문·라우트를 변경하지 않는다.

## 검증표

같은 스레드의 이전 질문이 범위에 영향을 주지 않도록 새 최상위 메시지로 시작한다.
각 질문의 요청 ts와 `decision_id`를 구분해 기록한다. 실자료 본문·인명·파일명은
검증 보고서에 복제하지 않고 합격 여부와 사유 코드·workspace/채널 ID만 남긴다.

| 경우 | 확인 기준 |
|---|---|
| 현재 채널에서 기존 질문 재실행 | 공유된 원문을 읽고 답변. `mgmt` 조직명이 현재 `tyit` 문서를 제외하지 않음. 실제 변환/조회 실패라면 해당 실패를 구분 |
| 현재 채널에 없는 주제 | 현재 채널에서 근거를 찾지 못했다고 안내. 존재하지 않는 다른 채널 자료를 추측하지 않음 |
| 본인 참여 다른 채널에만 있는 주제 | 채널 질문에서는 그 채널 근거가 답변에 섞이지 않음. 질문 문장에도 민감한 다른 채널 내용을 싣지 않음 |
| TYBot DM에서 조직명을 주제로 언급 | 참여 채널들에 공유된 해당 주제를 조회. 조직명 언급만으로 다른 workspace 하나로 줄이지 않음 |
| TYBot DM에서 workspace만 명시적으로 지정 | 기존 whitelist·share 정책·참여 범위 안에서만 선택. 조직명인 주제와 범위 지정 지시를 구분 |
| 문서는 있으나 문서 후보만 없음 | `no_hits`. 원문 전체가 없다는 `channel_no_archive`로 오판하지 않음 |
| 실제 권한 확인 실패·변환/전문봇 장애 | 일반 자료 없음으로 숨기지 않음. 운영 토큰 회수·권한 해제로 시험하지 않고 mock 회귀시험으로 검증 |

근거가 준비되지 않았는데 정상 답변을 강요하지 않는다. 정답의 내용과 출처 원문 일치는
사람이 확인한다. 실패 사유·채널 범위·PF 호출 여부는 별도 기준으로 판정한다.

## 요청 로그와 PF 호출

질문 직전에 셸에서 시작 시각을 잡고, 답변 완료 후 두 로그를 확인한다.

```bash
CHECK_SINCE=$(TZ=Asia/Seoul date --iso-8601=seconds)
```

```bash
sudo journalctl -u tybot.service --since "$CHECK_SINCE" --no-pager \
  | grep -E 'specialist_result=|qa user='
sudo journalctl -u hermes-rpc-shared.service --since "$CHECK_SINCE" --no-pager \
  | grep '\[rpc\] 완료'
```

동시에 처리한 다른 요청을 같은 요청으로 오인하지 않는다. `final_responder=hermes`나
모델명만으로 PF RPC라고 판정하지 않는다. 같은 `decision_id`의 `specialist_call`에서
`workspace`, `result`, `routing_reason`의 `phase=external-v3`를 대조한다.
worker의 해당 workspace 완료 로그는 보조 증거이며, 같은 시간대라는 이유만으로
특정 QA 요청과 일대일 대응했다고 단정하지 않는다. 원문이 포함될 수 있는 전체 로그를
저장소나 외부 인계에 붙이지 않는다.

## 저장된 근거 좌표 확인

새 검사기가 배포된 뒤 관리자 권한으로 실행한다. 이 도구는 Python 표준 라이브러리만
사용하고 환경파일·자격증명·아카이브는 읽지 않는다. QA 파일을 읽을 뿐 원문·질문·답변·
파일 경로는 출력하지 않는다. 파일을 생성하거나 덮어쓰지도 않는다.

```bash
sudo -u tybot /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/inspect_channel_scope_record.py \
  --qa-file /var/lib/tybot/qa-log/qa-2026-10.jsonl \
  --workspace tyit --channel C0BQYAC28F3 --latest
```

`--latest`는 해당 채널의 가장 최근 요청 ts를 고른다. 다른 사람이 먼저 질문했을 수
있으므로 출력의 `request_ts`가 방금 질문과 같은지 확인한다. 특정 요청을 재검증할 때는
`--latest` 대신 `--request-ts`에 실제 요청 ts를 지정한다. 월이 바뀌면 QA 파일도 바꾼다.

- 종료 0, `recorded_refs_in_channel`: 저장된 참조 좌표가 현재 채널 안에 있음.
- 종료 1, `outside_channel_reference`: 외부 workspace/채널 좌표가 기록됨. 배포 확대 중단.
- 종료 2: 답변 실패·참조 없음·깨진 기록·중복 요청·조회 오류 등으로 합격 판정 불가.
- `rpc_verified=false`, `all_tool_reads_verified=false`: 이 도구가 검증하지 않는 범위다.
  저장되지 않은 중간 도구 읽기·현재 Slack 권한·출처 내용의 정확성은 증명하지 못한다.

기록된 참조만 현재 채널이어도 중간 도구가 다른 자료를 읽었을 가능성을 배제할 수는
없다. 활성 `live_fetch`를 붙인 채널 거절 시험, broker 세션 격리 시험과 함께 판단한다.
DM은 현재 참여 권한과 공유 정책을 별도로 확인해야 하므로 이 검사기는 DM 채널을 거부한다.

## 완료 기록

배포 커밋, 검증 시각(KST), 요청 ts, decision ID, 답변 사유, PF 경로 확인 결과,
근거 좌표 검사 상태, 사람의 출처 확인 결과를 기록한다. 로컬 fixture 통과와 실제 서버
성공을 구분하고, 아직 실행하지 않은 항목은 미검증으로 남긴다.
