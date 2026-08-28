"""
Notion 페이지 수집기 — Week 1 샘플 수집용
Notion API Integration Token이 필요합니다.

사용법:
  python notion_fetch.py --token <NOTION_TOKEN> --page-id <PAGE_ID>
  python notion_fetch.py --token <NOTION_TOKEN> --search "온보딩"
"""

import os
import json
import time
import argparse
import re
from pathlib import Path

# .env 로드
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

try:
    import httpx
except ImportError:
    raise SystemExit("httpx가 필요합니다: pip install httpx")

NOTION_VERSION = "2022-06-28"
RATE_LIMIT_DELAY = 0.34  # 3 req/s 준수 (1/3 = 0.333s)
OUTPUT_DIR = Path(__file__).parent.parent.parent / "data" / "notion_samples"

def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def fetch_page(client: httpx.Client, token: str, page_id: str) -> dict:
    """단일 페이지 메타데이터 조회"""
    resp = client.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=notion_headers(token),
    )
    resp.raise_for_status()
    time.sleep(RATE_LIMIT_DELAY)
    return resp.json()

def fetch_blocks(client: httpx.Client, token: str, block_id: str) -> list:
    """페이지 블록(본문) 재귀 조회"""
    blocks = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = client.get(
            f"https://api.notion.com/v1/blocks/{block_id}/children",
            headers=notion_headers(token),
            params=params,
        )
        resp.raise_for_status()
        time.sleep(RATE_LIMIT_DELAY)
        data = resp.json()
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return blocks

def blocks_to_text(blocks: list, depth: int = 0) -> str:
    """블록 리스트를 마크다운 텍스트로 변환"""
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich_text = content.get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rich_text)
        indent = "  " * depth

        if btype == "paragraph":
            lines.append(f"{indent}{text}")
        elif btype.startswith("heading_"):
            level = int(btype[-1])
            lines.append(f"{'#' * level} {text}")
        elif btype == "bulleted_list_item":
            lines.append(f"{indent}- {text}")
        elif btype == "numbered_list_item":
            lines.append(f"{indent}1. {text}")
        elif btype == "to_do":
            checked = content.get("checked", False)
            lines.append(f"{indent}- [{'x' if checked else ' '}] {text}")
        elif btype == "toggle":
            lines.append(f"{indent}▸ {text}")
        elif btype == "callout":
            lines.append(f"{indent}> {text}")
        elif btype == "quote":
            lines.append(f"{indent}> {text}")
        elif btype == "divider":
            lines.append("---")
        elif text:
            lines.append(f"{indent}{text}")

    return "\n".join(lines)


def fetch_blocks_recursive(client: httpx.Client, token: str, block_id: str, depth: int = 0, max_depth: int = 3) -> str:
    """블록을 재귀적으로 가져와 텍스트로 변환 (최대 3단계)"""
    if depth > max_depth:
        return ""
    blocks = fetch_blocks(client, token, block_id)
    text = blocks_to_text(blocks, depth)
    # 자식 블록이 있는 블록들을 재귀 조회
    child_texts = []
    for block in blocks:
        if block.get("has_children"):
            child_text = fetch_blocks_recursive(client, token, block["id"], depth + 1, max_depth)
            if child_text.strip():
                child_texts.append(child_text)
    if child_texts:
        text = text + "\n" + "\n".join(child_texts)
    return text

def page_title(page: dict) -> str:
    """페이지 제목 추출"""
    props = page.get("properties", {})
    for key in ("title", "Name", "이름"):
        if key in props:
            rt = props[key].get("title", [])
            return "".join(t.get("plain_text", "") for t in rt)
    return page.get("id", "untitled")

def search_pages(client: httpx.Client, token: str, query: str) -> list:
    """Notion 워크스페이스 검색"""
    resp = client.post(
        "https://api.notion.com/v1/search",
        headers=notion_headers(token),
        json={"query": query, "filter": {"value": "page", "property": "object"}, "page_size": 20},
    )
    resp.raise_for_status()
    time.sleep(RATE_LIMIT_DELAY)
    return resp.json().get("results", [])

def safe_filename(title: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", title)[:80]

def save_page(client: httpx.Client, token: str, page: dict, idx: int) -> dict:
    """한 페이지 수집 후 저장. 검증 결과 반환."""
    page_id = page["id"].replace("-", "")
    title = page_title(page)
    url = page.get("url", "")

    print(f"  [{idx}] {title}")
    print(f"       URL: {url}")

    try:
        text = fetch_blocks_recursive(client, token, page_id)
        word_count = len(text.split())

        fname = f"{idx:02d}_{safe_filename(title)}.md"
        out_path = OUTPUT_DIR / fname
        out_path.write_text(
            f"---\ntitle: {title}\nnotion_url: {url}\npage_id: {page_id}\n---\n\n{text}",
            encoding="utf-8",
        )

        has_meaningful_text = word_count >= 50
        print(f"       저장: {fname} ({word_count} 단어) {'✓' if has_meaningful_text else '✗ 텍스트 부족'}")

        return {
            "idx": idx,
            "title": title,
            "url": url,
            "page_id": page_id,
            "word_count": word_count,
            "file": str(out_path),
            "meaningful": has_meaningful_text,
        }
    except Exception as e:
        print(f"       오류: {e}")
        return {"idx": idx, "title": title, "url": url, "meaningful": False, "error": str(e)}

def main():
    parser = argparse.ArgumentParser(description="Notion 페이지 샘플 수집기")
    parser.add_argument("--token", default=os.environ.get("NOTION_TOKEN", ""), help="Notion Integration Token (.env의 NOTION_TOKEN으로 대체 가능)")
    parser.add_argument("--search", default="온보딩", help="검색 키워드 (기본: 온보딩)")
    parser.add_argument("--page-id", help="특정 페이지 ID 직접 지정")
    args = parser.parse_args()

    if not args.token:
        parser.error("NOTION_TOKEN 환경변수 또는 --token 인수가 필요합니다.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    with httpx.Client(timeout=30) as client:
        if args.page_id:
            page = fetch_page(client, args.token, args.page_id)
            results.append(save_page(client, args.token, page, 1))
        else:
            print(f"🔍 Notion 검색: '{args.search}'")
            pages = search_pages(client, args.token, args.search)
            print(f"   {len(pages)}개 페이지 발견\n")
            for i, page in enumerate(pages[:10], 1):
                results.append(save_page(client, args.token, page, i))
                print()

    # 합격 기준 평가
    meaningful = [r for r in results if r.get("meaningful")]
    print("\n" + "="*50)
    print(f"📊 Premise 1 합격 기준: {len(meaningful)}/10 페이지 텍스트 충분")
    print(f"   {'✅ 합격' if len(meaningful) >= 6 else '❌ 불합격 — 구조화된 DB 페이지 우선 수집으로 전환'}")

    # 결과 JSON 저장
    summary_path = OUTPUT_DIR / "collection_summary.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {summary_path}")

if __name__ == "__main__":
    main()
