# Semantica — 프로젝트 전체 구조 요약

> JoyCity 전략사업본부 Notion 기반 온톨로지 검색 솔루션  
> 최종 수정: 2026-09-01 (시계열 이벤트 온톨로지 지원 추가)

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
- 의사결정 체인 추적 (`:Decision` 노드 + `LED_TO` 엣지)
- **시계열 이벤트 온톨로지** (`:Event`/`:Game` 노드 + `HAD_EVENT`/`FOLLOWED_BY` 엣지)

---

## 2. 전체 아키텍처

```
Notion API
    │
    ▼
[notion_fetch.py]        ← 페이지 수집 (전체 or 키워드 검색)
    │
    ▼
[ingest.py / sync.py]    ← 청킹(800자) → 임베딩 → Qdrant + FalkorDB 저장
    │                       LLM 트리플 추출 (subject / predicate / object)
    │                       결정 키워드 트리플 → :Decision 노드 + LED_TO 엣지
    ├──→ Qdrant            벡터 스토어 (768차원, text-multilingual-embedding-002)
    ├──→ FalkorDB          그래프 스토어 (노드: 엔티티/Decision, 엣지: REL/LED_TO)
    └──→ PostgreSQL        sync_log 테이블에 작업 결과 기록

[event_import.py]        ← 시계열 이벤트 CSV/JSON 일괄 임포트
    │                       --file / 단건 직접 입력 / --list 조회 모드
    ├──→ FalkorDB          :Event/:Game 노드 + HAD_EVENT/FOLLOWED_BY 엣지
    └──→ Qdrant            이벤트 설명 텍스트 벡터 저장

[server.py] (MCP)        ← FastMCP, Streamable HTTP (/mcp, 포트 8765)
    │                       Claude Code에서 MCP 도구로 호출
    ├── semantic_search   벡터 검색 (Qdrant query_points)
    ├── graph_search      그래프 탐색 (FalkorDB Cypher)
    ├── hybrid_search     벡터 + 그래프 결합 (복합 쿼리 자동 분해)
    ├── path_search       두 엔티티 간 최단 경로 탐색
    ├── decision_trace    의사결정 인과 체인 탐색 (:Decision → LED_TO)
    ├── timeline_search   시계열 이벤트 이력 조회 (:Event → FOLLOWED_BY)
    └──→ PostgreSQL       mcp_request_log 테이블에 호출 기록

[scripts/backup.sh]      ← 매일 새벽 3시 자동 백업
    ├── Qdrant            Snapshot API → .snapshot 파일
    ├── FalkorDB          BGSAVE → dump.rdb + appendonly.aof
    └── PostgreSQL        pg_dump → .sql.gz
```

---

## 3. 기술 스택

