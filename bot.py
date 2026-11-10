"""Japan fare alert bot.

Polls flight-deal RSS feeds and Secret Flying's deal pages, keeps only LAX -> Tokyo/Osaka deals under your
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import feedparser
import requests

import config

SEEN_FILE = Path(__file__).with_name("seen.json")
HEALTH_FILE = Path(__file__).with_name("health.json")
# Many deal sites (Secret Flying included) sit behind bot protection that
# rejects unknown user agents like "japan-fare-bot/1.0", so ask like a browser.
FEED_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
IN_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


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


# ------------------------------------------------------------ travel dates --

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
MONTH_NUM = {}
for _i, _name in enumerate(MONTH_NAMES, 1):
    MONTH_NUM[_name.lower()] = _i
    MONTH_NUM[_name[:3].lower()] = _i
MONTH_NUM["sept"] = 9
_month_words = sorted(MONTH_NUM, key=len, reverse=True)
# Capitalized or ALL CAPS only, so "you may want to book" isn't read as May.
MONTH_RX = re.compile(
    r"\b(" + "|".join(w.capitalize() + "|" + w.upper() for w in _month_words) + r")\b\.?"
    r"(?:\s+\d{1,2}(?:st|nd|rd|th)?\b)?,?(?:\s+(20\d\d)\b)?"
)
RANGE_GAP_RX = re.compile(r"^\s*(?:-|–|—|to|through|thru|until|till)\s*$", re.I)
# Months right after these are deadlines or post dates, not travel dates.
NOT_TRAVEL_RX = re.compile(
    r"(book(?:ed)?\s+(?:by|before)|sale\s+ends|ends|expires?|valid\s+until|"
    r"posted|updated|published)\s*(?:on\s*)?$", re.I)


def _year_for(month: int, today: date) -> int:
    """A month with no year means its next occurrence (this month counts)."""
    return today.year if month >= today.month else today.year + 1


def travel_months(text: str, today: date | None = None) -> list[tuple[int, int]]:
    """Every (year, month) a post says you can travel in, sorted.

    Understands "December 2026", "Dec 3", "Jan – Mar 2027", "between
    December and March", and lists like "Dec, Jan, Jun". Months with no year
    are assumed to be the next time that month comes around.
    """
    today = today or date.today()
    hits = []
    for m in MONTH_RX.finditer(text):
        if NOT_TRAVEL_RX.search(text[max(0, m.start() - 25):m.start()]):
            continue
        hits.append((m.start(), m.end(), MONTH_NUM[m.group(1).lower()],
                     int(m.group(2)) if m.group(2) else None))

    months: set[tuple[int, int]] = set()
    i = 0
    while i < len(hits):
        _, end, mon, yr = hits[i]
        if i + 1 < len(hits):
            nstart, _, nmon, nyr = hits[i + 1]
            gap = text[end:nstart]
            is_range = RANGE_GAP_RX.match(gap) or (
                gap.strip().lower() == "and"
                and text[max(0, hits[i][0] - 9):hits[i][0]].lower().strip().endswith("between"))
            if is_range:
                if yr is None and nyr is not None:
                    yr = nyr if mon <= nmon else nyr - 1
                yr = yr or _year_for(mon, today)
                nyr = nyr or (yr if nmon >= mon else yr + 1)
                y, mo = yr, mon
                for _ in range(24):  # walk the range month by month
                    months.add((y, mo))
                    if (y, mo) >= (nyr, nmon):
                        break
                    y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
                i += 2
                continue
        months.add((yr or _year_for(mon, today), mon))
        i += 1
    return sorted(months)


def _ym(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    y, m = text.split("-")
    return int(y), int(m)


WINDOWS = [(_ym(a), _ym(b)) for a, b in config.TRAVEL_WINDOWS]


def in_travel_window(months: list[tuple[int, int]]) -> bool:
    if not WINDOWS:
        return True
    return any(start <= ym and (end is None or ym <= end)
               for ym in months for start, end in WINDOWS)


def format_months(months: list[tuple[int, int]]) -> str:
    """[(2026,12),(2027,1),(2027,6)] -> "Dec 2026 – Jan 2027, Jun 2027"."""
    def label(ym):
        return f"{MONTH_NAMES[ym[1] - 1][:3]} {ym[0]}"
    runs: list[list[tuple[int, int]]] = []
    for ym in months:
        if runs:
            py, pm = runs[-1][-1]
            if ym == ((py + 1, 1) if pm == 12 else (py, pm + 1)):
                runs[-1].append(ym)
                continue
        runs.append([ym])
    return ", ".join(label(r[0]) if len(r) == 1 else f"{label(r[0])} – {label(r[-1])}" for r in runs)


def parse_route(title: str, summary: str = "") -> dict | None:
    """If the post is an LAX -> Japan fare, return its dests, USD price and trip type."""
    title = strip_html(title)
    summary = strip_html(summary)

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
    return {"dests": dests, "price": price, "trip": trip}


def classify(title: str, summary: str = "", today: date | None = None) -> dict | None:
    """Return alert details if this post is a deal we care about, else None.

    `summary` can be the post's full HTML; it's used for the destination,
    origin, error-fare wording and travel dates.
    """
    title = strip_html(title)
    summary = strip_html(summary)

    # 0. Skip deals the site has already marked as dead.
    if EXPIRED_RX.search(title):
        return None

    # 1-3. Route, price and trip type.
    route = parse_route(title, summary)
    if not route:
        return None
    dests, price, trip = route["dests"], route["price"], route["trip"]
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

    # 5. Travel dates: skip deals that only cover months you can't go.
    months = travel_months(title + " . " + summary, today)
    if months and not in_travel_window(months):
        return None
    if not months and not config.ALERT_IF_NO_DATES:
        return None

    return {"tier": tier, "price": price, "trip": trip, "dests": dests, "months": months}


# ------------------------------------------------------------------- feeds --

FEED_LINK_RX = re.compile(
    rb"<link\b[^>]*type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>", re.I)
HREF_RX = re.compile(rb"href=[\"']([^\"']+)[\"']", re.I)


def _get(url: str, accept: str | None = None):
    headers = dict(FEED_HEADERS, **({"Accept": accept} if accept else {}))
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp


def _discover_feeds(page: bytes) -> list[str]:
    """Feed URLs a web page advertises with <link rel="alternate" type="application/rss+xml">."""
    urls = []
    for tag in FEED_LINK_RX.findall(page[:200_000]):
        if (m := HREF_RX.search(tag)) and b"comments" not in m.group(1).lower():
            urls.append(html.unescape(m.group(1).decode("utf-8", "replace")))
    return urls


def fetch_feed(name: str, url: str) -> tuple[list, str | None]:
    """Return (entries, problem). `problem` is None when the feed was read fine."""
    try:
        resp = _get(url)
    except requests.RequestException as exc:
        problem = f"could not fetch feed ({exc})"
        print(f"[warn] {name}: {problem}", file=sys.stderr)
        return [], problem
    parsed = feedparser.parse(resp.content)
    if parsed.entries or not (parsed.bozo or not parsed.version):
        return parsed.entries, None

    # Got a web page instead of a feed: follow the feed link the page lists.
    for feed_url in _discover_feeds(resp.content):
        if feed_url.rstrip("/") == url.rstrip("/"):
            continue
        try:
            alt = feedparser.parse(_get(feed_url).content)
        except requests.RequestException:
            continue
        if alt.entries:
            print(f"[warn] {name}: {url} isn't a feed any more; using {feed_url} instead. "
                  f"Update FEEDS in config.py to that URL.", file=sys.stderr)
            return alt.entries, None

    # Say what came back instead, e.g. a "Just a moment..." bot-check page.
    page_title = re.search(rb"<title[^>]*>(.*?)</title>", resp.content[:5000], re.I | re.S)
    what = strip_html(page_title.group(1).decode("utf-8", "replace"))[:80] if page_title else ""
    redirected = f", redirected to {resp.url}" if resp.url.rstrip("/") != url.rstrip("/") else ""
    problem = (f"response wasn't a readable RSS feed "
               f"(HTTP {resp.status_code}{redirected}, {resp.headers.get('Content-Type', '?')}, "
               f"{len(resp.content):,} bytes{', page title: ' + repr(what) if what else ''})")
    print(f"[warn] {name}: {problem}", file=sys.stderr)
    return [], problem


# ------------------------------------------------------ secret flying pages --
# Secret Flying switched off its RSS feeds and REST API in 2026 (/feed/ now
# redirects to the home page), so the bot reads its public deal listings
# instead. Each card's CSS classes carry the site's own tags, including exact
# routes like "category-los-angeles-usa--tokyo-japan" and "category-error-fare",
# and dead deals get an EXPIRED stamp, so the listing alone is enough.

ARTICLE_RX = re.compile(r"<article\b([^>]*)>(.*?)</article>", re.I | re.S)
CLASS_RX = re.compile(r"""class=["']([^"']*)["']""", re.I)
CARD_TITLE_RX = re.compile(
    r"""<h3[^>]*entry-title[^>]*>\s*<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
