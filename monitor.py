import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

PAGES = [
    {"name": "ヨドバシ",     "post_id": 78763, "slug": "yodobashi"},
    {"name": "ビックカメラ", "post_id": 78776, "slug": "biccamera"},
    {"name": "ポケセン",     "post_id": 78787, "slug": "pokesen"},
    {"name": "量販店",       "post_id": 50167, "slug": "stores"},
]

WP_BASE = "https://gamenv.net/tc/wp-json/wp/v2"
PAGE_BASE = "https://gamenv.net/tc"

LINE_API = "https://api.line.me/v2/bot/message/push"


def load_state(gist_token: str, gist_id: str) -> dict:
    res = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"token {gist_token}"},
        timeout=10,
    )
    res.raise_for_status()
    content = res.json()["files"]["state.json"]["content"]
    return json.loads(content)


def save_state(gist_token: str, gist_id: str, state: dict) -> None:
    requests.patch(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"token {gist_token}"},
        json={"files": {"state.json": {"content": json.dumps(state, ensure_ascii=False)}}},
        timeout=10,
    ).raise_for_status()


def fetch_comments(post_id: int, since_id: int) -> list[dict]:
    url = f"{WP_BASE}/comments?post={post_id}&per_page=50&orderby=date&order=desc"
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    comments = res.json()
    return [c for c in comments if c["id"] > since_id]


def strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#039;", "'")
    return text.strip()


def extract_images(html: str) -> list[str]:
    return re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html)


def format_time(date_str: str) -> str:
    dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).astimezone(JST)
    return dt.strftime("%H:%M")


TEXT_LIMIT = 1000


def build_comment_text(page: dict, comment: dict) -> str:
    html = comment["content"]["rendered"]
    body = strip_html(html)
    if len(body) > TEXT_LIMIT:
        body = body[:TEXT_LIMIT] + "…"
    time_str = format_time(comment["date"])
    is_reply = comment.get("parent", 0) > 0
    header = f"↩ {page['name']}掲示板（返信） {time_str}" if is_reply else f"💬 {page['name']}掲示板 {time_str}"
    link = comment.get("link") or f"{PAGE_BASE}/{page['slug']}/#comment-{comment['id']}"
    return f"{header}\n\n{body}\n\n🔗 {link}"


def send_line_messages(token: str, user_id: str, messages: list[dict]) -> None:
    """1〜5件のmessageオブジェクトをまとめて1リクエストで送る"""
    requests.post(
        LINE_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"to": user_id, "messages": messages},
        timeout=10,
    ).raise_for_status()


def notify_comment(token: str, user_id: str, page: dict, comment: dict) -> None:
    """1コメント = 1通知（テキスト + 画像最大4枚を1リクエストにまとめる）"""
    text = build_comment_text(page, comment)
    images = extract_images(comment["content"]["rendered"])

    messages: list[dict] = [{"type": "text", "text": text}]
    for img_url in images[:4]:  # LINEは1リクエスト最大5メッセージ
        messages.append({
            "type": "image",
            "originalContentUrl": img_url,
            "previewImageUrl": img_url,
        })
    send_line_messages(token, user_id, messages)


def main() -> None:
    line_token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    line_user_id = os.environ["LINE_USER_ID"]
    gist_token = os.environ["GIST_TOKEN"]
    gist_id = os.environ["GIST_ID"]

    state = load_state(gist_token, gist_id)
    last_ids: dict[str, int] = state.get("last_comment_ids", {})

    state_updated = False

    for page in PAGES:
        post_id = page["post_id"]
        since_id = last_ids.get(str(post_id), 0)

        try:
            all_comments = fetch_comments(post_id, since_id=0)
            new_comments = [c for c in all_comments if c["id"] > since_id]
        except Exception as e:
            print(f"[{page['name']}] fetch error: {e}", file=sys.stderr)
            continue

        if not new_comments:
            continue

        notified = 0
        for c in sorted(new_comments, key=lambda x: x["id"]):
            try:
                notify_comment(line_token, line_user_id, page, c)
                notified += 1
                last_ids[str(post_id)] = c["id"]
                state_updated = True
            except Exception as e:
                print(f"[{page['name']}] LINE send error (comment {c['id']}): {e}", file=sys.stderr)
                break  # 失敗したらこのページは中断（次回再試行できるよう状態保存）

        if notified:
            print(f"[{page['name']}] {notified}件通知 (max_id={last_ids[str(post_id)]})")

    if state_updated:
        state["last_comment_ids"] = last_ids
        save_state(gist_token, gist_id, state)


if __name__ == "__main__":
    main()
