"""수집 결과를 정적 대시보드(docs/index.html)로 렌더링한다.

디자인 의도
-----------
이 페이지의 유일한 임무는 "지금 지원해야 하는 게 있나?"에 3초 안에 답하는 것.
그래서 각 행의 왼쪽에 '마감 레일'을 둔다. 접수기간 중 남은 비율만큼 채워지며,
D-7 이하로 떨어지면 색이 바뀐다. 장식이 아니라 실제 정보를 담은 구조물이다.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from pathlib import Path


TIER_ORDER = [3, 2, 1]
TIER_META = {
    3: ("최우선", "t3"),
    2: ("관심", "t2"),
    1: ("참고", "t1"),
}


def _parse_date(value: str) -> dt.date | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 8:
        return None
    try:
        return dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def _fmt(value: str) -> str:
    """YYYYMMDD → 26.08.14"""
    d = _parse_date(value)
    return d.strftime("%y.%m.%d") if d else ""


def _deadline_info(posted: str, deadline: str, today: dt.date) -> dict:
    """왼쪽 칸에 무엇을 띄울지 결정한다.

    마감일을 알면 D-day, 모르면 등록일, 둘 다 모르면 '날짜 미상'.
    날짜를 못 읽었다고 숨기지 않는다 — 판단은 사람이 한다.
    """
    end = _parse_date(deadline)
    start = _parse_date(posted)

    if end:
        days = (end - today).days
        if days < 0:
            return {"main": "마감", "sub": _fmt(deadline), "days": days,
                    "fill": 0.0, "urgent": False, "closed": True}
        total = (end - start).days if start and (end - start).days > 0 else 14
        return {
            "main": f"D-{days}" if days else "오늘",
            "sub": _fmt(deadline),
            "days": days,
            "fill": max(0.0, min(1.0, days / total)),
            "urgent": days <= 7,
            "closed": False,
        }

    if start:
        # 마감일은 모르고 등록일만 아는 경우 — 등록 후 경과일을 보여준다
        age = (today - start).days
        return {"main": _fmt(posted), "sub": f"{age}일 전 등록", "days": None,
                "fill": 0.0, "urgent": False, "closed": False, "unknown": True}

    return {"main": "—", "sub": "날짜 미상", "days": None,
            "fill": 0.0, "urgent": False, "closed": False, "unknown": True}


def render(records: list[dict], out_path: Path, today: dt.date | None = None) -> Path:
    today = today or dt.date.today()

    rows_by_tier: dict[int, list[str]] = {3: [], 2: [], 1: []}
    open_count = 0
    urgent_count = 0
    unknown_count = 0

    for rec in records:
        tier = rec.get("tier", 1)
        if tier not in rows_by_tier:
            continue
        dl = _deadline_info(rec.get("posted", ""), rec.get("deadline", ""), today)
        if dl.get("unknown"):
            unknown_count += 1
        if not dl["closed"]:
            open_count += 1
            if dl["urgent"]:
                urgent_count += 1

        state_class = "closed" if dl["closed"] else ("urgent" if dl["urgent"] else "open")
        if dl.get("unknown"):
            state_class += " nodate"
        is_new = rec.get("is_new", False)

        hits = rec.get("hits", [])[:4]
        chips = "".join(f'<span class="chip">{html.escape(h)}</span>' for h in hits)
        if rec.get("extra", {}).get("link_fallback"):
            chips += '<span class="chip fallback">목록에서 찾기</span>'
        if rec.get("extra", {}).get("manual"):
            chips += '<span class="chip fallback">직접 확인</span>'

        # 게시판에서 읽은 행 정보가 있으면 그것을 우선 보여준다.
        # (제목만으로는 상·하반기 공고가 구분이 안 되기 때문)
        board_meta = rec.get("extra", {}).get("meta", "")
        status = rec.get("extra", {}).get("status", "")
        status_badge = (f'<span class="status">{html.escape(status)}</span>'
                        if status else "")
        if board_meta:
            why_html = (f'<div class="why board">{status_badge}'
                        f'{html.escape(board_meta)}</div>'
                        f'<div class="why sub">{html.escape(rec.get("reason", ""))}</div>')
        else:
            why_html = f'<div class="why">{html.escape(rec.get("reason", ""))}</div>'

        rows_by_tier[tier].append(f"""
        <li class="row {state_class}">
          <div class="rail" style="--fill:{dl['fill']:.3f}" aria-hidden="true"></div>
          <div class="dday">
            <span class="dday-main">{html.escape(dl['main'])}</span>
            <span class="dday-sub">{html.escape(dl['sub'])}</span>
          </div>
          <div class="body">
            <div class="meta">
              <span class="org">{html.escape(rec.get('org', ''))}</span>
              <span class="group">{html.escape(rec.get('group', ''))}</span>
              {'<span class="new">NEW</span>' if is_new else ''}
            </div>
            <a class="title" href="{html.escape(rec.get('url', '#'))}"
               target="_blank" rel="noopener">{html.escape(rec.get('title', ''))}</a>
            {why_html}
            <div class="chips">{chips}</div>
          </div>
          <div class="dates">
            <div><span class="lbl">등록</span>{html.escape(_fmt(rec.get('posted', '')) or '미상')}</div>
            <div><span class="lbl">마감</span>{html.escape(_fmt(rec.get('deadline', '')) or '미상')}</div>
          </div>
        </li>""")

    sections = []
    for tier in TIER_ORDER:
        label, cls = TIER_META[tier]
        items = rows_by_tier[tier]
        if not items:
            continue
        sections.append(f"""
      <section class="tier {cls}">
        <h2><span class="tier-mark">{label}</span><span class="count">{len(items)}</span></h2>
        <ul class="rows">{''.join(items)}</ul>
      </section>""")

    if not sections:
        sections.append("""
      <section class="tier empty">
        <p>아직 조건에 맞는 공고가 없습니다. 감시는 계속 돌고 있습니다.</p>
        <p class="hint">놓친 공고가 있다면 config.yaml 의 keywords.core 를 넓히세요.</p>
      </section>""")

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>채용 감시 보드</title>
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link rel="stylesheet"
  href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
  :root {{
    --paper:#E7EBF0; --ink:#131A24; --ink-soft:#59667A; --line:#C3CCD8;
    --deep:#1B3A6B; --alert:#A82A52; --calm:#3D7A6B; --card:#F6F8FA;
    --mono: ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, monospace;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--paper); color:var(--ink);
    font-family:"Pretendard Variable", Pretendard, system-ui, sans-serif;
    line-height:1.5;
  }}
  .wrap {{ max-width:900px; margin:0 auto; padding:32px 20px 80px; }}

  header {{ border-bottom:2px solid var(--ink); padding-bottom:18px; margin-bottom:28px; }}
  h1 {{
    font-size:clamp(26px,5vw,38px); margin:0 0 4px; letter-spacing:-0.03em;
    font-weight:800;
  }}
  .sub {{ color:var(--ink-soft); font-size:13px; font-family:var(--mono); }}
  .tally {{ display:flex; gap:28px; margin-top:18px; }}
  .tally div {{ display:flex; flex-direction:column; }}
  .tally b {{ font-size:28px; font-family:var(--mono); font-weight:600; letter-spacing:-0.02em; }}
  .tally span {{ font-size:11px; color:var(--ink-soft); letter-spacing:0.08em; }}
  .tally .hot b {{ color:var(--alert); }}

  .tier {{ margin-bottom:34px; }}
  .tier h2 {{
    display:flex; align-items:baseline; gap:10px;
    font-size:13px; letter-spacing:0.14em; margin:0 0 10px;
    text-transform:uppercase; color:var(--ink-soft);
  }}
  .tier-mark {{
    background:var(--ink); color:var(--paper); padding:3px 9px;
    font-size:12px; letter-spacing:0.1em; border-radius:2px;
  }}
  .t3 .tier-mark {{ background:var(--alert); }}
  .t2 .tier-mark {{ background:var(--deep); }}
  .t1 .tier-mark {{ background:var(--ink-soft); }}
  .count {{ font-family:var(--mono); }}

  .rows {{ list-style:none; margin:0; padding:0; }}
  .row {{
    position:relative; display:grid;
    grid-template-columns:64px 1fr auto; gap:14px; align-items:start;
    background:var(--card); border:1px solid var(--line);
    border-radius:3px; padding:14px 16px 14px 22px; margin-bottom:8px;
  }}
  /* 마감 레일 — 남은 기간 비율만큼 채워진다 */
  .rail {{
    position:absolute; left:0; top:0; bottom:0; width:6px;
    background:linear-gradient(to top,
      var(--calm) calc(var(--fill) * 100%), var(--line) 0);
  }}
  .row.urgent .rail {{
    background:linear-gradient(to top,
      var(--alert) calc(var(--fill) * 100%), var(--line) 0);
  }}
  .row.closed {{ opacity:0.45; }}
  .row.closed .rail {{ background:var(--line); }}

  .dday {{ display:flex; flex-direction:column; padding-top:2px; }}
  .dday-main {{ font-family:var(--mono); font-size:15px; font-weight:600; letter-spacing:-0.02em; }}
  .dday-sub {{ font-size:9.5px; color:var(--ink-soft); margin-top:1px; letter-spacing:-0.01em; }}
  .row.urgent .dday-main {{ color:var(--alert); }}
  .row.nodate .dday-main {{ font-size:12px; color:var(--ink-soft); font-weight:500; }}

  .meta {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:3px; }}
  .org {{ font-weight:700; font-size:14px; }}
  .group {{ font-size:11px; color:var(--ink-soft); }}
  .new {{
    font-size:10px; font-family:var(--mono); letter-spacing:0.1em;
    background:var(--deep); color:#fff; padding:1px 5px; border-radius:2px;
  }}
  .title {{
    display:block; color:var(--ink); text-decoration:none;
    font-size:15px; font-weight:500; margin-bottom:5px;
  }}
  .title:hover {{ text-decoration:underline; text-underline-offset:3px; }}
  .why {{ font-size:12px; color:var(--ink-soft); }}
  .why.board {{ color:var(--ink); font-family:var(--mono); font-size:11.5px; }}
  .why.sub {{ font-size:11px; margin-top:2px; opacity:0.75; }}
  .status {{
    display:inline-block; font-size:10px; font-weight:700; letter-spacing:0.06em;
    background:var(--deep); color:#fff; padding:1px 6px; border-radius:2px;
    margin-right:6px; font-family:inherit;
  }}
  .chips {{ margin-top:6px; display:flex; gap:5px; flex-wrap:wrap; }}
  .chip {{
    font-size:11px; font-family:var(--mono); color:var(--deep);
    border:1px solid var(--line); border-radius:2px; padding:1px 6px;
  }}
  .chip.fallback {{ color:var(--alert); border-color:var(--alert); }}
  .dates {{
    font-family:var(--mono); font-size:11px; color:var(--ink-soft);
    white-space:nowrap; padding-top:4px; text-align:right;
  }}
  .dates .lbl {{ font-size:9px; letter-spacing:0.08em; margin-right:5px; opacity:0.7; }}

  .empty p {{ color:var(--ink-soft); }}
  .hint {{ font-size:12px; }}
  footer {{
    margin-top:40px; padding-top:14px; border-top:1px solid var(--line);
    font-size:11px; color:var(--ink-soft); font-family:var(--mono);
  }}
  a:focus-visible, .title:focus-visible {{ outline:2px solid var(--deep); outline-offset:3px; }}

  @media (max-width:600px) {{
    .row {{ grid-template-columns:52px 1fr; }}
    .dates {{ grid-column:1 / -1; padding-top:8px; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>채용 감시 보드</h1>
      <div class="sub">잡알리오 · 신용평가사 · 시중은행 · 보증기관 &nbsp;|&nbsp; 갱신 {stamp}</div>
      <div class="tally">
        <div><b>{open_count}</b><span>접수 중</span></div>
        <div class="hot"><b>{urgent_count}</b><span>D-7 이내</span></div>
        <div><b>{unknown_count}</b><span>날짜 미상</span></div>
      </div>
    </header>
    {''.join(sections)}
    <footer>
      마감일을 읽지 못한 공고는 등록일과 경과일을 대신 표시합니다. 직접 확인하세요.<br>
      공식 채용 페이지와 공개 API만 수집합니다.
    </footer>
  </div>
</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path
