"""공고 수집기.

두 종류의 소스를 하나의 표준 레코드로 정규화한다.

    {id, org, title, url, posted, deadline, source, group, raw_text}

- 잡알리오 오픈API : 공공기관 수시공시를 구조화된 JSON으로 제공. 신뢰도 높음.
- 민간 채용 페이지 : 공식 페이지만 대상. 구조가 제각각이라 두 가지 모드로 처리한다.
    links    → <a> 링크 목록을 공고로 간주 (정적 HTML 게시판형)
    snapshot → 본문 텍스트 해시만 비교. 변경 감지 시 "확인 필요" 한 건으로 보고
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup


# ----------------------------------------------------------------------
# 표준 레코드
# ----------------------------------------------------------------------

@dataclass
class Posting:
    org: str
    title: str
    url: str
    source: str                 # "alio" 또는 사이트 id
    group: str = ""             # 신용평가사 / 시중은행 / 보증기관 ...
    posted: str = ""
    deadline: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """중복 판정 키. 제목과 기관이 같으면 URL 파라미터가 달라도 같은 공고로 본다."""
        basis = f"{self.org}|{normalize(self.title)}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def haystack(self) -> str:
        """키워드 매칭 대상 텍스트."""
        parts = [self.org, self.title, str(self.extra.get("category", "")),
                 str(self.extra.get("employment_type", ""))]
        return " ".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["id"] = self.id
        return d


def normalize(text: str) -> str:
    """공백·괄호·특수문자를 걷어낸 비교용 문자열."""
    return re.sub(r"[\s\u200b()\[\]{}·,.\-_/]+", "", text or "").lower()


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------

def make_session(fetch_cfg: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": fetch_cfg.get("user_agent", "job-watch/1.0")})
    return s


class _LegacyTLSAdapter(requests.adapters.HTTPAdapter):
    """구형 TLS 서버용 어댑터.

    일부 국내 기관 서버는 OpenSSL 3.x 가 기본으로 막아둔
    legacy renegotiation 을 요구한다(UNSAFE_LEGACY_RENEGOTIATION_DISABLED).
    해당 사이트에만 예외적으로 허용한다. 인증서 검증은 그대로 유지한다.
    """

    def init_poolmanager(self, *args, **kwargs):
        import ssl
        from urllib3.util.ssl_ import create_urllib3_context

        ctx = create_urllib3_context()
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _session_for(site: dict, session: requests.Session, cfg: dict) -> requests.Session:
    """사이트별 특수 사정이 있으면 전용 세션을 만들어 준다."""
    if not site.get("legacy_ssl"):
        return session
    special = make_session(cfg["fetch"])
    special.mount("https://", _LegacyTLSAdapter())
    return special


# ----------------------------------------------------------------------
# 1) 잡알리오 오픈API
# ----------------------------------------------------------------------

def _dig(payload: Any, path: Iterable[str]) -> Any:
    """중첩 dict에서 경로를 따라 내려간다. 중간에 없으면 None."""
    cur = payload
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _xml_rows(text: str, path: list[str]) -> list[dict]:
    """XML 응답에서 공고 요소들을 dict 리스트로 바꾼다.

    워크넷 등 상당수 공공 API가 JSON이 아니라 XML로 응답한다.
    path 의 마지막 태그명을 가진 요소를 모두 찾아 자식 태그를 key/value 로 편다.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(text)
    tag = path[-1] if path else None
    if not tag:
        return []
    rows = []
    for elem in root.iter(tag):
        row = {child.tag: (child.text or "").strip() for child in elem}
        if row:
            rows.append(row)
    return rows


