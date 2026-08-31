# Semantica — 프로젝트 전체 구조 요약

> JoyCity 전략사업본부 Notion 기반 온톨로지 검색 솔루션  
> 최종 수정: 2026-08

---

## 1. 프로젝트 개요

Notion에 축적된 업무 문서(담당자, 팀, 프로세스, 정책 등)를 **벡터 검색**과 **지식 그래프**로 색인하고,  
**MCP(Model Context Protocol)** 서버를 통해 Claude Code에서 자연어로 질의할 수 있도록 하는 시스템.

### 핵심 목표
- Notion 문서 → 벡터(Qdrant) + 그래프(FalkorDB) 이중 색인
- Claude Code에서 MCP 도구를 통한 시맨틱 검색
- 본부별 독립 파이프라인 (멀티 본부 확장 구조)
- 수정 페이지 자동 증분 동기화 (cron)
- 운영 로그 PostgreSQL 저장

---

## 2. 전체 아키텍처

```
Notion API
    │
    ▼
[notion_fetch.py]        ← 페이지 수집 (전체 or 키워드 검색)
    │
    ▼
[ingest.py]              ← 청킹(800자) → 임베딩 → Qdrant + FalkorDB 저장
    │                       LLM 트리플 추출 (subject / predicate / object)
    ├──→ Qdrant            벡터 스토어 (768차원, text-multilingual-embedding-002)
    └──→ FalkorDB          그래프 스토어 (노드: 엔티티, 엣지: 관계)

[sync.py] (cron)         ← 증분 동기화: 수정된 페이지만 재처리
    │                       수동 편집 엣지(is_manual=true) 보존
    └──→ PostgreSQL        sync_log 테이블에 작업 결과 기록

[server.py] (MCP)        ← FastMCP 3.4.7, Streamable HTTP (/mcp)
    │                       Claude Code에서 MCP 도구로 호출
    ├── semantic_search   벡터 검색 (Qdrant query_points)
    ├── graph_search      그래프 탐색 (FalkorDB Cypher)
    └── hybrid_search     벡터 + 그래프 결합
          │
          └──→ PostgreSQL  mcp_request_log 테이블에 호출 기록
```

---

## 3. 기술 스택

| 역할 | 기술 |
|------|------|
| 임베딩 모델 | Google Vertex AI `text-multilingual-embedding-002` (768차원) |
| LLM (트리플 추출) | Anthropic Claude (AnthropicVertex) |
| 벡터 스토어 | Qdrant (Docker) |
| 그래프 스토어 | FalkorDB (Docker) |
| MCP 프레임워크 | FastMCP 3.4.7 (Streamable HTTP) |
| Notion API | REST API v2022-06-28 |
| 운영 로그 DB | PostgreSQL (psycopg2-binary) |
| 서버 인프라 | GCP Ubuntu (34.42.7.50) |
| 프로세스 관리 | systemd (`sementica-mcp.service`) |
| 스케줄러 | cron (매일 새벽 2시) |

---

## 4. 디렉토리 구조

```
sementica/
├── config/
│   └── departments.yaml         # 본부별 설정 (컬렉션, 그래프, 포트, 토큰 env)
│
├── schema/
│   └── ops_log.sql              # PostgreSQL 운영 로그 테이블 DDL
│
├── src/
│   ├── pipeline/
│   │   ├── dept_config.py       # departments.yaml 로더
│   │   ├── notion_fetch.py      # Notion 페이지 수집 (전체 / 검색어)
│   │   ├── ingest.py            # 전체 인제스천 (벡터 + 그래프)
│   │   └── sync.py              # 증분 동기화 (cron 대상)
│   │
│   ├── mcp/
│   │   └── server.py            # FastMCP MCP 서버 (3가지 도구)
│   │
│   ├── ops/
│   │   ├── __init__.py
│   │   └── db_logger.py         # PostgreSQL 운영 로그 기록 모듈
│   │
│   └── eval/
│       └── evaluate.py          # 평가 스크립트 (RAG 품질 측정)
│
├── data/
│   ├── strategic/
│   │   ├── notion_pages/        # 수집된 Notion 페이지 (.md)
│   │   └── sync_state.json      # 마지막 동기화 시각 기록
│   └── logs/
│       ├── mcp_server.log       # MCP 서버 로그
│       ├── sync_cron.log        # cron 동기화 로그
│       └── sync_strategic_YYYYMMDD.json  # 동기화 결과 JSON
│
├── docker-compose.yml           # Qdrant + FalkorDB + FalkorDB Browser
├── setup_cron.sh                # cron 등록 스크립트
├── requirements.txt
├── .env                         # 환경변수 (토큰, GCP 프로젝트 등)
└── project_summary.md           # 이 문서
```

