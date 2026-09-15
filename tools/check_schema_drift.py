#!/usr/bin/env python3
"""
스키마 파일과 코드가 어긋났는지 점검 — DB 없이도 됩니다
─────────────────────────────────────────────────────────────────────────────
2026-09-15 서버 이전 중에 드러난 문제입니다. `db_logger` 는 `notion_pages` 에
`route` · `content_hash` 를 오래전부터 쓰고 있었는데 `schema/ops_log.sql` 에는
그 컬럼이 없었습니다. 운영 DB 에만 `ALTER TABLE` 로 추가하고 파일에는 반영하지
않은 것입니다.

**기존 서버에서는 아무 문제가 없었습니다.** 컬럼이 이미 있으니까요. 그래서
새 서버를 세우기 전까지 아무도 몰랐고, 세우자마자 인제스트가 전부
`column "route" does not exist` 로 실패했습니다.

이런 드리프트는 **평소에 드러나지 않습니다.** 드러나는 시점이 하필 서버를
옮기거나 장애에서 복구할 때라, 제일 급할 때 발목을 잡습니다.

이 도구는 코드의 INSERT 문과 스키마 파일의 CREATE TABLE 을 대조합니다.
DB 에 붙지 않으므로 어디서든, CI 에서도 돌릴 수 있습니다.

실행:
    python tools/check_schema_drift.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCHEMA = ROOT / "schema" / "ops_log.sql"
CODE = ROOT / "src" / "ops" / "db_logger.py"

# CREATE TABLE 의 컬럼 줄 — 들여쓰기된 `이름  타입` 형태만 잡고
# CONSTRAINT · PRIMARY KEY 같은 줄은 제외합니다.
_COL = re.compile(r"^\s+([a-z_][a-z0-9_]*)\s+[A-Z]", re.M)
_NOT_COLUMN = {"constraint", "primary", "unique", "foreign", "check"}


def schema_columns(sql: str) -> dict:
    """{테이블명: {컬럼, ...}} — CREATE TABLE 과 ALTER TABLE ADD COLUMN 을 모두 봅니다.

    >>> s = '''CREATE TABLE IF NOT EXISTS t (
    ...     id   BIGSERIAL PRIMARY KEY,
    ...     name TEXT,
    ...     CONSTRAINT uq UNIQUE (name)
    ... );
    ... ALTER TABLE t ADD COLUMN IF NOT EXISTS extra VARCHAR(20);'''
    >>> sorted(schema_columns(s)["t"])
    ['extra', 'id', 'name']
    """
    out: dict = {}
    for m in re.finditer(r"CREATE TABLE(?: IF NOT EXISTS)? (\w+)\s*\((.*?)\n\);", sql, re.S):
        table, body = m.group(1), m.group(2)
        cols = {c for c in _COL.findall(body) if c not in _NOT_COLUMN}
        out.setdefault(table, set()).update(cols)
    for m in re.finditer(r"ALTER TABLE (\w+)\s+ADD COLUMN(?: IF NOT EXISTS)?\s+(\w+)", sql, re.I):
        out.setdefault(m.group(1), set()).add(m.group(2))
    return out


# 컬럼 목록으로 볼 수 있는 것: 식별자·쉼표·공백·줄바꿈·문자열 이음표뿐.
# 다른 문자가 섞여 있으면 f-string 으로 컬럼을 조립하는 INSERT 라, 정적으로는
# 읽을 수 없습니다. 억지로 파싱하면 코드 본문을 컬럼으로 오인합니다 —
# 실제로 store_snapshot 에서 `def`·`import`·`except` 같은 것이 컬럼으로 잡혔습니다.
_COL_LIST = re.compile(r"""^[\sa-z0-9_,"']+$""", re.I)


def code_columns(py: str) -> tuple[dict, set]:
    """INSERT INTO 문에서 (정적 컬럼 목록, 동적으로 조립된 테이블) 을 뽑습니다.

    >>> c = 'cur.execute("INSERT INTO t (a, b,\\n c) VALUES (%s,%s,%s)")'
    >>> cols, dyn = code_columns(c)
    >>> sorted(cols["t"]), dyn
    (['a', 'b', 'c'], set())

    컬럼을 f-string 으로 만드는 INSERT 는 **대조하지 않고 따로 알립니다**:

    >>> code_columns('f"INSERT INTO s (dept, {cols}) VALUES (%s, {ph})"')
    ({}, {'s'})

    실제 db_logger 의 store_snapshot 이 이 형태입니다. 걸러내지 않으면 정규식이
    다음 INSERT 까지 삼켜서 `def`·`import` 같은 것이 컬럼으로 잡힙니다.
    """
    out: dict = {}
    dynamic: set = set()
    for m in re.finditer(r"INSERT INTO (\w+)\s*\((.*?)\)\s*VALUES", py, re.S):
        table, body = m.group(1), m.group(2)
        if not _COL_LIST.match(body):
            dynamic.add(table)
            continue
        cols = set(re.findall(r"[a-z_][a-z0-9_]*", body))
        out.setdefault(table, set()).update(cols)
    return out, dynamic


def main() -> int:
    if not SCHEMA.exists() or not CODE.exists():
        print(f"❌ 파일 없음: {SCHEMA if not SCHEMA.exists() else CODE}")
        return 1

    sch = schema_columns(SCHEMA.read_text(encoding="utf-8"))
    cod, dynamic = code_columns(CODE.read_text(encoding="utf-8"))

    print(f"  스키마 테이블 {len(sch)}개 / 정적 INSERT {len(cod)}개\n")

    problems = 0
    for table in sorted(cod):
        if table not in sch:
            print(f"❌ {table}: 스키마에 테이블 자체가 없습니다")
            problems += 1
            continue
        missing = cod[table] - sch[table]
        if missing:
            print(f"❌ {table}: 코드가 쓰는데 스키마에 없는 컬럼 — {sorted(missing)}")
            problems += 1
        else:
            print(f"✅ {table}: {len(cod[table])}개 컬럼 일치")

    for table in sorted(dynamic):
        known = "스키마에 있음" if table in sch else "❌ 스키마에 없음"
        print(f"➖ {table}: 컬럼을 코드에서 조립 — 대조 불가 ({known})")
    if dynamic:
        print("   f-string 으로 컬럼 목록을 만드는 INSERT 는 정적으로 읽을 수 없습니다.")
        print("   해당 테이블은 실제 DB 에 붙어 확인해야 합니다.")

    if problems:
        print(f"\n  {problems}개 테이블이 어긋나 있습니다.")
        print("  schema/ops_log.sql 에 컬럼을 추가하고, **ALTER TABLE ... ADD COLUMN")
        print("  IF NOT EXISTS 도 함께** 넣으세요. CREATE TABLE IF NOT EXISTS 는")
        print("  테이블이 있으면 아무것도 하지 않아, 기존 DB 에는 반영되지 않습니다.")
        return 1

    print("\n  어긋난 곳 없음.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
