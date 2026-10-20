"""Selenium client for stockmarketgame.org.

Reads the portfolio, balances, pending orders and rankings, and places
trades. All locators live in :mod:`smg_trader.selectors` and can be overridden
from config.yaml, so adapting to SMG's HTML means editing config, not code.

Usage::

    with SMGScraper(cfg.smg) as smg:
        portfolio = smg.get_portfolio()
        board = smg.get_leaderboard(top_n=10)
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import Select, WebDriverWait

from .config import SMGConfig
from .models import Holding, LeaderboardEntry, PendingOrder, Portfolio, TradeOrder
from .parsing import map_columns, parse_int, parse_number, parse_ticker
from .selectors import (
    BALANCE_LABELS,
    DEFAULT_OPTION_TEXT,
    HOLDINGS_COLUMNS,
    PENDING_COLUMNS,
    RANKINGS_COLUMNS,
    merged_selectors,
)

log = logging.getLogger(__name__)

_BY = {"css": By.CSS_SELECTOR, "xpath": By.XPATH, "link": By.PARTIAL_LINK_TEXT,
       "id": By.ID, "name": By.NAME}

# Reads a table into rows of cell texts in one round trip.
_TABLE_JS = """
const t = arguments[0];
return Array.from(t.rows).map(r => Array.from(r.cells).map(c => (c.innerText || '').trim()));
"""


class SMGError(RuntimeError):
    pass


class LoginError(SMGError):
    pass


class OrderError(SMGError):
    pass


def parse_locator(locator: str) -> tuple[str, str]:
    prefix, sep, rest = locator.partition(":")
    if sep and prefix in _BY:
        return _BY[prefix], rest
    return By.CSS_SELECTOR, locator


def make_chrome_driver(cfg: SMGConfig) -> WebDriver:
    opts = webdriver.ChromeOptions()
    if cfg.headless:
        opts.add_argument("--headless=new")
    for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--window-size=1400,1000",
                "--disable-gpu", "--lang=en-US"):
        opts.add_argument(arg)
    if cfg.chrome_binary:
        opts.binary_location = cfg.chrome_binary
    service = Service(cfg.chromedriver_path) if cfg.chromedriver_path else Service()
    driver = webdriver.Chrome(options=opts, service=service)
    driver.set_page_load_timeout(cfg.page_load_timeout)
    return driver


class SMGScraper:
    def __init__(self, cfg: SMGConfig, driver_factory: Callable[[SMGConfig], WebDriver] = make_chrome_driver):
        self.cfg = cfg
        self.sel = merged_selectors(cfg.selectors)
        self.options = {**DEFAULT_OPTION_TEXT, **cfg.selectors.get("option_text", {})}
        self._driver_factory = driver_factory
        self.driver: WebDriver | None = None
        self._logged_in = False
        self._last_request = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> SMGScraper:
        if self.driver is None:
            self.driver = self._driver_factory(self.cfg)
        return self

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
        self.driver = None
        self._logged_in = False

    def __enter__(self) -> SMGScraper:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def d(self) -> WebDriver:
        if self.driver is None:
            self.start()
        assert self.driver is not None
        return self.driver

    # ------------------------------------------------------------------ helpers
    def _throttle(self) -> None:
        wait = self._last_request + self.cfg.min_seconds_between_requests - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, url: str) -> None:
        self._throttle()
        log.debug("GET %s", url)
        self.d.get(url)

    def _locators(self, key: str) -> list[tuple[str, str]]:
        if key not in self.sel:
            raise KeyError(f"No selectors configured for {key!r}")
        return [parse_locator(s) for s in self.sel[key]]

    def find(self, key: str, *, timeout: float | None = None, required: bool = True,
             visible: bool = False, root: Any = None) -> WebElement | None:
        """First element matching any locator for ``key``, waiting up to ``timeout``."""
        timeout = self.cfg.element_timeout if timeout is None else timeout
        locators = self._locators(key)
        scope = root or self.d

        def probe(_: Any) -> WebElement | bool:
            for by, value in locators:
                try:
                    for el in scope.find_elements(by, value):
                        if not visible or el.is_displayed():
                            return el
                except (NoSuchElementException, StaleElementReferenceException):
                    continue
                except WebDriverException as exc:  # invalid selector etc.
                    log.debug("locator %s=%s failed: %s", by, value, exc)
            return False

        try:
            found = probe(None) if timeout <= 0 else WebDriverWait(self.d, timeout, 0.25).until(probe)
        except TimeoutException:
            found = False
        if found:
            return found  # type: ignore[return-value]
        if required:
            raise SMGError(f"Element {key!r} not found (tried {len(locators)} selectors on {self.d.current_url})")
        return None

    def read_table(self, table: WebElement) -> tuple[list[str], list[list[str]]]:
        rows: list[list[str]] = self.d.execute_script(_TABLE_JS, table) or []
        rows = [r for r in rows if any(c for c in r)]
        if not rows:
            return [], []
        header_idx = 0
        # Some tables have a title row spanning all columns; use the widest early row as header.
        for i, row in enumerate(rows[:3]):
            if len(row) > len(rows[header_idx]):
                header_idx = i
        headers = rows[header_idx]
        body = [r for r in rows[header_idx + 1:] if len(r) >= max(2, len(headers) - 1)]
        return headers, body

    def value_by_label(self, labels: list[str]) -> float | None:
        """Find a number displayed next to a label such as 'Cash Balance'."""
        for label in labels:
            pred = ("translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ:', "
                    f"'abcdefghijklmnopqrstuvwxyz') = '{label.lower()}'")
            xpath = f"//*[not(self::script) and not(self::style) and {pred}]"
            for el in self.d.find_elements(By.XPATH, xpath):
                candidates = el.find_elements(By.XPATH, "following-sibling::*[1]")
                candidates += el.find_elements(By.XPATH, "../following-sibling::*[1]")
                for c in candidates:
                    value = parse_number(c.text)
                    if value is not None:
                        return value
                parent_text = el.find_element(By.XPATH, "..").text
                rest = parent_text.replace(el.text, "", 1)
                value = parse_number(rest)
                if value is not None:
                    return value
        return None

    def dump_page(self, name: str) -> Path:
        """Save HTML + screenshot to the debug dir (for writing selectors)."""
        out = Path(self.cfg.debug_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = out / f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '_', name.lower())}"
        stem.with_suffix(".html").write_text(self.d.page_source, encoding="utf-8")
        try:
            self.d.save_screenshot(str(stem.with_suffix(".png")))
        except WebDriverException:
            pass
        log.info("saved %s.html/.png (%s)", stem, self.d.current_url)
        return stem

    def _open(self, url: str, nav_key: str) -> None:
        self.ensure_logged_in()
        for attempt in range(2):
            if url:
                self._get(url)
            else:
                link = self.find(nav_key)
                self._throttle()
                link.click()  # type: ignore[union-attr]
            WebDriverWait(self.d, self.cfg.page_load_timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            if self.is_logged_in():
                return
            # The server session expired and we were bounced to the login page.
            log.info("SMG session expired; logging in again")
            self._logged_in = False
            if attempt == 0:
                self.login()
        raise SMGError(f"Still not logged in after opening {url or nav_key}")

    # ------------------------------------------------------------------ session
    def is_logged_in(self) -> bool:
        if self.driver is None:
            return False
        return self.find("logged_in_marker", timeout=0, required=False) is not None

    def ensure_logged_in(self) -> None:
        if not (self._logged_in and self.is_logged_in()):
            self.login()

    def login(self) -> None:
        if not self.cfg.username or not self.cfg.password:
            raise LoginError("SMG_USERNAME / SMG_PASSWORD are not set")
        last_error = "unknown error"
        for attempt in range(1, self.cfg.login_retries + 1):
            try:
                self._get(self.cfg.login_url)
                user = self.find("login_username", visible=True)
                pwd = self.find("login_password", visible=True)
                user.clear()  # type: ignore[union-attr]
                user.send_keys(self.cfg.username)  # type: ignore[union-attr]
                pwd.clear()  # type: ignore[union-attr]
                pwd.send_keys(self.cfg.password)  # type: ignore[union-attr]
                submit = self.find("login_submit", visible=True, required=False, timeout=2)
                if submit is not None:
                    submit.click()
                else:
                    pwd.submit()  # type: ignore[union-attr]

                deadline = time.monotonic() + self.cfg.element_timeout
                while time.monotonic() < deadline:
                    if self.is_logged_in():
                        self._logged_in = True
                        log.info("Logged in to SMG as %s", self.cfg.username)
                        return
                    err = self.find("login_error", timeout=0, required=False, visible=True)
                    if err is not None and err.text.strip():
                        raise LoginError(f"SMG rejected login: {err.text.strip()}")
                    time.sleep(0.5)
                last_error = "timed out waiting for the logged-in page"
            except LoginError as exc:
                self._safe_dump("login_failed")
                if re.search(r"invalid|incorrect|password|not found|locked", str(exc), re.I):
                    raise  # bad credentials: retrying will not help and may lock the account
                last_error = str(exc)
            except (SMGError, WebDriverException) as exc:
                last_error = str(exc).splitlines()[0]
            log.warning("login attempt %d/%d failed: %s", attempt, self.cfg.login_retries, last_error)
            self._safe_dump(f"login_attempt_{attempt}")
            time.sleep(min(2 ** attempt, 10))
        raise LoginError(f"Login failed after {self.cfg.login_retries} attempts: {last_error}")

    def _safe_dump(self, name: str) -> None:
        try:
            self.dump_page(name)
        except Exception:  # debugging aid only
            pass

    # ------------------------------------------------------------------ portfolio
    def get_holdings(self) -> list[Holding]:
        self._open(self.cfg.portfolio_url, "nav_portfolio")
        table = self.find("holdings_table", required=False)
        if table is None:
            if self.find("no_holdings_marker", timeout=0, required=False) is not None:
                return []
            self._safe_dump("holdings_not_found")
            raise SMGError("Holdings table not found; run `inspect` and set smg.selectors.holdings_table")
        headers, rows = self.read_table(table)
        cols = map_columns(headers, HOLDINGS_COLUMNS)
        missing = {"ticker", "shares"} - cols.keys()
        if missing:
            raise SMGError(f"Holdings table headers {headers} missing columns for {sorted(missing)}")
        return [h for row in rows if (h := self._holding_from_row(row, cols)) is not None]

    @staticmethod
    def _holding_from_row(row: list[str], cols: dict[str, int]) -> Holding | None:
        def cell(name: str) -> str | None:
            i = cols.get(name)
            return row[i] if i is not None and i < len(row) else None

        ticker = parse_ticker(cell("ticker"))
        shares = parse_int(cell("shares"))
        if not ticker or shares is None:
            return None  # totals / subtotal rows
        price = parse_number(cell("current_price"))
        value = parse_number(cell("market_value"))
        if price is None and value is not None and shares:
            price = abs(value) / abs(shares)
        if value is None and price is not None:
            value = price * abs(shares)
        position = (cell("position") or "").upper()
        short = "SHORT" in position or shares < 0
        return Holding(
            ticker=ticker,
            shares=abs(shares),
            current_price=price or 0.0,
            market_value=abs(value or 0.0),
            purchase_price=parse_number(cell("purchase_price")),
            gain_loss=parse_number(cell("gain_loss")),
            position="SHORT" if short else "LONG",
        )

    def get_balances(self) -> dict[str, float | None]:
        if self.cfg.account_summary_url:
            self._open(self.cfg.account_summary_url, "nav_account_summary")
        return {key: self.value_by_label(labels) for key, labels in BALANCE_LABELS.items()}

    def get_cash(self) -> float:
        self._open(self.cfg.account_summary_url or self.cfg.portfolio_url, "nav_portfolio")
        cash = self.value_by_label(BALANCE_LABELS["cash"])
        if cash is None:
            self._safe_dump("cash_not_found")
            raise SMGError("Cash balance not found; set smg.selectors or check BALANCE_LABELS")
        return cash

    def get_pending_orders(self) -> list[PendingOrder]:
        if not self.cfg.pending_orders_url and not self.sel.get("nav_pending"):
            return []
        try:
            self._open(self.cfg.pending_orders_url, "nav_pending")
        except SMGError:
            log.info("no pending orders page found")
            return []
        table = self.find("pending_table", required=False, timeout=3)
        if table is None:
            return []
        headers, rows = self.read_table(table)
        cols = map_columns(headers, PENDING_COLUMNS)
        out = []
        for row in rows:
            ticker = parse_ticker(row[cols["ticker"]]) if "ticker" in cols else None
            if not ticker:
                continue
            out.append(PendingOrder(
                ticker=ticker,
                action=row[cols["action"]] if "action" in cols else "",
                shares=parse_int(row[cols["shares"]]) or 0 if "shares" in cols else 0,
                status=row[cols["status"]] if "status" in cols else "",
            ))
        return out

    def get_portfolio(self) -> Portfolio:
        holdings = self.get_holdings()
        balances = {key: self.value_by_label(labels) for key, labels in BALANCE_LABELS.items()}
        if balances["cash"] is None or balances["total_equity"] is None:
            balances.update({k: v for k, v in self.get_balances().items() if v is not None})
        cash = balances["cash"]
        if cash is None:
            self._safe_dump("cash_not_found")
            raise SMGError("Cash balance not found; run `inspect` and adjust BALANCE_LABELS/selectors")
        invested = sum(h.market_value * (-1 if h.position == "SHORT" else 1) for h in holdings)
        portfolio = Portfolio(
            cash=cash,
            total_equity=balances["total_equity"] or cash + invested,
            holdings=holdings,
            buying_power=balances["buying_power"],
            pending_orders=self.get_pending_orders(),
        )
        log.info("Portfolio: cash $%s, equity $%s, %d holdings, %d pending",
                 f"{portfolio.cash:,.2f}", f"{portfolio.total_equity:,.2f}", len(holdings), len(portfolio.pending_orders))
        return portfolio

    # ------------------------------------------------------------------ rankings
    def get_leaderboard(self, top_n: int = 10) -> list[LeaderboardEntry]:
        self._open(self.cfg.rankings_url, "nav_rankings")
        region = self.cfg.region_filter
        select_el = self.find("region_select", timeout=2, required=False)
        if region and select_el is not None:
            if self._select_option(select_el, [region, "NC"]):
                button = self.find("region_apply", timeout=1, required=False, visible=True)
                if button is not None:
                    self._throttle()
                    button.click()
                time.sleep(1)
        table = self.find("rankings_table", required=False)
        if table is None:
            self._safe_dump("rankings_not_found")
            raise SMGError("Rankings table not found; set smg.rankings_url / smg.selectors.rankings_table")
        headers, rows = self.read_table(table)
        cols = map_columns(headers, RANKINGS_COLUMNS)
        if "rank" not in cols:
            raise SMGError(f"Rankings table headers {headers} have no rank column")
        entries = []
        for row in rows:
            def cell(name: str) -> str:
                i = cols.get(name)
                return row[i] if i is not None and i < len(row) else ""

            rank = parse_int(cell("rank"))
            if rank is None:
                continue
            if region and "region" in cols and select_el is None:
                if not _region_matches(cell("region"), region):
                    continue
            entries.append(LeaderboardEntry(
                rank=rank,
                team=cell("team"),
                school=cell("school"),
                total_equity=parse_number(cell("total_equity")),
                pct_return=parse_number(cell("pct_return")),
            ))
        entries.sort(key=lambda e: e.rank)
        return entries[:top_n] if top_n else entries

    # ------------------------------------------------------------------ trading
    @staticmethod
    def _select_option(el: WebElement, texts: list[str]) -> bool:
        if el.tag_name.lower() != "select":
            return False
        sel = Select(el)
        options = [(o.text.strip(), (o.get_attribute("value") or "").strip()) for o in sel.options]
        for text in texts:
            t = text.lower()
            # Exact text/value first so "Sell" never picks "Short Sell".
            for i, (label, value) in enumerate(options):
                if t in (label.lower(), value.lower()):
                    sel.select_by_index(i)
                    return True
            for i, (label, _) in enumerate(options):
                if t in label.lower():
                    sel.select_by_index(i)
                    return True
        return False

    def _choose(self, key: str, option_key: str, required: bool = True) -> None:
        texts = self.options.get(option_key, [option_key])
        el = self.find(key, timeout=2, required=False)
        if el is not None and self._select_option(el, texts):
            return
        # Radio buttons / toggle buttons labelled "Buy" / "Sell".
        for text in texts:
            for xpath in (
                f"//label[normalize-space(.)='{text}']",
                f"//input[@type='radio' and translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                f"'abcdefghijklmnopqrstuvwxyz')='{text.lower()}']",
                f"//button[normalize-space(.)='{text}']",
            ):
                found = self.d.find_elements(By.XPATH, xpath)
                if found:
                    found[0].click()
                    return
        if required:
            raise OrderError(f"Could not choose {option_key!r} for {key}")

    def place_order(self, order: TradeOrder) -> str:
        """Submit a market order. Returns the confirmation text SMG shows."""
        self._open(self.cfg.trade_url, "nav_trade")
        self._choose("trade_security_type", "security_type", required=False)
        self._choose("trade_action", order.action.value)
        symbol = self.find("trade_symbol", visible=True)
        symbol.clear()  # type: ignore[union-attr]
        symbol.send_keys(order.ticker)  # type: ignore[union-attr]
        shares = self.find("trade_shares", visible=True)
        shares.clear()  # type: ignore[union-attr]
        shares.send_keys(str(int(order.shares)))  # type: ignore[union-attr]
        self._choose("trade_order_type", "order_type", required=False)

        preview = self.find("trade_preview", timeout=2, required=False, visible=True)
        if preview is not None:
            self._throttle()
            preview.click()
            self._raise_trade_error("preview")

        submit = self.find("trade_submit", visible=True)
        self._throttle()
        submit.click()  # type: ignore[union-attr]
        confirmation = self.find("trade_confirmation", required=False, visible=True)
        if confirmation is None:
            self._raise_trade_error("submit")
            self._safe_dump("order_no_confirmation")
            raise OrderError("No confirmation shown after submitting the order")
        text = confirmation.text.strip()
        log.info("SMG confirmation: %s", text)
        m = re.search(r"(?:confirmation|order)\s*(?:number|#|no\.?|id)?\s*[:#]?\s*([A-Z0-9-]*\d[A-Z0-9-]*)",
                      text, re.I)
        return m.group(1) if m else text[:200]

    def _raise_trade_error(self, stage: str) -> None:
        err = self.find("trade_error", timeout=1, required=False, visible=True)
        if err is not None and err.text.strip():
            self._safe_dump(f"order_error_{stage}")
            raise OrderError(f"SMG rejected order at {stage}: {err.text.strip()}")

    # ------------------------------------------------------------------ debugging
    def inspect(self) -> list[Path]:
        """Log in and save every page the bot uses, for writing selectors."""
        saved = []
        self._get(self.cfg.login_url)
        saved.append(self.dump_page("login"))
        self.login()
        saved.append(self.dump_page("home_after_login"))
        pages = [
            ("portfolio", self.cfg.portfolio_url, "nav_portfolio"),
            ("account_summary", self.cfg.account_summary_url, "nav_account_summary"),
            ("pending_orders", self.cfg.pending_orders_url, "nav_pending"),
            ("trade", self.cfg.trade_url, "nav_trade"),
            ("rankings", self.cfg.rankings_url, "nav_rankings"),
        ]
        for name, url, nav in pages:
            try:
                self._open(url, nav)
                saved.append(self.dump_page(name))
            except SMGError as exc:
                log.warning("could not open %s page: %s", name, exc)
        return saved


def _region_matches(cell: str, region: str) -> bool:
    cell_l, region_l = cell.strip().lower(), region.strip().lower()
    abbrev = {"north carolina": "nc"}.get(region_l, region_l)
    return region_l in cell_l or cell_l == abbrev
