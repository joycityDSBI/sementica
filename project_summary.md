# Semantica — 프로젝트 전체 요약

> JoyCity 전략사업본부 Notion 기반 온톨로지 검색 솔루션  
> 최종 업데이트: 2026-09-10 (40문항 골든셋 **0.963** — 담당자·정책·관계 카테고리 만점)

---

## 1. 프로젝트 목표

Notion에 축적된 전략사업본부의 지식(게임 운영 이력, 의사결정 맥락, 팀 구조 등)을 **의미 기반**으로 검색·추론할 수 있도록 구조화하고, Snowflake Cortex와 연동하여 자연어 질의로 인사이트를 도출하는 시스템.

```
Notion 문서 → 전처리·임베딩 → Qdrant(벡터) + FalkorDB(그래프)
                                        ↓
                        MCP 서버 (Claude Desktop / Cursor)
                        REST API 서버 (Snowflake Cortex 연동)
```

---

## 2. 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│                        GCP VM (sementica)                   │
│                                                             │
│  ┌──────────────┐    ┌────────────────┐   ┌─────────────┐  │
│  │  sync.py     │    │  MCP 서버      │   │ REST API    │  │
│  │  ingest.py   │───▶│  (port 8765)   │   │ (port 8766) │  │
│  │  Notion API  │    │  FastMCP 4.0   │   │ Starlette   │  │
│  └──────────────┘    └───────┬────────┘   └──────┬──────┘  │
│         │                    │                    │         │
│         ▼                    ▼                    ▼         │
│  ┌──────────────┐    ┌───────────────┐   ┌─────────────┐   │
│  │  Qdrant      │    │  FalkorDB     │   │   ngrok     │   │
│  │  (port 6333) │    │  (port 6379)  │   │   HTTPS     │   │
│  │  벡터 768dim │    │  그래프 DB    │   │   터널      │   │
│  └──────────────┘    └───────────────┘   └──────┬──────┘   │
└─────────────────────────────────────────────────┼──────────┘
                                                  │ HTTPS
                              ┌───────────────────▼──────────┐
                              │      Snowflake (us-central1)  │
                              │                               │
                              │  Python UDF (sementica_*)     │
                              │  External Network Access      │
                              │  Cortex Analyst / Agent       │
                              └───────────────────────────────┘
```

---

## 3. 기술 스택

| 구성요소 | 기술 | 상세 |
|---------|------|------|
| 임베딩 | Google Vertex AI | `text-multilingual-embedding-002`, region: `us-east5`, 768차원 |
| LLM | Anthropic Claude | `claude-haiku-4-5@20251001` via AnthropicVertex, region: `global` |
| 벡터 DB | Qdrant | Docker, port 6333, collection: `strategic_pages` |
| 그래프 DB | FalkorDB | Docker, port 6379, graph: `strategic_kg` |
| MCP 서버 | FastMCP 4.0.0 | Streamable HTTP, port 8765 |
| REST 서버 | Starlette + uvicorn | port 8766, `src/mcp/rest_api.py` |
| HTTPS | ngrok | `agility-unadvised-constrain.ngrok-free.dev` (무료 플랜) |
| 운영 DB | PostgreSQL | notion_pages, sync_log, mcp_request_log 테이블 |
| 소스 | Notion API | 전략사업본부 연동 페이지 |
| Snowflake | us-central1.gcp | Python UDF + External Network Access |

---

## 4. 데이터 파이프라인

### 4-1. Notion 페이지 수집 (`notion_fetch.py`)
```bash
python src/pipeline/notion_fetch.py --dept strategic
python src/pipeline/notion_fetch.py --dept strategic --min-words 50   # 최소 단어 수 지정
python src/pipeline/notion_fetch.py --dept strategic --page-id <UUID>  # 단일 페이지 수집
```
- Notion API에서 전체 페이지 메타 + 본문 수집
- `.md` 파일로 `data/strategic/notion_pages/` 에 저장
- **`--min-words` 옵션 (기본 30)**: 단어 수 미만 페이지는 `.md` 파일 **미생성** (저장 전 필터링)
  - 기존: 저장 후 `⚠️ 텍스트 부족` 표시 → ingest 단계에서 건너뜀
  - 개선: 수집 단계에서 미리 제외 → 불필요한 파일 생성 없음

#### HTML 첨부 파일 추출 (2026-09-07 추가)

Notion 페이지에 업로드된 HTML 파일(`.html`/`.htm`)을 자동으로 다운로드해 텍스트로 변환 후 `.md`에 포함합니다.

**처리 대상 Notion 블록 타입**

| 블록 타입 | 처리 방식 | 판별 방법 |
|----------|----------|---------|
| `file` | `name` 필드 확인 (`.html`/`.htm`) → S3 URL GET | 파일명 기반 |
| `embed` | Notion S3 URL 감지 → GET 후 HTML 내용 확인 | URL 패턴 + 실제 content-type |

**Notion S3 URL 패턴** (`_is_notion_s3_url()`):
```
prod-files-secure.s3.us-west-2.amazonaws.com/...
notion-static.com/...
secure.notion-static.com/...
```
- ⚠️ HEAD 요청 시 `application/xml` (S3 에러 응답) 반환 → **반드시 GET으로 다운로드**
- URL에 확장자가 없으므로 실제 내용의 content-type + 본문 앞부분으로 HTML 여부 판별

**HTML 텍스트 변환 (`_HTMLStripper`)**:
- `html.parser.HTMLParser` 서브클래스, `_skip_depth` 카운터로 스킵 제어
- `_SKIP_TAGS`: `script, style, head, noscript, template` (void 요소 `meta, link` 제외)
  - ⚠️ 수정 포인트: `meta`, `link`는 HTML void 요소(닫는 태그 없음) → 포함 시 `_skip_depth`가 영구 증가해 `<body>` 내용 전체가 스킵됨
- `_BLOCK_TAGS`에 해당하는 요소에서 줄바꿈 삽입 → 가독성 있는 평문 출력

**`.md` 파일 내 마커**:
```markdown
[첨부 HTML: tbl_gbtw_cost_cut.html]
GBTW 회수세

