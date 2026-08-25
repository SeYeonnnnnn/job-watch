"""알림 발송. 텔레그램이 기본, 실패하면 콘솔 출력으로 조용히 떨어진다.

알림이 실패해도 수집과 대시보드 갱신은 계속되어야 하므로
여기서 예외를 밖으로 던지지 않는다.
"""

from __future__ import annotations

import os
import textwrap

import requests

TIER_ICON = {3: "●", 2: "○", 1: "·"}


def build_message(new_items: list[dict], limit: int = 15) -> str:
    if not new_items:
        return ""

    head = f"새 공고 {len(new_items)}건"
    lines = [head, ""]

    for rec in new_items[:limit]:
        icon = TIER_ICON.get(rec.get("tier", 1), "·")
        deadline = rec.get("deadline") or "상시"
        title = textwrap.shorten(rec.get("title", ""), width=70, placeholder="…")
        lines.append(f"{icon} [{rec.get('org', '')}] {title}")
        lines.append(f"   ~{deadline} · {rec.get('reason', '')}")
        if rec.get("url"):
            lines.append(f"   {rec['url']}")
        lines.append("")

    if len(new_items) > limit:
        lines.append(f"…외 {len(new_items) - limit}건은 보드에서 확인하세요.")

    return "\n".join(lines)


def send(new_items: list[dict], cfg: dict) -> bool:
    """True면 실제 발송 성공. False면 콘솔로만 출력됨."""
    message = build_message(new_items)
    if not message:
        return True

    tg = cfg.get("notify", {}).get("telegram", {})
    token = os.environ.get(tg.get("token_env", ""), "")
    chat_id = os.environ.get(tg.get("chat_id_env", ""), "")

    if not (tg.get("enabled") and token and chat_id):
        print("[알림] 텔레그램 설정이 없어 콘솔에만 출력합니다.\n")
        print(message)
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:            # noqa: BLE001 — 알림 실패로 파이프라인을 죽이지 않는다
        print(f"[알림] 텔레그램 전송 실패: {exc}\n")
        print(message)
        return False
