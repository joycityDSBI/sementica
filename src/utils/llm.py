"""
LLM 호출 래퍼 — SDK 버전 차이를 흡수합니다.
─────────────────────────────────────────────────────────────────────────────
왜 필요한가:
    추출·채점을 재현 가능하게 하려고 모든 호출에 temperature=0 을 넣었더니,
    운영 VM 의 anthropic 1.2.0 이
        TypeError: Messages.create() got an unexpected keyword argument 'temperature'
    를 내며 **모든 LLM 호출이 실패**했습니다. 인제스트는 정상 종료했지만
    그래프의 트리플이 0개, 299페이지 중 248개가 error 로 기록됐습니다.
    (임베딩은 Vertex 쪽이라 무사해서 벡터만 정상이었습니다.)

    0.99.0 에서는 같은 인자가 동작합니다. 즉 버전을 올린다고 해결되지 않으며,
    올리든 내리든 한쪽은 깨집니다. 그래서 호출 방식을 런타임에 고릅니다.

전달 방식 우선순위:
    ① temperature=  명명 인자        (0.x 계열)
    ② extra_body={"temperature": …}  (명명 인자가 없지만 raw body 통로가 있을 때)
    ③ 생략                            (둘 다 없으면 — 기본값 1.0 으로 샘플링됨)

    ③ 으로 떨어지면 **한 번 크게 경고**합니다. 조용히 빼면 추출이 회차마다
    달라지는데도 아무도 모르게 됩니다 — 실제로 그래서 그래프가 흔들렸습니다.
"""

import inspect
import threading

_lock = threading.Lock()
# None = 아직 판별 전, 이후 "named" | "extra_body" | "omit"
_strategy: str | None = None
_warned = False


def _anthropic_version() -> str:
    try:
        import anthropic

        return getattr(anthropic, "__version__", "unknown")
    except Exception:
        return "unknown"


def _warn_omitted() -> None:
    global _warned
    with _lock:
        if _warned:
            return
        _warned = True
    print(
        "\n"
        f"  ⚠️  이 anthropic SDK({_anthropic_version()})는 temperature 를 전달할 수 없습니다.\n"
        "      기본값(1.0)으로 샘플링되므로 같은 문서에서도 추출 결과가 달라지고,\n"
        "      평가 점수도 회차마다 흔들립니다. 그래프는 영구 저장물이라 그대로 남습니다.\n"
        "      SDK 버전을 바꿔야 합니다 (0.99.0 에서는 명명 인자가 동작합니다).\n"
    )


def _detect(create_fn) -> str:
    """create 함수의 시그니처로 전달 방식을 고릅니다."""
    try:
        params = inspect.signature(create_fn).parameters
    except (TypeError, ValueError):
        # 시그니처를 못 읽으면 일단 명명 인자로 시도하고 TypeError 로 판별합니다.
        return "named"
    if "temperature" in params:
        return "named"
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return "named"  # **kwargs 를 받으면 그대로 넘겨 봅니다
    if "extra_body" in params:
        return "extra_body"
    return "omit"


def create_message(client, **kwargs):
    """client.messages.create(**kwargs) — temperature 전달 방식을 자동 선택.

    temperature 가 없는 호출은 그대로 통과시킵니다.
    """
    global _strategy

    temp = kwargs.pop("temperature", None)
    create_fn = client.messages.create
    if temp is None:
        return create_fn(**kwargs)

    if _strategy is None:
        _strategy = _detect(create_fn)

    if _strategy == "named":
        try:
            return create_fn(temperature=temp, **kwargs)
        except TypeError as e:
            if "temperature" not in str(e):
                raise
            # 시그니처로는 받는 것처럼 보였지만 실제로는 거부 — 다음 방식으로.
            _strategy = "extra_body" if "extra_body" in _sig_params(create_fn) else "omit"

    if _strategy == "extra_body":
        extra = dict(kwargs.pop("extra_body", None) or {})
        extra.setdefault("temperature", temp)
        try:
            return create_fn(extra_body=extra, **kwargs)
        except TypeError as e:
            if "extra_body" not in str(e):
                raise
            _strategy = "omit"

    _warn_omitted()
    return create_fn(**kwargs)


def _sig_params(create_fn) -> set:
    try:
        return set(inspect.signature(create_fn).parameters)
    except (TypeError, ValueError):
        return set()


def strategy() -> str:
    """진단용 — 현재 선택된 전달 방식 ('미판별' 포함)."""
    return _strategy or "미판별"
