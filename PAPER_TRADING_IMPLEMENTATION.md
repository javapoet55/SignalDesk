# Finviz Agent paper trading

## Discovery and implementation plan

The supplied workspace contained a runtime log. Railway identified production as a successful CLI deployment with one volume-backed service. All 38 deployed Python, requirements and screen configuration files matched the local application snapshot by SHA-256. The baseline branch preserves that snapshot plus the local Dockerfile and test fixtures. No upstream Git repository was present.

The app uses Python 3.12, ThreadingHTTPServer, server-rendered HTML, inline CSS/JS, native day/night controls, file-backed settings, journals and run artifacts. StrategyManager runs uploaded Python strategies in a background queue and caches Finviz or Massive bars. The scheduler and price refresher are in-process threads. Session cookies, optional OTP and a configured dashboard username provide a single authenticated principal. No ORM, billing service, multi-account directory, metrics collector or frontend build was found. Existing tests use unittest. Railway runs python server.py and exposes /healthz.

1. P0. Reuse session authorization, navigation, the mounted data directory and the Finviz client. Add a dedicated SQLite file, reversible schema, immutable cash/fill history and decimal sizing. Add portfolio, preview, confirmation, positions and history routes. Estimate 1–2 developer weeks including rollout validation.
2. P1. Add charts, snapshots, comparisons, research provenance, and journal views. Estimate 1–2 weeks including realistic data validation.
3. P2. Add entitlement checks, exports, research analytics and operational visibility. Commercial data and analytics licenses require owner review. Estimate 1–2 weeks plus external licensing lead time.

Main risks are stale or unavailable bars, upstream corporate actions, shared dashboard credentials, concurrent confirmations and volume durability. Keep market I/O out of database write transactions. Test with a fictional feed. Production deployment requires explicit approval.

## Run or review the feature

