"""Backtest daily Finviz signals with equal starting capital per selected symbol."""

import math
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import vectorbtpro as vbt
from vectorbtpro.portfolio.enums import TradeStatus


def reached_target(entry, target_pct, highs, hold, observed_price):
    """A witnessed touch is definitive. A miss requires highs for the entire hold."""
    threshold = Decimal(str(entry)) * (1 + Decimal(str(target_pct)) / 100)
    observed_highs = [high for high in highs if high is not None]
    peak = max([observed_price, *observed_highs])
    if Decimal(str(peak)) >= threshold:
        return True
    if len(observed_highs) == hold:
        return False
    return None


def simulate(series, start, end, hold, fee_bps, detail=False, target=None):
    window = {symbol: bars[start:end] for symbol, bars in series.items()}
    days = [bar["day"] for bar in next(iter(window.values()))]
    index = pd.DatetimeIndex(days)
    opens = pd.DataFrame(
        {s: [b["open"] for b in bars] for s, bars in window.items()}, index=index
    )
    closes = pd.DataFrame(
        {s: [b["close"] for b in bars] for s, bars in window.items()}, index=index
    )
    entries = pd.DataFrame(False, index=index, columns=opens.columns)
    exits = entries.copy()
    for column, bars in enumerate(window.values()):
        available = 1
        for i in range(1, len(bars) - hold):
            if i >= available and bars[i - 1]["signal"]:
                entries.iat[i, column] = exits.iat[i + hold, column] = True
                available = i + hold + 1
    pf = vbt.Portfolio.from_signals(
        closes,
        entries=entries,
        exits=exits,
        price=opens,
        fees=fee_bps / 10000,
        init_cash=100000 / len(window),
        cash_sharing=False,
        freq="1D",
    )
    equity = pf.value.sum(axis=1)
    peaks = equity.cummax().clip(lower=100000)
    records = pf.trades.records
    result = {
        "hold": hold,
        "trades": len(records),
        "return_pct": float(equity.iloc[-1] / 100000 - 1) * 100,
        "drawdown_pct": float((1 - equity / peaks).max()) * 100,
        "equity": [round(float(x), 2) for x in equity],
    }
    if detail:
        fills = []
        for t in records.sort_values(["entry_idx", "col"]).to_dict("records"):
            fill = {
                "symbol": opens.columns[t["col"]],
                "entry": days[t["entry_idx"]],
                "exit": days[t["exit_idx"]],
                "buy": t["entry_price"],
                "sell": t["exit_price"],
                "pnl": t["pnl"],
                "pct": t["return"] * 100,
                "hold": t["exit_idx"] - t["entry_idx"],
            }
            if target is not None:
                bars = window[fill["symbol"]]
                held = bars[t["entry_idx"]:t["exit_idx"]]
                exit_open = bars[t["exit_idx"]]["open"]
                observed = max(exit_open, *(max(b["open"], b["close"]) for b in held))
                fill["target_hit"] = reached_target(
                    held[0]["open"], target, [b.get("high") for b in held], fill["hold"], observed
                )
            fills.append(fill)
        result["fills"] = fills
        result["benchmark"] = (
            closes.div(closes.iloc[0]).mean(axis=1) * 100000
        ).tolist()
    return result


TARGET_SESSIONS = 7       # Default maximum hold when a profit target is set.
MAX_TARGET_SESSIONS = 60  # Longest maximum hold a target exit may use.


def limit_price(entry, pct):
    return float(Decimal(str(entry)) * (1 + Decimal(str(pct)) / 100))


def stop_price(entry, pct):
    return float(Decimal(str(entry)) * (1 - Decimal(str(pct)) / 100))


