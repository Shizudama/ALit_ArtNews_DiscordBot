"""
Discord Scout Bot
- Bluesky と RSS から指定キーワードを含む投稿/記事を収集
- いいね/リポスト数の閾値で「影響力のある投稿」だけに絞り込み
- 重複を除外して Discord に通知
"""

import os
import sys
import json
import time
import datetime as dt
from pathlib import Path
from typing import Iterable

import requests
import feedparser
import yaml

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.yaml"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")
if not DISCORD_WEBHOOK:
    print("ERROR: DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
    sys.exit(1)

BSKY_SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"


# ---------- Config & State ----------

def load_config() -> dict:
    with CONFIG_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    return {"seen": []}


def save_state(state: dict) -> None:
    # 古いIDを切り捨ててファイルが肥大化しないようにする
    state["seen"] = state["seen"][-5000:]
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- Bluesky ----------

def search_bluesky(query: str, since_iso: str, lang: str | None = None) -> list[dict]:
    q = f"{query} lang:{lang}" if lang else query
    params = {"q": q, "sort": "latest", "limit": 100, "since": since_iso}
    try:
        r = requests.get(BSKY_SEARCH_URL, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("posts", [])
    except Exception as e:
        print(f"[WARN] Bluesky search failed for '{query}': {e}", file=sys.stderr)
        return []


def is_bsky_influential(post: dict, like_min: int, repost_min: int) -> bool:
    likes = post.get("likeCount", 0)
    reposts = post.get("repostCount", 0)
    return likes >= like_min or reposts >= repost_min


def format_bsky_embed(post: dict, matched: list[str]) -> dict:
    record = post.get("record", {}) or {}
    author = post.get("author", {}) or {}
    text = record.get("text", "") or ""
    handle = author.get("handle", "unknown")
    display = author.get("displayName") or handle
    rkey = (post.get("uri", "") or "").rsplit("/", 1)[-1]
    web_url = f"https://bsky.app/profile/{handle}/post/{rkey}"

    return {
        "author": {"name": f"{display} (@{handle})", "url": f"https://bsky.app/profile/{handle}"},
        "description": text[:1500],
        "url": web_url,
        "color": 0x1185FE,  # Bluesky brand
        "footer": {
            "text": f"Bluesky | ❤️ {post.get('likeCount', 0)} 🔁 {post.get('repostCount', 0)} 💬 {post.get('replyCount', 0)} | {', '.join(matched)}"
        },
        "timestamp": record.get("createdAt"),
    }


# ---------- RSS ----------

def fetch_rss(url: str) -> list[dict]:
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"[WARN] RSS parse failed for {url}: {e}", file=sys.stderr)
        return []

    source_title = feed.feed.get("title", url)
    items = []
    for e in feed.entries:
        items.append({
            "id": e.get("id") or e.get("link") or "",
            "title": e.get("title", ""),
            "summary": e.get("summary", "") or e.get("description", ""),
            "link": e.get("link", ""),
            "published": e.get("published", "") or e.get("updated", ""),
            "source": source_title,
        })
    return items


def format_rss_embed(item: dict, matched: list[str]) -> dict:
    # HTMLタグの簡易除去
    import re
    summary = re.sub(r"<[^>]+>", "", item.get("summary", "") or "")
    return {
        "title": (item.get("title") or "")[:256],
        "description": summary[:500],
        "url": item.get("link", ""),
        "color": 0xFF8C00,
        "footer": {"text": f"RSS: {item['source']} | {', '.join(matched)}"},
    }


# ---------- Matching ----------

def match_keywords(text: str, keywords: list[dict]) -> list[str]:
    """テキストにマッチしたキーワードのリストを返す"""
    if not text:
        return []
    text_lower = text.lower()
    matched = []
    for kw in keywords:
        word = kw["word"]
        if word.lower() in text_lower:
            matched.append(word)
    return matched


def threshold_for(matched: list[str], keywords: list[dict]) -> tuple[int, int]:
    """マッチしたキーワードのうち最も低い閾値を採用する(複数マッチ時に厳しすぎないように)"""
    kw_map = {k["word"]: k for k in keywords}
    likes = [kw_map[m].get("like_threshold", 30) for m in matched if m in kw_map]
    reposts = [kw_map[m].get("repost_threshold", 5) for m in matched if m in kw_map]
    return (min(likes) if likes else 30, min(reposts) if reposts else 5)


# ---------- Discord ----------

def post_to_discord(embeds: list[dict]) -> None:
    """1リクエストあたり最大10 embedまで送信可能"""
    for i in range(0, len(embeds), 10):
        batch = embeds[i:i + 10]
        payload = {"embeds": batch, "username": "Scout"}
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=30)
        if r.status_code == 429:
            retry = float(r.headers.get("Retry-After", "5"))
            print(f"[INFO] rate limited, sleep {retry}s")
            time.sleep(retry + 1)
            r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=30)
        r.raise_for_status()
        time.sleep(1)  # ゆるくスロットル


# ---------- Main ----------

def main() -> int:
    cfg = load_config()
    state = load_state()
    seen: set[str] = set(state.get("seen", []))

    keywords: list[dict] = cfg["keywords"]
    rss_feeds: list[str] = cfg.get("rss_feeds", [])
    lookback_hours: int = cfg.get("lookback_hours", 6)
    max_posts_per_run: int = cfg.get("max_posts_per_run", 30)
    bsky_lang: str | None = cfg.get("bluesky_lang", "ja")

    since_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    new_embeds: list[tuple[dt.datetime, dict]] = []  # (時刻, embed) で後でソート

    # === Bluesky ===
    for kw in keywords:
        word = kw["word"]
        posts = search_bluesky(word, since_iso, lang=bsky_lang)
        for post in posts:
            uri = post.get("uri")
            if not uri or uri in seen:
                continue
            seen.add(uri)

            text = (post.get("record") or {}).get("text", "") or ""
            matched = match_keywords(text, keywords)
            if not matched:
                continue

            like_min, repost_min = threshold_for(matched, keywords)
            if not is_bsky_influential(post, like_min, repost_min):
                continue

            created_str = (post.get("record") or {}).get("createdAt", "")
            try:
                created_at = dt.datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except Exception:
                created_at = dt.datetime.now(dt.timezone.utc)

            embed = format_bsky_embed(post, matched)
            new_embeds.append((created_at, embed))
            print(f"[HIT] Bluesky: {uri} (likes={post.get('likeCount')}, kw={matched})")

    # === RSS ===
    for feed_url in rss_feeds:
        items = fetch_rss(feed_url)
        for item in items:
            item_id = item.get("id") or item.get("link")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)

            text = (item.get("title") or "") + " " + (item.get("summary") or "")
            matched = match_keywords(text, keywords)
            if not matched:
                continue

            # RSSは公開日時(あれば)でソート用の時刻を決める
            published = dt.datetime.now(dt.timezone.utc)

            embed = format_rss_embed(item, matched)
            new_embeds.append((published, embed))
            print(f"[HIT] RSS: {item.get('link')} (kw={matched})")

    # 新しい順にソートし、上限でカット
    new_embeds.sort(key=lambda x: x[0], reverse=True)
    embeds_to_send = [e for _, e in new_embeds[:max_posts_per_run]]

    if embeds_to_send:
        try:
            post_to_discord(embeds_to_send)
            print(f"[DONE] Posted {len(embeds_to_send)} items")
        except Exception as e:
            print(f"[ERROR] Discord post failed: {e}", file=sys.stderr)
            # 通知に失敗しても state は更新する(同じ内容を何度も再投稿しないため)
    else:
        print("[DONE] No new items")

    state["seen"] = list(seen)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