| 역할 | 기술 |
|------|------|
| 임베딩 모델 | Google Vertex AI `text-multilingual-embedding-002` (768차원) |
| LLM (트리플 추출 / 쿼리 분해) | Anthropic Claude (AnthropicVertex / claude-haiku-4-5) |
| 벡터 스토어 | Qdrant (Docker) |
| 그래프 스토어 | FalkorDB (Docker, AOF 활성화) |
| MCP 프레임워크 | FastMCP (Streamable HTTP) |
| Notion API | REST API v2022-06-28 |
| 운영 로그 DB | PostgreSQL 16 (Docker, psycopg2-binary) |
| 서버 인프라 | GCP Ubuntu (34.42.7.50) |
| 프로세스 관리 | systemd (`sementica-mcp.service`) |
| 스케줄러 | cron (동기화 새벽 2시 / 백업 새벽 3시) |

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
│   │   ├── ingest.py            # 전체 인제스천 (벡터 + 그래프 + Decision 노드)
│   │   ├── sync.py              # 증분 동기화 (--limit / --full / --dry-run)
│   │   ├── event_import.py      # 시계열 이벤트 CSV/JSON 일괄 임포트 (신규)
│   │   └── semantica_helper.py  # 엔티티 중복 제거, 경로 탐색, 의사결정 추적,
│   │                            #   이벤트 노드 관리 (upsert_event_node, get_event_chain)
│   │
│   ├── mcp/
│   │   └── server.py            # FastMCP MCP 서버 (6가지 도구)
│   │
│   ├── ops/
│   │   ├── __init__.py
│   │   └── db_logger.py         # PostgreSQL 운영 로그 기록 모듈
│   │
│   └── eval/
│       ├── evaluate.py          # 골든셋 평가 (--golden 외부 골든셋 지원)
│       └── gen_golden_set.py    # 실데이터 기반 골든셋 자동 생성
│
├── scripts/
│   ├── backup.sh                # DB 백업 (Qdrant + FalkorDB + PostgreSQL)
│   └── test_mcp.py              # MCP 6개 도구 연결 검증 스크립트
│
├── data/
│   ├── strategic/
│   │   ├── notion_pages/        # 수집된 Notion 페이지 (.md)
│   │   └── sync_state.json      # 마지막 동기화 시각 기록
│   ├── events/                  # 이벤트 임포트 CSV/JSON 파일 보관
│   ├── backups/                 # 로컬 백업 (YYYYMMDD_HHMMSS/qdrant|falkordb|postgres)
│   ├── eval/
│   │   ├── golden_set_YYYYMMDD.json    # 자동 생성 골든셋
│   │   ├── eval_result_*.json          # 평가 결과 JSON
│   │   └── eval_report_*.md            # 평가 리포트 Markdown
│   └── logs/
│       ├── mcp_server.log       # MCP 서버 로그
│       ├── sync_cron.log        # cron 동기화 로그
│       └── backup.log           # 백업 로그
│
├── docker-compose.yml           # Qdrant + FalkorDB + FalkorDB Browser + PostgreSQL
├── setup_cron.sh                # cron 등록 스크립트 (동기화 + 백업)
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
python src/pipeline/notion_fetch.py --dept strategic
python src/pipeline/notion_fetch.py --dept strategic --search "프로세스"
python src/pipeline/notion_fetch.py --dept strategic --page-id <PAGE_ID>
```

---

### 5-3. 인제스천 (`ingest.py`)

```bash
python src/pipeline/ingest.py --dept strategic --reset  # 초기 전체 인제스천
python src/pipeline/ingest.py --dept strategic           # 추가 인제스천
```

**처리 흐름:**
1. Notion 텍스트 → **청킹** (800자, 200자 오버랩) → Vertex AI 임베딩 → **Qdrant upsert**
2. 페이지 전체 → Claude → **트리플 추출** → FalkorDB 노드/엣지 저장
3. 결정 키워드 트리플 → **`:Decision` 노드** 생성 + **`LED_TO`** 인과 엣지 자동 연결

**결정 키워드 (26개):** 승인, 결정, 채택, 선택, 완료, 확정, 검토, 허가, 처리, 배정, 지정, 선정, 의결, 보고, 승낙, 거부, 반려, 취소, 변경, 수정, 합의, 위임, 지시, 요청, 승계, 이관

---

### 5-4. 증분 동기화 (`sync.py`)

```bash
python src/pipeline/sync.py --dept strategic --search "프로세스"       # 키워드 필터
python src/pipeline/sync.py --dept strategic --full                    # 전체 재동기화
python src/pipeline/sync.py --dept strategic --search "프로세스" --limit 50  # 50페이지만
python src/pipeline/sync.py --dept strategic --dry-run                 # 확인만
```

**동작 원리:**
1. `sync_state.json`의 `last_sync_time` 기준으로 수정 페이지 감지
2. Qdrant: 기존 벡터 삭제 후 재임베딩
3. FalkorDB: `is_manual=true` 수동 편집 엣지 보존, 나머지 삭제 후 재삽입
4. 결정 키워드 트리플 → `:Decision` 노드 + `LED_TO` 엣지 갱신

---

### 5-5. MCP 서버 (`server.py`)

```bash
# 실행
python src/mcp/server.py --dept strategic

# Claude Code 등록 (1회)
claude mcp add --transport http strategic-ontology http://34.42.7.50:8765/mcp

