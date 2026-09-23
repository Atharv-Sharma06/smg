"""A small stateful imitation of the SMG website for Selenium tests.

It is NOT a copy of stockmarketgame.org's HTML (which we could not inspect);
it uses a similar shape (ASP.NET-style field names, link navigation,
label/value balance tables, preview-then-place trade flow) so the scraper's
generic locators and table parsing are exercised in a real browser.
"""

from __future__ import annotations

import html
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PRICES = {"AAPL": 190.00, "KO": 60.00, "MSFT": 410.00, "PENNY": 2.50, "XOM": 110.00}


class MockSMGState:
    def __init__(self, username: str = "student1", password: str = "secret"):
        self.username = username
        self.password = password
        self.sessions: set[str] = set()
        self.cash = 50_000.00
        self.holdings: dict[str, dict] = {
            "AAPL": {"shares": 100, "purchase": 170.00},
            "KO": {"shares": 50, "purchase": 58.00},
        }
        self.pending: list[dict] = []
        self.orders: list[dict] = []
        self.hold_orders_pending = False  # when True, trades go to pending instead of filling
        self.login_attempts = 0
        self.rankings = [
            (1, "Tar Heel Traders", "Chapel Hill HS", "North Carolina", 112_400.0, 12.4),
            (2, "Bull Durham", "Durham School of Arts", "North Carolina", 109_900.0, 9.9),
            (3, "Old Dominion Capital", "Richmond HS", "Virginia", 109_000.0, 9.0),
            (4, "Queen City Quants", "Charlotte Latin", "North Carolina", 106_100.0, 6.1),
            (5, "Wolfpack Wealth", "Raleigh Charter", "North Carolina", 104_800.0, 4.8),
        ]

    @property
    def equity(self) -> float:
        return self.cash + sum(PRICES[t] * h["shares"] for t, h in self.holdings.items())


def _page(title: str, body: str, logged_in: bool = True) -> str:
    nav = ""
    if logged_in:
        nav = (
            '<nav><a href="/home">Home</a> | <a href="/holdings">Account Holdings</a> | '
            '<a href="/trade">Enter a Trade</a> | <a href="/pending">Pending Orders</a> | '
            '<a href="/rankings">Rankings</a> | <a href="/logout">Log Out</a></nav>'
        )
    return (f"<!doctype html><html><head><title>{title}</title></head><body>{nav}"
            f"<h1>{title}</h1>{body}</body></html>")