---

## 5. 주요 컴포넌트 상세

### 5-1. 본부 설정 (`config/departments.yaml`)

```yaml
departments:
  strategic:
    name: "전략사업본부"
    notion_token_env: "NOTION_TOKEN"   # .env에서 읽는 환경변수명
    qdrant_collection: "strategic_pages"
    falkordb_graph:    "strategic_kg"
    mcp_port:          8765
    data_dir:          "data/strategic"
```

- 본부 추가 시 이 파일에 항목 추가 → `.env`에 토큰 추가 → fetch + ingest + server 실행
- `dept_config.py`의 `load_dept(dept_name)`으로 로드

---

### 5-2. Notion 수집 (`notion_fetch.py`)

```bash
# 전체 수집
python src/pipeline/notion_fetch.py --dept strategic

# 키워드 검색 수집
python src/pipeline/notion_fetch.py --dept strategic --search "프로세스"

# 특정 페이지
python src/pipeline/notion_fetch.py --dept strategic --page-id <PAGE_ID>
```

- API 권한 내 페이지를 페이지네이션으로 수집 (100개 단위)
- 블록 재귀 파싱 → Markdown 텍스트 변환 (최대 depth 4)
- 결과: `data/{dept}/notion_pages/*.md` + `fetch_summary.json`

---

### 5-3. 인제스천 (`ingest.py`)

```bash
# 초기 전체 인제스천 (컬렉션/그래프 초기화 후 전체 재처리)
python src/pipeline/ingest.py --dept strategic --reset

# 추가 인제스천 (기존 데이터 유지)
python src/pipeline/ingest.py --dept strategic
```

**처리 흐름:**
1. Notion 페이지 텍스트 → **청킹** (800자 단위, 200자 오버랩)
2. 각 청크 → Vertex AI 임베딩 → **Qdrant upsert**
   - `chunk_id` = `uuid5(f"{source_url}#chunk{i}")`
   - payload: `{title, source_url, page_id, text, chunk_index, chunk_total}`
3. 페이지 전체 텍스트 → Claude LLM → **트리플 추출**
   - 형식: `subject(name, type) / predicate(name, condition, order) / object(name, type)`
4. 트리플 → **FalkorDB 노드 + 엣지 저장**

---

### 5-4. 증분 동기화 (`sync.py`)

```bash
# 키워드 필터 증분 동기화 (cron 대상)
python src/pipeline/sync.py --dept strategic --search "프로세스"

# 전체 재동기화
python src/pipeline/sync.py --dept strategic --full

# 확인만 (변경 없음)
python src/pipeline/sync.py --dept strategic --search "프로세스" --dry-run
```

**동작 원리:**
1. `data/strategic/sync_state.json`에서 `last_sync_time` 읽기
2. Notion 검색 API에 `--search` 키워드로 조회 → `last_edited_time > last_sync_time` 필터
3. 수정된 페이지마다:
   - Qdrant: `source_url` 필터로 **기존 벡터 삭제** (`is_manual` 제외 없음)
   - FalkorDB: `source_url` 필터로 **자동 생성 엣지만 삭제** (`is_manual=true` 엣지 보존)
   - 재임베딩 + 재삽입
4. `sync_state.json` 갱신 + PostgreSQL `sync_log` 기록

**수동 편집 보호:**
FalkorDB에서 사용자가 직접 편집한 엣지는 `is_manual: true` 속성을 부여하면 cron 동기화 시 삭제되지 않음.

```cypher
-- 수동 편집 엣지 보호 설정
MATCH ()-[r:REL {source_url: "...", rel_name: "업로드"}]->()
SET r.condition = "매월 1일 한정", r.is_manual = true
```

---

### 5-5. MCP 서버 (`server.py`)

```bash
# 실행
python src/mcp/server.py --dept strategic

# Claude Code 등록 (로컬에서 1회 실행)
claude mcp add --transport http strategic-ontology http://34.42.7.50:8765/mcp
```

**제공 도구:**

| 도구 | 설명 | 사용 사례 |
|------|------|-----------|
| `semantic_search` | Qdrant 벡터 검색 | 문서 내용, 정책 검색 |
| `graph_search` | FalkorDB Cypher 탐색 | 담당자, 팀, 관계 질의 |
| `hybrid_search` | 벡터 + 그래프 결합 | 복합 질문 |
| `path_search` | 두 엔티티 간 최단 경로 | "A와 B는 어떤 관계?" |
| `decision_trace` | 의사결정 인과 체인 탐색 | "A 승인 경위는?", "팀이 내린 결정들은?" |

