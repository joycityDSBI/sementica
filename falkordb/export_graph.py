"""
FalkorDB 전체 그래프 내보내기 + HTML 시각화

실행:
  python falkordb/export_graph.py --dept strategic
  python falkordb/export_graph.py --dept strategic --output graph.json  # JSON만
  python falkordb/export_graph.py --dept strategic --html               # HTML 시각화 생성

결과:
  data/strategic/graph_export.json  — node-edge JSON
  data/strategic/graph_view.html    — 인터랙티브 시각화 (브라우저에서 열기)
"""
import argparse
import json
import os
import sys
from pathlib import Path

_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "pipeline"))

FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))

# 라벨별 색상
NODE_COLORS = {
    "Game":     "#3B82F6",
    "Team":     "#10B981",
    "Person":   "#F59E0B",
    "Event":    "#EF4444",
    "Metric":   "#8B5CF6",
    "Strategy": "#06B6D4",
    "Issue":    "#F97316",
    "Insight":  "#EC4899",
    "Decision": "#6B7280",
}
DEFAULT_COLOR = "#94A3B8"


def export_graph(graph_name: str) -> dict:
    import falkordb
    db = falkordb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
    g  = db.select_graph(graph_name)

    # ── 노드 ──────────────────────────────────────────────────────
    nr = g.query(
        "MATCH (n) RETURN id(n) AS nid, n.name AS name, "
        "labels(n)[0] AS lbl, n.source_url AS url"
    )
    nodes = []
    for row in nr.result_set:
        nid, name, lbl, url = row
        nodes.append({
            "id":    nid,
            "name":  name or f"node_{nid}",
            "type":  lbl or "Unknown",
            "url":   url or "",
            "color": NODE_COLORS.get(lbl, DEFAULT_COLOR),
        })

    # ── 엣지 ──────────────────────────────────────────────────────
    er = g.query("""
        MATCH (n)-[r]->(m)
        RETURN
            id(n) AS from_id,
            id(m) AS to_id,
            CASE type(r)
                WHEN 'HAD_EVENT' THEN 'HAD_EVENT'
                ELSE coalesce(r.rel_name, type(r))
            END AS rel,
            coalesce(r.source_url, '') AS url
    """)
    edges = []
    for row in er.result_set:
        from_id, to_id, rel, url = row
        edges.append({
            "from": from_id,
            "to":   to_id,
            "rel":  rel or "REL",
            "url":  url or "",
        })

    # ── 통계 ──────────────────────────────────────────────────────
    type_counts = {}
    for n in nodes:
        type_counts[n["type"]] = type_counts.get(n["type"], 0) + 1
    rel_counts = {}
    for e in edges:
        rel_counts[e["rel"]] = rel_counts.get(e["rel"], 0) + 1

    return {
        "graph":      graph_name,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "type_counts": type_counts,
        "rel_counts":  rel_counts,
        "nodes": nodes,
        "edges": edges,
    }


