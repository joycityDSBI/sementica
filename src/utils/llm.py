"""
LLM 호출 래퍼 — SDK 버전에 따라 temperature 전달 방식을 자동 선택합니다.
─────────────────────────────────────────────────────────────────────────────
왜 필요한가:
    추출·채점을 재현 가능하게 하려고 모든 호출에 temperature=0 을 넣었더니,
    운영 VM 의 anthropic 1.2.0 이
        TypeError: Messages.create() got an unexpected keyword argument 'temperature'
    를 내며 **모든 LLM 호출이 실패**했습니다. 인제스트는 정상 종료했는데
    그래프의 트리플이 0개, 299페이지 중 248개가 error 였습니다.
    (임베딩은 Vertex 쪽이라 무사해서 벡터만 멀쩡했습니다.)

    0.99.0 에는 명명 인자가 있고 1.2.0 에는 없습니다. 즉 버전을 올려도 내려도
    한쪽이 깨지므로, 어느 통로가 열려 있는지 런타임에 고릅니다.

전달 방식 (위에서부터 시도):
    ① temperature=                      명명 인자 (0.x)
    ② output_config={"temperature": …}  1.x 에서 샘플링 설정이 모인 자리로 보임
    ③ extra_body={"temperature": …}     raw body 통로
    ④ 생략                               전부 막혔을 때 — 크게 경고

    후보는 시그니처로 1차 선별하고, 실제 호출 결과로 확정합니다. 서명에는
    있지만 API 가 거부하는 경우가 있어 첫 호출 결과까지 봐야 합니다.

    ④ 로 떨어지면 한 번 크게 경고합니다. 조용히 빼면 추출이 회차마다 달라지는데도
    아무도 모르게 됩니다 — 실제로 그래서 그래프가 흔들렸습니다.

    어느 방식이 쓰이는지 미리 보려면:  python tools/probe_llm.py
"""

import inspect
import threading

_lock = threading.Lock()
_strategy: str | None = None  # None=미판별, 이후 named|output_config|extra_body|omit
_warned = False

# (이름, kwargs 생성기, 시그니처에서 요구하는 파라미터)
_CANDIDATES = [
    ("named", lambda t: {"temperature": t}, "temperature"),
    ("output_config", lambda t: {"output_config": {"temperature": t}}, "output_config"),
    ("extra_body", lambda t: {"extra_body": {"temperature": t}}, "extra_body"),
]


def _anthropic_version() -> str:
    try:
        import anthropic

        return getattr(anthropic, "__version__", "unknown")
    except Exception:
        return "unknown"


def _params(create_fn) -> set:
    try:
        return set(inspect.signature(create_fn).parameters)
    except (TypeError, ValueError):
        return set()


def _accepts_anything(create_fn) -> bool:
    """**kwargs 를 받는 함수인지 — 그렇다면 시그니처 선별이 무의미합니다."""
    try:
        return any(
            p.kind is inspect.Parameter.VAR_KEYWORD
            for p in inspect.signature(create_fn).parameters.values()
        )
    except (TypeError, ValueError):
        return False


def _is_param_rejection(exc: Exception, name: str) -> bool:
    """이 예외가 '그 파라미터를 못 받는다'는 뜻인지 판정.

    네트워크·쿼터 오류까지 삼키면 후보를 잘못 탈락시키므로, 파라미터 이름이
    오류 메시지에 나올 때만 다음 후보로 넘어갑니다.
    """
    msg = str(exc).lower()
    if isinstance(exc, TypeError):
        return name in msg or "unexpected keyword" in msg
    return name in msg and any(k in msg for k in ("unexpected", "invalid", "unknown", "extra"))


def _warn_omitted() -> None:
    global _warned
    with _lock:
        if _warned:
            return
        _warned = True
    print(
        "\n"
        f"  ⚠️  이 anthropic SDK({_anthropic_version()})로는 temperature 를 전달할 수 없습니다.\n"
        "      기본값(1.0)으로 샘플링되므로 같은 문서에서도 추출 결과가 달라지고,\n"
        "      평가 점수도 회차마다 흔들립니다. 그래프는 영구 저장물이라 그대로 남습니다.\n"
        "      python tools/probe_llm.py 로 통로를 다시 확인해 보세요.\n"
    )


def create_message(client, **kwargs):
    """client.messages.create(**kwargs) — temperature 전달 방식을 자동 선택합니다.

    temperature 가 없는 호출은 손대지 않고 그대로 넘깁니다.
    """
    global _strategy

    temp = kwargs.pop("temperature", None)
    create_fn = client.messages.create
    if temp is None:
        return create_fn(**kwargs)

    # 이미 확정된 방식이 있으면 그대로 사용
    if _strategy is not None:
        if _strategy == "omit":
            return create_fn(**kwargs)
        extra = next(mk for name, mk, _ in _CANDIDATES if name == _strategy)(temp)
        return create_fn(**_merge(kwargs, extra))

    # 첫 호출 — 시그니처로 후보를 좁히고 실제 호출로 확정
    params = _params(create_fn)
    wildcard = _accepts_anything(create_fn)
    for name, mk, needs in _CANDIDATES:
        if not wildcard and needs not in params:
            continue
        try:
            result = create_fn(**_merge(kwargs, mk(temp)))
        except Exception as e:
            if _is_param_rejection(e, needs):
                continue  # 이 통로는 막혀 있음 — 다음 후보
            raise  # 네트워크·쿼터 등 실제 오류는 감추지 않습니다
        _strategy = name
        return result

    _strategy = "omit"
    _warn_omitted()
    return create_fn(**kwargs)


def _merge(kwargs: dict, extra: dict) -> dict:
    """호출자가 이미 넘긴 extra_body/output_config 를 덮어쓰지 않고 합칩니다."""
    merged = dict(kwargs)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def strategy() -> str:
    """진단용 — 현재 확정된 전달 방식."""
    return _strategy or "미판별"


def reset() -> None:
    """진단·테스트용 — 판별 상태 초기화."""
    global _strategy, _warned
    _strategy = None
    _warned = False