# 연결 검증
python scripts/test_mcp.py --url http://localhost:8765
```

**제공 도구 (5개):**

| 도구 | 설명 | 사용 사례 |
|------|------|-----------|
| `semantic_search` | Qdrant 벡터 검색 | 문서 내용, 정책 검색 |
| `graph_search` | FalkorDB Cypher 탐색 | 담당자, 팀, 관계 질의 |
| `hybrid_search` | 벡터 + 그래프 결합 + **복합 쿼리 자동 분해** | 복합 질문 |
| `path_search` | 두 엔티티 간 최단 경로 | "A와 B는 어떤 관계?" |
| `decision_trace` | 의사결정 인과 체인 탐색 | "A 승인 경위는?" |
| `timeline_search` | 게임/서비스 시계열 이벤트 이력 조회 | "POTC 2026년 이벤트 목록" |

**`hybrid_search` 복합 쿼리 분해:**
- 15자 이상 + 복합 패턴(담당하는, 작성한, 관련된 등) 또는 6단어 이상 쿼리 자동 감지
- Claude Haiku로 서브쿼리 2~3개 분해 → 각각 검색 → URL 중복 제거 + coverage 가중 재랭킹 병합
- 응답에 `decomposed: true`, `sub_queries: [...]` 포함

**접근 제어:** GCP 방화벽 IP 화이트리스트 (포트 8765) — 앱 레벨 API Key 없음

**MCP 세션 초기화:**
```bash
# 세션 ID 획득 후 호출 필요
curl -si -X POST http://localhost:8765/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{...}}'
```

---

### 5-6. 의사결정 체인 (`semantica_helper.py`)

```python
from semantica_helper import is_decision_triplet, record_decision_node, trace_decision_chain

# 인제스천/동기화 시점에 자동 생성
if is_decision_triplet(triplet):
    record_decision_node(graph, triplet, source_url)

# MCP decision_trace 도구 내부 호출
result = trace_decision_chain(graph, "운영팀", max_depth=4)
# → {entity, found, decisions: [...], chain_summary: [...]}
```

- **`decision_id`**: `uuid5(source_url|subject|action|outcome)` — 안정적 중복 제거
- **`LED_TO` 엣지**: 이전 결정의 `outcome` == 다음 결정의 `subject`일 때 자동 연결

---

### 5-7. 운영 로그 (`src/ops/db_logger.py`)

`POSTGRES_URL` 없으면 no-op (서버/sync 중단 없음).

```sql
mcp_request_log (id, ts, dept, tool, query, result_count, duration_ms, error)
sync_log        (id, ts, dept, search_keyword, since_time, modified_found,
                 processed, skipped, errors, new_chunks, new_triplets, duration_sec,
                 status, error_detail)
```

---

---

## 5-7. 시계열 이벤트 온톨로지

### 그래프 스키마

```
(:Game {name, platform, genre, source_url})
    │
    ├─[:HAD_EVENT {date}]──→ (:Event {
    │                           event_id,    # uuid5(game|event_type|date)
    │                           game,        # 게임명 (비정규화)
    │                           event_type,  # client_update | server_update |
    │                           date,        #   user_event | season |
    │                           date_ts,     #   content_release | maintenance |
    │                           year,        #   incident | kpi_milestone
    │                           month,
    │                           quarter,     # "Q1"~"Q4"
    │                           title,
    │                           description,
    │                           target,      # "신규유저,복귀유저"
    │                           source_url,
    │                           ts           # 기록 시각
    │                         })
    │                           │
    │                           └─[:FOLLOWED_BY {days_diff}]──→ (다음 이벤트)
    │                           └─[:MANAGED_BY]──────────────→ (:Team/:Person)
```

### 데이터 입력 방법

```bash
# 1. Notion 문서 자동 감지 (ingest/sync 시 날짜 명시 이벤트 자동 추출)
python src/pipeline/ingest.py --dept strategic

# 2. CSV 일괄 임포트
python src/pipeline/event_import.py --dept strategic \
    --file data/events/potc_2026.csv

# CSV 컬럼: game, event_type, date, title, description, target, manager, source_url

# 3. 단건 직접 입력
python src/pipeline/event_import.py --dept strategic \
    --game POTC --event-type client_update \
    --date 2026-04-12 --title "클라이언트 업데이트"

# 4. 이벤트 목록 조회
python src/pipeline/event_import.py --dept strategic \
    --list --game POTC --from-date 2026-01-01
