"""
Semantica DAG 공통 설정 — **DAG 파일에는 로직을 두지 않습니다**

사내 「리포트자동화」 관례를 따릅니다. DAG 파일은 "무엇을 언제 어떤 순서로"
만 적고, 실제 하는 일은 전부 기존 CLI 진입점이 담당합니다. 그래야 같은 코드가
로컬 CLI 로도, DAG 으로도, 수동 실행으로도 똑같이 동작합니다.

    로컬 확인:  .venv/bin/python src/pipeline/sync.py --dept strategic
    Airflow:    같은 명령을 BashOperator/SSHOperator 가 실행

Airflow 가 **어디서 도는가** 에 따라 실행 방식이 갈립니다:

  local  Airflow 가 Semantica VM 위에서 돕니다 → BashOperator
  ssh    회사 Airflow 가 별도 서버에서 돕니다 → SSHOperator 로 VM 에 접속
         (Qdrant·FalkorDB 가 VM 에 있으므로 작업은 반드시 VM 에서 실행돼야 합니다)

Airflow Variable 로 고릅니다:
    semantica_exec_mode   = local | ssh          (기본 local)
    semantica_ssh_conn_id = semantica_vm         (ssh 모드에서 쓸 Connection)
    semantica_root        = /home/seongin/sementica
    semantica_dept        = strategic
"""

from __future__ import annotations

from datetime import timedelta

from airflow.models import Variable

# ── 경로·대상 ────────────────────────────────────────────────────────────────
ROOT = Variable.get("semantica_root", default_var="/home/seongin/sementica")
DEPT = Variable.get("semantica_dept", default_var="strategic")
VENV = f"{ROOT}/.venv/bin/python"

EXEC_MODE = Variable.get("semantica_exec_mode", default_var="local").strip().lower()
SSH_CONN_ID = Variable.get("semantica_ssh_conn_id", default_var="semantica_vm")

# ── 기본 인자 ────────────────────────────────────────────────────────────────
# 이메일 알림은 **켜지 않습니다.** Semantica 쪽(src/ops/notify.py)이 이미 작업
# 결과를 보내고, 거기에는 Airflow 가 모르는 수치(페이지·트리플·이벤트 수)가
# 들어 있습니다. 양쪽을 다 켜면 실패 한 번에 메일이 두 통 오고, 둘 중 어느
# 것이 진짜인지 판단해야 합니다.
DEFAULT_ARGS = {
    "owner": "data-science",
    "email_on_failure": False,
    "email_on_retry": False,
    "retry_delay": timedelta(minutes=10),
    "execution_timeout": timedelta(hours=3),
}


def job(name: str) -> str:
    """작업 이름 하나로 VM 에서 실행할 명령을 만듭니다.

    명령 문자열을 DAG 쪽에서 조립하지 않는 이유가 둘입니다.

    ① **작업 정의가 한곳에** 있어야 합니다. 명령이 DAG 에 흩어져 있으면
       스크립트 경로나 인자가 바뀌었을 때 DAG 이 옛 명령을 계속 씁니다.

    ② **SSH 강제 명령**과 맞물립니다. Airflow 서버에서 이 VM 으로 SSH 를
       열 때, 그 키를 `run_job.sh` 에 묶어두면 **허용된 작업만** 실행됩니다.
       방화벽으로 접속 주소를 좁히는 것과 별개로, 접속한 뒤에 무엇을 할 수
       있는지도 좁혀야 합니다 — Airflow 서버가 털렸을 때 그 키로 VM 에서
       임의 명령이 돌아가는 상황을 막습니다.

    >>> job("sync")
    '/home/seongin/sementica/scripts/run_job.sh sync'
    """
    return f"{ROOT}/scripts/run_job.sh {name}"


def run(task_id: str, command: str, **kwargs):
    """실행 모드에 맞는 오퍼레이터를 돌려줍니다.

    DAG 파일이 BashOperator / SSHOperator 를 직접 고르지 않게 감쌉니다 —
    배포 위치가 바뀌어도 DAG 은 그대로입니다.
    """
    if EXEC_MODE == "ssh":
        from airflow.providers.ssh.operators.ssh import SSHOperator

        return SSHOperator(
            task_id=task_id,
            ssh_conn_id=SSH_CONN_ID,
            command=command,
            # 긴 작업 중 연결이 끊기면 Airflow 는 실패로 보지만 VM 에서는
            # 계속 돌 수 있습니다. keepalive 로 그 상황을 줄입니다.
            conn_timeout=60,
            cmd_timeout=int(timedelta(hours=3).total_seconds()),
            get_pty=True,
            **kwargs,
        )

    from airflow.operators.bash import BashOperator

    return BashOperator(task_id=task_id, bash_command=command, **kwargs)


# ── 작업별 명령 ──────────────────────────────────────────────────────────────
# 전부 기존 CLI 입니다. 여기서 새로 만드는 동작은 없습니다.


def sync_command() -> str:
    """Notion 증분 동기화. content_hash 비교라 **재시도해도 안전합니다.**"""
    return job("sync")


def backup_command() -> str:
    """Qdrant·FalkorDB·PostgreSQL·Notion 캐시·설정 파일."""
    return job("backup")


def glossary_snapshot_command() -> str:
    """용어집 스냅샷 갱신.

    ⚠️ 운영 VM 은 catalog.joycityplay.com 에 접근하지 못합니다(방화벽).
    VM 에서 돌리면 실패하고, 그러면 기존 스냅샷이 그대로 쓰입니다(동작은 계속).

    회사 Airflow 워커가 API 에 닿는다면 워커에서 받아 VM 으로 밀어 넣는
    2단계로 나누는 쪽이 맞습니다 — 네트워크 구성을 확인한 뒤에 정하세요.
    """
    return job("glossary")


def eval_command() -> str:
    """dev 골든셋 평가.

    ⚠️ **holdout 은 실행할 수 없습니다** — run_job.sh 의 허용 목록에 없습니다.
    정기적으로 돌리면 그 순간 holdout 이 dev 가 되고, 일반화를 잴 수단이
    사라집니다. 사람이 큰 변경 뒤에 한 번 돌리는 것입니다.
    """
    return job("eval-dev")
