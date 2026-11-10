"""Run with:  python -m pytest -q   (or just: python test_bot.py)"""
from datetime import date, datetime, timedelta, timezone

from bot import build_message, classify, price_note, find_booking_links, format_months, google_flights_url, travel_months

NOV = date(2026, 11, 2)


def tier(title, summary=""):
    info = classify(title, summary)
    return info and info["tier"]


def test_great_roundtrip():
    assert tier("Los Angeles, USA to Tokyo, Japan for only $499 USD roundtrip") == "great"


def test_good_with_airport_name():
    info = classify("United – $689: Los Angeles – Tokyo (Haneda), Japan. Roundtrip, including all Taxes")
    assert info["tier"] == "good"
    assert "HND" in info["dests"] and "Tokyo" in info["dests"]
    assert info["price"] == 689


def test_error_fare_is_flagged():
    assert tier("ERROR FARE: Los Angeles to Osaka, Japan for only $300 USD roundtrip") == "error"


def test_error_fare_word_in_body():
    assert tier("Los Angeles to Tokyo for $310 roundtrip", "Looks like a mistake fare, book fast") == "error"


def test_too_expensive_is_skipped():
    assert tier("Los Angeles to Tokyo, Japan for only $980 USD roundtrip") is None


def test_wrong_direction_is_skipped():
    assert tier("Tokyo, Japan to Los Angeles, USA for only $450 USD roundtrip") is None


def test_other_destination_is_skipped():
    assert tier("Los Angeles to Seoul, South Korea for only $500 USD roundtrip") is None


def test_multi_city_post_uses_body():
    info = classify(
        "Non-stop from US cities to Japan from only $520 roundtrip",
        "<p>Los Angeles to Osaka, Japan $520; New York to Tokyo $640</p>",
    )
    assert info["tier"] == "great" and "KIX" in info["dests"]


def test_lax_needs_whole_word():
    assert tier("Relax in Tokyo with these hotel deals from $99") is None


def test_one_way_thresholds():
    info = classify("Los Angeles to Narita, Japan for only $299 USD one-way")
    assert info["trip"] == "oneway" and info["tier"] == "great"
    assert tier("Los Angeles to Narita, Japan for only $520 USD one way") is None


def test_comma_price():
    assert classify("Los Angeles to Tokyo $1,450 roundtrip in business class") is None


def test_foreign_currency_is_not_read_as_usd():
    assert classify("Los Angeles to Tokyo for CAD $600 roundtrip")["price"] is None


def test_expired_deal_is_skipped():
    assert tier("[FARE GONE] Alaska Air: Seattle / Los Angeles – Tokyo / Osaka, Japan. Starting $195. Roundtrip") is None


def test_booking_links_from_post():
    entry = {"summary": (
        '<p>Dates: <a href="https://www.google.com/travel/flights?tfs=abc&amp;curr=USD">Mar 3</a>, '
        '<a href="https://www.google.com/travel/flights?tfs=def">Mar 10</a> '
        '<a href="https://twitter.com/share?u=x">Tweet</a> '
        '<a href="https://www.skyscanner.com/transport/flights/lax/kix/">Skyscanner</a> '
        '<a href="https://www.secretflying.com/go/123">GO TO DEAL</a></p>')}
    links = find_booking_links(entry)
    assert links == [
        ("Google Flights", "https://www.google.com/travel/flights?tfs=abc&curr=USD"),
        ("Skyscanner", "https://www.skyscanner.com/transport/flights/lax/kix/"),
        ("Book on deal site", "https://www.secretflying.com/go/123"),
    ]


def test_google_flights_fallback_url():
    assert google_flights_url("KIX", "oneway") == (
        "https://www.google.com/travel/flights?q=Flights%20from%20LAX%20to%20KIX%20one%20way&curr=USD&hl=en")


def test_alert_always_has_book_link():
    entry = {"title": "Los Angeles to Tokyo, Japan for only $499 USD roundtrip", "link": "https://example.com/post"}
    msg = build_message(entry, "Test", classify(entry["title"]))
    book = next(f for f in msg["embeds"][0]["fields"] if "Book" in f["name"])
    assert "google.com/travel/flights" in book["value"] and "Tokyo" in book["value"]


def months(text):
    return travel_months(text, NOV)


def test_month_range_across_new_year():
    assert months("Travel dates: December 2026 – March 2027") == [(2026, 12), (2027, 1), (2027, 2), (2027, 3)]


def test_month_range_without_years():
    assert months("Availability: Jan - Mar") == [(2027, 1), (2027, 2), (2027, 3)]
    assert months("Various dates between December and February") == [(2026, 12), (2027, 1), (2027, 2)]


def test_month_list_and_specific_dates():
    assert months("Sample dates: Dec 3 – Dec 10, Jun 5 2027") == [(2026, 12), (2027, 6)]


def test_may_the_verb_is_ignored():
    assert months("You may want to book fast. Travel in April") == [(2027, 4)]


def test_booking_deadline_is_not_a_travel_date():
    assert months("Book by December 5. Travel March – April") == [(2027, 3), (2027, 4)]


def test_format_months():
    assert format_months([(2026, 12), (2027, 1), (2027, 6)]) == "Dec 2026 – Jan 2027, Jun 2027"


def test_winter_break_deal_kept():
    info = classify("Los Angeles to Tokyo, Japan for only $499 USD roundtrip",
                    "<p>Travel dates: December – January</p>", NOV)
    assert info["tier"] == "great" and info["months"] == [(2026, 12), (2027, 1)]


