"""
Notion 페이지 전체 수집기 — 멀티 본부 지원
API 권한이 있는 모든 페이지를 수집해 data/{dept}/notion_pages/ 에 저장합니다.

사용법:
  # 전략사업본부 전체 페이지 수집
  python src/pipeline/notion_fetch.py --dept strategic

  # 특정 페이지만 수집
  python src/pipeline/notion_fetch.py --dept strategic --page-id <PAGE_ID>

  # 검색어로 수집 (기존 방식)
  python src/pipeline/notion_fetch.py --dept strategic --search "점검"

  # 사용 가능한 본부 목록 확인
  python src/pipeline/notion_fetch.py --list-depts
"""

import argparse
import json
import os
import re
import sys
import time
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
    raise SystemExit("httpx가 필요합니다: pip install httpx") from None

NOTION_VERSION   = "2022-06-28"
RATE_LIMIT_DELAY = 0.34   # 3 req/s 준수


# ─── Notion API 헬퍼 ─────────────────────────────────────────────────────────
def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_page_meta(client: httpx.Client, token: str, page_id: str) -> dict:
    resp = client.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=notion_headers(token),
    )
    resp.raise_for_status()
    time.sleep(RATE_LIMIT_DELAY)
    return resp.json()


def fetch_all_pages(client: httpx.Client, token: str, limit: int = 0) -> list:
    """API 권한이 있는 모든 페이지를 페이지네이션으로 수집
    limit > 0 이면 해당 수에 도달하면 API 호출 즉시 중단
    """
    pages = []
    cursor = None
    page_num = 1
    while True:
        # 남은 수집 필요량 계산
        if limit > 0:
            remaining = limit - len(pages)
            if remaining <= 0:
                break
            page_size = min(100, remaining)
        else:
            page_size = 100

        body = {
            "filter": {"value": "page", "property": "object"},
            "page_size": page_size,
        }
        if cursor:
            body["start_cursor"] = cursor

        resp = client.post(
            "https://api.notion.com/v1/search",
            headers=notion_headers(token),
            json=body,
        )
        resp.raise_for_status()
        time.sleep(RATE_LIMIT_DELAY)
        data = resp.json()

        results = data.get("results", [])
        pages.extend(results)
        print(f"  페이지 {page_num}: {len(results)}개 수집 (누적 {len(pages)}개)"
              + (f" / 목표 {limit}개" if limit else ""))

        if not data.get("has_more"):
            break
        if limit > 0 and len(pages) >= limit:
            print(f"  ✅ 목표 {limit}개 도달 — 수집 완료")
            break
        cursor = data.get("next_cursor")
        page_num += 1

    return pages


def search_pages(client: httpx.Client, token: str, query: str) -> list:
    """검색어로 페이지 탐색"""
    resp = client.post(
        "https://api.notion.com/v1/search",
        headers=notion_headers(token),
        json={
            "query": query,
            "filter": {"value": "page", "property": "object"},
            "page_size": 50,
        },
    )
    resp.raise_for_status()
    time.sleep(RATE_LIMIT_DELAY)
    return resp.json().get("results", [])


def fetch_blocks(client: httpx.Client, token: str, block_id: str) -> list:
    """블록 목록 페이지네이션 수집"""
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
    """블록 → 마크다운 텍스트"""
    lines = []
    for block in blocks:
        btype   = block.get("type", "")
        content = block.get(btype, {})
        rich    = content.get("rich_text", [])
        text    = "".join(t.get("plain_text", "") for t in rich)
        indent  = "  " * depth

        if btype == "paragraph":
            if text:
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
        elif btype in ("callout", "quote"):
            lines.append(f"{indent}> {text}")
        elif btype == "divider":
            lines.append("---")
        elif btype == "code":
            lang = content.get("language", "")
            lines.append(f"```{lang}\n{text}\n```")
        elif btype == "table_row":
            cells = content.get("cells", [])
            cell_texts = [" ".join(t.get("plain_text", "") for t in cell) for cell in cells]
            lines.append("| " + " | ".join(cell_texts) + " |")
        elif text:
            lines.append(f"{indent}{text}")

    return "\n".join(line for line in lines if line.strip() or not lines)


def fetch_blocks_recursive(client, token, block_id, depth=0, max_depth=4) -> str:
    """블록을 재귀적으로 가져와 텍스트로 변환"""
    if depth > max_depth:
        return ""
    blocks = fetch_blocks(client, token, block_id)
    text   = blocks_to_text(blocks, depth)

    child_parts = []
    for block in blocks:
        if block.get("has_children"):
            child = fetch_blocks_recursive(client, token, block["id"], depth + 1, max_depth)
            if child.strip():
                child_parts.append(child)

    if child_parts:
        text = text + "\n" + "\n".join(child_parts)
    return text


