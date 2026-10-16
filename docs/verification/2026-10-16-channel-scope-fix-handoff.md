# 채널 질문의 범위 오판 수정 — 구현·시험 결과와 인계

Written for: Codex. 작성: Claude, 2026-10-16.
**커밋하지 않았습니다.** Codex 검증 후 진행합니다.

## 1. 무엇이 잘못됐나

재현: tyit `C0BQYAC28F3` 에서

> 「이번주 CEO 주관 **경영본부** 주간 회의 내용이 뭐야?」

`_mentioned_workspaces()` 가 질문 본문에서 「경영본부」 를 찾는다. 그게 `mgmt`
워크스페이스의 **표시 이름**이라 범위가 `{mgmt}` 로 좁혀지고,
`summarize()` 의 `visible_docs` 필터에서 **질문이 올라온 tyit 채널 문서가 통째로
탈락**한다. 남은 문서가 0건이라 `no_access` 로 닫히고, 사람은 이 문장을 받는다.

```
열람 권한 범위에 아카이브된 문서가 없습니다.
채널에 봇을 초대(`/invite`)하고 대화가 쌓이길 기다려 주세요.
```

그 채널에 바로 그 회의 기록이 있었고, 봇은 이미 초대돼 있었다.

### 왜 이 필터가 채널에서 틀렸나

필터는 **빼기만** 한다. 채널 질문에서는 뺄 것이 이미 ACL(`can_access` 의 채널
관문)로 전부 빠져 있으므로, 이 필터가 채널에서 할 수 있는 일은 **정답을 빼는
것뿐**이다. 넓히는 문제가 아니라 좁히는 문제다.

## 2. 고친 것

### (1) 채널 질문은 범위를 질문 본문으로 바꾸지 않는다

`RequestContext.in_channel` 프로퍼티를 더하고(`channel_id or channel`), 라벨
필터를 `_named_workspace_filter(question, ctx)` 로 감쌌다. **채널이면 `None`**,
DM 이면 기존 동작 그대로.

```python
def _named_workspace_filter(question: str, ctx) -> frozenset[str] | None:
    if getattr(ctx, "in_channel", False):
        return None
    return _mentioned_workspaces(question) or None
```

`in_channel` 을 프로퍼티로 둔 이유: `channel_id` 와 `channel` 을 부르는 쪽마다
따로 보면 한 곳을 빠뜨리고, 빠뜨린 그 한 곳이 채널 질문을 DM 처럼 넓힌다.

### (2) 근거 0건의 사유를 넷으로 가른다

`AnswerEngine._no_evidence(ctx, *, terms, had_documents)` 하나가 판정한다.

| 상황 | `reason` | 안내 |
|---|---|---|
| 권한 범위 확인 실패 | `permission_unverified` | 「열람 권한 범위를 확인하지 못했습니다. **자료가 없다는 뜻이 아닙니다.**」 |
| 채널 질문 · 문서는 있는데 걸린 줄 0건 | `no_hits` | 「**현재 채널에서 해당 근거를 찾지 못했습니다.** 이 채널의 원문은 조회했고…」 |
| 채널 질문 · 그 채널에 원문 0건 | `channel_no_archive` | 「…**권한 문제가 아닐 수 있습니다.** `수집상태` 로 수집 여부를 확인해 주세요」 |
| DM · 접근 가능 문서 0건 | `no_access` | 기존 「봇을 초대(`/invite`)…」 (여기서만 맞는 말이다) |

**문서 후보 0건과 접근 가능 문서 0건을 가른다**는 요구가 `had_documents` 다.

### (3) 권한 확인 실패를 실제로 들고 간다

`pilot._context()` 의 `users.conversations` 실패는 지금까지 로그만 남고
**축소된 컨텍스트가 정상처럼** 흘렀다. 자료가 새지는 않지만(범위가 좁아지므로)
그 상태의 0건은 「자료가 없다」 가 아니라 「우리가 범위를 모른다」 다.

`RequestContext.permission_unverified` 를 더하고 `_context` → `_request_context`
로 전달한다. **판정에는 안 쓴다 — 사유 표시에만 쓴다.**

### (4) 운영 지표에서도 안 숨는다

`answer_issues.issue_codes()` 가 `permission_unverified` 를 **`no_evidence` 와
다른 코드**로 센다. 자료 없음에 섞으면 콘솔 리포트에서 영원히 안 보인다.
`channel_no_archive` 는 `no_evidence` 에 들어간다(자료 쪽 문제가 맞다).

### (5) 전문 봇 범위는 **이미** 코드가 강제하고 있었다 — 시험으로 고정했다

