# Semantica — 프로젝트 전체 요약

> JoyCity 전략사업본부 Notion 기반 온톨로지 검색 솔루션  
> 최종 업데이트: 2026-09-10 (페이지 절단 정책 수정 — 골든셋 **0.950**, 이벤트 주체 분류, 타임라인 통합)

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
# ① 현재 데이터에서 골든셋 자동 생성
python src/eval/gen_golden_set.py --dept strategic --count 20 --baseline

# ② 평가 실행
python src/eval/evaluate.py --dept strategic --golden data/eval/golden_set_YYYYMMDD.json
```

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

③에서 골든셋을 현재 데이터로 재생성해 문항이 전부 바뀌었으므로 ②까지와는 비교할 수 없습니다.
난이도별로는 ⑤에서 easy 1.00 / medium 0.89 / **hard 1.00** 입니다.

**⑤의 결정적 수정**: 페이지 본문을 앞 4000자로 자르던 것이 복합·정책 카테고리 실패의 유일한 원인이었습니다.
Q07(근거가 청크 26·27, 페이지 25226자)·Q18·Q20 모두 **검색은 1~2위로 성공했지만 전달 단계에서
근거가 잘려나가고** 있었습니다. 한도를 16000자로 올리고 초과 시 매칭 청크 중심으로 윈도우를 잡자
세 문항이 동시에 0.0 → 1.0 이 되었습니다.

> 그 전에 시도한 청크 오버샘플(④)은 검색량만 4배 늘렸을 뿐 전달 범위를 바꾸지 않아 효과가 없었고,
> Q13은 오히려 악화시켰습니다. **"검색됐는가"와 "전달됐는가"를 구분해 진단해야 합니다** —
> `tools/diag_golden_miss.py` 가 그 세 단계(근거·검색·전달)를 분리해 보여줍니다.

**남은 0.5 문항 (골든셋 품질 이슈)**

| 문항 | 내용 | 판단 |
|------|------|------|
| Q08 | "쿼리에서 GROUP BY 절 항목은?" | 문서에 쿼리가 여러 개인데 **어느 쿼리인지 특정하지 않음**. 검색이 풍부해질수록 불리 |
| Q13 | "키링 프리셋 저장은 어떤 문제와 관련?" | 정답이 그래프 관계인데 그래프 1건 대 벡터 22건 — 문서 부가 정보가 답변을 지배 |

> 이 둘을 1.0 으로 만들려 시스템을 조정하면 골든셋 오버피팅입니다. 문항 교체나
> 관계 질문의 그래프 가중치 조정을 별도 과제로 다루는 편이 낫습니다.

> ⚠️ **평가와 서비스가 다른 코드를 탄다**는 점이 반복해서 문제를 일으켰습니다.
> 컨텍스트 예산 불일치, `utils` 임포트 실패(평가만 통과) 모두 같은 원인입니다.
> 타임라인 판정은 `resolve_timeline_query()`로 공통화했지만, 벡터·그래프 검색은 여전히 분리돼 있습니다.

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
| 37 | 골든셋 문항 품질 개선 | 🔜 | Q08(질문 모호), Q13(그래프 관계인데 벡터가 지배) — 남은 0.5 두 문항 |
| 38 | 청크 오버샘플 재검토 | 🔜 | 효과 미확인. `CHUNK_OVERSAMPLE` A/B 필요 (Q13 악화 의심) |
| 39 | VM 용어집 네트워크 복구 | 🔜 | `catalog.joycityplay.com` 접근 차단 (curl → 000). 스냅샷으로 우회 중 |
| 40 | 평가·서비스 검색 경로 통합 | 🔜 | 타임라인만 공통화됨. 벡터·그래프는 여전히 이중 구현 |
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
