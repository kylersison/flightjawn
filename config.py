"""Settings for the Japan fare alert bot. Tweak these freely."""

# Deal feeds to watch (RSS). Both are curated deal sites, so most posts are
# already "good" prices — the bot narrows them to your routes.
FEEDS = {
    "Secret Flying": "https://www.secretflying.com/feed/",
    "The Flight Deal (LAX)": "https://www.theflightdeal.com/category/flight-deals/lax/feed/",
}

# Where you fly from. Keywords are matched as whole words, case-insensitive.
ORIGINS = {
    "LAX": ["los angeles", "lax"],
    # Add more home airports later, e.g.:
    # "SNA": ["santa ana", "orange county", "sna"],
    # "LGB": ["long beach", "lgb"],
}

# Where you want to go.
DESTINATIONS = {
    "HND": ["haneda", "hnd"],
    "NRT": ["narita", "nrt"],
    "Tokyo": ["tokyo", "tyo"],
    "KIX": ["osaka", "kansai", "kix"],
}

# A post whose title only says "Japan" still counts if its body mentions
# one of the destinations above.
COUNTRY_WORDS = ["japan"]

# Price tiers in USD. Anything above MAX_PRICE is ignored (unless it looks
# like an error fare). Starting points — adjust to what feels like a steal.
GREAT_PRICE = {"roundtrip": 550, "oneway": 325}
MAX_PRICE = {"roundtrip": 750, "oneway": 450}

# When you can travel. Deals whose posts only list months outside these
# windows are skipped. Format: ("YYYY-MM", "YYYY-MM"), both ends included;
# use None for "and anytime after". Use an empty list [] to turn the filter off.
TRAVEL_WINDOWS = [
    ("2026-12", "2027-01"),  # winter break: December – January
    ("2027-06", None),       # June onward
]

# Post deals whose post doesn't list any travel months we could read.
ALERT_IF_NO_DATES = True

# Post deals even when no USD price could be read from the title.
ALERT_IF_NO_PRICE = True

# Phrases that mark a likely error fare (these get the loud ping).
ERROR_FARE_WORDS = [
    "error fare",
    "mistake fare",
    "price glitch",
    "fare glitch",
    "glitch fare",
    "pricing error",
]

# Who gets pinged for error fares: "@everyone", "@here", a role like
# "<@&123456789012345678>", or "" for no ping. Can be overridden with the
# ERROR_FARE_MENTION environment variable.
ERROR_FARE_MENTION = "@everyone"

# Price memory: every LAX -> Japan price the bot sees goes into prices.json,
# and each alert says how it compares ("$80 above the best in 12 months").
PRICE_MEMORY_DAYS = 365   # how far back "the best" looks
PRICE_RECENT_DAYS = 60    # for the "lowest in 60 days" badge

# Forget deals older than this so seen.json stays small.
SEEN_RETENTION_DAYS = 60

BOT_NAME = "Japan Fare Watch"
