"""
LLM 호출 래퍼 — SDK 버전 차이를 흡수합니다.
─────────────────────────────────────────────────────────────────────────────
왜 필요한가:
    추출·채점을 재현 가능하게 하려고 모든 호출에 temperature=0 을 넣었는데,
    운영 VM 의 anthropic SDK 가 그 인자를 받지 않아
    `Messages.create() got an unexpected keyword argument 'temperature'`
    로 **모든 LLM 호출이 실패**했습니다. 인제스트는 정상 종료했지만 그래프에
    트리플이 0개, 299페이지 중 248개가 error 로 기록됐습니다.

    (실패가 status='error' 로 드러난 것 자체는 정상입니다 — 그 추적이 없었다면
     "트리플 없는 문서"로 조용히 넘어갔을 것입니다.)

동작:
    temperature 를 붙여 호출해 보고, SDK 가 거부하면 그 인자만 빼고 재시도합니다.
    이때 **한 번 크게 경고**합니다 — 조용히 빼면 온도 1.0 으로 돌아가 그래프가
    회차마다 달라지는데도 아무도 모르게 됩니다.

    근본 해결은 SDK 업그레이드입니다:  pip install -U 'anthropic[vertex]'
"""

import threading

_lock = threading.Lock()
_supports_temperature: bool | None = None
_warned = False


def _warn_once(err: Exception) -> None:
    global _warned
    with _lock:
        if _warned:
            return
        _warned = True
    try:
        import anthropic

        ver = getattr(anthropic, "__version__", "unknown")
    except Exception:
        ver = "unknown"
    print(
        "\n"
        "  ⚠️  설치된 anthropic SDK 가 temperature 인자를 받지 않습니다"
        f" (버전: {ver}).\n"
        "      temperature 없이 호출합니다 — 기본값 1.0 으로 샘플링되므로\n"
        "      추출 결과가 회차마다 달라지고 평가 점수도 흔들립니다.\n"
        "      해결:  pip install -U 'anthropic[vertex]'\n"
    )


def create_message(client, **kwargs):
    """client.messages.create(**kwargs) — temperature 미지원 SDK 에서도 동작.

    temperature 를 지원하지 않는다고 한 번 확인되면 이후에는 붙이지 않습니다
    (호출마다 실패 후 재시도하지 않도록).
    """
    if _supports_temperature is False:
        kwargs.pop("temperature", None)
        return client.messages.create(**kwargs)

    try:
        result = client.messages.create(**kwargs)
    except TypeError as e:
        # temperature 때문에 난 TypeError 인지 확인 — 다른 인자 오류면 그대로 올립니다.
        if "temperature" not in str(e) or "temperature" not in kwargs:
            raise
        globals()["_supports_temperature"] = False
        _warn_once(e)
        kwargs.pop("temperature", None)
        return client.messages.create(**kwargs)

    if "temperature" in kwargs:
        globals()["_supports_temperature"] = True
    return result
