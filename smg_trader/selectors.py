"""Locators for stockmarketgame.org pages.

Each key maps to a list of candidates tried in order; the first one that
matches wins. Prefixes: ``css:`` (default), ``xpath:``, ``link:`` (partial
link text), ``id:``, ``name:``.

The defaults are deliberately generic (password inputs, tables whose headers
say "Symbol", links whose text says "Rankings") because the bot was written
without access to SMG's live HTML. Run ``python -m smg_trader inspect`` to
save the real pages to ``data/debug/`` and put exact selectors under
``smg.selectors`` in config.yaml; overrides are tried before these defaults.
"""

from __future__ import annotations

from typing import Any


def _ci_contains(text: str) -> str:
    """XPath predicate: element text contains ``text`` (case-insensitive)."""
    return (
        "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        f"'abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')"
    )


def _button(*texts: str) -> list[str]:
    preds = " or ".join(_ci_contains(t) for t in texts)
    value_preds = " or ".join(
        "contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        f"'abcdefghijklmnopqrstuvwxyz'), '{t.lower()}')"
        for t in texts
    )
    return [
        f"xpath://button[{preds}]",
        f"xpath://input[(@type='submit' or @type='button') and ({value_preds})]",
        f"xpath://a[contains(@class, 'btn') and ({preds})]",
    ]


def _table_with_header(*texts: str) -> str:
    preds = " and ".join(f".//*[(self::th or self::td) and {_ci_contains(t)}]" for t in texts)
    return f"xpath:(//table[{preds}])[1]"


DEFAULT_SELECTORS: dict[str, list[str]] = {
    # --- login -------------------------------------------------------------
    "login_username": [
        "css:input[name='username' i]", "css:input[id*='user' i]", "css:input[name*='user' i]",
        "css:input[type='email']", "css:input[id*='login' i][type='text']",
        "xpath:(//input[@type='password']/ancestor::form//input[@type='text' or not(@type)])[1]",
    ],
    "login_password": ["css:input[type='password']"],
    "login_submit": [
        *_button("log in", "login", "sign in", "submit"),
        "css:form button[type='submit']", "css:form input[type='submit']",
    ],
    "logged_in_marker": [
        f"xpath://a[{_ci_contains('log out')} or {_ci_contains('logout')} or {_ci_contains('sign out')}]",
        "css:a[href*='logout' i]", "css:a[href*='signout' i]", "css:#logout",
    ],
    "login_error": [
        "css:.validation-summary-errors", "css:.alert-danger", "css:.error-message",
        "css:#loginError", "css:.error", "css:[role='alert']",
    ],
    # --- navigation (used when the *_url config values are blank) ----------
    "nav_portfolio": ["link:Account Holdings", "link:Holdings", "link:Portfolio"],
    "nav_account_summary": ["link:Account Summary", "link:Summary"],
    "nav_trade": ["link:Enter a Trade", "link:Trade"],
    "nav_pending": ["link:Pending Orders", "link:Pending", "link:Open Orders"],
    "nav_rankings": ["link:Rankings", "link:Leaderboard", "link:Standings"],
    # --- portfolio -----------------------------------------------------------
    "holdings_table": [
        "css:table#holdings", "css:table.holdings",
        _table_with_header("symbol", "shares"), _table_with_header("symbol", "quantity"),
    ],
    "pending_table": [
        "css:table#pending", "css:table.pending", _table_with_header("symbol", "status"),
    ],
    "no_holdings_marker": [f"xpath://*[{_ci_contains('no holdings')} or {_ci_contains('no positions')}]"],
    # --- rankings ------------------------------------------------------------
    "rankings_table": [
        "css:table#rankings", "css:table.rankings", _table_with_header("rank", "team"),
        _table_with_header("rank", "equity"), _table_with_header("rank"),
    ],
    "region_select": [
        "css:select[name*='region' i]", "css:select[id*='region' i]",
        "css:select[name*='state' i]", "css:select[id*='coordinator' i]",
    ],
    "region_apply": _button("go", "view", "search", "apply", "filter"),
    # --- trade entry -----------------------------------------------------------
    "trade_security_type": ["css:select[name*='security' i]", "css:select[id*='security' i]",
                            "css:select[name*='type' i][name*='sec' i]"],
    "trade_action": ["css:select[name*='action' i]", "css:select[id*='action' i]",
                     "css:select[name*='transaction' i]", "css:select[id*='transaction' i]"],
    "trade_symbol": ["css:input[name*='symbol' i]", "css:input[id*='symbol' i]", "css:input[name*='ticker' i]"],
    "trade_shares": ["css:input[name*='shares' i]", "css:input[id*='shares' i]",
                     "css:input[name*='quantity' i]", "css:input[id*='qty' i]"],
    "trade_order_type": ["css:select[name*='ordertype' i]", "css:select[id*='ordertype' i]",
                         "css:select[name*='order_type' i]"],
    "trade_preview": _button("preview", "review", "next", "continue"),
    "trade_submit": _button("place order", "submit order", "confirm", "place trade", "submit"),
    "trade_confirmation": ["css:.confirmation", "css:#confirmation", "css:.alert-success",
                           f"xpath://*[{_ci_contains('order has been')} or {_ci_contains('confirmation number')}]"],
    "trade_error": ["css:.alert-danger", "css:.validation-summary-errors", "css:.error-message",
                    "css:.error", "css:[role='alert']"],
}

# Visible option text for <select>/radio choices, tried in order (substring, case-insensitive).
DEFAULT_OPTION_TEXT: dict[str, list[str]] = {
    "security_type": ["Stock", "Equit"],
    "BUY": ["Buy"],
    "SELL": ["Sell"],
    "order_type": ["Market"],
}

HOLDINGS_COLUMNS = {
    "ticker": ["symbol", "ticker"],
    "shares": ["shares", "quantity", "qty"],
    "purchase_price": ["purchase price", "price paid", "cost basis", "avg cost", "cost/share", "purchase"],
    "current_price": ["current price", "last price", "last", "price"],
    "market_value": ["market value", "current value", "value"],
    "gain_loss": ["gain/loss", "gain / loss", "gain", "g/l", "profit"],
    "position": ["position", "long/short", "l/s"],
}

PENDING_COLUMNS = {
    "ticker": ["symbol", "ticker"],
    "action": ["transaction", "action", "type", "buy/sell"],
    "shares": ["shares", "quantity", "qty"],
    "status": ["status"],
}

RANKINGS_COLUMNS = {
    "rank": ["rank", "#", "place"],
    "team": ["team name", "team", "portfolio"],
    "school": ["school", "advisor", "organization"],
    "region": ["region", "state", "coordinator"],
    "total_equity": ["total equity", "equity", "portfolio value", "value"],
    "pct_return": ["% return", "percent", "return", "% gain", "gain"],
}

# Labels next to account balances on the summary / holdings page.
BALANCE_LABELS = {
    "cash": ["cash balance", "cash available", "available cash", "cash"],
    "buying_power": ["buying power"],
    "total_equity": ["total equity", "account value", "portfolio value", "net equity"],
}


def merged_selectors(overrides: dict[str, Any] | None) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in DEFAULT_SELECTORS.items()}
    for key, value in (overrides or {}).items():
        values = [value] if isinstance(value, str) else list(value)
        out[key] = values + [v for v in out.get(key, []) if v not in values]
    return out
