# 배포 구성

## 설치 — `deploy/install.sh`

유닛 파일에는 `User=seongin` / `/home/seongin/sementica` 가 박혀 있습니다.
**그대로 복사하면 다른 계정의 서버에서 기동에 실패합니다** — 2026-09-15 라이브
서버(`devadmin`)로 옮기면서 실제로 그랬습니다. 손으로 고치면 다음 서버에서 또
같은 일이 반복되므로, 설치 시점에 레포 위치와 소유 계정을 읽어 채웁니다.

```bash
bash deploy/install.sh                        # 현재 설치 상태만 출력
bash deploy/install.sh --dry-run ops          # 설치될 내용 확인 (root 불필요)
sudo bash deploy/install.sh ops rest logrotate
sudo systemctl enable --now sementica-ops
```

계정은 `whoami` 가 아니라 **레포 디렉터리의 소유자**를 씁니다 — 이 스크립트는
`sudo` 로 도니까요. 이미 설치된 유닛과 내용이 다르면 diff 를 보여준 뒤 덮어씁니다.

## 지금 도는 것

| 프로세스 | 관리 방식 | 포트 | 비고 |
|---|---|---|---|
| MCP 서버 | systemd `sementica-mcp.service` | 8765 | `src/mcp/server.py --dept strategic` |
| REST API | systemd `sementica-rest.service` | 8766 | Snowflake UDF 가 호출 |
| Ops 대시보드 | systemd `sementica-ops.service` | 8080 | **127.0.0.1 바인딩** — 아래 참고 |
| FalkorDB Browser | docker `falkordb-browser` | 3000 | 그래프 웹 UI |

> ⚠️ **6379 는 대시보드가 아닙니다.** Redis 프로토콜(RESP) 포트라 브라우저로
> 열면 연결이 끊깁니다. FalkorDB 웹 UI 는 **3000** 입니다.

## Ops 대시보드에는 인증이 없습니다

유닛이 `--host 127.0.0.1` 로 강제하는 이유입니다. `web_app.py` 자체 기본값은
`0.0.0.0` 이지만 그대로 노출하면 **접근 가능한 누구나 그래프를 지울 수
있습니다** — `/api/batch/run` 의 `confirm` 필드는 오조작 방지지 인증이 아닙니다
(소스를 본 사람은 `confirm == type` 을 그대로 보내면 됩니다).

접속은 SSH 터널로 하세요:

```bash
ssh -N -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 -p 50022 devadmin@<서버IP>
```

사내망에 직접 열어야 한다면 nginx 앞단에 인증을 붙이고 방화벽을 개발자 IP 로
제한하세요. `--host` 만 바꾸는 것으로는 무인증 파괴 엔드포인트가 그대로 열립니다.

## 정리해야 할 것

**REST API 가 systemd 밖에 있습니다.** `nohup python src/mcp/rest_api.py &` 로
떠 있어서 셸이 닫히면 같이 죽고, 재부팅 후에도 올라오지 않습니다. Snowflake UDF
가 이 API 를 호출하므로 죽으면 **UDF 쪽에서만** 에러가 나고 서버에는 흔적이
남지 않습니다. 2026-09-11 실제로 죽은 채로 방치됐습니다.

→ `sementica-rest.service` 를 만들어 두었습니다. 설치:

```bash
# .venv 에 의존성이 있는지 먼저 확인 (아래 "인터프리터" 참고)
cd ~/sementica && .venv/bin/python -c "import starlette, uvicorn; print('ok')"

sudo bash deploy/install.sh rest
sudo systemctl enable --now sementica-rest
systemctl status sementica-rest --no-pager
curl -s localhost:8766/rest/health; echo
```

**포트를 잡고 있는 유령 프로세스에 주의하세요.** `nohup ... &` 로 띄운 프로세스는
셸의 job 이 끊겨도(`[1]- Terminated` 메시지) 살아남을 수 있습니다. 실제로
2026-09-11 에 09:21 에 뜬 pyenv 프로세스가 8766 을 계속 잡고 있어, 새로 등록한
systemd 서비스가 바인딩에 실패하며 10초마다 재시작을 반복했습니다
(`[Errno 98] address already in use`, exit 3). 정리 순서:

```bash
sudo systemctl stop sementica-rest    # 먼저 재시작을 끈다 (안 그러면 경합)
sudo ss -lptn 'sport = :8766'         # 누가 잡고 있는지 확인
kill <PID>
sleep 2 && sudo ss -lptn 'sport = :8766'   # 비었는지 확인
sudo systemctl start sementica-rest
```

