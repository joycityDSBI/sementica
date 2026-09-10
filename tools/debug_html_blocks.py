#!/usr/bin/env python3
"""
Notion 페이지의 블록 구조 진단 스크립트
────────────────────────────────────────────────────────────────────
HTML 파일이 있는 페이지의 블록 유형·URL·Content-Type을 출력합니다.
_extract_attached_html()가 왜 HTML을 추출하지 못하는지 파악하는 데 사용.

사용법:
  python tools/debug_html_blocks.py --page-id 3c7ea67a568180b4b288fab957019624
  python tools/debug_html_blocks.py --page-id 3c7ea67a... --dept strategic
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_env_path = ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

try:
    import httpx
except ImportError:
    print("❌ httpx가 없습니다: pip install httpx")
    sys.exit(1)

NOTION_VERSION = "2022-06-28"
_SKIP_TYPES = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "divider",
    "column_list",
    "column",
    "bookmark",
}


def get_blocks(token: str, block_id: str) -> list:
    """Notion 블록 목록을 페이지네이션하며 가져옵니다."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
    }
    results = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = httpx.get(
            f"https://api.notion.com/v1/blocks/{block_id}/children",
            headers=headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def inspect_blocks(token: str, block_id: str, depth: int = 0) -> None:
    """블록을 재귀적으로 순회하며 중요 정보를 출력합니다."""
    indent = "  " * depth
    try:
        blocks = get_blocks(token, block_id)
    except Exception as e:
        print(f"{indent}⚠️  블록 조회 실패: {e}")
        return

    for block in blocks:
        btype = block.get("type", "unknown")
        bid = block.get("id", "")

        if btype in _SKIP_TYPES and depth > 0:
            continue  # 텍스트 블록은 재귀만 처리

        content = block.get(btype, {})

        # ── 중요 블록 유형 출력 ───────────────────────────────────────────
        if btype == "file":
            name = content.get("name", "(이름 없음)")
            file_type = content.get("type", "")  # "file" | "external"
            inner = content.get("file") or content.get("external") or {}
            url = inner.get("url", "")
            expiry = inner.get("expiry_time", "")
            print(f"{indent}📄 [file 블록]")
            print(f"{indent}   name        : {name}")
            print(f"{indent}   file.type   : {file_type}")
            print(f"{indent}   url(앞80자) : {url[:80]}")
            if expiry:
                print(f"{indent}   expiry_time : {expiry}")

            # Content-Type 확인 (GET으로 — S3 서명 URL은 HEAD 불가)
            if url:
                try:
                    r = httpx.get(url, follow_redirects=True, timeout=15)
                    ctype = r.headers.get("content-type", "알 수 없음")
                    text_start = r.text[:200].strip().lower()
                    is_html = "text/html" in ctype or text_start.startswith(
                        ("<!doctype html", "<html")
                    )
                    print(f"{indent}   Content-Type: {ctype}")
                    print(f"{indent}   HTML 여부   : {'✅ HTML' if is_html else '❌ HTML 아님'}")
                    if is_html:
                        print(f"{indent}   내용 앞부분 : {r.text[:80].strip()!r}")
                except Exception as e:
                    print(f"{indent}   Content-Type: 조회 실패 ({e})")

        elif btype == "embed":
            url = content.get("url", "")
            print(f"{indent}🔗 [embed 블록]")
            print(f"{indent}   url         : {url[:100]}")
            if url:
                try:
                    # S3 URL은 HEAD 대신 GET으로 실제 내용 확인
                    r = httpx.get(url, follow_redirects=True, timeout=15)
                    ctype = r.headers.get("content-type", "알 수 없음")
                    text_start = r.text[:200].strip().lower()
                    is_html = "text/html" in ctype or text_start.startswith(
                        ("<!doctype html", "<html")
                    )
                    print(f"{indent}   Content-Type: {ctype}")
                    print(f"{indent}   HTML 여부   : {'✅ HTML' if is_html else '❌ HTML 아님'}")
                    if is_html:
                        print(f"{indent}   내용 앞부분 : {r.text[:80].strip()!r}")
                except Exception as e:
                    print(f"{indent}   Content-Type: 조회 실패 ({e})")

        elif btype == "pdf":
            inner = content.get("file") or content.get("external") or {}
            url = inner.get("url", "")
            print(f"{indent}📑 [pdf 블록]  url={url[:80]}")

        elif btype == "image":
            inner = content.get("file") or content.get("external") or {}
            url = inner.get("url", "")
            print(f"{indent}🖼️  [image 블록] url={url[:80]}")

        elif btype == "link_preview":
            url = content.get("url", "")
            print(f"{indent}🔍 [link_preview 블록] url={url[:100]}")

        elif btype not in _SKIP_TYPES:
            # 그 외 텍스트 외 블록 — 유형만 표시
            print(f"{indent}📦 [{btype} 블록]  id={bid[:8]}")

        # ── 자식 블록 재귀 ────────────────────────────────────────────────
        if block.get("has_children"):
            inspect_blocks(token, bid, depth + 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Notion 페이지 HTML 블록 진단")
    parser.add_argument(
        "--page-id", required=True, help="Notion 페이지 ID (대시 포함/미포함 모두 가능)"
    )
    parser.add_argument("--dept", default="strategic", help="본부 키 (토큰 환경변수 결정에 사용)")
    args = parser.parse_args()

    # 본부 토큰 환경변수 결정
    try:
        import yaml

        cfg_path = ROOT / "config" / "departments.yaml"
        with cfg_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        token_env = cfg["departments"][args.dept].get("notion_token_env", "NOTION_TOKEN")
    except Exception:
        token_env = "NOTION_TOKEN"

    token = os.environ.get(token_env, "")
    if not token:
        print(f"❌ 환경변수 {token_env}가 없습니다.")
        sys.exit(1)

    # 페이지 ID 정규화 (대시 제거 후 표준 UUID 형식)
    raw_id = args.page_id.replace("-", "")
    if len(raw_id) != 32:
        print(f"❌ 페이지 ID가 올바르지 않습니다: {args.page_id}")
        sys.exit(1)
    page_id = f"{raw_id[:8]}-{raw_id[8:12]}-{raw_id[12:16]}-{raw_id[16:20]}-{raw_id[20:]}"

    print(f"\n🔍 페이지 블록 분석: {page_id}")
    print("=" * 60)
    inspect_blocks(token, page_id)
    print("=" * 60)
    print("분석 완료\n")
    print("💡 확인 사항:")
    print("  - [file 블록] name이 .html/.htm 으로 끝나는가?")
    print("  - [file 블록] Content-Type이 text/html 또는 application/octet-stream 인가?")
    print("  - [embed 블록] url이 .html 로 끝나지 않는데 Content-Type이 text/html 인가?")
    print("  - 예상과 다른 블록 유형([pdf], [link_preview] 등)이 있는가?")


if __name__ == "__main__":
    main()
