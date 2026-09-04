-- ================================================================
-- Semantica FalkorDB — 예시 쿼리 모음
-- ================================================================
-- 그래프: strategic_kg
-- 접속:   redis-cli -h localhost -p 6379
-- 실행:   GRAPH.QUERY strategic_kg "MATCH (n) RETURN n LIMIT 5"
--
-- ※ 모든 관계는 :REL 타입 고정 (FalkorDB 한국어 rel_type 미지원)
--    실제 관계명은 r.rel_name 속성에 저장
-- ================================================================


-- ────────────────────────────────────────────────────────────────
-- 0. 기본 통계 — 그래프 현황 파악
-- ────────────────────────────────────────────────────────────────

-- 전체 노드 수
MATCH (n) RETURN count(n) AS total_nodes;

-- 라벨별 노드 수
MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt
ORDER BY cnt DESC;

-- 관계명별 엣지 수 (r.rel_name 기준)
MATCH ()-[r:REL]->() RETURN r.rel_name AS relation, count(r) AS cnt
ORDER BY cnt DESC;

-- 전체 엣지 수
MATCH ()-[r]->() RETURN count(r) AS total_edges;


-- ────────────────────────────────────────────────────────────────
-- 예시 1. 특정 게임과 연결된 모든 엔티티
--         "POTC와 연관된 팀, 인물, 전략, 이벤트는?"
-- ────────────────────────────────────────────────────────────────
MATCH (g:Game {name: 'POTC'})-[r:REL]->(o)
RETURN
    g.name            AS game,
    r.rel_name        AS relation,
    labels(o)[0]      AS target_type,
    o.name            AS target,
    r.source_url      AS source_url
ORDER BY target_type, relation;

-- 역방향 포함 (POTC를 향하는 관계도 포함)
MATCH (n)-[r:REL]->(m)
WHERE n.name CONTAINS 'POTC' OR m.name CONTAINS 'POTC'
RETURN
    n.name        AS from_node,
    labels(n)[0]  AS from_type,
    r.rel_name    AS relation,
    m.name        AS to_node,
    labels(m)[0]  AS to_type,
    r.source_url  AS source_url
ORDER BY relation
LIMIT 50;


-- ────────────────────────────────────────────────────────────────
-- 예시 2. 특정 인물의 관계망
--         "이지인이 관련된 업무·팀·게임은?"
-- ────────────────────────────────────────────────────────────────
MATCH (p:Person)-[r:REL]->(o)
WHERE p.name CONTAINS '이지인'
RETURN
    p.name        AS person,
    r.rel_name    AS relation,
    labels(o)[0]  AS target_type,
    o.name        AS target,
    r.source_url  AS source_url;

-- 역방향: 이지인을 대상으로 하는 관계
MATCH (o)-[r:REL]->(p:Person)
WHERE p.name CONTAINS '이지인'
RETURN
    labels(o)[0]  AS source_type,
    o.name        AS source,
    r.rel_name    AS relation,
    p.name        AS person;

-- 2홉: 이지인과 연결된 노드가 다시 연결하는 곳까지
MATCH (p:Person)-[:REL*1..2]->(end)
WHERE p.name CONTAINS '이지인'
  AND p <> end
RETURN DISTINCT
    end.name       AS connected_node,
    labels(end)[0] AS node_type
LIMIT 30;


-- ────────────────────────────────────────────────────────────────
-- 예시 3. 게임별 이벤트 시계열
--         "POTC의 이벤트 이력을 날짜 순으로"
-- ────────────────────────────────────────────────────────────────
-- :Event 노드 직접 조회 (event_type, date 속성 활용)
MATCH (e:Event)
WHERE e.game = 'POTC'
RETURN
    e.date        AS date,
    e.event_type  AS event_type,
    e.title       AS title,
    e.source_url  AS source_url
ORDER BY e.date_ts ASC;

-- :Game → :Event 엣지 경유 (HAD_EVENT 관계)
MATCH (g:Game {name: 'POTC'})-[r:HAD_EVENT]->(e:Event)
RETURN
    r.date        AS date,
    e.event_type  AS event_type,
    e.title       AS title
ORDER BY r.date;

-- 전체 게임 이벤트 — 최근 30개
MATCH (e:Event)
RETURN
    e.game        AS game,
    e.date        AS date,
    e.event_type  AS event_type,
    e.title       AS title
ORDER BY e.date_ts DESC
LIMIT 30;

-- 이벤트 유형별 게임 집계
MATCH (e:Event)
RETURN e.game AS game, e.event_type AS event_type, count(e) AS cnt
ORDER BY game, cnt DESC;


-- ────────────────────────────────────────────────────────────────
-- 예시 4. 원인-결과 체인 탐색
--         "특정 이슈의 원인이 무엇이며, 무엇에 영향을 주는가?"
-- ────────────────────────────────────────────────────────────────
-- 특정 이슈로 들어오는 CAUSES 관계 (원인)
MATCH (cause)-[r:REL]->(issue:Issue)
WHERE r.rel_name = 'CAUSES'
  AND issue.name CONTAINS '이탈'
RETURN
    labels(cause)[0]  AS cause_type,
    cause.name        AS cause,
    r.rel_name        AS relation,
    issue.name        AS issue;

-- 특정 이슈에서 나가는 LEADS_TO / AFFECTS 관계 (결과)
MATCH (issue:Issue)-[r:REL]->(effect)
WHERE issue.name CONTAINS '이탈'
  AND r.rel_name IN ['LEADS_TO', 'AFFECTS']