def target_exit(bar, limit, age, complete=True, stop=None, max_age=TARGET_SESSIONS):
    """Return the first executable sell price, whether it filled the target, and why it sold.

    The reason is "target", "stop" or "time". Daily bars do not say whether the high or the low
    came first, so a session that touches both the stop and the target counts as stopped."""
    opening = bar["open"]
    if opening is None:
        raise ValueError("opening price unavailable")
    if stop is not None and opening <= stop:
        return opening, False, "stop"  # Gapped below the stop: the opening price is the first fill.
    if opening >= limit:
        return opening, True, "target"
    if complete:
        if stop is not None:
            low, close = bar.get("low"), bar.get("close")
            if (low is not None and low <= stop) or (close is not None and close <= stop):
                return stop, False, "stop"
            if low is None:
                raise ValueError("daily low unavailable")
        if max(bar.get("high") or 0, bar.get("close") or 0) >= limit:
            return limit, True, "target"
        if bar.get("high") is None:
            raise ValueError("daily high unavailable")
        if age >= max_age:
            if bar.get("close") is None:
                raise ValueError("closing price unavailable")
            return bar["close"], False, "time"
    return None


def simulate_orders(series, start, end, hold, fee_bps, detail=False, *, target, signal_close, stop_pct=None):
    # Separate earlier execution from the final close so the daily high precedes a close entry.
    symbols = list(series)
    days = [b["day"] for b in series[symbols[0]][start:end]]
    shape = (len(days) * 2, len(symbols))
    marks = np.empty(shape)
    prices = np.full(shape, np.nan)
    sizes = np.full(shape, np.nan)
    closed = {}
    for col, symbol in enumerate(symbols):
        bars = series[symbol][start:end]
        entry = None
        floor = None
        for i, bar in enumerate(bars):
            morning = 2 * i
            marks[morning : morning + 2, col] = bar["open"], bar["close"]
            if not signal_close and entry is None and i and bars[i - 1]["signal"]:
                entry = i
                limit = limit_price(bar["open"], target)
                floor = stop_price(bar["open"], stop_pct) if stop_pct else None
                sizes[morning, col], prices[morning, col] = np.inf, bar["open"]
            if entry is not None:
                age = i - entry + int(not signal_close)
                if target is None:
                    fill = (bar["open"], None, "hold") if age == hold else None
                else:
                    try:
                        fill = target_exit(bar, limit, age, stop=floor, max_age=hold)
                    except ValueError as exc:
                        raise ValueError(f"{symbol} on {days[i]}: {exc}. A target exit cannot be priced.") from exc
                if fill is not None:
                    price, hit, reason = fill
                    # A timed-out limit sells at the close. An intraday target or stop can free capital
                    # for a new close signal. Legacy open entries need separate same-day slots.
                    at_close = reason == "time" or (not signal_close and entry == i)
                    slot = morning + int(at_close)
                    sizes[slot, col], prices[slot, col] = -np.inf, price
                    closed[col, slot] = hit, reason
                    entry = None
            if signal_close and entry is None and bar["signal"] and np.isnan(sizes[morning + 1, col]):
                entry = i
                if target is not None:
                    limit = limit_price(bar["close"], target)
                    floor = stop_price(bar["close"], stop_pct) if stop_pct else None
                sizes[morning + 1, col], prices[morning + 1, col] = np.inf, bar["close"]
    frame = pd.DataFrame(marks, columns=symbols)
    portfolio = vbt.Portfolio.from_orders(
        frame,
        size=sizes,
        price=prices,
        direction="longonly",
        fees=fee_bps / 10000,
        init_cash=100000 / len(symbols),
        cash_sharing=False,
    )
    equity = portfolio.value.iloc[1::2].sum(axis=1)
    peaks = equity.cummax().clip(lower=100000)
    all_trades = portfolio.trades.records
    records = all_trades[all_trades["status"] == TradeStatus.Closed]
    result = dict(
        hold=hold,
        trades=len(records),
        return_pct=float(equity.iloc[-1] / 100000 - 1) * 100,
        drawdown_pct=float((1 - equity / peaks).max()) * 100,
        equity=equity.round(2).tolist(),
    )
    if detail:
        fills = []
        for trade in records.sort_values(["entry_idx", "col"]).to_dict("records"):
            first, last = trade["entry_idx"] // 2, trade["exit_idx"] // 2
            hit, reason = closed[trade["col"], trade["exit_idx"]]
            fills.append(
                dict(
                    symbol=symbols[trade["col"]],
                    entry=days[first],
                    exit=days[last],
                    buy=trade["entry_price"],
                    sell=trade["exit_price"],
                    pnl=trade["pnl"],
                    pct=trade["return"] * 100,
                    hold=last - first + int(not signal_close),
                    target_hit=hit,
                    exit_reason=reason,
                )
            )
        closes = frame.iloc[1::2]
        result.update(
            fills=fills,
            open_trades=len(all_trades) - len(records),
            benchmark=(closes.div(closes.iloc[0]).mean(axis=1) * 100000).tolist(),
        )
    return result