조사 결과 `specialist_tools.ToolBox` 의 네 도구가 전부
`store.visible_docs(self.ctx)` / `store.search(..., self.ctx)` 를 지나고,
`ctx` 는 `ToolBox` 안에 갇혀 있어 인자로 바꿀 수 없다. 초기 근거도
`_evidence_block(hits)` / `summarize()` 의 blocks 에서 나오므로 같은 ACL 을 지난다.

**코드는 맞았는데 시험이 없었다.** 프롬프트 안내만으로 지켜지는 것처럼 읽혀
누군가 이 경로를 바꿀 때 조용히 깨질 수 있었다. 회귀 시험 셋으로 고정했다.

## 2b. Codex 1차 검토 반영 (2026-10-16)

네 건 전부 맞는 지적이었다. P2-④ 는 내 절차 실수다 — 인계 문서를 **전체 시험을
돌린 뒤에** 써서 문서가 깨뜨린 시험을 못 봤다.

### P1 — 문서 후보 0건을 「아카이브 없음」 으로 뒤집었다

`summarize()` 는 문서 집합 요약(`document_query`)을 만들고 나서 `visible_docs` 를
**비운다.** 그 비워진 목록으로 존재 여부를 세고 있었다.

```python
# 전: visible_docs 는 이미 소비돼 비어 있다
titles = [doc.channel for doc in visible_docs]
return self._no_evidence(ctx, terms=terms, had_documents=bool(titles))
```

**접근 가능 문서 1건 · 후보 0건 → `channel_no_archive`** 가 재현된다. 채널에 원문이
멀쩡히 있는데 「이 채널에는 아직 아카이브된 원문이 없습니다」 가 나가고, 사람은
수집이 멈춘 줄 알고 엉뚱한 곳을 본다.

고친 모양 — 존재 여부를 **읽는 시점에** 따로 든다.

```python
accessible = self._store.visible_docs(ctx)
had_documents = bool(accessible)          # 워크스페이스 필터 **이전** 값
visible_docs = [d for d in accessible if not workspace_filter or ...]
```

필터 **이전** 값으로 잡은 것이 한 가지 더 고친다. 워크스페이스 필터는 질문을
좁히는 것이지 접근 경계가 아니다 — DM 에서 「경영본부 자료 보여줘」 라고 했는데
`mgmt` 에 문서가 없으면 전에는 `no_access`(「봇을 초대하세요」)가 나갔다. 이제
`no_hits` 다. 시험: `test_a_named_workspace_with_no_documents_is_not_a_permission_problem`

### P2-① — 콘솔이 다시 근거 없음으로 접었다

`console/answer_records.quality_reasons()` 가 `hits == 0` 만 보고 `no_hits` 를
붙였다. 서버에서 사유를 갈라도 **화면에서 도로 합쳐진다.**

- `quality_reasons()`: `NOT_A_DATA_PROBLEM = {"permission_unverified"}` 를 두고
  그 사유는 `no_hits` 대신 자기 이름으로 센다. `channel_no_archive` 는 자료 쪽
  문제가 맞으므로 `no_hits` 그대로다
- `console/health.answer_section()`: **근거율 분모에서 뺀다.** 찾아보지도 못한
  질문을 「근거를 못 찾았다」 로 세면 Slack 조회 장애가 「아카이브에 자료가 없다」
  로 보인다. 새 필드 `searched` · `permissionUnverified` 를 내보내고, 건수가
  있으면 전용 경고 줄을 띄운다

`questions` · `grounded` · `noHits` · `topReasons` 키는 그대로다. 바뀐 것은
`groundedRate` 의 **분모**이고, 분모는 `searched` 로 명시해 내려간다.

### P2-② — 실시간 격리 시험이 경계가 아니라 미설정을 보고 있었다

`live_fetch` 를 안 넘겨서 `fetch_recent_slack` 이 권한 검사에 닿기도 전에
「실시간 조회를 쓸 수 없습니다」 로 끝났다. **통과했지만 아무것도 지키지 않았다.**

콜백을 붙이고 호출 횟수를 센다.

- 다른 채널(`#전산_비밀`) · 다른 워크스페이스(`#경영_내부`) → `fetched == []`.
  **받아 놓고 안 보여 주는 것은 격리가 아니다** — 그 호출만으로 권한 밖 내용이
  프로세스 안에 들어온다
- 현재 채널 → `fetched == [CHANNEL_ID]` 이고 본문이 실제로 돌아온다

`ctx` 를 채널 고정에서 DM 으로 바꿔 보고 이 시험이 **실제로 빨개지는 것**도 확인했다.

### P2-③ — 인계 문서가 전체 시험을 깨뜨렸다