Use this directory as the build context. The local setup example uses zsh. The local Git repository has a baseline branch and a separate feature/paper-trading branch. No configured remote or production rollout is required for review.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR="$PWD/data"
read -rs "DASHBOARD_PASSWORD?Local dashboard password: "
export DASHBOARD_PASSWORD
python server.py
```

Sign in at http://localhost:8080/login, then select **Paper Trading**. The dashboard username defaults to admin. A valid Finviz Elite token enables minute bars. Without market data, portfolio management, journal and history still work. The ticket reports missing prices and execution is blocked. The tests exercise a fictional market independently of credentials and exchange opening hours.

## Simulation and data model

The core is long-only, USD-denominated equity simulation. Finviz supplies completed US regular-session minute bars. All order paths end in the local SQLite transaction. No brokerage SDK, order transport, or automatic strategy-to-fill path is present. Host TradingView watchlist sync stays on its existing routes.

* Portfolio capital, cash, fee, price, basis and shares use canonical decimal strings in SQLite. The ledger converts them to Python Decimal before arithmetic. Cash, notional and cost released on sale round to cents using half-even rounding. Quantity rounds down to six fractional decimals or whole shares. An explicit sale of an entire existing fractional position can close it after the settings change.
* Fixed share sizing uses the requested shares exactly. Dollar sizing divides dollars by reference price. Equity sizing divides the requested equity percentage by reference price. Cash-percentage buys reserve the flat fee inside the cash budget. A position-percentage sale multiplies current shares by the percentage. Other sizing methods add fees separately. A fill uses the approved minute close with zero modeled slippage. Unrepresentable, zero-notional or unaffordable trades fail validation.
* Buys capitalize fees into weighted-average cost. Partial sales release proportional cost at cent precision. The final sale releases all remaining basis. Realized P&L includes net sale proceeds less released basis and recorded cash dividends. Equity equals cash plus marked open positions. A missing position mark makes dependent totals unavailable. Historical marks retain the provider timestamp.
* A preview persists a Pending signal, the exact ticket, quote source and timestamp, sizing basis, before/after quantities and cash, and portfolio version. Each request key identifies one portfolio ticket. Confirmation rechecks the current price, market session, every preview mark's freshness through the oldest timestamp, portfolio version, ownership, and signal state. Concurrent modifications invalidate the old preview. The database serializes writes with BEGIN IMMEDIATE. An order, fill, position, cash movement and audit records commit together. Repeating confirmation returns the recorded outcome.
* A draft can be stored without a price. Editing creates a successor preview and closes the original draft or pending signal in the same transaction. Processing is an internal transaction state. Final outcomes are Simulated, Canceled, Expired or Rejected. A periodic sweep expires old previews. The transitions are guarded in SQL. Completed fills, orders, cash entries, snapshots, journal entries, corporate actions and audit events have update/delete guards.
* A confirmed split adjusts shares while preserving cost. A dividend adds cash and realized income. Each action records the original holding, resulting holding, effective-date/source text and request key. These are manual contemporaneous entries against current holdings. Record a correction with a linked journal entry and any appropriate subsequent confirmed simulation or corporate action. Never rewrite a historical fill.
* Reset archives the original and creates a successor. The old ledger and pending-signal cancellation audit remain available. Archive and reset require the current portfolio name. An archive freezes trading and stops scheduled sampling. Its overview still values remaining positions using available market marks.
* Forward migrations run on the first paper database access. The supplied down migration is destructive and is intended for disposable databases or a separately authorized rollback. For an application rollback, redeploy the prior image and preserve the volume. Use SQLite's backup API or a Railway volume backup before an approved production change. Copying a live WAL database file alone can omit committed data.

## HTTP interface

All paths below use the authenticated user's identity. Dollar and share amounts in responses are strings. Request objects should use decimal strings. Epoch times are UTC seconds. Errors return JSON with an error message and an HTTP status. The existing host login redirects unauthenticated browser requests. Paper-only identities authenticate at /paper/login.

| Method | Path | Purpose |
| --- | --- | --- |
| GET, POST | /paper/api/portfolios | List owned accounts or create one with name, capital, currency, description, benchmark, fractional, fee |
| GET | /paper/api/watchlist, /paper/api/securities | Scan tickers and most recent scan metadata for the dashboard principal. Optional research query selects a saved strategy result |
| GET | /paper/api/research?run=…&symbol=… | Saved output fields and provenance for a specific research result, available only to its dashboard owner |
| GET | /paper/api/bars?symbol=… | Up to 500 completed Finviz minute OHLCV bars |
| GET | /paper/api/entitlements | Effective account limits and enabled features |
| GET | /paper/api/portfolios/{id}/summary, /positions, /settings | Marked valuation, holdings, or lightweight settings |
| POST | …/{id}/settings | Update name, description, fee or fractional setting. For reset/archive send action and confirm_name |
| POST | …/{id}/draft, /preview | Enter side, symbol, method, amount, request_key, optional notes and saved research run. Preview accepts replaces for an unprocessed signal |
| POST | …/{id}/signals/{signal}/confirm, /cancel | Confirm an approved preview or cancel an eligible signal |
| GET | …/{id}/signals, /orders, /transactions, /ledger, /journal, /audit, /actions, /snapshots | Owner-scoped history. limit accepts 1–500, offset starts at zero. start/end are inclusive/exclusive UTC epochs. Signal/order/fill queries also support symbol, query, sort and direction |
| POST | …/{id}/journal | Save hypothesis, rationale, strategy, entry, exit, tags, observations, review and lessons with an optional signal ID |
| POST | …/{id}/action | Confirm a split multiplier or dividend per share with kind, symbol, value, reference, confirm_symbol and request_key |
| GET | …/{id}/analytics, /operations | Snapshot-based returns and health indicators |
| GET | …/{id}/export?kind=transactions, /report | CSV history and a downloadable PDF portfolio report |
| POST | …/{id}/explanation, /historical | Optional local AI explanation or separately licensed historical benchmark study |

Every JSON POST requires Content-Type application/json and X-Paper-Request 1. The maximum request body is 16 KiB. Mutations check the host origin for browser requests. For a preview, send a unique request_key of 8–80 alphanumeric, dash or underscore characters. A repeated key with a different ticket yields 409. The client must examine the returned signal status after confirm. A recorded terminal rejection or expiration is an auditable outcome even when the HTTP request succeeds.

Example preview body:

```json
{"symbol":"TEST","side":"Buy","method":"cash_pct","amount":"10","request_key":"example-preview-001","notes":"Fictional example"}
```

## Configuration and Railway

| Variable | Behavior |
| --- | --- |
| DATA_DIR, RAILWAY_VOLUME_MOUNT_PATH | Existing data directory resolution. The paper module stores paper/ledger.sqlite3 and its WAL sidecars here |
| DASHBOARD_USER, DASHBOARD_PASSWORD, LOGIN_OTP_* | Existing host authentication. Keep the dashboard account name stable since it owns that account's portfolios |
| FINVIZ_API_TOKEN or FINVIZ_API_KEY | Existing Finviz client. The provider handles retries and pacing. The paper adapter caches up to 128 symbols for 20 seconds and rejects bars older than 120 seconds for execution |
| PAPER_ACCOUNTS | Optional JSON object mapping separate usernames to salt:hash strings. Generate each value interactively with python -m paper_trading.identity. The scrypt parameters are N=16384, r=8, p=1 with 32-byte output. Store the resulting configuration as a Railway secret. These users open /paper/login and have private portfolios. They cannot read the dashboard's scan files or enter its strategy management routes |
| PAPER_ENTITLEMENTS | Optional JSON object with a wildcard default and principal-specific overrides. Keys are portfolios, history_days, research_days, advanced, ai, export, compute_credits. Named paper principals use paper/USERNAME. Access windows restrict what can be retrieved while retaining immutable ledger records |
| PAPER_PORTFOLIO_LIMIT, PAPER_EXPORT | Default active portfolio cap (25) and export switch (1). Per-principal overrides take precedence |
| PAPER_SNAPSHOT_SECONDS | Target interval between marked snapshots, default 900. One in-process loop checks each minute. It skips missing/stale marks, retries on subsequent cycles and logs failures without credentials |
| PAPER_LOCAL_MODEL | Optional Ollama model served on loopback port 11434. An account must also have ai enabled. The local model receives marked account facts and up to three recent journal entries. The UI renders the response as text under facts, assumptions and uncertainties |
| PAPER_VECTORBT_LICENSED | Explicit owner-controlled license flag. A provisioned VectorBT PRO install, advanced entitlement and positive daily compute allowance are also required. The adapter consumes a credit for a dispatched study. It has no ledger write capability for cash or positions |

The current Railway environment has one production service, one persistent volume, a Dockerfile build, dashboard authentication/OTP and Finviz and Massive credentials configured. Only variable names were inspected. A read-only SPY request from the running service returned 4,290 completed one-minute bars, ending at the November 25, 2026 15:59 New York bar. That bar began at 15:59 and finished at 16:00. The simulator reuses Finviz for bar execution. The existing strategy engine retains its provider selection. The optional enterprise model, license flag and additional paper logins are disabled by default.

For a Railway review environment, build from this directory, attach a dedicated persistent volume, set the dashboard credentials, optionally supply an authorized price feed, use python server.py and the assigned PORT, and check /healthz. Use one application replica per SQLite volume. Verify a portfolio survives a restart. Confirm successful sign-in, blocked stale quotes, preview/confirmation, export and snapshot behavior. Production promotion remains an explicit owner action. No production database migration or deployment was performed.

## Screens, performance and security

The eight views share native navigation and theme controls. The trading workspace includes the scan watchlist, completed minute candles, volume, a 20-bar moving average, fill markers and manual ticket. The overview and analytics use ledger-derived values. Sector exposure uses classifications from the most recent saved scan. Symbols without scan metadata show an unknown sector or company. Source run IDs, result CSV digests and the available strategy version are stored at signal creation. Newly executed strategies record their existing short script fingerprint in run metadata. Older runs can have an unknown source version.

Snapshots start with virtual capital, then record equity with a verified benchmark. Charts retain intraday samples from the latest week and daily closing samples earlier in the window. Maximum drawdown uses observed peaks. Volatility and Sharpe/Sortino use at least 20 consecutive-session return pairs, 252 sessions per year and a zero risk-free rate. Annualization requires one elapsed year. Holding duration is measured from the position's opening until each sale. No portfolio history is synthesized ahead of its creation or during data outages. The operations view shows snapshot time and rejected or expired signal counts.

The owner predicate is evaluated before reading or mutating accounts and child records. Published research files are available only to the authenticated dashboard principal. Paper-only sessions use random HttpOnly cookies, a paper-scoped path, twelve-hour expiry, and a global login-attempt budget. Regular API requests have a 180-per-minute budget per principal. The quote endpoint accepts a validated ticker only. Browser-generated HTML escapes data, the model integration has no order tools, and CSV text fields that could execute spreadsheet formulas receive a protective prefix. The ledger directory is created private. Production users require HTTPS. Direct filesystem access by an administrator remains outside the application's audit trust boundary.

SQLite WAL and five-second writer timeouts support this single-node workload. Market calls occur outside write transactions. History is indexed by owner/portfolio and time. A local warm benchmark of 120 alternating one-share preview/confirm pairs with an in-memory fictional provider measured 5.04 ms median and 6.39 ms p95, with the ending cash reconciling to the initial balance. Provider latency, larger holdings lists and SaaS traffic are outside this measurement. Valuation reads reprice open symbols. Bulk exports and historical analytics currently accumulate results in memory. Load-test high-volume accounts before sizing a public service.

## Validation and remaining work

Run core tests with python -m unittest test_paper_trading test_paper_http. To run the browser test install the Playwright test package in a development environment with Chrome available, then run PAPER_BROWSER_TEST=1 python -m unittest test_paper_browser. The test authenticates via the real server, creates and switches portfolios, tests invalid percentages, confirms a fictional buy and visits the responsive views. Optional PAPER_QA_SCREENSHOT writes the test workspace image.

Additional checks use node --check paper_trading/paper.js, ruff check --isolated paper_trading test_paper*.py, and mypy --follow-imports=silent --ignore-missing-imports --check-untyped-defs paper_trading. Build with docker build -t finviz-paper:review . and run the full offline Linux suite by mounting this directory's testdata folder read-only at /app/testdata. The host Dockerfile omits test fixtures from the deployed image.

* [x] Source discovery, checksums, feature branch and local Railway-compatible Docker build.
* [x] Accounting, migrations, user isolation, all five sizing methods, confirmation, history, auditing, rollback and concurrency tests.
* [x] Eight UI views, scan integration, research provenance, analytics, scheduled snapshots, journal and multi-portfolio comparison.
* [x] Configurable entitlements, CSV/PDF reports, gated local-model and VectorBT interfaces, and account-level monitoring.
* [ ] Public commercial clearance for market-data display and redistribution, and a separate VectorBT commercial license if that adapter is needed. A configured Finviz token or software membership does not establish redistribution rights. [VectorBT software license](https://vectorbt.pro/terms/software-license/)
* [ ] Validate the owner's licensed VectorBT build and chosen local model against real provisioned instances. Adapter tests use deterministic substitutes. Verify live Finviz minute delivery during an open session in an approved review environment.
* [ ] Automate corporate-action ingestion and historical ex-date replay if required. Current corporate actions apply to confirmed current holdings. Minute-close fills assume zero slippage and unrestricted liquidity. USD is the only executable currency.
* [ ] Resolve inherited regression failures, complete production-scale load/restore testing and obtain approval before promotion. A self-service tenant directory, billing checkout and automated subscription provisioning are outside the configured-account implementation.

The existing Linux baseline has nine failures and one error across journal table/date expectations, the strategy timeout exit code, and dated TradingView list behavior. The original macOS run also has platform-dependent strategy-sandbox failures. These tests were retained. Compare the actual failed test names against baseline before attributing a failure to this module.

## File inventory

Modified host files are .dockerignore, Dockerfile, requirements.txt, server.py and strategies.py. New files are this guide, test_paper_trading.py, test_paper_http.py, test_paper_browser.py, and the paper_trading package. The package contains __init__.py, ledger.py, market.py, identity.py, web.py, analytics.py, actions.py, research.py, reporting.py, policy.py, paper.html, paper.css, paper.js, and migrations/001_up.sql and 001_down.sql. Use git diff --name-status baseline for the exact feature manifest.

Public design references informed the ticket, clear simulation labeling, account overview and performance views. [TradingView paper account](https://www.tradingview.com/support/solutions/43000516466-paper-trading-main-functionality/), [Moomoo paper trading](https://www.moomoo.com/us/papertrading), [Webull paper trading](https://www.webull.com/help/faq/11069).

## Recorded verification · November 25, 2026

| Check | Observed result |
| --- | --- |
| Focused paper suite with real HTTP and headless Chrome | 17 tests passed in 4.29 seconds |
| Full suite in the Linux image | 275 tests, 264 passed, nine failures, one error and one skipped optional browser test. The browser test passed separately |
| Unmodified Linux baseline | 258 tests, 248 passed, nine failures and one error. The ten unsuccessful test names matched the feature run exactly |
| Static verification | Ruff passed for new Python code and tests. Mypy checked all ten package sources. Python compilation, JavaScript syntax, UI formatting and Git whitespace checks passed |
| Docker startup | Built finviz-paper:review from the Railway Dockerfile. The image served HTTP 200 with body ok at /healthz and redirected unauthenticated /paper requests to login |
| Provider connectivity | Read-only Finviz request from Railway succeeded. In-session execution remains a staging validation item |
| Workspace review | Desktop and phone layouts exercised. The review image uses fictional instruments and virtual holdings for illustration. [Workspace screenshot](docs/paper-trading-workspace.png) |

## Conditional paper sells

A buy preview accepts optional gain_pct, stop_price and limit_price fields. A positive gain percentage sets a target relative to this buy’s execution price, before fees. A stop-limit requires both dollar prices, with the limit at or below the stop and the stop below the entry. The preview shows the calculated thresholds. Confirming the buy explicitly authorizes a subsequent simulated sale of the purchased shares.

The in-process monitor evaluates fresh, observed completed Finviz minute closes during regular sessions. The first eligible close at or above the target can fill the exit. A close at or below the stop activates the limit leg and retires the gain target. The activated leg waits for a qualifying close at or above its floor. A price gap can leave it unfilled. Monitoring skips stale data, intrabar movements and observations missed during downtime.

Signal Manager shows each exit and allows cancellation. Manual sales and splits cancel active exits for the affected symbol. Archiving cancels all exits in that portfolio. Exit fills, fees and audit events commit atomically. GET …/{id}/exits lists rules and POST …/{id}/exits/{entry}/cancel cancels one. Migration 002 adds the exit table to existing databases. Apply down migrations in reverse version order on disposable databases.

Manual paper tickets default to the Finviz daily close of the most recently completed NYSE session. The ticket shows the price and session date, including weekends, holidays and early-close days. During an open session this mode uses the previous completed session. The current regular-session price remains selectable. Closing prices must match the expected exchange session for every security used in sizing. Confirmation rechecks the price and expires after two minutes from preview. The ledger records the quote time and the actual simulation time separately. Automated exits continue to use fresh regular-session minute bars.
