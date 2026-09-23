# SMG Automated Trader

A bot for [The Stock Market Game](https://www.stockmarketgame.org). It:

1. logs in with Selenium and scrapes your holdings, cash, pending orders and the NC rankings
2. generates buy/sell signals from three strategies (momentum, fundamentals, technical) and blends them
3. checks each order against SMG's rules, then places it through Selenium and confirms it went through
4. tracks P&L and drawdown, checks the leaderboard weekly, and sends Slack/email alerts

It runs in **dry-run mode by default**. To place real trades you need both `--live` on the command
line and `live_trading_enabled: true` (or `SMG_LIVE_TRADING=1`).

> **Selectors need a one-time tune-up.** This code was written without access to SMG's live HTML.
> The scraper finds elements in general ways (a password input, a table with a "Symbol" header, a
> link that says "Rankings") and is tested in a real browser against a mock SMG site
> (`tests/mock_smg/`). Run `inspect` (step 2 below) to capture the real pages, then put exact
> selectors in `config.yaml` wherever the defaults miss.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml     # edit team_name, universe, rules
cp .env.example .env                   # SMG_USERNAME / SMG_PASSWORD / SLACK_WEBHOOK_URL
```

You need Google Chrome installed. Selenium 4 finds or downloads a matching chromedriver on its own.
If it can't, set `CHROME_BINARY` and `CHROMEDRIVER_PATH` in `.env`.

## Getting started

| # | Step | Command |
|---|------|---------|
| 1 | Test the scraper: login, portfolio, cash, top 10 NC teams | `python -m smg_trader check` |
| 2 | Save the real SMG pages to `data/debug/` and fix selectors | `python -m smg_trader inspect --show-browser` |
| 3 | See the signals (no login needed) | `python -m smg_trader signals --strategy momentum` |
| 4 | Backtest momentum over the last 2 months | `python -m smg_trader backtest --strategy momentum --months 2` |
| 5 | Place your first test trade (10 shares of a stock over $3) | `python -m smg_trader trade KO buy 10` (dry run), then add `--live` |
| 6 | Set up Slack alerts | set `SLACK_WEBHOOK_URL` in `.env` |
| 7 | Dry-run for a week | `python -m smg_trader schedule` (logs signals and validated orders, submits nothing) |
| 8 | Go live | set `live_trading_enabled: true`, then `python -m smg_trader schedule --live` |

Other commands:

```bash
python -m smg_trader run                          # one full pipeline run, dry run
python -m smg_trader run --offline --ignore-hours # no login: paper portfolio, any time of day
python -m smg_trader run --live --leaderboard     # one live run plus a leaderboard check
python -m smg_trader leaderboard                  # check NC rankings now
python -m smg_trader -v ...                       # debug logging
```

If you'd rather use cron than the built-in scheduler (times in ET, so set `CRON_TZ` or convert to UTC):

```cron
CRON_TZ=America/New_York
0 10 * * 1-5  cd /path/to/smg && .venv/bin/python -m smg_trader run --live >> data/cron.log 2>&1
30 16 * * 5   cd /path/to/smg && .venv/bin/python -m smg_trader leaderboard >> data/cron.log 2>&1
```

## Tuning the scraper to the real SMG site

1. Run `python -m smg_trader inspect --show-browser`. It logs in and saves HTML and screenshots of
   the login, holdings, pending orders, trade and rankings pages to `data/debug/`.
2. Open the HTML files and find the right elements. Add overrides to `config.yaml`; they are tried
   before the built-in defaults in `smg_trader/selectors.py`:
   ```yaml
   smg:
     portfolio_url: https://.../AccountHoldings.aspx   # direct URLs are faster than link clicks
     rankings_url: https://.../Rankings.aspx
     selectors:
       holdings_table: "css:#ctl00_MainContent_gvHoldings"
       trade_symbol: "css:#ctl00_MainContent_txtSymbol"
       option_text: {security_type: ["Stocks"], BUY: ["Buy"], SELL: ["Sell"]}
   ```
3. Table columns are matched by **header text** (`Symbol`, `Shares`, `Current Price`, ...). Cash and
   equity are found by **label text** (`Cash Balance`, `Total Equity`). If SMG uses different words,
   add them to `HOLDINGS_COLUMNS` / `BALANCE_LABELS` in `selectors.py`.
4. Run `python -m smg_trader check` again until it prints your holdings correctly.

When a page can't be parsed, the scraper saves it to `data/debug/` automatically.

## How it works

```
SMGScraper ──portfolio──▶ Strategy (ensemble) ──signals──▶ sizing ──orders──▶ RuleValidator ──▶ TradeExecutor
     ▲                                                                                       │
     └──────────────── confirm: re-scrape holdings / pending orders ◀───────── place_order ◀──┘
Monitor: portfolio_history.csv (P&L, drawdown) · leaderboard.json (rank changes) · alerts (Slack/email/log)
```

| Module | Role |
|--------|------|
| `scraper.py` | Selenium login (with retries, re-login when the session expires), holdings, cash, pending orders, rankings, order entry, `inspect` |
| `strategies/` | `momentum` (vote counting), `fundamentals` (P/E, EPS growth, dividend yield), `technical` (50-day MA crossover, RSI), `ensemble` (weighted average) |
| `sizing.py` | Signals → whole-share orders. Sells come first; buys are sized by confidence × `max_position_pct` |
| `rules.py` | SMG rule checks, each printed as PASS/FAIL/WARN |
| `executor.py` | Dry-run or live execution, confirmation, `data/trades.csv` |
| `monitor.py` / `alerts.py` | Performance history, drawdown, rank-change and high-confidence alerts |
| `backtest.py` | Walk-forward backtest with no look-ahead; fills at the next day's open |
| `pipeline.py` / `scheduler.py` / `cli.py` | End-to-end run, APScheduler jobs, command line |

### Strategies

- **Momentum** counts votes from separate sources: price trend (20d and 60d), relative strength
  vs SPY, sector ETF trend, volume confirmation, closeness to the 52-week high, analyst
  upgrades/downgrades (last 30 days), and news-headline sentiment. It buys when at least
  `momentum_min_signals` sources are bullish and bulls outnumber bears; selling works the same
  way in reverse.
- **Fundamentals** scores P/E, EPS growth and dividend yield.
- **Technical** looks at 50-day MA crossovers (plus the 50/200 golden/death cross when there is
  enough data) and RSI(14) below 30 (oversold) or above 70 (overbought).
- **Ensemble** takes the weighted average of each strategy's score in [-1, 1]. Confidence is the
  absolute value of that average. A strategy that has no data for a ticker is left out and the
  remaining weights are rescaled.

Every signal has `ticker, action, confidence, score, reason`.

**Adding a strategy** takes one class and a config line. No core code changes:

```python
from smg_trader.strategies import Strategy, register_strategy

@register_strategy("mean_reversion")
class MeanReversion(Strategy):
    def score(self, ticker, ctx):
        closes = ctx.closes(ticker)            # history is capped at ctx.as_of (no look-ahead)
        if len(closes) < 20:
            return None                        # abstain
        window = closes.iloc[-20:]
        z = (closes.iloc[-1] - window.mean()) / window.std()
        return -z / 3, f"20d z-score {z:+.2f}", {"z": z}
```
```yaml
strategy:
  weights: {momentum: 0.4, technical: 0.3, fundamentals: 0.1, mean_reversion: 0.2}
```

Import the module before the pipeline runs, for example from `smg_trader/strategies/__init__.py`.
You can also add a momentum source by writing a `(ticker, ctx) -> Vote | None` function and adding
it to `DEFAULT_SOURCES`.

### SMG rules enforced before every order

| Rule | Behaviour |
|------|-----------|
| NYSE / NASDAQ only | Exchange looked up from market data. An unknown exchange is rejected |
| Minimum $3/share | Applies to buys. Selling a position that fell below $3 is allowed, with a warning |
| Minimum 10 shares per trade | Buys need 10+. A sell that closes out a smaller position is allowed |
| Whole shares only | Shares must be a positive integer |
| Cash (+ margin) | Cost plus commission must fit in cash minus `cash_reserve`, plus `margin_limit` if `use_margin`. Margin use triggers an interest-cost warning |
| Trading hours | 9:30–16:00 ET, Mon–Fri, excluding NYSE holidays (`--ignore-hours` works in dry-run only) |
| One active trader | A PID lock file (`data/trader.lock`) stops two bot processes from trading at once. Stale locks are reclaimed |
| Rate limiting | `max_trades_per_run`, `min_seconds_between_trades`, and a delay between page loads |
| Bot risk limits | `max_position_pct` per ticker; no duplicate order while one is pending; no short selling unless enabled |

### Order confirmation

After submitting, the executor scrapes the portfolio again. If the share count changed by the
order size, the order is `CONFIRMED`. If it shows up in pending orders, it is `PENDING` (SMG
queues orders placed outside market hours). Otherwise it is `UNCONFIRMED`, and you get an alert.

### Output files (`data/`)

| File | Contents |
|------|----------|
| `trades.csv` | Every order: timestamp, mode, ticker, action, shares, price, status, confidence, confirmation id, message, reason |
| `signals.csv` | Every signal from every run (useful for reviewing the dry-run week) |
| `portfolio_history.csv` | Daily equity, P&L, total return, peak, drawdown |
| `leaderboard.json` | Last NC top-10 snapshot |
| `runs/*.json` | Full snapshot of each run |
| `backtest/` | Equity curve, trades, summary |
| `debug/` | Saved HTML and screenshots |
| `bot.log` | Log output |

### Alerts

| Event | When |
|-------|------|
| `trade` | A live order was submitted, confirmed, pending or unconfirmed |
| `rank_change` | Your NC rank changed, or new teams entered the top 10 |
| `high_confidence` | A BUY/SELL signal had confidence above 0.8 |
| `drawdown` | Equity fell `drawdown_threshold` below its peak (alerts once when crossing) |
| `error` | A run, order or leaderboard check failed |

Channels are the log (always), Slack (`SLACK_WEBHOOK_URL`) and email (`SMTP_*` + `ALERT_EMAIL_TO`).

## Backtesting

```bash
python -m smg_trader backtest --strategy momentum --months 2           # Yahoo Finance data
python -m smg_trader backtest --start 2026-07-01 --end 2026-08-31 --tickers AAPL,MSFT,KO
python -m smg_trader backtest --data-dir data/prices                   # your own CSVs: Date,Open,High,Low,Close,Volume
```

On each trading day the strategy sees only data up to that day's close. Orders are sized and
validated with the live rules and filled at the next day's open, with commission. Analyst, news
and fundamentals inputs sit out of backtests, because only today's values exist for them.
Results are compared to SPY and to an equal-weighted buy-and-hold of the universe.

SMG doesn't export price history. For "2 months of SMG data", use Yahoo prices for the same
tickers and dates (the default), or put CSVs in `--data-dir`.

## Tests

```bash
pytest                    # 82 tests; the Selenium tests skip if Chrome can't start
CHROME_BINARY=... CHROMEDRIVER_PATH=... pytest tests/test_scraper_selenium.py
```

The Selenium tests start `tests/mock_smg/server.py`, a stateful mock SMG site, and drive it with
real headless Chrome. They cover login, bad credentials, re-login after the session expires,
portfolio data types, cash, the NC leaderboard filter, buy and sell orders, SMG-side rejections,
pending orders, and a full live pipeline run. You can open the mock site yourself with
`python tests/mock_smg/server.py` (login `student1` / `secret`).

## Known limits

- The default selectors are general guesses until they're checked against the real site (see above).
- Holdings data on SMG can lag 30–45 days for disclosure. The bot trades from your own account's
  live holdings page, which isn't affected.
- Yahoo Finance data is unofficial and sometimes rate-limited. `market_data: csv` is the offline fallback.
- Check your SMG program's rules for the current commission and margin terms, and set them in
  `rules:`.