**인터프리터가 서로 다릅니다.** MCP 는 `.venv/bin/python`, REST 는
`~/.pyenv/versions/3.11.9/bin/python` 으로 돌고 있었습니다. 두 프로세스가 다른
site-packages 를 보므로, anthropic 버전 차이나 `mcp` 패키지 섀도잉 같은 문제가
한쪽에만 나타납니다 — 고쳐놓고 다 고쳤다고 믿기 딱 좋은 구조입니다.
`sementica-rest.service` 는 `.venv` 를 씁니다. 의존성이 없으면 기동에 실패하니
위 확인 명령을 먼저 돌리고, 없으면 설치하세요:

```bash
.venv/bin/pip install -r requirements.txt
```

**유닛 이름이 레포와 다릅니다.** 레포에는 템플릿 `sementica-mcp@.service` 가
있는데 실제로는 고정 이름 `sementica-mcp.service` 가 설치돼 있습니다. 실제
배포본을 확인하려면:

```bash
systemctl cat sementica-mcp
```

내용이 레포와 다르면 레포 쪽을 맞춰주세요. 유닛 파일이 현실과 어긋나 있으면
다음에 서버를 새로 세울 때 조용히 틀립니다.

## ngrok

Snowflake 가 외부에서 REST API 에 접근하려면 HTTPS 터널이 필요합니다.
`scripts/start_with_ngrok.sh` 가 REST + ngrok 을 함께 띄웁니다. REST 를 systemd
로 옮긴 뒤에는 이 스크립트의 REST 기동 부분이 중복되므로, ngrok 만 따로 띄우는
쪽이 맞습니다.

ngrok URL 은 재시작마다 바뀌므로 `snowflake/01_network_access.sql` 의
`ALLOWED_NETWORK_RULES` 도 함께 갱신해야 합니다.

## 백업

`scripts/backup.sh` 하나가 전부를 담당합니다 (2026-09-15 에 `backup_to_gcs.sh`
를 합쳤습니다).

| 대상 | 비고 |
|---|---|
| Qdrant | 컬렉션 스냅샷 |
| FalkorDB | dump.rdb |
| PostgreSQL | 운영 로그 덤프 |
| Notion 페이지 캐시 | 재수집보다 빠르고, **그때의 Notion 상태**를 재현합니다 |
| 설정 파일 | systemd 유닛·crontab·departments.yaml·requirements·용어집 스냅샷 |

```bash
bash scripts/backup.sh              # 전체
bash scripts/backup.sh --files-only # 캐시 + 설정만
bash scripts/backup.sh --restore    # 복구 절차 출력
```

**합치기 전에는 무엇이 빠지고 있었나** — 백업 스크립트가 둘이었고 cron 은
`backup.sh` 만 돌렸습니다. 그래서 PostgreSQL 은 백업됐지만 **Notion 캐시와
systemd 유닛은 백업되지 않았습니다.** 유닛 파일이 레포와 어긋나 있을 수 있는
상황이라(위 참고) 서버가 날아가면 복원할 정본이 없었습니다.

`.env` 는 **값을 빼고 키 목록만** 남깁니다. 백업에 자격증명을 넣으면 백업
자체가 유출 경로가 됩니다. 복구할 때는 `env_keys_only.txt` 로 무엇이 필요한지
확인하고 각 자격증명은 원래 발급처에서 다시 받으세요.

## 로그 로테이션

cron 로그(`data/logs/*.log`)는 로테이션이 없으면 무한히 커집니다.

```bash
sudo bash deploy/install.sh logrotate
sudo logrotate -d /etc/logrotate.d/sementica    # 점검 (회전 없음)
```

systemd 로 옮긴 MCP·REST·Ops 는 **journald 가 관리하므로 대상이 아닙니다.**
journald 용량이 걱정되면 `/etc/systemd/journald.conf` 의 `SystemMaxUse=` 를
보세요 — logrotate 와는 다른 체계입니다.

`install.sh` 가 경로와 `create 0640 <계정> <계정>` 를 함께 치환하므로 다른
계정에 설치해도 손댈 것이 없습니다.

## 주기 실행

`setup_cron.sh` 가 `sync.py` 를 crontab 에 등록합니다.

⚠️ 이 스크립트는 `crontab -l` 이 일시적으로 실패하면 기존 crontab 을 통째로
덮어쓸 수 있습니다. 실행 전에 백업하세요:

```bash
crontab -l > ~/crontab.bak
```
