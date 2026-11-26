# Finviz strategy backtests

Open **Strategies**, select a completed daily close run, then use **VectorBT PRO backtest**. Choose a symbol present in the run's saved daily prices. **Backtest history** lists recent jobs.

Saved-signal mode uses actual buy signals recorded before the next available opening price. It groups matching strategy versions. For older runs lacking a version hash it groups by strategy name. Python replay evaluates the current daily `finviz_signals` strategy for one fixed symbol at a time, with price history capped at each evaluation date. This tests a chosen symbol, without reconstructing historical screener membership. Multi-timeframe, V5 and whole-script strategies can use their saved daily signals.

The engine starts each simulation with $100,000, enters at the next available daily open, and exits at the open after the specified holding period. It models long positions, one at a time, with the supplied percentage-based fee charged on both fills. Fee input is in basis points. Each fold starts flat and includes completed trades only. Training selects the holding period with the highest return among periods that produced trades. Walk-forward folds train on expanding windows and validate on subsequent bars. Equity and drawdown use daily closes.

## Services

- Finviz Agent requires `VBT_SERVICE_URL` and `VBT_SERVICE_TOKEN`. Both are server-side variables. The standalone service contains no uploaded strategy code.
- Build the `api` stage of this directory's Dockerfile from the repository root. Give it the service token, private `DATABASE_URL`, private `REDIS_URL` and `PORT`. Run it on Railway's private network.
- Build the `worker` stage in a private build context containing this directory and an authorized VectorBT PRO distribution at `licensed/vectorbtpro.tar.gz`. The verified package is 2026.10.5 at revision `673fd69ae72e74dda7636f8abdab19d72bb74b01`. Keep the licensed archive outside the Git checkout. The worker requires only the private database and Redis connection variables.
- PostgreSQL stores job inputs, historical price snapshots and results. Redis wakes workers after submission. Row locks prevent duplicate processing by concurrent workers. An interrupted job becomes available again when PostgreSQL releases the transaction.

**Backtest all buy signals** evaluates every distinct buy ticker in a selected run as a separate trade for each requested holding period. The entry is the first NYSE session open after both the signal date and run completion. The result uses saved Finviz opening prices and shows pending sessions and missing prices per symbol. Submit the original run again when newer prices are available to create a refreshed snapshot.
