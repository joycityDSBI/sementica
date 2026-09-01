#!/usr/bin/env python3
"""
MCP 서버 도구 검증 스크립트
사용법: python scripts/test_mcp.py [--url http://localhost:8765]
"""
import argparse
import json
import sys
import urllib.request
import urllib.error

def call_mcp(url: str, session_id: str, method: str, params: dict, req_id: int) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params,
    }).encode()
    req = urllib.request.Request(
        f"{url}/mcp",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": session_id,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    # SSE 형식("data: {...}") 또는 JSON 직접 응답 모두 처리
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line and line.startswith("{"):
            return json.loads(line)
    return json.loads(raw)


def initialize(url: str) -> str:
    """세션 초기화 → session_id 반환"""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-test", "version": "0.1.0"},
        },
    }).encode()
    req = urllib.request.Request(
        f"{url}/mcp",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        session_id = resp.headers.get("mcp-session-id", "")
        raw = resp.read().decode()

    if not session_id:
        # 일부 구현은 응답 body에 session_id 포함
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line and line.startswith("{"):
                data = json.loads(line)
                session_id = (data.get("result", {}) or {}).get("sessionId", "")
                break

    return session_id


def fmt(obj, indent=2) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent)


def extract_result(resp: dict):
    if "error" in resp:
        return None, resp["error"]
    result = resp.get("result", {})
    # MCP tools/call 응답: result.content[].text
    content = result.get("content", [])
    if content:
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if texts:
            try:
                parsed = json.loads(texts[0])
                # FastMCP x-fastmcp-wrap-result: {"result": [...]} 형태로 감싸는 경우
                if isinstance(parsed, dict) and list(parsed.keys()) == ["result"]:
                    return parsed["result"], None
                return parsed, None
            except Exception:
                return texts[0], None
    return result, None


