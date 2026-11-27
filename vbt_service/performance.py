"""Portfolio valuations and trade scoring shared by the saved history and the full report."""

from dataclasses import dataclass


CAPITAL = 100_000


@dataclass
class Performance:
    days: list
    equity: list
    trade_groups: list
    total_return_pct: float | None

    @property
    def scored(self):
        return len(self.trade_groups) - self.trade_groups.count("pending") - self.trade_groups.count("unscored")

    @property
    def win_rate_pct(self):
        scored = self.scored
        return 100 * self.trade_groups.count("wins") / scored if scored else None


def _group(value, has_target, hit):
    if not isinstance(value, (int, float)):
        return "pending"
    if has_target:
        return {True: "wins", False: "losses", None: "unscored"}[hit]
    return "wins" if value > 0 else "losses" if value < 0 else "flat"


def performance(info, result, hold_index=0):
    """History opens the report's default holding period, the first one for a saved buy basket."""
    has_target = info.get("profit_target_pct") is not None
    closed_net = None
    if "outcomes" in result:
        outcomes = result["outcomes"]
        end = info["dates"][hold_index + 1]
        groups, pnl = [], []
        for symbol, values in sorted(outcomes.items()):
            value = values[hold_index]
            hits = result.get("target_hits", {}).get(symbol)
            groups.append(_group(value, has_target, hits[hold_index] if hits else None))
            if isinstance(value, (int, float)):
                pnl.append((CAPITAL / len(outcomes)) * value / 100)
        if pnl and len(pnl) == len(outcomes):
            closed_net = sum(pnl)
        if info.get("exit_on_target"):
            days, equity = result["days"], result["equity"]
        else:
            days, equity = [], []
            for day, ratio in sorted((info.get("close_marks") or {}).items()):
                if day >= end:
                    break
                if ratio is None:
                    days, equity = [], []
                    break
                days.append(day)
                equity.append(CAPITAL * ratio / (1 + info["fee_bps"] / 10000))
            if closed_net is not None and days:
                days.append(end)
                equity.append(CAPITAL + closed_net)
    else:
        full = result.get("full_period", {})
        days, equity = result.get("days", []), full.get("equity", [])
        groups = [_group(t["pnl"], has_target, t.get("target_hit")) for t in full.get("fills", [])]

    total = 100 * (equity[-1] / CAPITAL - 1) if equity else None
    if closed_net is not None:
        total = closed_net / CAPITAL * 100
    return Performance(days, equity, groups, total)
