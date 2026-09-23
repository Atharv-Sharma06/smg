import pytest

from smg_trader.parsing import map_columns, parse_int, parse_number, parse_ticker
from smg_trader.selectors import HOLDINGS_COLUMNS, RANKINGS_COLUMNS, merged_selectors


@pytest.mark.parametrize("text,expected", [
    ("$1,234.56", 1234.56),
    ("(12.30)", -12.3),
    ("($1,000.00)", -1000.0),
    ("-5.2%", -5.2),
    ("- $12.30", -12.3),
    ("$-7", -7.0),
    ("12.4%", 12.4),
    ("1,000", 1000.0),
    ("−3.5", -3.5),
    ("--", None),
    ("", None),
    (None, None),
    ("n/a", None),
])
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_parse_int_and_ticker():
    assert parse_int("1,500") == 1500
    assert parse_ticker("AAPL") == "AAPL"
    assert parse_ticker("BRK.B") == "BRK.B"
    assert parse_ticker("Apple Inc (AAPL)") == "AAPL"
    assert parse_ticker("Total") is None
    assert parse_ticker("") is None


def test_map_columns_prefers_specific_fields():
    headers = ["Symbol", "Description", "Position", "Shares", "Purchase Price", "Current Price",
               "Market Value", "Gain/Loss"]
    cols = map_columns(headers, HOLDINGS_COLUMNS)
    assert cols == {"ticker": 0, "shares": 3, "purchase_price": 4, "current_price": 5,
                    "market_value": 6, "gain_loss": 7, "position": 2}


def test_map_columns_rankings():
    cols = map_columns(["Rank", "Team Name", "School", "Region", "Total Equity", "% Return"], RANKINGS_COLUMNS)
    assert cols == {"rank": 0, "team": 1, "school": 2, "region": 3, "total_equity": 4, "pct_return": 5}


def test_selector_overrides_go_first():
    merged = merged_selectors({"holdings_table": "css:#myTable", "custom": ["css:.x"]})
    assert merged["holdings_table"][0] == "css:#myTable"
    assert len(merged["holdings_table"]) > 1
    assert merged["custom"] == ["css:.x"]
