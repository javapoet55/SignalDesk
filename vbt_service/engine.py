"""Backtest daily Finviz signals with equal starting capital per selected symbol."""

import math
from datetime import date
from decimal import Decimal

import pandas as pd
import vectorbtpro as vbt


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
    if "prices" in data:
        return cohort_returns(data)
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
    trained = [simulate(series, 0, split, h, fee) for h in dict.fromkeys(holds)]
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
            else [simulate(series, 0, start, h, fee) for h in dict.fromkeys(holds)]
        )
        valid = [p for p in train if p["trades"]]
        winner = max(valid, key=lambda p: p["return_pct"]) if valid else None
        scores = simulate(series, start, end, winner["hold"], fee) if winner else {}
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
        "validation": simulate(series, split, len(bars), best["hold"], fee)
        if best
        else None,
        "full_period": simulate(
            series, 0, len(bars), best["hold"] if best else holds[0], fee, detail=True,
            target=data.get("profit_target_pct"),
        ),
    }
