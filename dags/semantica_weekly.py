"""
매주: 용어집 스냅샷 갱신 → 검색 품질 회귀 점검(dev)

두 작업 모두 매일 돌 필요가 없습니다. 용어집은 사람이 등록할 때만 바뀌고,
평가는 문항당 LLM 을 여러 번 부르므로 45문항이면 비용이 적지 않습니다.

**holdout 은 여기에 없습니다.** 정기적으로 돌리면 그 순간 holdout 이 dev 가
되고, "우리가 보면서 고친 문항"과 "처음 보는 문항"을 구분할 수단이 사라집니다.
holdout 은 큰 변경 뒤에 사람이 한 번 돌리는 것입니다.

읽는 법: 이 평가의 해상도는 ±0.02 입니다. 같은 코드로 두 번 돌린 결과가
0.956 / 0.944 로 갈린 적이 있습니다(답변 생성 비결정성). 한 주 사이에 0.02
움직인 것은 신호가 아닙니다 — **0.05 이상 떨어졌을 때** 보세요.
"""

from __future__ import annotations

import pendulum
from airflow import DAG
from semantica_common import (
    DEFAULT_ARGS,
    DEPT,
    ROOT,
    eval_command,
    glossary_snapshot_command,
    run,
)

with DAG(
    dag_id="semantica_weekly",
    description="용어집 스냅샷 갱신 + 검색 품질 회귀 점검",
    default_args=DEFAULT_ARGS,
    schedule="0 5 * * 1",  # 월요일 05:00 — 일일 작업(02:00)과 겹치지 않게
    start_date=pendulum.datetime(2026, 9, 15, tz="Asia/Seoul"),
    catchup=False,
    max_active_runs=1,
    tags=["semantica", "weekly"],
) as dag:
    # 용어집 API 에 닿지 못하면 실패합니다. 그래도 **다음 작업을 막지 않습니다** —
    # 스냅샷이 갱신되지 않았을 뿐 기존 것으로 계속 동작하므로, 평가까지 멈출
    # 이유가 없습니다. (운영 VM 의 방화벽 문제는 semantica_common 주석 참고)
    glossary = run(
        "refresh_glossary_snapshot",
        glossary_snapshot_command(),
        retries=1,
        doc_md=(
            "용어집 API → config/glossary_snapshot.json. 실패해도 기존 스냅샷으로 계속 동작합니다."
        ),
    )

    # dev 골든셋만 — holdout 은 사람이 돌립니다 (모듈 docstring 참고).
    evaluate = run(
        "evaluate_dev",
        eval_command(f"{ROOT}/data/eval/golden_v2_dev.json"),
        retries=0,  # LLM 비용이 드는 작업이라 자동 재시도하지 않습니다
        trigger_rule="all_done",  # 용어집 갱신이 실패해도 평가는 진행
        doc_md=(
            f"dev 골든셋 45문항으로 {DEPT} 검색 품질을 점검합니다. "
            "결과는 eval_run_log 에 golden_hash 와 함께 남습니다. "
            "±0.02 는 잡음이므로 0.05 이상 하락했을 때만 조사하세요."
        ),
    )

    glossary >> evaluate
