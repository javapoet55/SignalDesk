"""Technical indicators on daily bars, calculated with VectorBT PRO's native indicator classes.

This module is separate from the backtest engine. The worker runs it for requests marked
kind="indicators"; backtests never touch it. VectorBT PRO is imported only when an indicator
is calculated, so the API process, which has no licence, can still load the limits below.
"""

import math

KIND = "indicators"
MAX_SYMBOLS = 10
MAX_INDICATORS = 12
MAX_BARS = 600

# name -> ordered (parameter, default, minimum, maximum, whole number). Must match vbt_indicators.CATALOG.
PARAMS = {
    "sma": (("window", 20, 2, 250, True),),
    "ema": (("window", 20, 2, 250, True),),
    "bbands": (("window", 20, 2, 250, True), ("alpha", 2, 0.5, 5, False)),
    "rsi": (("window", 14, 2, 100, True),),
    "macd": (("fast_window", 12, 2, 100, True), ("slow_window", 26, 3, 250, True), ("signal_window", 9, 2, 100, True)),
    "atr": (("window", 14, 2, 100, True),),
    "obv": (),
}


def normalize(name, params):
    """Return the indicator's complete, checked parameters. Raises ValueError with a plain message."""
    if name not in PARAMS:
        raise ValueError(f"Unknown indicator: {name}.")
    params = dict(params or {})
    extra = set(params) - {key for key, *_ in PARAMS[name]}
    if extra:
        raise ValueError(f"{name.upper()} does not take {', '.join(sorted(extra))}.")
    clean = {}
    for key, default, low, high, whole in PARAMS[name]:
        value = params.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name.upper()} {key} must be a number.")
        if whole and value != int(value):
            raise ValueError(f"{name.upper()} {key} must be a whole number.")
        if not low <= value <= high:
            raise ValueError(f"{name.upper()} {key} must be between {low:g} and {high:g}.")
        clean[key] = int(value) if whole else float(value)
    if name == "macd" and clean["fast_window"] >= clean["slow_window"]:
        raise ValueError("MACD fast window must be shorter than the slow window.")
    return clean


def _line(result):
    """One output of an indicator run on one price series, as a plain pandas Series."""
    return result.iloc[:, 0] if getattr(result, "ndim", 1) == 2 else result


def _ema(vbt, close, window):
    # VectorBT PRO selects the average with wtype; the open-source library used ewm.
    try:
        return vbt.MA.run(close, window, wtype="exp").ma
    except TypeError:
        return vbt.MA.run(close, window, ewm=True).ma


def _need(frame, *columns):
    for column in columns:
        if frame[column].isna().any():
            raise ValueError(f"needs the daily {column} for every session, and some are missing.")


def _sma(vbt, frame, window):
    return {"sma": vbt.MA.run(frame["close"], window).ma}


def _ema_lines(vbt, frame, window):
    return {"ema": _ema(vbt, frame["close"], window)}


def _bbands(vbt, frame, window, alpha):
    bands = vbt.BBANDS.run(frame["close"], window=window, alpha=alpha)
    return {"upper": bands.upper, "middle": bands.middle, "lower": bands.lower}


def _rsi(vbt, frame, window):
    return {"rsi": vbt.RSI.run(frame["close"], window).rsi}


def _macd(vbt, frame, fast_window, slow_window, signal_window):
    macd = vbt.MACD.run(frame["close"], fast_window=fast_window, slow_window=slow_window, signal_window=signal_window)
    return {"macd": macd.macd, "signal": macd.signal, "hist": macd.hist}


def _atr(vbt, frame, window):
    _need(frame, "high", "low")
    return {"atr": vbt.ATR.run(frame["high"], frame["low"], frame["close"], window=window).atr}


def _obv(vbt, frame):
    _need(frame, "volume")
    return {"obv": vbt.OBV.run(frame["close"], frame["volume"]).obv}


RUNNERS = {"sma": _sma, "ema": _ema_lines, "bbands": _bbands, "rsi": _rsi, "macd": _macd, "atr": _atr, "obv": _obv}


def _values(series, keep):
    out = []
    for value in _line(series).iloc[keep:].tolist():
        number = float(value) if value is not None else math.nan
        out.append(round(number, 6) if math.isfinite(number) else None)
    return out


def compute(request):
    """Calculate every requested indicator for every symbol.

    Indicators are calculated on all the bars supplied, so a long average is already warmed up at
    the first displayed day; only days from display_from onward are returned. One indicator that
    cannot be calculated is reported in its own entry and does not stop the others.
    """
    import pandas as pd
    import vectorbtpro as vbt

    specs = [(spec["name"], normalize(spec["name"], spec.get("params"))) for spec in request["indicators"]]
    first_day = request.get("display_from") or ""
    symbols = {}
    for symbol, bars in request["series"].items():
        days = [bar["day"] for bar in bars]
        index = pd.DatetimeIndex(days)
        frame = {
            column: pd.Series([bar.get(column) for bar in bars], index=index, dtype="float64")
            for column in ("open", "high", "low", "close", "volume")
        }
        keep = next((i for i, day in enumerate(days) if day >= first_day), len(days))
        lines = []
        for name, params in specs:
            entry = dict(name=name, params=params)
            try:
                entry["outputs"] = {key: _values(series, keep) for key, series in RUNNERS[name](vbt, frame, **params).items()}
            except Exception as exc:  # Report it beside the indicators that did work.
                entry["error"] = (str(exc) or type(exc).__name__)[:200]
            lines.append(entry)
        symbols[symbol] = dict(days=days[keep:], close=_values(frame["close"], keep), bars_used=len(days), indicators=lines)
    return dict(engine="VectorBT PRO " + str(getattr(vbt, "__version__", "")), symbols=symbols)