```

### MCP timeline_search 사용 예

```
timeline_search(game="POTC")
timeline_search(game="POTC", event_type="user_event", from_date="2026-01-01")
timeline_search(game="POTC", from_date="2026-04-01", to_date="2026-09-30")
```

### 시계열 쿼리 패턴 (Cypher 직접 사용 시)

```cypher
-- POTC 이벤트 시간순 전체 조회
MATCH (g:Game {name:"POTC"})-[:HAD_EVENT]->(e:Event)
RETURN e.date, e.title, e.event_type ORDER BY e.date_ts ASC

-- 클라이언트 업데이트 이후 첫 이벤트
MATCH (e1:Event {event_type:"client_update", date:"2026-04-12"})
      -[:FOLLOWED_BY*1..3]->(e2:Event)
RETURN e1.title, e2.date, e2.title

-- 2026년 Q2에 user_event를 진행한 게임
MATCH (g:Game)-[:HAD_EVENT]->(e:Event {event_type:"user_event"})
WHERE e.year = 2026 AND e.quarter = "Q2"
RETURN g.name, e.date, e.title
```

---

## 6. 인프라 (GCP)

### Docker 서비스 (`docker-compose.yml`)

| 서비스 | 포트 | 역할 |
|--------|------|------|
| Qdrant | 6333 | 벡터 스토어 |
| FalkorDB | 6379 | 그래프 스토어 (AOF 활성화: `--appendonly yes --appendfsync everysec`) |
| FalkorDB Browser | 3000 | 그래프 시각화 UI |
| PostgreSQL | 5432 | 운영 로그 DB (auto-init: `schema/ops_log.sql`) |

```bash
docker-compose up -d
```

### systemd 서비스

```bash
sudo systemctl enable sementica-mcp
sudo systemctl start  sementica-mcp
sudo systemctl restart sementica-mcp   # 코드 변경 후 재시작 필요
sudo systemctl status sementica-mcp
```

### cron 자동화

```bash
# 등록 (동기화 새벽 2시 + 백업 새벽 3시)
bash setup_cron.sh --dept strategic --hour 2 --search "프로세스"

# 동기화만
bash setup_cron.sh --dept strategic --no-backup

# 백업만
bash setup_cron.sh --backup-only
```

---

## 7. 백업 정책 (`scripts/backup.sh`)

```bash
bash scripts/backup.sh                  # 전체 백업
bash scripts/backup.sh --qdrant-only
bash scripts/backup.sh --falkordb-only
bash scripts/backup.sh --postgres-only
bash scripts/backup.sh --restore        # 복구 가이드 출력
```

| DB | 방법 | 보존 |
|----|------|------|
| Qdrant | Snapshot API → `.snapshot` | 로컬 7일 |
| FalkorDB | `BGSAVE` → `dump.rdb` + `appendonly.aof` | 로컬 7일 |
| PostgreSQL | `pg_dump` → `.sql.gz` | 로컬 7일 |

- 백업 위치: `data/backups/YYYYMMDD_HHMMSS/`
- GCS 오프사이트: `.env`에 `GCS_BUCKET=gs://...` 설정 시 자동 업로드 (미설정 시 로컬만)
- 컨테이너명 설정: `.env`에 `FALKORDB_CONTAINER=sementica-falkordb` (기본값)

---

## 8. 환경변수 (`.env`)

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
FALKORDB_CONTAINER=sementica-falkordb   # backup.sh 컨테이너명

# PostgreSQL 운영 로그
POSTGRES_URL=postgresql://semantica:<password>@localhost:5432/semantica
POSTGRES_PASSWORD=<password>            # docker-compose PostgreSQL 초기화용

# 백업
BACKUP_RETENTION_DAYS=7                 # 로컬 백업 보존 일수
GCS_BUCKET=gs://joycity-sementica-backup  # 미설정 시 로컬만 유지
```

---

## 9. 검색 품질 평가

### 골든셋 자동 생성

```bash
# 실데이터 기반 골든셋 생성 (약 20분, 검증 포함)
python src/eval/gen_golden_set.py --dept strategic --count 20
```

**생성 흐름:**
1. Qdrant에서 60개 페이지 랜덤 샘플링
2. FalkorDB에서 80개 관계 샘플링
3. Claude로 카테고리별 Q&A 생성 (담당자·정책/규정·관계·문서위치·복합)
4. **검증**: 생성 즉시 실제 검색 → 답변 생성 → 정답 채점 → 통과한 Q&A만 채택
5. 카테고리·난이도 균형 조정 후 `data/eval/golden_set_YYYYMMDD.json` 저장

### 평가 실행

```bash
# 내장 기본 골든셋으로 평가
python src/eval/evaluate.py --dept strategic