2025-01  44.6%
…
```

**`has_html_attachment` 백필** (`tools/backfill_html_flag.py`):
```bash
# 전체 플로우 (서버)
python src/pipeline/notion_fetch.py --dept strategic   # 재수집
python tools/backfill_html_flag.py --dept strategic    # DB 백필
```
- `.md` 파일에서 `[첨부 HTML:` 마커 검색 → PostgreSQL `has_html_attachment=TRUE` 업데이트
- `--dry-run` 옵션으로 변경 없이 대상 목록만 확인 가능


#### 코퍼스 규모 정정 — 파일 1258개 = 페이지 299개 (2026-09-11)

수집 파일명이 `{수집 회차 순번}_{제목}.md` 였습니다. `fetch` 를 다시 돌리면 같은
페이지에 다른 번호가 붙어 **새 파일로 쌓이고**, 옛 파일은 지워지지 않습니다.
여러 회차의 결과가 누적돼 1258개 파일이 실제로는 **299개 Notion 페이지**였습니다(4.2배).

하위 단계는 전부 `page_id` / `notion_url` 을 키로 쓰므로(Qdrant 포인트 ID, `notion_pages`
UPSERT, 이벤트 ID, 엣지 MERGE) 사본들이 쓰기 시점에 합쳐집니다. 그래서 인제스트 로그는
"1225페이지 / 2653청크 저장"인데 저장소에는 268페이지 / 670청크만 있었습니다.
**데이터 손실은 없었지만, 매 인제스트의 3/4 이 이미 처리한 페이지를 LLM 에 다시 넣는
작업이었습니다.**

| | 로그(시도) | 저장소(실제) |
|---|---|---|
| 페이지 | 1225 | 268 |
| 청크 | 2653 | 670 |
| 이벤트 | 885 | 162 |

- 파일명을 `{page_id}_{제목}.md` 로 바꿨고, 제목이 변해도 같은 `page_id` 의 옛 파일을 지웁니다.
- 이미 쌓인 파일은 `tools/dedupe_pages.py` 로 정리합니다 (page_id 당 최신 1개).
- **로그의 "N개 저장"은 시도 횟수입니다.** 실제 상태는 `tools/stats.py` 로 확인하세요 —
  둘이 어긋나면 이런 종류의 문제가 있다는 신호입니다.

### 4-2. 전체 인제스트 (Full Ingest)
```bash
python src/pipeline/ingest.py --dept strategic
python src/pipeline/ingest.py --dept strategic --reset      # 완전 초기화 후 재구축
python src/pipeline/ingest.py --dept strategic --workers 15 # 워커 수 조정 (기본: 10)
```
- `.md` 파일 읽기 → 텍스트 청크 분할 → 임베딩 → Qdrant 저장
- LLM으로 트리플(주어-관계-목적어) 추출 → FalkorDB 저장
- PostgreSQL `notion_pages` 테이블 UPSERT
- `--reset`: Qdrant 컬렉션 + FalkorDB 그래프 삭제 후 재구축

**FalkorDB 엣지 중복 방지 (2026-09-09 수정)**:

| 레이어 | 방식 | 적용 조건 |
|--------|------|---------|
| in-memory `seen_edges` | `(subj_id, rel_name, obj_id, source_url)` 4-tuple set | 항상 |
| DB 중복 체크 | 3-MATCH 패턴으로 기존 엣지 확인 후 CREATE | `--reset` 없이 실행 시만 |
| `--reset` 스킵 | 그래프 초기화 직후이므로 DB 체크 불필요 | `--reset` 시 |

**중복 판단 기준**: `(subj, rel_name, obj, source_url)` 동일 → 같은 페이지 내 중복 (1개 유지)  
다른 `source_url`에서 같은 관계 추출 → 독립 증거로 각각 저장 (`evidence_chunk_id` → Qdrant 링크 보존)

**`--reset` 성능 개선**:
- DB 체크 생략 (`reset=True` 파라미터로 전달)
- 기본 워커 수 5 → 10 (Vertex AI 쿼터 내에서 병렬도 증가)


#### LLM 추출의 세 가지 제약 (2026-09-11)

**① 입력 절단 — 본문의 45% 가 추출 대상이 아니었습니다**

트리플·이벤트 추출이 `text[:3000]` 으로 잘려 있었습니다. 실측: 299페이지 344,338자 중
**155,294자(45%)가 LLM 에 도달하지 못했고**, 가장 긴 문서는 34,263자로 91% 가 보이지
않았습니다. 페이지 절단으로 복합 카테고리가 전멸했던 것과 같은 구조이며, 그때는 전달
단계였고 이번엔 추출 단계입니다.

상한을 없애는 방식은 쓰지 않습니다 — 입력은 들어가도 **응답**이 `max_tokens` 에서 잘려
JSON 파싱이 실패하고, 그 실패는 예외로 올라가 페이지 전체가 `error` 가 됩니다.
6,000자 창 / 600자 겹침으로 나눠 호출하고 결과를 합집합합니다(겹침 구간 중복은
`(주어, 관계, 목적어)` 로 제거). `stop_reason == "max_tokens"` 이면 창 크기를 줄이라고
경고합니다. 299페이지 중 266개는 여전히 1회 호출입니다.

→ 엣지 576 → **701** (+22%)

**② 온도 — SDK 버전에 따라 전달 경로가 다릅니다**

anthropic 0.99.0 에는 `temperature` 명명 인자가 있고 **1.2.0 에는 없습니다.**
모르고 인자를 넣었다가 `TypeError` 로 **모든 LLM 호출이 실패**해, 인제스트는 정상
종료했는데 그래프의 트리플이 0개, 299페이지 중 248개가 `error` 였습니다(임베딩은 Vertex
쪽이라 무사해서 벡터만 멀쩡했습니다). 1.x 의 공식 경로는 `extra_body={"temperature": …}`
이고, `src/utils/llm.create_message` 가 SDK 를 보고 경로를 고릅니다.

`tools/probe_llm.py` 로 **값이 실제로 반영되는지까지** 확인합니다. 통로가 열렸다는 것과
값이 적용된다는 것은 다릅니다 — 인자를 조용히 무시하는 통로를 "성공"으로 받아들이면,
온도 0 인 줄 알면서 1.0 으로 도는 최악이 됩니다.

**③ 재현성 — 온도 0 으로도 해결되지 않습니다**

온도 0 은 정상 작동합니다(짧은 생성 2회 동일 / 1.0 은 변동, probe 로 확인).
그런데도 **트리플 추출 일치율은 45.9%** 입니다(10페이지 2회). 긴 생성(2048토큰)에서는
서빙 계층 비결정성이 누적되고, 차이는 관계 발견이 아니라 **엔티티 이름 선택**에서 납니다:

```
1회차: IN_JOY_MOBILE →[제공]→ In-Joy
2회차: IN_JOY_MOBILE →[제공]→ 모바일 프로젝트
```

두 회차의 트리플 개수는 거의 같습니다(9/9, 19/19). 같은 사실을 다르게 부르는 것이라
온도로는 잡히지 않습니다. **해결은 추출 이후 단계** — `merge_node` 의 엔티티 정규화
범위를 넓혀 두 이름이 한 노드로 합쳐지게 하는 방향입니다.

> 실무적 영향: 재인제스트마다 관계 질문의 답이 달라질 수 있습니다. 골든셋은 관계 문항의
> 정답을 트리플이 아니라 **원문 인용문**으로 검증하도록 바꿔 이 흔들림에 내성을 갖췄습니다.

**④ 임베딩에 제목이 빠져 있었습니다**

청크 본문만 임베딩하고 제목은 payload 에만 저장했습니다. 긴 문서면 견디지만 짧은
문서는 치명적입니다 — Notion DB 행은 속성 한 줄이라 청크 전체가 100자 안팎인데,
질문이 묻는 문구가 제목에 있으면서 벡터에는 없었습니다. "구글 탑티어2 캠페인 소재
최적화 + TCPA 캡 해제"(102자, 청크 1개)는 두 번의 골든셋에서 연속 검색 실패했습니다.
임베딩 입력을 `{제목}

{청크}` 로 바꿨습니다(payload 의 `text` 는 원문 그대로라
전문 조립·앵커 윈도우·인용문 매칭은 영향 없음).

### 4-3. 완전 초기화 후 재인제스트
```bash
# Step 1: Notion 재수집
python src/pipeline/notion_fetch.py --dept strategic

# Step 2: Qdrant + FalkorDB 수동 초기화
#   (ingest.py --reset 의 FalkorDB 삭제가 묵음 실패할 수 있으므로 수동 삭제)
python3 -c "
from qdrant_client import QdrantClient
import falkordb
QdrantClient(url='http://localhost:6333').delete_collection('strategic_pages')
falkordb.FalkorDB(host='localhost', port=6379).select_graph('strategic_kg').delete()
print('초기화 완료')
"

# Step 3: 재인제스트
python src/pipeline/ingest.py --dept strategic
```

> ⚠️ **주의**: PostgreSQL `notion_pages` 테이블은 `--reset`으로 초기화되지 않음.
> ingest가 UPSERT로 덮어쓰므로 실제 데이터 정합성에는 문제 없음.
> 대시보드의 "전체 페이지" 수치는 PostgreSQL 누적 기록이며 Qdrant 실제 벡터 수와 다를 수 있음.

### 4-4. 증분 동기화 (Incremental Sync)
```bash
python src/pipeline/sync.py --dept strategic
```
- PostgreSQL `notion_pages`에서 `last_edited_time` 불러옴
- Notion API와 per-page 비교:
  - `page_id`가 DB에 없음 → 신규 페이지 (무조건 처리)
  - Notion 수정 시각 > DB 저장 시각 → 변경된 페이지
  - 동일하면 skip
- 처리 결과를 `sync_log`에 기록

### 4-5. Entity Linking — 동의어 해결기 (`src/utils/synonym_resolver.py`)

회사 비즈니스 용어집 API를 통해 엔티티 이름을 정규화하고, 검색 시 동의어를 자동 확장하며,
이벤트 주체가 게임인지 판정하는 근거(`category`)를 제공합니다.

```
https://catalog.joycityplay.com/api/glossary/all  (인증 없음)
→ { "terms": [{ "term": "POTC", "synonyms": ["캐리비안의 해적"], "category": "game" }, ...] }
```

**동작 원리**

| 함수 | 사용 시점 | 역할 |
|------|---------|------|
| `resolve(name)` | 인제스트 시 `merge_node()` 직전 | alias → canonical 정규화 (예: "드래곤슈퍼" → "DS") |
| `expand(name)` | 검색 시 `graph_search()`, `timeline_search()` | canonical 또는 alias → 모든 표현 확장 (쿼리 포괄 검색) |
| `resolve_in(name, cat)` | `classify_scope()` | 해당 카테고리 안에서만 canonical 조회 (게임 판정) |
| `category_of(name)` | 진단 | 용어 분류 반환 (`game` / `KPI` / …) |
| `terms_in(cat)` | 진단 | 카테고리별 canonical 목록 |
| `preload()` | `ingest.py` / `sync.py` / `server.py` 시작 | TTL(1시간) 캐시 강제 갱신 |

**FalkorDB 노드 정규화 흐름**:
```
LLM 추출 → "드래곤슈퍼" → resolve() → "DS" → merge_node() → FalkorDB (:Team {name: "DS"})
```

**용어집 현황** (2026-09-10 기준): 전체 109개 term, 그중 23개가 `category=game`
(`3on3, BLESS, CBZ, CCCC, DS, FS1, FS1R, FS2, FSF2, GBTW, GNSS, GOD, GW-CHINA,
HBZ, IMGN, JTWN, KOFS, ONE, POTC, RESU, TERA, WSB, WWM`).
`organization` 카테고리는 존재하지 않아 조직 판정은 그래프의 `:Team` 노드로 대체합니다.

**오프라인 스냅샷 폴백 (2026-09-10 추가)**

운영 VM에서 `catalog.joycityplay.com` 접근이 차단되어(`curl` → `000`) 용어집을 받을 수 없습니다.
용어집이 없으면 동의어 정규화와 주체 판정이 **모두 비활성화**되므로 스냅샷 폴백을 두었습니다.

```bash
# 접근 가능한 환경에서 스냅샷 갱신 후 커밋
python tools/fetch_glossary_snapshot.py     # → config/glossary_snapshot.json
```

| 순서 | 동작 |
|------|------|
| 1 | API 호출 (`GLOSSARY_TIMEOUT`, 기본 5초) |
| 2 | 실패 시 `config/glossary_snapshot.json` 로드 |
| 3 | 둘 다 실패하면 `_FAIL_RETRY`(120초) 간격으로 재시도, 연속 3회 실패 시 프로세스 내 포기 |

> ⚠️ **재시도 폭주 주의**: `resolve()`/`resolve_in()`은 인제스트 중 **이벤트마다** 호출됩니다.
> 실패 시 매번 네트워크를 재시도하면 타임아웃마다 파이프라인이 멈추므로,
> 시도 시각을 기록해 백오프를 걸고 일정 횟수 후 포기합니다.
> 스냅샷은 `term`·`synonyms`·`category`만 보존하며 `definition` 등은 저장하지 않습니다.

**MCP 검색 확장 흐름**:
```
query: "DS" → expand() → ["DS", "드래곤슈퍼", "Dragon Super"]
                → MATCH (n) WHERE ANY(form IN $forms WHERE n.name CONTAINS form)
```

**LLM 할루시네이션 방지 (EXTRACT_PROMPT 규칙 ②)**:
```
② 조직명은 약칭보다 공식 명칭 우선 — 단, 공식 명칭을 확실히 알 때만 변환할 것
   ※ 중요: 약칭의 원형을 모른다면 반드시 약칭 그대로 사용할 것.
      예) "데사실"의 원형을 모른다면 → "데사실" 그대로 사용
          (절대 "데이터전략실" 등으로 추론·변환 금지)
```

> 미등록 약칭("데사실")을 LLM이 "데이터전략실"로 추론해 존재하지 않는 노드를 생성하는 문제 수정.

---

### 4-5. 노드/엣지 구조 (FalkorDB)

**엔티티 타입 (8종 고정)**
| 타입 | 설명 | 예시 |
|------|------|------|
| `Game` | 게임 코드 | POTC, DS, FC |
| `Team` | 조직/팀 | 전략사업본부, DI팀 |
| `Person` | 실명 인물 | 홍길동 |
| `Event` | 이벤트 이력 | 2026-08 UA예산 증액 |
| `Metric` | 수치 지표 | DAU, 매출, ARPU |
| `Strategy` | 전략/계획 | Q3 마케팅 전략 |
| `Issue` | 문제/리스크 | 이탈율 상승 |
| `Insight` | 분석 결과 | 세그먼트별 LTV 차이 |

**이벤트 노드 주요 속성** (2026-09-10 기준)
```
:Event {
    event_id,        # uuid5(source_url) — Notion 행마다 고유
    title,           # DB "메모" 컬럼 우선, 없으면 meta title
    date,            # "YYYY-MM-DD"
    date_ts,         # Unix timestamp (쿼리 범위 필터용)
    year, month, quarter,
    scope,           # 주체 — 게임 코드 또는 조직명 (정본)
    scope_type,      # game | org | unknown
    scope_verified,  # 용어집 마스터로 확인되었는지 (bool)
    game,            # scope_type=game 일 때만 채움 (하위 호환)
    event_type,      # 정규화된 유형 (ua_campaign, ua_creative 등)
    category,        # 변경카테고리 원문 (예: "캠페인조정")
    manager,         # 담당자
    source_url,      # Notion 개별 행 URL
}
```

**이벤트 노드 개선 이유**:
- `title` = page ID 문제: `meta["title"]`(= 파일명 = page_id)이 아닌 DB "메모" 컬럼 우선 사용
- `event_id` 충돌: 기존 `uuid5(game|event_type|date)` → 같은 날 같은 유형 N건이 1개로 덮임  
  → `uuid5(source_url)` 로 변경 (Notion 행마다 고유 URL)
- `category` 추가: "캠페인조정", "소재 변경" 등 원문 보존 (event_type 변환 전 값)
- `scope` 도입: 아래 "주체 판정" 참고

**`DB_TITLE_KEYS` 우선순위** (메모 컬럼 추출):
```
메모 > memo > 제목 > 이벤트명 > 이벤트제목 > 내용 > description > 설명 > name > 이름
```

#### 주체(scope) 판정 — `classify_scope()` (2026-09-10 추가)

**해결한 문제**: 게임 컬럼이 없는 이벤트는 전부 `game="기타"`로 저장되어
재무실·인사팀 등 서로 다른 부서의 일정이 한 버킷에 뒤섞였고,
`FOLLOWED_BY`가 `e.game` 기준이라 **재무실 일정이 인사팀 일정의 직전 이벤트로 연결**됐습니다.
`"기타"`도 `MERGE (g:Game {name: ...})`를 타서 게임이 아닌 것이 게임 목록에 들어갔습니다.

| 순서 | 판정 근거 | 결과 |
|------|----------|------|
| ① | 용어집 `category=game` 매칭 (동의어 포함) | `game` / verified |
| ② | 용어집 `category=organization` 매칭 | `org` / verified |
| ③ | 게임 컬럼 출처인데 마스터 미등록 | `game` / **unverified** (등록 후보) |
| ④ | 제목·본문에 등장하는 `:Team` 노드 | `org` |
| ⑤ | 해당 없음 | `unknown` (scope 비움) |

- ④가 부서 판정의 실질 경로입니다 — Notion DB에 부서 컬럼이 없어 조직 정보가 제목·본문에만 존재하며,
  트리플 추출이 이미 만든 `:Team` 노드와 대조합니다.
- ②는 현재 항상 실패합니다 (용어집에 `organization` 카테고리 없음). 추가되면 자동 적용됩니다.
- `HAD_EVENT`가 `scope_type`에 따라 `:Game` 또는 `:Team`에 연결됩니다.
- `FOLLOWED_BY`는 `e.scope` 기준이며, scope가 비면 체인을 만들지 않습니다.

**미등록 게임·미분류 리포트**: 인제스트/동기화 종료 시 출력됩니다.
```
⚠️  용어집 미등록 게임 2종: "신규타이틀X"(2건), "프로젝트Y"(1건)
    → 용어집(category=game)에 등록하거나 Notion 컬럼 배치를 확인하세요
