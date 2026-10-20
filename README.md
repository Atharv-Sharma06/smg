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
python -m smg_trader pairs --pairs KO/PEP,XOM/CVX # z-score, hedge ratio, half-life for each pair
python -m smg_trader trade XOM short 20           # SHORT / COVER work like BUY / SELL
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
SMGScraper ──portfolio──▶ Strategy (ensemble) ──signals──▶ planner ──orders──▶ RuleValidator ──▶ TradeExecutor
                         PairsTrader ─────pair legs─────▶ (pairs + sizing or target weights)
     ▲                                                                                       │
     └──────────────── confirm: re-scrape holdings / pending orders ◀───────── place_order ◀──┘
Monitor: portfolio_history.csv (P&L, drawdown) · leaderboard.json (rank changes) · alerts (Slack/email/log)
```

| Module | Role |
|--------|------|
| `scraper.py` | Selenium login (with retries, re-login when the session expires), holdings, cash, pending orders, rankings, order entry, `inspect` |
| `strategies/` | `momentum` (vote counting), `fundamentals` (P/E, EPS growth, dividend yield), `technical` (50-day MA crossover, RSI), `ensemble` (weighted average), `pairs` (statistical arbitrage) |
| `planner.py` | One path from signals to orders, shared by live runs and backtests: pair trades, then the directional book |
| `sizing.py` | `signal` sizing (confidence × `max_position_pct`, longs and shorts), target → order diffs, fitting orders to cash |
| `allocation.py` | Target-weight portfolios: equal, confidence, inverse volatility, risk parity, mean-variance; caps and rebalancing |
| `pit.py` | Point-in-time store: saves what the bot saw each day so backtests can replay it |
| `rules.py` | SMG rule checks, each printed as PASS/FAIL/WARN |
| `executor.py` | Dry-run or live execution, linked orders all-or-nothing (with unwind), confirmation, `data/trades.csv` |
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
- **Pairs** (separate from the ensemble, see below) trades the spread between two related stocks.

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

### Short selling

Set `rules.allow_short_selling: true` to enable two more order types, `SHORT` (SMG "Short Sell")
and `COVER` (SMG "Short Cover"). Shorts are used in three places:

- **Signal sizing** (`portfolio.method: signal`): with `portfolio.short_exposure > 0`, a SELL
  signal on a stock you don't own opens a short of `max_position_pct × confidence` of equity. A
  BUY signal on a stock you're short covers it.
- **Target-weight portfolios**: the short book (see below).
- **Pairs trading**: one leg of every pair is a short.

Accounting follows a normal broker. The sale proceeds go into cash but can't be spent, and
`short_margin_requirement` (50%) of the short's value is held as collateral on top. Equity is
cash + longs − shorts. When SMG shows its own buying power, the bot never goes above it.

### Portfolio construction

`portfolio.method: signal` (the default) sizes each signal on its own. Any other method builds a
**target portfolio** and rebalances to it every run:

| Method | Weights |
|--------|---------|
| `equal` | Same weight for every name |
| `confidence` | Proportional to signal confidence |
| `inverse_vol` | Proportional to 1 / volatility, so calmer stocks get more |
| `risk_parity` | Each name contributes the same share of portfolio risk (uses the covariance matrix) |
| `mean_variance` | Long-only maximum risk-adjusted return. Expected return comes from the signal score |

Steps:
1. Pick the long book: BUY signals plus stocks you hold that aren't SELL, best `max_positions` by score.
2. Pick the short book: SELL signals plus shorts you hold that aren't BUY, if shorting is on.
3. Weight each book, scale it to `long_exposure` / `short_exposure`, and cap each name at
   `max_position_pct` (the excess goes to the other names).
4. Diff against current holdings. Positions that grew past target are **trimmed**, names that
   left the book are closed, and drifts under `rebalance_threshold` are left alone to avoid churn.

Covariance uses `cov_lookback_days` of daily returns, shrunk toward the diagonal by
`cov_shrinkage` so a short history doesn't produce extreme weights.

### Pairs trading

```yaml
rules: {allow_short_selling: true}
pairs:
  enabled: true
  pairs: [[KO, PEP], [XOM, CVX], [V, MA]]
