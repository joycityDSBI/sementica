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

라이브 서버(`dh123io-ld01`, 계정 `devadmin`) 기준입니다 — 2026-09-15 이전 완료.

| 프로세스 | 관리 방식 | 포트 | 비고 |
|---|---|---|---|
| MCP 서버 | systemd `sementica-mcp@strategic` | 8765 | 템플릿 유닛 |
| REST API | systemd `sementica-rest` | 8766 | Snowflake UDF 가 호출, Bearer 인증 |
| Ops 대시보드 | systemd `sementica-ops` | 8080 | **127.0.0.1 바인딩** — 아래 참고 |
| FalkorDB Browser | docker `falkordb-browser` | 3000 | 그래프 웹 UI |
| Qdrant / FalkorDB / PostgreSQL | docker compose | 6333 / 6379 / 5433 | |

> ⚠️ **6379 는 대시보드가 아닙니다.** Redis 프로토콜(RESP) 포트라 브라우저로
> 열면 연결이 끊깁니다. FalkorDB 웹 UI 는 **3000** 입니다.

## Ops 대시보드에는 인증이 없습니다

`/api/batch/run` 의 `confirm` 필드는 **오조작 방지지 인증이 아닙니다** — 소스를
본 사람은 `confirm == type` 을 그대로 보내면 그래프를 지울 수 있습니다.
그래서 기본 바인딩이 루프백입니다.

바인딩은 `.env` 의 `OPS_HOST` 로 정합니다 (유닛에 박혀 있지 않습니다):

| 설정 | 접근 방법 |
|---|---|
| (미설정) → `127.0.0.1` | SSH 터널 |
| `OPS_HOST=0.0.0.0` | 브라우저 직접 — **방화벽이 유일한 방어선** |

```bash
# 터널로 쓸 때 (FalkorDB Browser 3000 도 함께)
ssh -N -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 -p 50022 devadmin@<서버IP>

# 직접 열 때
echo 'OPS_HOST=0.0.0.0' >> .env
sudo systemctl restart sementica-ops
```

직접 열면 기동 로그에 경고가 남습니다. 나중에 방화벽 범위가 넓어져도 서버
쪽에 아무 흔적이 없으면 **열려 있다는 사실 자체를 잊게 되기** 때문입니다.

```
🌐  Semantica Ops Dashboard — http://0.0.0.0:8080
   ⚠️  0.0.0.0 로 바인딩 — 이 대시보드는 인증이 없습니다.
```

제대로 인증을 붙이려면 nginx 뒤에 Basic 인증으로 두는 쪽이 낫습니다. 단
**TLS 가 먼저 있어야 합니다** — Basic 인증은 자격증명을 base64 로 보낼 뿐
암호화하지 않아, 평문 HTTP 에 붙이면 비밀번호가 그대로 지나갑니다.

## 기동 확인 — `active (running)` 을 믿지 마세요

기동 직후 죽는 프로세스는 `Restart=` 가 다시 띄우므로 `systemctl status` 가
**늘 "방금 시작됨"** 으로 보입니다. 2026-09-15 에 Ops 대시보드가 정확히 그
상태였습니다 — `active (running) since ... 103ms ago` 인데 실제로는 의존성이
없어 안내만 찍고 죽기를 반복하고 있었습니다.

세 가지를 함께 보세요:

```bash
systemctl status sementica-ops --no-pager -l | head -20   # Scheduled restart 줄이 있는가
curl -s localhost:8080/api/depts; echo                     # {"depts":["strategic"]}
curl -s localhost:8766/rest/health; echo                   # {"status":"ok",...}
ss -tlnp | grep -E ':(8765|8766|8080)'                     # 실제로 LISTEN 하는가
```

MCP 는 `streamable-http` 라 health 엔드포인트가 없습니다. 8765 가 LISTEN 되고
`Application startup complete` 가 찍히면 정상입니다.

## 겪은 문제들 (해결됨)

**의존성이 선언되지 않았습니다** (2026-09-15). `fastapi`·`pydantic`·
`starlette`·`uvicorn` 이 `requirements.txt` 에 처음부터 없었습니다. 구 서버에는
손으로 깔려 있어 드러나지 않다가, 새 `.venv` 에서 Ops 가 재시작 루프에
빠졌습니다. `python tools/check_deps.py` 가 같은 종류를 미리 잡습니다.

**REST API 가 systemd 밖에 있었습니다** (~2026-09-14). `nohup` 으로만 떠 있어
셸이 닫히면 죽고 재부팅 후 안 올라왔습니다. Snowflake UDF 가 호출하므로 죽으면
**UDF 쪽에서만** 에러가 나고 서버에는 흔적이 남지 않습니다.

**포트를 잡고 있는 유령 프로세스.** `nohup ... &` 프로세스는 셸 job 이 끊겨도
(`[1]- Terminated`) 살아남습니다. 2026-09-11 에 pyenv 프로세스가 8766 을 계속
잡고 있어 새 systemd 서비스가 `[Errno 98] address already in use` 로 10초마다
재시작을 반복했습니다. systemd 로 옮긴 지금도 수동 실행 뒤에는 확인하세요:

```bash
sudo systemctl stop sementica-rest    # 먼저 재시작을 끈다 (안 그러면 경합)
sudo ss -lptn 'sport = :8766'         # 누가 잡고 있는지 확인
kill <PID>
sleep 2 && sudo ss -lptn 'sport = :8766'   # 비었는지 확인
sudo systemctl start sementica-rest
```