⚠️  주체 미분류 이벤트 2건 (예: "일반 업무 메모", "주간 회의")
```
> 이 리포트가 없으면 미등록 게임이 조용히 `unverified`로 쌓이고 용어집이 갱신되지 않습니다.

**관계 어휘 (20종 고정)**
```
BELONGS_TO, MANAGES, PARTICIPATES_IN, CAUSES, LEADS_TO,
MEASURED_BY, TARGETS, USES, SUPPORTS, CONFLICTS_WITH,
PRECEDES, FOLLOWS, PART_OF, REPORTS_TO, COLLABORATES_WITH,
AFFECTS, GENERATES, REFERENCES, RESOLVES, COMPETES_WITH
```

### 4-6. FalkorDB 그래프 조회 및 시각화

**예시 쿼리 파일 (`falkordb/01_example_queries.cypher`)**

| # | 쿼리 | 내용 |
|---|------|------|
| 0 | 기본 통계 | 노드/엣지 수, 라벨별·관계명별 집계 |
| 1 | 특정 게임 연관 엔티티 | POTC와 연결된 팀·인물·전략·이벤트 (양방향) |
| 2 | 특정 인물의 관계망 | 1홉·2홉, 역방향 포함 |
| 3 | 게임별 이벤트 시계열 | `:Event` 노드 + `HAD_EVENT` 관계, 날짜 정렬 |
| 4 | 원인-결과 체인 | `CAUSES → Issue → LEADS_TO/AFFECTS` 2홉 |
| 5 | 팀 조직도 | `BELONGS_TO, MANAGES, TARGETS, REPORTS_TO` |
| 6 | 두 엔티티 간 최단 경로 | `shortestPath((a)-[*1..5]-(b))` |
| 전체 | 전체 그래프 조회 | from/relation/to 3열, HAD_EVENT 포함, CSV 내보내기용 |

```bash
# redis-cli로 직접 실행
redis-cli -h localhost -p 6379
> GRAPH.QUERY strategic_kg "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC"
```

> ⚠️ FalkorDB 한국어 rel_type 미지원 → 모든 관계는 `:REL` 타입 고정.  
> 실제 관계명은 `r.rel_name` 속성에 저장됨.

**그래프 내보내기 + HTML 시각화 (`falkordb/export_graph.py`)**

```bash
# JSON 내보내기 (stdout)
python falkordb/export_graph.py --dept strategic

# 인터랙티브 HTML 생성
python falkordb/export_graph.py --dept strategic --html graph.html

# 직접 파일 지정
python falkordb/export_graph.py --output graph.json --html graph.html
```

- FalkorDB에서 노드·엣지 전체를 조회해 JSON 구조로 반환
- `--html`: Force-directed 레이아웃의 **자체 포함 인터랙티브 HTML** 생성
  - Canvas 기반 렌더링 (300 스텝 시뮬레이션)
  - 줌/패닝, 노드 클릭 → 사이드바 상세 정보
  - 타입별 색상 구분, 노드 타입 필터 드롭다운, 이름 검색
  - 외부 CDN 의존 없이 독립 실행 가능

| 노드 타입 | 색상 |
|---------|------|
| Game | #3B82F6 (파랑) |
| Team | #10B981 (초록) |
| Person | #F59E0B (주황) |
| Event | #EF4444 (빨강) |
| Metric | #8B5CF6 (보라) |
| Strategy | #06B6D4 (시안) |
| Issue | #F97316 (오렌지) |
| Insight | #EC4899 (핑크) |
| Decision | #6B7280 (회색) |

---

## 5. 서버 구성

### 5-1. MCP 서버 (`src/mcp/server.py`)
- **포트**: 8765
- **프로토콜**: Streamable HTTP (FastMCP 4.0.0)
- **도구 (Tools)**:
  - `semantic_search(query, limit)` — 벡터 유사도 검색 + **Parent Document Retrieval**
  - `graph_search(entity, depth)` — 그래프 엔티티 탐색
  - `timeline_search(game, event_type, from_date, to_date, limit, keyword)` — 이벤트 이력 + **벡터 크로스링킹**
  - `hybrid_search(query, limit)` — 벡터 + 그래프 + **이벤트 타임라인** 통합

#### 한국어 조사 매칭 (`src/utils/korean.py`, 2026-09-10 추가)

**해결한 문제**: 그래프 검색이 질문을 `.split()`으로 쪼개 `n.name CONTAINS <토큰>`을 실행했는데,
한국어 토큰에는 조사가 붙어 비교가 **자기 자신과 반대 방향**이 됐습니다.

```
질문 "데사실은 어느 부서와…"  → 토큰 "데사실은"
노드 이름                      → "데사실"
n.name CONTAINS "데사실은"     → 매칭 실패
```

조사가 우연히 없는 토큰만 매칭되어, 그래프 결과가 질문과 무관하게 0~42건으로 요동쳤습니다.
골든셋 20문항 중 6문항이 그래프 0건이었고, 그중 3문항이 온톨로지가 담당해야 할 관계 질문이었습니다.

| 함수 | 역할 |
|------|------|
| `match_nodes_in_text(graph, text)` | **역방향 매칭** — `$text CONTAINS n.name`. 조사와 무관하게 동작 |
| `strip_particle(word)` | 토큰 끝 조사 1회 제거 (어간 2자 보존 → `고명수`·`우편` 유지) |
| `entity_candidates(text)` | 원본 + 조사 제거 토큰을 모두 반환 (잘못된 제거로 매치를 잃지 않음) |
| `contains_as_token(text, name)` | 영문 코드의 단어 경계 검사 — `ONE ⊄ MILESTONE`, `DS ⊄ DSBI` |

> `contains_as_token`이 필요한 이유: 게임 마스터에 `ONE`·`GOD`·`DS` 같은 짧은 코드가 있어
> 단순 `CONTAINS`는 `MILESTONE`에서 `ONE`을 잡습니다. 한글은 조사가 바로 붙어
> 경계 판정이 불가능하므로 그대로 통과시키고, 조사 처리는 위 세 함수가 담당합니다.

#### 날짜 범위 추출 (`src/utils/datespan.py`, 2026-09-10 추가)

| 입력 | 결과 |
|------|------|
| `2026년 6월 19일` / `2026-06-19` | `2026-06-19` ~ `2026-06-19` |
| `26년 6월 19일` | 2자리 연도 보정 |
| `2026년 6월` | `2026-06-01` ~ `2026-06-30` |
| `2026년 2분기` / `Q2` | `2026-04-01` ~ `2026-06-30` |
| `2026년 2월 30일` | `2026-02-28` (말일 보정) |

- 분기를 월보다 **먼저** 매칭 — `"2분기"`의 `2`가 월로 오인되지 않도록
- `"지난달"` 등 상대 표현은 기준 시각에 따라 결과가 달라지므로 **의도적으로 미지원**
- `has_timeline_intent(text)` — 날짜 표현 또는 시계열 키워드 존재 여부

#### 이벤트 타임라인 통합 (2026-09-10 추가)

날짜 기반 질문이 `:Event` 노드에 닿지 못하던 문제를 해결했습니다.
`timeline_search`가 별도 도구로만 존재해, 호출자가 그 도구를 직접 고르지 않으면
이벤트 그래프에 아예 접근할 수 없었습니다.

`hybrid_search`가 `semantica_helper.resolve_timeline_query()`를 **기존 스레드 풀에서 병렬 호출**하므로
레이턴시가 추가되지 않습니다. 평가 파이프라인(`evaluate.py`)도 같은 함수를 사용해 판정이 어긋나지 않습니다.

**주체 결정 순서**
1. `:Game` / `:Event.game`에 이름이 있으면 그 게임으로 조회
2. 아니면 질문에 등장하는 그래프 노드 이름을 `keywords`로 조회
   — 게임 없는 이벤트는 `"기타"`로 저장되므로 부서명은 `game`이 아닌 제목·설명·카테고리·담당자에서 탐색
3. 주체 없이 날짜만 있으면 그 기간 전체 조회

**호출 조건**: 날짜 표현 또는 시계열 키워드가 **필수**. 주체·날짜가 모두 없으면 조회하지 않습니다
(`:Event` 전체 스캔 방지). `"점검 시작은 어느 팀 담당?"`처럼 키워드만 걸리는 질문은 제외됩니다.

```python
# 부서 업무 일정 — game 이 아닌 keyword 로 전달해야 함
timeline_search(keyword="재무실", from_date="2026-06-01", to_date="2026-06-30")
```

**Parent Document Retrieval (2026-09-03 적용, 2026-09-10 절단 정책 수정)**
```
벡터 유사도 → 상위 k 청크 → page_id 수집 + 최고점 청크를 앵커로 기록
                                ↓
          Qdrant scroll (page_id MatchAny 필터)
                                ↓
     같은 page_id의 모든 청크 → chunk_index 정렬 → 전체 본문 조합
                                ↓
     PAGE_MAX_CHARS(16000) 이하 → 전문 그대로
     초과 → 앵커 청크 중심 윈도우 (앞 1/3은 선행 문맥, 생략 표기 삽입)
                                ↓
     반환: {title, source_url, content, chunk_count, score, total_chars, truncated}
```
- 기존: `text_preview` (300자 미리보기) → 청크 단위 단편적 문맥
- 개선: `content` (페이지 전문) → 완전한 문맥 해석

> ⚠️ **절단은 앞에서 하면 안 됩니다.** 2026-09-10 이전에는 `full_text[:4000]` 으로
> 페이지 앞부분만 남겼습니다. 벡터 검색이 찾아낸 청크가 문서 뒤쪽에 있으면
> **근거를 검색해놓고 전달 직전에 버리는** 결과가 됩니다. 골든셋 Q07·Q18·Q20 이
> 정확히 이 경우였고(근거 청크 26·27 / 페이지 25226자), 수정 후 세 문항이 동시에
> 0.0 → 1.0 이 되었습니다. `PAGE_MAX_CHARS` 환경변수로 조정할 수 있습니다.

**그래프→벡터 크로스링킹 (2026-09-07 추가)**

`_fetch_pages_by_source_urls(qc, collection_name, source_urls, max_chars=2000)`:
- Qdrant `scroll`에 `FieldCondition(key="source_url", match=MatchAny(...))` 필터 적용
- 그래프에서 찾은 `source_url`로 벡터 DB의 동일 문서 청크를 직접 조회

```
그래프 노드/엣지 ─ source_url 수집
                         ↓
     Qdrant scroll (source_url MatchAny 필터)
                         ↓
  청크 조합 → content(최대 2000자) + chunk_count 반환