# ── Notion DB 속성 핸들러 (type → extractor) ──────────────────────────────────
# 각 함수는 val 딕셔너리를 받아 추출된 값 또는 None(미설정) 반환.
# checkbox는 False도 의미 있는 값이므로 항상 반환.

def _prop_select(val: dict):
    sel = val.get("select")
    return sel["name"] if sel else None

def _prop_multi_select(val: dict):
    items = val.get("multi_select", [])
    return [s["name"] for s in items] or None

def _prop_date(val: dict):
    dt = val.get("date")
    return dt["start"][:10] if dt and dt.get("start") else None  # YYYY-MM-DD

def _prop_people(val: dict):
    people = val.get("people", [])
    names  = [p.get("name", "") for p in people if p.get("name")]
    return names or None

def _prop_rich_text(val: dict):
    texts = val.get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in texts).strip() or None

def _prop_number(val: dict):
    return val.get("number")  # 0도 유효한 값; None이면 미설정

def _prop_checkbox(val: dict):
    return val.get("checkbox", False)  # False도 의미 있는 값

def _prop_url(val: dict):
    return val.get("url") or None

def _prop_email(val: dict):
    return val.get("email") or None

def _prop_phone(val: dict):
    return val.get("phone_number") or None

# relation은 ID만 있어 이름이 없으므로 생략
_PROP_EXTRACTORS: dict = {
    "select":       _prop_select,
    "multi_select": _prop_multi_select,
    "date":         _prop_date,
    "people":       _prop_people,
    "rich_text":    _prop_rich_text,
    "number":       _prop_number,
    "checkbox":     _prop_checkbox,
    "url":          _prop_url,
    "email":        _prop_email,
    "phone_number": _prop_phone,
}


def extract_db_properties(page: dict) -> dict:
    """
    Notion DB 항목의 속성을 평탄한 딕셔너리로 추출합니다.
    일반 페이지(title 속성만 있는 경우)는 빈 딕셔너리를 반환합니다.

    지원 속성 유형:
      select, multi_select, date, people, rich_text,
      number, checkbox, url, email, phone_number

    Returns:
        {"게임명": "POTC", "이벤트날짜": "2026-04-12", "담당자": ["김도형"], ...}
    """
    props  = page.get("properties", {})
    result = {}

    for key, val in props.items():
        ptype = val.get("type", "")
        if ptype == "title":
            continue  # 제목은 page_title()에서 별도 처리
        extractor = _PROP_EXTRACTORS.get(ptype)
        if extractor is None:
            continue  # 미지원 유형 (relation, formula 등)
        try:
            extracted = extractor(val)
            if extracted is not None:
                result[key] = extracted
        except Exception:
            continue

    return result


def page_title(page: dict) -> str:
    props = page.get("properties", {})
    for key in ("title", "Name", "이름", "제목"):
        if key in props:
            rt = props[key].get("title", [])
            t  = "".join(t.get("plain_text", "") for t in rt).strip()
            if t:
                return t
    return page.get("id", "untitled")


