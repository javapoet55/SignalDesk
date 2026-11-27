"""Prepare historical symbol series from completed Finviz strategy runs."""

import datetime as dt
import json
import math
import hashlib
from itertools import groupby
import tempfile
import threading
import time
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import strategies as sm
from backtest_dashboard import result_html as result_html


_REPLAY_SLOT = threading.BoundedSemaphore(1)
REPLAY_TIMEOUT = 300  # Seconds. Long ranges with indicator warm-up history took about 45 seconds in production.


def replay_timeout(manager, env=os.environ):
    """Seconds one historical replay may run: VBT_REPLAY_TIMEOUT_SEC (30–900) or five minutes,
    and never longer than a normal strategy run."""
    try:
        limit = max(30, min(900, int(env.get("VBT_REPLAY_TIMEOUT_SEC") or REPLAY_TIMEOUT)))
    except ValueError:
        limit = REPLAY_TIMEOUT
    return min(limit, manager.timeout)


TARGET_SESSIONS = 7       # Default maximum hold when a profit target is set.
MAX_TARGET_SESSIONS = 60  # Must match vbt_service.engine.MAX_TARGET_SESSIONS.


def target_hold(value):
    """The single maximum hold, in trading days, used with a profit target. Blank means the default."""
    text = (value or "").strip()
    if not text:
        return TARGET_SESSIONS
    try:
        hold = int(text)
    except ValueError:
        hold = 0
    if not 1 <= hold <= MAX_TARGET_SESSIONS:
        raise ValueError(f"With a profit target, enter one maximum hold of 1–{MAX_TARGET_SESSIONS} trading days.")
    return hold


def profit_target(value):
    if value is None or value.strip() == "":
        return None
    try:
        target = float(value)
    except ValueError:
        target = 0
    if not math.isfinite(target) or target <= 0:
        raise ValueError("Enter a positive profit target percentage or leave it blank.")
    return target


def stop_loss(value):
    if value is None or value.strip() == "":
        return None
    try:
        stop = float(value)
    except ValueError:
        stop = 0
    if not math.isfinite(stop) or not 0 < stop < 100:
        raise ValueError("Enter a stop loss between 0 and 100 percent or leave it blank.")
    return stop


def daily_low(bar):
    value, prices = bar.get("l"), [bar.get("o"), bar.get("c")]
    if (isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            and all(isinstance(p, (int, float)) and value <= p for p in prices)):
        return value
    return None


def daily_high(bar):
    value = bar.get("h")
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 and value >= max(bar["o"], bar["c"]):
        return value
    return None


def replay_failure(log, mem_mb):
    """Say why a replay stopped, from the exception the strategy process reported.

    The log lives in a temporary folder that is deleted with the replay, so this message is the
    only place the reason survives. A strategy may print a line for every ticker, and that output
    can land after the error in the log, so the last line of the log is not a reliable reason:
    look for the last Python traceback instead."""
    generic = "Historical Python replay failed or timed out. Try fewer sessions or archived signals."
    try:
        with open(log, "rb") as handle:
            handle.seek(max(0, os.path.getsize(log) - 200_000))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return generic
    error = re.compile(r"[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)\b(?::.*)?$")
    last = ""
    # Buffered strategy output can be cut off mid-line by the traceback, so the header may not start a line.
    headers = [i for i, line in enumerate(lines) if "Traceback (most recent call last)" in line]
    if headers:
        # The exception line is the first unindented line after the traceback's frames.
        last = next((line.strip() for line in lines[headers[-1] + 1:] if line and not line[0].isspace()), "")
    if not last:
        last = next((line.strip() for line in reversed(lines) if error.match(line)), "")
    if not last:
        return generic
    if last.startswith("MemoryError"):
        return (f"Historical Python replay ran out of memory (limit {mem_mb} MB). "
                "Choose a shorter date range or fewer symbols.")
    kind, _, detail = last.partition(": ")
    # replay_daily raises ValueError with a complete sentence, e.g. which symbol and date failed.
    reason = detail if kind == "ValueError" and detail else last
    return "Historical Python replay stopped. " + reason[:300].rstrip(".") + "."


MAX_CANDLE_DAYS = 5