def make_handler(state: MockSMGState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        # -------------------------------------------------------------- utils
        def _session(self) -> str | None:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            sid = cookie.get("sid")
            return sid.value if sid and sid.value in state.sessions else None

        def _send(self, content: str, status: int = 200, headers: dict | None = None) -> None:
            data = content.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str, headers: dict | None = None) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode()
            return {k: v[0] for k, v in parse_qs(raw).items()}

        # -------------------------------------------------------------- routes
        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path in ("/", "/login.html"):
                return self._send(self._login_page())
            if not self._session():
                return self._redirect("/login.html")
            if url.path == "/logout":
                state.sessions.discard(self._session())
                return self._redirect("/login.html")
            route = {
                "/home": self._home, "/holdings": self._holdings, "/trade": self._trade_form,
                "/pending": self._pending, "/rankings": lambda: self._rankings(parse_qs(url.query)),
            }.get(url.path)
            if route is None:
                return self._send(_page("Not found", "<p>404</p>"), 404)
            self._send(route())

        def do_POST(self) -> None:
            url = urlparse(self.path)
            form = self._form()
            if url.path == "/login.html":
                state.login_attempts += 1
                if form.get("ctl00$txtUserName") == state.username and form.get("ctl00$txtPassword") == state.password:
                    sid = secrets.token_hex(8)
                    state.sessions.add(sid)
                    return self._redirect("/home", {"Set-Cookie": f"sid={sid}; Path=/"})
                return self._send(self._login_page(error="Invalid user name or password."))
            if not self._session():
                return self._redirect("/login.html")
            if url.path == "/trade/preview":
                return self._send(self._trade_preview(form))
            if url.path == "/trade/place":
                return self._send(self._trade_place(form))
            self._send(_page("Not found", "<p>404</p>"), 404)

        # -------------------------------------------------------------- pages
        def _login_page(self, error: str = "") -> str:
            err = f'<div class="validation-summary-errors">{html.escape(error)}</div>' if error else ""
            body = f"""
            {err}
            <form method="post" action="/login.html">
              <label for="txtUserName">User Name</label>
              <input type="text" id="txtUserName" name="ctl00$txtUserName">
              <label for="txtPassword">Password</label>
              <input type="password" id="txtPassword" name="ctl00$txtPassword">
              <input type="submit" value="Log In" id="btnLogin">
            </form>"""
            return _page("Student Login", body, logged_in=False)

        def _home(self) -> str:
            return _page("Welcome", "<p>Team: Tar Heel Traders</p>")

        def _holdings(self) -> str:
            summary = f"""
            <table class="account-summary">
              <tr><td>Cash Balance</td><td>${state.cash:,.2f}</td></tr>
              <tr><td>Buying Power</td><td>${state.cash + 50_000:,.2f}</td></tr>
              <tr><td>Total Equity</td><td>${state.equity:,.2f}</td></tr>
            </table>"""
            rows = ""
            for t, h in state.holdings.items():
                px = PRICES[t]
                value = px * h["shares"]
                gl = (px - h["purchase"]) * h["shares"]
                gl_txt = f"${gl:,.2f}" if gl >= 0 else f"(${-gl:,.2f})"
                rows += (f"<tr><td>{t}</td><td>{t} Corp</td><td>Long</td><td>{h['shares']:,}</td>"
                         f"<td>${h['purchase']:,.2f}</td><td>${px:,.2f}</td><td>${value:,.2f}</td>"
                         f"<td>{gl_txt}</td></tr>")
            if not state.holdings:
                table = "<p>You have no holdings.</p>"
            else:
                table = f"""
                <table id="gvHoldings">
                  <tr><th colspan="8">Stock Holdings</th></tr>
                  <tr><th>Symbol</th><th>Description</th><th>Position</th><th>Shares</th>
                      <th>Purchase Price</th><th>Current Price</th><th>Market Value</th><th>Gain/Loss</th></tr>
                  {rows}
                  <tr><td>Total</td><td></td><td></td><td></td><td></td><td></td>
                      <td>${state.equity - state.cash:,.2f}</td><td></td></tr>
                </table>"""
            return _page("Account Holdings", summary + table)

        def _pending(self) -> str:
            rows = "".join(
                f"<tr><td>{p['ticker']}</td><td>{p['action']}</td><td>{p['shares']}</td><td>Pending</td></tr>"
                for p in state.pending
            )
            return _page("Pending Orders", f"""
            <table id="gvPending"><tr><th>Symbol</th><th>Transaction</th><th>Shares</th><th>Status</th></tr>
            {rows}</table>""")

        def _rankings(self, query: dict) -> str:
            region = query.get("ctl00$ddlRegion", ["All"])[0]
            rows = [r for r in state.rankings if region in ("All", r[3])]
            opts = "".join(f'<option{" selected" if o == region else ""}>{o}</option>'
                           for o in ("All", "North Carolina", "Virginia"))
            body_rows = "".join(
                f"<tr><td>{i}</td><td>{team}</td><td>{school}</td><td>{reg}</td>"
                f"<td>${eq:,.2f}</td><td>{ret:.2f}%</td></tr>"
                for i, (_, team, school, reg, eq, ret) in enumerate(rows, 1)
            )
            return _page("Rankings", f"""
            <form method="get" action="/rankings">
              <select name="ctl00$ddlRegion" id="ddlRegion">{opts}</select>
              <input type="submit" value="Go">
            </form>
            <table class="rankings">
              <tr><th>Rank</th><th>Team Name</th><th>School</th><th>Region</th>
                  <th>Total Equity</th><th>% Return</th></tr>
              {body_rows}
            </table>""")

        def _trade_form(self, error: str = "") -> str:
            err = f'<div class="alert-danger">{html.escape(error)}</div>' if error else ""
            return _page("Enter a Trade", f"""
            {err}
            <form method="post" action="/trade/preview">
              <select name="ctl00$ddlSecurityType"><option>Mutual Funds</option><option>Stocks</option></select>
              <select name="ctl00$ddlTransactionType">
                <option value="">-- choose --</option><option>Buy</option><option>Sell</option>
                <option>Short Sell</option><option>Short Cover</option>
              </select>
              <input type="text" name="ctl00$txtSymbol">
              <input type="text" name="ctl00$txtShares">
              <select name="ctl00$ddlOrderType"><option>Limit</option><option>Market</option></select>
              <input type="submit" value="Preview Order">
            </form>""")

        def _trade_preview(self, form: dict) -> str:
            symbol = form.get("ctl00$txtSymbol", "").upper().strip()
            action = form.get("ctl00$ddlTransactionType", "")
            shares = form.get("ctl00$txtShares", "")
            if form.get("ctl00$ddlSecurityType") != "Stocks":
                return self._trade_form("Please choose a security type of Stocks.")
            if symbol not in PRICES:
                return self._trade_form(f"Symbol {symbol} was not found.")
            if action not in ("Buy", "Sell"):
                return self._trade_form("Choose a transaction type.")
            if not shares.isdigit():
                return self._trade_form("Shares must be a whole number.")
            hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in form.items())
            return _page("Preview Order", f"""
            <p>{action} {shares} shares of {symbol} at market (~${PRICES[symbol]:.2f})</p>
            <form method="post" action="/trade/place">{hidden}
              <input type="submit" value="Place Order"></form>""")

        def _trade_place(self, form: dict) -> str:
            symbol = form["ctl00$txtSymbol"].upper().strip()
            action = form["ctl00$ddlTransactionType"]
            shares = int(form["ctl00$txtShares"])
            price = PRICES[symbol]
            if action == "Buy" and shares * price > state.cash:
                return self._trade_form("Insufficient cash for this order.")
            held = state.holdings.get(symbol, {}).get("shares", 0)
            if action == "Sell" and shares > held:
                return self._trade_form("You cannot sell more shares than you own.")
            conf = secrets.token_hex(4).upper()
            state.orders.append({"ticker": symbol, "action": action, "shares": shares, "confirmation": conf})
            if state.hold_orders_pending:
                state.pending.append({"ticker": symbol, "action": action, "shares": shares})
            elif action == "Buy":
                state.cash -= shares * price
                h = state.holdings.setdefault(symbol, {"shares": 0, "purchase": price})
                h["shares"] += shares
            else:
                state.cash += shares * price
                state.holdings[symbol]["shares"] -= shares
                if state.holdings[symbol]["shares"] == 0:
                    del state.holdings[symbol]
            return _page("Order Confirmation", f"""
            <div class="confirmation">Your order has been placed. Confirmation Number: {conf}</div>""")

    return Handler


class MockSMGServer:
    def __init__(self, state: MockSMGState | None = None):
        self.state = state or MockSMGState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> MockSMGServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":  # manual poking: python tests/mock_smg/server.py
    with MockSMGServer() as srv:
        print(f"Mock SMG at {srv.url}/login.html (student1 / secret). Ctrl+C to stop.")
        try:
            srv.thread.join()
        except KeyboardInterrupt:
            pass