```

**`timeline_search` 강화**:
```python
# 이벤트 노드의 source_url → 연결된 Notion 원문 첨부
{
    "title": "GBTW UA예산 증액",
    "date": "2026-08",
    "source_url": "https://app.notion.com/p/...",
    "page_content": "…Notion 원문 본문 (최대 1500자)…",
    "page_chunk_count": 12,
}
```

**`hybrid_search` 강화**:
```python
# 반환 구조
{
    "semantic_results": [...],  # 벡터 검색 결과 (Parent Document Retrieval 적용)
    "graph_results": [...],  # 그래프 엔티티
    "linked_pages": [...],  # 그래프 엣지 source_url로 연결된 추가 문서
    # (semantic_results에 없는 페이지만 포함)
}
```

- **실행**:
  ```bash
  python src/mcp/server.py --dept strategic
  ```

### 5-2. 웹 운영 대시보드 (`src/ops/web_app.py`)
- **포트**: 8080
- **프레임워크**: FastAPI + uvicorn
- **탭 구성**: 현황 / 배치 실행 / 검색 테스트 / 골든셋
- **주요 API 엔드포인트**:

  | 경로 | 설명 |
  |------|------|
  | `GET /api/pages` | PostgreSQL `notion_pages` 페이지 목록 (dept 필터, 제목 검색) |
  | `GET /api/qdrant-stats` | Qdrant 전체 컬렉션 통계 |
  | `GET /api/qdrant-chunks` | **특정 `page_id`의 모든 청크 조회** |
  | `GET /api/graph-stats` | FalkorDB 노드·엣지·이벤트 수 |
  | `GET /api/sync-log` | 동기화 이력 |
  | `POST /api/batch/run` | 배치 작업 실행 (fetch/ingest/sync 등) |

- **`/api/qdrant-chunks` 기능 (2026-09-03 추가)**:
  - 페이지 목록에서 status=ok 행마다 **🔍 청크** 버튼 표시
  - 클릭 시 모달 팝업: 해당 page_id의 Qdrant 청크 전체를 `chunk_index` 순으로 표시
  - 각 청크의 내용, 길이, UUID, Notion 원본 링크 확인 가능
  - Parent Document Retrieval 조합 결과를 사전 검증하는 용도

- **HTML 첨부 컬럼 (2026-09-07 추가)**:
  - 페이지 목록에 **HTML** 컬럼 추가
  - `has_html_attachment=TRUE` 인 페이지에 📎 아이콘 표시
  - PostgreSQL `notion_pages.has_html_attachment` 기반

- **대시보드 수치 출처**:

  | 카드 | 출처 | 비고 |
  |------|------|------|
  | 전체 페이지 | PostgreSQL `notion_pages` | 누적 기록 (reset 무관) |
  | 벡터 청크 | PostgreSQL `SUM(chunk_count)` | Qdrant 실제 수와 다를 수 있음 |
  | 그래프 노드/엣지 | FalkorDB | 현재 그래프 실제 수 |
  | 이벤트 / 게임 | FalkorDB | `:Event` / `:Game` 노드 수 |

- **실행**:
  ```bash
  nohup python src/ops/web_app.py > logs/web_app.log 2>&1 &
  ```

### 5-3. REST API 서버 (`src/mcp/rest_api.py`)
- **포트**: 8766
- **프레임워크**: Starlette + uvicorn
- **인증**: `SNOWFLAKE_REST_TOKEN` Bearer 토큰 (미설정 시 인증 없음)
- **엔드포인트**:

  | 메서드 | 경로 | 설명 |
  |--------|------|------|
  | GET | `/rest/health` | 헬스 체크 |
  | POST | `/rest/search` | 벡터 검색 |
  | POST | `/rest/graph` | 그래프 탐색 |
  | POST | `/rest/events` | 이벤트 이력 |
  | POST | `/rest/hybrid` | 통합 검색 |
  | POST | `/snowflake/search` | Snowflake External Function 형식 |
  | POST | `/snowflake/events` | Snowflake External Function 형식 |
  | POST | `/snowflake/hybrid` | Snowflake External Function 형식 |

- **실행**:
  ```bash
  python src/mcp/rest_api.py --dept strategic --port 8766
  ```

---

## 6. Snowflake 연동 (구현 완료)

### 6-1. 아키텍처 개요
```
Snowflake Cortex (모 모델/오케스트레이터)
    │
    ├─ Cortex Analyst: Snowflake 내부 KPI/지표 데이터 분석
    │
    └─ sementica_* Python UDF: Semantica REST API 호출
           │  External Network Access (HTTPS)
           │  ngrok HTTPS 터널
           └─▶ Semantica REST API (port 8766)
                    ├─ Qdrant (벡터 검색)
                    └─ FalkorDB (그래프 검색)
```

### 6-2. Snowflake 설정 파일

| 파일 | 내용 |
|------|------|
| `snowflake/01_network_access.sql` | Network Rule + External Access Integration |
| `snowflake/02_python_udfs.sql` | Python UDF 3종 생성 |
| `snowflake/03_test_queries.sql` | VARIANT 파싱 + Cortex COMPLETE 예시 |

### 6-3. Python UDF 사용 예

```sql
-- 벡터 검색
SELECT sementica_search('POTC 마케팅 이력', 5);

-- 이벤트 이력 (게임, 유형, 기간, 개수)
SELECT sementica_events('POTC', 'ua_budget', '2026-08-01', '2026-08-31', 20);

-- 통합 검색
SELECT sementica_hybrid('DS 매출 감소 원인', 8);

-- Cortex LLM + Semantica 컨텍스트 조합
SELECT SNOWFLAKE.CORTEX.COMPLETE(
    'claude-3-5-haiku',
    CONCAT(
        '검색 결과:\n', sementica_hybrid('DS 매출 감소 원인', 5)::VARCHAR,
        '\n\n질문: DS 게임의 매출이 감소한 주요 원인은?'
    )
);
```

### 6-4. 설계 결정: External Function → Python UDF

Snowflake External Function은 `API_PROVIDER`로 AWS/Azure/GCP API Gateway를 반드시 사용해야 합니다. 범용 HTTPS 엔드포인트 직접 연결이 불가능하므로, **External Network Access + Python UDF** 방식을 채택했습니다. API Gateway 구축 없이 ngrok HTTPS URL을 직접 호출할 수 있습니다.

---

## 7. 운영 스크립트 및 재시작

### 7-1. 스크립트 목록

| 스크립트 | 용도 |
|---------|------|
| `scripts/start_with_ngrok.sh` | REST API + ngrok 동시 시작 |
| `scripts/backup.sh` | 로컬 백업 |
| `scripts/backup_to_gcs.sh` | GCS 백업 |
| `scripts/create_indexes.py` | Qdrant 인덱스 생성 |
| `scripts/test_mcp.py` | MCP 서버 테스트 |
| `tools/backfill_html_flag.py` | `has_html_attachment` DB 백필 |
| `tools/debug_html_blocks.py` | Notion 페이지 HTML 블록 구조 진단 |
| `tools/fetch_glossary_snapshot.py` | 용어집 오프라인 스냅샷 생성 (VM 네트워크 차단 대응) |
| `tools/diag_golden_miss.py` | 실패 문항 진단 — 근거·검색·전달 3단계 중 어디서 잃었는지 |
| `tools/diag_generation.py` | 생성 단계 진단 — 컨텍스트 길이 vs 프롬프트 A/B/C 비교 |
| `tools/stats.py` | **저장소 실측** — Qdrant·FalkorDB·notion_pages 를 직접 세어 로그와 대조 |
| `tools/query.py` | FalkorDB Cypher 실행 (`redis-cli` 없이). `--rel <이름>` 으로 관계 요약 |
| `tools/probe_llm.py` | temperature 전달 통로 확인 — **값이 실제로 반영되는지**까지 검증 |
| `tools/dedupe_pages.py` | 중복 수집 `.md` 정리 (page_id 당 1개) |
| `tools/check_missing_vectors.py` | 벡터가 실제로 유실된 페이지 탐지 + `--fix` 로 재처리 예약 |
| `tools/check_extraction_stability.py` | 트리플 추출 재현성 측정 (같은 문서 2회 추출 비교) |
| `tools/ab_retrieval.py` | 검색 파라미터 A/B — LLM 생성·채점 없이 검색 단계만 결정적으로 측정 |
| `src/eval/gen_golden_set.py` | 현재 데이터에서 골든셋 자동 생성 |
| `src/eval/evaluate.py` | 골든셋 기반 검색 품질 평가 |

**실패 문항 진단 흐름**:
```bash
# ① 근거·검색·전달 중 어디서 잃었는지
python tools/diag_golden_miss.py --dept strategic \
    --golden data/eval/golden_set_YYYYMMDD.json \
    --result data/eval/eval_result_YYYYMMDD_HHMMSS.json

# ② "전달됨"으로 분류된 문항 → 생성 단계 원인 분리
python tools/diag_generation.py --dept strategic \
    --golden data/eval/golden_set_YYYYMMDD.json --ids Q07,Q18,Q20
```
> ①의 근거 판정은 **의미 기반**입니다. 골든셋 정답은 `verify_grounded` 가 재서술을
> 허용해 채택한 문장이라 원문에 같은 문자열이 없습니다 — 문자열 매칭으로 검사하면
> 모든 문항이 "데이터 없음"으로 오판됩니다.
> ③ 전달 검사는 `server.py` 의 `_window_around_anchor` 를 소스에서 추출해 그대로
> 적용하므로, 진단기와 운영 코드가 어긋나지 않습니다.

**`tools/backfill_html_flag.py` 사용법**:
```bash
# 재수집 후 DB 플래그 동기화
python src/pipeline/notion_fetch.py --dept strategic
python tools/backfill_html_flag.py --dept strategic

# 변경 없이 대상 목록만 확인
python tools/backfill_html_flag.py --dept strategic --dry-run
```

**`tools/debug_html_blocks.py` 사용법**:
```bash
# 특정 페이지의 블록 타입·URL·Content-Type 진단
python tools/debug_html_blocks.py --page-id 3c7ea67a568180b4b288fab957019624
```
- `file`/`embed`/`pdf`/`image`/`link_preview` 블록을 재귀적으로 순회
- S3 URL은 GET 요청으로 실제 Content-Type + HTML 여부 확인

### 7-2. 서비스 재시작 (git pull 후)

```bash
git pull

# 전체 재시작
pkill -f rest_api.py; pkill -f ngrok; pkill -f web_app.py; pkill -f "server.py"
sleep 2

# REST API + ngrok (bash로 실행 — 파일시스템 noexec 우회)
bash scripts/start_with_ngrok.sh

# 웹 대시보드
nohup python src/ops/web_app.py > logs/web_app.log 2>&1 &

# MCP 서버
nohup python src/mcp/server.py --dept strategic \
  --transport streamable-http --port 8765 > logs/mcp.log 2>&1 &
```

> ⚠️ **`./scripts/start_with_ngrok.sh` Permission denied 발생 시**:  
> `bash scripts/start_with_ngrok.sh` 으로 실행 (파일시스템 noexec 마운트 우회)

### 7-3. FalkorDB 수동 초기화

`ingest.py --reset`의 FalkorDB 삭제는 `graph.delete()` 를 사용합니다 (2026-09-09 수정).

```bash
# --reset 실행 시 출력 예시 (정상)
#   🗑️  FalkorDB 그래프 삭제: strategic_kg
#   ✅ FalkorDB 연결 완료 — 그래프: strategic_kg

# 수동 초기화 (필요 시)
python3 -c "
import falkordb
falkordb.FalkorDB(host='localhost', port=6379).select_graph('strategic_kg').delete()
print('FalkorDB 삭제 완료')
"
```

> ⚠️ `db.delete_graph()` 메서드는 falkordb 1.x에 없음 → `select_graph().delete()` 사용  
> `--reset` 로그에 `🗑️  FalkorDB 그래프 삭제` 메시지가 없으면 삭제 실패 → 수동 삭제 필요

**중복 엣지 확인 쿼리** (재인제스트 후 검증용):
```cypher
MATCH (s)-[r:REL]->(o)
WITH s.name AS subj, o.name AS obj, r.rel_name AS rel, r.source_url AS url, COUNT(r) AS cnt
WHERE cnt > 1
RETURN subj, obj, rel, url, cnt ORDER BY cnt DESC LIMIT 20
```

### 7-4. ngrok 상태 확인 및 재시작

```bash
# 현재 ngrok 터널 URL 확인 (ngrok 로컬 API)
curl http://localhost:4040/api/tunnels

