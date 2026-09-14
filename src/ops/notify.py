"""
작업 결과 알림 — 실패를 보러 가지 않아도 알 수 있게
─────────────────────────────────────────────────────────────────────────────
2026-09-14 인제스트가 `partial` 로 끝났는데, 진단 도구를 돌리기 전까지 아무도
몰랐습니다. `ingest_log` · `eval_run_log` · 웹 대시보드까지 있는데도 **보러
가야만 알 수 있는** 구조였기 때문입니다. 매일 새벽 2시 `sync.py` 와 3시 백업도
마찬가지로, 실패하면 로그 파일에만 남고 끝입니다.

**성공해도 보냅니다.** 실패할 때만 보내면 "메일이 안 왔다" 가 두 가지를 뜻하게
됩니다 — 잘 돌았거나, cron 이 아예 안 돌았거나. 후자가 더 위험한데 구분할 수
없습니다. 매번 보내면 침묵 자체가 신호가 됩니다.

설정 (.env):
    ALERT_EMAIL_TO=seongin@joycity.com
    SMTP_HOST=smtp.example.com
    SMTP_PORT=587
    SMTP_USER=...                 # 생략 시 인증 없이 전송 시도 (사내 릴레이)
    SMTP_PASSWORD=...
    SMTP_FROM=semantica@joycity.com   # 생략 시 SMTP_USER, 그것도 없으면 호스트명
    SMTP_TLS=1                    # STARTTLS. 465 포트면 SMTP_SSL=1
    ALERT_ON_SUCCESS=1            # 0 이면 실패에만 보냅니다

설정이 없으면 **조용히 넘어갑니다.** 알림은 보조 기능이고, 이것 때문에
인제스트가 멈추면 안 됩니다.

확인:
    python src/ops/notify.py --test
"""

import os
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

# ─── .env 로드 ────────────────────────────────────────────────────────────────
# 이 모듈은 **단독으로도 실행됩니다** — `notify.py --test` 와 backup.sh 가
# 서브프로세스로 부르는 경로가 그렇습니다. ingest.py·sync.py 가 import 할 때는
# 그쪽이 이미 .env 를 올려둬서 동작하지만, 단독 실행에서는 아무도 올려주지
# 않아 "설정이 부족합니다" 만 나왔습니다.
#
# 코드는 다른 진입점들과 **글자 그대로 같습니다**(setdefault, 따옴표 미제거).
# 여기서만 다르게 처리하면 같은 .env 가 프로세스마다 다르게 읽힙니다.
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# SMTP 가 응답하지 않을 때 인제스트가 매달리지 않도록 짧게 끊습니다.
SMTP_TIMEOUT: float = float(os.environ.get("SMTP_TIMEOUT", "10"))
SUBJECT_PREFIX: str = os.environ.get("ALERT_SUBJECT_PREFIX", "[Semantica]")