def fetch_api(api: dict, cfg: dict, session: requests.Session, raw: bool = False):
    """공공 오픈API 한 곳을 페이지 단위로 긁는다 (JSON/XML 공통).

    endpoint / field_map 이 실제 스펙과 다르면 빈 결과가 나온다.
    그때는 `python watch.py probe-api <id>` 로 원문을 찍어보고 config 를 고칠 것.
    """
    if not api.get("enabled"):
        return []

    key = os.environ.get(api["service_key_env"], "")
    if not key:
        raise RuntimeError(
            f"환경변수 {api['service_key_env']} 가 비어 있습니다. "
            "공공데이터포털에서 발급받은 인증키를 넣으세요."
        )

    fm = api["field_map"]
    fmt = api.get("format", "json").lower()
    timeout = cfg["fetch"]["timeout"]
    out: list[Posting] = []

    for page in range(1, api.get("max_pages", 5) + 1):
        params = dict(api.get("static_params", {}))
        params[api.get("auth_param", "serviceKey")] = key
        params[api["page_param"]] = page
        params[api["rows_param"]] = api.get("rows_per_page", 100)

        resp = session.get(api["endpoint"], params=params, timeout=timeout)
        resp.raise_for_status()

        if raw:
            return resp.text[:6000]

        path = api.get("result_path", ["result"])
        if fmt == "xml":
            try:
                rows = _xml_rows(resp.text, path)
            except Exception as exc:            # noqa: BLE001
                raise RuntimeError(f"XML 파싱 실패: {exc}\n앞부분:\n{resp.text[:400]}")
        else:
            try:
                payload = resp.json()
            except ValueError:
                raise RuntimeError(f"JSON이 아닌 응답입니다. 앞부분:\n{resp.text[:400]}")
            rows = _dig(payload, path)

        if not isinstance(rows, list) or not rows:
            break

        for row in rows:
            out.append(Posting(
                org=str(row.get(fm["org"], "")).strip(),
                title=str(row.get(fm["title"], "")).strip(),
                url=str(row.get(fm.get("url", ""), "")).strip(),
                posted=str(row.get(fm.get("posted", ""), "")).strip(),
                deadline=str(row.get(fm.get("deadline", ""), "")).strip(),
                source=api["id"],
                group=api.get("group", "오픈API"),
                extra={
                    "employment_type": row.get(fm.get("employment_type", ""), ""),
                    "category": row.get(fm.get("category", ""), ""),
                },
            ))

        if len(rows) < api.get("rows_per_page", 100):
            break
        time.sleep(0.3)

    return out


# ----------------------------------------------------------------------
# 2) 민간 채용 페이지
# ----------------------------------------------------------------------

# 공고 링크로 보기 어려운 텍스트 (네비게이션, 푸터 등)
_JUNK_LINK = re.compile(
    r"^(홈|home|로그인|회원가입|검색|더보기|이전|다음|목록|top|메뉴|사이트맵|"
    r"개인정보|이용약관|바로가기|\d+)$", re.I
)


_ID_PARAM_RE = re.compile(
    r"\b(?:nttSn|nttNo|nttId|seq|sn|idx|articleNo|bbsSeq|boardSeq|postId|no|id)"
    r"\s*[=:]\s*['\"]?(\d{2,12})",
    re.I,
)


def _extract_id(anchor) -> str:
    """javascript:fn_view('5054726') / onclick="go(5054726)" 에서 글 번호를 뽑는다.

    함수명은 사이트마다 다르지만 인자로 넘기는 글 번호는 공통이다.
    주의: 인자가 여러 개면 (예: fn_view('407','5054726')) 게시판 ID가 먼저 나온다.
    그래서 ① 이름이 붙은 파라미터를 먼저 찾고 ② 없으면 가장 긴 숫자를 고른다.
    글 번호가 게시판 ID보다 자릿수가 큰 것이 일반적이기 때문이다.
    """
    sources = [anchor.get("href") or "", anchor.get("onclick") or ""]
    for key, value in anchor.attrs.items():
        if key.startswith("data-") and isinstance(value, str):
            sources.append(value)

    # ① nttSn=5054726 처럼 이름이 붙은 값
    for src in sources:
        m = _ID_PARAM_RE.search(src or "")
        if m:
            return m.group(1)

    # ② 이름이 없으면 가장 긴 숫자 (동률이면 먼저 나온 것)
    best = ""
    for src in sources:
        for candidate in re.findall(r"\d{2,12}", src or ""):
            if len(candidate) > len(best):
                best = candidate
    return best


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


# 게시판 행에서 뽑아낼 것들
_DATE_RE = re.compile(r"(20\d{2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})")
_STATUS_WORDS = ["사전공고", "진행중", "접수중", "접수예정", "예정", "종료", "마감", "완료"]
CLOSED_STATUS = {"종료", "마감", "완료"}