def candle_days(value):
    """Trading days per price bar used to compute signals: blank or 1 is daily."""
    text = (value or "").strip()
    if not text:
        return 1
    try:
        days = int(text)
    except ValueError:
        days = 0
    if not 1 <= days <= MAX_CANDLE_DAYS:
        raise ValueError(f"Choose price bars of 1–{MAX_CANDLE_DAYS} trading days.")
    return days


def replay(st, meta, cache, symbol, sessions, candle_days=1):
    strategy = st.meta().get(meta["strategy"], {})
    if candle_days > 1 and not strategy.get("candles"):
        raise ValueError(
            "This strategy only works on daily price bars. A strategy opts in to longer bars with a CANDLE_DAYS setting."
        )
    if strategy.get("kind") != "generic" or not 4 <= sessions <= 252:
        raise ValueError(
            "Historical Python replay requires a generic daily strategy and 4–252 sessions."
        )
    if strategy.get("timeframes") and set(strategy["timeframes"]) - {"1d"}:
        raise ValueError(
            "Use archived signals for strategies that require multiple timeframes."
        )
    if not _REPLAY_SLOT.acquire(blocking=False):
        raise ValueError(
            "A historical strategy replay is already running. Try again when it finishes."
        )
    try:
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            # Freeze the exact current strategy for the entire replay.
            source = (st.scripts_dir / strategy["file"]).read_bytes()
            version = hashlib.sha256(source).hexdigest()[:12]
            script = work / "strategy.py"
            script.write_bytes(source)
            script.chmod(0o644)
            work.chmod(0o755)
            output = work / "result.json"
            output.touch()
            sm._chown_nobody(output)
            log = work / "replay.log"
            args = [
                "replay",
                str(script),
                "--symbol",
                symbol if isinstance(symbol, str) else ",".join(symbol),
                "--cache",
                str(cache),
                "--asof",
                meta["asof"],
                "--sessions",
                str(sessions),
                "--output",
                str(output),
            ]
            if strategy.get("candles"):
                args += ["--candle-label", sm.candles_label(f"{candle_days}d")]
            if candle_days > 1:
                args += ["--candle-days", str(candle_days)]
            timeout = replay_timeout(st)
            code, _ = sm.run_sandboxed(args, work, log, timeout, timeout, st.mem_mb)
            symbol_count = 1 if isinstance(symbol, str) else len(symbol)
            if code == -1:
                raise ValueError(
                    f"Historical Python replay ran longer than {timeout} seconds and was stopped. "
                    "Choose a shorter date range or fewer symbols."
                )
            if code:
                raise ValueError(replay_failure(log, st.mem_mb))
            if output.stat().st_size > 20000 * symbol_count:
                raise ValueError(
                    "Historical Python replay returned more signal data than expected. Choose fewer sessions."
                )
            result = json.loads(output.read_text())
            buys = result["buys"]
            signals = (
                {s: set(days) for s, days in buys.items()}
                if isinstance(buys, dict)
                else set(buys)
            )
            return signals, set(result["days"]), version, result["warmup_sessions"]
    finally:
        _REPLAY_SLOT.release()


class ReplayCoverageError(ValueError):
    def __init__(self, missing, symbols, sessions):
        self.covered_symbols = [s for s in symbols if s not in missing]
        missing_days = set().union(*missing.values())
        complete_spans = (
            list(days) for available, days in groupby(sessions, lambda day: day not in missing_days)
            if available
        )
        longest = max(complete_spans, key=len, default=[])
        self.common_range = (longest[0], longest[-1]) if len(longest) >= 4 else None
        self.requested_range = (sessions[0], sessions[-1])
        self.symbol_count = len(symbols)
        ticker = next(iter(missing))
        message = f"{ticker} lacks a daily open or close on {min(missing[ticker])}."
        if len(missing) > 1:
            message += f" {len(missing)} symbols have incomplete prices in this range."
        if self.common_range:
            message += f" All selected symbols have complete prices from {longest[0]} through {longest[-1]}."
        elif not self.covered_symbols:
            message += " Choose another range or symbol selection with at least four fully priced sessions."
        super().__init__(message)