CARD_DATE_RX = re.compile(r"<time[^>]*entry-date[^>]*>\s*([^<]+?)\s*</time>", re.I)


def _slug_words(slug: str) -> str:
    return slug.strip("_").replace("-", " ")


def parse_secret_flying_cards(page: str) -> list[dict]:
    """Deal cards on a Secret Flying listing page, as feed-like entries.

    Expired deals and ad slots are left out. Each entry's "content" holds the
    card's tags in words, e.g. "los angeles usa → tokyo japan / error fare",
    which is what the matcher reads to find the origin and destinations.
    """
    entries = []
    for attrs, body in ARTICLE_RX.findall(page):
        title_m = CARD_TITLE_RX.search(body)
        if not title_m or "expiredStamp" in body:
            continue
        cls = CLASS_RX.search(attrs)
        slugs = [c[len("category-"):] for c in (cls.group(1).split() if cls else [])
                 if c.startswith("category-")]
        # Route tags are exact ("X-usa--Y-japan"). When a card has them, keep
        # only routes from your origin to your destinations, so a post with
        # LA -> Seoul and New York -> Tokyo doesn't look like LA -> Tokyo.
        routes = [tuple(map(_slug_words, s.split("--", 1))) for s in slugs if "--" in s]
        if routes:
            tags = [f"{a} → {b}" for a, b in routes
                    if ORIGIN_RX.search(a) and any(rx.search(b) for rx in DEST_RX.values())]
        else:
            tags = [_slug_words(s) for s in slugs]
        if "error-fare" in slugs:
            tags.append("error fare")
        link = html.unescape(title_m.group(1)).strip()
        entry = {
            "id": link,
            "link": link,
            "title": strip_html(title_m.group(2)),
            "summary": "",
            "content": [{"value": "Secret Flying tags: " + " / ".join(tags)}],
        }
        if (date_m := CARD_DATE_RX.search(body)):
            try:
                entry["published_parsed"] = time.strptime(date_m.group(1).strip(), "%b %d, %Y")
            except ValueError:
                pass
        entries.append(entry)
    return entries