RETURN
    issue.name        AS issue,
    r.rel_name        AS relation,
    labels(effect)[0] AS effect_type,
    effect.name       AS effect;

-- 원인 → 이슈 → 결과 전체 체인 (2홉)
MATCH (cause)-[r1:REL]->(issue:Issue)-[r2:REL]->(effect)
WHERE r1.rel_name = 'CAUSES'
  AND r2.rel_name IN ['LEADS_TO', 'AFFECTS', 'RESOLVES']
RETURN
    cause.name   AS cause,
    r1.rel_name  AS rel1,
    issue.name   AS issue,
    r2.rel_name  AS rel2,
    effect.name  AS effect
LIMIT 20;


-- ────────────────────────────────────────────────────────────────
-- 예시 5. 팀 조직도 — 팀·인물 구조 파악
--         "팀별 구성원과 담당 게임/업무는?"
-- ────────────────────────────────────────────────────────────────
-- 팀 목록 + 소속 인물
MATCH (p:Person)-[r:REL]->(t:Team)
WHERE r.rel_name IN ['BELONGS_TO', 'PART_OF', 'REPORTS_TO']
RETURN
    t.name       AS team,
    r.rel_name   AS relation,
    p.name       AS person
ORDER BY team, person;

-- 팀이 담당하는 게임·전략
MATCH (t:Team)-[r:REL]->(o)
WHERE r.rel_name IN ['MANAGES', 'TARGETS', 'SUPPORTS']
RETURN
    t.name        AS team,
    r.rel_name    AS relation,
    labels(o)[0]  AS target_type,
    o.name        AS target
ORDER BY team, relation;

-- 팀 → 인물 → 게임 연결 구조
MATCH (t:Team)-[r1:REL]->(p:Person)-[r2:REL]->(g:Game)
RETURN
    t.name       AS team,
    p.name       AS person,
    r2.rel_name  AS person_game_rel,
    g.name       AS game
LIMIT 30;


-- ────────────────────────────────────────────────────────────────
-- 예시 6 (보너스). 두 엔티티 간 최단 경로
--         "이지인과 POTC는 어떻게 연결되는가?"
-- ────────────────────────────────────────────────────────────────
MATCH p = shortestPath(
    (a {name: '이지인'})-[*1..5]-(b {name: 'POTC'})
)
RETURN
    [n IN nodes(p) | n.name]              AS path_nodes,
    [r IN relationships(p) | r.rel_name]  AS path_relations,
    length(p)                             AS hops;


-- ────────────────────────────────────────────────────────────────
-- 전체 그래프 조회 — node-edge 관계 시각화용
-- ────────────────────────────────────────────────────────────────

-- ── A. 전체 엣지 목록 (기본 시각화용) ─────────────────────────
-- from_node, relation, to_node 3열 구조 → CSV 내보내기 가능
MATCH (n)-[r:REL]->(m)
RETURN
    id(n)         AS from_id,
    n.name        AS from_name,
    labels(n)[0]  AS from_type,
    r.rel_name    AS relation,
    id(m)         AS to_id,
    m.name        AS to_name,
    labels(m)[0]  AS to_type,
    r.source_url  AS source_url;

-- HAD_EVENT 포함 전체 엣지 (Game → Event 관계 추가)
MATCH (n)-[r]->(m)
RETURN
    id(n)           AS from_id,
    n.name          AS from_name,
    labels(n)[0]    AS from_type,
    CASE type(r)
        WHEN 'REL'       THEN r.rel_name
        WHEN 'HAD_EVENT' THEN 'HAD_EVENT'
        ELSE type(r)
    END             AS relation,
    id(m)           AS to_id,
    m.name          AS to_name,
    labels(m)[0]    AS to_type
ORDER BY from_type, from_name;


-- ── B. 노드 목록만 (시각화 레이아웃용) ────────────────────────
MATCH (n)
RETURN
    id(n)         AS node_id,
    n.name        AS name,
    labels(n)[0]  AS type,
    n.source_url  AS source_url
ORDER BY type, name;


-- ── C. 라벨별 색상 매핑 제안 ──────────────────────────────────
-- (시각화 툴에서 색상 지정 시 참고)
-- Game     → #3B82F6 (파랑)
-- Team     → #10B981 (초록)
-- Person   → #F59E0B (주황)
-- Event    → #EF4444 (빨강)
-- Metric   → #8B5CF6 (보라)
-- Strategy → #06B6D4 (시안)
-- Issue    → #F97316 (오렌지)
-- Insight  → #EC4899 (핑크)
-- Decision → #6B7280 (회색)


-- ── D. Python으로 전체 그래프 내보내기 ───────────────────────
-- 아래는 Python에서 실행하는 코드 (주석)
--
-- import falkordb, json
-- db = falkordb.FalkorDB(host='localhost', port=6379)
-- g  = db.select_graph('strategic_kg')
--
-- # 노드
-- nodes = g.query("MATCH (n) RETURN id(n), n.name, labels(n)[0], n.source_url")
-- # 엣지
-- edges = g.query("""
--     MATCH (n)-[r]->(m)
--     RETURN id(n), id(m),
--            CASE type(r) WHEN 'HAD_EVENT' THEN 'HAD_EVENT' ELSE r.rel_name END,
--            r.source_url
-- """)
-- graph_data = {
--     "nodes": [{"id": r[0], "name": r[1], "type": r[2], "url": r[3]}
--               for r in nodes.result_set],
--     "edges": [{"from": r[0], "to": r[1], "rel": r[2], "url": r[3]}
--               for r in edges.result_set],
-- }
-- print(json.dumps(graph_data, ensure_ascii=False, indent=2))