def calendar_replay(manager, meta, cache, history, symbol, start, end, *, tickers=None, candle_days=1, skipped=None):
    """Replay the strategy over the NYSE sessions in a date range.

    Symbols in a basket that cannot be priced on every session are left out and recorded in
    `skipped` (ticker -> first unpriced day) when the caller passes a dict for it."""
    from paper_trading.market import exchange
    from finviz_prices import parse_quote_csv

    try:
        first, last = sorted((dt.date.fromisoformat(start), dt.date.fromisoformat(end)))
    except ValueError:
        raise ValueError("Choose both a historical start date and end date.") from None
    if last.isoformat() > meta["asof"]:
        raise ValueError(f"This run has data through {meta['asof']}. Choose an end date on or before that date.")
    calendar = exchange()
    if first < calendar.first_session.date() or last > calendar.last_session.date():
        raise ValueError(
            f"Choose dates within the available NYSE calendar ({calendar.first_session.date()} – {calendar.last_session.date()})."
        )
    expected = [d.date().isoformat() for d in calendar.sessions_in_range(first, last)]
    if not 4 <= len(expected) <= 252:
        raise ValueError(f"{first} – {last} contains {len(expected)} NYSE sessions. Choose a range with 4–252 sessions.")
    symbols = [symbol] if isinstance(symbol, str) else symbol
    available = {ticker for prices in history.values() for ticker in prices}
    keys = {}
    for ticker in symbols:
        key = next(
            (
                v
                for v in (ticker, ticker.replace("-", "."), ticker.replace(".", "-"))
                if v in available
            ),
            None,
        )
        if key is None:
            raise ValueError(f"{ticker} is missing from the saved run’s price history.")
        keys[ticker] = key
    if len(set(keys.values())) != len(keys):
        raise ValueError("Each ticker in a basket must identify a different security.")
    # Preserve the original snapshot wherever possible. Prepend matching Finviz history for indicator warm-up.
    hist = {d: dict(prices) for d, prices in history.items() if d <= expected[-1]}
    first_day = first.isoformat()
    if manager.finviz and meta.get("price_source", "finviz") == "finviz":
        for sym in dict.fromkeys([*keys.values(), "SPY"]):
            path = manager.finviz.cache_dir / (sym + ".csv")
            if not sm.regular_file(path, manager.finviz.cache_dir):
                continue
            archive, _ = parse_quote_csv(path.read_text())
            earliest = min(
                (d for d, v in history.items() if sym in v), default="9999-12-31"
            )
            if (
                earliest in archive
                and sym in history.get(earliest, {})
                and not math.isclose(
                    archive[earliest]["c"], history[earliest][sym]["c"], rel_tol=0.001
                )
            ):
                raise ValueError(
                    "Finviz price histories have different adjustment bases; choose a consistent snapshot."
                )
            prior = [d for d in sorted(archive) if d < earliest and d < first_day][-1000:]
            window = [d for d in expected if d < earliest and d in archive]
            for d in prior + window:
                hist.setdefault(d, {})[sym] = archive[d]
    missing = {}
    for ticker, key in keys.items():
        for day in expected:
            bar = hist.get(day, {}).get(key, {})
            if any(
                not isinstance(bar.get(k), (int, float))
                or not math.isfinite(bar[k])
                or bar[k] <= 0
                for k in ("o", "c")
            ):
                missing.setdefault(ticker, set()).add(day)
    covered = [ticker for ticker in symbols if ticker not in missing]
    if missing and covered and not isinstance(symbol, str) and skipped is not None:
        # Leave out the symbols that cannot be priced on every session and run the rest.
        skipped.update((ticker, min(days)) for ticker, days in missing.items())
        symbols = covered
        keys = {ticker: keys[ticker] for ticker in covered}
        symbol = covered[0] if len(covered) == 1 else covered
    elif missing:
        raise ReplayCoverageError(missing, symbols, expected)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        folder.chmod(0o755)
        snapshot = folder / "history.json"
        snapshot.write_text(
            json.dumps(
                {
                    "history": hist,
                    "tickers": tickers
                    if tickers is not None
                    else json.loads(cache.read_text()).get("tickers", []),
                }
            )
        )
        snapshot.chmod(0o644)
        buys, days, version, warmup_sessions = replay(
            manager, dict(meta, asof=expected[-1]), snapshot, symbol, len(expected),
            **({"candle_days": candle_days} if candle_days > 1 else {}),
        )
    if days != set(expected):
        raise ValueError("The replay did not evaluate every selected session.")
    by_symbol = {symbols[0]: buys} if isinstance(symbol, str) else buys
    series = {
        ticker: [
            dict(
                day=d,
                open=hist[d][key]["o"],
                high=daily_high(hist[d][key]),
                low=daily_low(hist[d][key]),
                close=hist[d][key]["c"],
                signal=d in by_symbol[ticker],
            )
            for d in expected
        ]
        for ticker, key in keys.items()
    }
    return (series[symbol] if isinstance(symbol, str) else series), version, warmup_sessions


