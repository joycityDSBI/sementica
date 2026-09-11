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
import contextlib
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

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

NOTION_VERSION = "2022-06-28"
RATE_LIMIT_DELAY = 0.34  # 3 req/s 준수


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
        print(
            f"  페이지 {page_num}: {len(results)}개 수집 (누적 {len(pages)}개)"
            + (f" / 목표 {limit}개" if limit else "")
        )

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


def query_database(
    client: httpx.Client,
    token: str,
    database_id: str,
    since_iso: str | None = None,
) -> list:
    """
    Notion 데이터베이스의 모든 항목(row)을 직접 쿼리합니다.

    /search API는 DB 항목을 누락할 수 있으므로
    departments.yaml 에 등록된 DB는 이 함수로 직접 수집합니다.

    Args:
        database_id: 하이픈 포함/제외 모두 허용 (Notion API가 정규화)
        since_iso:   ISO 8601 문자열. 지정 시 last_edited_time > since_iso 인 항목만 반환.

    Returns:
        Notion page 객체 목록 (각 항목은 page 객체, db_properties 포함)
    """
    items = []
    cursor = None
    batch = 1
    # 하이픈 제거 — API는 양쪽 포맷을 허용하지만 일관성 유지
    db_id = database_id.replace("-", "")

    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        if since_iso:
            body["filter"] = {
                "timestamp": "last_edited_time",
                "last_edited_time": {"after": since_iso},
            }
        resp = client.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=notion_headers(token),
            json=body,
        )
        resp.raise_for_status()
        time.sleep(RATE_LIMIT_DELAY)
        data = resp.json()

        results = data.get("results", [])
        items.extend(results)
        print(f"    DB 배치 {batch:02d}: {len(results)}개 수집 (누적 {len(items)}개)")
        batch += 1

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return items


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
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rich)
        indent = "  " * depth

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


class _HTMLStripper(HTMLParser):
    """
    HTML → 평문 텍스트 변환기 (표준 라이브러리만 사용).

    script·style·head 등 비표시 태그 내용을 건너뛰고,
    블록 레벨 태그(p, div, h1~h6, tr, li …)에서 줄바꿈을 삽입합니다.
    """

    # HTML void 요소(meta, link 등)는 닫는 태그가 없으므로 _SKIP_TAGS에 포함하면
    # _skip_depth가 증가만 하고 감소하지 않아 이후 모든 body 내용이 스킵됩니다.
    # → script/style/head/noscript/template만 스킵 (void 요소 제외)
    _SKIP_TAGS: ClassVar[set] = {
        "script",
        "style",
        "head",
        "noscript",
        "template",
    }
    _BLOCK_TAGS: ClassVar[set] = {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "thead",
        "tbody",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str):
        if not self._skip_depth:
            self._parts.append(data)

    def result(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t]+", " ", raw)  # 연속 공백 → 단일 스페이스
        raw = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", raw)  # 3줄 이상 → 2줄
        return raw.strip()


def _html_to_text(html_content: str) -> str:
    """HTML 문자열에서 평문 텍스트를 추출합니다."""
    stripper = _HTMLStripper()
    with contextlib.suppress(Exception):
        stripper.feed(html_content)
    return stripper.result()


def _is_notion_s3_url(url: str) -> bool:
    """Notion이 내부적으로 사용하는 S3 파일 URL 여부를 판단합니다."""
    lower = url.lower()
    return (
        "prod-files-secure.s3" in lower
        or "notion-static.com" in lower
        or "secure.notion-static.com" in lower
    )


def _is_html_content(resp) -> bool:
    """HTTP 응답이 실제 HTML 문서인지 판단합니다.

    Content-Type이 application/xml·application/octet-stream 등으로 잘못 보고되는
    경우를 대비해 본문 앞부분도 확인합니다.
    S3 서명 URL: HEAD → application/xml(접근 오류), GET → 실제 Content-Type
    """
    ctype = resp.headers.get("content-type", "").lower()
    if "text/html" in ctype:
        return True
    # Content-Type이 불확실하면 본문 앞 512바이트로 판단
    text_start = resp.text[:512].lstrip().lower()
    return (
        text_start.startswith("<!doctype html")
        or text_start.startswith("<html")
        or ("<html" in text_start[:200] and "<body" in text_start)
    )


