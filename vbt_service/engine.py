"""Backtest one symbol using saved, point-in-time Finviz signals."""

import math
from datetime import date

import pandas as pd
import vectorbtpro as vbt


def simulate(bars, hold, fee_bps):
    index = pd.DatetimeIndex([b["day"] for b in bars])
    opens = pd.Series([b["open"] for b in bars], index=index)
    closes = pd.Series([b["close"] for b in bars], index=index)
    entries = pd.Series(False, index=index)
    exits = entries.copy()
    available = 1
    for i in range(1, len(bars)):
        if i >= available and bars[i - 1]["signal"] and i + hold < len(bars):
            entries.iloc[i] = exits.iloc[i + hold] = True
            available = i + hold + 1
    pf = vbt.Portfolio.from_signals(
        closes,
        entries=entries,
        exits=exits,
        price=opens,
        fees=fee_bps / 10000,
        init_cash=100000,
        freq="1D",
    )
    equity = pf.value
    return {
        "hold": hold,
        "trades": int(entries.sum()),
        "return_pct": float(pf.total_return) * 100,
        "drawdown_pct": abs(float(pf.max_drawdown)) * 100,
        "equity": [round(float(x), 2) for x in equity],
    }


def calculate(data):
    bars, holds, fee = data["bars"], data["holds"], data["fee_bps"]
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
    if days != sorted(set(days)) or any(
        type(b["signal"]) is not bool
        or any(not math.isfinite(b[k]) or b[k] <= 0 for k in ("open", "close"))
        for b in bars
    ):
        raise ValueError("Daily bars must be unique, ordered and have positive prices.")
    split = int(len(bars) * 0.7)
    trained = [simulate(bars[:split], h, fee) for h in dict.fromkeys(holds)]
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
            else [simulate(bars[:start], h, fee) for h in dict.fromkeys(holds)]
        )
        valid = [p for p in train if p["trades"]]
        winner = max(valid, key=lambda p: p["return_pct"]) if valid else None
        scores = simulate(bars[start:end], winner["hold"], fee) if winner else {}
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
        "validation": simulate(bars[split:], best["hold"], fee) if best else None,
        "full_period": simulate(bars, best["hold"] if best else holds[0], fee),
    }