def dataset(
    root,
    rid,
    symbol,
    holds,
    fee,
    signal_tickers,
    manager=None,
    sessions=None,
    interval=None,
    candle_days=1,
):
    symbols = [symbol] if isinstance(symbol, str) else symbol
    if (
        not sm.RESULT_RE.fullmatch(rid)
        or not symbols
        or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", s) for s in symbols)
    ):
        raise ValueError("Select a saved run and valid symbols.")
    if not isinstance(symbol, str) and (not interval or len(symbols) < 2):
        raise ValueError(
            "Select at least two distinct symbols and a historical date range."
        )
    if (
        not 1 <= len(holds) <= 10
        or any(not 1 <= h <= 252 for h in holds)
        or not 0 <= fee <= 100
    ):
        raise ValueError(
            "Use up to ten holding periods of 1–252 sessions and fees of 0–100 basis points."
        )
    folder = root / rid
    meta = sm.read_meta(folder)
    cache = folder / "history_cache.json"
    if (
        folder.is_symlink()
        or meta.get("status") != "ok"
        or meta.get("mode") != "close"
        or meta.get("candles", "1d") != "1d"
        or not sm.regular_file(cache, folder)
    ):
        raise ValueError("Choose a completed strategy run with daily closing prices.")
    version = meta.get("strategy_sha256")
    snapshot = json.loads(cache.read_text())
    history = snapshot["history"]
    if candle_days > 1 and interval is None:
        raise ValueError(
            "Longer price bars need a date range. Saved signals were calculated on the run's own daily bars."
        )
    if interval is not None:
        skipped = {}
        bars, version, warmup_sessions = calendar_replay(
            manager,
            meta,
            cache,
            history,
            symbol,
            *interval,
            tickers=snapshot.get("tickers", []),
            skipped=skipped,
            **({"candle_days": candle_days} if candle_days > 1 else {}),
        )
        remaining = [s for s in symbols if s not in skipped]
        single = isinstance(bars, list)  # One symbol was asked for, or only one could be priced.
        payload = dict(bars=bars) if single else dict(series=bars)
        return dict(
            **payload,
            strategy=meta["strategy"],
            version=version,
            run=rid,
            symbol=remaining[0] if single else f"{len(remaining)} symbols",
            observed_days=len(bars) if single else len(next(iter(bars.values()))),
            holds=holds,
            fee_bps=fee,
            source="Historical date-range replay",
            warmup_sessions=warmup_sessions,
            skipped_symbols=skipped,
            candle_days=candle_days,
        )
    bars = []
    for day, prices in sorted(history.items()):
        if day > meta["asof"]:
            break
        bar = (
            prices.get(symbol)
            or prices.get(symbol.replace("-", "."))
            or prices.get(symbol.replace(".", "-"))
        )
        if bar and all(
            isinstance(bar.get(k), (int, float))
            and math.isfinite(bar[k])
            and bar[k] > 0
            for k in ("o", "c")
        ):
            bars.append(dict(day=day, open=bar["o"], high=daily_high(bar), low=daily_low(bar), close=bar["c"], signal=False))
    if not 4 <= len(bars) <= 3000:
        raise ValueError("Choose a symbol with 4–3000 daily bars in the selected run.")
    if sessions is not None:
        signal_days, evaluated, version, warmup_sessions = replay(manager, meta, cache, symbol, sessions)
        bars = [
            dict(bar, signal=bar["day"] in signal_days)
            for bar in bars
            if bar["day"] in evaluated
        ]
        if len(bars) < 4:
            raise ValueError("Historical replay needs at least four daily bars.")
        return dict(
            strategy=meta["strategy"],
            version=version,
            run=rid,
            symbol=symbol,
            bars=bars,
            observed_days=len(bars),
            holds=holds,
            fee_bps=fee,
            source="Historical Python replay",
            warmup_sessions=warmup_sessions,
        )
    from paper_trading.market import exchange

    cal = exchange()
    by_day = {bar["day"]: bar for bar in bars}
    observed = set()
    for other in root.iterdir():
        if other.is_symlink() or not sm.RESULT_RE.fullmatch(other.name):
            continue
        run = sm.read_meta(other)
        day = run.get("asof")
        if (
            day not in by_day
            or run.get("strategy") != meta["strategy"]
            or run.get("strategy_sha256") != version
            or run.get("status") != "ok"
            or run.get("mode") != "close"
            or run.get("candles", "1d") != "1d"
        ):
            continue
        if not cal.is_session(day):
            continue
        bar = by_day[day]
        deadline = cal.session_open(cal.next_session(day))
        stamp = run.get("finished_utc")
        if not stamp or dt.datetime.fromisoformat(stamp) >= deadline:
            continue
        observed.add(day)
        if symbol in signal_tickers(other):
            bar["signal"] = True
    if not observed:
        raise ValueError(
            "This version has no saved daily signals evaluated before a later opening price in this cache."
        )
    bars = [bar for bar in bars if bar["day"] >= min(observed)]
    if len(bars) < 4:
        raise ValueError(
            "At least four bars from the first archived evaluation are needed."
        )
    return dict(
        strategy=meta["strategy"],
        version=version or "unversioned archive",
        run=rid,
        symbol=symbol,
        bars=bars,
        observed_days=len(observed),
        holds=holds,
        fee_bps=fee,
        source="Archived signals",
    )


