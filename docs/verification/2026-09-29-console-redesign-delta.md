# 콘솔 개편 — inventory 이후 달라진 것

작성: 2026-09-29 · 기준 커밋 `fc41603` · 단계: [구현 사양](../design/workspace-service-console-redesign.md) §12.2 **1단계(재확인)**

대상: §12.2 3~10단계를 구현하는 개발자와 AI 에이전트

[2026-09-28 inventory](2026-09-28-console-redesign-inventory.md) 를 다시 쓰지 않는다.
**그 뒤에 달라진 것만** 적는다. 달라진 줄 모르고 옛 지도를 따라가면, 이미 있는 표를
다시 만들거나 남이 쓰는 화면을 덮는다.

---

## 1. 2단계 스키마가 이미 들어와 있다

`7f92b75`(스키마·이관) → `685e52f`(QA 수정) 로 `bot_catalog` · `bot_connection` ·
`bot_connection_secret` · `specialist_route` 가 `deploy/sql/bot_connection_schema.sql`
에 있다. **3단계는 이 표를 쓰는 것이지 만드는 것이 아니다.**

## 2. 격리 DB 검증이 실제로 돌았다 — 그 결과로 네 커밋

inventory 를 쓸 때는 「돌려야 한다」 였다. 지금은 돌았고, 거기서 나온 수정이 있다.

| 커밋 | 무엇이 틀렸었나 |
|---|---|
| `b08366f` | 내가 쓴 픽스처가 `specialist_bot.name` 의 `btrim(name) <> ''` CHECK 를 어겼다. 빈 이름은 **애초에 들어갈 수 없다** — 카탈로그 backfill 의 `coalesce(nullif(btrim(s.name), ''), s.key)` 는 그래서 닿지 않는 가지다(해롭지는 않다) |
| `5a167a2` | 격리 시험이 앞 시험이 만든 상태에 기대고 있었다. 각자 `_prepared` 로 비우고 시작한다 |
| `ea37855` | 깨끗한 DB 에는 `pg_trgm` 도 없어 fallback 인덱스가 못 섰다. `index_schema.sql` 이 확장을 먼저 만든다 |
| `008dba1` | preflight 의 「남의 표」 판정이 접두어만 봤다. `bot_catalog`·`bot_connection`·`channel` 같은 접두어 없는 우리 표를 남의 것으로 읽어 검증이 시작조차 안 됐다 |

**`ea37855` 가 `index_schema.sql` 을 고쳤다.** 그 파일은 release gate 대상
(`release_gate.GATED_SQL`)이므로 **검증 지문이 이미 바뀌었다.** 그 전에 받아 둔
검증 artifact 가 있다면 무효다 — 다시 받아야 게이트가 열린다.

## 3. PR #1 — Slack 라이선스 현황 (`6f4f09a`)

이 작업이 **보존해야 하는 것**이다. 건드리지 않는다.

| 파일 | 무엇 |
|---|---|
| `src/tybot/console/license_routes.py` · `license_store.py` | `GET`·`PUT /api/licenses` |
| `console-web/src/pages/Licenses.tsx` · `Licenses.css` | 화면 |
| `deploy/sql/slack_license_schema.sql` | `slack_license_allocation` |
| `console-web/src/App.tsx` | 메뉴 `운영 > 라이선스 현황`(`/manage/licenses`, admin 전용) 1줄 + import·라우팅 각 1줄 |
| `src/tybot/console/app.py` | `include_router` 2줄(파일 끝, 정적 마운트 **앞**) |

**그리고 이 PR 이 좋은 선례를 남겼다:** 새 API 를 `app.py` 에 쌓지 않고 라우터
모듈로 두고, `app.py` 에는 등록 두 줄만 남긴다. 봇 관리 API 도 같은 모양으로 둔다 —
`app.py` 는 이미 3000줄이 넘고, 거기에 계속 쌓으면 충돌 지점이 한 파일에 몰린다.

### 짚어 둘 것 — 라이선스 스키마는 게이트 밖이다

`slack_license_schema.sql` 은 `verify_schema_isolated.TARGET_FILES` 에도
`release_gate.GATED_SQL` 에도 없다. 즉 **격리 DB 검증을 한 번도 받지 않는다**
(inventory §6 이 경고한 것과 같은 자리).

이번 작업에서 **고치지 않는다.** 넣으면 지문이 또 바뀌고 재검증이 필요해지는데,
그건 이 표의 주인이 정할 일이다. 사실만 남긴다.

## 4. 그대로인 것

inventory §2~§7 의 나머지는 **하나도 안 바뀌었다.** 확인한 것:

- `workspace_service_store.py` · `workspace_service_identity.py` — 변경 없음
- `specialist_store.py` — 배정을 여전히 전량 `DELETE` 후 재삽입한다
  (inventory §7-4·§7-7 이 3단계에 넘긴 요구가 그대로 유효하다)
- `console-web/src/components/ArchivingPanel.tsx` · `pages/Workspaces.tsx` — 변경 없음
- `runtime_workspaces()` 가 여전히 `workspace_secret` 을 읽는다(Master 기동 경로)

---

## 5. 이번 작업(3~4단계)의 경계

- **표를 만들지 않는다.** `deploy/sql` 의 게이트 대상 파일을 이번에 바꾸지 않는다 —
  바꾸면 지문이 달라져 격리 DB 재검증이 필요해진다
- 정체성(`bot_catalog`)과 런타임 상태(`specialist_bot`·`specialist_deployment`)를
  **합치지 않는다.** read model 이 나란히 보여 줄 뿐이다
- 기존 `/api/workspaces/{key}/archiving/services/*` 는 **호출부가 사라질 때까지**
  살려 둔다. 지금 부르는 곳은 `ArchivingPanel.tsx` 한 곳이다
- 연결을 저장하거나 검증해도 Archiver 수집·채널 `active`·writer 인수는 켜지지 않는다.
  그 판정은 `archiving_state` 와 release gate 가 계속 소유한다