def fetch_secret_flying(name: str, urls: list[str]) -> tuple[list, str | None]:
    """Read every listing page; returns (entries, problem) like fetch_feed."""
    entries, problems, seen_links = [], [], set()
    for i, url in enumerate(urls):
        if i:
            time.sleep(1)  # robots.txt asks for a 1 s crawl delay
        try:
            resp = _get(url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8")
        except requests.RequestException as exc:
            problems.append(f"{url}: could not fetch ({exc})")
            continue
        page = resp.content.decode(resp.encoding or "utf-8", "replace")
        cards = parse_secret_flying_cards(page)
        if not cards and "entry-title" not in page:
            problems.append(f"{url}: no deal cards found (HTTP {resp.status_code}); "
                            f"the page layout may have changed")
        for card in cards:
            if card["link"] not in seen_links:
                seen_links.add(card["link"])
                entries.append(card)
    for p in problems:
        print(f"[warn] {name}: {p}", file=sys.stderr)
    # Healthy as long as at least one page gave us deals.
    problem = "; ".join(problems) if problems and not entries else None
    return entries, problem


def all_sources() -> list:
    """(name, fetch) pairs; each fetch() returns (entries, problem)."""
    sources = [(name, (lambda n=name, u=url: fetch_feed(n, u))) for name, url in config.FEEDS.items()]
    if getattr(config, "SECRET_FLYING_PAGES", None):
        sources.append(("Secret Flying",
                        lambda: fetch_secret_flying("Secret Flying", config.SECRET_FLYING_PAGES)))
    return sources


def entry_key(entry) -> str:
    return entry.get("id") or entry.get("link") or entry.get("title", "")


def entry_time(entry) -> str | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc).isoformat()


