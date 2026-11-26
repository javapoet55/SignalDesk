BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS paper_portfolios (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
 capital TEXT NOT NULL, cash TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency='USD'),
 status TEXT NOT NULL CHECK(status IN ('active','archived')), version INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS portfolio_owner ON paper_portfolios(owner,created);
CREATE TABLE IF NOT EXISTS paper_portfolio_settings (
 pid TEXT PRIMARY KEY REFERENCES paper_portfolios, benchmark TEXT NOT NULL, fractional INTEGER NOT NULL, fee TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_signals (
 id TEXT PRIMARY KEY, pid TEXT NOT NULL REFERENCES paper_portfolios, request_key TEXT NOT NULL, ticket TEXT NOT NULL,
 preview TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('Draft','Pending','Processing','Simulated','Canceled','Rejected','Expired')), reason TEXT NOT NULL DEFAULT '',
 created INTEGER NOT NULL, processed INTEGER, UNIQUE(pid,request_key)
);
CREATE TABLE IF NOT EXISTS paper_orders (
 id TEXT PRIMARY KEY REFERENCES paper_signals, pid TEXT NOT NULL REFERENCES paper_portfolios,
 status TEXT NOT NULL, reason TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_transactions (
 id TEXT PRIMARY KEY, pid TEXT NOT NULL REFERENCES paper_portfolios, signal TEXT NOT NULL UNIQUE REFERENCES paper_orders,
 symbol TEXT NOT NULL, side TEXT NOT NULL, qty TEXT NOT NULL, price TEXT NOT NULL, notional TEXT NOT NULL,
 fee TEXT NOT NULL, realized TEXT NOT NULL, source TEXT NOT NULL, stamp INTEGER NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
 pid TEXT NOT NULL REFERENCES paper_portfolios, symbol TEXT NOT NULL, qty TEXT NOT NULL, cost TEXT NOT NULL,
 opened INTEGER NOT NULL, PRIMARY KEY(pid,symbol)
);
CREATE TABLE IF NOT EXISTS paper_cash_ledger (
 id INTEGER PRIMARY KEY, pid TEXT NOT NULL REFERENCES paper_portfolios, signal TEXT UNIQUE REFERENCES paper_orders,
 delta TEXT NOT NULL, balance TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_portfolio_snapshots (
 id INTEGER PRIMARY KEY, pid TEXT NOT NULL REFERENCES paper_portfolios, created INTEGER NOT NULL,
 equity TEXT NOT NULL, benchmark TEXT, version INTEGER NOT NULL, UNIQUE(pid,created,version)
);
CREATE TABLE IF NOT EXISTS paper_journal_entries (
 id TEXT PRIMARY KEY, pid TEXT NOT NULL REFERENCES paper_portfolios, signal TEXT REFERENCES paper_signals,
 fields TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_audit_events (
 id INTEGER PRIMARY KEY, pid TEXT NOT NULL REFERENCES paper_portfolios, subject TEXT NOT NULL, action TEXT NOT NULL,
 details TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS signal_time ON paper_signals(pid,created);
CREATE INDEX IF NOT EXISTS transaction_time ON paper_transactions(pid,created);
CREATE INDEX IF NOT EXISTS event_time ON paper_audit_events(pid,created);
CREATE INDEX IF NOT EXISTS journal_time ON paper_journal_entries(pid,created);
CREATE TRIGGER IF NOT EXISTS immutable_transactions_UPDATE BEFORE UPDATE ON paper_transactions BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_transactions_DELETE BEFORE DELETE ON paper_transactions BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_orders_UPDATE BEFORE UPDATE ON paper_orders BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_orders_DELETE BEFORE DELETE ON paper_orders BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_cash_ledger_UPDATE BEFORE UPDATE ON paper_cash_ledger BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_cash_ledger_DELETE BEFORE DELETE ON paper_cash_ledger BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_events_UPDATE BEFORE UPDATE ON paper_audit_events BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_events_DELETE BEFORE DELETE ON paper_audit_events BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_portfolio_snapshots_UPDATE BEFORE UPDATE ON paper_portfolio_snapshots BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_portfolio_snapshots_DELETE BEFORE DELETE ON paper_portfolio_snapshots BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_journal_entries_UPDATE BEFORE UPDATE ON paper_journal_entries BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_journal_entries_DELETE BEFORE DELETE ON paper_journal_entries BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TABLE IF NOT EXISTS paper_corporate_actions (
 pid TEXT NOT NULL REFERENCES paper_portfolios, request_key TEXT NOT NULL, symbol TEXT NOT NULL,
 kind TEXT NOT NULL, value TEXT NOT NULL, qty_before TEXT NOT NULL, qty_after TEXT NOT NULL, cash_delta TEXT NOT NULL,
 reference TEXT NOT NULL, created INTEGER NOT NULL, PRIMARY KEY(pid,request_key)
);
CREATE TRIGGER IF NOT EXISTS immutable_actions_update BEFORE UPDATE ON paper_corporate_actions BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_actions_delete BEFORE DELETE ON paper_corporate_actions BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE INDEX IF NOT EXISTS fills_symbol ON paper_transactions(pid,symbol,created);
CREATE TRIGGER IF NOT EXISTS signal_transition BEFORE UPDATE OF status ON paper_signals WHEN NOT (
 (OLD.status='Draft' AND NEW.status='Canceled') OR
 (OLD.status='Pending' AND NEW.status IN ('Processing','Canceled','Expired','Rejected')) OR
 (OLD.status='Processing' AND NEW.status='Simulated')
) BEGIN SELECT RAISE(ABORT,'invalid signal transition'); END;
CREATE TRIGGER IF NOT EXISTS protect_signal BEFORE DELETE ON paper_signals BEGIN SELECT RAISE(ABORT,'immutable signal'); END;
CREATE INDEX IF NOT EXISTS pending_expiry ON paper_signals(CAST(json_extract(preview,'$.oldest_stamp') AS INTEGER)) WHERE status='Pending';
PRAGMA user_version=1;
COMMIT;