def _extract_attached_html(client, block: dict) -> str:
    """
    Notion file/embed 블록에서 HTML 파일을 다운로드하고 텍스트를 추출합니다.

    지원 블록 유형:
      - file  : Notion에 직접 첨부된 .html/.htm 파일
                content["name"] 기반 판단 (S3 서명 URL에 파일명 없음)
      - embed : Notion 내부 S3 파일 embed (prod-files-secure.s3…)
                URL 확장자가 없으므로 실제 다운로드 후 내용으로 판단
              + 외부 .html/.htm URL embed

    Notion 첨부 파일의 서명된 S3 URL은 만료 시간이 있으므로
    수집 시점에 즉시 다운로드합니다.

    Returns:
        추출된 평문 텍스트. HTML이 아니거나 다운로드 실패 시 "".
    """
    btype = block.get("type", "")
    content = block.get(btype, {})

    is_html_by_name = False
    is_notion_s3_emb = False

    if btype == "file":
        # file 블록: 파일명(name) 기반 HTML 판단
        # S3 서명 URL은 쿼리스트링만 있어 확장자 체크 불가 → name 필드 사용
        name = content.get("name", "").lower()
        if not (name.endswith(".html") or name.endswith(".htm")):
            return ""
        is_html_by_name = True
        inner = content.get("file") or content.get("external") or {}
        url = inner.get("url", "")

    elif btype == "embed":
        url = content.get("url", "")
        url_lower = url.lower()
        # Notion 내부 S3 embed: URL에 파일명 확장자 없음 → 다운로드 후 내용 확인
        is_s3 = _is_notion_s3_url(url)
        if is_s3:
            is_notion_s3_emb = True
        elif not (url_lower.endswith(".html") or url_lower.endswith(".htm")):
            return ""  # YouTube 등 일반 외부 embed — 건너뜀
    else:
        return ""

    if not url:
        return ""

    try:
        resp = client.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()

        if is_html_by_name:
            # file 블록: name으로 이미 확인 완료 → Content-Type 재확인 불필요
            # (S3는 application/octet-stream 반환 가능)
            pass
        elif is_notion_s3_emb:
            # Notion S3 embed: 실제 내용으로 HTML 여부 판단
            if not _is_html_content(resp):
                return ""  # 이미지·PDF·XML 오류 응답 등 — 건너뜀
        else:
            # 외부 .html URL embed
            ctype = resp.headers.get("content-type", "").lower()
            if "html" not in ctype and not url.lower().endswith((".html", ".htm")):
                return ""

        return _html_to_text(resp.text)

    except Exception as e:
        disp = content.get("name", url[:60])
        print(f"        ⚠️  HTML 첨부 다운로드 실패 ({disp}): {e}")
        return ""


def _table_to_md(client, token, table_block: dict) -> str:
    """
    Notion table 블록을 Markdown 테이블 문자열로 변환합니다.

    table_row 자식을 즉시 가져와 인라인으로 배치합니다.
    has_column_header == True 이면 첫 행 뒤에 구분선을 추가합니다.

    예시 출력:
        | 캠페인명          | 예산      | 담당자 |
        | ---               | ---       | ---    |
        | KR_And_UAC_tCPA   | 5,000,000 | 김린아  |
    """
    has_header = table_block.get("table", {}).get("has_column_header", False)
    rows = fetch_blocks(client, token, table_block["id"])
    if not rows:
        return ""

    lines = []
    for i, row in enumerate(rows):
        cells = row.get("table_row", {}).get("cells", [])
        cell_texts = [" ".join(t.get("plain_text", "") for t in cell) for cell in cells]
        lines.append("| " + " | ".join(cell_texts) + " |")
        # 헤더 행 다음 구분선 삽입
        if i == 0 and has_header and cell_texts:
            lines.append("| " + " | ".join(["---"] * len(cell_texts)) + " |")

    return "\n".join(lines)


