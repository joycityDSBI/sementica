# Semantica — 프로젝트 전체 요약

> JoyCity 전략사업본부 Notion 기반 온톨로지 검색 솔루션  
> 최종 업데이트: 2026-09-03

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

### 4-1. 전체 수집 (Full Ingest)
```bash
python src/pipeline/ingest.py --dept strategic
```
- Notion API에서 전체 페이지 수집
- 텍스트 청크 분할 → 임베딩 → Qdrant 저장
- LLM으로 트리플(주어-관계-목적어) 추출 → FalkorDB 저장
- PostgreSQL `notion_pages` 테이블 UPSERT

### 4-2. 증분 동기화 (Incremental Sync)
```bash
python src/pipeline/sync.py --dept strategic
```
- PostgreSQL `notion_pages`에서 `last_edited_time` 불러옴
- Notion API와 per-page 비교:
  - `page_id`가 DB에 없음 → 신규 페이지 (무조건 처리)
  - Notion 수정 시각 > DB 저장 시각 → 변경된 페이지
  - 동일하면 skip
- 처리 결과를 `sync_log`에 기록

### 4-3. 노드/엣지 구조 (FalkorDB)

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

### 5-2. REST API 서버 (`src/mcp/rest_api.py`)
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

## 7. 운영 스크립트

| 스크립트 | 용도 |
|---------|------|
| `scripts/start_with_ngrok.sh` | REST API + ngrok 동시 시작 |
| `scripts/backup.sh` | 로컬 백업 |
| `scripts/backup_to_gcs.sh` | GCS 백업 |
| `scripts/create_indexes.py` | Qdrant 인덱스 생성 |
| `scripts/test_mcp.py` | MCP 서버 테스트 |

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
| 9 | Cortex Analyst YAML 모델 | 🔜 | Step 4 — KPI/매출 테이블 필요 |
| 10 | Cortex Agent 도구 정의 | 🔜 | Step 5 |
| 11 | End-to-End 통합 테스트 | 🔜 | Step 6 |
| 12 | EntityDeduplicator (그래프 중복 병합) | 🔜 | 향후 개선 |
| 13 | HTTPS 고정 URL (ngrok 유료 or 도메인) | 🔜 | 프로덕션 시 필요 |

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
| FalkorDB 시각화 | 공식 UI 없음, `redis-cli` 또는 커스텀 웹앱으로 조회 |
| Snowflake 계정 | `SEONGIN-us-central1.gcp` |
