# Airflow DAG

cron 에서 옮겨왔습니다. 옮긴 이유는 스케줄러 취향이 아니라 **순서** 입니다.

예전 구성은 `0 2 * * *` sync, `0 3 * * *` backup — "sync 가 1시간 안에 끝난다"는
**시계 기반 가정**입니다. 변경이 많은 날 sync 가 길어지면 백업이 동기화 중인
상태를 뜨고, Qdrant 와 FalkorDB 가 서로 다른 시점을 담은 백업이 만들어집니다.
둘 다 "성공"으로 끝나므로 알림으로도 잡히지 않습니다.

| DAG | 주기 | 내용 |
|---|---|---|
| `semantica_daily` | 매일 02:00 | Notion 동기화 → **완료 후** 백업 |
| `semantica_weekly` | 월 05:00 | 용어집 스냅샷 갱신 → dev 골든셋 평가 |

## 설계 — DAG 파일에 로직을 두지 않습니다

사내 「리포트자동화」 관례를 따릅니다. DAG 은 "무엇을 언제 어떤 순서로" 만
적고, 하는 일은 **기존 CLI 진입점**이 담당합니다.

```bash
# 로컬에서 그대로 확인 가능 — DAG 이 실행하는 것과 같은 명령입니다
.venv/bin/python src/pipeline/sync.py --dept strategic
bash scripts/backup.sh
```

새 로직을 DAG 에 넣지 마세요. 넣는 순간 Airflow 없이는 재현할 수 없게 되고,
장애가 났을 때 손으로 돌려볼 방법이 사라집니다.

## 배포

```bash
# DAG 파일을 Airflow 의 dags 폴더로
cp dags/semantica_*.py $AIRFLOW_HOME/dags/
```

Airflow Variable 네 개로 동작이 정해집니다:

| Variable | 기본값 | 설명 |
|---|---|---|
| `semantica_root` | `/home/seongin/sementica` | 프로젝트 경로 |
| `semantica_dept` | `strategic` | 대상 본부 |
| `semantica_exec_mode` | `local` | `local`(BashOperator) 또는 `ssh`(SSHOperator) |
| `semantica_ssh_conn_id` | `semantica_vm` | ssh 모드에서 쓸 Connection |

```bash
airflow variables set semantica_root /home/seongin/sementica
airflow variables set semantica_dept strategic
airflow variables set semantica_exec_mode ssh
```

### Airflow 가 어디서 도는가

**작업은 반드시 Semantica VM 에서 실행돼야 합니다.** Qdrant·FalkorDB 가 거기
있고, 스크립트가 `.env` 와 `data/` 를 상대 경로로 찾기 때문입니다.

- **Airflow 가 같은 VM 에 있다면** → `semantica_exec_mode=local`
- **회사 Airflow가 별도 서버라면** → `semantica_exec_mode=ssh` + Connection 등록

```bash
airflow connections add semantica_vm \
    --conn-type ssh --conn-host <VM_IP> --conn-login seongin \
    --conn-extra '{"key_file": "/path/to/key", "conn_timeout": 60}'
```

ssh 모드는 `apache-airflow-providers-ssh` 가 필요합니다.

## 왜 이렇게 했는가

**`max_active_runs=1`** — 두 sync 가 같은 그래프에 동시에 쓰면 상태가 깨집니다.
cron 에는 이 보호가 없어서, 앞 실행이 길어지면 그대로 겹쳤습니다.

**`catchup=False`** — sync 는 "지금 Notion 상태"를 가져오는 작업이라 과거 실행을
재현한다는 개념이 없습니다. 놓친 날을 몰아서 돌리면 같은 일을 여러 번 하면서
서로를 덮어쓸 뿐입니다.

**재시도 정책이 작업마다 다릅니다** — sync 는 `content_hash` 비교라 재시도해도
안전해서 2회, 백업은 1회, 평가는 **0회**입니다(문항당 LLM 을 여러 번 부르므로
자동 재시도가 그대로 비용입니다).

**Airflow 이메일 알림을 켜지 않았습니다** — `src/ops/notify.py` 가 이미 결과를
보내고, 거기에는 Airflow 가 모르는 수치(페이지·트리플·이벤트 수)가 들어 있습니다.
둘 다 켜면 실패 한 번에 메일이 두 통 오고 어느 쪽이 진짜인지 따져야 합니다.

## 일부러 넣지 않은 것

**holdout 평가** — 정기적으로 돌리면 그 순간 holdout 이 dev 가 되고, "우리가
보면서 고친 문항"과 "처음 보는 문항"을 구분할 수단이 사라집니다. 큰 변경 뒤에
**사람이 한 번** 돌리는 것입니다.

**골든셋 재생성** — 문항이 바뀌면 이전 회차와 총점 비교가 깨지고, 자동화하면
holdout 파일까지 덮어씁니다. 생성은 사람이 결정할 일입니다.

**`ingest.py --reset`** — 그래프와 벡터를 통째로 지우고 다시 만듭니다.
스케줄에 올릴 종류가 아닙니다.

## 알려진 제약

**용어집 스냅샷 갱신이 운영 VM 에서는 실패합니다.** VM 이
`catalog.joycityplay.com` 에 닿지 못합니다(방화벽, project_summary 48번).
실패해도 기존 스냅샷으로 계속 동작하므로 평가는 `trigger_rule="all_done"` 로
이어집니다.

회사 Airflow 워커가 용어집 API 에 닿는다면, 워커에서 받아 VM 으로 밀어 넣는
2단계로 나누는 것이 맞습니다. 그건 네트워크 구성을 확인한 뒤에 정하세요.

## 평가 결과 읽는 법

**해상도는 ±0.02 입니다.** 같은 코드로 두 번 돌린 결과가 0.956 / 0.944 로
갈린 적이 있습니다(답변 생성 비결정성). 한 주 사이 0.02 변동은 신호가 아닙니다.
**0.05 이상 떨어졌을 때** 보세요.

결과는 `eval_run_log` 에 `golden_hash` 와 함께 남습니다. 골든셋이 바뀌면 해시도
바뀌므로, **같은 해시끼리만** 비교하세요.