# ----------------------------------------------------------- price history --

PRICES_FILE = Path(__file__).with_name("prices.json")


def load_prices() -> list[dict]:
    if not PRICES_FILE.exists():
        return []
    try:
        return json.loads(PRICES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        print("[warn] prices.json was unreadable; starting price history fresh", file=sys.stderr)
        return []


def save_prices(records: list[dict]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.PRICE_MEMORY_DAYS)
    kept = [r for r in records if datetime.fromisoformat(r["date"]) >= cutoff]
    kept.sort(key=lambda r: r["date"])
    PRICES_FILE.write_text(json.dumps(kept, indent=1) + "\n")


def _short_date(d: datetime, now: datetime) -> str:
    return f"{d:%b} {d.day}" + ("" if d.year == now.year else f", {d.year}")


def price_note(price: int, trip: str, records: list[dict], key: str, now: datetime) -> str:
    """How this price compares with every earlier LAX -> Japan price seen."""
    trip_label = "one-way" if trip == "oneway" else "round-trip"
    past = [r for r in records if r["trip"] == trip and r["key"] != key]
    if not past:
        return f"First {trip_label} price the bot has tracked, so nothing to compare yet."

    oldest = min(datetime.fromisoformat(r["date"]) for r in past)
    span = (now - oldest).days
    window = (f"in {config.PRICE_MEMORY_DAYS // 30} months" if span >= config.PRICE_MEMORY_DAYS - 5
              else f"since tracking began ({_short_date(oldest, now)})")
    best = min(past, key=lambda r: (r["price"], r["date"]))
    best_txt = f"${best['price']:,} on {_short_date(datetime.fromisoformat(best['date']), now)}"
    n = f"{len(past)} {trip_label} deal{'s' if len(past) != 1 else ''} tracked"

    if price < best["price"]:
        return f"🏆 **Lowest {trip_label} price {window}** (previous best {best_txt}) · {n}"
    if price == best["price"]:
        return f"🏆 **Ties the lowest {trip_label} price {window}** ({best_txt}) · {n}"

    line = f"${price - best['price']:,} above the best {window} ({best_txt}) · {n}"
    recent_days = config.PRICE_RECENT_DAYS
    recent = [r["price"] for r in past
              if now - datetime.fromisoformat(r["date"]) <= timedelta(days=recent_days)]
    if span >= recent_days and recent and price <= min(recent):
        line = f"📉 **Lowest in {recent_days} days** · " + line
    return line


def record_price(records: list[dict], known: set[str], key: str, entry, route: dict | None,
                 now: datetime) -> None:
    if not route or route["price"] is None or key in known:
        return
    records.append({
        "key": key,
        "date": now.isoformat(),
        "price": route["price"],
        "trip": route["trip"],
        "dests": route["dests"],
        "title": strip_html(entry.get("title", ""))[:200],
    })
    known.add(key)


# ----------------------------------------------------------------- booking --

# Sites a deal post might link to for actually buying the ticket. Matched
# against the link's host; first match wins.
BOOKING_SITES = [
    ("google.com/travel/flights", "Google Flights"),
    ("google.com/flights", "Google Flights"),
    ("matrix.itasoftware.com", "ITA Matrix"),
    ("skyscanner.", "Skyscanner"),
    ("kayak.", "Kayak"),
    ("momondo.", "momondo"),
    ("expedia.", "Expedia"),
    ("priceline.", "Priceline"),
    ("cheapoair.", "CheapOair"),
    ("kiwi.com", "Kiwi.com"),
    ("trip.com", "Trip.com"),
    ("united.com", "United"),
    ("delta.com", "Delta"),
    ("aa.com", "American"),
    ("alaskaair.com", "Alaska"),
    ("ana.co.jp", "ANA"),
    ("jal.co.jp", "JAL"),
    ("zipair.net", "ZIPAIR"),
    ("singaporeair.com", "Singapore Airlines"),
    ("koreanair.com", "Korean Air"),
    ("evaair.com", "EVA Air"),
    ("china-airlines.com", "China Airlines"),
    ("philippineairlines.com", "Philippine Airlines"),
]
# Links on the deal site itself that are booking buttons ("GO TO DEAL").
BOOKING_TEXT_RX = re.compile(r"\b(go to deal|book( now| here| it)?|view deal|search flights)\b", re.I)
LINK_RX = re.compile(r"<a\s[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
MAX_BOOKING_LINKS = 3


def entry_html(entry) -> str:
    parts = [c.get("value", "") for c in entry.get("content", []) or []]
    parts.append(entry.get("summary", ""))
    return " ".join(parts)


def find_booking_links(entry) -> list[tuple[str, str]]:
    """Booking links (label, url) found in the deal post's text, best first."""
    found: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for href, text in LINK_RX.findall(entry_html(entry)):
        url = html.unescape(href).strip()
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        where = urlparse(url).netloc.lower() + urlparse(url).path.lower()
        label = next((name for key, name in BOOKING_SITES if key in where), None)
        if label is None and BOOKING_TEXT_RX.search(strip_html(text)):
            label = "Book on deal site"
        if label:
            seen_urls.add(url)
            found.append((label, url))
    # Several links to the same site (e.g. one per date) → keep the first.
    by_label: dict[str, str] = {}
    for label, url in found:
        by_label.setdefault(label, url)
    return list(by_label.items())[:MAX_BOOKING_LINKS]


def google_flights_url(dest: str, trip: str) -> str:
    """A Google Flights search for LAX → dest (always works as a fallback)."""
    q = f"Flights from LAX to {dest}" + (" one way" if trip == "oneway" else "")
    return f"https://www.google.com/travel/flights?q={quote(q)}&curr=USD&hl=en"


def booking_field(entry, info: dict) -> dict:
    links = [f"[{label}]({url})" for label, url in find_booking_links(entry)]
    for dest in info["dests"][:2]:
        links.append(f"[Search Google Flights: LAX → {dest}]({google_flights_url(dest, info['trip'])})")
    # Discord caps field values at 1024 chars; drop links from the end to fit.
    while len(" · ".join(links)) > 1024 and len(links) > 1:
        links.pop()
    return {"name": "🎟️ Book it", "value": " · ".join(links), "inline": False}


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
            *([{"name": "📊 Price history", "value": info["history"], "inline": False}]
              if info.get("history") else []),
            {"name": "🗓️ Travel dates",
             "value": format_months(info["months"]) if info.get("months") else "Not listed, check the post",
             "inline": False},
            booking_field(entry, info),
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


# ------------------------------------------------------------ feed health --
# A blocked feed used to fail silently (green run, no alerts, ever). Now the
# bot remembers which feeds work and posts to Discord when one breaks or
# recovers, and the GitHub run shows a warning.

def load_health() -> dict:
    try:
        return json.loads(HEALTH_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_health(health: dict) -> None:
    HEALTH_FILE.write_text(json.dumps(health, indent=1, sort_keys=True) + "\n")


def health_message(source: str, problem: str | None, since: str | None) -> dict:
    if problem:
        text = (f"⚠️ **{source} feed isn't working**, so deals from it are being missed.\n"
                f"`{problem[:300]}`\nThe bot keeps checking and will say when it recovers.")
        color = 0xFDD835
    else:
        text = f"✅ **{source} feed is working again**" + (f" (broken since {since[:10]})." if since else ".")
        color = 0x43A047
    return {"username": config.BOT_NAME, "allowed_mentions": {"parse": []},
            "embeds": [{"description": text, "color": color}]}


def update_health(health: dict, source: str, problem: str | None, now: str) -> dict | None:
    """Record this run's result for a feed; return a Discord message if it changed."""
    prev = health.get(source)  # None (never checked) or {"ok": bool, "since": iso}
    ok = problem is None
    if prev is not None and prev.get("ok") == ok:
        if not ok:
            prev["last_problem"] = problem
        return None
    health[source] = {"ok": ok, "since": now, **({"last_problem": problem} if problem else {})}
    if prev is None and ok:
        return None  # first time we've seen it, and it works: nothing to say
    return health_message(source, problem, prev.get("since") if prev else None)


def gh_annotation(level: str, title: str, message: str) -> None:
    if IN_GITHUB_ACTIONS:
        print(f"::{level} title={title}::{message}")


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

def run_once(webhook: str | None, dry_run: bool, post_existing: bool) -> bool:
    """Check every feed once. Returns False if no feed could be read at all."""
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    posted = matched = 0
    prices = load_prices()
    known_prices = {r["key"] for r in prices}
    health = load_health()
    working = 0

    sources = all_sources()
    for source, fetch in sources:
        entries, problem = fetch()
        print(f"{source}: {len(entries)} posts")
        if problem:
            gh_annotation("warning", f"{source} feed unreadable", problem)
        else:
            working += 1
        if not dry_run:
            notice = update_health(health, source, problem, now)
            if notice:
                post_to_discord(webhook, notice)
        for entry in entries:
            key = entry_key(entry)
            body = entry_html(entry)
            route = parse_route(entry.get("title", ""), body)
            if not dry_run and key in seen:
                record_price(prices, known_prices, key, entry, route, now_dt)
                continue
            info = classify(entry.get("title", ""), body)
            if info and info["price"] is not None:
                info["history"] = price_note(info["price"], info["trip"], prices, key, now_dt)
            # Remember every LAX -> Japan price (even ones too pricey to post)
            # so later alerts have something to compare against.
            record_price(prices, known_prices, key, entry, route, now_dt)
            if info:
                matched += 1
                label = f"[{info['tier']}] {strip_html(entry.get('title', ''))}"
                if info["months"]:
                    label += f"  ({format_months(info['months'])})"
                if info.get("history"):
                    label += f"\n      {info['history']}"
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
        return working > 0
    save_seen(seen)
    save_prices(prices)
    save_health(health)
    if first_run and not post_existing:
        print("First run: remembered current posts; only new deals will be posted from now on.")
    print(f"Done: {posted} posted, {matched} matched, {working}/{len(sources)} sources readable.")
    if working == 0:
        gh_annotation("error", "No feeds readable", "Every deal feed failed this run; see the warnings above.")
    return working > 0


def send_test(webhook: str) -> None:
    entry = {
        "title": "TEST: Los Angeles to Osaka, Japan for only $300 USD roundtrip (error fare)",
        "link": "https://www.google.com/travel/flights",
        "summary": "This is a test alert from your Japan fare bot. If you can read this, the webhook works! "
                   "Travel dates: December 2026 – January 2027.",
        "content": [{"value": '<a href="https://www.google.com/travel/flights?q=Flights%20from%20LAX%20to%20KIX">'
                              'Google Flights</a>'}],
    }
    info = classify(entry["title"], entry["summary"], today=date(2026, 11, 1))
    info["history"] = "🏆 **Lowest round-trip price in 12 months** (previous best $389 on Mar 3) · 41 round-trip deals tracked (sample)"
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
            any_feed_ok = run_once(webhook, args.dry_run, args.post_existing)
        except Exception as exc:  # keep the loop alive through one bad run
            if not args.loop:
                raise
            print(f"[error] {exc}", file=sys.stderr)
            any_feed_ok = True
        if not args.loop:
            if not any_feed_ok:
                sys.exit(1)  # red X in GitHub Actions (and an email) instead of a fake green
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