**인터프리터가 서로 달랐습니다.** MCP 는 `.venv`, REST 는 pyenv 3.11.9 로
돌았습니다. 서로 다른 site-packages 를 보므로 anthropic 버전 차이나 `mcp` 패키지
섀도잉 같은 문제가 한쪽에만 나타납니다 — 고쳐놓고 다 고쳤다고 믿기 딱 좋은
구조입니다. 세 유닛 모두 `.venv` 를 씁니다.

**유닛 이름이 레포와 달랐습니다.** 구 서버에는 고정 이름
`sementica-mcp.service` 가 설치돼 있었고 레포에는 템플릿만 있었습니다. 라이브
서버는 템플릿(`sementica-mcp@strategic`)으로 통일했으므로 레포와 일치합니다.

## 레포 작업은 반드시 `devadmin` 으로

root 로 `git pull` 을 돌리면 새 객체가 root 소유로 생깁니다. 그다음부터
devadmin 의 pull 이 이렇게 실패합니다:

```
error: insufficient permission for adding an object to repository database .git/objects
fatal: failed to write object
```

**root 가 필요한 것은 두 가지뿐입니다** — `systemctl` 과 `/etc` 아래 설치.
나머지(`git`·`pip`·`python`)는 전부 devadmin 으로 하세요.

```bash
# devadmin 셸에서
cd ~/sementica && git pull
.venv/bin/pip install -r requirements.txt

# root 가 필요할 때만
sudo bash deploy/install.sh ops
sudo systemctl restart sementica-ops
```

### root 셸에 머물러 있는지 확인

PuTTY 로 devadmin 으로 접속해도 `sudo su` / `sudo -i` 를 한 번 치면 그 셸은
계속 root 입니다. `sudo su`(대시 없음)는 작업 디렉터리를 유지해서 프롬프트만
`root@` 로 바뀌므로 알아채기 어렵습니다.

```bash
whoami           # root 면 올라와 있는 것
echo $SUDO_USER  # devadmin 이면 sudo 로 올라온 상태
exit             # devadmin 으로 복귀
```

### 이미 오염됐다면

```bash
sudo chown -R devadmin:devadmin /home/devadmin/sementica
find /home/devadmin/sementica -not -uid "$(id -u devadmin)" | head   # 비어야 정상
```

`install.sh` 가 이 검사를 먼저 돌립니다. 레포가 오염된 상태로 설치하면
**`User=root` 짜리 유닛이 조용히 써지기** 때문입니다 — 설치 스크립트는 레포
디렉터리의 소유자를 서비스 계정으로 삼습니다.

## HTTPS 노출 (nginx)

Snowflake UDF 가 공개 인터넷에서 REST API 를 호출합니다. **ngrok 은 2026-09-15
에 제거했습니다** — 재시작마다 URL 이 바뀌어 Snowflake 쪽 네트워크 규칙과 UDF
3개 + 프로시저 1개를 매번 다시 만들어야 했습니다. 고정 도메인 + nginx 로
대체합니다.

```
[인터넷] ──443──> [VM nginx] ──> 127.0.0.1:8766 (sementica-rest)
```

### 순서를 지켜야 합니다

**인증서 파일이 없으면 nginx 가 기동에 실패합니다.** 그래서 설정 파일은 443
블록을 주석 상태로 배포하고, 인증서를 받은 뒤 푸는 2단계입니다.

```bash
# ① 도메인 발급 + DNS A 레코드 → 이 서버의 공인 IP
# ② 방화벽 80 개방 (HTTP-01 검증용. DNS-01 이면 생략 가능)

# ③ 설정 배포 — 이 시점엔 80 번만 뜹니다
sudo apt install nginx
sudo bash deploy/install.sh --domain semantica.example.com nginx
sudo nginx -t && sudo systemctl reload nginx

# ④ 인증서 발급
sudo certbot certonly --webroot -w /var/www/html -d semantica.example.com
sudo ls -l /etc/letsencrypt/live/semantica.example.com/fullchain.pem

# ⑤ 443 블록 주석 해제 후 반영
sudo nginx -t && sudo systemctl reload nginx

# ⑥ 방화벽 443 개방 / 8766 인바운드 차단
```

### 두 가지 전제

**공인 CA 인증서여야 합니다.** Snowflake 는 표준 TLS 검증을 하므로 사내 CA 나
자체 서명 인증서는 신뢰하지 않고, UDF 런타임에 CA 를 추가할 방법도 없습니다.

**공인 접근 경로가 필요합니다.** VM 은 사설 IP(`10.123.20.3`)입니다 — ngrok 이
해주던 일이 바로 이것이었습니다. 공인 IP 부여, GCP HTTPS LB, 또는 사내 리버스
프록시 중 하나를 먼저 정해야 인증서 검증부터 통과합니다.

REST 유닛은 이제 `--host 127.0.0.1` 입니다. **8766 인바운드를 방화벽에서
닫으세요** — nginx 를 세워도 8766 이 외부에 열려 있으면 평문 HTTP 우회로가
남고 Bearer 토큰이 그대로 지나갑니다.

```bash
ss -tlnp | grep :8766      # 127.0.0.1:8766 이어야 합니다
```

**Snowflake 쪽**은 호스트를 두 곳에만 둡니다 (`01_network_access.sql`):

| 위치 | 바꿀 때 |
|---|---|
| `NETWORK RULE` 의 `VALUE_LIST` | `CREATE OR REPLACE NETWORK RULE ...` |
| `semantica_base_url` 시크릿 | `ALTER SECRET ... SET SECRET_STRING = '...'` |

UDF 본문에는 URL 이 없습니다 — 시크릿에서 읽으므로 **도메인이 바뀌어도 UDF 를
다시 만들 필요가 없습니다.**

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