- **Transport**: Streamable HTTP (`/mcp` 엔드포인트)
- **프로세스 관리**: systemd (`sementica-mcp.service`) — 서버 재부팅 시 자동 시작
- **로그**: `data/logs/mcp_server.log` + PostgreSQL `mcp_request_log`

---

### 5-6. 운영 로그 (`src/ops/db_logger.py`)

`POSTGRES_URL` 환경변수가 없으면 no-op으로 동작 (서버/sync 중단 없음).

**테이블 구조:**

```sql
-- MCP 도구 호출 로그
mcp_request_log (id, ts, dept, tool, query, result_count, duration_ms, error)

-- 동기화 작업 로그
sync_log (id, ts, dept, search_keyword, since_time, modified_found,
          processed, skipped, errors, new_chunks, new_triplets, duration_sec,
          status, error_detail)
```

**초기화:**
```bash
psql $POSTGRES_URL -f schema/ops_log.sql
```

---

## 6. 인프라 (GCP)

### Docker 서비스 (`docker-compose.yml`)

| 서비스 | 포트 | 역할 |
|--------|------|------|
| Qdrant | 6333 | 벡터 스토어 |
| FalkorDB | 6379 | 그래프 스토어 |
| FalkorDB Browser | 3000 | 그래프 시각화 UI |

```bash
docker-compose up -d
```

### systemd 서비스

```bash
# MCP 서버 자동 시작 서비스
sudo systemctl enable sementica-mcp
sudo systemctl start  sementica-mcp
sudo systemctl status sementica-mcp
```

### cron 자동 동기화

```bash
# 등록
bash setup_cron.sh --dept strategic --hour 2 --search "프로세스"

# 등록된 cron (매일 새벽 2시)
0 2 * * * cd ~/sementica && .venv/bin/python src/pipeline/sync.py \
  --dept strategic --search '프로세스' >> data/logs/sync_cron.log 2>&1
```

---

## 7. 환경변수 (`.env`)

```env
# Notion
NOTION_TOKEN=secret_xxx

# Google Cloud / Vertex AI
GOOGLE_CLOUD_PROJECT=joycity-xxx
VERTEX_AI_LOCATION=us-east5

# Qdrant
QDRANT_URL=http://localhost:6333

# FalkorDB
FALKORDB_HOST=localhost
FALKORDB_PORT=6379

# PostgreSQL 운영 로그
POSTGRES_URL=postgresql://user:pass@host:5432/dbname
```

---

## 8. 평가 결과

```bash
python src/eval/evaluate.py --dept strategic
```

| 단계 | 조건 | 점수 |
|------|------|------|
| 초기 | 5000자 텍스트, 7페이지 | 0.450 |
| 개선 1 | 5000자 텍스트 확장 | 0.625 |
| 개선 2 | 800자 청킹 적용, 7페이지 | **0.950** |
| 실전 | 48페이지 (전략사업본부) | 0.900 |

---

## 9. 신규 본부 확장 방법

1. `config/departments.yaml`에 본부 항목 추가
2. `.env`에 해당 본부 Notion 토큰 추가
3. 페이지 수집: `python src/pipeline/notion_fetch.py --dept <new_dept>`
4. 인제스천: `python src/pipeline/ingest.py --dept <new_dept> --reset`
5. MCP 서버 실행: `python src/mcp/server.py --dept <new_dept> --port <포트>`
6. Claude Code 등록: `claude mcp add --transport http <name> http://<IP>:<포트>/mcp`
7. cron 등록: `bash setup_cron.sh --dept <new_dept>`

---

## 10. 주요 운영 명령어 참조

```bash
# 서비스 상태 확인
sudo systemctl status sementica-mcp

# MCP 서버 로그 실시간 확인
tail -f data/logs/mcp_server.log

# 수동 동기화 (dry-run)
.venv/bin/python src/pipeline/sync.py --dept strategic --search "프로세스" --dry-run

# sync_state 초기화 (인제스천 완료 후)
python3 -c "
import json, datetime
from pathlib import Path
p = Path('data/strategic/sync_state.json')
p.write_text(json.dumps({
    'last_sync_time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'total_synced': 0
}, ensure_ascii=False, indent=2))
print(p.read_text())
"

# FalkorDB 인덱스 생성 (속도 개선)
# FalkorDB Browser에서 실행:
# CREATE INDEX ON :Person(name)
# CREATE INDEX ON :Team(name)
# CREATE INDEX ON :Process(name)

# cron 확인
crontab -l
```

---

*Semantica — JoyCity 전략사업본부 온톨로지 검색 솔루션*
