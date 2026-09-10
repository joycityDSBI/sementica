"""
한국어 질문에서 날짜 범위를 추출하는 유틸
─────────────────────────────────────────────────────────────────────────────
timeline_search / hybrid_search 가 질문에 담긴 기간 조건을 자동으로
from_date · to_date 로 변환할 때 사용합니다.

지원 표현:
    2026년 6월 19일 / 2026-06-19 / 2026.6.19  → 그 하루
    26년 6월 19일                              → 2000년대로 보정
    2026년 6월 / 2026-06                       → 그 달 전체
    2026년 2분기 / 2026년 Q2                   → 분기 전체
    2026년                                     → 그 해 전체

미지원(의도적):
    "지난달", "최근", "올해" 같은 상대 표현은 기준 시각에 따라 결과가
    달라지므로 처리하지 않습니다. 호출부가 명시적으로 다루어야 합니다.
"""

import calendar
import re

# 시계열 의도를 나타내는 키워드 — 날짜 표현이 없어도 이력 조회로 볼 수 있는 단어
TIMELINE_KEYWORDS: frozenset = frozenset(
    {
        # 강한 신호 — 이 단어가 있으면 날짜가 없어도 시계열 질문으로 봅니다.
        "이력",
        "언제",
        "타임라인",
        "히스토리",
        "연혁",
        "추이",
    }
)

# 약한 신호 — 시계열 맥락에서 자주 쓰이지만 단독으로는 판단 근거가 못 됩니다.
# "서버 오픈 시간 테이블 경로는?" 처럼 시계열이 아닌 질문에도 흔히 등장하므로,
# **날짜 표현이 함께 있을 때만** 시계열로 간주합니다.
WEAK_TIMELINE_KEYWORDS: frozenset = frozenset(
    {
        "일정",
        "변경",
        "업데이트",
        "패치",
        "점검",
        "장애",
        "이벤트",
        "시즌",
        "출시",
        "오픈",
        "종료",
        "진행",
        "적용",
        "캠페인",
        "소재",
    }
)

_RE_YMD = re.compile(r"(\d{2,4})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})\s*일?")
_RE_YM = re.compile(r"(\d{2,4})\s*[년.\-/]\s*(\d{1,2})\s*월?(?!\s*\d)")
_RE_QUARTER = re.compile(r"(\d{2,4})\s*년?\s*(?:(\d)\s*분기|[Qq]\s*(\d))")
_RE_YEAR = re.compile(r"(\d{4})\s*년")


def _norm_year(y: int) -> int:
    """2자리 연도를 2000년대로 보정합니다. 26 → 2026"""
    return y + 2000 if y < 100 else y


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def extract_date_range(text: str) -> tuple[str, str]:
    """질문에서 날짜 범위를 추출합니다.

    구체적인 표현을 우선합니다 (일 > 월 > 분기 > 연).

    Returns:
        (from_date, to_date) — YYYY-MM-DD 형식. 못 찾으면 ("", "").

    >>> extract_date_range("2026년 6월 19일에 OFF된 소재는?")
    ('2026-06-19', '2026-06-19')
    >>> extract_date_range("2026년 6월 변경 내역")
    ('2026-06-01', '2026-06-30')
    >>> extract_date_range("2026년 2분기 이벤트")
    ('2026-04-01', '2026-06-30')
    >>> extract_date_range("담당자는 누구인가요?")
    ('', '')
    """
    # ① 연-월-일 → 하루
    m = _RE_YMD.search(text)
    if m:
        y, mo, d = _norm_year(int(m.group(1))), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            iso = f"{y:04d}-{mo:02d}-{min(d, _last_day(y, mo)):02d}"
            return iso, iso

    # ② 분기 (월보다 먼저 — "2026년 2분기"의 "2"가 월로 오인되지 않도록)
    m = _RE_QUARTER.search(text)
    if m:
        y = _norm_year(int(m.group(1)))
        q = int(m.group(2) or m.group(3))
        if 1 <= q <= 4:
            start_mo = (q - 1) * 3 + 1
            end_mo = start_mo + 2
            return (
                f"{y:04d}-{start_mo:02d}-01",
                f"{y:04d}-{end_mo:02d}-{_last_day(y, end_mo):02d}",
            )

    # ③ 연-월 → 그 달 전체
    m = _RE_YM.search(text)
    if m:
        y, mo = _norm_year(int(m.group(1))), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{_last_day(y, mo):02d}"

    # ④ 연 → 그 해 전체
    m = _RE_YEAR.search(text)
    if m:
        y = _norm_year(int(m.group(1)))
        return f"{y:04d}-01-01", f"{y:04d}-12-31"

    return "", ""


def has_timeline_intent(text: str) -> bool:
    """시계열 이력 조회가 필요한 질문인지 판단합니다.

    판정 기준:
      · 날짜 표현이 있으면 → 시계열
      · 강한 키워드(이력·언제·타임라인 …)가 있으면 → 시계열
      · 약한 키워드(오픈·변경·적용 …)만 있으면 → **아님**

    약한 키워드를 단독으로 인정하면 오탐이 큽니다. "서버 오픈 시간 테이블의
    경로는?" 같은 질문이 시계열로 분류되면 이벤트가 수십 건 조회되어
    정작 필요한 문서를 컨텍스트에서 밀어냅니다.

    >>> has_timeline_intent("2026년 6월 19일 변경 내역")
    True
    >>> has_timeline_intent("RESU 이벤트 이력 알려줘")
    True
    >>> has_timeline_intent("서버 오픈 시간 테이블의 전체 경로는?")
    False
    >>> has_timeline_intent("빌드 채널 담당자는 누구인가요?")
    False
    """
    if any(extract_date_range(text)):
        return True
    return any(k in text for k in TIMELINE_KEYWORDS)
