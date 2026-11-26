"""Prepare historical symbol series from completed Finviz strategy runs."""

import datetime as dt
import json
import math
import hashlib
import tempfile
import threading
import time
import os
import re
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import strategies as sm
from backtest_dashboard import result_html as result_html


_REPLAY_SLOT = threading.BoundedSemaphore(1)


def replay(st, meta, cache, symbol, sessions):
    strategy = st.meta().get(meta["strategy"], {})
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
                args += ["--candle-label", sm.candles_label("1d")]
            timeout = min(90, st.timeout)
            code, _ = sm.run_sandboxed(args, work, log, timeout, timeout, st.mem_mb)
            symbol_count = 1 if isinstance(symbol, str) else len(symbol)
            if code or output.stat().st_size > 20000 * symbol_count:
                raise ValueError(
                    "Historical Python replay failed or timed out. Try fewer sessions or archived signals."
                )
            result = json.loads(output.read_text())
            buys = result["buys"]
            signals = (
                {s: set(days) for s, days in buys.items()}
                if isinstance(buys, dict)
                else set(buys)
            )
            return signals, set(result["days"]), version
    finally:
        _REPLAY_SLOT.release()


def calendar_replay(manager, meta, cache, history, symbol, start, end, *, tickers=None):
    from paper_trading.market import exchange
    from finviz_prices import parse_quote_csv

    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise ValueError("Choose both a historical start date and end date.") from None
    if first > last or last.isoformat() > meta["asof"] or (last - first).days > 370:
        raise ValueError(
            "Choose an ordered historical interval within the run’s as-of date, up to 252 sessions."
        )
    expected = [d.date().isoformat() for d in exchange().sessions_in_range(first, last)]
    if not 4 <= len(expected) <= 252:
        raise ValueError("Select an interval containing 4–252 NYSE sessions.")
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
            prior = [d for d in sorted(archive) if d < earliest and d < start][-1000:]
            window = [d for d in expected if d < earliest and d in archive]
            for d in prior + window:
                hist.setdefault(d, {})[sym] = archive[d]
    for ticker, key in keys.items():
        for day in expected:
            bar = hist.get(day, {}).get(key, {})
            if any(
                not isinstance(bar.get(k), (int, float))
                or not math.isfinite(bar[k])
                or bar[k] <= 0
                for k in ("o", "c")
            ):
                raise ValueError(
                    f"{ticker} lacks a daily open or close on {day}. Choose a shorter range or remove this ticker."
                )
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
        buys, days, version = replay(
            manager, dict(meta, asof=expected[-1]), snapshot, symbol, len(expected)
        )
    if days != set(expected):
        raise ValueError("The replay did not evaluate every selected session.")
    by_symbol = {symbols[0]: buys} if isinstance(symbol, str) else buys
    series = {
        ticker: [
            dict(
                day=d,
                open=hist[d][key]["o"],
                close=hist[d][key]["c"],
                signal=d in by_symbol[ticker],
            )
            for d in expected
        ]
        for ticker, key in keys.items()
    }
    return (series[symbol] if isinstance(symbol, str) else series), version


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
    if interval is not None:
        bars, version = calendar_replay(
            manager,
            meta,
            cache,
            history,
            symbol,
            *interval,
            tickers=snapshot.get("tickers", []),
        )
        payload = dict(bars=bars) if isinstance(symbol, str) else dict(series=bars)
        return dict(
            **payload,
            strategy=meta["strategy"],
            version=version,
            run=rid,
            symbol=symbol if isinstance(symbol, str) else f"{len(symbols)} symbols",
            observed_days=len(bars)
            if isinstance(symbol, str)
            else len(bars[symbols[0]]),
            holds=holds,
            fee_bps=fee,
            source="Historical date-range replay",
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
            bars.append(dict(day=day, open=bar["o"], close=bar["c"], signal=False))
    if not 4 <= len(bars) <= 3000:
        raise ValueError("Choose a symbol with 4–3000 daily bars in the selected run.")
    if sessions is not None:
        signal_days, evaluated, version = replay(manager, meta, cache, symbol, sessions)
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
        )
    by_day = {bar["day"]: (bar, nxt) for bar, nxt in zip(bars, bars[1:])}
    observed = set()
    et = ZoneInfo("America/New_York")
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
        bar, nxt = by_day[day]
        deadline = dt.datetime.fromisoformat(nxt["day"] + "T09:30:00").replace(
            tzinfo=et
        )
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
    data["preparation_seconds"] = time.monotonic() - started
    return service("/jobs", data)["id"]


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
    except OSError as exc:
        raise ValueError(
            "The backtest service is unavailable. Check the private service connection."
        ) from exc


def cohort(manager, rid, holds, fee, signal_tickers, now=None):
    """Freeze the exact buy list, then look up observed opens on the required sessions."""
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
    finished = dt.datetime.fromisoformat(meta["finished_utc"])
    cal = exchange()
    entry = cal.next_session(cal.date_to_session(meta["asof"], direction="previous"))
    while cal.session_open(entry) <= finished:
        entry = cal.next_session(entry)
    sessions = [entry] + [cal.session_offset(entry, h) for h in holds]
    dates = [s.date().isoformat() for s in sessions]
    elapsed = [cal.session_open(s) <= now for s in sessions]
    close_days = [
        s.date().isoformat()
        for s in cal.sessions_in_range(entry, max(sessions))
        if cal.session_close(s) <= now
    ]
    ratios = {day: [] for day in close_days}
    prices = {}
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
        values = [
            bars.get(day, {}).get("o") if ready else None
            for day, ready in zip(dates, elapsed)
        ]
        prices[symbol] = [
            v if v is not None and math.isfinite(v) and v > 0 else None for v in values
        ]
        if prices[symbol][0]:
            for day in close_days:
                close = bars.get(day, {}).get("c")
                if close and math.isfinite(close) and close > 0:
                    ratios[day].append(close / prices[symbol][0])
    return dict(
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
    )
