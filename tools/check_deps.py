#!/usr/bin/env python3
"""
코드가 import 하는데 requirements.txt 에 없는 패키지를 찾습니다
─────────────────────────────────────────────────────────────────────────────
2026-09-15 라이브 서버 이전 중에 드러난 문제입니다. `web_app.py` 는 `fastapi`
와 `pydantic` 을, `rest_api.py` 는 `starlette` 를 **처음부터** import 하고
있었는데 `requirements.txt` 에는 넷 다 없었습니다.

**구 서버에서는 아무 문제가 없었습니다.** 손으로 깔려 있었으니까요. 그래서
`.venv` 를 새로 만들기 전까지 아무도 몰랐고, 만들자마자 Ops 대시보드가
`pip install fastapi uvicorn` 만 찍고 죽는 재시작 루프에 빠졌습니다.

하필 그 상태에서 `systemctl status` 는 **`active (running)`** 이라고 나옵니다
(방금 재시작된 순간을 보여주므로). 상태만 보면 정상으로 읽힙니다.

`check_schema_drift.py` 와 같은 종류의 도구입니다 — 평소에 드러나지 않고,
서버를 새로 세우거나 복구할 때 제일 급한 순간에 드러나는 어긋남을 미리 잡습니다.
설치 여부와 무관하게 **소스만 읽으므로** CI 에서도 돌아갑니다.

실행:
    python tools/check_deps.py
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
REQ = ROOT / "requirements.txt"
# `dags/` 는 제외합니다 — **Airflow 서버에서** 돌므로 이 VM 의 requirements 에
# airflow·pendulum 이 들어가면 안 됩니다. DAG 이 이 레포의 다른 코드를 import
# 하지도 않습니다(CLI 를 호출할 뿐입니다).
SCAN_DIRS = ("src", "tools", "scripts", "falkordb")

# import 이름과 배포 이름이 다른 것들. 자동으로 알아내려면 패키지가 **설치되어
# 있어야** 하는데(importlib.metadata), 그러면 "설치 안 된 곳에서도 돌아간다"는
# 이 도구의 목적이 사라집니다. 그래서 손으로 적습니다.
ALIASES = {
    "yaml": "pyyaml",
    "psycopg2": "psycopg2-binary",
    "google": "google-cloud-aiplatform",  # google.genai / google.cloud 양쪽
    "redis": "redis",
    "dotenv": "python-dotenv",
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
}

# requirements 에 적지 않아도 되는 것들 — 다른 패키지가 반드시 끌고 오거나
# 표준 배포에 포함됩니다. 넣을 때는 **왜 안전한지**를 함께 적으세요.
IMPLIED = {
    "pkg_resources": "setuptools 에 포함",
}


def local_modules() -> set:
    """레포 안에서 import 가능한 이름 — 서드파티로 오인하면 안 됩니다.

    `src/` 가 sys.path 에 들어가므로 `server`·`utils` 같은 이름이 최상위
    모듈처럼 보입니다. `src/mcp/` 처럼 실제 패키지와 이름이 겹치는 것도
    있어(그래서 rest_api.py 가 순환 import 로 죽은 적이 있습니다) 로컬로
    분류합니다 — 놓치는 쪽이 없는 것을 있다고 우기는 쪽보다 낫습니다.
    """
    names = set()
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        names.add(d)
        for p in base.rglob("*.py"):
            names.add(p.stem)
            rel = p.relative_to(base).parts
            if len(rel) > 1:
                names.add(rel[0])
    return names


def declared() -> set:
    """requirements.txt 가 선언한 배포 이름 (소문자, extras 제외).

    >>> import io
    >>> sorted(_parse_req(io.StringIO(
    ...     'anthropic[vertex]>=0.40.0\\n# 주석\\nqdrant-client>=1.9\\n')))
    ['anthropic', 'qdrant-client']
    """
    if not REQ.exists():
        return set()
    with REQ.open(encoding="utf-8") as fh:
        return _parse_req(fh)


def canon(name: str) -> str:
    """PEP 503 정규화 — `qdrant_client` 와 `qdrant-client` 는 같은 것입니다.

    이걸 안 하면 import 이름(밑줄)과 배포 이름(하이픈)이 다른 패키지가
    전부 "선언 안 됨"으로 잡혀, 도구가 늑대 소년이 됩니다.

    >>> canon('Qdrant_Client'), canon('google-genai')
    ('qdrant-client', 'google-genai')
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_req(lines) -> set:
    out = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)", line)
        if m:
            out.add(canon(m.group(1)))
    return out


def imported(py: str) -> set:
    """소스에서 최상위 import 이름을 뽑습니다.

    >>> sorted(imported('import os\\nfrom fastapi import FastAPI\\nimport a.b.c'))
    ['a', 'fastapi', 'os']

    상대 import 는 레포 안이므로 제외합니다:

    >>> imported('from . import sibling')
    set()
    """
    try:
        tree = ast.parse(py)
    except SyntaxError:
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
    return out


def main() -> int:
    if not REQ.exists():
        print(f"❌ {REQ} 가 없습니다")
        return 1

    decl = declared()
    local = local_modules()
    stdlib = set(sys.stdlib_module_names)

    # {import 이름: {그것을 쓰는 파일, ...}}
    uses: dict = {}
    scanned = 0
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            scanned += 1
            for name in imported(p.read_text(encoding="utf-8", errors="replace")):
                uses.setdefault(name, set()).add(str(p.relative_to(ROOT)))

    missing: dict = {}
    for name, files in uses.items():
        if name in stdlib or name in local or name in IMPLIED:
            continue
        dist = canon(ALIASES.get(name, name))
        if dist not in decl:
            missing[name] = (dist, sorted(files))

    external = {n for n in uses if n not in stdlib and n not in local}
    print(f"  파일 {scanned}개 / 외부 import {len(external)}종")
    print(f"  requirements.txt 선언 {len(decl)}개\n")

    if not missing:
        print("  선언되지 않은 외부 의존성 없음.")
        return 0

    print(f"❌ 코드가 쓰는데 requirements.txt 에 없는 패키지 {len(missing)}개\n")
    for name, (dist, files) in sorted(missing.items()):
        shown = ", ".join(files[:3])
        more = f" 외 {len(files) - 3}개" if len(files) > 3 else ""
        alias = f"  (배포명: {dist})" if dist != name else ""
        print(f"    {name}{alias}")
        print(f"        {shown}{more}")

    print("\n  requirements.txt 에 추가하세요. 전이 의존성으로 들어온다는")
    print("  이유로 생략하지 마세요 — 상위 패키지가 그것을 떼는 날 조용히")
    print("  깨지고, 그때는 원인이 이 파일에 안 남아 있습니다.")
    print("\n  이름이 다른 패키지는 이 파일의 ALIASES 에 넣으세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
