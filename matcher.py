"""직무 매칭.

설계 원칙: 미탐(놓침)이 오탐(잡음)보다 훨씬 비싸다.
기보 AI·데이터 공고를 한 번 놓치면 1년을 기다려야 하지만,
알림이 하루 10건 와도 3초 스크롤이면 끝난다.

세 축을 OR로 묶는다.

    (1) 기관 화이트리스트 (잡알리오)   → 직무 무관하게 통과
    (2) 감시 사이트 출처 (config.sites) → 이미 선별한 곳이므로 문턱을 낮춘다
    (3) core 직무 키워드               → 출처 무관하게 통과

(2)가 중요한 이유: 은행 공채 제목은 "신입행원 UB 부문 채용"처럼
사내 용어라 데이터·리스크 키워드가 하나도 안 걸린다.
출처를 신뢰하지 않으면 공채 자체를 통째로 놓친다.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from collectors import Posting, CLOSED_STATUS


TIER_LABEL = {3: "최우선", 2: "관심", 1: "참고", 0: "제외"}


@dataclass
class Match:
    tier: int                  # 3 최우선 / 2 관심 / 1 참고 / 0 제외
    score: int
    hits: list[str]
    reason: str

    @property
    def label(self) -> str:
        return TIER_LABEL[self.tier]


def _found(haystack: str, words: list[str]) -> list[str]:
    low = haystack.lower()
    return [w for w in words if w.lower() in low]


def _days_until(deadline: str) -> int | None:
    """YYYYMMDD 문자열까지 남은 일수. 파싱 불가면 None(=판단 보류)."""
    digits = re.sub(r"\D", "", deadline or "")
    if len(digits) != 8:
        return None
    try:
        end = dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None
    return (end - dt.date.today()).days


def evaluate(posting: Posting, cfg: dict) -> Match:
    kw = cfg["keywords"]
    watch = cfg.get("watchlist_institutions", [])
    hay = posting.haystack

    # 게시판이 스스로 "종료/마감"이라고 표시한 건은 볼 이유가 없다.
    # (제목이 아니라 목록의 상태 칸에서 읽은 값이라 오판 위험이 낮다)
    status = posting.extra.get("status", "")
    if status in CLOSED_STATUS:
        return Match(0, 0, [], f"게시판 상태: {status}")

    # 접수기간이 이미 지난 건도 제외.
    # 상태 표시가 없는 사이트(신평사 등)에서는 이쪽이 유일한 단서다.
    days_left = _days_until(posting.deadline)
    if days_left is not None and days_left < 0:
        return Match(0, 0, [], f"접수 마감 ({posting.deadline})")

    core = _found(hay, kw["core"])
    related = _found(hay, kw.get("related", []))
    signal = _found(hay, kw.get("signal", []))
    opens = _found(hay, kw.get("open_recruitment", []))

    watched_org = [inst for inst in watch if inst in posting.org]
    # config.sites 에 등록된 출처 = 사용자가 직접 고른 감시 대상
    watched_source = posting.source != "alio"
    trusted = bool(watched_org) or watched_source

    # --- 0단계: 확실한 노이즈 제거 --------------------------------
    excluded = _found(hay, kw.get("exclude", []))
    if excluded and not (trusted and (core or opens)):
        return Match(0, 0, excluded, f"제외 키워드: {', '.join(excluded)}")

    score = (len(core) * 3 + len(opens) * 2 + len(related)
             + len(signal) + len(watched_org) * 3 + (2 if watched_source else 0))
    hits = core + opens + related + signal

    origin = watched_org[0] if watched_org else posting.org

    # --- 1) 감시 대상 + 희망 직무 : 즉시 확인 ----------------------
    if trusted and core:
        return Match(3, score, hits,
                     f"{origin} · 희망직무 키워드 {', '.join(core[:3])}")

    # --- 2) 감시 대상의 공채 : 직무 키워드 없어도 통과 -------------
    if trusted and opens:
        return Match(3 if watched_org else 2, score, hits,
                     f"{origin} 공채 공고 ({', '.join(opens[:2])})")

    # --- 3) 화이트리스트 기관의 그 밖의 공고 -----------------------
    #     부문 이름은 해마다 바뀐다. 기관이 맞으면 일단 본다.
    if watched_org:
        return Match(2, score, hits,
                     f"{origin} 공고 (직무 키워드 미검출 — 부문 개편 가능성)")

    # --- 4) 출처는 맞지만 직무·공채 신호가 전혀 없는 경우 ----------
    #     감시 사이트라도 무관한 계열사·직군 공고까지 보드에 쌓이면
    #     정작 봐야 할 것이 묻힌다. 인접 직무 신호만으로는 남기지 않는다.
    #     (놓칠 위험은 core 키워드를 넓히는 쪽으로 대응한다)

    # --- 5) 출처 무관, 직무 키워드만으로 --------------------------
    if core:
        return Match(3 if opens else 2, score, hits,
                     f"희망직무 키워드 {', '.join(core[:3])}")

    # 인접 직무(IT·디지털 등)만 걸린 건은 남기지 않는다.
    #   '백엔드 개발자 / 정규직' 같은 공고가 보드를 채우면
    #   정작 봐야 할 신용·리스크 공고가 묻힌다.
    #   놓칠 위험은 core 키워드를 넓히는 쪽으로 대응한다.

    # --- 6) 자동 수집이 불가능한 곳은 체크리스트로만 남긴다 ---------
    if posting.extra.get("manual"):
        note = posting.extra.get("reason_note") or "자동 수집 불가"
        return Match(1, 0, [], note)

    # --- 7) snapshot 모드는 내용을 모르니 항상 확인 대상 -----------
    if posting.title.startswith("[페이지 변경 감지]"):
        return Match(2, 1, [], "감시 페이지 내용이 바뀜")

    return Match(0, score, hits, "매칭 없음")


def evaluate_all(postings: list[Posting], cfg: dict) -> list[tuple[Posting, Match]]:
    scored = [(p, evaluate(p, cfg)) for p in postings]
    keep = [(p, m) for p, m in scored if m.tier > 0]
    keep.sort(key=lambda pm: (-pm[1].tier, -pm[1].score, pm[0].org))
    return keep
