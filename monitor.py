import json
import os
import sys
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

import requests

JST = timezone(timedelta(hours=9))

# bot識別情報を含めつつ、サイト側で弾かれないUA
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; pokeca-monitor/1.0; +https://github.com/9mak/pokeca-monitor)"
}

PAGES = [
    {"name": "ヨドバシ",     "post_id": 78763, "slug": "yodobashi", "color": "#E60012", "header_text_color": "#FFFFFF"},
    {"name": "ビックカメラ", "post_id": 78776, "slug": "biccamera", "color": "#E50012", "header_text_color": "#FFFFFF"},
    {"name": "ポケセン",     "post_id": 78787, "slug": "pokesen",   "color": "#FFCB05", "header_text_color": "#1B1B1B"},
    {"name": "量販店",       "post_id": 50167, "slug": "stores",    "color": "#3B82F6", "header_text_color": "#FFFFFF"},
]

WP_BASE = "https://gamenv.net/tc/wp-json/wp/v2"
PAGE_BASE = "https://gamenv.net/tc"

LINE_API = "https://api.line.me/v2/bot/message/broadcast"

TEXT_LIMIT = 1000


def safe_error_str(e: Exception) -> str:
    """例外を安全な文字列に変換する。

    requests.HTTPErrorのstr(e)はリクエストヘッダ（Authorization: Bearer ...）を
    含む可能性があるため、HTTPステータスコードのみを抽出する。
    """
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code} {e.response.reason}"
    if isinstance(e, requests.Timeout):
        return "Timeout"
    if isinstance(e, requests.ConnectionError):
        return "ConnectionError"
    # その他は例外クラス名のみ（メッセージは含めない）
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
    """指定postの最新50件のコメントを取得（フィルタなし）。"""
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


def format_time(date_str: str) -> str:
    dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).astimezone(JST)
    return dt.strftime("%-m/%-d %H:%M")


def build_flex_bubble(page: dict, comment: dict) -> dict:
    html = comment["content"]["rendered"]
    body = strip_html(html)
    if len(body) > TEXT_LIMIT:
        body = body[:TEXT_LIMIT] + "…"
    time_str = format_time(comment["date_gmt"])
    is_reply = comment.get("parent", 0) > 0
    header_label = f"↩ {page['name']}掲示板（返信）" if is_reply else f"💬 {page['name']}掲示板"
    link = comment.get("link") or f"{PAGE_BASE}/{page['slug']}/#comment-{comment['id']}"

    header_text_color = page["header_text_color"]
    sub_color = "#1B1B1BAA" if header_text_color == "#1B1B1B" else "#FFFFFFCC"

    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": page["color"],
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": header_label, "color": header_text_color, "weight": "bold", "size": "md"},
                {"type": "text", "text": time_str, "color": sub_color, "size": "xs", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": body or "（本文なし）", "wrap": True, "size": "md", "color": "#1B1B1B"},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": page["color"],
                    "action": {"type": "uri", "label": "サイトで見る", "uri": link},
                }
            ],
        },
    }


def send_line_messages(token: str, messages: list[dict]) -> None:
    """1〜5件のmessageオブジェクトを Bot 友達全員にブロードキャスト送信"""
    requests.post(
        LINE_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"messages": messages},
        timeout=10,
    ).raise_for_status()


def notify_comment(token: str, page: dict, comment: dict) -> None:
    """1コメント = 1通知（Flex Message + 画像最大4枚を1リクエストにまとめる）"""
    bubble = build_flex_bubble(page, comment)
    is_reply = comment.get("parent", 0) > 0
    alt_prefix = "↩" if is_reply else "💬"
    alt_text = f"{alt_prefix} {page['name']}掲示板に新着コメント"

    messages: list[dict] = [{
        "type": "flex",
        "altText": alt_text,
        "contents": bubble,
    }]
    images = extract_images(comment["content"]["rendered"])
    for img_url in images[:4]:  # LINEは1リクエスト最大5メッセージ
        messages.append({
            "type": "image",
            "originalContentUrl": img_url,
            "previewImageUrl": img_url,
        })
    send_line_messages(token, messages)


def main() -> None:
    line_token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    gist_token = os.environ["GIST_TOKEN"]
    gist_id = os.environ["GIST_ID"]

    state = load_state(gist_token, gist_id)
    last_ids: dict[str, int] = state.get("last_comment_ids", {})

    state_updated = False

    for page in PAGES:
        post_id = page["post_id"]
        since_id = last_ids.get(str(post_id), 0)

        try:
            all_comments = fetch_comments(post_id)
        except Exception as e:
            print(f"[{page['name']}] fetch error: {safe_error_str(e)}", file=sys.stderr)
            continue

        new_comments = [c for c in all_comments if c["id"] > since_id]
        if not new_comments:
            continue

        notified = 0
        for c in sorted(new_comments, key=lambda x: x["id"]):
            try:
                notify_comment(line_token, page, c)
                notified += 1
                last_ids[str(post_id)] = c["id"]
                state_updated = True
            except Exception as e:
                print(f"[{page['name']}] LINE send error (comment {c['id']}): {safe_error_str(e)}", file=sys.stderr)
                break  # 失敗したらこのページは中断（次回再試行できるよう状態保存）

        if notified:
            print(f"[{page['name']}] {notified}件通知 (max_id={last_ids[str(post_id)]})")

    if state_updated:
        state["last_comment_ids"] = last_ids
        save_state(gist_token, gist_id, state)


if __name__ == "__main__":
    main()