def _cfg() -> dict:
    return {
        "to": [a.strip() for a in os.environ.get("ALERT_EMAIL_TO", "").split(",") if a.strip()],
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "user": os.environ.get("SMTP_USER", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from": os.environ.get("SMTP_FROM", "").strip(),
        "tls": os.environ.get("SMTP_TLS", "1").strip().lower() in {"1", "true", "yes"},
        "ssl": os.environ.get("SMTP_SSL", "0").strip().lower() in {"1", "true", "yes"},
        "on_success": os.environ.get("ALERT_ON_SUCCESS", "1").strip().lower()
        in {"1", "true", "yes"},
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["to"] and c["host"])


def send_mail(subject: str, body: str) -> bool:
    """메일 한 통. 성공하면 True. **예외를 밖으로 내보내지 않습니다.**"""
    c = _cfg()
    if not c["to"] or not c["host"]:
        return False

    msg = EmailMessage()
    msg["Subject"] = f"{SUBJECT_PREFIX} {subject}" if SUBJECT_PREFIX else subject
    msg["From"] = c["from"] or c["user"] or f"semantica@{socket.gethostname()}"
    msg["To"] = ", ".join(c["to"])
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    try:
        if c["ssl"]:
            server = smtplib.SMTP_SSL(
                c["host"], c["port"], timeout=SMTP_TIMEOUT, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(c["host"], c["port"], timeout=SMTP_TIMEOUT)
        with server:
            if c["tls"] and not c["ssl"]:
                server.starttls(context=ssl.create_default_context())
            if c["user"]:
                server.login(c["user"], c["password"])
            server.send_message(msg)
        return True
    except Exception as exc:
        # 알림 실패로 파이프라인을 멈추지 않습니다. 다만 조용히 삼키면
        # "메일이 안 오는데 왜 안 오는지" 를 알 수 없으므로 표준출력에 남깁니다.
        print(f"  ⚠️  알림 메일 전송 실패: {type(exc).__name__}: {exc}")
        return False


def _fmt(label: str, value) -> str:
    return f"  {label:<16} {value}"


def notify_job(
    job: str,
    dept: str,
    status: str,
    stats: dict | None = None,
    errors: list | None = None,
    duration_sec: int = 0,
    extra: str = "",
) -> bool:
    """작업 1회 결과를 메일로 보냅니다.

    Args:
        job:     "인제스트" / "동기화" / "백업" 처럼 사람이 읽을 이름
        status:  success | partial | failed | dry_run
        stats:   {라벨: 값} — 제목줄과 본문에 들어갑니다
        errors:  오류 문자열 목록. 앞 10건만 본문에 담습니다

    Returns:
        보냈으면 True. 설정이 없거나 성공 알림이 꺼져 있으면 False.
    """
    c = _cfg()
    ok = status in {"success", "dry_run"}
    if ok and not c["on_success"]:
        return False
    if not is_configured():
        return False

    mark = {"success": "✅", "dry_run": "🔍", "partial": "⚠️", "failed": "❌"}.get(status, "•")
    stats = stats or {}
    head = " / ".join(f"{k} {v}" for k, v in list(stats.items())[:3])
    subject = f"{mark} {job} {status}" + (f" — {head}" if head else "")

    lines = [
        f"{job} 결과: {status}",
        "",
        _fmt("본부", dept),
        _fmt("서버", socket.gethostname()),
        _fmt("소요", f"{duration_sec // 60}분 {duration_sec % 60}초" if duration_sec else "-"),
        "",
    ]
    lines += [_fmt(k, v) for k, v in stats.items()]
    if errors:
        lines += ["", f"오류 {len(errors)}건 (앞 10건):"]
        lines += [f"  · {str(e)[:200]}" for e in errors[:10]]
        if len(errors) > 10:
            lines.append(f"  … 외 {len(errors) - 10}건")
    if extra:
        lines += ["", extra]
    lines += [
        "",
        "─" * 50,
        "이 메일은 성공·실패 모두 발송됩니다. 메일이 오지 않으면 작업 자체가",
        "실행되지 않은 것이니 cron 과 서비스 상태를 확인하세요.",
    ]
    return send_mail(subject, "\n".join(lines))


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="알림 설정 확인 / 셸에서 결과 발송")
    ap.add_argument("--test", action="store_true", help="테스트 메일 발송")
    ap.add_argument("--job", default="", help="작업 이름 (셸 스크립트용)")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--status", default="success", help="success|partial|failed")
    ap.add_argument("--stats", default="", help="'라벨=값' 을 쉼표로 구분")
    ap.add_argument("--errors", default="", help="오류 메시지를 줄바꿈으로 구분")
    ap.add_argument("--duration", type=int, default=0, help="소요 초")
    args = ap.parse_args()

    # 셸 스크립트(backup.sh 등)에서 결과를 보낼 때
    if args.job:
        stats = {}
        for pair in args.stats.split(","):
            if "=" in pair:
                k, _, v = pair.partition("=")
                stats[k.strip()] = v.strip()
        errs = [e for e in args.errors.splitlines() if e.strip()]
        sent = notify_job(
            job=args.job,
            dept=args.dept,
            status=args.status,
            stats=stats,
            errors=errs,
            duration_sec=args.duration,
        )
        if not sent and is_configured() and args.status in {"partial", "failed"}:
            return 1  # 실패 알림이 나가지 못한 것은 그 자체로 문제입니다
        return 0

    c = _cfg()
    print("■ 알림 설정")
    print(_fmt("수신", ", ".join(c["to"]) or "❌ ALERT_EMAIL_TO 미설정"))
    print(_fmt("SMTP", f"{c['host'] or '❌ SMTP_HOST 미설정'}:{c['port']}"))
    print(_fmt("발신", c["from"] or c["user"] or f"semantica@{socket.gethostname()}"))
    print(_fmt("인증", c["user"] or "없음 (익명 릴레이)"))
    print(_fmt("암호화", "SMTPS" if c["ssl"] else ("STARTTLS" if c["tls"] else "없음")))
    print(_fmt("성공 시 발송", "예" if c["on_success"] else "아니오 (실패만)"))

    if not is_configured():
        print("\n❌ 설정이 부족합니다. .env 에 ALERT_EMAIL_TO 와 SMTP_HOST 를 넣으세요.")
        return 1
    if not args.test:
        print("\n  --test 로 실제 발송을 확인할 수 있습니다.")
        return 0

    sent = notify_job(
        job="알림 테스트",
        dept="strategic",
        status="success",
        stats={"페이지": 0, "이벤트": 0},
        duration_sec=3,
        extra="이 메일이 보이면 알림 설정이 정상입니다.",
    )
    print("\n✅ 발송 완료" if sent else "\n❌ 발송 실패 (위 오류 참고)")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(_main())