# URL만 추출
curl -s http://localhost:4040/api/tunnels | python3 -c "
import sys, json
d = json.load(sys.stdin)
for t in d['tunnels']:
    print(t['public_url'])
"

# ngrok 프로세스 확인
ps aux | grep ngrok

# ngrok 재시작
pkill -f ngrok; sleep 1
nohup ngrok http 8766 > logs/ngrok.log 2>&1 &
sleep 2

# 새 URL 확인 후 Snowflake UDF에 반영
curl http://localhost:4040/api/tunnels
```

> ⚠️ **ngrok 무료 플랜**: 재시작 시 URL 변경됨.  
> URL 변경 후 `snowflake/01_network_access.sql` (Network Rule) 및 `snowflake/02_python_udfs.sql` (UDF 엔드포인트)를 새 URL로 재생성해야 함.

### 7-5. 검색 품질 평가 (골든셋)

```bash
# ① 현재 데이터에서 골든셋 자동 생성 (샘플 수는 count 에 비례해 자동 설정)
python src/eval/gen_golden_set.py --dept strategic --count 40

# 쿼터에 여유가 있으면 워커를 늘려 단축
python src/eval/gen_golden_set.py --dept strategic --count 40 --workers 12

# ② 평가 실행
python src/eval/evaluate.py --dept strategic --golden data/eval/golden_set_YYYYMMDD.json
```

> `--baseline` 은 문항당 LLM 2회를 추가하므로 40문항에서는 생략을 권합니다.

결과: `data/eval/eval_result_*.json`, `eval_report_*.md`

**채택 기준 — 검색 통과가 아니라 원문 근거** (2026-09-09 수정)

기존 생성기는 `verify_by_search()`로 **검색 파이프라인이 답할 수 있는 질문만** 채택했습니다.
그런데 그 함수는 평가와 **동일한 파이프라인**입니다.

```
질문 생성 → [검색이 답할 수 있는가?] → pass만 채택
                                          ↓
              그 골든셋으로 같은 파이프라인 평가 → 당연히 ~100%
```

결과적으로 "이미 답할 수 있는 질문"만 남아 점수가 인위적으로 높아지고 약점이 측정되지 않았습니다.
현재는 `verify_grounded()`가 **정답이 소스 원문으로 뒷받침되는지**만 확인하며(LLM 환각 필터),
검색 통과 여부는 `--baseline` 플래그로 `baseline_pass` 필드에 **참고 기록**만 합니다.

> `baseline_pass: false`인 문항이 곧 **개선 대상**입니다. 이전 방식에서는 그 문항들이 아예 제외됐습니다.

**평가 파이프라인 정렬** (2026-09-09 수정)

평가가 실제 서비스와 다른 파이프라인을, 약 1/6의 정보량으로 측정하고 있었습니다.

| | `server.py` (서비스) | `evaluate.py` (수정 전) |
|---|---|---|
| 문서 단위 | 페이지 전체 재조립 (Parent Document Retrieval) | 청크 원본 |
| 문서당 길이 | 4000자 | 2000자 |
| 총 전달량 | 자르지 않음 | **5000자 → 실질 2건** |

벡터를 8건 검색해도 LLM은 2건만 봤습니다. 검색은 성공했는데 **전달 단계에서 실패**한 문항들이
검색 실패로 오인됐습니다. 현재는 `_fetch_full_pages()`를 이식하고 예산 상수를 서비스와 맞췄습니다
(`PAGE_MAX_CHARS=4000`, `TOP_PAGES=6`, `CONTEXT_MAX_CHARS=26000`).

**평가 이력**

| 일자 | 전체 | 담당자 | 정책·규정 | 관계 | 문서위치 | 복합 | 비고 |
|------|------|--------|----------|------|---------|------|------|
| 2026-09-09 ① | 0.775 | 1.00 | 1.00 | 0.70 | 0.67 | 0.33 | 조사 수정 전 |
| 2026-09-09 ② | 0.825 | 1.00 | 1.00 | **0.90** | 0.67 | 0.33 | 조사 매칭 수정 후 |
| 2026-09-10 ③ | 0.850 | 1.00 | 0.75 | 1.00 | 1.00 | 0.33 | 재인제스트 + 새 골든셋(20문항 교체) |
| 2026-09-10 ④ | 0.825 | 1.00 | 0.75 | 0.90 | 1.00 | 0.33 | 청크 오버샘플 — 효과 없음, Q13 악화 |
| 2026-09-10 ⑤ | **0.950** | 1.00 | 0.88 | 0.90 | 1.00 | **1.00** | **페이지 절단 제거 + 앵커 윈도우** |
| 2026-09-10 ⑥ | 0.800 | 0.90 | 0.88 | 1.00 | 0.67 | 0.33 | 검색 경로 통합 + 골든셋 재생성 |
| 2026-09-10 ⑦ | **0.925** | 1.00 | 1.00 | 1.00 | 1.00 | 0.50 | **골든셋 모호성 필터** — 4개 카테고리 만점 |
| 2026-09-10 ⑧ | 0.900 | 1.00 | 1.00 | 0.90 | 0.92 | 0.58 | **40문항으로 확대** (`--count` 버그 수정) |
| 2026-09-10 ⑨ | **0.950** | 1.00 | 1.00 | 0.90 | 0.92 | **0.92** | **예산 기반 컨텍스트 채우기** |
| 2026-09-10 ⑩ | **0.963** | 1.00 | 1.00 | **1.00** | 0.83 | 0.92 | **분해 시 원본 질문 포함** (Q27 회복) |
| 2026-09-11 ⑪ | 0.838 | 0.90 | 0.94 | 0.80 | 0.75 | 0.75 | 코퍼스 정정(1258파일 → 299페이지) 후 첫 측정 |
| 2026-09-11 ⑫ | **0.925** | **1.00** | 0.88 | 0.95 | **1.00** | 0.75 | 관계 문항 출처 오연결 수정 + 골든셋 재생성 |

> ⑪ 이전 회차와는 **비교할 수 없습니다.** 코퍼스가 1258개 "페이지"에서 실제 299개로
> 정정되었고(아래 4-1 참고), 그래프 내용·문항·채점 근거가 모두 달라졌습니다.
> ⑫가 현재 기준선입니다.

⑧부터 40문항입니다. 카테고리당 6~10문항이 되어 한 문항의 영향이 0.33 → 0.1~0.17 로
줄었고, 회차 간 점수가 안정됐습니다. 난이도별로는 ⑩에서
easy 1.00 (14) / medium 0.94 (18) / **hard 0.94** (8) 입니다.

> ⚠️ **측정 노이즈에 주의하세요.** LLM 생성은 비결정적이라 코드가 같아도 문항 점수가
> 흔들립니다. ⑩에서 Q31(팀시티 경로)이 이유 없이 1.0 → 0.5 로 내려갔습니다 — 근거는
> 여전히 컨텍스트에 있었고 답변 구성만 달라졌습니다. 전체 +0.013 도 Q27 회복(+0.025)과
> Q31 하락(−0.0125)이 상쇄된 값입니다.
> **회차 간 0.01~0.02 차이로 개선을 판단하지 말고, 카테고리 추세와 개별 문항의
> 실패 원인을 보세요.**

골든셋은 ③·⑥·⑦에서 각각 재생성되어 문항이 전부 바뀝니다. **회차 간 총점 비교는
의미가 없고**, 카테고리 추세와 개별 문항의 실패 원인을 봐야 합니다.

⑥은 검색 경로를 서비스와 통합한 뒤의 첫 측정입니다. 그전까지 평가는 `server.py` 만
튜닝된 값과 다른 랭킹(coverage 0.15 vs 0.20, 복합 판정 15자/6단어 vs 12자/5단어)을
재고 있었으므로, **⑤ 이전 점수는 서비스 성능이 아닙니다.**

⑦에서 골든셋 생성에 모호성 필터를 넣자 ⑥에서 반복되던 문항 유형
("쿼리에서 GROUP BY 항목은?" 처럼 대상 미특정)이 사라지고 담당자·정책·관계·문서위치가
모두 1.00 이 되었습니다. 난이도별로는 easy 1.00 / medium 0.88 / hard 0.90 입니다.

**⑤의 결정적 수정**: 페이지 본문을 앞 4000자로 자르던 것이 복합·정책 카테고리 실패의 유일한 원인이었습니다.
Q07(근거가 청크 26·27, 페이지 25226자)·Q18·Q20 모두 **검색은 1~2위로 성공했지만 전달 단계에서
근거가 잘려나가고** 있었습니다. 한도를 16000자로 올리고 초과 시 매칭 청크 중심으로 윈도우를 잡자
세 문항이 동시에 0.0 → 1.0 이 되었습니다.

> 그 전에 시도한 청크 오버샘플(④)은 검색량만 4배 늘렸을 뿐 전달 범위를 바꾸지 않아 효과가 없었고,
> Q13은 오히려 악화시켰습니다. **"검색됐는가"와 "전달됐는가"를 구분해 진단해야 합니다** —
> `tools/diag_golden_miss.py` 가 그 세 단계(근거·검색·전달)를 분리해 보여줍니다.

#### 골든셋 문항 품질 — 채택 관문 2개 (2026-09-10 추가)

⑥까지 반복해서 부분점수를 받던 문항들은 검색 실패가 아니라 **문항 결함**이었습니다.

| 유형 | 예 | 문제 |
|------|-----|------|
| 대상 미특정 | "쿼리에서 GROUP BY 절 항목은?" | 문서에 쿼리가 여럿 → 답이 갈림 |
| 범위 초과 | Q "제공하는 곳은?" A "…분기마다 제공" | 주기를 묻지 않았는데 정답에 포함 |
| 문맥 의존 | "해당 쿼리는…" | 질문만으로 대상을 알 수 없음 |

`gen_golden_set.py` 가 이제 두 관문을 모두 통과한 문항만 채택합니다
(`verify_qa` 가 한 번의 LLM 호출로 두 판정을 함께 수행).

```
① grounded    정답이 원문에 근거하는가        (환각 필터)
② answerable  질문이 답을 하나로 특정하는가   (모호성 필터)
```

②를 프롬프트 규칙만으로 대체할 수 없습니다 — 규칙을 넣어도 모델이 계속
미특정 질문을 만들기 때문에, 후보마다 원문과 대조해 판정합니다.
탈락 사유는 실행 요약과 `meta.rejected` 에 집계되어, 생성기가 어느 규칙을
반복해서 어기는지 드러납니다.

> ⚠️ **검증 모델을 낮추지 마세요.** 속도를 위해 Haiku 로 바꿔 실측했더니
> "범위 초과" 판정을 놓치고 JSON 이 잘려 8건 중 2건이 파싱 실패했습니다.
> 판정 기준이 조용히 느슨해지면 골든셋이 오염되고 그 위의 모든 점수가
> 무의미해집니다. `GOLDEN_JUDGE_MODEL` 로 바꿀 수는 있으나 권장하지 않습니다.

**생성 속도** (2026-09-10): 페이지·관계 처리를 병렬화하고(`--workers`, 기본 8)
두 관문을 한 호출로 합쳐, 순차·분리 호출 대비 문항당 LLM 호출이 절반입니다.
`--count` 는 카테고리 비율을 유지하며 스케일되고(`_scale_targets`),
`--sample-pages`·`--sample-rels` 는 `count` 에 비례해 자동 설정됩니다.

> `--count 40` 을 줘도 20문항만 나오던 버그가 있었습니다 — `CATEGORY_TARGETS` 가
> 합 20 고정이었고 `--count` 는 안내 문구에만 쓰였습니다. 관계 탐색 범위와
> 샘플 수도 고정이라 문항을 늘려도 후보가 따라 늘지 않았습니다.

#### 컨텍스트는 개수가 아니라 예산으로 채웁니다 (2026-09-10)

`TOP_PAGES=6` 처럼 **개수**로 제한하면 짧은 문서가 상위를 차지할 때 컨텍스트가
텅 빈 채로 근거가 잘려나갑니다. 실측 사례:

```
Q38  1~6위: 51자 단편 6개  → 컨텍스트 958자 (예산 60000자 중 1.6%)
    ✂7위: 344자 근거          ← 근거만 주면 1.0