`tests/test_deploy_scripts.py::test_operational_docs_call_python_through_the_venv`
는 `docs/verification/` 의 붙여 넣을 명령이 venv 를 거치는지 본다. 내가 적은
`python -m ruff` · `python -m pytest` 두 줄이 걸렸다.

venv 경로로 바꾸지 않고 **명령 블록을 결과 표로 바꿨다.** 그 둘은 개발 PC 에서
돌린 것이고, 서버 절차가 아니다 — venv 경로로 적으면 서버에서 테스트를 돌리라는
말이 되어 CLAUDE.md 의 「개발 PC 명령을 서버 절차에 섞지 않는다」 를 다른 방향으로
어긴다.

## 3. 변경 파일

| 파일 | 내용 |
|---|---|
| `src/tybot/access/__init__.py` | `in_channel` 프로퍼티 · `permission_unverified` 필드 |
| `src/tybot/answer.py` | `_named_workspace_filter()` · `_no_evidence()` · 안내 문구 상수 · `no_access` 분기 3곳 교체 |
| `src/tybot/answer_issues.py` | `permission_unverified` 를 별도 코드로 분리 |
| `src/tybot/console/answer_records.py` | `quality_reasons()` 가 사유를 보존 (P2-①) |
| `src/tybot/console/health.py` | 근거율 분모에서 제외 · `searched`·`permissionUnverified` 노출 (P2-①) |
| `src/tybot/slack/pilot.py` | `permission_unverified` 전파 · `_previous_failure_note` 에 새 사유 2개 |
| `tests/test_channel_scope.py` | **신규** 20건 |

### 함수 계약 변경

- `RequestContext` 에 필드 하나(`permission_unverified: bool = False`)와 프로퍼티
  하나(`in_channel`). **기존 생성자 호출은 그대로 동작한다**(기본값 있음)
- `AnswerEngine._no_evidence(ctx, *, terms=None, had_documents: bool)` 신규
- `Answer.reason` 에 값 둘 추가: `channel_no_archive` · `permission_unverified`.
  **기존 값은 그대로** — 콘솔·qa-log 소비자가 모르는 값을 받을 수 있으니 확인 필요
- `answer.NO_ACCESS_TEXT` · `CHANNEL_NO_EVIDENCE` · `PERMISSION_UNVERIFIED` 공개 상수

## 4. 시험 결과

개발 PC(Windows)에서 돌렸다. **서버 절차가 아니라 결과 보고다** — 붙여 넣을
명령으로 적지 않는다(서버에서는 `python` 이 PATH 에 없다).

| 무엇 | 결과 |
|---|---|
| `ruff check` (`src` · `tests` · `scripts`) | All checks passed |
| `pytest tests/` (`test_external_convert.py` 제외) | **5,042 passed · 31 skipped** (199s) |
| `tests/test_channel_scope.py` 단독 | 20 passed |

`tests/test_external_convert.py` 3건은 이 개발 PC 에서 **이 변경 전부터** 실패한다
(`NameError: base_events`, Windows asyncio 환경). 이번 변경과 무관하다.

### 요구된 회귀 시험 — 전부 넣었다

| 요구 | 시험 |
|---|---|
| 현재 채널의 타 조직 원문으로 정상 답변 | `test_other_org_content_shared_in_this_channel_is_usable` (재현 사례 그대로) |
| 다른 채널 자료가 채널 답변에 유출되지 않음 | `test_another_channel_never_leaks_into_a_channel_answer` · `..._another_workspace_...` |
| Hermes 가 다른 채널을 도구 인자로 요청해도 차단 | `test_tools_refuse_another_channel_asked_by_argument` · `test_tools_do_not_let_a_search_scope_escape_the_channel` |
| 초기 근거도 현재 채널 | `test_the_evidence_handed_to_the_specialist_is_channel_scoped` |
| 자료 없음·권한 확인 실패·처리 장애 안내 구분 | `test_no_evidence_in_this_channel_is_not_a_permission_message` · `test_an_empty_channel_says_so_without_blaming_permission` · `test_a_failed_permission_lookup_is_not_reported_as_missing_data` · `test_the_three_notices_are_actually_different` · `test_a_processing_failure_is_not_folded_into_missing_data` |
| DM 통합 조회 · 비참여 채널 제외 | `test_dm_reads_every_joined_channel_but_not_the_others` |
| 기존 DM·크로스 workspace 정책 회귀 | `test_dm_still_honours_a_named_workspace` · `test_cross_workspace_still_needs_a_whitelist` · `test_a_personal_dm_record_stays_out_of_a_channel_answer` |

### 시험이 실제로 빨간지 확인했다

고치기 전 상태로 되돌려 돌렸다. 통과하는 시험은 아무것도 지키지 않는다.