def fetch_blocks_recursive(client, token, block_id, depth=0, max_depth=4) -> str:
    """
    블록을 재귀적으로 가져와 텍스트로 변환합니다.

    Notion 페이지 안의 table 블록은 인라인으로 처리합니다:
      - 테이블이 문서 내 원래 위치에 삽입됨 (기존에는 맨 뒤에 붙었음)
      - has_column_header 일 때 구분선 자동 추가
    """
    if depth > max_depth:
        return ""
    blocks = fetch_blocks(client, token, block_id)

    parts = []
    for block in blocks:
        btype = block.get("type", "")

        # ── 테이블: 행을 즉시 가져와 인라인 Markdown 테이블로 변환 ─────────
        if btype == "table":
            md = _table_to_md(client, token, block)
            if md:
                parts.append(md)
            continue  # table_row 자식은 이미 처리 완료

        # ── HTML 첨부 파일 / embed ───────────────────────────────────────
        # Notion에 올려둔 .html/.htm 파일을 다운로드해 텍스트로 변환합니다.
        # 서명된 S3 URL은 만료되므로 수집 시점에 즉시 처리합니다.
        if btype in ("file", "embed"):
            html_text = _extract_attached_html(client, block)
            if html_text:
                name = block.get(btype, {}).get("name", "HTML 첨부")
                print(f"        📎 HTML 첨부 추출: {name} ({len(html_text)} 자)")
                parts.append(f"[첨부 HTML: {name}]\n{html_text}")
            elif btype == "file":
                # HTML이 아닌 첨부 파일은 파일명만 기록
                name = block.get("file", {}).get("name", "")
                if name:
                    parts.append(f"[첨부 파일: {name}]")
            continue

        # ── 일반 블록 ────────────────────────────────────────────────────
        block_text = blocks_to_text([block], depth)
        if block_text.strip():
            parts.append(block_text)

        # 자식이 있는 블록 재귀 (table/file/embed 제외 — 위에서 처리)
        if block.get("has_children") and btype not in ("table", "file", "embed"):
            child = fetch_blocks_recursive(client, token, block["id"], depth + 1, max_depth)
            if child.strip():
                parts.append(child)

    return "\n".join(part for part in parts if part.strip())


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
    names = [p.get("name", "") for p in people if p.get("name")]
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
    "select": _prop_select,
    "multi_select": _prop_multi_select,
    "date": _prop_date,
    "people": _prop_people,
    "rich_text": _prop_rich_text,
    "number": _prop_number,
    "checkbox": _prop_checkbox,
    "url": _prop_url,
    "email": _prop_email,
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
    props = page.get("properties", {})
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
    """
    Notion 페이지의 제목을 반환합니다.

    Notion DB 항목은 title 속성의 컬럼 이름이 자유롭게 설정됩니다.
    (예: "메모", "이름", "제목", "Name" 등)
    따라서 이름이 아닌 type == "title" 인 속성을 찾아 반환합니다.
    """
    props = page.get("properties", {})
    # type == "title" 인 속성 탐색 (컬럼명 무관)
    for val in props.values():
        if val.get("type") == "title":
            rt = val.get("title", [])
            t = "".join(t.get("plain_text", "") for t in rt).strip()
            if t:
                return t
    return page.get("id", "untitled")


