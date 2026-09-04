# Semantica — 프로젝트 전체 요약

> JoyCity 전략사업본부 Notion 기반 온톨로지 검색 솔루션  
> 최종 업데이트: 2026-09-03 (Parent Document Retrieval, 대시보드 청크 뷰어, notion_fetch 개선, FalkorDB 쿼리/시각화, 방화벽 포트)

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
```
- Notion API에서 전체 페이지 메타 + 본문 수집
- `.md` 파일로 `data/strategic/notion_pages/` 에 저장
- **`--min-words` 옵션 (기본 30)**: 단어 수 미만 페이지는 `.md` 파일 **미생성** (저장 전 필터링)
  - 기존: 저장 후 `⚠️ 텍스트 부족` 표시 → ingest 단계에서 건너뜀
  - 개선: 수집 단계에서 미리 제외 → 불필요한 파일 생성 없음

### 4-2. 전체 인제스트 (Full Ingest)
```bash
python src/pipeline/ingest.py --dept strategic
```
- `.md` 파일 읽기 → 텍스트 청크 분할 → 임베딩 → Qdrant 저장
- LLM으로 트리플(주어-관계-목적어) 추출 → FalkorDB 저장
- PostgreSQL `notion_pages` 테이블 UPSERT
- `--reset`: Qdrant 컬렉션 + FalkorDB 그래프 삭제 후 재구축

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

**이벤트 노드 주요 속성**
```
:Event { title, date, date_ts, year, month, quarter,
         game, event_type, source, page_id }
```

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
  - `timeline_search(game, event_type, from_date, to_date, limit)` — 이벤트 이력
  - `hybrid_search(query, limit)` — 벡터 + 그래프 통합 + **Parent Document Retrieval**

**Parent Document Retrieval (2026-09-03 적용)**
```
벡터 유사도 → 상위 k 청크 → page_id 수집
                                ↓
          Qdrant scroll (page_id MatchAny 필터)
                                ↓
     같은 page_id의 모든 청크 → chunk_index 정렬 → 전체 본문 조합
                                ↓
     반환: {title, source_url, content(최대 4000자), chunk_count, score}
```
- 기존: `text_preview` (300자 미리보기) → 청크 단위 단편적 문맥
- 개선: `content` (전체 페이지, 최대 4000자) → 완전한 문맥 해석
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
  | `GET /api/qdrant-chunks` | **특정 `page_id`의 모든 청크 조회** (신규) |
  | `GET /api/graph-stats` | FalkorDB 노드·엣지·이벤트 수 |
  | `GET /api/sync-log` | 동기화 이력 |
  | `POST /api/batch/run` | 배치 작업 실행 (fetch/ingest/sync 등) |

- **`/api/qdrant-chunks` 기능 (2026-09-03 추가)**:
  - 페이지 목록에서 status=ok 행마다 **🔍 청크** 버튼 표시
  - 클릭 시 모달 팝업: 해당 page_id의 Qdrant 청크 전체를 `chunk_index` 순으로 표시
  - 각 청크의 내용, 길이, UUID, Notion 원본 링크 확인 가능
  - Parent Document Retrieval 조합 결과를 사전 검증하는 용도

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

`ingest.py --reset` 의 FalkorDB 삭제는 예외를 묵음 처리하므로 실패해도 알 수 없음.
완전 초기화가 필요할 때는 수동으로 삭제:

```bash
python3 -c "
import falkordb
falkordb.FalkorDB(host='localhost', port=6379).select_graph('strategic_kg').delete()
print('FalkorDB 삭제 완료')
"
```

> `delete_graph()` 메서드 없음 → `select_graph().delete()` 사용

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
| 14 | Cortex Analyst YAML 모델 | 🔜 | KPI/매출 테이블 시맨틱 모델 작성 필요 |
| 15 | End-to-End 통합 테스트 | 🔜 | Snowflake ↔ Semantica ↔ Cortex 전구간 |
| 16 | EntityDeduplicator (그래프 중복 병합) | 🔜 | 향후 개선 |
| 17 | HTTPS 고정 URL (ngrok 유료 or 도메인) | 🔜 | 프로덕션 시 필요 |

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