# 자동 생성 골든셋으로 평가
python src/eval/evaluate.py --dept strategic --golden data/eval/golden_set_YYYYMMDD.json
```

### 최근 평가 결과 (내장 기본 골든셋 기준)

| 카테고리 | 점수 | 문항 수 |
|---------|------|--------|
| 정책/규정 | **1.00** | 4 |
| 담당자 | **0.90** | 5 |
| 문서위치 | 0.83 | 3 |
| 관계 | 0.80 | 5 |
| 복합 | 0.67 | 3 |
| **전체 평균** | **0.850** | **20** |

- 목표: 0.70 → **달성**
- `hybrid_search` 복합 쿼리 분해 적용 후 복합 카테고리 지속 개선 중

---

## 10. 신규 본부 확장 방법

1. `config/departments.yaml`에 본부 항목 추가
2. `.env`에 해당 본부 Notion 토큰 추가
3. 페이지 수집: `python src/pipeline/notion_fetch.py --dept <new_dept>`
4. 인제스천: `python src/pipeline/ingest.py --dept <new_dept> --reset`
5. MCP 서버 실행: `python src/mcp/server.py --dept <new_dept>`
6. Claude Code 등록: `claude mcp add --transport http <name> http://<IP>:<포트>/mcp`
7. cron 등록: `bash setup_cron.sh --dept <new_dept>`
8. 골든셋 생성: `python src/eval/gen_golden_set.py --dept <new_dept>`

---

## 11. 주요 운영 명령어 참조

```bash
# ─── 서비스 관리 ───────────────────────────────────────────
sudo systemctl restart sementica-mcp
sudo systemctl status  sementica-mcp
tail -f data/logs/mcp_server.log

# ─── MCP 연결 검증 ────────────────────────────────────────
python scripts/test_mcp.py

# ─── 동기화 ────────────────────────────────────────────────
.venv/bin/python src/pipeline/sync.py --dept strategic --dry-run
.venv/bin/python src/pipeline/sync.py --dept strategic --search "프로세스" --limit 50

# ─── 백업 ──────────────────────────────────────────────────
bash scripts/backup.sh
bash scripts/backup.sh --restore        # 복구 절차 안내

# ─── 이벤트 임포트 ──────────────────────────────────────────
# CSV 일괄 임포트
python src/pipeline/event_import.py --dept strategic \
    --file data/events/potc_2026.csv
# 단건 직접 입력
python src/pipeline/event_import.py --dept strategic \
    --game POTC --event-type client_update \
    --date 2026-04-12 --title "클라이언트 업데이트"
# 이벤트 목록 조회
python src/pipeline/event_import.py --dept strategic --list --game POTC

# ─── 평가 ──────────────────────────────────────────────────
python src/eval/gen_golden_set.py --dept strategic --count 20
python src/eval/evaluate.py --dept strategic \
  --golden data/eval/golden_set_$(date +%Y%m%d).json

# ─── FalkorDB 인덱스 (FalkorDB Browser에서 실행) ──────────
# CREATE INDEX ON :Person(name)
# CREATE INDEX ON :Team(name)
# CREATE INDEX ON :Process(name)
# CREATE INDEX ON :Decision(subject)
# CREATE INDEX ON :Event(game)
# CREATE INDEX ON :Event(date_ts)
# CREATE INDEX ON :Game(name)

# ─── sync_state 초기화 ────────────────────────────────────
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

# ─── cron 확인 ────────────────────────────────────────────
crontab -l
```

---

## 12. 향후 예정 작업 (Deferred)

| 항목 | 내용 |
|------|------|
| GCS 오프사이트 백업 | `.env`에 `GCS_BUCKET` 설정 시 자동 활성화 |
| Snowflake Cortex 연동 | Cloudflare Tunnel + External Network Access + Python UDF |
| 복합 카테고리 점수 개선 | 골든셋 품질 향상 + limit 조정 |
| FalkorDB Decision 노드 활성화 | 전체 re-sync 후 decision_trace 실전 검증 |

---

*Semantica — JoyCity 전략사업본부 온톨로지 검색 솔루션*
