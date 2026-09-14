#!/usr/bin/env python3
"""
requirements.txt 를 **지금 도는 버전**으로 고정합니다
─────────────────────────────────────────────────────────────────────────────
2026-09-14 사고: anthropic 이 조용히 1.2.0 으로 올라가면서 `temperature` 명명
인자가 사라져 **모든 LLM 호출이 실패하고 그래프가 통째로 비었습니다.**
`anthropic[vertex]>=0.40.0` 은 그걸 전혀 막지 못합니다 — 하한만 있고 상한이
없으면, `.venv` 를 다시 만드는 순간 같은 일이 반복됩니다.

fastmcp 만 `<5.0.0` 상한이 있는데, 그것도 이미 한 번 데인 뒤에 붙인 것입니다.
사고를 겪은 패키지에만 붙이는 방식으로는 다음 사고를 막을 수 없습니다.

이 도구는 **현재 설치된 버전을 그대로 상한으로** 박습니다:

    anthropic[vertex]>=0.40.0        →  anthropic[vertex]==1.2.0
    qdrant-client>=1.9.0             →  qdrant-client==1.12.1

주석과 구조는 그대로 둡니다 — 왜 그 패키지가 필요한지 적어둔 설명이 사라지면
다음 사람이 지워도 되는 줄 알게 됩니다.

전체 의존성 트리는 requirements.lock.txt 에 따로 남깁니다. requirements.txt 는
**직접 쓰는 것**만, lock 은 **재현에 필요한 전부**입니다.

올릴 때:
    의도적으로 올린 뒤 이 도구를 다시 돌리세요. 그러면 "언제 무엇을 왜 올렸는지"
    가 git 이력에 남습니다. 조용히 올라가는 것과 정반대입니다.

실행 (반드시 대상 환경의 파이썬으로):
    .venv/bin/python tools/pin_requirements.py            # 미리보기
    .venv/bin/python tools/pin_requirements.py --write    # 반영
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
REQ = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock.txt"

# 패키지 줄 파싱: 이름[추가] 버전제약 <정렬 공백> #주석
# spec 을 non-greedy 로 두고 gap 을 따로 잡습니다 — 그러지 않으면 spec 이 주석
# 앞 정렬 공백까지 삼켜서, 고정한 뒤 파일의 주석 정렬이 무너집니다.
_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)"
    r"(?P<extras>\[[^\]]+\])?"
    r"(?P<spec>\s*[<>=!~][^#]*?)?"
    r"(?P<gap>\s*)"
    r"(?P<comment>#.*)?$"
)


def installed_versions() -> dict:
    """{정규화된 패키지명: 버전}"""
    try:
        from importlib.metadata import distributions

        out = {}
        for d in distributions():
            name = (d.metadata["Name"] or "").strip()
            if name:
                out[normalize(name)] = d.version
        return out
    except Exception as exc:
        print(f"❌ 설치 목록을 읽지 못했습니다: {exc}")
        return {}


def normalize(name: str) -> str:
    """PEP 503 정규화 — psycopg2_binary 와 psycopg2-binary 를 같게 봅니다.

    >>> normalize("psycopg2_binary")
    'psycopg2-binary'
    >>> normalize("PyYAML")
    'pyyaml'
    >>> normalize("google.genai")
    'google-genai'
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def pin_line(line: str, versions: dict) -> tuple[str, str]:
    """한 줄을 고정합니다. (새 줄, 사유) — 사유가 빈 문자열이면 변경 없음.

    >>> v = {"anthropic": "1.2.0"}
    >>> pin_line("anthropic[vertex]>=0.40.0", v)[0]
    'anthropic[vertex]==1.2.0'

    주석은 **같은 칸에** 남습니다. 버전 문자열 길이가 달라진 만큼 공백을
    조정하므로, 고정한 뒤에도 파일의 주석 정렬이 무너지지 않습니다:

    >>> src = "anthropic[vertex]>=0.40.0  # 주석"
    >>> out = pin_line(src, v)[0]
    >>> out
    'anthropic[vertex]==1.2.0   # 주석'
    >>> out.index("#") == src.index("#")
    True

    이미 정확히 고정돼 있으면 그대로 둡니다:

    >>> pin_line("anthropic==1.2.0", v)
    ('anthropic==1.2.0', '')

    주석·빈 줄은 건드리지 않습니다:

    >>> pin_line("# 설명", v)
    ('# 설명', '')
    >>> pin_line("", v)
    ('', '')

    설치되지 않은 패키지는 그대로 두고 알립니다:

    >>> pin_line("nothere>=1.0", v)
    ('nothere>=1.0', '설치되지 않음 — 그대로 둠')
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return line, ""

    m = _LINE.match(stripped)
    if not m:
        return line, ""

    name = m.group("name")
    extras = m.group("extras") or ""
    comment = m.group("comment") or ""
    gap = m.group("gap") or ""
    spec = (m.group("spec") or "").strip()

    ver = versions.get(normalize(name))
    if not ver:
        return line, "설치되지 않음 — 그대로 둠"
    if spec == f"=={ver}":
        return line, ""

    new_spec = f"=={ver}"
    if comment:
        # 주석 정렬을 유지합니다 — 길이가 달라진 만큼 공백을 늘리거나 줄입니다.
        # 최소 두 칸은 남겨 주석이 버전에 붙지 않게 합니다.
        gap = " " * max(2, len(gap) + len(spec) - len(new_spec))
    return f"{name}{extras}{new_spec}{gap}{comment}", f"{spec or '(제약 없음)'} → {new_spec}"


def main() -> int:
    ap = argparse.ArgumentParser(description="requirements 를 현재 버전으로 고정")
    ap.add_argument("--write", action="store_true", help="실제로 반영")
    ap.add_argument("--no-lock", action="store_true", help="lock 파일 생성 생략")
    args = ap.parse_args()

    if not REQ.exists():
        print(f"❌ 없음: {REQ}")
        return 1

    versions = installed_versions()
    if not versions:
        return 1
    print(f"  현재 환경: {sys.executable}")
    print(f"  설치 패키지 {len(versions)}개\n")

    lines = REQ.read_text(encoding="utf-8").splitlines()
    out, changes, missing = [], [], []
    for line in lines:
        new, why = pin_line(line, versions)
        out.append(new)
        if why == "설치되지 않음 — 그대로 둠":
            missing.append(line.strip())
        elif why:
            changes.append((line.strip(), new.strip()))

    if changes:
        print("■ 고정할 항목")
        for old, new in changes:
            print(f"    {old}")
            print(f"  → {new}\n")
    else:
        print("■ 모두 이미 고정돼 있습니다.")

    if missing:
        print("■ ⚠️  requirements 에 있는데 설치되지 않음")
        for m in missing:
            print(f"    {m}")
        print("    → 이 환경이 실제 운영 환경이 맞는지 확인하세요.\n")

    if not args.write:
        print("  미리보기입니다. 반영하려면 --write 를 붙이세요.")
        return 0

    REQ.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"  💾 {REQ}")

    if not args.no_lock:
        try:
            froze = subprocess.run(
                [sys.executable, "-m", "pip", "freeze"],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            ).stdout
            header = (
                "# 전체 의존성 트리 — 재현용 스냅샷입니다.\n"
                "# requirements.txt 는 **직접 쓰는 것**만, 이 파일은 **전부** 입니다.\n"
                "#   재현: pip install -r requirements.lock.txt\n"
                "#   갱신: .venv/bin/python tools/pin_requirements.py --write\n"
                f"# 생성 환경: {sys.executable}\n\n"
            )
            LOCK.write_text(header + froze, encoding="utf-8")
            print(f"  💾 {LOCK} ({len(froze.splitlines())}개)")
        except Exception as exc:
            print(f"  ⚠️  lock 생성 실패: {type(exc).__name__}: {exc}")

    print("\n  다음: git diff 로 확인하고 커밋하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