def _row_meta(anchor, title: str, max_extra: int = 160) -> dict:
    """링크를 감싼 게시판 행(tr/li/div)의 나머지 텍스트를 긁는다.

    제목만으로는 '2026년도 상반기 채용 공고'와 '2026년도 하반기 채용 공고'가
    구분은 되어도 진행 여부를 알 수 없다. 행에 붙은 등록일·상태가 그 정보를 준다.

    조상을 위로 올라가되, 제목보다 조금 더 큰 첫 번째 요소에서 멈춘다.
    너무 크면(페이지 전체 등) 포기한다.
    """
    title_norm = re.sub(r"\s+", " ", title).strip()
    node, row_text = anchor, ""

    for _ in range(5):
        node = getattr(node, "parent", None)
        if node is None or node.name in ("body", "html", "[document]"):
            break
        text = re.sub(r"\s+", " ", node.get_text(" ")).strip()
        if len(text) <= len(title_norm) + 2:
            continue                      # 제목뿐 — 더 위로
        if len(text) > len(title_norm) + max_extra:
            break                         # 너무 큼 — 행이 아니라 목록 전체
        row_text = text
        break

    if not row_text:
        return {}

    # 제목을 덜어내고 남은 것이 메타데이터
    meta = row_text.replace(title_norm, " ")
    meta = re.sub(r"\s*[|·/]\s*", " | ", meta)
    meta = re.sub(r"\s+", " ", meta).strip(" |-·")

    result: dict[str, Any] = {}
    if meta:
        result["meta"] = meta[:max_extra]

    # 날짜가 두 개 이상이면 접수기간(시작 ~ 마감)으로 본다.
    # "2026.08.14 ~ 2026.09.01" 또는 "등록 2026.08.14 마감 2026.09.01" 모두 잡힌다.
    dates = [f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
             for m in _DATE_RE.finditer(meta)]
    if len(dates) >= 2:
        result["posted"] = dates[0]
        result["deadline"] = dates[-1]
    elif len(dates) == 1:
        # 물결표 뒤에 홀로 있으면 마감일, 아니면 등록일
        idx = meta.find("~")
        pos = _DATE_RE.search(meta).start()
        result["deadline" if 0 <= idx < pos else "posted"] = dates[0]

    for word in _STATUS_WORDS:
        if word in meta:
            result["status"] = word
            break

    return result


