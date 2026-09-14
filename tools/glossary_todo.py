#!/usr/bin/env python3
"""
용어집에 등록·수정할 항목 뽑기 — 그래프 실측 기반
─────────────────────────────────────────────────────────────────────────────
용어집은 사람이 관리합니다. 이 도구는 **무엇을 등록할지 정하지 않고**, 그래프가
실제로 무엇을 쌓았는지 보여줘서 담당자가 판단할 재료를 만듭니다. 근거(엣지 수,
현재 표기)를 함께 내보내므로 목록만 보고도 검토할 수 있습니다.

왜 필요한가:

  · 조회 단계에서 이미 합치고 있지만(`utils/retrieval.lookup_entity`), 그건
    **매 질문마다 다시 하는 일**입니다. 용어집에 등록하면 인제스트 시점에
    한 번 정규화되어 그래프 자체가 깨끗해지고, 재인제스트해도 유지됩니다.
  · 조회 단계가 못 합치는 것도 있습니다 — `데브옵스` 와 `데브옵스팀` 은 표기
    차이가 아니라 다른 이름이라, 사람이 같은 것이라고 알려줘야 합니다.

내보내는 항목:

  ① 용어집 내부 모순    코드로 못 고칩니다. 담당자가 용어집에서 풀어야 합니다.
  ② 표기 통일           그래프에 여러 표기로 쌓인 것 — 동의어로 등록하면 합쳐집니다.
  ③ 미등록 조직·팀      엣지가 많은데 용어집에 없는 `:Team` / `:Role` 노드.
  ④ 미등록 업무 용어    그 외 엣지가 많은 미등록 노드.
  ⑤ 데이터 자산         테이블·뷰·경로. **용어집 대상이 아닙니다** — 참고용.
  ⑥ 인물                사람 이름. 용어집이 아니라 인사 정보로 다뤄야 합니다.

실행:
    python tools/glossary_todo.py --dept strategic
    python tools/glossary_todo.py --dept strategic --min-edges 3 --out glossary_todo.md
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pipeline"))

_env = ROOT / ".env"
if _env.exists():
    for raw in _env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))

# 조직으로 볼 라벨 — organization 카테고리 후보
_ORG_LABELS = {"Team", "Role"}
_PERSON_LABELS = {"Person"}


def is_data_asset(name: str) -> bool:
    """테이블·뷰·경로·URL 인가 — 업무 용어집에 넣을 대상이 아닙니다.

    >>> is_data_asset("dataplatform-reporting.DataService.T_0420_0000_UAPerformanceRaw_V1")
    True
    >>> is_data_asset("f_common_payment_detail_view")
    True
    >>> is_data_asset("work/RESU/README.md")
    True
    >>> is_data_asset("https://notion.so/abc")
    True
    >>> is_data_asset("마케팅사이언스팀")
    False
    >>> is_data_asset("RESU")
    False
    """
    if not name:
        return False
    low = name.lower()
    if low.startswith(("http://", "https://")) or "/" in name:
        return True
    # 점으로 구분된 식별자 경로 (project.dataset.table)
    if name.count(".") >= 2 and " " not in name:
        return True
    # 스네이크케이스 식별자 — 테이블·뷰·컬럼 이름의 전형
    if re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+){2,}", low):
        return True
    return bool(re.search(r"_(view|raw|tbl|table)$", low))


def main() -> int:
    ap = argparse.ArgumentParser(description="용어집 등록·수정 후보 추출")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--min-edges", type=int, default=2, help="미등록 후보의 최소 엣지 수")
    ap.add_argument("--top", type=int, default=25, help="유형별 최대 출력 수")
    ap.add_argument("--out", default="", help="마크다운으로 저장할 경로")
    args = ap.parse_args()

    import falkordb
    from dept_config import load_dept
    from utils import synonym_resolver as sr

    g = falkordb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT).select_graph(
        load_dept(args.dept)["falkordb_graph"]
    )

    if not sr.is_available():
        print("❌ 용어집을 로드하지 못했습니다 (API·스냅샷 모두 실패)")
        return 1

    rows = (
        g.query(
            "MATCH (n) WHERE n.name IS NOT NULL AND n.name <> '' AND NOT n:Event "
            "OPTIONAL MATCH (n)-[r:REL]-() "
            "RETURN labels(n)[0], n.name, count(r)"
        ).result_set
        or []
    )
    nodes = [(lbl or "?", str(name), deg or 0) for lbl, name, deg in rows]

    out: list = []

    def w(line: str = "") -> None:
        out.append(line)
        print(line)

    w(f"# 용어집 등록·수정 후보 ({args.dept})")
    w()
    w(f"그래프 노드 {len(nodes)}개 / 용어집 {len(sr._expand_map)}개 term 기준.")
    w()

    # ── ① 용어집 내부 모순 ──────────────────────────────────────────────
    w("## ① 용어집 내부 모순 — 담당자 확인 필요")
    w()
    conf = sr.conflicts()
    if not conf:
        w("없음.")
    else:
        w("표기를 지우면 서로 다른 용어를 가리킵니다. 어느 쪽으로도 정할 수 없어")
        w("코드는 **정규화를 포기하고 원본을 그대로 둡니다**. 용어집에서 풀어야 합니다.")
        w()
        w("| 표기 | 가리키는 용어 | 확인할 것 |")
        w("|---|---|---|")
        for key, canons in conf:
            forms = {f: c for c, fs in sr._expand_map.items() for f in fs if sr.norm_key(f) == key}
            detail = ", ".join(f"`{f}`→{c}" for f, c in sorted(forms.items()))
            w(f"| `{key}` | {' / '.join(canons)} | {detail} |")
        w()
        w("> 예: `RU` 의 동의어에 `DRU` 가, `DRU` 의 동의어에 `RU` 가 들어 있으면")
        w("> 두 용어가 서로를 삼킵니다. 같은 개념이면 하나로 합치고, 다른 개념이면")
        w("> 상호 동의어 등록을 지우세요.")
    w()

    # 용어가 다른 용어의 동의어로도 등록된 경우
    dup_as_syn = [
        (f, canon)
        for canon, forms in sr._expand_map.items()
        for f in forms
        if f != canon and f in sr._expand_map
    ]
    if dup_as_syn:
        w("### 용어가 다른 용어의 동의어로도 등록됨")
        w()
        w("코드는 **용어 자신을 우선**하도록 처리했지만, 의도한 관계인지 확인이 필요합니다.")
        w()
        w("| 용어 | ~의 동의어로도 등록됨 |")
        w("|---|---|")
        for f, canon in sorted(set(dup_as_syn)):
            w(f"| `{f}` | `{canon}` |")
        w()

    # ── ② 표기 통일 ─────────────────────────────────────────────────────
    groups: dict = defaultdict(list)
    for lbl, name, deg in nodes:
        groups[sr.norm_key(name)].append((name, lbl, deg))
    # 데이터 자산은 뺍니다. ⑤ 에서 "등록하지 마세요" 라고 해놓고 여기서
    # "동의어로 등록하세요" 라고 하면 지시가 충돌합니다. 표기가 갈린 테이블은
    # 조회 단계에서 이미 합쳐지므로 검색에는 문제가 없습니다.
    spelling = {
        k: v
        for k, v in groups.items()
        if k and len({n for n, _l, _d in v}) > 1 and not any(is_data_asset(n) for n, _l, _d in v)
    }
    w("## ② 표기 통일 — 동의어로 등록하면 그래프가 합쳐집니다")
    w()
    if not spelling:
        w("없음.")
    else:
        w("현재는 조회 단계에서 합치고 있어 검색 결과는 정상입니다. 용어집에 등록하면")
        w("인제스트 시점에 하나로 저장되어 그래프 자체가 깨끗해집니다.")
        w()
        w("| 대표(제안) | 동의어로 등록할 표기 | 엣지 |")
        w("|---|---|---|")
        for _k, v in sorted(spelling.items(), key=lambda x: -sum(d for *_, d in x[1])):
            by_name: dict = defaultdict(int)
            for n, _l, d in v:
                by_name[n] += d
            ordered = sorted(by_name, key=lambda n: (-by_name[n], len(n), n))
            head, rest = ordered[0], ordered[1:]
            w(f"| `{head}` | {', '.join(f'`{r}`' for r in rest)} | {sum(by_name.values())} |")
    w()

    # ── ②-b 조직 약칭 후보 ──────────────────────────────────────────────
    # "데브옵스" 와 "데브옵스팀" 은 표기 차이가 아니라 다른 이름이라 조회 단계가
    # 합치지 못합니다. 사람이 같은 것이라고 알려줘야 합니다.
    #
    # 이름 포함 관계는 오탐이 압도적입니다 — 전체 노드로 보면 507건인데 "로그" ⊂
    # "프로그램팀" 같은 우연이 대부분입니다. 그래서 두 가지로 좁힙니다:
    #   · 조직 라벨(:Team/:Role)끼리만
    #   · **접두** 일치만 (뒤에 붙는 경우만 약칭으로 봅니다)
    # 그래도 "기획팀" ⊂ "RESU 기획팀" 같은 건 다른 조직일 수 있으므로, 이건
    # 접미라 걸리지 않습니다. 판단은 담당자 몫입니다.
    org_names = sorted({(n, d) for lbl, n, d in nodes if lbl in _ORG_LABELS})
    abbrev = [
        (a, da, b, db)
        for a, da in org_names
        for b, db in org_names
        if a != b
        and len(sr.norm_key(a)) >= 2
        and sr.norm_key(b).startswith(sr.norm_key(a))
        and sr.norm_key(a) != sr.norm_key(b)
        # 게임 이름은 조직의 약칭이 아닙니다 — "RESU" 는 "RESU 기획팀" 의
        # 약칭이 아니라 그 팀이 담당하는 게임입니다. 용어집이 이미 알고 있으므로
        # 물어볼 필요가 없습니다.
        and sr.category_of(a) != "game"
    ]
    w("## ②-b 조직 약칭 후보 — 담당자 판단 필요")
    w()
    if not abbrev:
        w("없음.")
    else:
        w("표기 차이가 아니라 **다른 이름**이라 조회 단계가 합치지 못합니다. 같은")
        w("조직이면 짧은 쪽을 동의어로 등록하세요. 다른 조직이면 그대로 두면 됩니다.")
        w()
        w("| 짧은 이름 | 엣지 | 긴 이름 | 엣지 |")
        w("|---|---|---|---|")
        for a, da, b, db in sorted(set(abbrev), key=lambda x: -(x[1] + x[3]))[: args.top]:
            w(f"| `{a}` | {da} | `{b}` | {db} |")
    w()

    # ── 미등록 노드 분류 ────────────────────────────────────────────────
    by_name_deg: dict = defaultdict(int)
    by_name_lbl: dict = defaultdict(set)
    for lbl, name, deg in nodes:
        by_name_deg[name] += deg
        by_name_lbl[name].add(lbl)

    unreg = [
        (n, d, by_name_lbl[n])
        for n, d in by_name_deg.items()
        if d >= args.min_edges and sr.resolve(n) == n and not sr.category_of(n)
    ]
    unreg.sort(key=lambda x: (-x[1], x[0]))

    orgs = [u for u in unreg if u[2] & _ORG_LABELS and not is_data_asset(u[0])]
    people = [u for u in unreg if u[2] & _PERSON_LABELS]
    assets = [u for u in unreg if is_data_asset(u[0])]
    seen = {id(x) for x in orgs + people + assets}
    terms = [u for u in unreg if id(u) not in seen]

    def table(title: str, items: list, note: str = "") -> None:
        w(f"## {title} ({len(items)}건)")
        w()
        if note:
            w(note)
            w()
        if not items:
            w("없음.")
            w()
            return
        w("| 이름 | 라벨 | 엣지 |")
        w("|---|---|---|")
        for n, d, lbls in items[: args.top]:
            w(f"| `{n}` | {'/'.join(sorted(lbls))} | {d} |")
        if len(items) > args.top:
            w(f"| … | | *{len(items) - args.top}건 더* |")
        w()

    table(
        "③ 미등록 조직·팀",
        orgs,
        "`category=organization` 으로 등록하면 이벤트 주체 판정(`classify_scope`)이 "
        "정확해집니다. 현재 `organization` 카테고리가 비어 있어 조직 판정을 그래프 "
        "`:Team` 노드로 대체하고 있습니다.",
    )
    table(
        "④ 미등록 업무 용어",
        terms,
        "엣지가 많은데 용어집에 없는 것들입니다. 약칭·별칭이 함께 쓰이는 용어라면 "
        "등록 가치가 큽니다.",
    )
    table(
        "⑤ 데이터 자산 — 용어집 대상 아님 (참고)",
        assets,
        "테이블·뷰·경로입니다. 업무 용어집이 아니라 데이터 카탈로그에서 다룰 대상이라 "
        "**등록하지 마세요.** 한 토큰 차이가 곧 다른 객체라 동의어로 묶으면 위험합니다 "
        "(`payment_detail_view` vs `payment_raw_detail_view`).",
    )
    w(f"## ⑥ 인물 — 용어집 대상 아님 ({len(people)}건)")
    w()
    w("사람 이름은 업무 용어가 아니므로 등록하지 마세요. 다만 본문 추출이 이름을")
    w("줄여 쓰는 경우가 있어 같은 사람이 두 노드로 갈립니다 — 아래 참고.")
    w()

    # 이름이 줄여 쓰인 것으로 보이는 인물
    pnames = sorted({n for lbl, n, _d in nodes if lbl in _PERSON_LABELS})
    short = [
        (a, b)
        for a in pnames
        for b in pnames
        if a != b and len(a) >= 2 and b.endswith(a) and len(b) - len(a) <= 2
    ]
    if short:
        w("### 축약된 인물 이름으로 보이는 쌍")
        w()
        w("| 축약형 | 전체형(추정) | 엣지(축약/전체) |")
        w("|---|---|---|")
        for a, b in sorted(set(short)):
            w(f"| `{a}` | `{b}` | {by_name_deg[a]} / {by_name_deg[b]} |")
        w()
        w("> 자동 병합하지 않습니다 — 동명이인이면 서로 다른 사람을 합치게 됩니다.")
        w()

    if args.out:
        Path(args.out).write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\n💾 저장: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