def target_cohort(data):
    days = data["target_days"]
    completed = data["completed_sessions"]
    prices = data["prices"]
    outcomes, hits, fills, roundtrips = {}, {}, {}, {}
    fee = data["fee_bps"] / 10000
    allocation = 100000 / len(prices)
    daily = [[] for _ in range(completed)]
    for symbol, quotes in prices.items():
        entry = quotes[0]
        status = (
            "Pending entry" if not data["elapsed"][0] else "Entry price unavailable"
        )
        hit = None
        if entry is not None:
            bars = data["target_bars"][symbol]
            limit = limit_price(entry, data["profit_target_pct"])
            floor = stop_price(entry, data["stop_loss_pct"]) if data.get("stop_loss_pct") else None
            status = "Pending exit"
            sell = None
            for i, bar in enumerate(bars):
                if sell is None:
                    try:
                        execution = target_exit(bar, limit, i + 1, i < completed, floor, data["holds"][0])
                    except ValueError as exc:
                        status = f"{days[i]}: {exc}"
                        break
                    if execution is not None:
                        sell, hit, reason = execution
                        fills[symbol] = dict(exit=days[i], sell=sell, hold=i + 1, reason=reason)
                        roundtrips[symbol] = (entry, sell)
                if i < completed:
                    mark = sell * (1 - fee) if sell is not None else bar["close"]
                    if mark is not None:
                        daily[i].append(allocation * mark / (entry * (1 + fee)))
        outcomes[symbol], hits[symbol] = [status], [hit]
    if roundtrips:
        frame = pd.DataFrame(roundtrips)
        entries = pd.DataFrame(False, index=frame.index, columns=frame.columns)
        entries.iloc[0] = True
        pf = vbt.Portfolio.from_signals(
            frame,
            entries=entries,
            exits=~entries,
            fees=fee,
            init_cash=allocation,
            cash_sharing=False,
        )
        for symbol, value in pf.total_return.items():
            outcomes[symbol] = [float(value) * 100]
    # Publish a contiguous series only while every allocation can be valued.
    equity = []
    for values in daily:
        if len(values) != len(prices):
            break
        equity.append(round(sum(values), 2))
    mark_days = days[:len(equity)]
    if data.get("entry_basis") == "signal_close" and all(v[0] is not None for v in prices.values()):
        mark_days = [data["dates"][0], *mark_days]
        equity.insert(0, round(100000 / (1 + fee), 2))
    return dict(
        engine=vbt.__version__,
        outcomes=outcomes,
        target_hits=hits,
        target_fills=fills,
        days=mark_days,
        equity=equity,
    )


def cohort_returns(data):
    holds, elapsed, prices = data["holds"], data["elapsed"], data["prices"]
    outcomes = {symbol: [] for symbol in prices}
    target = data.get("profit_target_pct")
    hits = {}
    for i, hold in enumerate(holds, 1):
        completed = {}
        for symbol, series in prices.items():
            if not elapsed[0]:
                status = "Pending entry"
            elif series[0] is None:
                status = "Entry price unavailable"
            elif not elapsed[i]:
                status = "Pending exit"
            elif series[i] is None:
                status = "Exit price unavailable"
            else:
                completed[symbol] = [series[0], series[i]]
                status = None
            outcomes[symbol].append(status)
            if target is not None:
                hit = None
                if status is None:
                    highs = data.get("session_highs", {}).get(symbol, [])[:hold]
                    hit = reached_target(series[0], target, highs, hold, series[i])
                hits.setdefault(symbol, []).append(hit)
        if completed:
            frame = pd.DataFrame(
                completed, index=pd.to_datetime([data["dates"][0], data["dates"][i]])
            )
            entries = pd.DataFrame(False, index=frame.index, columns=frame.columns)
            entries.iloc[0] = True
            pf = vbt.Portfolio.from_signals(
                frame,
                entries=entries,
                exits=~entries,
                fees=data["fee_bps"] / 10000,
                init_cash=100000,
                cash_sharing=False,
            )
            for symbol, value in pf.total_return.items():
                outcomes[symbol][-1] = float(value) * 100
    result = {"outcomes": outcomes, "engine": vbt.__version__}
    if target is not None:
        result["target_hits"] = hits
    return result