```

For each pair (A, B), over the last `lookback_days`:
- the hedge ratio β is the regression slope of log A on log B, and the spread is log A − β·log B
- **z ≥ `entry_z`**: A is expensive relative to B, so **short A, buy B**. **z ≤ −`entry_z`**:
  buy A, short B.
- **exit** when |z| falls back under `exit_z`. **Stop** when |z| reaches `stop_z`, and never
  enter beyond it.
- a pair is only opened while it looks mean-reverting: daily-return correlation ≥
  `min_correlation` and spread half-life ≤ `max_half_life` days. Open pairs that stop looking
  mean-reverting are closed.

Legs are sized roughly dollar-neutral, 1 : β, within `capital_pct` of equity and the per-position
cap. That capital is held back from the directional book until the pair opens. Pair tickers are
kept out of the directional book so the two never fight over a position.

Both legs go out as **linked orders**. They're validated together, and if either fails, neither
is sent. If the first leg fills and the second fails at SMG, the bot reverses the first and sends
an alert (a leg left PENDING has to be cancelled by hand). Run `python -m smg_trader pairs` to see
each pair's statistics before you trade it.

### SMG rules enforced before every order

| Rule | Behaviour |
|------|-----------|
| NYSE / NASDAQ only | Exchange looked up from market data. An unknown exchange is rejected |
| Minimum $3/share | Applies to buys. Selling a position that fell below $3 is allowed, with a warning |
| Short sales | Need `allow_short_selling`, a price ≥ `min_short_price`, collateral, and no long position in the same stock. You can't cover more than you're short |
| Minimum 10 shares per trade | Opening orders need 10+. Closing out a smaller position is allowed |
| Whole shares only | Shares must be a positive integer |
| Cash (+ margin) | Cost plus commission must fit in cash minus `cash_reserve`, plus `margin_limit` if `use_margin`. Margin use triggers an interest-cost warning |
| Trading hours | 9:30–16:00 ET, Mon–Fri, excluding NYSE holidays (`--ignore-hours` works in dry-run only) |
| One active trader | A PID lock file (`data/trader.lock`) stops two bot processes from trading at once. Stale locks are reclaimed |
| Rate limiting | `max_trades_per_run`, `min_seconds_between_trades`, and a delay between page loads |
| Bot risk limits | `max_position_pct` per ticker (long or short), `max_gross_exposure` for longs + shorts, no duplicate order while one is pending |
| Linked orders | All legs of a pair must pass, or none are sent |

### Order confirmation

After submitting, the executor scrapes the portfolio again. If the share count changed by the
order size, the order is `CONFIRMED`. If it shows up in pending orders, it is `PENDING` (SMG
queues orders placed outside market hours). Otherwise it is `UNCONFIRMED`, and you get an alert.
Shorts are checked against the short position.

### Output files (`data/`)

| File | Contents |
|------|----------|
| `trades.csv` | Every order: timestamp, mode, ticker, action, shares, price, status, confidence, confirmation id, message, reason |
| `signals.csv` | Every signal from every run (useful for reviewing the dry-run week) |
| `portfolio_history.csv` | Daily equity, P&L, total return, peak, drawdown |
| `leaderboard.json` | Last NC top-10 snapshot |
| `runs/*.json` | Full snapshot of each run |
| `pit/` | Daily fundamentals and headlines, saved for point-in-time backtests |
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
python -m smg_trader backtest --strategy ensemble --method risk_parity # target-weight rebalancing
python -m smg_trader backtest --pairs KO/PEP,XOM/CVX --short           # pairs trading
```

On each trading day the strategy sees only data up to that day's close. Orders go through the
same planner and rules as live runs, linked legs stay all-or-nothing, and orders fill at the next
day's open with commission. Results are compared to SPY and to an equal-weighted buy-and-hold of
the universe.

**News, analyst and fundamentals in backtests.** These use *point-in-time* data: only what was
public on each day. Where it comes from:

| Input | Source for past dates |
|-------|----------------------|
| Fundamentals | Yahoo's reported quarterly EPS, by report date, gives trailing P/E and EPS growth; dividend history gives yield. Snapshots in `data/pit/` take priority |
| Analyst upgrades/downgrades | Yahoo's dated rating history, which goes back years |
| News headlines | Yahoo only keeps the latest headlines. The bot saves them every live run to `data/pit/`, so coverage builds up over time |
| Your own data | `--data-dir` with `fundamentals_history.csv` (date,ticker,pe_ratio,eps_growth,dividend_yield), `analyst.csv` (date,ticker,action,firm) and `news.csv` (date,ticker,headline). `date` is when it became public |

When nothing is known for a day, that input sits out instead of using today's value. The summary
shows coverage, for example `Point-in-time fundamentals 85%, analyst 60%, news 10%`, so you can
tell how much of the backtest actually used each input.

SMG doesn't export price history. For "2 months of SMG data", use Yahoo prices for the same
tickers and dates (the default), or put CSVs in `--data-dir`.

## Tests

```bash
pytest                    # 137 tests; the 16 Selenium tests skip if Chrome can't start
CHROME_BINARY=... CHROMEDRIVER_PATH=... pytest tests/test_scraper_selenium.py
```

The Selenium tests start `tests/mock_smg/server.py`, a stateful mock SMG site, and drive it with
real headless Chrome. They cover login, bad credentials, re-login after the session expires,
portfolio data types, cash, the NC leaderboard filter, buy, sell, short and cover orders,
SMG-side rejections, pending orders, a linked pair trade with an unwind, and a full live pipeline run. You can open the mock site yourself with
`python tests/mock_smg/server.py` (login `student1` / `secret`).

## Known limits

- The default selectors are general guesses until they're checked against the real site (see above).
- Holdings data on SMG can lag 30–45 days for disclosure. The bot trades from your own account's
  live holdings page, which isn't affected.
- Yahoo Finance data is unofficial and sometimes rate-limited. `market_data: csv` is the offline fallback.
- Check your SMG program's rules for the current commission, margin and short-selling terms (the
  $5 short minimum and 50% collateral are typical broker values, not confirmed SMG rules), and set
  them in `rules:`. Also check how SMG labels Short Sell / Short Cover on its trade page.
- Pairs need two stocks that really move together. A spread that has reverted in the past can
  stop reverting, which is what `stop_z` and the correlation/half-life checks are for.
- Point-in-time news coverage for past dates is thin until the bot has been saving headlines for a while.