```

UA 히스토리의 짧은 단편이 슬롯을 채우고, coverage 부스트까지 받아 굳어졌습니다
(단편은 서브쿼리 2개에 걸려 1.2배, 구체적인 근거는 1개에만 걸려 부스트 없음).

이제 `CONTEXT_MAX_CHARS` 가 찰 때까지 채우고 `MAX_CONTEXT_DOCS`(25)는 안전장치로만
둡니다. 긴 문서는 예산에서 자연히 3~4건으로 제한되므로 회귀가 없습니다.
Q38·Q39 가 동시에 0.0 → 1.0 이 되었고 복합 카테고리가 0.58 → 0.92 로 올랐습니다.

#### 검색 파라미터 A/B 결과 (2026-09-10)

평가 점수로 파라미터를 판단할 수 없어 — LLM 생성이 비결정적이라 근거가 그대로
있는데도 문항 점수가 ±0.5 움직입니다 — **검색 단계만 결정적으로 측정**하는
`tools/ab_retrieval.py` 로 확인했습니다. 40문항, 근거 페이지가 최종 컨텍스트에
포함되는 비율:

| 설정 | 벡터 의존 30문항 근거 포함률 | 판단 |
|------|------------------------|------|
| **oversample 8** (= `limit` 10, 청크 80) | **100% (30/30)** | ✅ **채택** |
| oversample 4 | 미측정 (아래 참고) | — |
| coverage boost 0.0 / 0.10 / 0.20 | 모두 동일 | recall 무영향 (유효) |
| dedupe 0.85 | −3.3%p | ❌ **적용 안 함** (유효) |

`CHUNK_OVERSAMPLE` 을 **4 → 8** 로 올렸습니다. 근거는 위 100% 한 줄입니다 —
100% 가 상한이므로 4 가 얼마든 8 이 그보다 나쁠 수 없고, 비용은 Qdrant top-k
가 40 → 80 청크로 커지는 것뿐입니다. 반환 **페이지** 수는 `limit` 이 정하므로
페이지 조립 비용은 그대로입니다.

**Q03·Q08 은 임베딩 문제가 아니었습니다.** 두 문항은 "검색 실패 — 임베딩·청킹
문제"로 분류돼 별도 과제(#47)로 잡혀 있었는데, 청크 풀만 넓히니 회복됐습니다.
페이지 단위 집계가 후보를 좁히고 있었을 뿐입니다.

> ⚠️ **이전 표(1배 57.5% / 2배 70.0% / 4배 77.5% / 8배 77.5%)는 무효입니다.**
> 측정 도구가 낮은 oversample 을 "캐시된 페이지 목록을 앞에서 N개만 남기는"
> 방식으로 흉내냈는데, `vector_search_pages` 에서 `limit` 은 **페이지** 수이고
> `oversample` 은 후보 **청크** 풀만 넓힙니다. 그래서 그 숫자들은
> "페이지를 1/2/5/10개 넘겼을 때의 recall" 이었습니다.
> 다만 **max_over 행(=8배)만은 자르기가 일어나지 않아 유효**했고, 그것이 위
> 100% 입니다. 도구는 oversample 값마다 실제 검색을 다시 하도록 고쳤습니다.
>
> boost·dedupe 행은 모두 같은 oversample 끼리의 비교라 이 결함의 영향을
> 받지 않으며, 결론은 그대로 유효합니다.

**바로잡은 판단**

- **near-duplicate 제거는 해롭습니다.** 정작 고치려던 Q36 을 포함해 문항을
  잃었습니다 — 유사해 보이는 문서도 세부가 다르고 답은 그 세부에 있으며,
  근거가 중복 그룹의 최상위가 아니면 지워집니다. `NEAR_DUP_THRESHOLD` 기본값을
  1.0(비활성)으로 두고 함수만 남겼습니다. 참고로 이 함수는 파이프라인에
  **연결되어 있지 않아** 환경변수만 바꿔서는 아무 효과가 없습니다.
- **청크 오버샘플 판단을 세 번째로 바꿨습니다.** 도입 시 "효과 없음"(페이지
  절단에 가려짐), A/B 후 "4가 적정"(측정 오류), 지금 "8"(유효한 한 줄 근거).
  앞의 두 번은 측정 방식 자체가 틀렸던 것이라, 값을 또 바꿀 때는 반드시
  수정된 도구로 재측정하세요.

> ⚠️ **관계 카테고리는 이 지표로 판단하지 마세요.** 관계 문항의 근거는 그래프
> 트리플인데 `source_url` 은 벡터 문서를 가리킵니다. 관계 10문항 중 8건이 벡터
> recall 실패로 잡히지만 평가에서는 모두 1.0 입니다. 도구가 카테고리를 나눠
> 출력하니 벡터 의존 카테고리로만 비교하세요.

#### 분해 시 원본 질문도 검색합니다 (2026-09-10)

복합 쿼리를 서브쿼리로 분해하면 **긴 엔티티 이름이 축약**되어 그래프 노드 매칭
(`$text CONTAINS n.name`)이 깨집니다.

```
원본 : "ADNW/빅미디어 규모-효율 진단 문서를 작성한 조직은?"  → 노드 매칭 ✅
서브1: "ADNW 빅미디어 규모 효율 진단"                        → ❌
서브2: "진단 문서 작성 조직"                                  → ❌
```

`search_queries()` 가 분해 시 원본을 함께 반환해, 온전한 이름으로도 검색합니다.
원본에 걸린 문서는 coverage 가 1 늘어 재랭킹에서도 우대됩니다.

적용 후 Q27 이 0.0 → 1.0 이 되어 관계 카테고리가 1.00 이 되었습니다
(그래프 8 → 9건, "데이터사이언스실" 정확히 응답).

> ⚠️ **평가와 서비스가 다른 코드를 타면 반드시 문제가 됩니다.**
> 컨텍스트 예산 불일치, `utils` 임포트 실패(평가만 통과), 튜닝 누락
> (coverage 0.15 vs 0.20) 모두 같은 원인이었습니다.
> 2026-09-10 에 `src/utils/retrieval.py` 로 검색 경로를 통합했습니다 —
> **검색 파라미터 튜닝은 이 파일에서만** 하세요.

---

## 8. 구현 완료 / 예정

| # | 항목 | 상태 | 비고 |
|---|------|------|------|
| 1 | Notion → Qdrant 벡터 인덱싱 | ✅ | `ingest.py` |
| 2 | Notion → FalkorDB 그래프 구축 | ✅ | 트리플 추출, 8종 엔티티 |
| 3 | 증분 동기화 (신규 페이지 포함) | ✅ | `sync.py`, PostgreSQL per-page 비교 |
| 4 | MCP 서버 (Claude Desktop 연동) | ✅ | port 8765 |
| 5 | REST API 서버 | ✅ | port 8766, `rest_api.py` |
| 6 | ngrok HTTPS 터널 | ✅ | `start_with_ngrok.sh` |
| 7 | Snowflake External Network Access | ✅ | `01_network_access.sql` |
| 8 | Snowflake Python UDF | ✅ | `02_python_udfs.sql`, 테스트 완료 |
| 9 | Parent Document Retrieval | ✅ | `server.py` `_fetch_full_pages()`, 2026-09-03 |
| 10 | 웹 대시보드 Qdrant 청크 뷰어 | ✅ | `web_app.py` `/api/qdrant-chunks` + 모달 UI |
| 11 | notion_fetch `--min-words` 필터 | ✅ | 수집 단계에서 텍스트 부족 페이지 제외 |
| 12 | FalkorDB 예시 쿼리 (`falkordb/01_example_queries.cypher`) | ✅ | 6종 예시 쿼리 + 전체 그래프 조회 |
| 13 | FalkorDB 그래프 내보내기 + HTML 시각화 (`falkordb/export_graph.py`) | ✅ | Force-directed 인터랙티브 HTML |
| 14 | Notion HTML 첨부 자동 추출 | ✅ | `notion_fetch.py` `_extract_attached_html()`, 2026-09-07 |
| 15 | has_html_attachment 대시보드 컬럼 | ✅ | PostgreSQL + 웹 대시보드 📎 아이콘, 2026-09-07 |
| 16 | 그래프→벡터 크로스링킹 | ✅ | `server.py` `_fetch_pages_by_source_urls()`, timeline/hybrid, 2026-09-07 |
| 17 | Entity Linking (동의어 해결기) | ✅ | `synonym_resolver.py`, Business Glossary API, 2026-09-09 |
| 18 | FalkorDB 엣지 중복 생성 수정 | ✅ | 3단계 수정 (쿼리 패턴 · graph.delete() · --reset 스킵), 2026-09-09 |
| 19 | Event 노드 title·category·event_id 개선 | ✅ | 메모 컬럼 우선, source_url 기반 ID, category 보존, 2026-09-09 |
| 20 | 재인제스트 성능 개선 | ✅ | DB 체크 스킵 + 워커 10, 2026-09-09 |
| 21 | sync.py 코드 검증 및 수정 | ✅ | DB 체크 제거(항상 무의미), seen_edges 타입 수정, preload 추가, 2026-09-09 |
| 22 | LLM 할루시네이션 방지 | ✅ | EXTRACT_PROMPT 규칙 ② 수정 — 약칭 원형 추측 금지, 2026-09-09 |
| 23 | 엣지 생성 MERGE 전환 | ✅ | `CREATE` → `MERGE {rel_name, source_url}` — DB 레벨 멱등성, 2026-09-09 |
| 24 | 골든셋 채택 기준 수정 | ✅ | 자기충족 검증 제거 → 원문 근거 기반, `--baseline` 분리, 2026-09-09 |
| 25 | Claude 리전 분리 | ✅ | 임베딩 리전 오용 수정 (`ANTHROPIC_VERTEX_REGION`), 2026-09-09 |
| 26 | 한국어 조사 매칭 | ✅ | `utils/korean.py` — 역방향 매칭·조사 제거·단어 경계, 2026-09-10 |
| 27 | 평가 파이프라인 정렬 | ✅ | Parent Document Retrieval 이식 + 컨텍스트 예산 서비스 일치, 2026-09-09 |
| 28 | 이벤트 타임라인 통합 | ✅ | `hybrid_search` + `resolve_timeline_query()`, `utils/datespan.py`, 2026-09-10 |
| 29 | 게임 없는 타임라인 조회 | ✅ | `get_event_chain(keywords=...)` — 부서 업무 일정 대응, 2026-09-10 |
| 30 | 용어집 카테고리 인덱싱 | ✅ | `category_of()` / `resolve_in()` / `terms_in()`, 2026-09-10 |
| 31 | 이벤트 주체(scope) 분류 | ✅ | `classify_scope()` — game/org/unknown, `"기타"` 버킷 해소, 2026-09-10 |
| 32 | 미등록 게임·미분류 리포트 | ✅ | 인제스트 요약에 노출 → 용어집 갱신 순환, 2026-09-10 |
| 33 | `sys.path` 버그 수정 | ✅ | `eval/` 외 전 진입점에서 `utils` 임포트 실패 → Entity Linking 미작동, 2026-09-10 |
| 34 | 용어집 오프라인 스냅샷 | ✅ | VM 네트워크 차단 대응 + 재시도 폭주 방지, 2026-09-10 |
| 35 | 페이지 절단 정책 수정 | ✅ | 앞 4000자 → 16000자 + 앵커 윈도우. 복합 0.33 → 1.00, 2026-09-10 |
| 36 | 검색 실패 원인 진단 도구 | ✅ | `diag_golden_miss.py`(근거·검색·전달), `diag_generation.py`(길이·프롬프트), 2026-09-10 |
| 37 | 평가·서비스 검색 경로 통합 | ✅ | `utils/retrieval.py` — 파라미터·랭킹·분해 단일화, 2026-09-10 |
| 38 | 골든셋 모호성 필터 | ✅ | `verify_answerable()` 채택 관문 추가, 4개 카테고리 만점, 2026-09-10 |
| 39 | 타임라인 트리거 정밀화 | ✅ | 강·약 키워드 분리 — 약한 키워드는 날짜 동반 시에만, 2026-09-10 |
| 40 | 40문항 골든셋 평가 | ✅ | `--count` 버그 수정, 카테고리당 6~10문항, 2026-09-10 |
| 41 | 골든셋 생성 속도 개선 | ✅ | 병렬 8워커 + 검증 2회→1회, 2026-09-10 |
| 42 | 예산 기반 컨텍스트 채우기 | ✅ | 개수 제한 제거 — 복합 0.58 → 0.92, 2026-09-10 |
| 43 | 분해 시 원본 질문 포함 | ✅ | 엔티티 이름 축약으로 그래프 매칭 실패하던 문제, 2026-09-10 |
| 44 | 검색 파라미터 A/B 도구 | ✅ | `tools/ab_retrieval.py` — LLM 노이즈 없이 검색만 측정, 2026-09-10 |
| 45 | 청크 오버샘플 검증 | ✅ | **8 채택** — limit 10 에서 벡터 의존 30문항 100%. 이전 두 번의 판단은 측정 오류, 2026-09-10 |
| 46 | near-duplicate 제거 | ⛔ | A/B 결과 −7.5%p — **적용하지 않음**. 함수는 남기되 기본 비활성 |
| 47 | 벡터 검색 실패 문항 개선 | ✅ | Q03·Q08 은 임베딩 문제가 아니라 청크 풀 부족이었음 — oversample 8 로 해소, 2026-09-10 |
| 48 | VM 용어집 네트워크 복구 | 🔜 | `catalog.joycityplay.com` 접근 차단 (curl → 000). 스냅샷으로 우회 중 |
| 49 | 전체 코드 리뷰 반영 | ✅ | 동기화 데이터 소실·Event 필드 누락·대시보드 컬럼명·인덱스 대상 그래프·무인증 파괴 엔드포인트, 2026-09-10 |
| 50 | 오버샘플 재측정 | ✅ | `CHUNK_OVERSAMPLE=8` 확정, 2026-09-10 |
| 51 | 유실 페이지 점검 | ✅ | `tools/check_missing_vectors.py` — 22건 중 21건은 장부 흔적, **실유실 1건**(GBTW 감액 및 ROAS KPI 검토) 재처리, 2026-09-10 |
| 52 | 수집 파일 중복 제거 | ✅ | 파일명을 `page_id` 기준으로 — 1258파일이 299페이지였음, 2026-09-11 |
| 53 | 추출 입력 절단 해소 | ✅ | `text[:3000]` → 6000자 창 분할. 본문의 45% 가 누락되고 있었음. 엣지 576 → 701, 2026-09-11 |
| 54 | LLM 온도 전달 경로 | ✅ | anthropic 1.x 는 `extra_body`. `utils/llm` 이 SDK 별로 선택, `probe_llm` 이 반영 여부 검증, 2026-09-11 |
| 55 | 임베딩에 제목 포함 | ✅ | 짧은 DB 행이 검색에 안 잡히던 문제, 2026-09-11 |
| 56 | 관계 문항 출처 연결 | ✅ | 5개 묶음 중 첫 관계의 URL 을 무조건 쓰던 버그 — `source_index` 로 정확히 연결, 2026-09-11 |
| 57 | 인덱스 자동 생성 | ✅ | `--reset` 이 그래프와 함께 인덱스를 지우므로 ingest 안으로 이동. 누락된 4개(`Event.event_id`·`Event.scope`·`Decision.name`·`Unknown.name`) 추가, 2026-09-11 |
| 58 | 진단 도구 정비 | ✅ | 7종 추가. 파이프라인을 **베끼지 않고 호출**하도록 통일 — 베낀 도구는 파이프라인이 바뀌면 어긋납니다, 2026-09-11 |
| 59 | Snowflake UDF 인증 | ✅ | `SECRETS` + `Authorization` 헤더. 없는 동안 서버 토큰을 켤 수 없었음, 2026-09-11 |
| 60 | 엔티티 정규화 확대 | 🔜 | 추출 비결정성(45.9%)의 실질적 해법. `merge_node` 범위 확대 |
| 61 | 이벤트 `manager` 누락 | ✅ | **결함 아님**(2026-09-11 정정). DB 속성에 담당자 컬럼이 있으면 정상 기록됨(GBTW 페이지 → 박준혁). 94.5% 가 빈 것은 UA 히스토리 DB 에 담당자 컬럼이 없고 LLM 추출이 보수적으로 비우기 때문. 키워드 검색은 title·description·game·category·scope 도 함께 봄 |
| 62 | FOLLOWED_BY 건너뛰기 엣지 | 🔜 | 병렬 인제스트에서 두 스레드가 같은 구간을 동시에 가르면 재발 — 락 필요 |
| 37 | End-to-End 통합 테스트 | 🔜 | Snowflake ↔ Semantica ↔ Cortex 전구간 |
| 38 | LLM 결과 캐싱 | 🔜 | content_hash 기반 triplets 캐시 → --reset 속도 대폭 단축 |
| 39 | 동의어 사전 "데사실" 등록 | 🔜 | Business Glossary API에 데사실 → 데이터사이언스실 추가 필요 |
| 40 | EntityDeduplicator (그래프 중복 병합) | 🔜 | 향후 개선 |
| 41 | HTTPS 고정 URL (ngrok 유료 or 도메인) | 🔜 | 프로덕션 시 필요 |

> **Cortex Analyst YAML 모델**은 대상에서 제외되었습니다 — Cortex에 Analytics Agent를 직접 생성하고
> UDF로 온톨로지 API를 호출하는 구조로 동작 확인이 완료되어, `05_cortex_agent.sql`의
> Stored Procedure 오케스트레이터와 함께 불필요해졌습니다.

---

## 9. 주요 환경변수 (`.env`)

```env
# Notion
NOTION_TOKEN=secret_xxx

