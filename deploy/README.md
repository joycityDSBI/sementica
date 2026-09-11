# 배포 구성

운영 서버(`sementica`, 계정 `seongin`, `/home/seongin/sementica`)에서 도는 것들입니다.

## 지금 도는 것

| 프로세스 | 관리 방식 | 포트 | 비고 |
|---|---|---|---|
| MCP 서버 | systemd `sementica-mcp.service` | 8765 | `src/mcp/server.py --dept strategic` |
| REST API | **없음 — 수동 `nohup`** | 8766 | Snowflake UDF 가 호출 |
| Ops 대시보드 | systemd `sementica-ops.service` | 8080 | nginx 경유, 127.0.0.1 바인딩 |

## 정리해야 할 것

**REST API 가 systemd 밖에 있습니다.** `nohup python src/mcp/rest_api.py &` 로
떠 있어서 셸이 닫히면 같이 죽고, 재부팅 후에도 올라오지 않습니다. Snowflake UDF
가 이 API 를 호출하므로 죽으면 **UDF 쪽에서만** 에러가 나고 서버에는 흔적이
남지 않습니다. 2026-09-11 실제로 죽은 채로 방치됐습니다.

→ `sementica-rest.service` 를 만들어 두었습니다. 설치:

```bash
# .venv 에 의존성이 있는지 먼저 확인 (아래 "인터프리터" 참고)
cd ~/sementica && .venv/bin/python -c "import starlette, uvicorn; print('ok')"

sudo cp deploy/sementica-rest.service /etc/systemd/system/
sudo systemctl daemon-reload
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

## 주기 실행

`setup_cron.sh` 가 `sync.py` 를 crontab 에 등록합니다.

⚠️ 이 스크립트는 `crontab -l` 이 일시적으로 실패하면 기존 crontab 을 통째로
덮어쓸 수 있습니다. 실행 전에 백업하세요:

```bash
crontab -l > ~/crontab.bak
```