def test_spring_only_deal_skipped():
    assert classify("Los Angeles to Tokyo, Japan for only $499 USD roundtrip",
                    "<p>Travel dates: February – May 2027</p>", NOV) is None


def test_deal_overlapping_summer_kept():
    assert classify("Los Angeles to Osaka, Japan for only $520 USD roundtrip",
                    "<p>Availability: March – July 2027</p>", NOV)["tier"] == "great"


def test_november_only_deal_skipped():
    assert classify("Los Angeles to Tokyo, Japan for only $499 USD roundtrip",
                    "<p>Travel November 2026</p>", NOV) is None


def test_no_dates_still_posts():
    assert classify("Los Angeles to Tokyo, Japan for only $499 USD roundtrip", "", NOV)["months"] == []


NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def rec(price, days_ago, trip="roundtrip", key=None):
    return {"key": key or f"k{price}-{days_ago}", "price": price, "trip": trip,
            "date": (NOW - timedelta(days=days_ago)).isoformat()}


def test_price_note_no_history():
    assert "nothing to compare" in price_note(499, "roundtrip", [], "new", NOW)


def test_price_note_new_low_over_full_year():
    note = price_note(399, "roundtrip", [rec(450, 300), rec(520, 370)], "new", NOW)
    assert "Lowest round-trip price in 12 months" in note and "$450 on Jan 6" in note


def test_price_note_short_history_says_so():
    note = price_note(399, "roundtrip", [rec(450, 20)], "new", NOW)
    assert "since tracking began (Oct 13)" in note


def test_price_note_above_best_and_lowest_recent():
    note = price_note(530, "roundtrip", [rec(450, 200), rec(600, 30), rec(560, 10)], "new", NOW)
    assert note.startswith("📉 **Lowest in 60 days**") and "$80 above the best" in note


def test_price_note_above_best_only():
    note = price_note(620, "roundtrip", [rec(450, 200), rec(600, 30)], "new", NOW)
    assert note.startswith("$170 above the best") and "Lowest in 60" not in note


def test_price_note_ignores_other_trip_type_and_itself():
    records = [rec(250, 100, trip="oneway"), rec(499, 0, key="same")]
    assert "nothing to compare" in price_note(499, "roundtrip", records, "same", NOW)


if __name__ == "__main__":
    import sys
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)} tests passed")
    sys.exit(0)


# ------------------------------------------------------------ feed health --

from bot import update_health  # noqa: E402

T1, T2, T3 = "2026-11-10T00:00:00+00:00", "2026-11-10T01:00:00+00:00", "2026-11-10T02:00:00+00:00"


def test_health_first_working_run_is_quiet():
    health = {}
    assert update_health(health, "Feed", None, T1) is None
    assert health["Feed"]["ok"] is True


def test_health_alerts_once_when_feed_breaks_then_recovers():
    health = {}
    update_health(health, "Feed", None, T1)
    broke = update_health(health, "Feed", "HTTP 403", T2)
    assert broke and "isn't working" in broke["embeds"][0]["description"]
    assert update_health(health, "Feed", "HTTP 403", T3) is None  # no repeat ping every run
    fixed = update_health(health, "Feed", None, T3)
    assert fixed and "working again" in fixed["embeds"][0]["description"]


def test_health_alerts_when_feed_never_worked():
    health = {}
    msg = update_health(health, "Feed", "HTTP 403", T1)
    assert msg and "isn't working" in msg["embeds"][0]["description"]


# ---------------------------------------------------- secret flying pages --

from pathlib import Path  # noqa: E402

from bot import entry_html, parse_secret_flying_cards  # noqa: E402

LISTING = (Path(__file__).parent / "fixtures" / "secretflying_listing.html").read_text(encoding="utf-8")


def sf_cards():
    return {c["link"].rstrip("/").rsplit("/", 1)[-1]: c for c in parse_secret_flying_cards(LISTING)}


def test_sf_skips_ads_and_expired_deals():
    cards = sf_cards()
    assert len(cards) == 3
    assert "expired-example" not in cards


def test_sf_error_fare_to_japan_from_us_cities_is_caught():
    # The real Nov 2026 error fare: title only says "US cities to Japan",
    # LAX and Tokyo/Osaka are in the card's route tags.
    card = sf_cards()["error-fare-us-cities-to-japan-from-only-154-roundtrip"]
    info = classify(card["title"], entry_html(card), today=NOV)
    assert info["tier"] == "error"
    assert info["price"] == 154
    assert set(info["dests"]) == {"Tokyo", "KIX"}
    assert card["published_parsed"][:3] == (2026, 11, 1)


def test_sf_unrelated_route_is_ignored():
    card = sf_cards()["los-angeles-san-francisco-vice-versa-86-roundtrip"]
    assert "&amp;" not in card["title"]
    assert classify(card["title"], entry_html(card), today=NOV) is None


def test_sf_other_city_to_tokyo_doesnt_count_as_lax():
    card = sf_cards()["mixed-routes-example"]  # LA -> Seoul and New York -> Tokyo
    assert classify(card["title"], entry_html(card), today=NOV) is None


def test_sf_alert_message_builds():
    card = sf_cards()["error-fare-us-cities-to-japan-from-only-154-roundtrip"]
    info = classify(card["title"], entry_html(card), today=NOV)
    msg = build_message(card, "Secret Flying", info)
    assert msg["content"] == "@everyone"
    assert msg["embeds"][0]["url"].endswith("-154-roundtrip/")