def to_list(result) -> list:
    """결과를 항상 리스트로 정규화"""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("result", "results", "items", "data"):
            if key in result and isinstance(result[key], list):
                return result[key]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8765")
    args = ap.parse_args()

    url = args.url.rstrip("/")
    print(f"▶ MCP 서버: {url}")

    # ── 1) 초기화 ──────────────────────────────────────────────────────────────
    print("\n[0] 세션 초기화...")
    try:
        sid = initialize(url)
    except Exception as e:
        print(f"  ❌ 초기화 실패: {e}")
        sys.exit(1)
    print(f"  ✅ session_id: {sid or '(헤더 없음 — 계속 진행)'}")

    # ── 2) tools/list ──────────────────────────────────────────────────────────
    print("\n[1] tools/list...")
    resp = call_mcp(url, sid, "tools/list", {}, 1)
    if "error" in resp:
        print(f"  ❌ {resp['error']}")
        sys.exit(1)
    tools = resp.get("result", {}).get("tools", [])
    print(f"  등록된 도구 ({len(tools)}개):")
    for t in tools:
        print(f"    • {t['name']} — {t.get('description','')[:60]}")

    tool_names = {t["name"] for t in tools}

    # ── 3) semantic_search ────────────────────────────────────────────────────
    print("\n[2] semantic_search — '운영팀 담당 업무'...")
    if "semantic_search" in tool_names:
        resp = call_mcp(url, sid, "tools/call",
                        {"name": "semantic_search",
                         "arguments": {"query": "운영팀 담당 업무", "limit": 3}}, 2)
        result, err = extract_result(resp)
        if err:
            print(f"  ❌ {err}")
        else:
            hits = to_list(result)
            print(f"  ✅ 결과 {len(hits)}건")
            for h in hits[:2]:
                if isinstance(h, dict):
                    title = h.get("title") or h.get("page_title", "")
                    score = h.get("score", "")
                    print(f"    • [{score:.3f}] {title}" if isinstance(score, float) else f"    • {title}")
    else:
        print("  ⚠️  도구 없음")

    # ── 4) graph_search ───────────────────────────────────────────────────────
    print("\n[3] graph_search — entity='운영팀'...")
    if "graph_search" in tool_names:
        resp = call_mcp(url, sid, "tools/call",
                        {"name": "graph_search",
                         "arguments": {"entity": "운영팀", "depth": 1}}, 3)
        result, err = extract_result(resp)
        if err:
            print(f"  ❌ {err}")
        else:
            r = result if isinstance(result, dict) else {}
            outgoing = r.get("outgoing", [])
            incoming = r.get("incoming", [])
            print(f"  ✅ 발견: {r.get('found', False)}, 엔티티: {r.get('entity','')}")
            print(f"     outgoing {len(outgoing)}개, incoming {len(incoming)}개")
            for rel in outgoing[:3]:
                if isinstance(rel, dict):
                    print(f"    → [{rel.get('relation','')}] {rel.get('target_name','')}")
    else:
        print("  ⚠️  도구 없음")

    # ── 5) hybrid_search ──────────────────────────────────────────────────────
    print("\n[4] hybrid_search — '점검 프로세스 담당자'...")
    if "hybrid_search" in tool_names:
        resp = call_mcp(url, sid, "tools/call",
                        {"name": "hybrid_search",
                         "arguments": {"query": "점검 프로세스 담당자", "limit": 3}}, 4)
        result, err = extract_result(resp)
        if err:
            print(f"  ❌ {err}")
        else:
            r = result if isinstance(result, dict) else {}
            sem = r.get("semantic_results", [])
            gph = r.get("graph_results", [])
            summary = r.get("entity_summary", [])
            print(f"  ✅ 벡터 {len(sem)}건, 그래프 {len(gph)}건")
            for h in sem[:2]:
                if isinstance(h, dict):
                    print(f"    • {h.get('title','')}")
            for s in summary[:3]:
                print(f"    ↔ {s}")
    else:
        print("  ⚠️  도구 없음")

    # ── 6) path_search ────────────────────────────────────────────────────────
    print("\n[5] path_search — '운영팀' → '점검'...")
    if "path_search" in tool_names:
        resp = call_mcp(url, sid, "tools/call",
                        {"name": "path_search",
                         "arguments": {"start_entity": "운영팀", "end_entity": "점검",
                                       "max_hops": 5}}, 5)
        result, err = extract_result(resp)
        if err:
            print(f"  ❌ {err}")
        else:
            r = result if isinstance(result, dict) else {}
            found = r.get("found", False)
            path_nodes = r.get("path_nodes", [])
            hops = r.get("hops", len(path_nodes))
            print(f"  ✅ 경로 {'발견' if found else '없음'} ({hops} 홉)")
            if path_nodes:
                print(f"    {' → '.join(str(p) for p in path_nodes)}")
            elif not found:
                print(f"    (연결 경로 없음 — 엔티티명을 실제 데이터에 맞게 조정 필요)")
    else:
        print("  ⚠️  도구 없음")

    # ── 7) decision_trace ────────────────────────────────────────────────────
    print("\n[6] decision_trace — entity='운영팀'...")
    if "decision_trace" in tool_names:
        resp = call_mcp(url, sid, "tools/call",
                        {"name": "decision_trace",
                         "arguments": {"entity": "운영팀", "max_depth": 4}}, 6)
        result, err = extract_result(resp)
        if err:
            print(f"  ❌ {err}")
        else:
            r = result if isinstance(result, dict) else {}
            decisions = r.get("decisions", [])
            chain = r.get("chain_summary", [])
            print(f"  ✅ Decision 노드 {r.get('found', len(decisions))}개")
            for d in decisions[:3]:
                if isinstance(d, dict):
                    print(f"    • [{d.get('action','')}] {d.get('subject','')} → {d.get('outcome','')}")
            for s in chain[:3]:
                print(f"    ↻ {s}")
            if not decisions:
                print("    (Decision 노드 없음 — sync.py --full 재실행 후 확인 필요)")
    else:
        print("  ⚠️  도구 없음")

    print("\n━━━ 검증 완료 ━━━")


if __name__ == "__main__":
    main()
