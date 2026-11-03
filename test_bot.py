"""Run with:  python -m pytest -q   (or just: python test_bot.py)"""
from datetime import date

from bot import build_message, classify, find_booking_links, format_months, google_flights_url, travel_months

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


if __name__ == "__main__":
    import sys
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)} tests passed")
    sys.exit(0)