def safe_filename(title: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", title)[:80]


# ─── 페이지 저장 ──────────────────────────────────────────────────────────────
def save_page(client, token, page, idx, output_dir: Path,
              min_words: int = 30) -> dict:
    """Notion 페이지를 .md 로 저장합니다.

    Args:
        min_words: 이 단어 수 미만인 페이지는 .md 파일을 저장하지 않고 건너뜁니다.
                   기본값 30. --min-words CLI 인수로 조정 가능.
    """
    page_id      = page["id"].replace("-", "")
    title        = page_title(page)
    url          = page.get("url", "")
    last_edited  = page.get("last_edited_time", "")   # ISO 8601 문자열
    db_props     = extract_db_properties(page)   # DB 항목이면 속성 추출, 일반 페이지면 {}

    print(f"  [{idx:03d}] {title[:60]}")
    print(f"        {url}")
    if db_props:
        print(f"        DB 속성: {list(db_props.keys())}")

    try:
        text       = fetch_blocks_recursive(client, token, page_id)
        word_count = len(text.split())

        # ── 텍스트 부족 페이지는 저장하지 않고 건너뜀 ──────────────────────
        if word_count < min_words:
            print(f"        ⏭️  건너뜀: {word_count} 단어 (최소 {min_words} 단어 미만)")
            return {"idx": idx, "title": title, "url": url, "page_id": page_id,
                    "word_count": word_count, "file": None, "meaningful": False,
                    "db_properties": db_props, "skip_reason": "텍스트 부족"}

        # frontmatter 구성 — DB 속성이 있으면 db_properties 줄 추가
        frontmatter = (
            f"---\n"
            f"title: {title}\n"
            f"notion_url: {url}\n"
            f"page_id: {page_id}\n"
            f"last_edited_time: {last_edited}\n"
        )
        if db_props:
            frontmatter += f"db_properties: {json.dumps(db_props, ensure_ascii=False)}\n"
        frontmatter += "---\n\n"

        fname    = f"{idx:03d}_{safe_filename(title)}.md"
        out_path = output_dir / fname
        out_path.write_text(frontmatter + text, encoding="utf-8")

        print(f"        저장: {fname} ({word_count} 단어) ✅")
        return {"idx": idx, "title": title, "url": url, "page_id": page_id,
                "word_count": word_count, "file": str(out_path), "meaningful": True,
                "db_properties": db_props}
    except Exception as e:
        print(f"        ❌ 오류: {e}")
        return {"idx": idx, "title": title, "url": url, "meaningful": False, "error": str(e)}


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Notion 페이지 전체 수집기 (멀티 본부 지원)")
    parser.add_argument("--dept",       default="strategic",
                        help="본부 이름 (config/departments.yaml 의 key, 기본: strategic)")
    parser.add_argument("--search",     default="",
                        help="검색어 지정 시 해당 키워드 페이지만 수집 (미지정 시 전체 수집)")
    parser.add_argument("--page-id",    help="특정 페이지 ID 직접 지정")
    parser.add_argument("--list-depts", action="store_true",
                        help="사용 가능한 본부 목록 출력 후 종료")
    parser.add_argument("--limit",      type=int, default=0,
                        help="수집 최대 페이지 수 (0=무제한, 기본: 0)")
    parser.add_argument("--min-words",  type=int, default=30,
                        help="저장할 최소 단어 수 (기본: 30). 미만인 페이지는 .md 파일을 만들지 않음")
    args = parser.parse_args()

    # 본부 목록 출력
    if args.list_depts:
        sys.path.insert(0, str(Path(__file__).parent))
        from dept_config import list_depts
        depts = list_depts()
        print("사용 가능한 본부:")
        for d in depts:
            print(f"  - {d}")
        return

    # 본부 설정 로드
    sys.path.insert(0, str(Path(__file__).parent))
    from dept_config import load_dept
    dept_cfg = load_dept(args.dept)

    token      = dept_cfg["notion_token"]
    output_dir = dept_cfg["data_dir"] / "notion_pages"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"📥 Notion 수집 — {dept_cfg['name']} ({args.dept})")
    print(f"   저장 경로: {output_dir}")
    print("=" * 60)

    min_words = args.min_words
    results = []
    with httpx.Client(timeout=60) as client:
        if args.page_id:
            # 특정 페이지
            page = fetch_page_meta(client, token, args.page_id)
            results.append(save_page(client, token, page, 1, output_dir, min_words=min_words))

        elif args.search:
            # 검색어 수집
            print(f"\n🔍 검색: '{args.search}'")
            pages = search_pages(client, token, args.search)
            print(f"   {len(pages)}개 발견\n")
            limit = args.limit or len(pages)
            for i, page in enumerate(pages[:limit], 1):
                results.append(save_page(client, token, page, i, output_dir, min_words=min_words))
                print()

        else:
            # 전체 수집 (limit 있으면 API 호출 단계에서 중단)
            if args.limit:
                print(f"\n🌐 최대 {args.limit}개 페이지 수집 중...")
            else:
                print("\n🌐 API 권한 내 모든 페이지 수집 중...")
            pages = fetch_all_pages(client, token, limit=args.limit)
            print(f"\n   총 {len(pages)}개 페이지 수집 완료\n")
            for i, page in enumerate(pages, 1):
                results.append(save_page(client, token, page, i, output_dir, min_words=min_words))
                print()

    # 결과 요약
    meaningful   = [r for r in results if r.get("meaningful")]
    skipped_text = [r for r in results if r.get("skip_reason") == "텍스트 부족"]
    errored      = [r for r in results if r.get("error")]

    print("\n" + "=" * 60)
    print(f"📊 수집 완료 — {dept_cfg['name']}")
    print(f"   전체: {len(results)}개")
    print(f"   저장: {len(meaningful)}개 ✅")
    print(f"   텍스트 부족 ({min_words}단어 미만): {len(skipped_text)}개 ⏭️  (파일 미생성)")
    print(f"   오류: {len(errored)}개")
    print(f"   저장 경로: {output_dir}")
    print("=" * 60)

    # 요약 저장
    summary_path = output_dir / "fetch_summary.json"
    summary_path.write_text(
        json.dumps({
            "dept": args.dept,
            "name": dept_cfg["name"],
            "total": len(results),
            "meaningful": len(meaningful),
            "results": results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n결과: {summary_path}")
    print("\n다음 단계:")
    print(f"  python src/pipeline/ingest.py --dept {args.dept} --reset")


if __name__ == "__main__":
    main()