def safe_filename(title: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", title)[:80]


# ─── 페이지 저장 ──────────────────────────────────────────────────────────────
def save_page(client, token, page, idx, output_dir: Path, min_words: int = 30) -> dict:
    """Notion 페이지를 .md 로 저장합니다.

    Args:
        min_words: 이 단어 수 미만인 페이지는 .md 파일을 저장하지 않고 건너뜁니다.
                   기본값 30. --min-words CLI 인수로 조정 가능.
    """
    page_id = page["id"].replace("-", "")
    title = page_title(page)
    url = page.get("url", "")
    last_edited = page.get("last_edited_time", "")  # ISO 8601 문자열
    db_props = extract_db_properties(page)  # DB 항목이면 속성 추출, 일반 페이지면 {}

    print(f"  [{idx:03d}] {title[:60]}")
    print(f"        {url}")
    if db_props:
        print(f"        DB 속성: {list(db_props.keys())}")

    try:
        text = fetch_blocks_recursive(client, token, page_id)
        word_count = len(text.split())

        # ── DB 항목: page body가 비어있으면 속성값에서 텍스트 합성 ──────────
        # Notion DB row는 속성(properties)만 채워지고 page body는 빈 경우가 많다.
        # 이 경우 속성값을 줄글로 합성해 벡터 임베딩과 LLM 추출에 활용한다.
        if db_props and word_count < min_words:
            prop_lines = [f"{k}: {v}" for k, v in db_props.items()]
            prop_text = "\n".join(prop_lines)
            text = (prop_text + ("\n\n" + text if text.strip() else "")).strip()
            word_count = len(text.split())
            if word_count >= min_words:
                print(f"        🔧 DB 속성에서 텍스트 합성 ({word_count} 단어)")

        # ── 텍스트 부족 페이지는 저장하지 않고 건너뜀 ──────────────────────
        # DB 항목은 구조적 데이터 — 속성값이 하나라도 있으면 단어 수 기준 완화 (5단어)
        # 일반 페이지는 min_words(기본 30단어) 기준 유지
        effective_min = 1 if db_props else min_words
        if word_count < effective_min:
            print(f"        ⏭️  건너뜀: {word_count} 단어 (최소 {effective_min} 단어 미만)")
            return {
                "idx": idx,
                "title": title,
                "url": url,
                "page_id": page_id,
                "word_count": word_count,
                "file": None,
                "meaningful": False,
                "db_properties": db_props,
                "skip_reason": "텍스트 부족",
            }

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

        # 파일명은 page_id 로 시작합니다. 예전에는 수집 **회차의 순번**(idx)을
        # 앞에 붙였는데, fetch 를 다시 돌리면 같은 페이지에 다른 번호가 붙어
        # 새 파일로 쌓였습니다. 실측: 파일 1258개 = 실제 페이지 299개(4.2배).
        # 인제스트가 같은 페이지를 네 번씩 LLM 에 넣고, Qdrant 포인트 ID 가
        # notion_url 기준이라 서로 덮어써서, 로그의 "1225개 저장"과 저장소의
        # 268개가 4배 어긋났습니다.
        fname = f"{page_id}_{safe_filename(title)}.md"
        out_path = output_dir / fname

        # 제목이 바뀌면 이름도 바뀌므로, 같은 page_id 의 옛 파일을 정리합니다.
        for stale in output_dir.glob(f"{page_id}_*.md"):
            if stale != out_path:
                with contextlib.suppress(OSError):
                    stale.unlink()

        out_path.write_text(frontmatter + text, encoding="utf-8")

        print(f"        저장: {fname} ({word_count} 단어) ✅")
        return {
            "idx": idx,
            "title": title,
            "url": url,
            "page_id": page_id,
            "word_count": word_count,
            "file": str(out_path),
            "meaningful": True,
            "db_properties": db_props,
        }
    except Exception as e:
        print(f"        ❌ 오류: {e}")
        return {"idx": idx, "title": title, "url": url, "meaningful": False, "error": str(e)}


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Notion 페이지 전체 수집기 (멀티 본부 지원)")
    parser.add_argument(
        "--dept",
        default="strategic",
        help="본부 이름 (config/departments.yaml 의 key, 기본: strategic)",
    )
    parser.add_argument(
        "--search",
        default="",
        help="검색어 지정 시 해당 키워드 페이지만 수집 (미지정 시 전체 수집)",
    )
    parser.add_argument("--page-id", help="특정 페이지 ID 직접 지정")
    parser.add_argument(
        "--list-depts", action="store_true", help="사용 가능한 본부 목록 출력 후 종료"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="수집 최대 페이지 수 (0=무제한, 기본: 0)"
    )
    parser.add_argument(
        "--min-words",
        type=int,
        default=30,
        help="저장할 최소 단어 수 (기본: 30). 미만인 페이지는 .md 파일을 만들지 않음",
    )
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

    token = dept_cfg["notion_token"]
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

            # ── departments.yaml 에 등록된 Notion DB 직접 쿼리 ───────────────
            # /search API는 DB 항목을 누락할 수 있으므로 명시적으로 DB를 쿼리한다.
            notion_databases = dept_cfg.get("notion_databases", [])
            if notion_databases:
                print(f"\n📂 Notion DB 직접 쿼리 ({len(notion_databases)}개 DB)")
                offset = len(results)
                for db_entry in notion_databases:
                    db_id = db_entry["id"] if isinstance(db_entry, dict) else str(db_entry)
                    db_name = db_entry.get("name", db_id) if isinstance(db_entry, dict) else db_id
                    print(f"\n  🗄️  {db_name} ({db_id})")
                    try:
                        db_items = query_database(client, token, db_id)
                        print(f"     총 {len(db_items)}개 항목")
                        for j, item in enumerate(db_items, 1):
                            results.append(
                                save_page(
                                    client, token, item, offset + j, output_dir, min_words=min_words
                                )
                            )
                            print()
                        offset += len(db_items)
                    except Exception as e:
                        print(f"     ❌ DB 쿼리 실패: {e}")

    # 결과 요약
    meaningful = [r for r in results if r.get("meaningful")]
    skipped_text = [r for r in results if r.get("skip_reason") == "텍스트 부족"]
    errored = [r for r in results if r.get("error")]

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
        json.dumps(
            {
                "dept": args.dept,
                "name": dept_cfg["name"],
                "total": len(results),
                "meaningful": len(meaningful),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n결과: {summary_path}")
    print("\n다음 단계:")
    print(f"  python src/pipeline/ingest.py --dept {args.dept} --reset")


if __name__ == "__main__":
    main()
