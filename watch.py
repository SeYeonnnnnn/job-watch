#!/usr/bin/env python3
"""채용 공고 감시기.

사용법
------
  python watch.py run                 수집 → 필터 → 신규 판정 → 알림 → 보드 갱신
  python watch.py run --dry-run       알림 없이 결과만 출력
  python watch.py probe-alio          잡알리오 API 원문 응답 확인 (field_map 맞출 때)
  python watch.py probe-site <id>     사이트가 정적인지 SPA인지 판별
  python watch.py render              저장된 상태로 보드만 다시 그림
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import datetime as dt
from pathlib import Path

import yaml

import collectors
import matcher
import notify
import render as renderer


ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state" / "seen.json"
BOARD_PATH = ROOT / "docs" / "index.html"


# ----------------------------------------------------------------------
# 상태 저장 — 무엇을 이미 봤는지 기억해야 "신규"를 판정할 수 있다
# ----------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 수집
# ----------------------------------------------------------------------

def collect(cfg: dict) -> list[collectors.Posting]:
    session = collectors.make_session(cfg["fetch"])
    postings: list[collectors.Posting] = []

    # 1) 공공 오픈API (잡알리오 / 워크넷 등)
    for api in cfg.get("apis", []):
        if not api.get("enabled"):
            continue
        try:
            found = collectors.fetch_api(api, cfg, session)
            print(f"[{api['id']}] {len(found)}건 수집")
            postings.extend(found)
        except Exception as exc:                       # noqa: BLE001
            print(f"[{api['id']}] 실패: {exc}", file=sys.stderr)

    # 2) 민간·공식 채용 페이지
    delay = cfg["fetch"].get("delay_seconds", 2)
    for site in cfg.get("sites", []):
        try:
            found = collectors.fetch_site(site, cfg, session)
            print(f"[{site['id']}] {len(found)}건")
            postings.extend(found)
        except Exception as exc:                       # noqa: BLE001
            print(f"[{site['id']}] 실패: {exc}", file=sys.stderr)
        time.sleep(delay)

    return postings


# ----------------------------------------------------------------------
# 실행
# ----------------------------------------------------------------------

def cmd_run(args) -> int:
    cfg = load_config()
    state = load_state()
    today = dt.date.today().isoformat()

    postings = collect(cfg)
    scored = matcher.evaluate_all(postings, cfg)
    print(f"\n필터 통과: {len(scored)} / 전체 {len(postings)}")

    new_items: list[dict] = []
    records: list[dict] = []

    for posting, match in scored:
        key = posting.id
        prev = state.get(key)

        # snapshot 모드는 해시가 바뀌었을 때만 '신규'로 본다
        snapshot = posting.extra.get("snapshot")
        if prev and snapshot and prev.get("snapshot") != snapshot:
            is_new = True
        elif prev:
            is_new = False
        else:
            is_new = True

        record = posting.to_dict()
        record.update({
            "tier": match.tier,
            "score": match.score,
            "hits": match.hits,
            "reason": match.reason,
            "first_seen": prev.get("first_seen", today) if prev else today,
            "last_seen": today,
            "snapshot": snapshot,
            "is_new": is_new,
        })

        state[key] = record
        records.append(record)
        if is_new and match.tier >= cfg["notify"].get("min_tier_to_notify", 2):
            new_items.append(record)

    # 이번 실행에서 수집된 것만 보드에 올린다.
    # 과거 기록까지 계속 쌓으면, 원본에서 내려간 공고가 보드에 영원히 남는다.
    # state 는 '신규 판정'과 first_seen 보존용으로만 쓴다.
    records.sort(key=lambda r: (-r.get("tier", 0),
                                r.get("deadline") or "9999",
                                r.get("posted") or ""))

    print(f"신규: {len(new_items)}건")

    if not args.dry_run:
        notify.send(new_items, cfg)
        save_state(state)

    renderer.render(records, BOARD_PATH)
    print(f"보드 갱신: {BOARD_PATH}")
    return 0


def cmd_probe_api(args) -> int:
    cfg = load_config()
    api = next((a for a in cfg.get("apis", []) if a["id"] == args.api_id), None)
    if not api:
        ids = ", ".join(a["id"] for a in cfg.get("apis", []))
        print(f"'{args.api_id}' 없음. 사용 가능: {ids}", file=sys.stderr)
        return 1
    session = collectors.make_session(cfg["fetch"])
    api = dict(api, enabled=True)          # probe 는 꺼져 있어도 돌려본다
    print(collectors.fetch_api(api, cfg, session, raw=True))
    print("\n※ 위 구조를 보고 config.yaml 의 result_path / field_map 을 맞추세요.")
    return 0


def cmd_probe_site(args) -> int:
    cfg = load_config()
    site = next((s for s in cfg["sites"] if s["id"] == args.site_id), None)
    if not site:
        print(f"'{args.site_id}' 를 config.yaml 에서 찾을 수 없습니다.", file=sys.stderr)
        return 1
    session = collectors.make_session(cfg["fetch"])
    result = collectors.probe_site(site, cfg, session)
    print(f"{site['name']} ({site['url']})")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_probe_links(args) -> int:
    """게시판 링크의 원본 속성을 덤프한다. detail_url 틀을 만들 때 사용."""
    cfg = load_config()
    site = next((s for s in cfg["sites"] if s["id"] == args.site_id), None)
    if not site:
        print(f"'{args.site_id}' 를 config.yaml 에서 찾을 수 없습니다.", file=sys.stderr)
        return 1
    session = collectors.make_session(cfg["fetch"])
    rows = collectors.probe_links(site, cfg, session)
    print(f"{site['name']} ({site['url']})\n")
    for row in rows:
        print(f"제목 : {row['title']}")
        print(f"href : {row['href']}")
        if row["onclick"]:
            print(f"onclick: {row['onclick']}")
        if row["data_attrs"]:
            print(f"data : {row['data_attrs']}")
        print(f"추출된 번호: {row['extracted_id'] or '(없음)'}")
        print("-" * 50)
    if not rows:
        print("공고로 인정된 링크가 없습니다.")
    return 0


def cmd_render(args) -> int:
    state = load_state()
    records = list(state.values())
    for r in records:
        r["is_new"] = False
    records.sort(key=lambda r: (-r.get("tier", 0), r.get("deadline") or "9999"))
    renderer.render(records, BOARD_PATH)
    print(f"보드 갱신: {BOARD_PATH} ({len(records)}건)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="금융권 채용 공고 감시기")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="수집하고 알림을 보낸다")
    p_run.add_argument("--dry-run", action="store_true",
                       help="알림·상태저장 없이 결과만 확인")
    p_run.set_defaults(func=cmd_run)

    p_api = sub.add_parser("probe-api", help="오픈API 원문 확인 (alio / worknet)")
    p_api.add_argument("api_id")
    p_api.set_defaults(func=cmd_probe_api)

    p_site = sub.add_parser("probe-site", help="사이트 구조 판별")
    p_site.add_argument("site_id")
    p_site.set_defaults(func=cmd_probe_site)

    p_links = sub.add_parser("probe-links", help="게시판 링크 원본 확인")
    p_links.add_argument("site_id")
    p_links.set_defaults(func=cmd_probe_links)

    p_render = sub.add_parser("render", help="보드만 다시 그린다")
    p_render.set_defaults(func=cmd_render)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