def submit_job(data, started):
    data["entry_basis"] = "signal_close"
    if data.get("stop_loss_pct") and not data.get("profit_target_pct"):
        raise ValueError("Enter a profit target to use a stop loss.")
    if data.get("profit_target_pct"):
        data["exit_on_target"] = True
        if "bars" in data and data.get("source") != "Historical date-range replay":
            from paper_trading.market import exchange

            days = [bar["day"] for bar in data["bars"]]
            expected = exchange().sessions_in_range(days[0], days[-1])
            missing = set(s.date().isoformat() for s in expected) - set(days)
            if missing:
                raise ValueError(f"Missing a daily bar on {min(missing)}. Choose a fully covered date range for target exits.")
    data["preparation_seconds"] = time.monotonic() - started
    return service("/jobs", data)["id"]


class JobNotFound(ValueError):
    """The service answered, but it has no job or run with this ID."""


def service(path, data=None):
    url = os.environ["VBT_SERVICE_URL"].rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ["VBT_SERVICE_TOKEN"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise JobNotFound("This backtest was not found. It may have been removed.") from exc
        raise ValueError(
            "The backtest service is unavailable. Check the private service connection."
        ) from exc
    except OSError as exc:
        raise ValueError(
            "The backtest service is unavailable. Check the private service connection."
        ) from exc


def cohort(manager, rid, holds, fee, signal_tickers, now=None, *, target=None):
    """Buy the saved signal basket at its as-of close and price exits on subsequent sessions."""
    import finviz_prices
    from paper_trading.market import exchange

    if not sm.RESULT_RE.fullmatch(rid):
        raise ValueError("Select a valid strategy run.")
    folder = manager.results_dir / rid
    meta = sm.read_meta(folder)
    if (
        folder.is_symlink()
        or meta.get("status") != "ok"
        or meta.get("mode") != "close"
        or meta.get("candles", "1d") != "1d"
        or not meta.get("finished_utc")
        or not meta.get("asof")
    ):
        raise ValueError("Select a completed daily close run.")
    holds = list(dict.fromkeys(holds))
    if (
        not 1 <= len(holds) <= 10
        or any(not 1 <= h <= 252 for h in holds)
        or not 0 <= fee <= 100
    ):
        raise ValueError(
            "Use 1–10 holding periods of 1–252 sessions and fees of 0–100 basis points."
        )
    symbols = list(dict.fromkeys(s.strip().upper() for s in signal_tickers(folder)))
    if not symbols or len(symbols) > 5000:
        raise ValueError("Select a run with 1–5,000 buy symbols.")
    if not manager.finviz or meta.get("price_source", "finviz") != "finviz":
        raise ValueError("Batch signal outcomes require a Finviz daily-price run.")
    now = now or dt.datetime.now(dt.timezone.utc)
    cal = exchange()
    if not cal.is_session(meta["asof"]):
        raise ValueError("The saved signal date has no NYSE closing session.")
    entry = cal.date_to_session(meta["asof"])
    if target is not None:
        # One value is the chosen maximum hold. A comparison list has no meaning with a target.
        holds = holds if len(holds) == 1 else [TARGET_SESSIONS]
    sessions = [entry] + [cal.session_offset(entry, h) for h in holds]
    dates = [s.date().isoformat() for s in sessions]
    elapsed = [cal.session_close(entry) <= now] + [cal.session_open(s) <= now for s in sessions[1:]]
    close_days = [
        s.date().isoformat()
        for s in cal.sessions_in_range(entry, max(sessions))
        if cal.session_close(s) <= now
    ]
    ratios = {day: [] for day in close_days} if target is None else {}
    prices = {}
    target_bars = {}
    window = cal.sessions_in_range(cal.next_session(entry), sessions[-1]) if target is not None else []
    completed_sessions = sum(cal.session_close(s) <= now for s in window)
    snapshot = folder / "history_cache.json"
    saved_quotes = {}
    if elapsed[0] and sm.regular_file(snapshot, folder):
        saved_quotes = json.loads(snapshot.read_text()).get("history", {}).get(dates[0], {})
    observed_days = [s.date().isoformat() for s in window if cal.session_open(s) <= now]

    def positive(value):
        return (
            value if isinstance(value, (float, int)) and math.isfinite(value) and value > 0 else None
        )

    for symbol in symbols:
        bars = {}
        if elapsed[0] and re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", symbol):
            for candidate in dict.fromkeys(
                [symbol, symbol.replace("-", "."), symbol.replace(".", "-")]
            ):
                path = manager.finviz.cache_dir / (candidate + ".csv")
                if sm.regular_file(path, manager.finviz.cache_dir):
                    try:
                        bars, _ = finviz_prices.parse_quote_csv(path.read_text())
                    except (OSError, ValueError, finviz_prices.FinvizPriceError):
                        continue
                    if bars:
                        break
        aliases = (symbol, symbol.replace("-", "."), symbol.replace(".", "-"))
        recorded = next((saved_quotes[key]["c"] for key in aliases if positive(saved_quotes.get(key, {}).get("c"))), None)
        cached = positive(bars.get(dates[0], {}).get("c"))
        if recorded and cached and not math.isclose(recorded, cached, rel_tol=.001):
            raise ValueError(f"{symbol} has inconsistent recorded and cached closes on {dates[0]}. Check price adjustments before replaying.")
        buy = (recorded or cached) if elapsed[0] else None
        values = [buy] + [bars.get(day, {}).get("o") if ready else None for day, ready in zip(dates[1:], elapsed[1:])]
        prices[symbol] = [positive(v) for v in values]
        if target is not None:
            target_bars[symbol] = []
            for i, day in enumerate(observed_days):
                bar = bars.get(day, {})
                complete = i < completed_sessions
                target_bars[symbol].append(
                    dict(
                        open=positive(bar.get("o")),
                        high=daily_high(bar) if complete else None,
                        low=daily_low(bar) if complete else None,
                        close=positive(bar.get("c")) if complete else None,
                    )
                )
        elif prices[symbol][0]:
            for day in close_days:
                close = buy if day == dates[0] else bars.get(day, {}).get("c")
                if close and math.isfinite(close) and close > 0:
                    ratios[day].append(close / prices[symbol][0])
    return dict(
        **(
            dict(
                target_bars=target_bars,
                target_days=[s.date().isoformat() for s in window],
                completed_sessions=completed_sessions,
            )
            if target is not None
            else {}
        ),
        close_marks={
            day: sum(values) / len(symbols) if len(values) == len(symbols) else None
            for day, values in ratios.items()
        },
        run=rid,
        strategy=meta["strategy"],
        version=meta.get("strategy_sha256") or "unversioned archive",
        symbol=f"{len(symbols)} buys",
        source="Run buy signals",
        holds=holds,
        fee_bps=fee,
        dates=dates,
        elapsed=elapsed,
        prices=prices,
        signal_day=meta["asof"],
        entry_basis="signal_close",
    )