# GCP
GCP_PROJECT=joycity-xxx
VERTEX_AI_LOCATION=us-east5          # 임베딩 리전
ANTHROPIC_VERTEX_REGION=global       # Claude LLM 리전

# Qdrant / FalkorDB
QDRANT_URL=http://localhost:6333
FALKOR_HOST=localhost
FALKOR_PORT=6379

# PostgreSQL (운영 로그)
POSTGRES_URL=postgresql://user:pass@host:5432/dbname

# REST API 보안
SNOWFLAKE_REST_TOKEN=                # 미설정 시 인증 없음
SNOWFLAKE_REST_PORT=8766

# 검색 튜닝 (선택 — 미설정 시 코드 기본값)
PAGE_MAX_CHARS=16000                 # 페이지 본문 전달 한도. 초과 시 앵커 윈도우
CONTEXT_MAX_CHARS=60000              # 평가 컨텍스트 총량 상한

# 용어집 (선택)
GLOSSARY_API_URL=https://catalog.joycityplay.com/api/glossary/all
GLOSSARY_TIMEOUT=5                   # 초. 실패 시 스냅샷 폴백
GLOSSARY_SNAPSHOT=                   # 기본: config/glossary_snapshot.json
```

---

## 10. 알려진 제약 사항

| 항목 | 내용 |
|------|------|
| ngrok 무료 플랜 | 재시작 시 URL 변경 → Snowflake UDF 재생성 필요 |
| HTTPS 미설정 | 현재 ngrok으로 우회 중, 프로덕션 시 고정 HTTPS 필요 |
| FalkorDB `delete_graph()` 미지원 | `select_graph().delete()` 로 대체, `--reset` 묵음 실패 가능 |
| 대시보드 벡터 청크 수치 | PostgreSQL SUM이므로 Qdrant 실제 벡터 수와 다를 수 있음 |
| 스크립트 실행 권한 | 파일시스템 noexec 마운트 시 `bash script.sh` 로 우회 |
| FalkorDB 시각화 | 공식 UI 없음, `redis-cli` 또는 커스텀 웹앱으로 조회 |
| Snowflake 계정 | `SEONGIN-us-central1.gcp` |
| **트리플 추출 재현성 45.9%** | 온도 0 을 적용해도 재인제스트마다 엔티티 이름이 달라집니다. 관계 질문의 답이 회차마다 바뀔 수 있습니다 (4-2 ③ 참고) |
| **anthropic SDK 버전 의존** | 1.x 는 `temperature` 명명 인자가 없습니다. `utils/llm.create_message` 가 흡수하지만, SDK 를 바꿀 때는 `tools/probe_llm.py` 로 먼저 확인하세요 |
| 짧은 DB 행 검색 | Notion DB 행은 청크가 100자 안팎이라 벡터 유사도가 낮습니다. 제목 임베딩으로 완화했으나 한계는 남아 있습니다 |

---

## 11. 방화벽 포트 목록

방화벽 신청 시 개방 요청해야 하는 포트 목록 (GCP VM 기준).

### 인바운드 (외부 → VM)

| 포트 | 프로토콜 | 용도 | 접근 대상 |
|------|---------|------|---------|
| 22 | TCP | SSH 접속 | 개발자 IP |
| 8080 | TCP | 웹 운영 대시보드 (`web_app.py`) | 개발자 IP |
| 8765 | TCP | MCP 서버 (`server.py`) | Claude Desktop / Cursor (개발자 IP) |
| 8766 | TCP | REST API 서버 (`rest_api.py`) | ngrok(내부), 개발자 IP |
| 6333 | TCP | Qdrant 벡터 DB (HTTP REST + 대시보드) | 개발자 IP |
| 6379 | TCP | FalkorDB (Redis 프로토콜) | 개발자 IP |
| 4040 | TCP | ngrok 로컬 관리 UI | localhost only |

> `6333` (Qdrant 대시보드: `http://<vm-ip>:6333/dashboard`) 및  
> `6379` (FalkorDB, redis-cli 접속)는 개발자 IP에서 직접 접근 필요.

### 아웃바운드 (VM → 외부)

| 포트 | 프로토콜 | 목적지 | 용도 |
|------|---------|-------|------|
| 443 | TCP | `api.notion.com` | Notion API 페이지 수집 |
| 443 | TCP | `us-east5-aiplatform.googleapis.com` | Vertex AI 임베딩 |
| 443 | TCP | Anthropic / Claude API 엔드포인트 | LLM 트리플 추출 |
| 443 | TCP | `ngrok.com`, `*.ngrok-free.dev` | ngrok HTTPS 터널 |
| 443 | TCP | Snowflake (us-central1.gcp) | 쿼리 결과 수신 (Snowflake → Semantica 방향은 아웃바운드 불필요) |

---

## 12. 변경 이력