def calculate(data):
    target_mode = data.get("exit_on_target", False)
    signal_close = data.get("entry_basis") == "signal_close"
    holds = data["holds"]
    if target_mode and (not data.get("profit_target_pct") or len(holds) != 1
                        or type(holds[0]) is not int or not 1 <= holds[0] <= MAX_TARGET_SESSIONS):
        raise ValueError(f"Target exits require a positive profit target and one maximum hold of 1–{MAX_TARGET_SESSIONS} sessions.")
    stop_pct = data.get("stop_loss_pct")
    if stop_pct is not None and (not target_mode or not math.isfinite(stop_pct) or not 0 < stop_pct < 100):
        raise ValueError("A stop loss needs a profit target and must be between 0 and 100 percent.")
    if "prices" in data:
        return target_cohort(data) if target_mode else cohort_returns(data)
    series = data.get("series") or {data.get("symbol", ""): data["bars"]}
    bars = next(iter(series.values()))
    holds, fee = data["holds"], data["fee_bps"]
    if not 4 <= len(bars) <= 3000 or not 1 <= len(holds) <= 10:
        raise ValueError("Supply 4–3000 daily bars and 1–10 holding periods.")
    if (
        not math.isfinite(fee)
        or not 0 <= fee <= 100
        or any(type(h) is not int or not 1 <= h <= 252 for h in holds)
    ):
        raise ValueError(
            "Holding periods must be 1–252 sessions and fees 0–100 basis points."
        )
    days = [date.fromisoformat(b["day"]) for b in bars]
    calendar = [b["day"] for b in bars]
    if any([b["day"] for b in items] != calendar for items in series.values()):
        raise ValueError("All symbols must cover the same sessions.")
    if days != sorted(set(days)) or any(
        type(b["signal"]) is not bool
        or any(not math.isfinite(b[k]) or b[k] <= 0 for k in ("open", "close"))
        for items in series.values()
        for b in items
    ):
        raise ValueError("Daily bars must be unique, ordered and have positive prices.")
    split = int(len(bars) * 0.7)
    def run(start, end, hold, detail=False):
        if target_mode or signal_close:
            return simulate_orders(series, start, end, hold, fee, detail,
                                   target=data["profit_target_pct"] if target_mode else None,
                                   signal_close=signal_close, stop_pct=stop_pct)
        return simulate(series, start, end, hold, fee, detail, target=data.get("profit_target_pct") if detail else None)

    trained = [run(0, split, h) for h in dict.fromkeys(holds)]
    candidates = [r for r in trained if r["trades"]]
    best = max(candidates, key=lambda r: r["return_pct"]) if candidates else None
    folds = []
    for left, right in ((0.5, 0.7), (0.7, 1.0)):
        start, end = int(len(bars) * left), int(len(bars) * right)
        if end - start < 2:
            continue
        train = (
            trained
            if start == split
            else [run(0, start, h) for h in dict.fromkeys(holds)]
        )
        valid = [p for p in train if p["trades"]]
        winner = max(valid, key=lambda p: p["return_pct"]) if valid else None
        scores = run(start, end, winner["hold"]) if winner else {}
        folds.append(
            dict(
                start=bars[start]["day"],
                end=bars[end - 1]["day"],
                hold=scores.get("hold"),
                trades=scores.get("trades"),
                return_pct=scores.get("return_pct"),
            )
        )
    return {
        "engine": vbt.__version__,
        "folds": folds,
        "days": [b["day"] for b in bars],
        "parameters": [{k: v for k, v in r.items() if k != "equity"} for r in trained],
        "validation_start": bars[split]["day"],
        "validation": run(split, len(bars), best["hold"])
        if best
        else None,
        "full_period": run(
            0, len(bars), best["hold"] if best else holds[0], detail=True,
        ),
    }
