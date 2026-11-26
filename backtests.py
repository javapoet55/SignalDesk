"""Prepare one historical symbol series from completed Finviz strategy runs."""

import datetime as dt
import json
import math
import hashlib
import tempfile
import threading
import os
import re
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import strategies as sm


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
                symbol,
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
            if code or output.stat().st_size > 20000:
                raise ValueError(
                    "Historical Python replay failed or timed out. Try fewer sessions or archived signals."
                )
            result = json.loads(output.read_text())
            return set(result["buys"]), set(result["days"]), version
    finally:
        _REPLAY_SLOT.release()


def dataset(root, rid, symbol, holds, fee, signal_tickers, manager=None, sessions=None):
    if not sm.RESULT_RE.fullmatch(rid) or not re.fullmatch(
        r"[A-Z0-9][A-Z0-9.\-]{0,14}", symbol
    ):
        raise ValueError("Select a saved run and a valid symbol.")
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
    history = json.loads(cache.read_text())["history"]
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


def result_html(job, refresh_path):
    import html

    e = lambda value: html.escape(str(value))
    info = job["inputs"]
    top = (
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Strategy backtest</title><style>body{font:16px system-ui;max-width:850px;margin:4vh auto;padding:1rem}"
        "td,th{padding:.5rem;text-align:right}svg{width:100%;height:180px}a{color:#387dc1}</style>"
        f"<main><a href='/strategies/results/{e(info['run'])}/'>Return to strategy</a>"
        f"<h1>{e(info['strategy'])} · {e(info['symbol'])}</h1>"
    )
    if job["error"]:
        return top + "<p>Backtest failed. " + e(job["error"]) + "</p></main></html>"
    r = job["result"]
    if r is None:
        return (
            top
            + f"<p>Queued or running.</p><a href='{e(refresh_path)}'>Refresh result</a></main></html>"
        )
    full = r["full_period"]
    equity = full["equity"]
    low, high = min(equity), max(equity)
    points = " ".join(
        f"{i * 800 / (len(equity) - 1):.2f},{165 - (v - low) * 150 / max(0.01, high - low):.2f}"
        for i, v in enumerate(equity)
    )
    rows = "".join(
        f"<tr><td>{int(p['hold'])}</td><td>{int(p['trades'])}</td><td>{p['return_pct']:.2f}%</td>"
        f"<td>{p['drawdown_pct']:.2f}%</td></tr>"
        for p in r["parameters"]
    )
    validation = r["validation"]
    test = (
        f"{validation['trades']} trades, {validation['return_pct']:.2f}% return over the held-out period."
        if validation
        else "No training trades qualified a holding period for validation."
    )
    folds = "".join(
        "<tr><td>"
        + e(f["start"])
        + " to "
        + e(f["end"])
        + "</td><td>"
        + (str(f["hold"]) if f["hold"] else "No qualifying period")
        + "</td><td>"
        + (str(f["trades"]) if f["trades"] is not None else "—")
        + "</td><td>"
        + (f"{f['return_pct']:.2f}%" if f["return_pct"] is not None else "—")
        + "</td></tr>"
        for f in r.get("folds", [])
    )
    return top + (
        f"<p>{e(r['days'][0])} through {e(r['days'][-1])} · {info['observed_days']} evaluation dates · {e(info.get('source', 'Archived signals'))} · "
        f"Strategy version {e(info['version'])} · VectorBT PRO {e(r['engine'])}</p><h2>Training results</h2><table><tr><th>Hold (sessions)</th><th>Trades</th>"
        f"<th>Return</th><th>Max drawdown</th></tr>{rows}</table><h2>Validation from {e(r['validation_start'])}</h2>"
        f"<p>{e(test)}</p><h2>Walk-forward validation</h2><table><tr><th>Held-out dates</th><th>Training-selected hold</th><th>Trades</th><th>Return</th></tr>{folds}</table><h2>Full-period equity</h2><p>{full['hold']} sessions · {full['trades']} trades · "
        f"{full['return_pct']:.2f}% return · {full['drawdown_pct']:.2f}% maximum drawdown</p>"
        f"<svg viewBox='0 0 800 180' role='img' aria-label='Full-period equity curve'><polyline points='{points}' "
        f"fill='none' stroke='#278673' stroke-width='3'/></svg><p>Starting cash $100,000. Final equity ${equity[-1]:,.2f}. "
        f"Fee {info['fee_bps']:g} basis points per side.</p></main></html>"
    )