| 날짜 | 내용 |
|------|------|
| 2026-09-11 | **평가 기준선 재설정 — 0.925** (담당자·문서위치 1.00). 코퍼스·그래프·문항이 모두 달라져 이전 회차와 비교 불가 |
| 2026-09-11 | **Snowflake UDF 정비** — `SECRETS` + `Authorization` 헤더(없어서 서버 토큰을 켤 수 없었음), 오류 본문 보존, 시크릿 전체 경로(integration 은 계정 레벨이라 `USE SCHEMA` 무효) |
| 2026-09-11 | **임베딩에 제목 포함** — 100자짜리 DB 행이 검색에 안 잡히던 문제. 질문이 묻는 문구가 제목에 있으면서 벡터에는 없었음 |
| 2026-09-11 | **관계 문항 출처 오연결 수정** — 5개 묶음 중 첫 관계의 URL 을 무조건 사용. 진단이 "근거 없음"으로 오판하던 원인 |
| 2026-09-11 | **추출 입력 절단 해소** — `text[:3000]` 이 본문의 45%(155,294자)를 버리고 있었음. 6000자 창 분할로 엣지 576 → 701 |
| 2026-09-11 | **LLM 온도** — anthropic 1.x 에 `temperature` 인자가 없어 모든 호출이 실패하고 그래프가 비었던 사고. `extra_body` 경로 + SDK 자동 선택 + 반영 여부 검증 도구 |
| 2026-09-11 | **코퍼스 규모 정정** — 파일 1258개 = 페이지 299개(4.2배 중복). 수집 파일명이 회차 순번 기준이라 재수집마다 누적. 이전의 모든 규모 추정이 이 위에 있었음 |
| 2026-09-10 | **전체 코드 리뷰 반영** — 동기화 중 일시적 오류로 페이지가 영구 소실되던 문제(삭제 후 재생성 실패 + 해시 기록 → 이후 영영 건너뜀), `:Event` 의 `manager` 미기록·날짜 미갱신, 대시보드 두 패널 컬럼명 오류(`created_at` vs `ts`), 인덱스가 빈 그래프에 생성되던 문제, 무인증 파괴 엔드포인트, REST `limit` 무제한 |
| 2026-09-10 | **평가 신뢰성 수정** — `--golden` 경로 오타 시 내장 골든셋으로 조용히 폴백, 분해 모델이 서비스(Haiku)와 평가(Sonnet)로 갈림, 하네스 실패(API 오류)가 오답으로 집계 |
| 2026-09-10 | **`CHUNK_OVERSAMPLE` 4 → 8** — limit 10 에서 벡터 의존 30문항 **100%**. "임베딩·청킹 문제"로 분류했던 Q03·Q08 이 청크 풀만 넓히니 회복 |
| 2026-09-10 | **벡터 유실 점검** (`tools/check_missing_vectors.py`) — Qdrant 를 직접 확인. `status='ok' AND chunk_count=0` 22건 중 21건은 해시 스킵이 카운트를 0 으로 덮어쓴 장부 흔적, **실제 유실 1건** |
| 2026-09-10 | **A/B 측정 오류 정정** — oversample 을 결과 슬라이싱으로 흉내내 실제로는 "페이지 개수"를 재고 있었음. 도구 수정, 무효 수치 폐기. boost·dedupe 결론은 유효 |
| 2026-09-10 | ~~**검색 파라미터 A/B** — 오버샘플 4 적정 확인~~ (위 항목으로 정정), near-duplicate 제거는 −7.5%p 로 **미적용** 결정 |
| 2026-09-10 | 분해 시 **원본 질문도 검색 대상에 포함** — 긴 엔티티 이름이 축약되어 그래프 노드 매칭이 깨지던 문제. 관계 0.90 → **1.00**, 전체 **0.963** |
| 2026-09-10 | **컨텍스트를 개수가 아닌 예산으로 채움** — 짧은 단편이 슬롯을 채워 근거를 밀어내던 문제. 복합 0.58 → 0.92, 전체 **0.900 → 0.950** |
| 2026-09-10 | 골든셋 생성 병렬화(`--workers` 8) + 검증 2회→1회 병합. 검증 모델은 품질 문제로 Sonnet 유지 |
| 2026-09-10 | `--count` 버그 수정 — `CATEGORY_TARGETS` 고정(합 20)이라 40을 줘도 20문항만 생성. 샘플 수·관계 탐색 범위도 `count` 비례로 |
| 2026-09-10 | **골든셋 모호성 필터** (`verify_answerable`) — 대상 미특정·범위 초과·문맥 의존 문항 제거. 담당자·정책·관계·문서위치 **모두 1.00**, 전체 0.925 |
| 2026-09-10 | 타임라인 트리거 정밀화 — 강·약 키워드 분리. "서버 오픈 시간 테이블 경로" 같은 질문에 이벤트 20건이 붙어 문서를 밀어내던 오탐 제거 |
| 2026-09-10 | 이벤트 원문(`page_content`) 첨부를 `resolve_timeline_query()` 로 이동 — 평가에만 빠져 있던 불일치 해소 |
| 2026-09-10 | **평가·서비스 검색 경로 통합** (`utils/retrieval.py`) — coverage 부스트·복합 판정·분해 프롬프트가 서로 달라 평가가 서비스와 다른 랭킹을 재던 문제 |
| 2026-09-10 | **페이지 절단 정책 수정** — 앞 4000자 → 16000자 + 앵커 청크 중심 윈도우. 검색된 근거를 전달 직전에 버리던 문제. 골든셋 **0.825 → 0.950**, 복합 0.33 → 1.00 |
| 2026-09-10 | 진단 도구 2종 추가 — `diag_golden_miss.py`(근거·검색·전달 3단계), `diag_generation.py`(컨텍스트 길이 vs 프롬프트) |
| 2026-09-10 | 청크 오버샘플 도입 — 페이지 단위 집계로 인한 다양성 손실 보정. 단독 효과는 확인되지 않음 |
| 2026-09-10 | 평가·서비스 타임라인 판정 공통화 — `resolve_timeline_query()`, 중복 구현 제거 |
| 2026-09-10 | **`sys.path` 버그 수정** — `eval/` 외 모든 진입점에서 `utils` 임포트 실패. 전부 `try/except` 폴백이라 조용히 넘어갔고, **Entity Linking이 인제스트에서 한 번도 작동한 적 없었음** |
| 2026-09-10 | 용어집 오프라인 스냅샷 (`config/glossary_snapshot.json`, `tools/fetch_glossary_snapshot.py`) — VM에서 API 차단(curl → 000) 대응 |
| 2026-09-10 | 용어집 재시도 폭주 방지 — 실패 시 백오프(120초)·3회 후 포기. 이벤트마다 재시도해 인제스트가 멈추던 문제 |
| 2026-09-10 | 이벤트 주체 분류 `classify_scope()` — game/org/unknown. `"기타"` 버킷 해소, `HAD_EVENT`를 `:Game`/`:Team`으로 분기, `FOLLOWED_BY`를 `scope` 기준으로 |
| 2026-09-10 | 미등록 게임·미분류 이벤트 리포트 — 인제스트 요약에 노출 |
| 2026-09-10 | 용어집 카테고리 인덱싱 — `category_of()` / `resolve_in()` / `terms_in()` (game 23종) |
| 2026-09-10 | 게임명 없는 타임라인 조회 — `get_event_chain(keywords=...)`, `timeline_search(keyword=...)` |
| 2026-09-10 | 이벤트 타임라인을 `hybrid_search`에 통합 (`resolve_timeline_query()`), 날짜 파서 `utils/datespan.py` 추가 |
| 2026-09-10 | 한국어 조사 매칭 수정 (`utils/korean.py`) — 역방향 매칭·조사 제거·영문 코드 단어 경계. 관계 카테고리 0.70 → 0.90 |
| 2026-09-09 | 평가 파이프라인을 서비스와 정렬 — Parent Document Retrieval 이식, 컨텍스트 예산 5000자 → 26000자 |
| 2026-09-09 | 골든셋 채택 기준 수정 — 자기충족 검증(`verify_by_search`) 제거, 원문 근거(`verify_grounded`) 기반으로 전환 |
| 2026-09-09 | Claude 리전 분리 — `evaluate.py`·`week1_verify.py`가 임베딩 리전을 Claude에 전달해 400 오류 |
| 2026-09-09 | 엣지 생성을 `CREATE` → `MERGE {rel_name, source_url}` 로 전환 — DB 레벨 멱등성 확보 |
| 2026-09-09 | Entity Linking 추가 (`synonym_resolver.py`) — Business Glossary API 기반 동의어 해결, `resolve()` / `expand()` / `preload()` |
| 2026-09-09 | FalkorDB 엣지 중복 생성 근본 원인 수정 — ① 쿼리 패턴 3-MATCH 분리, ② `db.delete_graph()` → `graph.delete()`, ③ `--reset` 시 DB 체크 스킵 |
| 2026-09-09 | `ingest.py` `--workers` 기본값 5 → 10, `--reset` 시 DB 중복 체크 건너뜀으로 재인제스트 성능 개선 |
| 2026-09-09 | `sync.py` DB 체크 제거 — `delete_page_edges()` 이후 항상 빈 결과이므로 불필요, `seen_edges` 타입 4-tuple 수정, `preload()` 추가 |
| 2026-09-09 | Event 노드 title 개선 (`semantica_helper.py`) — `DB_TITLE_KEYS`/"메모" 컬럼 우선, page_id 사용 방지 |
| 2026-09-09 | Event 노드 `event_id` 충돌 수정 — `uuid5(game\|event_type\|date)` → `uuid5(source_url)`, 같은 날 같은 유형 중복 방지 |
| 2026-09-09 | Event 노드 `category` 필드 추가 — 변경카테고리 원문 보존, `ON MATCH SET` 재동기화 지원 |
| 2026-09-09 | ruff lint/format 전체 적용 — `synonym_resolver.py` RUF005 (`[canonical] + synonyms` → `[canonical, *synonyms]`) 외 |
| 2026-09-07 | `_HTMLStripper._SKIP_TAGS` void 요소 버그 수정 — `meta`, `link` 제거로 `<body>` 내용 스킵 문제 해결 |
| 2026-09-07 | Notion HTML 첨부 자동 추출 (`notion_fetch.py`) — `file`/`embed` 블록, Notion S3 URL 지원, `[첨부 HTML:]` 마커 포함 |
| 2026-09-07 | `has_html_attachment` DB 컬럼 추가 — PostgreSQL, `ingest.py`, `sync.py`, `db_logger.py` |
| 2026-09-07 | 웹 대시보드 HTML 컬럼 추가 (`web_app.py`) — 📎 아이콘 표시 |
| 2026-09-07 | 그래프→벡터 크로스링킹 (`server.py`) — `_fetch_pages_by_source_urls()`, `timeline_search` page_content 첨부, `hybrid_search` linked_pages |
| 2026-09-07 | 백필/진단 도구 추가 — `tools/backfill_html_flag.py`, `tools/debug_html_blocks.py` |
| 2026-09-03 | Parent Document Retrieval 적용 (`server.py`) — 청크 단위 → 페이지 전체 본문(최대 4000자) 반환 |
| 2026-09-03 | 웹 대시보드 Qdrant 청크 뷰어 추가 (`web_app.py`) — 🔍 청크 버튼 + 모달 UI |
| 2026-09-03 | notion_fetch `--min-words` 옵션 추가 — 수집 단계에서 텍스트 부족 페이지 제외 |
| 2026-09-03 | Snowflake `05_cortex_agent.sql` — `content` 필드 반영, `source_url` 인용 강화 |
| 2026-09-03 | Snowflake `03_test_queries.sql` — Parent Document Retrieval 적용 후 VARIANT 파싱 `content/chunk_count` 반영 |
| 2026-09-03 | FalkorDB 예시 쿼리 추가 (`falkordb/01_example_queries.cypher`) — 6종 쿼리 + 전체 그래프 조회 |
| 2026-09-03 | FalkorDB 그래프 내보내기 + HTML 시각화 추가 (`falkordb/export_graph.py`) — Force-directed 인터랙티브 HTML |
| 2026-09-03 | FalkorDB 초기화 방법 확인 — `delete_graph()` 없음, `select_graph().delete()` 사용 |
| 2026-09-03 | ngrok 상태 확인·재시작 방법 추가 — 로컬 API `localhost:4040`, URL 변경 시 Snowflake UDF 재생성 필요 |
| 2026-09-03 | 방화벽 포트 목록 추가 — 인바운드 7종, 아웃바운드 5종 (6333 Qdrant, 6379 FalkorDB 포함) |
| 2026-09-03 | ngrok HTTPS 터널 + REST API 서버 + 웹 대시보드 구성 완료 |
| 2026-09-03 | Snowflake Python UDF 3종 (`sementica_search/events/hybrid`) 구현·테스트 완료 |