def build_html(data: dict) -> str:
    nodes_js = json.dumps(data["nodes"], ensure_ascii=False)
    edges_js = json.dumps(data["edges"], ensure_ascii=False)
    stats_js = json.dumps({
        "node_count":  data["node_count"],
        "edge_count":  data["edge_count"],
        "type_counts": data["type_counts"],
        "rel_counts":  data["rel_counts"],
    }, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>Semantica Graph — {data['graph']}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; height: 100vh; display: flex; flex-direction: column; }}
  header {{ padding: 10px 16px; background: #1e293b; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
  header h1 {{ font-size: 15px; font-weight: 600; color: #f1f5f9; }}
  .badge {{ background: #334155; border-radius: 4px; padding: 3px 8px; font-size: 11px; color: #94a3b8; }}
  .controls {{ display: flex; gap: 8px; flex-wrap: wrap; margin-left: auto; align-items: center; }}
  input[type=text] {{ background: #1e293b; border: 1px solid #475569; border-radius: 4px; color: #e2e8f0; padding: 5px 10px; font-size: 12px; width: 200px; }}
  select {{ background: #1e293b; border: 1px solid #475569; border-radius: 4px; color: #e2e8f0; padding: 5px 8px; font-size: 12px; }}
  button {{ background: #3b82f6; border: none; border-radius: 4px; color: #fff; padding: 5px 12px; font-size: 12px; cursor: pointer; }}
  button:hover {{ background: #2563eb; }}
  .layout {{ display: flex; flex: 1; overflow: hidden; }}
  #canvas-wrap {{ flex: 1; position: relative; overflow: hidden; }}
  canvas {{ position: absolute; inset: 0; }}
  #sidebar {{ width: 260px; background: #1e293b; border-left: 1px solid #334155; overflow-y: auto; padding: 12px; font-size: 12px; flex-shrink: 0; }}
  #sidebar h2 {{ font-size: 12px; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 8px; }}
  .stat-row {{ display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #1e293b; }}
  .stat-val {{ color: #3b82f6; font-weight: 600; }}
  .legend-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; }}
  #node-detail {{ margin-top: 16px; background: #0f172a; border-radius: 6px; padding: 10px; display: none; }}
  #node-detail h3 {{ font-size: 13px; font-weight: 600; margin-bottom: 6px; }}
  #node-detail a {{ color: #60a5fa; word-break: break-all; font-size: 11px; }}
  .rel-item {{ padding: 3px 0; border-bottom: 1px solid #1e293b; color: #cbd5e1; }}
  .rel-item span {{ color: #60a5fa; font-weight: 600; }}
</style>
</head>
<body>
<header>
  <h1>🕸️ Semantica Graph</h1>
  <span class="badge" id="h-graph">{data['graph']}</span>
  <span class="badge" id="h-nodes">노드 {data['node_count']}</span>
  <span class="badge" id="h-edges">엣지 {data['edge_count']}</span>
  <div class="controls">
    <input type="text" id="search-box" placeholder="노드 이름 검색..." oninput="searchNode()">
    <select id="filter-type" onchange="applyFilter()">
      <option value="">전체 유형</option>
    </select>
    <button onclick="resetView()">↺ 초기화</button>
  </div>
</header>
<div class="layout">
  <div id="canvas-wrap">
    <canvas id="c"></canvas>
  </div>
  <div id="sidebar">
    <h2>노드 유형</h2>
    <div id="legend"></div>
    <h2 style="margin-top:14px">관계 통계</h2>
    <div id="rel-stats"></div>
    <div id="node-detail">
      <h3 id="d-name">—</h3>
      <div style="color:#94a3b8;margin-bottom:6px"><span id="d-type"></span></div>
      <a id="d-url" href="#" target="_blank"></a>
      <div id="d-rels" style="margin-top:8px"></div>
    </div>
  </div>
</div>
<script>
const RAW_NODES = {nodes_js};
const RAW_EDGES = {edges_js};
const STATS     = {stats_js};

// ── 캔버스 세팅 ──────────────────────────────────────────────────
const wrap   = document.getElementById('canvas-wrap');
const canvas = document.getElementById('c');
const ctx    = canvas.getContext('2d');

let W, H;
function resize() {{
  W = canvas.width  = wrap.clientWidth;
  H = canvas.height = wrap.clientHeight;
}}
resize();
window.addEventListener('resize', () => {{ resize(); draw(); }});

// ── 노드 맵 ─────────────────────────────────────────────────────
const nodeMap = {{}};
RAW_NODES.forEach(n => nodeMap[n.id] = n);

// ── Force-directed layout (간단한 구현) ──────────────────────────
const REPULSE  = 8000;
const ATTRACT  = 0.006;
const DAMPING  = 0.85;
const LINK_LEN = 120;

RAW_NODES.forEach(n => {{
  n.x  = W / 2 + (Math.random() - 0.5) * W * 0.6;
  n.y  = H / 2 + (Math.random() - 0.5) * H * 0.6;
  n.vx = 0; n.vy = 0;
  n.r  = n.type === 'Game' ? 14 : n.type === 'Team' ? 12 : 9;
}});

let simRunning = true;
let simStep = 0;
function simulate() {{
  if (!simRunning) return;
  // 척력
  for (let i = 0; i < RAW_NODES.length; i++) {{
    const a = RAW_NODES[i];
    for (let j = i + 1; j < RAW_NODES.length; j++) {{
      const b   = RAW_NODES[j];
      const dx  = b.x - a.x || 0.1;
      const dy  = b.y - a.y || 0.1;
      const d2  = dx*dx + dy*dy || 1;
      const f   = REPULSE / d2;
      a.vx -= f * dx / Math.sqrt(d2);
      a.vy -= f * dy / Math.sqrt(d2);
      b.vx += f * dx / Math.sqrt(d2);
      b.vy += f * dy / Math.sqrt(d2);
    }}
  }}
  // 인력 (엣지)
  RAW_EDGES.forEach(e => {{
    const a = nodeMap[e.from]; const b = nodeMap[e.to];
    if (!a || !b) return;
    const dx = b.x - a.x; const dy = b.y - a.y;
    const d  = Math.sqrt(dx*dx + dy*dy) || 1;
    const f  = (d - LINK_LEN) * ATTRACT;
    a.vx += f * dx / d; a.vy += f * dy / d;
    b.vx -= f * dx / d; b.vy -= f * dy / d;
  }});
  // 중심 인력
  RAW_NODES.forEach(n => {{
    n.vx += (W/2 - n.x) * 0.0005;
    n.vy += (H/2 - n.y) * 0.0005;
    n.vx *= DAMPING; n.vy *= DAMPING;
    n.x  += n.vx;    n.y  += n.vy;
    n.x   = Math.max(n.r, Math.min(W - n.r, n.x));
    n.y   = Math.max(n.r, Math.min(H - n.r, n.y));
  }});
  simStep++;
  if (simStep > 300) simRunning = false;
}}

// ── 줌/팬 ────────────────────────────────────────────────────────
let scale = 1, tx = 0, ty = 0;
let dragging = false, lastMX = 0, lastMY = 0;
canvas.addEventListener('wheel', e => {{
  e.preventDefault();
  const z = e.deltaY < 0 ? 1.1 : 0.9;
  scale *= z; draw();
}}, {{ passive: false }});
canvas.addEventListener('mousedown', e => {{ dragging = true; lastMX = e.clientX; lastMY = e.clientY; }});
canvas.addEventListener('mousemove', e => {{
  if (dragging) {{
    tx += e.clientX - lastMX; ty += e.clientY - lastMY;
    lastMX = e.clientX; lastMY = e.clientY; draw();
  }}
  handleHover(e);
}});
canvas.addEventListener('mouseup',   () => dragging = false);
canvas.addEventListener('mouseleave',() => dragging = false);
canvas.addEventListener('click', handleClick);

function resetView() {{ scale = 1; tx = 0; ty = 0; draw(); }}

function toCanvas(x, y) {{
  return [x * scale + tx, y * scale + ty];
}}
function fromCanvas(cx, cy) {{
  return [(cx - tx) / scale, (cy - ty) / scale];
}}

// ── 필터 상태 ────────────────────────────────────────────────────
let filterType    = '';
let searchTerm    = '';
let selectedNode  = null;
let hoveredNode   = null;

function visibleNodes() {{
  return RAW_NODES.filter(n =>
    (!filterType || n.type === filterType) &&
    (!searchTerm || n.name.includes(searchTerm))
  );
}}
const visSet = () => new Set(visibleNodes().map(n => n.id));

// ── 그리기 ───────────────────────────────────────────────────────
function draw() {{
  ctx.clearRect(0, 0, W, H);
  ctx.save();
  ctx.translate(tx, ty);
  ctx.scale(scale, scale);

  const vis = visSet();

  // 엣지
  RAW_EDGES.forEach(e => {{
    const a = nodeMap[e.from]; const b = nodeMap[e.to];
    if (!a || !b || !vis.has(a.id) || !vis.has(b.id)) return;
    const highlight = selectedNode && (e.from === selectedNode.id || e.to === selectedNode.id);
    ctx.strokeStyle = highlight ? '#60a5fa' : 'rgba(148,163,184,0.2)';
    ctx.lineWidth   = highlight ? 1.5 : 0.8;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();

    // 관계명 (하이라이트 시만)
    if (highlight) {{
      ctx.fillStyle  = '#94a3b8';
      ctx.font       = '9px sans-serif';
      ctx.textAlign  = 'center';
      ctx.fillText(e.rel, (a.x+b.x)/2, (a.y+b.y)/2 - 3);
    }}
  }});

  // 노드
  visibleNodes().forEach(n => {{
    const isSelected = selectedNode && n.id === selectedNode.id;
    const isHovered  = hoveredNode  && n.id === hoveredNode.id;
    // 그림자
    if (isSelected || isHovered) {{
      ctx.shadowColor = n.color;
      ctx.shadowBlur  = 12;
    }}
    ctx.beginPath();
    ctx.arc(n.x, n.y, n.r + (isSelected ? 3 : 0), 0, Math.PI*2);
    ctx.fillStyle = n.color;
    ctx.fill();
    ctx.shadowBlur = 0;
    // 라벨
    if (scale > 0.6 || isSelected || isHovered) {{
      ctx.fillStyle  = '#f1f5f9';
      ctx.font       = `${{isSelected ? 'bold ' : ''}}${{Math.max(9, 11 * scale)}}px sans-serif`;
      ctx.textAlign  = 'center';
      ctx.fillText(n.name.length > 14 ? n.name.slice(0,13)+'…' : n.name, n.x, n.y + n.r + 11);
    }}
  }});

  ctx.restore();
  if (simRunning) {{ simulate(); requestAnimationFrame(draw); }}
}}

// ── 인터랙션 ─────────────────────────────────────────────────────
function hitTest(e) {{
  const [mx, my] = fromCanvas(e.offsetX, e.offsetY);
  const vis = visSet();
  for (const n of RAW_NODES) {{
    if (!vis.has(n.id)) continue;
    const d = Math.hypot(n.x - mx, n.y - my);
    if (d <= n.r + 4) return n;
  }}
  return null;
}}

function handleHover(e) {{
  const n = hitTest(e);
  if (n !== hoveredNode) {{ hoveredNode = n; canvas.style.cursor = n ? 'pointer' : 'default'; draw(); }}
}}

function handleClick(e) {{
  const n = hitTest(e);
  selectedNode = (n && selectedNode && n.id === selectedNode.id) ? null : n;
  showDetail(selectedNode);
  draw();
}}

function showDetail(n) {{
  const el = document.getElementById('node-detail');
  if (!n) {{ el.style.display = 'none'; return; }}
  el.style.display = 'block';
  document.getElementById('d-name').textContent = n.name;
  document.getElementById('d-type').textContent = n.type;
  const link = document.getElementById('d-url');
  if (n.url) {{ link.href = n.url; link.textContent = '🔗 Notion 원본'; }}
  else link.textContent = '';

  const rels = RAW_EDGES.filter(e => e.from === n.id || e.to === n.id);
  const relDiv = document.getElementById('d-rels');
  relDiv.innerHTML = rels.slice(0,15).map(e => {{
    const other = nodeMap[e.from === n.id ? e.to : e.from];
    const dir   = e.from === n.id ? '→' : '←';
    return `<div class="rel-item">${{dir}} <span>${{e.rel}}</span> ${{other?.name ?? '?'}}</div>`;
  }}).join('') + (rels.length > 15 ? `<div style="color:#475569;margin-top:4px">+ ${{rels.length-15}}개 더</div>` : '');
}}

// ── 사이드바 ─────────────────────────────────────────────────────
function buildSidebar() {{
  // 범례
  const leg = document.getElementById('legend');
  const sel = document.getElementById('filter-type');
  Object.entries(STATS.type_counts).sort((a,b)=>b[1]-a[1]).forEach(([t, cnt]) => {{
    const color = {json.dumps(NODE_COLORS)}[t] || '#94a3b8';
    leg.innerHTML += `<div class="stat-row">
      <span><span class="legend-dot" style="background:${{color}}"></span>${{t}}</span>
      <span class="stat-val">${{cnt}}</span></div>`;
    sel.innerHTML += `<option value="${{t}}">${{t}} (${{cnt}})</option>`;
  }});
  // 관계 통계
  const rs = document.getElementById('rel-stats');
  Object.entries(STATS.rel_counts).sort((a,b)=>b[1]-a[1]).slice(0,15).forEach(([r,cnt]) => {{
    rs.innerHTML += `<div class="stat-row"><span style="color:#cbd5e1">${{r}}</span><span class="stat-val">${{cnt}}</span></div>`;
  }});
}}

function applyFilter() {{
  filterType = document.getElementById('filter-type').value;
  selectedNode = null;
  document.getElementById('node-detail').style.display = 'none';
  draw();
}}

function searchNode() {{
  searchTerm = document.getElementById('search-box').value.trim();
  const found = RAW_NODES.find(n => n.name.includes(searchTerm));
  if (found) {{
    selectedNode = found;
    tx = W/2 - found.x * scale;
    ty = H/2 - found.y * scale;
    showDetail(found);
  }}
  draw();
}}

buildSidebar();
draw();
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="FalkorDB 그래프 내보내기 + 시각화")
    parser.add_argument("--dept",   default="strategic", help="본부 이름 (departments.yaml key)")
    parser.add_argument("--output", default="",          help="JSON 출력 경로 (기본: data/{dept}/graph_export.json)")
    parser.add_argument("--html",   action="store_true", help="HTML 시각화 파일도 생성")
    args = parser.parse_args()

    from dept_config import load_dept
    cfg        = load_dept(args.dept)
    graph_name = cfg["falkordb_graph"]
    data_dir   = Path(cfg["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"📊 그래프 내보내기 — {graph_name}")
    data = export_graph(graph_name)

    print(f"  노드: {data['node_count']}개")
    print(f"  엣지: {data['edge_count']}개")
    print(f"  유형: {data['type_counts']}")

    # JSON 저장
    json_path = Path(args.output) if args.output else data_dir / "graph_export.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ JSON 저장: {json_path}")

    # HTML 시각화
    if args.html:
        html_path = data_dir / "graph_view.html"
        html_path.write_text(build_html(data), encoding="utf-8")
        print(f"✅ HTML 저장: {html_path}")
        print(f"   → 브라우저에서 열기: file://{html_path.resolve()}")
        print(f"   → 서버 접근:         http://34.42.7.50:8080/... (정적 파일 서빙 필요)")


if __name__ == "__main__":
    main()