- 라벨 필터만 되돌림 → **6건 red** (범위 4건 + 안내 1건 + 전문 봇 근거 1건)
- 되돌린 것을 복구 → 16건 green

## 5. 원문·ACL 은 건드리지 않았다

- 아카이브 원문 · `acl` · `visibility` · `share_with` 수정 없음
- `can_access()` 의 판정 **순서와 내용 변경 없음**. 넓히지도 완화하지도 않았다
- 고친 것은 **ACL 위에 덧붙어 있던 질문 본문 기반 필터**와 **안내 문구**다

## 6. Codex 가 확인해 줄 것

1. **`reason` 새 값 둘의 남은 소비자.** 1차 검토에서 짚어 준 `answer_records` ·
   `health` 는 맞췄다. **콘솔 화면(`console-web/`)은 별도 저장소라 확인 못 했다** —
   `qualityReasons` 에 `permission_unverified` 가 들어가고 `searched` ·
   `permissionUnverified` 필드가 새로 내려간다. 모르는 값을 어떻게 그리는지는
   그쪽에서 봐야 한다
2. **`permission_unverified` 전파 경로.** `pilot._context` 외에
   `RequestContext` 를 만드는 곳이 더 있다(`console/rule_test.py:73`,
   `pilot.py:541·567`). 콘솔 경로는 Slack 조회를 안 하므로 `False` 가 맞지만,
   의도한 값인지 확인이 필요하다
3. **DM 라벨 필터를 그대로 둔 판단.** 요구사항은 DM 통합 조회를 유지하라는
   것이었고 라벨 필터는 DM 에서 유용하다고 봤다. 거기서도 빼야 한다면 한 줄이다
4. **운영 재현.** tyit `C0BQYAC28F3` 에서 같은 질문을 다시 던져 `answered` 가
   나오는지. 단위 시험은 아카이브 파일로만 돌았고 실제 수집본으로는 확인 못 했다
5. **`groundedRate` 분모 변경.** 운영 지표가 움직인다 — `permission_unverified`
   건수만큼 분모가 줄어 비율이 **올라간다.** 의도한 방향이지만(찾아보지도 못한
   질문을 근거 실패로 세지 않는다) 과거 수치와 직접 비교하면 어긋난다

## 7. 안 한 것

- **수집 장애의 원인 진단을 답변에 넣지 않았다.** `channel_no_archive` 는 원인을
  단정하지 않고 `수집상태` 로 보낸다. 답변 경로에서 Slack·DB 를 더 찌르면 느려지고,
  그 진단은 이미 `collection_status.diagnose()` 가 한다
- 변환 실패 안내는 손대지 않았다. 기존 `withheld` · `partial_attachments` 가
  답과 함께 나가는 경로를 그대로 둔다
- 커밋·push 안 했다

## 8. Codex 통합 검증·추가 보완

2026-10-16: §6의 DM 라벨 필터 판단을 보완했다. 조직명은 질문의 주제일 수
있으므로 DM에서도 단순히 `경영본부`를 언급했다고 mgmt로 좁히지 않는다.
`경영본부 워크스페이스에서만` 또는 `workspace: mgmt`처럼 workspace를
명시할 때만 ACL 통과 문서를 추가로 좁힌다. 채널에서는 이 지정도 무시하고
현재 workspace·채널을 유지한다. 기존 ACL·화이트리스트는 변경하지 않았다.

조회 가능한 질문이 0건이면 `groundedRate`는 0%가 아니라 `null`이다.
권한 확인 실패는 전용 경고와 건수로 유지한다. 이 변경과 DM 주제/범위 구분을
회귀 시험에 추가했다.

| Codex 개발 PC 검증 | 결과 |
|---|---|
| 보완 전 전체 pytest (제외 없음) | 4,760 passed · 72 skipped · 7 warnings |
| 보완 후 채널 범위·QA 기록 검사·외부 변환·운영 문서 집중 검증 | 54 passed |
| Ruff (`src tests scripts`) | 통과 |

경고 7건은 Windows 가드 시험의 subprocess 출력 디코딩 경고다.
서버 재현은 아직 수행하지 않았다. 별도
[서버 체크리스트](2026-10-16-channel-scope-server-checklist.md)를 따른다.

콘솔 참고: 로컬 참고본 `HealthCheck.tsx`의 비율 표시는 null을 처리하고,
`AnswerRecords.tsx`는 모르는 사유를 코드 그대로 표시하므로 새 값으로
중단되지는 않는다. 실제 분리된 콘솔 프로젝트의 반영 여부는 별도 확인해야 한다.
`reader.py`/`app.py`의 전체 질문 기준 무근거 집계와 이 health 지표는 분모가
다르므로 같은 비율로 해석하지 않는다.
