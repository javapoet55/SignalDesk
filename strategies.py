"""Strategies tab: store uploaded strategy .py files, run them on a scan's tickers with
Massive price data, and keep every result on the volume.

/data/strategies/
    scripts/<name>.py           uploaded strategies
    data/mcap.csv               optional market-cap list (Ticker column) for the V5 universe rule
    strategies.json             settings (kind, auto-run, ...)
    results/<YYYY-MM-DD_HHMMSS>_<name>/
        tickers.txt             the scan's tickers used
        history_cache.json      prices given to the strategy (V5 format)
        signals.csv             the strategy's result table
        output.txt              everything the strategy printed
        meta.json               status, as-of date, data mode, counts
/data/prices/                  Massive daily bars (see prices.py)

Uploaded code runs in a separate process: no secrets in its environment, an
unprivileged user when the server runs as root (as on Railway), CPU/memory/time
limits, and only its own result folder writable.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import os
import queue
import re
import shutil
import signal
import stat
import uuid
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import finviz_agent as fa
import finviz_prices as fvp
import prices as px
from strategy_vault import StrategyVault

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "strategy_runner.py"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
RESULT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}_[A-Za-z0-9_.-]{1,80}$")
RESULT_FILES = {
    "signals.csv": "text/csv; charset=utf-8",
    "output.txt": "text/plain; charset=utf-8",
    "tickers.txt": "text/plain; charset=utf-8",
    "meta.json": "application/json; charset=utf-8",
    "history_cache.json": "application/json; charset=utf-8",
    "ticker_status.csv": "text/csv; charset=utf-8",
    "script_cache.json": "application/json; charset=utf-8",
}
# Whole-script strategies may write their CSV under their own name (e.g. v5_daily_dashboard_<date>.csv).
EXTRA_CSV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,150}\.csv$")


def result_file_type(name: str) -> Optional[str]:
    if name in RESULT_FILES:
        return RESULT_FILES[name]
    return "text/csv; charset=utf-8" if EXTRA_CSV_RE.match(name) else None


def read_meta(d: Path) -> dict:
    """A result's meta.json (while a run is going the folder belongs to the sandbox user, so only a
    plain file inside the folder is trusted)."""
    try:
        if regular_file(d / "meta.json", d):
            m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            if isinstance(m, dict):
                m.setdefault("id", d.name)
                return m
    except (OSError, ValueError):
        pass
    return {"id": d.name, "status": "unknown"}


def read_text(path: Path, inside: Path, limit: int = 2_000_000) -> str:
    if not regular_file(path, inside):
        return ""
    with path.open("rb") as fh:
        fh.seek(max(0, os.fstat(fh.fileno()).st_size - limit))
        return fh.read().decode("utf-8", errors="replace")


def read_csv(path: Path, inside: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """(fields, rows) of a result CSV; nothing for anything that is not a plain file in `inside`."""
    if not regular_file(path, inside):
        return [], []
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)
MAX_SCRIPT_BYTES = 1_000_000
MAX_DATA_BYTES = 10_000_000
NOBODY = 65534


TIMEFRAME_SESSIONS = {1: 6, 5: 15, 15: 15, 30: 70}  # sessions kept per bar size for TIMEFRAMES strategies
CANDLE_RE = re.compile(r"^([1-9][0-9]{0,2})([dm])$")
CANDLE_LIMITS = {"d": (1, 30), "m": (1, 390)}


UNIVERSE_FIELDS = (
    ("price_min", "Minimum stock price ($)"),
    ("price_max", "Maximum stock price ($)"),
    ("cap_min", "Minimum market cap ($M)"),
    ("cap_max", "Maximum market cap ($M)"),
)


def universe_filters(form: dict) -> dict:
    filters = {}
    for key, label in UNIVERSE_FIELDS:
        raw = str(form.get(key, "")).strip()
        if raw:
            try:
                value = float(raw)
            except ValueError:
                value = -1
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be a nonnegative number.")
            filters[key] = value
    for field, label in (("price", "Stock price"), ("cap", "Market cap")):
        if filters.get(field + "_min", 0) > filters.get(field + "_max", math.inf):
            raise ValueError(f"{label} minimum cannot exceed its maximum.")
    return filters


def _scan_number(raw: str, market_cap: bool = False) -> Optional[float]:
    text = (raw or "").strip().upper().replace(",", "").lstrip("$")
    scale = 1
    if market_cap and text[-1:] in ("K", "M", "B", "T"):
        scale = {"K": .001, "M": 1, "B": 1000, "T": 1000000}[text[-1]]
        text = text[:-1]
    try:
        value = float(text) * scale
        return value if math.isfinite(value) and value >= 0 else None
    except ValueError:
        return None


def filter_scan_tickers(scan_dir: Path, tickers: List[str], filters: dict) -> List[str]:
    path = scan_dir / "top_gainers.csv"
    if not path.is_file():
        raise ValueError("This scan has no price and market-cap snapshot. Choose another scan or run a new scan.")
    snapshots = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = (row.get("ticker") or "").strip().upper()
            snapshot = snapshots.setdefault(ticker, {})
            for field, column in (("price", "price"), ("cap", "market_cap")):
                value = _scan_number(row.get(column), market_cap=field == "cap")
                if value is not None:
                    snapshot.setdefault(field, value)

    def matches(ticker: str) -> bool:
        for key, bound in filters.items():
            field, side = key.split("_")
            value = snapshots.get(ticker.upper(), {}).get(field)
            if value is None or (side == "min" and value < bound) or (side == "max" and value > bound):
                return False
        return True

    return [ticker for ticker in tickers if matches(ticker)]


def parse_candles(spec: str) -> Tuple[int, str]:
    """'1d' daily, '5d' 5-day, '15m' 15-minute -> (5, 'd'). Raises ValueError for anything else."""
    m = CANDLE_RE.match((spec or "").strip().lower())
    if not m:
        raise ValueError("Unknown candle size.")
    n, unit = int(m.group(1)), m.group(2)
    lo, hi = CANDLE_LIMITS[unit]
    if not lo <= n <= hi:
        raise ValueError(f"Pick {lo}–{hi} {'days' if unit == 'd' else 'minutes'} per candle.")
    return n, unit


def candles_spec(number: str, unit: str) -> str:
    """Form fields (number, 'days'/'minutes') -> a checked spec like '5d'."""
    u = {"days": "d", "day": "d", "d": "d", "minutes": "m", "minute": "m", "min": "m", "m": "m"}.get(
        (unit or "days").strip().lower())
    if u is None:
        raise ValueError("Pick days or minutes for the candle size.")
    try:
        n = int(str(number or "1").strip())
    except ValueError:
        raise ValueError("Enter a whole number for the candle size.") from None
    lo, hi = CANDLE_LIMITS[u]
    if not lo <= n <= hi:
        raise ValueError(f"Pick {lo}–{hi} {'days' if u == 'd' else 'minutes'} per candle.")
    spec = f"{n}{u}"
    parse_candles(spec)
    return spec


def candles_label(spec: str) -> str:
    n, unit = parse_candles(spec or "1d")
    return "Daily" if (n, unit) == (1, "d") else f"{n}-day" if unit == "d" else f"{n}-min"


def tv_link_ok(url: str) -> str:
    """A TradingView link the page can safely open: https on tradingview.com (or a subdomain), no credentials."""
    from urllib.parse import urlsplit
    url = url.strip()
    if len(url) > 1000:
        raise ValueError("That link is too long.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
    except ValueError:
        raise ValueError("That doesn't look like a link.") from None
    if p.scheme.lower() not in ("http", "https") or p.username or p.password or any(c in url for c in "\"<>'` \t\n\r"):
        raise ValueError("Paste a normal https:// TradingView link.")
    if host != "tradingview.com" and not host.endswith(".tradingview.com"):
        raise ValueError("Use a TradingView link (tradingview.com), e.g. a chart or script page.")
    return "https://" + url.split("://", 1)[1]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _atomic_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


SANDBOX_TZ: Dict[str, str] = {}  # set from SCHEDULE_TZ so a script's datetime.now() is your local time


def sandbox_env() -> Dict[str, str]:
    """Only harmless variables: no tokens, keys or passwords reach uploaded code."""
    return {**SANDBOX_TZ,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }


class RunCancelled(Exception):
    pass


def run_sandboxed(args: List[str], cwd: Path, log_path: Optional[Path], timeout: int,
                  cpu_seconds: int, mem_mb: int, cancel_event=None) -> Tuple[int, str]:
    """Run `python strategy_runner.py <args>` isolated. Returns (exit code, captured text if no log)."""
    cmd = [sys.executable, "-I", "-c",
           "import resource,runpy,sys;"
           f"resource.setrlimit(resource.RLIMIT_CPU,({cpu_seconds},{cpu_seconds}));"
           f"resource.setrlimit(resource.RLIMIT_AS,({mem_mb}<<20,{mem_mb}<<20));"
           f"sys.argv=[{str(RUNNER)!r}]+sys.argv[1:];"
           f"runpy.run_path({str(RUNNER)!r},run_name='__main__')", *args]
    kwargs = dict(cwd=str(cwd), env=sandbox_env(), start_new_session=True)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        kwargs.update(user=NOBODY, group=NOBODY, extra_groups=[])
    out_fh = log_path.open("a", encoding="utf-8") if log_path else None
    try:
        proc = subprocess.Popen(cmd, stdout=out_fh or subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                **kwargs)
        try:
            deadline = time.monotonic() + timeout
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        proc.kill()
                    proc.communicate()
                    return -2, ""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    out, _ = proc.communicate(timeout=min(0.25, remaining))
                    return proc.returncode, out or ""
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.communicate()
            if out_fh:
                out_fh.write(f"\nStopped: the strategy ran longer than {timeout // 60} minutes.\n")
            return -1, ""
    finally:
        if out_fh:
            out_fh.close()


def _chown_nobody(path: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(path, NOBODY, NOBODY)


def _as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def kill_sandbox_leftovers() -> None:
    """Stop every process still running as the sandbox user (e.g. one the strategy started in the
    background), so nothing can change the result folder after the server takes it back."""
    if not _as_root():
        return
    try:
        subprocess.run([sys.executable, "-I", "-c", "import os,signal\ntry:\n os.kill(-1,signal.SIGKILL)\n"
                        "except OSError:\n pass"], user=NOBODY, group=NOBODY, extra_groups=[],
                       env=sandbox_env(), timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def seal_result_dir(out: Path) -> List[str]:
    """After a run: take the folder back from the sandbox user and delete anything that is not a
    plain file or folder (symlinks, pipes, ...). The server reads and writes files here with full
    rights, so a symlink planted by uploaded code must never be followed. Returns what was removed."""
    removed: List[str] = []
    kill_sandbox_leftovers()
    for dirpath, dirnames, filenames in os.walk(out, topdown=True, followlinks=False):
        for nm in list(dirnames) + filenames:
            p = os.path.join(dirpath, nm)
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                os.unlink(p)
                removed.append(os.path.relpath(p, out))
                if nm in dirnames:
                    dirnames.remove(nm)
            elif st.st_nlink > 1:  # a hard link to some file elsewhere: drop the link, never read it
                os.unlink(p)
                removed.append(os.path.relpath(p, out))
        if _as_root():
            os.chown(dirpath, 0, 0)
            os.chmod(dirpath, 0o755)
    return removed


def regular_file(path: Path, inside: Path) -> bool:
    """True only for a plain (non-symlink) file that really is inside `inside`."""
    try:
        st = os.lstat(path)
        path.resolve().relative_to(inside.resolve())
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(st.st_mode)


class StrategyManager:
    def __init__(self, data_dir: Path, reports_dir: Path, tz, env: Dict[str, str],
                 log: Callable[[str], None] = print):
        self.root = data_dir / "strategies"
        self.scripts_dir = self.root / "scripts"
        self.data_files_dir = self.root / "data"
        self.results_dir = self.root / "results"
        self.vault = StrategyVault(self.root / "vault")
        self._file_lock = threading.RLock()
        for d in (self.scripts_dir, self.data_files_dir, self.results_dir):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o755)
        os.chmod(self.root, 0o755)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            # The volume root may be private (0700). Let the sandbox user pass THROUGH it (not list it)
            # so it can reach strategies/ — everything else in /data keeps its own permissions.
            mode = data_dir.stat().st_mode & 0o7777
            if not mode & 0o001:
                os.chmod(data_dir, mode | 0o001)
        self.meta_path = self.root / "strategies.json"
        self.reports_dir = reports_dir
        self.tz = tz
        self.log = log
        tz_name = getattr(tz, "key", "") or ""
        if tz_name and re.fullmatch(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*", tz_name) and \
                Path("/usr/share/zoneinfo", tz_name).is_file():
            SANDBOX_TZ["TZ"] = tz_name
        self.timeout = (int(env["STRATEGY_TIMEOUT_SEC"]) if env.get("STRATEGY_TIMEOUT_SEC")
                        else int(env.get("STRATEGY_TIMEOUT_MIN", "15") or 15) * 60)
        self.mem_mb = int(env.get("STRATEGY_MEMORY_MB", "2048") or 2048)
        key = px.clean_api_key(env.get("MASSIVE_API_KEY") or "")
        self.client = (px.MassiveClient(key, base_url=env.get("MASSIVE_BASE_URL") or px.DEFAULT_BASE,
                                        min_interval=float(env.get("MASSIVE_MIN_INTERVAL", "0.25") or 0.25),
                                        log=log) if key else None)
        history_days = int(env.get("PRICE_HISTORY_DAYS", "150") or 150)
        self.prices = px.PriceStore(data_dir / "prices", history_days, log)
        token = (env.get("FINVIZ_API_TOKEN") or env.get("FINVIZ_API_KEY") or "").strip()
        self.finviz = None
        if token:
            fv_client = fa.FinvizClient(token, base_url=env.get("FINVIZ_BASE_URL") or fa.DEFAULT_BASE_URL,
                                        min_interval=float(env.get("FINVIZ_PRICE_INTERVAL", "0.55") or 0.55),
                                        log=log)
            self.finviz = fvp.FinvizPriceSource(fv_client, data_dir / "prices" / "finviz", history_days, log=log)
        # FinViz (same token as the scans) is the default; Massive only when asked for or no FinViz token.
        choice = (env.get("PRICE_SOURCE") or "").strip().lower()
        self.price_source = choice if choice in ("finviz", "massive") else ("finviz" if token else "massive")
        self._meta_lock = threading.Lock()
        self.sandbox_error = self._preflight()
        if self.sandbox_error:
            log(f"WARNING: strategies cannot run: {self.sandbox_error}")
        self._jobs: "queue.Queue[dict]" = queue.Queue()
        self._queue_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self.current: Optional[dict] = None
        self.queued: List[dict] = []
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def _preflight(self) -> Optional[str]:
        """Prove the locked-down process can start, read the runner and reach the scripts folder."""
        probe = self.scripts_dir / ".preflight.py"
        try:
            probe.write_text("def finviz_signals(tickers, asof, cache):\n    return []\n", encoding="utf-8")
            os.chmod(probe, 0o644)
            code, out = run_sandboxed(["check", str(probe)], cwd=self.scripts_dir, log_path=None,
                                      timeout=120, cpu_seconds=120, mem_mb=self.mem_mb)
            last = out.strip().splitlines()[-1] if out.strip() else ""
            if code == 0 and '"generic"' in last:
                return None
            return ("the locked-down strategy process could not start or reach its files "
                    f"(exit {code}): {out.strip()[-300:] or 'no output'}")
        except OSError as err:
            return f"could not prepare the strategy folder: {err}"
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    # ── settings ───────────────────────────────────────────────────────────
    def _load_all(self) -> dict:
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def meta(self) -> Dict[str, dict]:
        m = self._load_all().get("strategies", {})
        return m if isinstance(m, dict) else {}

    def _save_meta(self, strategies: Dict[str, dict]) -> None:
        data = self._load_all()  # keep the other settings (auto set, last selection)
        data["strategies"] = strategies
        _atomic_json(self.meta_path, data)

    def _save_key(self, key: str, value) -> None:
        with self._meta_lock:
            data = self._load_all()
            data[key] = value
            _atomic_json(self.meta_path, data)

    # The set of strategies that runs after every new scan (one shared setting).
    def auto_config(self) -> dict:
        data = self._load_all()
        cfg = data.get("auto")
        if isinstance(cfg, dict):
            names = [n for n in cfg.get("names", []) if n in data.get("strategies", {})]
            return {"names": names, "mode": cfg.get("mode", "close"), "candles": cfg.get("candles", "1d")}
        legacy = [n for n, m in self.meta().items() if m.get("auto")]  # older per-strategy setting
        modes = {self.meta()[n].get("auto_mode", "close") for n in legacy}
        return {"names": sorted(legacy), "mode": modes.pop() if len(modes) == 1 else "close", "candles": "1d"}

    def set_auto_set(self, names: List[str], enabled: bool, mode: str, candles: str = "1d") -> str:
        if mode not in ("close", "intraday"):
            raise ValueError("Unknown price basis.")
        parse_candles(candles)
        known = self.meta()
        names = [n for n in dict.fromkeys(names) if n in known]
        if enabled and not names:
            raise ValueError("Select at least one strategy to run after each scan.")
        self._save_key("auto", {"names": names if enabled else [], "mode": mode, "candles": candles})
        with self._meta_lock:  # retire the old per-strategy flags
            m = self.meta()
            for n in m:
                m[n].pop("auto", None); m[n].pop("auto_mode", None)
            self._save_meta(m)
        if not enabled:
            return "Strategies will no longer run automatically after scans."
        return (f"After each new scan, {len(names)} strateg{'y' if len(names) == 1 else 'ies'} will run "
                f"({'intraday' if mode == 'intraday' else 'latest close'}"
                + (f", {candles_label(candles)} candles" if candles != "1d" else "") + f"): {', '.join(names)}.")

    def selection(self) -> dict:
        sel = self._load_all().get("selection")
        return sel if isinstance(sel, dict) else {}

    def save_selection(self, names: List[str], scan: str, mode: str, when: str, candles: str = "1d", filters: Optional[dict] = None) -> None:
        self._save_key("selection", {"names": list(dict.fromkeys(names)), "scan": scan, "mode": mode,
                                     "when": when, "candles": candles,
                                     "filters": self.selection().get("filters", {}) if filters is None else filters})

    def list(self) -> List[Tuple[str, dict]]:
        m = self.meta()
        return sorted(((n, m[n]) for n in m if (self.scripts_dir / m[n]["file"]).is_file()), key=lambda x: x[0].lower())

    # ── uploads ────────────────────────────────────────────────────────────
    def upload_strategy(self, filename: str, content: bytes) -> str:
        with self._file_lock:
            return self._upload_strategy(filename, content)

    def _upload_strategy(self, filename: str, content: bytes) -> str:
        if self.sandbox_error:
            raise ValueError("Strategies can't run on this server right now: " + self.sandbox_error)
        base = Path(filename or "").name
        if not base.lower().endswith(".py"):
            raise ValueError("Choose a Python file ending in .py.")
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", base[:-3])[:80].strip("._-") or "strategy"
        if not NAME_RE.match(name):
            raise ValueError("Rename the file using letters, numbers, dashes or underscores.")
        if not content:
            raise ValueError("That file is empty.")
        if len(content) > MAX_SCRIPT_BYTES:
            raise ValueError("That file is over 1 MB; strategy scripts should be much smaller.")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("That file is not plain UTF-8 text.") from None
        tmp = self.scripts_dir / f".upload_{name}.py"
        tmp.write_bytes(content)
        os.chmod(tmp, 0o644)
        previous_content = None
        try:
            code, out = run_sandboxed(["check", str(tmp)], cwd=self.scripts_dir, log_path=None,
                                      timeout=60, cpu_seconds=60, mem_mb=self.mem_mb)
            try:
                result = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
            except (ValueError, IndexError):
                result = {}
            if code != 0 or not result:
                raise ValueError("Could not check the file: " + (out.strip()[-400:] or f"exit {code}"))
            if result.get("error"):
                raise ValueError(result["error"])
            if not result.get("kind"):
                raise ValueError(
                    "This file is not a supported strategy. It needs one of: (1) a main() that loads "
                    "CACHE_FILE = '…/history_cache.json' and writes a CSV"
                    + (f" — {result['script_problem']}" if result.get("script_problem") else "")
                    + "; (2) the two V5 functions passes_v5_filters(idx, closes, volumes) and "
                    f"detect_macd_cross_up(idx, closes) (missing: {', '.join(result.get('missing', []))}); "
                    "or (3) a finviz_signals(tickers, asof, cache) function.")
            final = self.scripts_dir / f"{name}.py"
            if final.exists():
                if not regular_file(final, self.scripts_dir):
                    raise ValueError("The current strategy file is invalid. Nothing was replaced.")
                previous_content = final.read_bytes()
                self.vault.save("versions", name, self.meta().get(name, {}), previous_content)
            os.replace(tmp, final)
        finally:
            if tmp.exists():
                tmp.unlink()
        with self._meta_lock:
            m = self.meta()
            prev = m.get(name, {})
            m[name] = {"file": f"{name}.py", "kind": result["kind"],
                       "uploaded_utc": utc_now().isoformat(timespec="seconds"),
                       "sha256": hashlib.sha256(content).hexdigest()[:12], "bytes": len(content),
                       "auto": prev.get("auto", False), "auto_mode": prev.get("auto_mode", "close")}
            if prev.get("tv_url"):
                m[name]["tv_url"] = prev["tv_url"]
            if result.get("candles") is True:
                m[name]["candles"] = True
            if isinstance(result.get("timeframes"), list):
                m[name]["timeframes"] = result["timeframes"]
            for key in ("title", "description"):
                if isinstance(result.get(key), str) and result[key]:
                    m[name][key] = result[key]
            try:
                self._save_meta(m)
            except OSError:
                if previous_content is not None:
                    rollback = self.scripts_dir / f".rollback_{name}.py"
                    rollback.write_bytes(previous_content)
                    os.chmod(rollback, 0o644)
                    os.replace(rollback, final)
                else:
                    final.unlink()
                raise
        if result["kind"] == "script":
            st = result.get("settings") or {}
            moved = ", ".join((st.get("cache") or []) + (st.get("out") or []))
            other = st.get("other") or []
            return (f"{'Updated' if prev else 'Added'} '{name}' (whole script). Each run uses your main() "
                    f"unchanged, with {moved} pointed at the scan's prices and the result folder."
                    + (f" Note: {', '.join(other)} also point to files on your Mac; the server can't see them."
                       if other else ""))
        kind = "V5-style strategy" if result["kind"] == "v5" else "custom strategy (finviz_signals)"
        notes = {"build_spy_regime": "no market-regime filter", "get_tier": "no tier sizing",
                 "simulate_trade": "no trade tracking"}
        without = [notes[f] for f in result.get("without", []) if f in notes]
        return (f"{'Updated' if prev else 'Added'} '{name}' ({kind})."
                + (f" Note: {', '.join(without)} in this file." if without else ""))

    def delete_strategy(self, name: str) -> str:
        with self._file_lock:
            return self._delete_strategy(name)

    def _delete_strategy(self, name: str) -> str:
        with self._meta_lock:
            m = self.meta()
            if name not in m:
                raise ValueError("That strategy no longer exists. Reload the page.")
            path = self.scripts_dir / m[name]["file"]
            if not regular_file(path, self.scripts_dir):
                raise ValueError("The strategy file is missing or invalid. Nothing was deleted.")
            content = path.read_bytes()
            self.vault.save("trash", name, m[name], content)
            path.unlink()
            del m[name]
            data = self._load_all()
            data["strategies"] = m
            for key in ("auto", "selection"):
                if isinstance(data.get(key), dict) and isinstance(data[key].get("names"), list):
                    data[key]["names"] = [n for n in data[key]["names"] if n != name]
            try:
                _atomic_json(self.meta_path, data)
            except OSError:
                path.write_bytes(content)
                os.chmod(path, 0o644)
                raise
        return f"Moved '{name}' to the recycle bin for 30 days. Its past results are kept."

    def archive_entries(self, bucket: str) -> List[dict]:
        with self._file_lock:
            return self.vault.entries(bucket)

    def archive_record(self, bucket: str, id: str) -> dict:
        with self._file_lock:
            return self.vault.read(bucket, id)[0]

    def purge_expired_strategies(self) -> None:
        with self._file_lock:
            self.vault.purge_expired()

    def restore_strategy(self, bucket: str, id: str) -> str:
        with self._file_lock:
            record, content = self.vault.read(bucket, id)
            name = record["name"]
            if bucket == "trash" and (name in self.meta() or (self.scripts_dir / f"{name}.py").exists()):
                raise ValueError("A strategy with this name already exists. Restore was cancelled to avoid overwriting it.")
            # Apply the same sandbox validation as a new upload. A version rollback also archives
            # the current active file first, so the rollback itself is recoverable.
            self.upload_strategy(f"{name}.py", content)
            if bucket == "trash":
                self.vault.remove(bucket, id)
            return (f"Restored '{name}' from the recycle bin." if bucket == "trash"
                    else f"Restored the saved version of '{name}'. The previous active version was preserved.")

    def set_note(self, name: str, note: str) -> str:
        if len(note) > 1000:
            raise ValueError("Strategy notes can contain at most 1000 characters.")
        with self._meta_lock:
            meta = self.meta()
            if name not in meta:
                raise ValueError("That strategy no longer exists. Reload the page.")
            meta[name]["note"] = note.strip()
            self._save_meta(meta)
        return f"Note saved for '{name}'."

    def set_tv_link(self, name: str, url: str) -> str:
        """Save (or with an empty url, remove) a strategy's TradingView link."""
        url = (url or "").strip()
        if url:
            url = tv_link_ok(url)
        with self._meta_lock:
            m = self.meta()
            if name not in m:
                raise ValueError("That strategy no longer exists. Reload the page.")
            if url:
                m[name]["tv_url"] = url
            else:
                m[name].pop("tv_url", None)
            self._save_meta(m)
        return f"TradingView link saved for '{name}'." if url else f"TradingView link removed from '{name}'."

    def set_auto(self, name: str, auto: bool, mode: str) -> str:
        if mode not in ("close", "intraday"):
            raise ValueError("Unknown data mode.")
        with self._meta_lock:
            m = self.meta()
            if name not in m:
                raise ValueError("That strategy no longer exists. Reload the page.")
            m[name].update(auto=bool(auto), auto_mode=mode)
            self._save_meta(m)
        return (f"'{name}' will run automatically after each scan ({'intraday' if mode == 'intraday' else 'latest close'})."
                if auto else f"'{name}' will only run when you press Run.")

    def mcap_path(self) -> Path:
        return self.data_files_dir / "mcap.csv"

    def upload_mcap(self, content: bytes) -> str:
        if not content or len(content) > MAX_DATA_BYTES:
            raise ValueError("The market-cap file must be a CSV under 10 MB.")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("The market-cap file is not plain UTF-8 text.") from None
        rows = list(csv.DictReader(text.splitlines()))
        if not rows or "Ticker" not in rows[0]:
            raise ValueError("The market-cap CSV needs a 'Ticker' column (like your 'tickers_with_mcap' file).")
        n = sum(1 for r in rows if (r.get("Ticker") or "").strip())
        self.mcap_path().write_text(text, encoding="utf-8")
        os.chmod(self.mcap_path(), 0o644)
        return f"Market-cap list saved ({n:,} tickers). V5's >$50M rule will use it."

    def delete_mcap(self) -> str:
        if self.mcap_path().exists():
            self.mcap_path().unlink()
        return "Market-cap list removed. V5 runs will skip the market-cap rule."

    # ── runs ───────────────────────────────────────────────────────────────
    def enqueue(self, name: str, scan: str, mode: str, reason: str, candles: str = "1d",
                     filters: Optional[dict] = None) -> str:
        parse_candles(candles)
        filters = universe_filters(filters or {})
        if name not in self.meta():
            raise ValueError(f"Strategy '{name}' no longer exists. Reload the page.")
        if mode not in ("close", "intraday"):
            raise ValueError("Unknown data mode.")
        if scan != "latest" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}", scan):
            raise ValueError("Unknown scan.")
        with self._queue_lock:
            # the same strategy on the same scan with the same prices is already waiting or running
            for other in self.queued + ([self.current] if self.current else []):
                if (other["name"], other["scan"], other["mode"], other.get("candles", "1d")) == \
                        (name, scan, mode, candles) and scan != "latest" and other.get("filters", {}) == filters:
                    return f"'{name}' is already queued for that scan."
            job = {"job_id": uuid.uuid4().hex, "name": name, "scan": scan, "mode": mode, "reason": reason, "candles": candles, "filters": filters,
                   "queued_utc": utc_now().isoformat(timespec="seconds")}
            self.queued.append(job)
            self._jobs.put(job)
        return f"'{name}' is queued. Results appear below when it finishes."

    def enqueue_many(self, names: List[str], scan: str, mode: str, reason: str, candles: str = "1d",
                     filters: Optional[dict] = None) -> List[str]:
        """Queue several strategies on the same scan and prices; returns problems (if any)."""
        problems = []
        for n in names:
            try:
                if filters:
                    self.enqueue(n, scan, mode, reason, candles, filters)
                else:
                    self.enqueue(n, scan, mode, reason, candles)
            except ValueError as err:
                problems.append(str(err))
        return problems

    def enqueue_auto(self, scan_stamp: str) -> int:
        cfg = self.auto_config()
        for name in cfg["names"]:
            self.enqueue(name, scan_stamp, cfg["mode"], "after scan", cfg.get("candles", "1d"))
        return len(cfg["names"])

    def _check_cancel(self):
        if self._cancel_event.is_set():
            raise RunCancelled()

    def cancel(self, ident: str) -> str:
        """Cancel an exact queued/current job, or close a stale running result."""
        with self._queue_lock:
            for job in self.queued:
                if job.get("job_id") == ident:
                    job["cancelled"] = True
                    self.queued.remove(job)
                    return "Queued job cancelled."
            cur = self.current
            if cur and ident in (cur.get("job_id"), cur.get("result_id")):
                self._cancel_event.set()
                cur["stopping"] = True
                return "Stop requested. Any active price request will finish before the job stops."
            if RESULT_RE.fullmatch(ident or ""):
                folder = self.results_dir / ident
                meta = read_meta(folder)
                if meta.get("status") == "running":
                    meta.update(status="cancelled", finished_utc=utc_now().isoformat(timespec="seconds"),
                                error="Cancelled by user; no active worker for this result.")
                    _atomic_json(folder / "meta.json", meta)
                    return "Inactive running result marked cancelled."
        raise ValueError("This job has already finished or is no longer queued. Reload the page.")

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            with self._queue_lock:
                if job.get("cancelled"):
                    continue
                self._cancel_event.clear()
                if job in self.queued:
                    self.queued.remove(job)
                self.current = dict(job, started_utc=utc_now().isoformat(timespec="seconds"))
            try:
                self._run(job)
            except RunCancelled:
                cur = self.current or {}
                rid = cur.get("result_id")
                if rid:
                    folder = self.results_dir / rid
                    meta = read_meta(folder)
                    meta.update(status="cancelled", finished_utc=utc_now().isoformat(timespec="seconds"), error="Stopped by user")
                    _atomic_json(folder / "meta.json", meta)
                    with (folder / "output.txt").open("a") as fh:
                        fh.write("\nStopped by user.\n")
            except Exception as err:  # never let the worker die
                self.log(f"strategy run failed: {type(err).__name__}: {err}")
            finally:
                with self._queue_lock:
                    self.current = None

    def _resolve_scan(self, scan: str) -> Optional[Path]:
        if scan == "latest":
            runs = sorted((p for p in self.reports_dir.iterdir()
                           if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}", p.name)),
                          key=lambda p: p.name) if self.reports_dir.is_dir() else []
            runs = [p for p in runs if (p / "tickers.txt").is_file()]
            return runs[-1] if runs else None
        p = self.reports_dir / scan
        return p if (p / "tickers.txt").is_file() else None

    def _run(self, job: dict) -> None:
        name, mode = job["name"], job["mode"]
        started = utc_now()
        base = f"{started.astimezone(self.tz):%Y-%m-%d_%H%M%S}_{name}"
        rid, n = base, 1
        while True:  # the same strategy can start twice in one second (auto-run + schedule): never collide
            out = self.results_dir / rid
            try:
                out.mkdir(parents=True)
                break
            except FileExistsError:
                n += 1
                rid = f"{base}-{n}"
        os.chmod(out, 0o755)
        with self._queue_lock:
            if self.current and self.current.get("job_id") == job.get("job_id"):
                self.current["result_id"] = rid
        log_path = out / "output.txt"
        meta = {"id": rid, "strategy": name, "mode": mode, "reason": job["reason"], "status": "running",
                "started_utc": started.isoformat(timespec="seconds")}
        _atomic_json(out / "meta.json", meta)

        def note(msg: str) -> None:
            if self._cancel_event.is_set():
                raise RunCancelled()
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(msg + "\n")

        def finish(status: str, **extra) -> None:
            if self._cancel_event.is_set():
                raise RunCancelled()
            meta.update(status=status, finished_utc=utc_now().isoformat(timespec="seconds"), **extra)
            _atomic_json(out / "meta.json", meta)
            self.log(f"strategy {name}: {status}")

        strat = self.meta().get(name)
        if not strat or not (self.scripts_dir / strat["file"]).is_file():
            note("The strategy file was removed before this run started.")
            return finish("error", error="strategy removed")
        meta["strategy_sha256"] = strat.get("sha256")
        scan_dir = self._resolve_scan(job["scan"])
        if scan_dir is None:
            note("No scan with a tickers.txt was found. Run a scan first.")
            return finish("error", error="no scan")
        shutil.copyfile(scan_dir / "tickers.txt", out / "tickers.txt")
        meta["scan"] = scan_dir.name
        tickers_text = (out / "tickers.txt").read_text(encoding="utf-8").splitlines()
        tickers = [t for t in (tickers_text[1] if len(tickers_text) > 1 else "").split(",") if t]
        filters = job.get("filters") or {}
        if filters:
            meta["filters"] = filters
            before = len(tickers)
            try:
                tickers = filter_scan_tickers(scan_dir, tickers, filters)
            except ValueError as error:
                note(str(error))
                return finish("error", error=str(error))
            (out / "tickers.txt").write_text(
                tickers_text[0] + "\n" + ",".join(tickers) + "\n", encoding="utf-8"
            )
            note(f"Scan filters kept {len(tickers)} of {before} tickers. "
                 "Tickers with missing values for an active filter were excluded.")
            if not tickers:
                return finish("error", error="No tickers matched the stock price and market cap filters.")
        note(f"Strategy: {name} ({strat['kind']})   Scan: {scan_dir.name}   Tickers: {len(tickers)}")

        meta["price_source"] = self.price_source
        spec = job.get("candles") or "1d"
        n_candle, unit = parse_candles(spec)
        if spec != "1d" and not (strat.get("candles") is True and strat["kind"] == "generic"):
            note(f"{candles_label(spec)} candles were picked, but this strategy only works on daily candles, so it "
                 "ran on daily candles. (A strategy opts in with a CANDLE_DAYS setting, like fmfi_scanner.py.)")
            spec, n_candle, unit = "1d", 1, "d"
        if unit == "m" and (self.price_source != "finviz" or self.finviz is None):
            note("Minute candles need FinViz prices (FINVIZ_API_TOKEN, and PRICE_SOURCE not set to massive); "
                 "this run used daily candles.")
            spec, n_candle, unit = "1d", 1, "d"
        meta.update(candles=spec)
        if unit == "m":
            return self._run_minutes(strat, tickers, n_candle, mode, scan_dir.name[:10], started, out, log_path,
                                     meta, note, finish)
        if self.price_source == "finviz":
            cache_info = self._prices_from_finviz(tickers, mode, scan_dir.name[:10], started, note,
                                                  history_days=min(self.finviz.history_days * n_candle, 2600)
                                                  if n_candle > 1 else None)
            if cache_info is None:
                return finish("error", error="no price data")
            cache, info, intraday = cache_info
        else:
            cache_info = self._prices_from_massive(tickers, mode, scan_dir.name[:10], started, note)
            if cache_info is None:
                return finish("error", error="no price data")
            cache, info, intraday = cache_info
        if not cache["history"]:
            note("No price data available.")
            return finish("error", error="no price data")
        scan_day = scan_dir.name[:10]
        today = started.astimezone(px.ET).date().isoformat()
        cache_path = out / "history_cache.json"
        days = sorted(cache["history"])
        if intraday and info.get("last_day") == today:
            asof = today
        else:
            asof = [d for d in days if d <= scan_day][-1] if any(d <= scan_day for d in days) else days[-1]
        if n_candle > 1:  # n-day candles, each ticker's newest one ending on its last day up to the as-of day
            daily_n = len([d for d in days if d <= asof])
            cache = fvp.day_candles({"tickers": cache.get("tickers", []),
                                     "history": {d: cache["history"][d] for d in days if d <= asof}}, n_candle)
            days = sorted(cache["history"])
            note(f"Built {candles_label(spec)} candles from {daily_n} trading days: {len(days)} candle end dates "
                 "(each ticker's newest candle ends on its last trading day).")
        extra = [t for t in (strat.get("timeframes") or []) if t != "1d"]
        if extra and strat["kind"] == "generic" and n_candle == 1:
            cache["timeframes"] = self._extra_timeframes(tickers, extra, mode, scan_day, asof, started, note)
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        os.chmod(cache_path, 0o644)
        source_label = "FinViz" if self.price_source == "finviz" else "Massive"
        meta.update(asof=asof, data_note=f"{source_label}: " + ((info.get("intraday") or "intraday")
                                                                 if intraday else "latest close"),
                    price_days=len(days), first_price_day=days[0])
        note(f"Prices ({source_label}): {len(days)} {'days' if n_candle == 1 else candles_label(spec) + ' candles'} "
             f"({days[0]} → {days[-1]}); as of {asof}"
             + (f"; {info['intraday']}" if intraday and info.get("intraday") else ""))
        self._run_strategy(strat, out, log_path, cache_path, asof, note, finish, spec)

    def _extra_timeframes(self, tickers, tfs, mode, scan_day, asof, started, note) -> Dict[str, dict]:
        """Minute bars a strategy asked for with TIMEFRAMES (e.g. ["1m", "30m", "1d"]), in addition to the
        daily bars: {"1m": history, ...}, regular session, New York time, no bar after the as-of day.
        Each size keeps the recent sessions its 20-bar indicators need (FinViz keeps ~3 weeks of 1-15
        minute bars and ~9 months of 30-minute bars)."""
        found: Dict[str, dict] = {}
        if self.price_source != "finviz" or self.finviz is None:
            note("This strategy also uses minute bars, which come from FinViz (FINVIZ_API_TOKEN, PRICE_SOURCE not "
                 "massive); it ran with daily bars only.")
            return found
        forming = mode == "intraday" and scan_day == started.astimezone(self.tz).date().isoformat()
        for tf in tfs:
            minutes = int(tf[:-1])
            sessions = TIMEFRAME_SESSIONS.get(minutes, 10)
            per_day = -(-390 // minutes)
            note(f"Getting {minutes}-minute prices from FinViz Elite for {len(set(tickers)) + 1} tickers "
                 f"(regular session, last {sessions} sessions) …")
            try:
                c, info = self.finviz.build_minute_cache(tickers, minutes, include_forming=forming, now_utc=started,
                                                        progress=note, cancel_check=self._check_cancel, max_candles=sessions * per_day)
            except fvp.FinvizPriceError as err:
                note(f"  {minutes}-minute prices failed: {err}")
                continue
            hist = {k: v for k, v in c["history"].items() if k[:10] <= asof}
            found[tf] = hist
            missing = info.get("missing") or {}
            note(f"  {minutes}-minute bars: {info['tickers_with_prices']} of {info['tickers_requested']} tickers, "
                 f"{len({k[:10] for k in hist})} sessions up to {asof}"
                 + (f"; none for {len(missing)}: " + ", ".join(sorted(missing)[:20]) if missing else ""))
        return found

    def _run_minutes(self, strat, tickers, minutes, mode, scan_day, started, out, log_path, meta, note, finish):
        """Minute candles from FinViz intraday bars (regular session, New York time)."""
        label = candles_label(f"{minutes}m")
        today = started.astimezone(px.ET).date().isoformat()
        forming = mode == "intraday" and scan_day == started.astimezone(self.tz).date().isoformat()
        base = fvp.intraday_base(minutes)
        note(f"Getting {base}-minute prices from FinViz Elite for {len(set(tickers)) + 1} tickers (incl. SPY) "
             f"to build {label} candles (regular session 9:30–16:00 ET) …")
        try:
            cache, info = self.finviz.build_minute_cache(tickers, minutes, include_forming=forming, now_utc=started,
                                                        progress=note, cancel_check=self._check_cancel)
        except fvp.FinvizPriceError as err:
            note(f"FinViz prices failed: {err}")
            return finish("error", error="no price data")
        missing = info.get("missing") or {}
        note(f"FinViz prices: {info['tickers_with_prices']} of {info['tickers_requested']} tickers"
             + (f"; no prices for {len(missing)}: " + ", ".join(sorted(missing)[:30])
                + (" …" if len(missing) > 30 else "") if missing else ""))
        if scan_day < today:  # an older scan: only candles up to that day's close
            cache["history"] = {k: v for k, v in cache["history"].items() if k[:10] <= scan_day}
        keys = sorted(cache["history"])
        if not keys:
            note(f"No {label} candles on or before {scan_day}. FinViz keeps about "
                 f"{'9 months' if base == 30 else '2–3 weeks'} of {base}-minute bars.")
            return finish("error", error="no price data")
        asof = keys[-1]
        cache_path = out / "history_cache.json"
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        os.chmod(cache_path, 0o644)
        meta.update(asof=asof, data_note=f"FinViz: {label} candles" + (", newest may still be forming" if forming
                                                                       else ", completed candles"),
                    price_days=len(keys), first_price_day=keys[0])
        note(f"Prices (FinViz): {len(keys)} {label} candles ({keys[0]} → {keys[-1]} ET); as of {asof} ET"
             + ("; the newest candle may still be forming" if forming else "; completed candles only"))
        self._run_strategy(strat, out, log_path, cache_path, asof, note, finish, f"{minutes}m")

    def _prices_from_finviz(self, tickers, mode, scan_day, started, note, history_days=None):
        if self.finviz is None:
            note("FINVIZ_API_TOKEN is not set, so FinViz prices are unavailable.")
            return None
        intraday = mode == "intraday"
        if intraday and scan_day != started.astimezone(self.tz).date().isoformat():
            note("Intraday mode only applies to today's scan; using latest close for this older scan.")
            intraday = False
        note(f"Getting daily prices from FinViz Elite for {len(set(tickers)) + 1} tickers (incl. SPY) …")
        try:
            cache, info = self.finviz.build_cache(tickers, include_intraday=intraday, progress=note, cancel_check=self._check_cancel,
                                                  history_days=history_days)
        except fvp.FinvizPriceError as err:
            note(f"FinViz prices failed: {err}")
            return None
        missing = info.get("missing") or {}
        note(f"FinViz prices: {info['tickers_with_prices']} of {info['tickers_requested']} tickers"
             + (f"; no prices for {len(missing)}: " + ", ".join(sorted(missing)[:30])
                + (" …" if len(missing) > 30 else "") if missing else ""))
        if info.get("reused"):
            note(f"  {info['reused']} reused from the download saved after the last market close, "
                 f"{info['downloaded']} downloaded now.")
        if info.get("dropped_today"):
            note("Market is open: today's unfinished bar was left out (latest close mode).")
        return cache, info, intraday

    def _prices_from_massive(self, tickers, mode, scan_day, started, note):
        if self.client is not None:
            try:
                before = len(self.prices.trading_days())
                note("Updating daily prices from Massive" + (" (first run: downloading history; on a free "
                     "Massive plan this can take ~30 minutes)" if before == 0 else "") + " …")
                res = self.prices.update(self.client)
                note(f"Prices: {res}")
            except px.PriceError as err:
                note(f"Price update failed: {err}")
                if not self.prices.trading_days():
                    return None
                note("Continuing with the prices already stored.")
        elif not self.prices.trading_days():
            note("MASSIVE_API_KEY is not set and no prices are stored. Add the key in Railway → Variables.")
            return None
        intraday = mode == "intraday"
        if intraday and scan_day != started.astimezone(self.tz).date().isoformat():
            note("Intraday mode only applies to today's scan; using latest close for this older scan.")
            intraday = False
        try:
            cache, info = self.prices.build_cache(tickers, include_intraday=intraday, client=self.client)
        except px.PriceError as err:
            note(f"Could not get intraday prices: {err}. Using latest close instead.")
            cache, info = self.prices.build_cache(tickers, include_intraday=False)
            intraday = False
        return cache, info, intraday

    def _run_strategy(self, strat, out, log_path, cache_path, asof, note, finish, spec="1d") -> None:
        for p in (out, out / "tickers.txt", log_path, out / "meta.json"):
            _chown_nobody(p)
        args = ["run", str(self.scripts_dir / strat["file"]), "--tickers", str(out / "tickers.txt"),
                "--cache", str(cache_path), "--out", str(out), "--asof", asof]
        if strat.get("candles") is True:
            args += ["--candle-label", candles_label(spec)]
        if strat["kind"] == "v5" and self.mcap_path().is_file():
            args += ["--mcap", str(self.mcap_path())]
        elif strat["kind"] == "v5":
            note("No market-cap list uploaded: the >$50M universe rule is not applied.")
        code, _ = run_sandboxed(args, cwd=out, log_path=log_path, timeout=self.timeout,
                                cpu_seconds=self.timeout, mem_mb=self.mem_mb, cancel_event=self._cancel_event)
        removed = seal_result_dir(out)
        if removed:
            note("Removed links the strategy created (not allowed): " + ", ".join(removed[:20]))
        counts: Dict[str, int] = {}
        sig = out / "signals.csv"
        status_file = out / "ticker_status.csv"
        _, rows = read_csv(status_file if regular_file(status_file, out) else sig, out)
        for r in rows:
            k = r.get("Status") or "rows"
            counts[k] = counts.get(k, 0) + 1
        ok = code == 0 and regular_file(sig, out)
        finish("ok" if ok else "error", exit_code=code, counts=counts,
               mcap_rule=bool(strat["kind"] == "v5" and self.mcap_path().is_file()))

    def delete_result(self, rid: str) -> str:
        if not RESULT_RE.fullmatch(rid or ""):
            raise ValueError("Invalid run ID.")
        with self._queue_lock:
            folder = self.results_dir / rid
            if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != self.results_dir.resolve():
                raise ValueError("That run no longer exists. Reload the page.")
            meta = read_meta(folder)
            if meta.get("status") == "running" or (self.current and self.current.get("result_id") == rid):
                raise ValueError("Stop this run before deleting its history.")
            shutil.rmtree(folder)
        return "Run history deleted, including its result files and log."

    def results(self, limit: int = 100) -> List[dict]:
        out = []
        for p in sorted(self.results_dir.iterdir(), key=lambda p: p.name, reverse=True)[:limit]:
            if p.is_dir() and RESULT_RE.match(p.name):
                m = read_meta(p)
                out.append(m)
        return out

    def update_prices_async(self) -> bool:
        if self.client is None or self.price_source != "massive" or self.prices.status.get("running"):
            return False

        def go():
            try:
                self.prices.update(self.client)
            except px.PriceError as err:
                self.log(f"price update failed: {err}")
        threading.Thread(target=go, daemon=True).start()
        return True
