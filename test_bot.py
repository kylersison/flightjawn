"""Run with:  python -m pytest -q   (or just: python test_bot.py)"""
from bot import classify


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


if __name__ == "__main__":
    import sys
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)} tests passed")
    sys.exit(0)
