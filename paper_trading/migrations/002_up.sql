BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS paper_exits (
 entry TEXT PRIMARY KEY REFERENCES paper_signals(id), pid TEXT NOT NULL REFERENCES paper_portfolios(id),
 symbol TEXT NOT NULL, qty TEXT NOT NULL, buy_price TEXT NOT NULL, target TEXT, stop TEXT, floor TEXT,
 status TEXT NOT NULL CHECK(status IN ('Armed','Stop triggered','Filled','Canceled')),
 last_stamp INTEGER NOT NULL, sell TEXT UNIQUE REFERENCES paper_signals(id)
);
CREATE INDEX IF NOT EXISTS exit_watch ON paper_exits(pid,status);
PRAGMA user_version=2;
COMMIT;
