"""
매일: Notion 동기화 → 백업

cron 에서 옮겨온 이유는 **순서** 입니다. 예전에는 2시 sync, 3시 backup 으로
시계에 기대고 있었는데, 그건 "sync 가 1시간 안에 끝난다"는 가정입니다.
변경이 많은 날 sync 가 길어지면 백업이 **동기화 중인 상태**를 뜨고, Qdrant 와
FalkorDB 가 서로 다른 시점을 담은 백업이 조용히 만들어집니다. 둘 다 "성공"으로
끝나므로 알림으로도 잡히지 않습니다.

여기서는 backup 이 sync 의 **완료**를 기다립니다.
"""

from __future__ import annotations

import pendulum
from airflow import DAG
from semantica_common import DEFAULT_ARGS, backup_command, run, sync_command

with DAG(
    dag_id="semantica_daily",
    description="Notion 증분 동기화 후 백업",
    default_args=DEFAULT_ARGS,
    schedule="0 2 * * *",
    start_date=pendulum.datetime(2026, 9, 15, tz="Asia/Seoul"),
    # 놓친 날짜를 몰아서 돌리지 않습니다. sync 는 "지금 Notion 상태"를 가져오는
    # 작업이라 과거 실행을 재현한다는 개념이 없습니다 — catchup 을 켜면 같은
    # 일을 여러 번 하면서 서로를 덮어쓸 뿐입니다.
    catchup=False,
    # **동시 실행 금지.** 두 sync 가 같은 그래프에 동시에 쓰면 상태가 깨집니다.
    # cron 에는 이 보호가 없었습니다 — 앞 실행이 길어지면 그대로 겹쳤습니다.
    max_active_runs=1,
    tags=["semantica", "daily"],
) as dag:
    # content_hash 비교라 중간에 끊겨도 다시 돌리면 됩니다.
    sync = run(
        "sync_notion",
        sync_command(),
        retries=2,
        doc_md="Notion 변경분을 Qdrant·FalkorDB 에 반영합니다. 재시도 안전.",
    )

    # 백업은 sync 가 끝난 **뒤에** — 이것이 Airflow 로 옮긴 이유입니다.
    backup = run(
        "backup",
        backup_command(),
        retries=1,
        doc_md=(
            "Qdrant·FalkorDB·PostgreSQL·Notion 캐시·설정 파일. "
            "실패한 단계가 있으면 종료 코드로 보고합니다."
        ),
    )

    sync >> backup
