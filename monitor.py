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


def build_message(page: dict, new_comments: list[dict], all_comments: list[dict]) -> tuple[str, list[str]]:
    """テキストメッセージと画像URLリストを返す"""
    comment_map = {c["id"]: c for c in all_comments}
    count = len(new_comments)
    page_url = f"{PAGE_BASE}/{page['slug']}/"
    lines = [f"【{page['name']}掲示板】{count}件の新着\n"]

    images: list[str] = []

    for c in sorted(new_comments, key=lambda x: x["id"]):
        html = c["content"]["rendered"]
        text = strip_html(html)[:100]
        time_str = format_time(c["date"])
        parent_id = c.get("parent", 0)
        comment_url = f"{page_url}#comment-{c['id']}"

        if parent_id and parent_id in comment_map:
            parent_text = strip_html(comment_map[parent_id]["content"]["rendered"])[:30]
            lines.append(f"  ↩ 匿名 {time_str}（返信）")
            lines.append(f"  ← {parent_text}...")
            lines.append(f"  {text}")
            lines.append(f"  🔗 {comment_url}")
        else:
            lines.append(f"💬 匿名 {time_str}")
            lines.append(text)
            lines.append(f"🔗 {comment_url}")

        imgs = extract_images(html)
        images.extend(imgs)
        lines.append("")

    return "\n".join(lines).strip(), images


def send_line_text(token: str, user_id: str, text: str) -> None:
    requests.post(
        LINE_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "to": user_id,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=10,
    ).raise_for_status()


def send_line_image(token: str, user_id: str, image_url: str) -> None:
    requests.post(
        LINE_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "to": user_id,
            "messages": [{
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            }],
        },
        timeout=10,
    ).raise_for_status()


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

        try:
            text_msg, images = build_message(page, new_comments, all_comments)
            send_line_text(line_token, line_user_id, text_msg)
            for img_url in images[:3]:  # 最大3枚
                send_line_image(line_token, line_user_id, img_url)
        except Exception as e:
            print(f"[{page['name']}] LINE send error: {e}", file=sys.stderr)
            continue

        max_id = max(c["id"] for c in new_comments)
        last_ids[str(post_id)] = max_id
        state_updated = True
        print(f"[{page['name']}] {len(new_comments)}件通知 (max_id={max_id})")

    if state_updated:
        state["last_comment_ids"] = last_ids
        save_state(gist_token, gist_id, state)


if __name__ == "__main__":
    main()
