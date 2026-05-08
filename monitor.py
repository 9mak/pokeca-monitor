import json
import os
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; pokeca-monitor/1.0; +https://github.com/9mak/pokeca-monitor)"
}

PAGES = [
    {"name": "ヨドバシ",     "post_id": 78763, "slug": "yodobashi", "color": "#E60012"},
    {"name": "ビックカメラ", "post_id": 78776, "slug": "biccamera", "color": "#E50012"},
    {"name": "ポケセン",     "post_id": 78787, "slug": "pokesen",   "color": "#FFCB05"},
    {"name": "量販店",       "post_id": 50167, "slug": "stores",    "color": "#3B82F6"},
]

WP_BASE = "https://gamenv.net/tc/wp-json/wp/v2"
PAGE_BASE = "https://gamenv.net/tc"

DESC_LIMIT = 4000
TITLE_LIMIT = 256
WEBHOOK_USERNAME = "pokeca-monitor"

# Discord webhook の rate limit (チャンネル5req/sec, webhook 30req/min) 用
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_RETRY_CAP_SEC = 30


def safe_error_str(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code} {e.response.reason}"
    if isinstance(e, requests.Timeout):
        return "Timeout"
    if isinstance(e, requests.ConnectionError):
        return "ConnectionError"
    return type(e).__name__


def load_state(gist_token: str, gist_id: str) -> dict:
    res = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"token {gist_token}"},
        timeout=10,
    )
    res.raise_for_status()
    files = res.json().get("files", {})
    state_file = files.get("state.json")
    if not state_file or not state_file.get("content"):
        return {"last_comment_ids": {}}
    try:
        return json.loads(state_file["content"])
    except json.JSONDecodeError:
        return {"last_comment_ids": {}}


def save_state(gist_token: str, gist_id: str, state: dict) -> None:
    requests.patch(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"token {gist_token}"},
        json={"files": {"state.json": {"content": json.dumps(state, ensure_ascii=False)}}},
        timeout=10,
    ).raise_for_status()


def fetch_comments(post_id: int) -> list[dict]:
    url = f"{WP_BASE}/comments?post={post_id}&per_page=50&orderby=date&order=desc"
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    return res.json()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs):  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "br":
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in ("p", "div", "li"):
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks).strip()


def strip_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.get_text()


class _ImageExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag != "img":
            return
        for k, v in attrs:
            if k == "src" and v and v.startswith("https://"):
                self.urls.append(v)
                break


def extract_images(html: str) -> list[str]:
    """img タグから https:// プレフィックスのみのURLを抽出（SSRF対策）。"""
    parser = _ImageExtractor()
    parser.feed(html)
    parser.close()
    return parser.urls


def hex_to_int(color: str) -> int:
    return int(color.lstrip("#"), 16)


def to_iso_utc(date_str: str) -> str:
    return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).isoformat()


def build_comment_embed(page: dict, comment: dict) -> dict:
    html = comment["content"]["rendered"]
    body = strip_html(html)
    if len(body) > DESC_LIMIT:
        body = body[:DESC_LIMIT] + "…"
    is_reply = comment.get("parent", 0) > 0
    title_prefix = "↩" if is_reply else "💬"
    suffix = "（返信）" if is_reply else ""
    title = f"{title_prefix} {page['name']}掲示板{suffix}"[:TITLE_LIMIT]
    link = comment.get("link") or f"{PAGE_BASE}/{page['slug']}/#comment-{comment['id']}"

    embed: dict = {
        "title": title,
        "description": body or "（本文なし）",
        "url": link,
        "color": hex_to_int(page["color"]),
        "timestamp": to_iso_utc(comment["date_gmt"]),
    }
    images = extract_images(html)
    if images:
        embed["image"] = {"url": images[0]}
        if len(images) > 1:
            embed["footer"] = {"text": f"画像 他{len(images) - 1}枚はサイトで確認"}
    return embed


def build_error_embed(errors: list[str]) -> dict:
    body = "\n".join(f"• {e}" for e in errors)
    if len(body) > DESC_LIMIT:
        body = body[:DESC_LIMIT] + "…"
    return {
        "title": f"⚠ pokeca-monitor エラー ({len(errors)}件)",
        "description": body,
        "color": hex_to_int("#9CA3AF"),
    }


def send_webhook(webhook_url: str, embeds: list[dict]) -> None:
    """1〜10個の embed を1リクエストで送信。429 は Retry-After に従って再送。"""
    payload = {"username": WEBHOOK_USERNAME, "embeds": embeds}

    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        res = requests.post(webhook_url, json=payload, timeout=10)
        if res.status_code != 429:
            res.raise_for_status()
            return
        if attempt == RATE_LIMIT_MAX_RETRIES:
            res.raise_for_status()
            return
        retry_after = float(res.headers.get("Retry-After", "1"))
        time.sleep(min(retry_after, RATE_LIMIT_RETRY_CAP_SEC))


def notify_comment(webhook_url: str, page: dict, comment: dict) -> None:
    send_webhook(webhook_url, [build_comment_embed(page, comment)])


def notify_error(webhook_url: str, errors: list[str]) -> None:
    send_webhook(webhook_url, [build_error_embed(errors)])


def main() -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    gist_token = os.environ["GIST_TOKEN"]
    gist_id = os.environ["GIST_ID"]

    state = load_state(gist_token, gist_id)
    last_ids: dict[str, int] = state.get("last_comment_ids", {})

    state_updated = False
    errors: list[str] = []

    for page in PAGES:
        post_id = page["post_id"]
        since_id = last_ids.get(str(post_id), 0)

        try:
            all_comments = fetch_comments(post_id)
        except Exception as e:
            msg = f"[{page['name']}] fetch error: {safe_error_str(e)}"
            print(msg, file=sys.stderr)
            errors.append(msg)
            continue

        new_comments = [c for c in all_comments if c["id"] > since_id]
        if not new_comments:
            continue

        ordered = sorted(new_comments, key=lambda x: x["id"])

        notified = 0
        for c in ordered:
            try:
                notify_comment(webhook_url, page, c)
                notified += 1
            except Exception as e:
                msg = f"[{page['name']}] webhook send error (comment {c['id']}): {safe_error_str(e)}"
                print(msg, file=sys.stderr)
                errors.append(msg)
            last_ids[str(post_id)] = c["id"]
            state_updated = True

        if notified:
            print(f"[{page['name']}] {notified}件通知 (max_id={last_ids[str(post_id)]})")

    if state_updated:
        state["last_comment_ids"] = last_ids
        save_state(gist_token, gist_id, state)

    if errors:
        try:
            notify_error(webhook_url, errors)
        except Exception as e:
            print(f"error notify failed: {safe_error_str(e)}", file=sys.stderr)


if __name__ == "__main__":
    main()
