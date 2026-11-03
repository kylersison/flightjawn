"""Japan fare alert bot.

Polls flight-deal RSS feeds, keeps only LAX -> Tokyo/Osaka deals under your
price limits, and posts them to a Discord channel through a webhook.

Usage:
    python bot.py               check feeds once and post new deals
    python bot.py --dry-run     show what would be posted (no Discord, no saving)
    python bot.py --test        send a fake error-fare alert to check the webhook
    python bot.py --loop 10     keep running, checking every 10 minutes
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

import config

SEEN_FILE = Path(__file__).with_name("seen.json")
USER_AGENT = "japan-fare-bot/1.0 (personal hobby project; polls every 10 min)"


# ---------------------------------------------------------------- matching --

def _word_rx(words: list[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


ORIGIN_RX = _word_rx([w for ws in config.ORIGINS.values() for w in ws])
DEST_RX = {code: _word_rx(ws) for code, ws in config.DESTINATIONS.items()}
COUNTRY_RX = _word_rx(config.COUNTRY_WORDS)
ERROR_RX = _word_rx(config.ERROR_FARE_WORDS)
PRICE_RX = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)")
FOREIGN_CURRENCY_RX = re.compile(r"\b(CAD|AUD|NZD|HKD|SGD|EUR|GBP|JPY|MXN)\b")
ONEWAY_RX = re.compile(r"\bone[\s-]?way\b", re.I)
EXPIRED_RX = re.compile(r"\b(fare gone|expired|no longer available|sold out)\b", re.I)


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def classify(title: str, summary: str = "") -> dict | None:
    """Return alert details if this post is a deal we care about, else None."""
    title = strip_html(title)
    summary = strip_html(summary)

    # 0. Skip deals the site has already marked as dead.
    if EXPIRED_RX.search(title):
        return None

    # 1. Destination: named in the title, or the title says "Japan" and the
    #    body names Tokyo/Osaka/etc.
    dests = [code for code, rx in DEST_RX.items() if rx.search(title)]
    if not dests and COUNTRY_RX.search(title):
        dests = [code for code, rx in DEST_RX.items() if rx.search(summary)]
    if not dests:
        return None

    # 2. Origin: in the title (and before the destination, so we skip
    #    "Tokyo to Los Angeles"), or listed in the body for multi-city posts.
    origin = ORIGIN_RX.search(title)
    if origin:
        first_dest = min(
            (m.start() for rx in DEST_RX.values() if (m := rx.search(title))),
            default=len(title),
        )
        if first_dest < origin.start():
            return None
    elif not ORIGIN_RX.search(summary):
        return None

    # 3. Price and trip type (from the title only; bodies list many cities).
    price = None
    if not FOREIGN_CURRENCY_RX.search(title):
        prices = [int(p.replace(",", "")) for p in PRICE_RX.findall(title)]
        price = min(prices) if prices else None
    trip = "oneway" if ONEWAY_RX.search(title) else "roundtrip"
    is_error = bool(ERROR_RX.search(title) or ERROR_RX.search(summary))

    # 4. Tier.
    if is_error:
        tier = "error"
    elif price is None:
        if not config.ALERT_IF_NO_PRICE:
            return None
        tier = "deal"
    elif price <= config.GREAT_PRICE[trip]:
        tier = "great"
    elif price <= config.MAX_PRICE[trip]:
        tier = "good"
    else:
        return None

    return {"tier": tier, "price": price, "trip": trip, "dests": dests}


# ------------------------------------------------------------------- feeds --

def fetch_feed(name: str, url: str) -> list:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[warn] {name}: could not fetch feed ({exc})", file=sys.stderr)
        return []
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        print(f"[warn] {name}: response wasn't a readable RSS feed", file=sys.stderr)
    return parsed.entries


def entry_key(entry) -> str:
    return entry.get("id") or entry.get("link") or entry.get("title", "")


def entry_time(entry) -> str | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc).isoformat()


# ----------------------------------------------------------------- discord --

STYLE = {
    "error": (0xE53935, "🚨 Possible ERROR FARE: book fast, these die within hours"),
    "great": (0xFB8C00, "🔥 Great price"),
    "good": (0x43A047, "✅ Good price"),
    "deal": (0x1E88E5, "✈️ New deal (couldn't read the price, check the post)"),
}


def build_message(entry, source: str, info: dict) -> dict:
    color, headline = STYLE[info["tier"]]
    summary = strip_html(entry.get("summary", ""))
    if len(summary) > 300:
        summary = summary[:297].rsplit(" ", 1)[0] + "…"
    trip_label = "one-way" if info["trip"] == "oneway" else "round trip"
    price = f"${info['price']:,} {trip_label}" if info["price"] else "See post"

    embed = {
        "title": strip_html(entry.get("title", "Flight deal"))[:256],
        "url": entry.get("link"),
        "description": f"**{headline}**\n{summary}",
        "color": color,
        "fields": [
            {"name": "Route", "value": "LAX → " + ", ".join(info["dests"]), "inline": True},
            {"name": "Price", "value": price, "inline": True},
            {"name": "Source", "value": source, "inline": True},
        ],
        "footer": {"text": "Double-check on Google Flights before booking"},
    }
    if ts := entry_time(entry):
        embed["timestamp"] = ts

    mention = ""
    if info["tier"] == "error":
        mention = os.environ.get("ERROR_FARE_MENTION", config.ERROR_FARE_MENTION)
    return {
        "username": config.BOT_NAME,
        "content": mention,
        "allowed_mentions": {"parse": ["everyone", "roles"] if mention else []},
        "embeds": [embed],
    }


def post_to_discord(webhook: str, payload: dict) -> None:
    for _ in range(4):
        resp = requests.post(webhook, json=payload, timeout=15)
        if resp.status_code == 429:  # rate limited: wait as told, then retry
            time.sleep(float(resp.json().get("retry_after", 2)) + 0.5)
            continue
        resp.raise_for_status()
        time.sleep(1)  # stay well under Discord's webhook limits
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


# -------------------------------------------------------------------- seen --

def load_seen() -> dict | None:
    if not SEEN_FILE.exists():
        return None
    try:
        return json.loads(SEEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        print("[warn] seen.json was unreadable; starting fresh", file=sys.stderr)
        return {}


def save_seen(seen: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.SEEN_RETENTION_DAYS)
    kept = {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= cutoff}
    SEEN_FILE.write_text(json.dumps(kept, indent=1, sort_keys=True) + "\n")


# --------------------------------------------------------------------- run --

def run_once(webhook: str | None, dry_run: bool, post_existing: bool) -> None:
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    now = datetime.now(timezone.utc).isoformat()
    posted = matched = 0

    for source, url in config.FEEDS.items():
        entries = fetch_feed(source, url)
        print(f"{source}: {len(entries)} posts in feed")
        for entry in entries:
            key = entry_key(entry)
            if not dry_run and key in seen:
                continue
            info = classify(entry.get("title", ""), entry.get("summary", ""))
            if info:
                matched += 1
                label = f"[{info['tier']}] {strip_html(entry.get('title', ''))}"
                if dry_run:
                    print("  would post:", label)
                elif first_run and not post_existing:
                    print("  (first run, not posting old deal):", label)
                else:
                    post_to_discord(webhook, build_message(entry, source, info))
                    posted += 1
                    print("  posted:", label)
            seen[key] = now

    if dry_run:
        print(f"Dry run: {matched} matching deal(s); nothing posted or saved.")
        return
    save_seen(seen)
    if first_run and not post_existing:
        print("First run: remembered current posts; only new deals will be posted from now on.")
    print(f"Done: {posted} posted.")


def send_test(webhook: str) -> None:
    entry = {
        "title": "TEST: Los Angeles to Osaka, Japan for only $300 USD roundtrip (error fare)",
        "link": "https://www.google.com/travel/flights",
        "summary": "This is a test alert from your Japan fare bot. If you can read this, the webhook works!",
    }
    info = classify(entry["title"], entry["summary"])
    post_to_discord(webhook, build_message(entry, "Test", info))
    print("Test alert sent. Check your Discord channel.")


def load_env_file() -> None:
    """Load KEY=value lines from a .env file next to bot.py, if there is one.

    Values here win over the terminal's environment, so a stale variable in
    your shell can't override the webhook you saved. (.env is gitignored.)
    """
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ[key.strip()] = value


def main() -> None:
    ap = argparse.ArgumentParser(description="LAX → Japan flight deal alerts for Discord")
    ap.add_argument("--dry-run", action="store_true", help="print matches, don't post or save")
    ap.add_argument("--test", action="store_true", help="send a fake alert to the webhook")
    ap.add_argument("--loop", type=int, metavar="MIN", help="keep running, checking every MIN minutes")
    ap.add_argument("--post-existing", action="store_true",
                    help="on the very first run, post deals already in the feeds")
    args = ap.parse_args()

    load_env_file()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not args.dry_run and not re.match(r"https://(\w+\.)?discord(app)?\.com/api/webhooks/", webhook):
        sys.exit("No Discord webhook found. Paste your webhook URL into the .env file "
                 "(DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...) or use --dry-run.")

    if args.test:
        send_test(webhook)
        return
    while True:
        try:
            run_once(webhook, args.dry_run, args.post_existing)
        except Exception as exc:  # keep the loop alive through one bad run
            if not args.loop:
                raise
            print(f"[error] {exc}", file=sys.stderr)
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