def fetch_site(site: dict, cfg: dict, session: requests.Session) -> list[Posting]:
    # manual 모드: 아예 요청을 보내지 않는다.
    #   robots.txt 로 크롤링을 막았거나, 봇 접근을 차단하거나,
    #   공고 목록이 자바스크립트로만 그려져 정적 수집이 무의미한 곳.
    #   보드에 '직접 확인' 항목으로만 남겨 체크리스트 역할을 하게 한다.
    if site.get("mode") == "manual":
        return [Posting(
            org=site["name"],
            title=f"[직접 확인] {site['name']} 채용 페이지",
            url=site["url"],
            source=site["id"],
            group=site.get("group", ""),
            extra={"manual": True, "reason_note": site.get("note", "")},
        )]

    timeout = cfg["fetch"]["timeout"]
    session = _session_for(site, session, cfg)
    resp = session.get(site["url"], timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    soup = BeautifulSoup(resp.text, "html.parser")

    mode = site.get("mode", "links")

    if mode == "links":
        lf = cfg.get("link_filter", {})
        must_any = [w.lower() for w in lf.get("must_contain_any", [])]
        never_any = [w.lower() for w in lf.get("exclude_any", [])]
        min_len = lf.get("min_length", 8)

        postings = []
        seen_titles = set()
        for a in soup.find_all("a"):
            text = re.sub(r"\s+", " ", a.get_text(" ")).strip()
            if not text or len(text) < min_len or _JUNK_LINK.match(text):
                continue
            # 공고처럼 보이는 링크만 남긴다 (메뉴·푸터 제거의 핵심)
            if must_any and not any(w in text.lower() for w in must_any):
                continue
            # '채용 중인 공고 확인하기' 같은 안내 링크는 공고가 아니다
            if never_any and any(w in text.lower() for w in never_any):
                continue
            if normalize(text) in seen_titles:
                continue
            seen_titles.add(normalize(text))

            # 게시판 목록은 javascript:fn_view('123') 같은 링크를 자주 쓴다.
            # 그런 주소는 밖에서 열리지 않으므로, 목록 페이지 주소로 대체한다.
            href = (a.get("href") or "").strip()
            target = requests.compat.urljoin(site["url"], href) if href else ""
            usable = target.startswith(("http://", "https://")) and \
                not href.lower().startswith(("javascript:", "mailto:", "tel:")) and \
                href not in ("", "#")

            extra = _row_meta(a, text)

            if not usable:
                # 스크립트 링크 → 사이트별 상세 주소 틀로 재조립
                template = site.get("detail_url", "")
                post_id = _extract_id(a)
                if template and post_id:
                    target = template.replace("{id}", post_id)
                    usable = True
                    extra["post_id"] = post_id
                else:
                    target = site["url"]
                    extra["link_fallback"] = True

            postings.append(Posting(
                org=site["name"],
                title=text[:200],
                url=target,
                posted=extra.pop("posted", ""),
                deadline=extra.pop("deadline", ""),
                source=site["id"],
                group=site.get("group", ""),
                extra=extra,
            ))
        return postings

    # snapshot 모드: 페이지 전체를 한 건으로 취급하고 해시로 변경만 감지
    text = _visible_text(soup)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return [Posting(
        org=site["name"],
        title=f"[페이지 변경 감지] {site['name']} 채용 페이지 확인 필요",
        url=site["url"],
        source=site["id"],
        group=site.get("group", ""),
        extra={"snapshot": digest, "text_length": len(text)},
    )]


def probe_links(site: dict, cfg: dict, session: requests.Session) -> list[dict]:
    """공고 링크의 원본 속성을 그대로 보여준다.

    detail_url 틀을 만들려면 실제 href/onclick 이 어떻게 생겼는지 봐야 한다.
    """
    session = _session_for(site, session, cfg)
    resp = session.get(site["url"], timeout=cfg["fetch"]["timeout"])
    resp.encoding = resp.apparent_encoding or resp.encoding
    soup = BeautifulSoup(resp.text, "html.parser")

    lf = cfg.get("link_filter", {})
    must_any = [w.lower() for w in lf.get("must_contain_any", [])]
    min_len = lf.get("min_length", 8)

    out = []
    for a in soup.find_all("a"):
        text = re.sub(r"\s+", " ", a.get_text(" ")).strip()
        if not text or len(text) < min_len or _JUNK_LINK.match(text):
            continue
        if must_any and not any(w in text.lower() for w in must_any):
            continue
        out.append({
            "title": text[:60],
            "href": (a.get("href") or "")[:120],
            "onclick": (a.get("onclick") or "")[:120],
            "data_attrs": {k: v for k, v in a.attrs.items() if k.startswith("data-")},
            "extracted_id": _extract_id(a),
        })
        if len(out) >= 8:
            break
    return out


def probe_site(site: dict, cfg: dict, session: requests.Session) -> dict:
    """이 사이트가 정적인지 SPA인지 판별해준다. mode 결정용."""
    session = _session_for(site, session, cfg)
    resp = session.get(site["url"], timeout=cfg["fetch"]["timeout"])
    resp.encoding = resp.apparent_encoding or resp.encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    text = _visible_text(soup)
    links = [re.sub(r"\s+", " ", a.get_text(" ")).strip() for a in soup.find_all("a")]
    meaningful = [t for t in links if t and len(t) >= 6 and not _JUNK_LINK.match(t)]

    spa_hint = len(text) < 400 and len(meaningful) < 5
    return {
        "status": resp.status_code,
        "text_length": len(text),
        "link_count": len(links),
        "meaningful_links": len(meaningful),
        "sample_links": meaningful[:10],
        "verdict": "SPA로 보임 → mode: snapshot 권장" if spa_hint
                   else "정적 HTML로 보임 → mode: links 사용 가능",
    }
