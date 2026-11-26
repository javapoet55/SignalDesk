#!/usr/bin/env python3
"""Cloud runner for finviz_agent.py.

One always-on process that:
  * runs the agent when you schedule it on the Scans tab (Run now, Schedule for
    later, Every weekday) in YOUR time zone (daylight saving handled, unlike
    UTC-only cron), and
  * serves a password-protected web page listing every run by date, with links
    to each run's report, tickers.txt, CSV and JSON.

Designed for Railway (one service + one volume) but runs anywhere Python 3.9+
does. Standard library only, plus the 'tzdata' package on minimal containers.

Environment variables
---------------------
FINVIZ_API_TOKEN     required  FinViz Elite API token (passed to the agent; never shown)
DASHBOARD_PASSWORD   required  password for the web page
DASHBOARD_USER       optional  user name for the web page (default: admin)
SCHEDULE_TIMES       optional  comma-separated HH:MM (default: 12:00,13:30), or off. Used ONLY the
                               first time this version starts, to create your scan schedule;
                               after that, scan times are managed on the Scans tab
SCHEDULE_DAYS        optional  days for those first scan times, e.g. mon-fri (default: mon-fri)
SCHEDULE_TZ          optional  IANA time zone (default: America/Los_Angeles)
SCREENS_JSON         optional  full screens.json text, used ONLY to create the presets file on
                               the volume the first time; after that, presets are managed on
                               the web page (Your presets: Add / Remove)
KEEP_DAYS            optional  delete runs older than this many days (default: 0 = keep all)
DATA_DIR             optional  where runs are stored (default: the Railway volume
                               mount path, else ./data)
PORT                 set by Railway
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import hmac
import html
import json
import os
import re
import shlex
import shutil
import signal
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # find finviz_agent.py next to this file even under `python -I`
    sys.path.insert(0, str(_HERE))
import backtests
import finviz_agent as fa  # noqa: E402  same folder; its rules validate presets added on the page
import strategies as sm  # noqa: E402  Strategies tab (uploaded .py strategies + price data)
try:  # TradingView watchlist sync (optional: everything else works without it)
    import tv_sync as tvs  # noqa: E402
except ImportError:  # pragma: no cover
    tvs = None
import schedules as sched  # noqa: E402  scheduled strategy runs
import login_otp as otp  # noqa: E402  email verification for browser sign-in
import change_guard as cg  # noqa: E402  single-use approvals for configuration changes
import journal as jn  # noqa: E402  Journal tab (your trades, imports, stats)
import journal_chart as jc
import trading_notes as tn
import notebook as nb
import open_trade_marks as otm  # noqa: E402  interactive journal performance explorer
import email.parser  # noqa: E402
import email.policy  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    sys.exit("Python 3.9+ is required.")

HERE = Path(__file__).resolve().parent
AGENT = HERE / "finviz_agent.py"
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
LOG_RE = re.compile(r"^[A-Za-z0-9_.-]+\.log$")
RUN_FILES: Dict[str, str] = {
    "report.html": "text/html; charset=utf-8",
    "tickers.txt": "text/plain; charset=utf-8",
    "top_gainers.csv": "text/csv; charset=utf-8",
    "summary.json": "application/json; charset=utf-8",
}
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
RUN_TIMEOUT_SEC = 20 * 60
AGENT_EXIT_TEXT = {
    0: "OK",
    1: "finished with warnings",
    2: "configuration error (check screens.json / SCREENS_JSON)",
    3: "FinViz rejected the token (check FINVIZ_API_TOKEN)",
    4: "every screen failed",
    5: "HTTPS certificate problem",
    -1: "timed out",
    -2: "could not start the agent",
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def parse_times(spec: str) -> List[dt.time]:
    if spec.strip().lower() in ("off", "none", "manual"):
        return []  # no automatic scans; use Run now on the Scans tab or a scheduled "new scan" job
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(f"SCHEDULE_TIMES: {part!r} is not a 24-hour HH:MM time (e.g. 13:30).")
        out.append(dt.time(int(m.group(1)), int(m.group(2))))
    if not out:
        raise ValueError("SCHEDULE_TIMES needs at least one time.")
    return sorted(set(out))


def parse_days(spec: str) -> List[int]:
    days: set = set()
    for part in spec.lower().replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a[:3] not in DAY_NAMES or b[:3] not in DAY_NAMES:
                raise ValueError(f"SCHEDULE_DAYS: unknown day range {part!r}.")
            i, j = DAY_NAMES.index(a[:3]), DAY_NAMES.index(b[:3])
            days.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
        else:
            if part[:3] not in DAY_NAMES:
                raise ValueError(f"SCHEDULE_DAYS: unknown day {part!r}. Use mon,tue,wed,thu,fri,sat,sun.")
            days.add(DAY_NAMES.index(part[:3]))
    if not days:
        raise ValueError("SCHEDULE_DAYS needs at least one day.")
    return sorted(days)


@dataclass
class Settings:
    tz_name: str
    tz: dt.tzinfo
    times: List[dt.time]
    days: List[int]
    data_dir: Path
    user: str
    password: str
    port: int
    keep_days: int
    screens_json: Optional[str]
    token_present: bool
    extra_args: List[str] = field(default_factory=list)

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        tz_name = env.get("SCHEDULE_TZ", "").strip() or "America/Los_Angeles"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            raise ValueError(f"SCHEDULE_TZ: unknown time zone {tz_name!r} (e.g. America/Los_Angeles).") from None
        data_dir = env.get("DATA_DIR", "").strip() or env.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        try:
            port = int(env.get("PORT", "8080"))
            keep_days = int(env.get("KEEP_DAYS", "0") or 0)
        except ValueError:
            raise ValueError("PORT and KEEP_DAYS must be whole numbers.") from None
        return cls(
            tz_name=tz_name,
            tz=tz,
            times=parse_times(env.get("SCHEDULE_TIMES", "") or "12:00,13:30"),
            days=parse_days(env.get("SCHEDULE_DAYS", "") or "mon-fri"),
            data_dir=Path(data_dir) if data_dir else HERE / "data",
            user=env.get("DASHBOARD_USER", "").strip() or "admin",
            password=env.get("DASHBOARD_PASSWORD", ""),
            port=port,
            keep_days=max(0, keep_days),
            screens_json=env.get("SCREENS_JSON") or None,
            token_present=bool((env.get("FINVIZ_API_TOKEN") or env.get("FINVIZ_API_KEY") or "").strip()),
            extra_args=shlex.split(env.get("FINVIZ_AGENT_EXTRA_ARGS", "")),
        )

    def schedule_text(self) -> str:
        if not self.times:
            return "Off (scans run only when started from the page or a schedule)"
        if self.days == [0, 1, 2, 3, 4]:
            day_text = "Mon–Fri"
        else:
            day_text = ", ".join(DAY_NAMES[d].title() for d in self.days)
        times = ", ".join(f"{t:%H:%M}" for t in self.times)
        return f"{day_text} at {times} ({self.tz_name})"


# --------------------------------------------------------------------------
# Persistent state (on the volume)
# --------------------------------------------------------------------------
class State:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self._data, dict):
                self._data = {}
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def update(self, **values) -> None:
        with self._lock:
            self._data.update(values)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)


# --------------------------------------------------------------------------
# Running the agent
# --------------------------------------------------------------------------
def prune_old(settings: Settings, today: dt.date) -> List[str]:
    """Delete run folders and logs older than KEEP_DAYS (0 = keep everything)."""
    if settings.keep_days <= 0:
        return []
    cutoff = today - dt.timedelta(days=settings.keep_days)
    removed: List[str] = []
    if settings.reports_dir.is_dir():
        for p in settings.reports_dir.iterdir():
            if p.is_dir() and STAMP_RE.match(p.name) and dt.date.fromisoformat(p.name[:10]) < cutoff:
                shutil.rmtree(p)
                removed.append(p.name)
    if settings.logs_dir.is_dir():
        for p in settings.logs_dir.iterdir():
            if p.is_file() and LOG_RE.match(p.name) and re.match(r"\d{4}-\d{2}-\d{2}", p.name):
                if dt.date.fromisoformat(p.name[:10]) < cutoff:
                    p.unlink()
                    removed.append(p.name)
    return removed


class Runner:
    def __init__(self, settings: Settings, state: State, config_path: Path,
                 log: Callable[[str], None] = print):
        self.settings = settings
        self.state = state
        self.config_path = config_path
        self.log = log
        self._lock = threading.Lock()
        self.running = False
        self.current: Optional[Dict[str, str]] = None
        self._thread: Optional[threading.Thread] = None
        self.on_finished: Optional[Callable[[int], None]] = None  # e.g. auto-run strategies

    def command(self) -> List[str]:
        return [
            sys.executable, str(AGENT),
            "--config", str(self.config_path),
            "--out", str(self.settings.reports_dir),
            "--timezone", self.settings.tz_name,
            "--quiet",
            *self.settings.extra_args,
        ]

    def start(self, reason: str, slot: Optional[dt.datetime] = None) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.current = {"reason": reason, "started_utc": utc_now().isoformat(timespec="seconds")}
        if slot is not None:
            self.state.update(last_slot=slot.isoformat())
        self._thread = threading.Thread(target=self._run, args=(reason,), daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, reason: str) -> None:
        started = utc_now()
        self.settings.logs_dir.mkdir(parents=True, exist_ok=True)
        log_name = f"{started.astimezone(self.settings.tz):%Y-%m-%d_%H%M%S}_{reason}.log"
        log_path = self.settings.logs_dir / log_name
        code = -2
        try:
            with log_path.open("w", encoding="utf-8") as fh:
                fh.write(f"Run started {started.astimezone(self.settings.tz):%Y-%m-%d %H:%M:%S %Z} ({reason})\n")
                fh.flush()
                try:
                    proc = subprocess.run(self.command(), stdout=fh, stderr=subprocess.STDOUT,
                                          cwd=str(HERE), timeout=RUN_TIMEOUT_SEC)
                    code = proc.returncode
                except subprocess.TimeoutExpired:
                    code = -1
                    fh.write(f"\nStopped: the run took longer than {RUN_TIMEOUT_SEC // 60} minutes.\n")
                except OSError as err:
                    fh.write(f"\nCould not start the agent: {err}\n")
                fh.write(f"\nExit code {code}: {AGENT_EXIT_TEXT.get(code, 'error')}\n")
        except OSError as err:
            self.log(f"could not write log {log_path}: {err}")
        finished = utc_now()
        result = {
            "reason": reason,
            "started_utc": started.isoformat(timespec="seconds"),
            "finished_utc": finished.isoformat(timespec="seconds"),
            "exit_code": code,
            "status": AGENT_EXIT_TEXT.get(code, "error"),
            "log": log_name,
        }
        self.state.update(last_run=result)
        self.log(f"run finished ({reason}): exit {code} {result['status']}; log {log_name}")
        try:
            removed = prune_old(self.settings, finished.astimezone(self.settings.tz).date())
            if removed:
                self.log(f"pruned {len(removed)} old item(s)")
        except OSError as err:
            self.log(f"pruning failed: {err}")
        with self._lock:
            self.running = False
            self.current = None
        if self.on_finished is not None:
            try:
                self.on_finished(code)
            except Exception as err:  # a follow-up must never break the scheduler
                self.log(f"after-run step failed: {type(err).__name__}: {err}")


# --------------------------------------------------------------------------
# Screens configuration: one screens.json on the volume, managed from the page
# --------------------------------------------------------------------------
MAX_NAME_LEN = 80
MAX_URL_LEN = 2000


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def seed_config(settings: Settings) -> Optional[str]:
    """On first start, create the page-managed screens.json on the volume from the
    SCREENS_JSON variable (if set) or the uploaded screens.json. Afterwards the file
    on the volume is the single source of truth. Returns an error text or None."""
    path = settings.data_dir / "screens.json"
    if path.is_file():
        return None
    if settings.screens_json:
        text, source = settings.screens_json, "The SCREENS_JSON variable"
    else:
        try:
            text, source = (HERE / "screens.json").read_text(encoding="utf-8"), "The uploaded screens.json"
        except OSError as err:
            return f"Could not read the uploaded screens.json: {err}"
    try:
        cfg = json.loads(text)
    except ValueError as err:
        return f"{source} is not valid JSON: {err}"
    if not isinstance(cfg, dict):
        return f"{source} must contain a JSON object."
    _write_json_atomic(path, cfg)
    return None


def check_config(path: Path) -> Tuple[Optional[str], int]:
    """(error text or None, number of screens) using the agent's own rules."""
    try:
        cfg = fa.load_config(path)
        count = len(fa.build_screens(cfg))
        fa.parse_timeframes(cfg.get("timeframes", ["change"]))
        return None, count
    except fa.AgentError as err:
        return str(err), 0


class PresetStore:
    """Add/remove saved presets in the volume's screens.json, validated before saving."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def presets(self) -> List[dict]:
        cfg = fa.load_config(self.path)
        return [p for p in (cfg.get("saved_presets") or []) if isinstance(p, dict)]

    def builtin_count(self) -> int:
        cfg = fa.load_config(self.path)
        sig = cfg.get("builtin_signals", {"enabled": True, "include": "all"})
        try:
            return len([s for s in fa.build_screens({"builtin_signals": sig})])
        except fa.AgentError:
            return 0

    def top_n(self) -> int:
        try:
            return int(fa.load_config(self.path).get("top_n", 10))
        except (fa.AgentError, TypeError, ValueError):
            return 10

    def set_top_n(self, value: str) -> str:
        """The usual number of top gainers kept from every screen (presets on 'usual' and built-in screens)."""
        try:
            n = int(str(value).strip())
        except ValueError:
            raise ValueError("Enter a whole number of stocks per screen.") from None
        if not 1 <= n <= 500:
            raise ValueError("Pick 1–500 stocks per screen.")
        with self._lock:
            cfg = fa.load_config(self.path)
            cfg["top_n"] = n
            _write_json_atomic(self.path, cfg)
        return f"Every screen will keep its top {n} gainers from the next scan (presets set to another Keep value keep theirs)."

    def set_keep(self, index: int, name: str, keep: str) -> str:
        """How many stocks to keep from one preset: '' = the usual top N, a number, or 'all'."""
        keep = (keep or "").strip().lower()
        try:
            fa.parse_keep(keep)
        except fa.AgentError as err:
            raise ValueError(str(err)) from None
        with self._lock:
            cfg = fa.load_config(self.path)
            presets = [p for p in (cfg.get("saved_presets") or []) if isinstance(p, dict)]
            if not (0 <= index < len(presets)) or str(presets[index].get("name", "")) != name:
                raise ValueError("The preset list changed. Reload the page and try again.")
            if keep:
                presets[index]["keep"] = keep
            else:
                presets[index].pop("keep", None)
            cfg["saved_presets"] = presets
            try:
                fa.build_screens(cfg)
            except fa.AgentError as err:
                raise ValueError(str(err)) from None
            _write_json_atomic(self.path, cfg)
        what = ("every stock" if keep == "all" else f"the top {keep}" if keep else
                f"the top {self.top_n()} (the usual)")
        return f"'{name}' will keep {what} from the next scan."

    def add(self, name: str, url: str, keep: str = "") -> str:
        keep = (keep or "").strip().lower()
        name = " ".join((name or "").split())
        url = (url or "").strip()
        if not name:
            raise ValueError("Enter a name for the preset.")
        if not url:
            raise ValueError("Paste the preset's FinViz URL.")
        if len(name) > MAX_NAME_LEN:
            raise ValueError(f"Keep the name under {MAX_NAME_LEN} characters.")
        if len(url) > MAX_URL_LEN:
            raise ValueError("That URL is too long to be a FinViz screener link.")
        try:
            fa.screen_from_preset(name, url, keep)  # same checks the agent applies at run time
        except fa.AgentError as err:
            raise ValueError(str(err)) from None
        with self._lock:
            cfg = fa.load_config(self.path)
            presets = [p for p in (cfg.get("saved_presets") or []) if isinstance(p, dict)]
            if any(str(p.get("name", "")).strip().lower() == name.lower() for p in presets):
                raise ValueError(f"A preset named '{name}' already exists. Pick a different name.")
            presets.append({"name": name, "url": url, **({"keep": keep} if keep else {})})
            cfg["saved_presets"] = presets
            try:
                fa.build_screens(cfg)
            except fa.AgentError as err:
                raise ValueError(str(err)) from None
            _write_json_atomic(self.path, cfg)
        return f"Added '{name}'. It will be included from the next run."

    def remove(self, index: int, name: str) -> str:
        with self._lock:
            cfg = fa.load_config(self.path)
            presets = [p for p in (cfg.get("saved_presets") or []) if isinstance(p, dict)]
            if not (0 <= index < len(presets)) or str(presets[index].get("name", "")) != name:
                raise ValueError("The preset list changed. Reload the page and try again.")
            removed = presets.pop(index)
            cfg["saved_presets"] = presets
            try:
                fa.build_screens(cfg)
            except fa.AgentError:
                raise ValueError("Can't remove the last screen: there would be nothing left to scan.") from None
            _write_json_atomic(self.path, cfg)
        return f"Removed '{removed.get('name')}'. Its URL was: {removed.get('url', '')}"


# --------------------------------------------------------------------------
# Web page
# --------------------------------------------------------------------------
@dataclass
class App:
    settings: Settings
    state: State
    runner: Runner
    config_error: Optional[str]
    screen_count: int
    presets: Optional[PresetStore] = None
    strategies: Optional["sm.StrategyManager"] = None
    tradingview: Optional[object] = None  # tv_sync.TVSync
    schedules: Optional["sched.ScheduleStore"] = None
    journal: Optional["jn.JournalStore"] = None
    notes: Optional["tn.NoteStore"] = None
    notebook: Optional["nb.Notebook"] = None
    open_marks: Optional["otm.OpenTradeMarks"] = None
    clock: Callable[[], dt.datetime] = utc_now
    _flash: Optional[Tuple[str, str]] = None
    _flash_lock: threading.Lock = field(default_factory=threading.Lock)

    def refresh_config(self) -> None:
        self.config_error, self.screen_count = check_config(self.runner.config_path)

    def set_flash(self, kind: str, text: str, section: str = "presets") -> None:
        with self._flash_lock:
            self._flash = (kind, text, section)

    def pop_flash(self, section: str = "presets") -> Optional[Tuple[str, str]]:
        """Return the pending message if it belongs to `section` (shown where the page lands)."""
        with self._flash_lock:
            if self._flash and self._flash[2] == section:
                flash, self._flash = self._flash, None
                return flash[0], flash[1]
            return None


@dataclass
class RunInfo:
    stamp: str
    when: dt.datetime
    tickers: Optional[List[str]]
    files: List[str]


def list_runs(settings: Settings) -> List[RunInfo]:
    runs: List[RunInfo] = []
    if not settings.reports_dir.is_dir():
        return runs
    for p in settings.reports_dir.iterdir():
        if not (p.is_dir() and STAMP_RE.match(p.name)):
            continue
        try:
            when = dt.datetime.strptime(p.name, "%Y-%m-%d_%H%M%S").replace(tzinfo=settings.tz)
        except ValueError:
            continue
        tickers: Optional[List[str]] = None
        tfile = p / "tickers.txt"
        if tfile.is_file():
            lines = tfile.read_text(encoding="utf-8").splitlines()
            line = lines[1] if len(lines) > 1 else ""
            tickers = [t for t in line.split(",") if t]
        runs.append(RunInfo(p.name, when, tickers, [f for f in RUN_FILES if (p / f).is_file()]))
    runs.sort(key=lambda r: r.stamp, reverse=True)
    return runs


def recent_logs(settings: Settings, n: int = 10) -> List[str]:
    if not settings.logs_dir.is_dir():
        return []
    names = sorted((p.name for p in settings.logs_dir.iterdir() if p.is_file() and LOG_RE.match(p.name)),
                   reverse=True)
    return names[:n]


_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--up:#0f7b3f;--down:#b3261e;
--accent:#1f4e79;--btn:#1f4e79;--btnink:#fff;--warnbg:#fff4dc;--warn:#8a5a00}
:root[data-theme=dark]{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#a3a29b;
--line:#33322f;--up:#4cc283;--down:#ff8a80;--accent:#8fb8e0;--btn:#8fb8e0;--btnink:#141413;--warnbg:#3a2f14;--warn:#f0c26b}
:root{color-scheme:light}:root[data-theme=dark]{color-scheme:dark}
.app-nav{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:18px}
.app-nav .tabs{flex:1;margin-bottom:0;min-width:0}
.account-controls{display:flex;align-items:center;justify-content:flex-end;gap:12px;flex-wrap:wrap;margin-left:auto}
.welcome{font-size:14px;font-weight:500;white-space:nowrap}
.logout-form{margin:0}.logout-form button{background:transparent;color:var(--ink);border:1px solid var(--line);
font-size:13px;padding:8px 12px;white-space:nowrap}
.signin{max-width:440px;margin:10vh auto;padding:24px}.signin label{display:block;margin:16px 0 0}
.signin input{display:block;width:100%;font:inherit;padding:10px;margin-top:5px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--ink)}.signin button{margin-top:20px;width:100%}
.theme-switch{display:inline-flex;gap:3px;padding:3px;background:var(--card);border:1px solid var(--line);
border-radius:10px;flex-shrink:0;margin-left:auto}
.theme-switch button{background:transparent;color:var(--muted);padding:6px 10px;border-radius:7px;
font-size:13px;font-weight:500;white-space:nowrap;min-height:32px}
.theme-switch button[aria-pressed=true]{background:var(--btn);color:var(--btnink)}
.theme-switch button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(pointer:coarse){.theme-switch button{min-height:44px}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 60px}h1{font-size:26px;margin:0 0 4px}
h2{font-size:18px;margin:30px 0 10px}.muted{color:var(--muted)}a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin:12px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.k{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.v{font-size:15px;font-weight:600}
.ok{color:var(--up)}.bad{color:var(--down)}.warn{background:var(--warnbg);color:var(--warn);border-radius:8px;
padding:8px 12px;margin:10px 0;font-size:14px;white-space:pre-wrap}
button{background:var(--btn);color:var(--btnink);border:0;border-radius:8px;padding:8px 14px;font:inherit;
font-weight:600;cursor:pointer}button[disabled]{opacity:.55;cursor:default}
.tick{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13.5px;word-break:break-all;
background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0;
max-height:220px;overflow-y:auto}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:7px 8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
td.n{text-align:right;font-variant-numeric:tabular-nums}.links a{margin-right:12px;white-space:nowrap}
.wrap{overflow-x:auto}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.okbox{background:var(--card);border:1px solid var(--up);color:var(--up);border-radius:8px;padding:8px 12px;
margin:10px 0;font-size:14px;word-break:break-word}
.addform{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr) auto auto;gap:10px;align-items:end;margin-top:14px}
.addform select{display:block;margin-top:4px}td.keep{white-space:nowrap}.topnform{display:flex;flex-wrap:wrap;gap:8px 10px;align-items:center;margin:0 0 12px}
.topnform label{font-weight:600}.topnform input{width:80px;font:inherit;font-size:14px;color:var(--ink);
background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 8px}.topnform .muted{font-size:13px}.keepform{display:flex;gap:6px;align-items:center;margin:0}button.ghost.keepsave{color:var(--accent)}
.addform label{display:block;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.addform input{display:block;width:100%;margin-top:4px;font:inherit;font-size:14px;color:var(--ink);
background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.addform input:focus{outline:2px solid var(--accent);outline-offset:1px}
button.ghost{background:transparent;color:var(--down);border:1px solid var(--line);padding:4px 10px;font-weight:500}
td.name{font-weight:600;overflow-wrap:break-word;min-width:8.5em}td.url{color:var(--muted);font-size:13px;word-break:break-all}
td.act{text-align:right;white-space:nowrap}td.act form{margin:0}
@media (max-width:640px){.addform{grid-template-columns:1fr}}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:0 0 18px;overflow-x:auto}
.tabs a{padding:8px 14px;border-bottom:2px solid transparent;color:var(--muted);font-weight:600;white-space:nowrap}
.tabs a:hover{text-decoration:none;color:var(--ink)}.tabs a.on{color:var(--ink);border-bottom-color:var(--accent)}
select,input[type=file]{font:inherit;font-size:14px;color:var(--ink);background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:7px 8px;max-width:100%}
.forms{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin-top:10px}
.forms form{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0}
.forms label.inline{display:flex;gap:6px;align-items:center;font-size:14px}
.shead{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:baseline}
.tag{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 8px}
pre.out{white-space:pre-wrap;word-break:break-word;font-size:12.5px;background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:10px 12px;max-height:480px;overflow:auto}
details summary{cursor:pointer;color:var(--accent);margin:8px 0}
"""


def _fmt_local(iso_utc: Optional[str], tz: dt.tzinfo) -> str:
    if not iso_utc:
        return "—"
    try:
        return f"{dt.datetime.fromisoformat(iso_utc).astimezone(tz):%a %b %d, %H:%M:%S %Z}"
    except ValueError:
        return iso_utc


def _finviz_link(url: str) -> Optional[str]:
    """Return the URL only if it is a plain http(s) finviz.com link (safe to put in href)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme in ("http", "https") and parts.netloc.lower().endswith("finviz.com"):
        return url
    return None


def render_presets(app: App) -> str:
    e = html.escape
    out = ["<h2 id='presets'>Your presets</h2><div class='card'>"]
    flash = app.pop_flash()  # result of the last Add/Remove, shown where the page lands (#presets)
    if flash:
        out.append(f"<div class='{'okbox' if flash[0] == 'ok' else 'warn'}' id='msg' role='status'>"
                   f"{e(flash[1])}</div>")
    if app.presets is None:
        return "".join(out) + "<p class='muted'>Preset editing is not available.</p></div>"
    try:
        presets = app.presets.presets()
        builtin = app.presets.builtin_count()
    except fa.AgentError as err:
        return "".join(out) + f"<div class='warn'>Could not read the presets file: {e(str(err))}</div></div>"
    top_n = app.presets.top_n()
    out.append("<form method='post' action='/presets/topn' class='topnform'>"
               "<label for='topn'>Usual top per screen</label>"
               f"<input type='number' id='topn' name='top_n' min='1' max='500' step='1' value='{top_n}' required>"
               "<button type='submit' class='ghost keepsave'>Save</button>"
               "<span class='muted'>How many top gainers each screen adds to the scan's tickers, unless a preset's "
               "Keep says otherwise.</span></form>")
    if presets:
        out.append("<div class='wrap'><table><thead><tr><th>Name</th><th>FinViz screen</th><th>Keep</th><th></th></tr>"
                   "</thead><tbody>")
        for i, p in enumerate(presets):
            name = str(p.get("name", ""))
            url = str(p.get("url", ""))
            off = " <span class='muted'>(off)</span>" if p.get("enabled", True) is False else ""
            link = _finviz_link(url)
            shown = e(url if len(url) <= 90 else url[:87] + "…")
            url_html = (f"<a href='{e(link, quote=True)}' target='_blank' rel='noopener noreferrer'>{shown}</a>"
                        if link else shown)
            out.append(
                f"<tr><td class='name'>{e(name)}{off}</td><td class='url'>{url_html}</td>"
                "<td class='keep'><form method='post' action='/presets/keep' class='keepform'>"
                f"<input type='hidden' name='index' value='{i}'>"
                f"<input type='hidden' name='name' value='{e(name, quote=True)}'>"
                f"<select name='keep' aria-label='Stocks to keep from {e(name, quote=True)}'>"
                + _keep_options(str(p.get("keep", "") or ""), top_n)
                + "</select><button type='submit' class='ghost keepsave'>Save</button></form></td>"
                "<td class='act'><form method='post' action='/presets/remove'>"
                f"<input type='hidden' name='index' value='{i}'>"
                f"<input type='hidden' name='name' value='{e(name, quote=True)}'>"
                "<button type='submit' class='ghost'>Remove</button></form></td></tr>"
            )
        out.append("</tbody></table></div>")
    else:
        out.append("<p class='muted'>No saved presets yet. Add one below.</p>")
    out.append(
        "<form method='post' action='/presets/add' class='addform' autocomplete='off'>"
        f"<label>Name<input name='name' required maxlength='{MAX_NAME_LEN}' placeholder='e.g. Small-cap momentum'>"
        "</label>"
        f"<label>FinViz URL<input name='url' type='url' required maxlength='{MAX_URL_LEN}' "
        "placeholder='https://elite.finviz.com/screener…'></label>"
        "<label>Keep<select name='keep'>" + _keep_options("", app.presets.top_n()) + "</select></label>"
        "<button type='submit'>Add</button></form>"
        "<p class='muted' style='margin:10px 0 0;font-size:13.5px'>In FinViz, load the preset, copy the address-bar "
        "URL and paste it here. <b>Keep</b> sets how many of each preset's top gainers go into the scan's tickers "
        "(All = every stock in that screen); built-in screens keep the usual top. Duplicates are removed when "
        "the lists are merged. Changes apply from the next run. "
        f"Also scanning {builtin} built-in FinViz screen{'s' if builtin != 1 else ''}.</p>"
    )
    if app.settings.screens_json:
        out.append("<p class='muted' style='margin:6px 0 0;font-size:13.5px'>Presets are managed on this page now; "
                   "the SCREENS_JSON variable in Railway is no longer used and can be deleted.</p>")
    out.append("</div>")
    return "".join(out)


KEEP_CHOICES = ("5", "10", "20", "25", "50", "100", "all")


def _keep_options(current: str, top_n: int) -> str:
    current = (current or "").strip().lower()
    opts = [("", f"Top {top_n} (usual)")] + [(k, "All" if k == "all" else f"Top {k}") for k in KEEP_CHOICES]
    if current and current not in [v for v, _ in opts]:
        opts.append((current, f"Top {current}"))
    return "".join(f"<option value='{html.escape(v, quote=True)}'{' selected' if v == current else ''}>"
                   f"{html.escape(t)}</option>" for v, t in opts)


TABS = (("scans", "/", "Scans"), ("strategies", "/strategies", "Strategies"), ("journal", "/journal", "Journal"),
        ("report", "/latest/report.html", "Latest report"))
TABS = TABS + (("paper", "/paper", "Paper Trading"), ("notes", "/notes", "Trading Notes"), ("tradingview", "/tradingview", "TradingView"), ("notebook", "/notebook", "Notes"))


_THEME_INIT = """<script>(function(){
var root=document.documentElement, key='finviz-color-theme', chosen=null;
var system=window.matchMedia('(prefers-color-scheme: dark)');
try{var saved=localStorage.getItem(key);if(saved==='light'||saved==='dark')chosen=saved;}catch(e){}
function apply(theme){
root.dataset.theme=theme;
document.querySelectorAll('[data-theme-choice]').forEach(function(b){
b.setAttribute('aria-pressed',String(b.dataset.themeChoice===theme));
});
}
apply(chosen||(system.matches?'dark':'light'));
function greeting(){var h=new Date().getHours();document.querySelectorAll('[data-welcome]').forEach(function(el){
el.textContent='Good '+(h<12?'Morning':h<17?'Afternoon':'Evening')+' Sri';});}
document.addEventListener('DOMContentLoaded',function(){apply(root.dataset.theme);greeting();});
setInterval(greeting,60000);
document.addEventListener('click',function(e){
var b=e.target.closest('[data-theme-choice]');if(!b)return;
chosen=b.dataset.themeChoice;apply(chosen);
try{localStorage.setItem(key,chosen);}catch(e){}
});
system.addEventListener('change',function(e){if(!chosen)apply(e.matches?'dark':'light');});
window.addEventListener('storage',function(e){if(e.key!==key)return;
chosen=e.newValue==='light'||e.newValue==='dark'?e.newValue:null;
apply(chosen||(system.matches?'dark':'light'));
});
})();</script>"""


def theme_control() -> str:
    return ("<div class='theme-switch' role='group' aria-label='Color theme'>"
            "<button type='button' data-theme-choice='light' aria-pressed='false'>"
            "<span aria-hidden='true'>☀</span> Day</button>"
            "<button type='button' data-theme-choice='dark' aria-pressed='false'>"
            "<span aria-hidden='true'>☾</span> Night</button></div>")


def account_controls() -> str:
    return ("<div class='account-controls'><span class='welcome' data-welcome>Welcome Sri</span>"
            + theme_control() + "<form class='logout-form' method='post' action='/logout'>"
            "<button type='submit'>Logout</button></form></div>")


def render_tabs(active: str) -> str:
    return ("<div class='app-nav'><nav class='tabs'>" + "".join(
        f"<a href='{href}' class='{'on' if key == active else ''}'>{label}</a>" for key, href, label in TABS)
        + "</nav>" + account_controls() + "</div>")


def report_nav_bar() -> bytes:
    """Small bar injected at the top of served report pages, linking back to the app tabs."""
    return ("<div class='report-nav'><nav><a href='/'>← Scans</a>"
            "<a href='/strategies'>Strategies</a><a href='/paper'>Paper Trading</a><a href='/journal'>Journal</a>"
            "<a href='/latest/report.html' aria-current='page'>Latest report</a><a href='/notes'>Trading Notes</a></nav>"
            + theme_control() + "</div>").encode("utf-8")


REPORT_TICKER_VIEWS = r"""<script id="report-ticker-views">
for (const heading of document.querySelectorAll("h2")) {
  if (!heading.textContent.startsWith("Summary — ")) continue;
  const table = heading.nextElementSibling?.querySelector("table");
  if (!table) continue;
  const header = document.createElement("th");
  header.textContent = "View";
  table.tHead.rows[0].append(header);
  for (const row of table.tBodies[0].rows) {
    const anchor = row.cells[0].querySelector("a[href^='#']");
    const section = anchor && document.getElementById(anchor.getAttribute("href").slice(1));
    const symbols = section ? Array.from(section.querySelectorAll("tbody tr"),
      item => item.cells[1].querySelector("a").textContent.trim()) : [];
    const cell = row.insertCell();
    if (!symbols.length) {
      cell.textContent = "—";
      continue;
    }
    const link = document.createElement("a");
    link.textContent = "View";
    link.setAttribute("aria-label", "View ranked tickers for " + anchor.textContent);
    link.href = URL.createObjectURL(new Blob([symbols.join(",") + "\n"],
      {type: "text/plain;charset=utf-8"}));
    link.target = "_blank";
    link.rel = "noopener";
    cell.append(link);
  }
}
</script>"""


def themed_report(body: bytes) -> bytes:
    """Apply the Journal palette when serving both existing and future reports."""
    if b"<body>" not in body:
        return body
    body = body.replace(b"</body>", REPORT_TICKER_VIEWS.encode() + b"</body>", 1)
    palette = _JOURNAL_DESIGN_CSS.split(".journal-app main", 1)[0]
    style = """
:root{color-scheme:light}:root[data-theme=dark]{color-scheme:dark}
.journal-app main{max-width:1280px;padding:28px 30px 60px}
.journal-app .card{border-radius:15px;padding:22px;box-shadow:var(--jshadow)}
.journal-app th{background:var(--bg)}.journal-app tbody tr:hover{background:var(--jsoft)}
.report-note{font-size:11px;margin-left:6px;padding:2px 6px;border:1px solid var(--line);border-radius:5px}
.journal-app h1{letter-spacing:-.7px}.journal-app h2{letter-spacing:-.3px}
.report-nav{max-width:1280px;margin:auto;padding:20px 30px;display:flex;align-items:center;
gap:20px;flex-wrap:wrap;border-bottom:1px solid var(--line)}
.report-nav nav{display:flex;gap:22px;flex-wrap:wrap;flex:1}
.report-nav nav a{font-size:14px;color:var(--muted);font-weight:600}
.report-nav nav a[aria-current=page]{color:var(--accent)}
.report-nav .theme-switch{display:inline-flex;gap:3px;padding:3px;background:var(--card);
border:1px solid var(--line);border-radius:10px}
.report-nav button{font-family:inherit;font-size:12px;font-weight:600;border:0;background:transparent;color:var(--muted);
padding:7px 11px;border-radius:7px;cursor:pointer}
.report-nav button[aria-pressed=true]{background:var(--btn);color:var(--btnink)}
.report-nav button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:640px){.journal-app main{padding:24px 16px 40px}.report-nav{padding:16px;gap:16px}
.report-nav nav{gap:16px}.journal-app .card{padding:16px}}
"""
    body = re.sub(rb"(<a href='https://finviz[.]com/quote[.]ashx[?]t=([A-Z0-9.%\-]+)'[^>]*>[^<]+</a>)",
                  lambda m: m[1] + b" <a class='report-note' href='/notes?new=1&amp;symbol=" + m[2] + b"'>Note</a>", body)
    head = (_THEME_INIT + "<style>" + palette + style + "</style>").encode("utf-8")
    return body.replace(b"</head>", head + b"</head>", 1).replace(
        b"<body>", b"<body class='journal-app'>" + report_nav_bar(), 1)



def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _scan_job_text(app: App, j: dict) -> str:
    s = app.settings
    if j["repeat"] == "once":
        when = dt.datetime.fromisoformat(j["at_utc"]).astimezone(s.tz)
        return f"Once · {when:%a %b %d, %H:%M %Z}"
    nxt = app.schedules.next_fire(j) if app.schedules else None
    return (f"{_cap(sched.days_text(sched.job_days(j)))} at {j['time']}"
            + (f" · next {nxt.astimezone(s.tz):%a %b %d, %H:%M %Z}" if nxt else ""))


def scan_schedule_summary(app: App) -> str:
    """'Every weekday at 12:00, 13:30 · 1 one-time scan' (scan jobs only), or 'None scheduled'."""
    if app.schedules is None:
        return "None scheduled"
    jobs = [j for j in app.schedules.jobs("scan") if j["status"] == "scheduled"]
    groups: Dict[str, List[str]] = {}
    for j in jobs:
        if j["repeat"] != "once":
            groups.setdefault(sched.days_text(sched.job_days(j)), []).append(j["time"])
    parts = [f"{_cap(k)} at {', '.join(sorted(v))}" for k, v in groups.items()]
    once = sum(1 for j in jobs if j["repeat"] == "once")
    if once:
        parts.append(f"{once} one-time scan{'s' if once != 1 else ''}")
    return " · ".join(parts) + f" ({app.settings.tz_name})" if parts else "None scheduled"


def render_scan_card(app: App, now: dt.datetime, running: bool) -> str:
    """Run a scan: Run now / Schedule for later / Every weekday, and the list of scheduled scans."""
    e = html.escape
    s = app.settings
    local_now = now.astimezone(s.tz)
    default_at = local_now + dt.timedelta(minutes=15)
    default_at = default_at.replace(minute=(default_at.minute // 5) * 5, second=0, microsecond=0)
    status = "A scan is running" if running else "Idle"
    out = [f"<div class='card act' id='scan'><div class='shead'><span class='v'>Run a scan</span>"
           f"<span class='muted' style='font-size:13px'>{e(status)}</span></div>", _flash_html(app, "scans"),
           "<form id='scango' method='post' action='/scans/go' class='when' style='margin-top:6px'>"
           "<label>Run schedule<select name='when' id='when'>"
           "<option value='now' selected>Run now</option><option value='once'>Schedule for later</option>"
           "<option value='weekdays'>Every weekday</option></select></label>"
           f"<label id='dl'>Date<input type='date' name='date' id='date' value='{default_at:%Y-%m-%d}' "
           f"min='{local_now:%Y-%m-%d}'></label>"
           f"<label id='tl'>Time<input type='time' name='time' id='time' value='{default_at:%H:%M}' step='60'></label>"
           f"<span class='tz' id='tz'>{e(_tz_label(s.tz, default_at))}</span>"
           f"<span class='go'><button type='submit' id='gobtn'>Run scan</button></span></form>"
           "<div class='muted' id='sum' style='font-size:13px;margin-top:8px'>Each scan covers all your presets "
           "and the built-in FinViz screens.</div>"]
    jobs = app.schedules.jobs() if app.schedules else []
    scans = [j for j in jobs if sched.job_kind(j) == "scan" and j["status"] in ("scheduled", "missed")]
    scans.sort(key=lambda j: (j["status"] != "scheduled", app.schedules.next_fire(j) or now))
    out.append("<h3>Scheduled scans</h3>")
    if not scans:
        out.append("<p class='muted' style='margin:4px 0'>None. Pick Schedule for later or Every weekday above.</p>")
    for j in scans:
        label = "Cancel" if j["status"] == "scheduled" else "Dismiss"
        extra = " · <span class='bad'>missed (server was offline)</span>" if j["status"] == "missed" else ""
        hist = j.get("history") or []
        last = (f"<div class='muted' style='font-size:13px'>Last: {e(_fmt_short(hist[-1].get('at_utc'), s.tz))} · "
                f"{e(str(hist[-1].get('result', '')))}</div>") if hist else ""
        out.append(f"<div class='line'><span>{e(_scan_job_text(app, j))}{extra}{last}</span>"
                   "<form method='post' action='/scans/schedule/cancel' style='margin:0'>"
                   f"<input type='hidden' name='id' value='{e(j['id'], quote=True)}'>"
                   f"<button type='submit' class='ghost'>{label}</button></form></div>")
    fresh = [j for j in jobs if sched.job_kind(j) == "strategies" and j.get("scan") == TICKER_FRESH
             and j["status"] == "scheduled"]
    if fresh:
        out.append(f"<p class='muted' style='font-size:13px;margin:8px 0 0'>Also: {len(fresh)} strategy schedule"
                   f"{'s' if len(fresh) != 1 else ''} with “New scan at run time” start a scan first "
                   "(manage on the <a href='/strategies#activity'>Strategies</a> tab).</p>")
    out.append("</div>")
    return "".join(out)


_SCAN_JS = r"""
var f=document.getElementById('scango');if(!f)return;var dirty=false,running=__RUNNING__;
var when=document.getElementById('when'),dl=document.getElementById('dl'),tl=document.getElementById('tl'),
tz=document.getElementById('tz'),date=document.getElementById('date'),time=document.getElementById('time'),
btn=document.getElementById('gobtn'),sum=document.getElementById('sum');
function tzLabel(){try{var d=new Date((date.value||'2000-01-01')+'T'+(time.value||'12:00')+':00Z');
var p=new Intl.DateTimeFormat('en-US',{timeZone:__TZ__,timeZoneName:'short'}).formatToParts(d);
var n=p.filter(function(x){return x.type==='timeZoneName'})[0];var lo=new Intl.DateTimeFormat('en-US',
{timeZone:__TZ__,timeZoneName:'longOffset'}).formatToParts(d).filter(function(x){return x.type==='timeZoneName'})[0];
return (n?n.value:'')+(lo?' · '+lo.value.replace('GMT','UTC').replace('-','−'):'');}catch(e){return tz.textContent;}}
function upd(){var w=when.value;dl.style.display=w==='once'?'':'none';tl.style.display=w==='now'?'none':'';
tz.style.display=w==='now'?'none':'';tz.textContent=tzLabel();var z=tz.textContent.split(' ·')[0];
btn.textContent=w==='now'?(running?'Scan running…':'Run scan now'):'Schedule scan';btn.disabled=w==='now'&&running;
sum.textContent=w==='now'?'Scans all your presets and the built-in FinViz screens now.':w==='once'?
'One scan on '+date.value+' at '+time.value+' '+z+'.':'A scan every weekday (Mon–Fri) at '+time.value+' '+z+'.';}
[when,date,time].forEach(function(x){['input','change'].forEach(function(ev){x.addEventListener(ev,
function(){dirty=true;upd()})})});
upd();if(running)setTimeout(function(){if(!dirty)location.reload()},10000);
"""


_DASHBOARD_THEME_CSS = """
.dashboard-app h1{font-size:30px;letter-spacing:-.8px;margin-bottom:8px}
.dashboard-app .card{margin:18px 0}.dashboard-app .card.act{border-top:3px solid var(--accent)}
.dashboard-app th{background:var(--bg)}.dashboard-app tbody tr:hover{background:var(--jsoft)}
.dashboard-app .tabs a.on{color:var(--accent)}
.dashboard-app button:focus-visible,.dashboard-app a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.dashboard-app .tick{background:var(--bg)}
@media(max-width:640px){.dashboard-app h1{font-size:25px}.dashboard-app .card{padding:16px}}
"""


def render_notebook(app, submitted=None, error="", saved=False):
    try:
        data = app.notebook.read() if submitted is None else dict(text=submitted.get("text",""), revision=submitted.get("revision",""))
        content = nb.content(data, error, saved)
    except nb.NotebookError as err:
        content = "<div class='warn' role='alert'>" + html.escape(str(err)) + "</div>"
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Notes · Signal desk</title>" + _THEME_INIT
            + f"<style>{_CSS}{_JOURNAL_DESIGN_CSS}</style></head><body class='journal-app'><main>"
            + render_tabs("notebook") + content + "</main></body></html>")


def render_notes(app, query, submitted=None, error=""):
    try:
        notes = app.notes.list()
    except tn.NoteError as err:
        return ("<!doctype html><html><head><meta charset='utf-8'><title>Trading Notes</title>" + _THEME_INIT
                + f"<style>{_CSS}{_JOURNAL_DESIGN_CSS}</style></head><body class='journal-app'><main>"
                + render_tabs("notes") + "<div class='warn' role='alert'>" + html.escape(str(err)) + "</div></main></body></html>")
    today = app.clock().astimezone(app.settings.tz).date().isoformat()
    content = tn.render_content(notes, query, today, submitted, error)
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Trading Notes · Signal desk</title>" + _THEME_INIT
            + f"<style>{_CSS}{_JOURNAL_DESIGN_CSS}</style></head><body class='journal-app'><main>"
            + render_tabs("notes") + content + "</main></body></html>")


def render_dashboard(app: App, notice: str = "") -> str:
    e = html.escape
    s = app.settings
    now = app.clock()
    nxt = app.schedules.next_scan() if app.schedules else None
    last = app.state.get("last_run") or {}
    running = app.runner.running
    runs = list_runs(s)
    out: List[str] = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>FinViz Agent</title>",
        "<noscript><meta http-equiv='refresh' content='10'></noscript>" if running else "",
        _THEME_INIT + f"<style>{_CSS}{_STRAT_CSS}{_JOURNAL_DESIGN_CSS}{_DASHBOARD_THEME_CSS}</style></head><body class='journal-app dashboard-app'><main>",
        render_tabs("scans"),
        "<h1>FinViz screener gainers</h1>",
        f"<div class='muted'>Scheduled scans: {e(scan_schedule_summary(app))}</div>",
    ]
    if notice:
        out.append(f"<div class='warn'>{e(notice)}</div>")
    if not s.token_present:
        out.append("<div class='warn'>FINVIZ_API_TOKEN is not set, so runs will fail. Add it in Railway → "
                   "Variables.</div>")
    if app.config_error:
        out.append(f"<div class='warn'>Configuration problem:\n{e(app.config_error)}</div>")

    if running and app.runner.current:
        last_html = (f"<span class='v'>Running now…</span><div class='muted'>started "
                     f"{e(_fmt_local(app.runner.current.get('started_utc'), s.tz))}</div>")
    elif last:
        cls = "ok" if last.get("exit_code") == 0 else "bad"
        log_link = (f" · <a href='/logs/{urllib.parse.quote(last['log'])}'>log</a>"
                    if last.get("log") and LOG_RE.match(last["log"]) else "")
        last_html = (f"<span class='v {cls}'>{e(str(last.get('status', '')))}</span>"
                     f"<div class='muted'>{e(_fmt_local(last.get('finished_utc'), s.tz))} "
                     f"({e(str(last.get('reason', '')))}){log_link}</div>")
    else:
        last_html = "<span class='v'>No runs yet</span>"
    nxt_text = f"{nxt[0].astimezone(s.tz):%a %b %d, %H:%M %Z}" if nxt else "None scheduled"
    out.append(
        "<div class='card'><div class='grid'>"
        f"<div><div class='k'>Next scan</div><div class='v'>{e(nxt_text)}</div></div>"
        f"<div><div class='k'>Last run</div>{last_html}</div>"
        f"<div><div class='k'>Screens</div><div class='v'>{app.screen_count}</div></div>"
        "</div></div>"
    )
    out.append(render_scan_card(app, now, running))

    latest = s.reports_dir / "latest_tickers.txt"
    if latest.is_file():
        lines = latest.read_text(encoding="utf-8").splitlines()
        head = lines[0] if lines else ""
        tick = lines[1] if len(lines) > 1 else ""
        out.append(
            "<h2>Latest tickers</h2><div class='card'>"
            f"<div class='muted'>{e(head)} · {len([t for t in tick.split(',') if t])} tickers</div>"
            f"<div class='tick' id='tk'>{e(tick) or '<span class=muted>(none)</span>'}</div>"
            "<div class='row'><button type='button' id='cp'>Copy tickers</button>"
            "<a href='/latest/tickers.txt'>tickers.txt</a><a href='/latest/report.html'>latest report</a>"
            "<span id='cpmsg' class='muted'></span></div></div>"
        )

    out.append(render_presets(app))

    out.append("<h2>All runs</h2>")
    if not runs:
        out.append("<p class='muted'>No runs yet. They will appear here after the next scheduled run, "
                   "or use Run a scan above.</p>")
    current_day = None
    for r in runs:
        day = r.when.date()
        if day != current_day:
            if current_day is not None:
                out.append("</tbody></table></div>")
            current_day = day
            out.append(f"<div class='card wrap'><div class='v'>{e(f'{r.when:%A, %B %d, %Y}')}</div>"
                       "<table><thead><tr><th>Time</th><th class='n'>Tickers</th><th>Files</th>"
                       "</tr></thead><tbody>")
        labels = {"report.html": "Report", "tickers.txt": "Tickers", "top_gainers.csv": "CSV",
                  "summary.json": "JSON"}
        links = "".join(f"<a href='/runs/{r.stamp}/{f}'>{labels[f]}</a>" for f in RUN_FILES if f in r.files)
        count = "—" if r.tickers is None else str(len(r.tickers))
        out.append(f"<tr><td>{e(f'{r.when:%H:%M:%S %Z}')}</td><td class='n'>{count}</td>"
                   f"<td class='links'>{links}</td></tr>")
    if current_day is not None:
        out.append("</tbody></table></div>")

    logs = recent_logs(s)
    if logs:
        out.append("<h2>Recent logs</h2><div class='card'>"
                   + "<br>".join(f"<a href='/logs/{urllib.parse.quote(n)}'>{e(n)}</a>" for n in logs)
                   + "</div>")
    out.append(
        "<p class='muted'>Information only, not investment advice.</p></main>"
        "<script>(function(){var b=document.getElementById('cp');if(!b)return;b.onclick=function(){"
        "var t=document.getElementById('tk').textContent,m=document.getElementById('cpmsg');"
        "if(navigator.clipboard){navigator.clipboard.writeText(t).then(function(){m.textContent='Copied';},"
        "function(){m.textContent='Select and copy manually';});}else{m.textContent='Select and copy manually';}"
        "};})();</script>"
        "<script>(function(){" + _SCAN_JS.replace("__TZ__", json.dumps(s.tz_name))
        .replace("__RUNNING__", "true" if running else "false") + "})();</script></body></html>"
    )
    return "\n".join(x for x in out if x)


def parse_multipart(content_type: str, body: bytes) -> Dict[str, Tuple[Optional[str], bytes]]:
    """Parse multipart/form-data (file uploads) with the standard library's email parser."""
    if not content_type.lower().startswith("multipart/form-data") or "boundary=" not in content_type:
        raise ValueError("Upload the file using the form on the page.")
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    if not msg.is_multipart():
        raise ValueError("The upload could not be read. Try again.")
    out: Dict[str, Tuple[Optional[str], bytes]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            out[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return out


def _flash_html(app: App, section: str) -> str:
    f = app.pop_flash(section)
    if not f:
        return ""
    return (f"<div class='{'okbox' if f[0] == 'ok' else 'warn'}' role='status'>{html.escape(f[1])}</div>")


def _stamp_label(stamp: str) -> str:
    """'2026-10-25_120003' -> 'Oct 25, 12:00'."""
    try:
        return f"{dt.datetime.strptime(stamp, '%Y-%m-%d_%H%M%S'):%b %d, %H:%M}"
    except ValueError:
        return stamp


def _scan_label(r: "RunInfo") -> str:
    n = "?" if r.tickers is None else len(r.tickers)
    return f"{r.when:%a %b %d, %H:%M %Z} · {n} tickers"


SIGNAL_TXT = "signal_tickers.txt"
BUY_STATUSES = {"BUY", "BUY SIGNAL", "SIGNAL"}  # custom (fmfi), V5-style, V5 dashboard


def signal_tickers(d: Path) -> List[str]:
    """The tickers a result signalled, in table order, once each.
    With a Status column: only buy rows (V5 'BLOCKED BY REGIME' is not a buy). Without one
    (e.g. a whole-script strategy whose own CSV lists only its picks): every row's ticker."""
    fields, rows = sm.read_csv(d / "signals.csv", d)
    has_status = "Status" in fields
    out: List[str] = []
    for r in rows:
        t = (r.get("Ticker") or "").strip()
        if t and t not in out and (not has_status or (r.get("Status") or "").strip() in BUY_STATUSES):
            out.append(t)
    return out


def _counts_text(counts: Dict[str, int]) -> str:
    if not counts:
        return "—"
    if "SIGNAL" in counts or "NO SCANNER FIRED" in counts or "FILTERED OUT" in counts:
        return f"{counts.get('SIGNAL', 0)} signal{'' if counts.get('SIGNAL', 0) == 1 else 's'} of {sum(counts.values())}"
    if any(k in counts for k in ("BUY SIGNAL", "BUY", "NO SIGNAL", "BLOCKED BY REGIME")):
        # "BUY SIGNAL" from V5-style strategies, "BUY" from custom ones (e.g. fmfi_scanner)
        parts = [f"{counts.get('BUY SIGNAL', 0) + counts.get('BUY', 0)} buy"]
        if counts.get("BLOCKED BY REGIME"):
            parts.append(f"{counts['BLOCKED BY REGIME']} blocked")
        return " · ".join(parts)
    return f"{sum(counts.values())} rows"


_STRAT_CSS = """
.slist{padding:0;overflow:hidden}.slist .shead{padding:14px 16px;border-bottom:1px solid var(--line);align-items:center}
.srow{display:flex;gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);cursor:pointer}
.srow:last-of-type{border-bottom:0}.srow.on{background:color-mix(in srgb,var(--accent) 9%,transparent)}
.srow input{width:18px;height:18px;accent-color:var(--accent);flex:none}.srow .nm{flex:1;min-width:0}
.srow .t1{font-weight:600}.srow .t2{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.selbar{padding:14px 16px;border-top:1px solid var(--line)}.selbar .cnt{font-weight:600}
.when{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin-top:12px;padding-top:14px;
border-top:1px solid var(--line)}.when label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}
.when input{font:inherit;font-size:14px;color:var(--ink);background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:7px 8px}.when .go{margin-left:auto}.when .tz{font-size:13px;color:var(--muted);padding-bottom:9px}
.when .times{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}
#tlist{display:flex;flex-wrap:wrap;gap:6px}.tslot{display:inline-flex;align-items:center;gap:2px}
.tdel{background:transparent;color:var(--muted);border:0;padding:4px 6px;font-size:16px;line-height:1;font-weight:400}
.tdel:hover{color:var(--down)}.tadd{background:transparent;color:var(--accent);border:1px solid var(--line);
padding:7px 10px;font-weight:600}
.top2{display:grid;grid-template-columns:1fr 1fr auto auto;gap:14px;align-items:end}
.cpick{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}
.cpick .row{display:flex;gap:6px;flex-wrap:nowrap}.top2 .cpick select{width:auto}
.cpick input{width:72px;font:inherit;font-size:14px;color:var(--ink);background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:7px 8px}.chint{grid-column:1/-1;font-size:12.5px;color:var(--muted);margin-top:-4px}.top2 label{display:flex;
flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}.top2 select{width:100%}
.run-filters{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;
border:0;border-top:1px solid var(--line);padding:12px 0 0;margin:0;min-width:0}
.run-filters legend{font-size:13px;padding-right:8px}.run-filters input{width:100%;min-width:0;box-sizing:border-box;font:inherit;color:var(--ink);
background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:7px 8px}
.run-filters small{grid-column:1/-1;color:var(--muted)}
.under{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center;margin:12px 0}
.under label{display:flex;gap:8px;align-items:center;font-size:14px}.under input[type=checkbox]{width:18px;height:18px;
accent-color:var(--accent)}.btn2{background:transparent;color:var(--ink);border:1px solid var(--line)}
.act h3{font-size:14px;margin:14px 0 6px;color:var(--muted);font-weight:600}.act .line{display:flex;gap:10px;
justify-content:space-between;align-items:center;flex-wrap:wrap;padding:6px 0;border-bottom:1px solid var(--line)}
.vis{position:absolute;left:-9999px}
.tvlink{border:1px solid var(--accent);color:var(--accent);border-radius:8px;padding:5px 10px;font-weight:600;
font-size:13px;white-space:nowrap}.tvlink:hover{text-decoration:none;background:var(--bg)}
button.tvedit{color:var(--accent);padding:5px 9px;font-size:13px}button.tvedit.add{font-weight:600}
.tvdlg{border:1px solid var(--line);border-radius:12px;padding:20px;max-width:520px;width:calc(100% - 32px);
background:var(--card);color:var(--ink)}.tvdlg::backdrop{background:rgba(0,0,0,.35)}.tvdlg h3{margin:0 0 4px}
.tvdlg label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:var(--muted);margin-top:10px}
.tvdlg input{font:inherit;font-size:14px;color:var(--ink);background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:9px 10px}.tvhint{font-size:13px}.tvbtns{display:flex;justify-content:flex-end;gap:10px}
#tvcancel{color:var(--ink)}
@media (max-width:640px){.top2{grid-template-columns:1fr}.when .go{margin-left:0;width:100%}.when .go button{width:100%}}
"""

TICKER_FRESH = "fresh"


_ACRONYMS = {"fmfi", "rsi", "macd", "ema", "sma", "atr", "vwap", "adx", "obv", "roc", "mfi", "cci", "ma", "bb", "spy",
             "etf", "eps", "pe", "v1", "v2", "v3", "v4", "v5", "v6"}


def _pretty_name(name: str) -> str:
    words = re.sub(r"[_\-.]+", " ", name).split()
    if not words:
        return name
    out = [w.upper() if w.lower() in _ACRONYMS else w for w in words]
    return " ".join([out[0][:1].upper() + out[0][1:]] + out[1:])


def _fmt_short(iso_utc: Optional[str], tz) -> str:
    try:
        return f"{dt.datetime.fromisoformat(iso_utc).astimezone(tz):%b %d, %H:%M}"
    except (TypeError, ValueError):
        return "—"


def _tz_label(tz, when: dt.datetime) -> str:
    off = when.astimezone(tz).utcoffset() or dt.timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "−"
    return f"{when.astimezone(tz):%Z} · UTC{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def _job_text(app: App, j: dict) -> str:
    s = app.settings
    n = len(j["names"])
    who = f"{n} strateg{'y' if n == 1 else 'ies'}"
    src = {"latest": "latest scan", TICKER_FRESH: "new scan first"}.get(j["scan"], f"scan {_stamp_label(j['scan'])}")
    basis = ("intraday" if j["mode"] == "intraday" else "latest close") + (
        f" · {sm.candles_label(j['candles'])} candles" if j.get("candles", "1d") != "1d" else "")
    if j["repeat"] == "once":
        when = dt.datetime.fromisoformat(j["at_utc"]).astimezone(s.tz)
        timing = f"{when:%a %b %d, %H:%M %Z}"
    else:
        nxt = app.schedules.next_fire(j)
        timing = f"every weekday at {j['time']}" + (f" · next {nxt.astimezone(s.tz):%a %b %d, %H:%M %Z}" if nxt else "")
    filters = j.get("filters", {})
    bounds = [f"{label} {filters[key]:g}" for key, label in sm.UNIVERSE_FIELDS if key in filters]
    return " · ".join([who, timing, src, basis, *bounds])


def _tv_controls(name: str, m: dict) -> str:
    """'TV ↗' (opens the saved TradingView link in a new tab and copies it, for TradingView Desktop's
    'Open link from clipboard') plus an edit button; '+ TV link' when none is saved yet."""
    e = html.escape
    url = m.get("tv_url") or ""
    label = _pretty_name(name)
    edit = (f"<button type='button' class='ghost tvedit{'' if url else ' add'}' hidden "
            f"data-name='{e(name, quote=True)}' data-label='{e(label, quote=True)}' data-url='{e(url, quote=True)}' "
            f"aria-label='{'Edit' if url else 'Add'} TradingView link for {e(label, quote=True)}'>"
            f"{'✎' if url else '+ TV link'}</button>")
    if not url:
        return edit
    return (f"<a class='tvlink' href='{e(url, quote=True)}' target='_blank' rel='noopener noreferrer' "
            f"title='Open in TradingView (link also copied for TradingView Desktop: Open link from clipboard)'>"
            f"TV ↗</a>" + edit)


_STRATEGIES_THEME_CSS = """
.strategies-app .card.slist{padding:0}
.strategies-app button.strategy-note{color:var(--accent)}
.strategies-app button.strategy-note,.strategies-app button.del{display:inline-flex;align-items:center;justify-content:center;min-width:34px;min-height:34px;padding:7px}
.strategies-app button[hidden]{display:none}
#strategy-note-text{box-sizing:border-box;display:block;width:100%;max-width:100%;margin-top:8px;font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;resize:vertical}
@media(pointer:coarse){.strategies-app button.strategy-note,.strategies-app button.del{min-width:44px;min-height:44px}}
.strategies-app .warn{overflow-wrap:anywhere}
.strategies-app .srow.on{background:var(--jsoft)}
.strategies-app input[type=checkbox],.strategies-app input[type=radio]{accent-color:var(--accent)}
.strategies-app .srow:hover{background:var(--jsoft)}
"""


def _cancel_strategy_button(ident, label="Stop", disabled=False):
    if not ident:
        return ""
    return ("<form method='post' action='/strategies/cancel' style='display:inline-block;margin:0 0 0 8px'>"
            "<input type='hidden' name='id' value='" + html.escape(ident, quote=True) + "'>"
            "<button type='submit' class='ghost'" + (" disabled" if disabled else "") + ">" + label + "</button></form>")


def _delete_run_button(rid):
    return ("<form method='post' action='/strategies/results/delete' style='display:inline-block;margin:0 0 0 8px'>"
            "<input type='hidden' name='id' value='" + html.escape(rid, quote=True) + "'>"
            "<button type='submit' class='ghost delete-run' aria-label='Delete run " + html.escape(rid, quote=True)
            + "' title='Delete run history (OTP required)' style='display:inline-flex;align-items:center;padding:6px'>"
            "<svg width='17' height='17' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' aria-hidden='true'>"
            "<path d='M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'/></svg></button></form>")


def render_strategies(app: App) -> str:
    e = html.escape
    s, st = app.settings, app.strategies
    now = app.clock()
    busy = bool(st.current or st.queued)
    runs = [r for r in list_runs(s) if r.tickers is not None][:30]
    strategies = st.list()
    names = [n for n, _ in strategies]
    sel = st.selection()
    chosen = set(sel.get("names") or names)  # first visit: everything selected
    chosen &= set(names)
    sel_scan, sel_mode, sel_when = sel.get("scan", "latest"), sel.get("mode", "close"), sel.get("when", "now")
    auto = st.auto_config()
    local_now = now.astimezone(s.tz)
    default_at = (local_now + dt.timedelta(minutes=15))
    default_at = default_at.replace(minute=(default_at.minute // 5) * 5, second=0, microsecond=0)

    out: List[str] = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>Strategies</title>",
        _THEME_INIT + f"<style>{_CSS}{_STRAT_CSS}{_JOURNAL_DESIGN_CSS}{_DASHBOARD_THEME_CSS}{_STRATEGIES_THEME_CSS}</style></head><body class='journal-app dashboard-app strategies-app'><main>", render_tabs("strategies"),
        "<h1>Strategies</h1>",
        "<div class='muted'>Select strategies, then run them together on the same scan.</div>",
        "<p><a href='/strategies/archive'>Recycle bin &amp; version history →</a></p>",
    ]
    if os.environ.get("VBT_SERVICE_URL") and os.environ.get("VBT_SERVICE_TOKEN"):
        out.append("<p><a href='/strategies/backtests'>VectorBT PRO backtest history</a></p>")

    if st.price_source == "massive" and st.client is None:
        out.append("<div class='warn'>MASSIVE_API_KEY is not set, so there are no prices to run strategies on.</div>")
    if st.price_source == "finviz" and st.finviz is None:
        out.append("<div class='warn'>FINVIZ_API_TOKEN is not set, so FinViz prices are unavailable.</div>")
    if st.sandbox_error:
        out.append(f"<div class='warn'>Strategies can't run on this server: {e(st.sandbox_error)}</div>")
    out.append(_flash_html(app, "strategies"))

    latest_label = f"Latest scan · {_stamp_label(runs[0].stamp)}" if runs else "Latest scan (none yet)"
    scan_opts = [("latest", latest_label), (TICKER_FRESH, "New scan at run time")] + [
        (r.stamp, _scan_label(r)) for r in runs]
    if sel_scan not in {v for v, _ in scan_opts}:
        sel_scan = "latest"
    opt = lambda pairs, cur: "".join(  # noqa: E731
        f"<option value='{e(v, quote=True)}'{' selected' if v == cur else ''}>{e(t)}</option>" for v, t in pairs)

    saved_filters = sel.get("filters", {})
    filter_fields = "".join(
        f"<label>{e(label)}<input name='{key}' type='number' min='0' step='any' "
        f"placeholder='Any' value='{e(str(saved_filters.get(key, '')), quote=True)}'></label>"
        for key, label in sm.UNIVERSE_FIELDS
    )
    out.append("<form id='go' method='post' action='/strategies/go'>")
    out.append(
        "<div class='card top2'>"
        f"<label>Ticker source<select name='scan'>{opt(scan_opts, sel_scan)}</select></label>"
        "<label>Price basis<select name='mode'>"
        + opt([("close", "Latest close"), ("intraday", "Include today's intraday bar")], sel_mode)
        + "</select></label>"
        + _candles_picker(sel.get("candles", "1d"), [n for n, m in strategies if m.get("candles") is True])
        + "<div><div class='muted' style='font-size:13px'>Price data</div><div>"
        + ("FinViz Elite" if st.price_source == "finviz" else "Massive") + "</div></div>"
        + _candles_hint([n for n, m in strategies if m.get("candles") is True])
        + "<fieldset class='run-filters'><legend>Optional ticker filters</legend>" + filter_fields
        + "<small>Uses values from the selected scan. Leave a bound blank for no limit. "
          "Market cap is in millions (1,000 = $1 billion). Missing values are excluded when a filter is set.</small>"
          "</fieldset></div>")

    out.append("<div class='card slist'><div class='shead'><div><span class='v'>Your strategies</span> "
               f"<span class='muted'>{len(strategies)}</span></div>"
               + ("<label class='inline' style='display:flex;gap:8px;align-items:center'>"
                  "<input type='checkbox' id='all' style='width:18px;height:18px;accent-color:var(--accent)'>"
                  " Select all</label>" if strategies else "") + "</div>")
    if not strategies:
        out.append("<p class='muted' style='padding:0 16px'>No strategies yet. Use Upload strategy below.</p>")
    for name, m in strategies:
        kind = {"v5": "V5-style", "script": "Script"}.get(m.get("kind"), "Custom")
        on = name in chosen
        out.append(
            f"<label class='srow{' on' if on else ''}'>"
            f"<input type='checkbox' name='s' value='{e(name, quote=True)}'{' checked' if on else ''}>"
            f"<span class='nm'><div class='t1'>{e(_pretty_name(name))}</div><div class='t2'>{e(m['file'])}</div></span>"
            + ("<span class='tag'>auto</span>" if name in auto["names"] else "")
            + f"<span class='tag'>{kind}</span>"
            + _tv_controls(name, m)
            + f"<button type='button' class='ghost strategy-note' hidden data-name='{e(name, quote=True)}' "
              f"data-label='{e(_pretty_name(name), quote=True)}' data-note='{e(m.get('note', ''), quote=True)}' "
              f"aria-label='Edit note for {e(_pretty_name(name), quote=True)}' title='Strategy note'>"
              "<svg width='17' height='17' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' aria-hidden='true'>"
              "<path d='M14 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-9M16 3l5 5-9 9-5 1 1-5Z'/></svg></button>"
            # type=button (not submit) so pressing Enter in the run form can never trigger a delete;
            # the page script confirms, then posts the separate delete form below. Hidden until it runs.
            + f"<button type='button' class='ghost del' hidden data-name='{e(name, quote=True)}' "
              f"data-label='{e(_pretty_name(name), quote=True)}' aria-label='Delete {e(_pretty_name(name), quote=True)}'>"
              "<svg width='17' height='17' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' aria-hidden='true'>"
              "<path d='M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'/></svg></button></label>")
    if strategies:
        out.append(
            f"<div class='selbar'><div class='cnt' id='cnt'>{len(chosen)} of {len(strategies)} selected</div>"
            "<div class='muted' style='font-size:13px'>Shared scan and prices apply to every selection.</div>"
            "<div class='when'>"
            "<label>Run schedule<select name='when' id='when'>"
            + opt([("now", "Run now"), ("once", "Schedule for later"), ("weekdays", "Every weekday")], sel_when)
            + "</select></label>"
            f"<label id='dl'>Date<input type='date' name='date' id='date' value='{default_at:%Y-%m-%d}' "
            f"min='{local_now:%Y-%m-%d}'></label>"
            # one or more times: each becomes its own scheduled run (cancel any of them on its own)
            "<div id='tl' class='times'><span>Time</span><div id='tlist'>"
            f"<span class='tslot'><input type='time' name='time' id='time' value='{default_at:%H:%M}' step='60' "
            "aria-label='Time 1'><button type='button' class='tdel' hidden aria-label='Remove this time'>×</button>"
            "</span></div></div>"
            "<button type='button' id='addtime' class='btn2 tadd' hidden>+ Add time</button>"
            f"<span class='tz' id='tz'>{e(_tz_label(s.tz, default_at))}</span>"
            "<span class='go'><button type='submit' id='gobtn'>Run strategies</button></span></div>"
            "<div class='muted' id='sum' style='font-size:13px;margin-top:8px'></div></div>")
    out.append("</div>")
    out.append("<div class='under'>"
               "<label><input type='checkbox' name='auto' id='auto'"
               + (" checked" if auto["names"] else "") + "> Run selected strategies after each new scan</label>"
               "<button type='submit' class='vis' id='autobtn' formaction='/strategies/autoset'>Save</button>"
               "<label class='btn2' style='border:1px solid var(--line);border-radius:8px;padding:7px 12px;"
               "cursor:pointer;font-weight:600' for='upfile'>Upload strategy</label></div>")
    if auto["names"]:
        out.append(f"<div class='muted' style='font-size:13px;margin:-6px 0 10px'>After each new scan: "
                   f"{e(', '.join(_pretty_name(n) for n in auto['names']))} "
                   f"({'intraday' if auto['mode'] == 'intraday' else 'latest close'}"
                   + (f", {e(sm.candles_label(auto['candles']))} candles" if auto.get("candles", "1d") != "1d" else "")
                   + ")</div>")
    out.append("</form>")
    out.append("<dialog id='tvdlg' class='tvdlg' aria-labelledby='tvdlg-h'><form method='post' "
               "action='/strategies/tvlink' id='tvform'><h3 id='tvdlg-h'>TradingView link</h3>"
               "<p class='muted' id='tvdlg-for'></p><input type='hidden' name='name' id='tvname'>"
               "<label>Link<input type='url' name='url' id='tvurl' maxlength='1000' "
               "placeholder='https://www.tradingview.com/chart/…' autocomplete='off'></label>"
               "<p class='muted tvhint'>Paste a chart, layout or script link from TradingView. Leave it empty and "
               "Save to remove the link.</p><div class='tvbtns'><button type='button' class='ghost' id='tvcancel'>"
               "Cancel</button><button type='submit'>Save</button></div></form></dialog>")
    out.append("<dialog id='strategy-note-dialog' class='tvdlg' aria-labelledby='strategy-note-heading'>"
               "<form method='post' action='/strategies/note'><h3 id='strategy-note-heading'>Strategy note</h3>"
               "<p class='muted' id='strategy-note-label'></p><input type='hidden' name='name' id='strategy-note-name'>"
               "<label for='strategy-note-text'>Your note</label><textarea name='note' id='strategy-note-text' "
               "maxlength='1000' rows='7' placeholder='Add your strategy rules, reminders, or observations…'></textarea>"
               "<p class='muted' id='strategy-note-count' aria-live='polite'>0 / 1000 characters</p>"
               "<div class='tvbtns'><button type='button' class='ghost' id='strategy-note-cancel'>Cancel</button>"
               "<button type='submit'>Save note</button></div></form></dialog>")
    out.append("<form id='delform' method='post' action='/strategies/delete' class='vis'>"
               "<input type='hidden' name='name' id='delname'></form>")
    out.append("<form id='up' method='post' action='/strategies/upload' enctype='multipart/form-data' class='vis'>"
               "<input type='file' name='file' id='upfile' accept='.py'><button type='submit'>Upload</button></form>"
               "<noscript><form method='post' action='/strategies/upload' enctype='multipart/form-data' class='forms'>"
               "<input type='file' name='file' accept='.py' required><button type='submit'>Upload strategy</button>"
               "</form></noscript>")

    # ── run activity ──
    cur, queued = st.current, list(st.queued)
    jobs = app.schedules.jobs("strategies") if app.schedules else []
    upcoming = [j for j in jobs if j["status"] == "scheduled"]
    missed = [j for j in jobs if j["status"] == "missed"]
    status = (f"Running {_pretty_name(cur['name'])}" + (f" · {len(queued)} queued" if queued else "")
              if cur else (f"{len(queued)} queued" if queued else "Idle · no strategies queued"))
    out.append(f"<div class='card act' id='activity'><div class='shead'><span class='v'>Run activity</span>"
               f"<span class='muted' style='font-size:13px'>{e(status)}</span></div>" + _flash_html(app, "results"))
    if cur or queued:
        out.append("<h3>Now</h3>")
        if cur:
            out.append(f"<div class='line'><span>{e(_pretty_name(cur['name']))} · running</span>"
                       f"<span class='muted'>{e(_fmt_local(cur.get('started_utc'), s.tz))}</span>"
                       + _cancel_strategy_button(cur.get('job_id', ''), 'Stopping…' if cur.get('stopping') else 'Stop', bool(cur.get('stopping'))) + "</div>")
        for q in queued:
            out.append(f"<div class='line'><span>{e(_pretty_name(q['name']))} · queued ({e(q['reason'])})</span></div>")
    if upcoming or missed:
        out.append("<h3>Scheduled</h3>")
        for j in upcoming + missed:
            label = "Cancel" if j["status"] == "scheduled" else "Dismiss"
            extra = " · <span class='bad'>missed (server was offline)</span>" if j["status"] == "missed" else ""
            names_txt = ", ".join(_pretty_name(n) for n in j["names"])
            out.append(
                f"<div class='line'><span>{e(_job_text(app, j))}{extra}<div class='muted' style='font-size:13px'>"
                f"{e(names_txt)}</div></span>"
                "<form method='post' action='/strategies/schedule/cancel' style='margin:0'>"
                f"<input type='hidden' name='id' value='{e(j['id'], quote=True)}'>"
                f"<button type='submit' class='ghost'>{label}</button></form></div>")
    results = st.results(50)
    if results:
        out.append("<h3>Results</h3><div class='wrap'><table><thead><tr><th>Started</th><th>Strategy</th><th>Scan</th>"
                   "<th>As of</th><th>Prices</th><th>Status</th><th>Signals</th><th></th></tr></thead><tbody>")
        for m in results:
            rid, status_r = m.get("id", ""), m.get("status", "")
            cls = "ok" if status_r == "ok" else ("bad" if status_r == "error" else "")
            scan = m.get("scan", "")
            scan_html = (f"<a href='/runs/{e(scan)}/report.html'>{e(_stamp_label(scan))}</a>"
                         if STAMP_RE.match(scan or "") else "—")
            src = {"finviz": "FinViz ", "massive": "Massive "}.get(m.get("price_source", ""), "")
            out.append(
                f"<tr><td style='white-space:nowrap'>{e(_fmt_short(m.get('started_utc'), s.tz))}</td>"
                f"<td>{e(_pretty_name(m.get('strategy', '')))}<div class='muted' style='font-size:12px'>"
                f"{e(m.get('reason', ''))}</div></td><td style='white-space:nowrap'>{scan_html}</td>"
                f"<td style='white-space:nowrap'>{e(m.get('asof', '—'))}</td>"
                f"<td>{e(src + ('intraday' if m.get('mode') == 'intraday' else 'close'))}"
                + (f"<div class='muted' style='font-size:12px'>{e(_candles_text(m.get('candles')))} candles</div>"
                   if (m.get('candles') or '1d') != '1d' else "") + "</td>"
                f"<td class='{cls}'>{e(status_r)}</td><td>{e(_counts_text(m.get('counts', {})))}</td>"
                f"<td class='links'><a href='/strategies/results/{e(rid)}/'>View</a>"
                f"<a href='/strategies/results/{e(rid)}/signals.csv'>CSV</a>"
                f"<a href='/strategies/results/{e(rid)}/{SIGNAL_TXT}' title='Signal tickers, comma-separated'>"
                "TXT</a>" + (_cancel_strategy_button(rid, "Stop") if status_r == "running" else _delete_run_button(rid)) + "</td></tr>")
        out.append("</tbody></table></div>")
    elif not (cur or queued or upcoming or missed):
        out.append("<p class='muted'>Your selected strategies will appear here when you start a run.</p>")
    out.append("</div>")

    # ── data & configuration ──
    days = st.prices.trading_days()
    ps = st.prices.status
    data_flash = _flash_html(app, "data")
    out.append(f"<details id='data' class='card'{' open' if data_flash else ''}><summary class='v' "
               "style='color:var(--ink)'>Data &amp; configuration</summary>" + data_flash)
    out.append(
        "<div class='card'><div class='v'>Daily prices (FinViz Elite)</div>"
        "<div class='muted'>Downloaded for each run's tickers plus SPY with your FinViz token (about 2 tickers a "
        "second). A download made after the market close (1:05 pm Pacific) is reused until the next close; "
        "intraday runs while the market is open download fresh prices. 'Latest close' leaves out today's "
        "unfinished bar while the market is open; 'intraday' keeps it.</div>"
        + ("" if st.price_source == "finviz" else "<div class='muted'>Not in use: PRICE_SOURCE is set to massive.</div>")
        + "<div class='forms'><form method='post' action='/strategies/prices/test-finviz'>"
        "<button type='submit'>Test FinViz prices</button></form></div></div>")
    if st.price_source == "massive":
        prog = (f"Updating: {ps.get('done', 0)} of {ps.get('total', 0)} days…" if ps.get("running") else "")
        out.append(
            "<div class='card'><div class='v'>Daily prices (Massive)</div>"
            f"<div class='muted'>{len(days)} trading days stored" + (f", {e(days[0])} → {e(days[-1])}" if days else "")
            + ".</div>" + (f"<div class='muted'>{e(prog)}</div>" if prog else "")
            + (f"<div class='warn'>{e(str(ps['error']))}</div>" if ps.get("error") else "")
            + "<div class='forms'><form method='post' action='/strategies/prices/update'><button type='submit'>"
            "Update prices now</button></form><form method='post' action='/strategies/prices/test'>"
            "<button type='submit' class='ghost' style='color:var(--accent)'>Test Massive key</button></form></div></div>")
    out.append(
        "<div class='card'><div class='v'>Market-cap list (optional, V5)</div>"
        "<div class='muted'>Your 'tickers_with_mcap abv 50m.csv' (needs a Ticker column). Without it, V5 runs "
        "skip the &gt;$50M rule.</div>"
        f"<div style='margin-top:6px'>{'✅ Uploaded' if st.mcap_path().is_file() else 'Not uploaded'}</div>"
        "<div class='forms'><form method='post' action='/strategies/mcap' enctype='multipart/form-data'>"
        "<input type='file' name='file' accept='.csv' required aria-label='Market-cap CSV'>"
        "<button type='submit'>Upload CSV</button></form>"
        + ("<form method='post' action='/strategies/mcap/delete'><button type='submit' class='ghost'>Remove"
           "</button></form>" if st.mcap_path().is_file() else "") + "</div></div>")
    if strategies:
        out.append("<div class='card'><div class='v'>Manage strategies</div><div class='muted'>Uploading a file with "
                   "the same name saves the previous version. Deleted strategies can be restored for 30 days. "
                   "<a href='/strategies/archive'>Recycle bin &amp; version history</a></div>")
        for name, m in strategies:
            out.append(f"<div class='line' style='display:flex;justify-content:space-between;align-items:center;"
                       f"padding:6px 0;border-bottom:1px solid var(--line)'><span>{e(m['file'])} · uploaded "
                       f"{e(_fmt_local(m.get('uploaded_utc'), s.tz))}</span>"
                       "<form method='post' action='/strategies/delete' style='margin:0'>"
                       f"<input type='hidden' name='name' value='{e(name, quote=True)}'>"
                       "<button type='submit' class='ghost'>Remove</button></form></div>")
        out.append("</div>")
    out.append(f"<div class='muted' style='font-size:13px'>Scan schedule: {e(scan_schedule_summary(app))} "
               "(change it on the <a href='/#scan'>Scans</a> tab). Time zone for "
               f"scheduled runs: {e(s.tz_name)}.</div></details>")
    out.append("<p class='muted'>Information only, not investment advice.</p></main>")
    out.append("<script>(function(){" + _STRAT_JS.replace("__TZ__", json.dumps(s.tz_name))
               .replace("__BUSY__", "true" if busy else "false") + "})();</script></body></html>")
    return "\n".join(x for x in out if x)


_STRAT_JS = r"""
var f=document.getElementById('go');if(!f)return;var dirty=false;
var boxes=[].slice.call(f.querySelectorAll("input[name='s']")),all=document.getElementById('all');
var when=document.getElementById('when'),dl=document.getElementById('dl'),tl=document.getElementById('tl'),
tz=document.getElementById('tz'),date=document.getElementById('date'),time=document.getElementById('time'),
btn=document.getElementById('gobtn'),sum=document.getElementById('sum'),cnt=document.getElementById('cnt'),
tlist=document.getElementById('tlist'),addt=document.getElementById('addtime');
function slots(){return tlist?[].slice.call(tlist.querySelectorAll('.tslot')):[];}
function times(){var seen={},out=[];slots().forEach(function(s){var v=s.querySelector('input').value;
if(v&&!seen[v]){seen[v]=1;out.push(v);}});return out.sort();}
function tzLabel(){try{var d=new Date((date.value||'2000-01-01')+'T'+(time.value||'12:00')+':00Z');
var p=new Intl.DateTimeFormat('en-US',{timeZone:__TZ__,timeZoneName:'short'}).formatToParts(d);
var n=p.filter(function(x){return x.type==='timeZoneName'})[0];var lo=new Intl.DateTimeFormat('en-US',
{timeZone:__TZ__,timeZoneName:'longOffset'}).formatToParts(d).filter(function(x){return x.type==='timeZoneName'})[0];
return (n?n.value:'')+(lo?' · '+lo.value.replace('GMT','UTC').replace('-','−'):'');}catch(e){return tz.textContent;}}
function upd(){var c=boxes.filter(function(b){return b.checked}).length;
boxes.forEach(function(b){b.closest('.srow').classList.toggle('on',b.checked)});
if(cnt)cnt.textContent=c+' of '+boxes.length+' selected';
if(all){all.checked=c===boxes.length&&c>0;all.indeterminate=c>0&&c<boxes.length;}
if(!when)return;var w=when.value;dl.style.display=w==='once'?'':'none';tl.style.display=w==='now'?'none':'';
tz.style.display=w==='now'?'none':'';tz.textContent=tzLabel();
var n=slots().length,ts=times();if(addt)addt.hidden=w==='now'||n>=12;
slots().forEach(function(s){s.querySelector('.tdel').hidden=n<2;});
btn.textContent=w==='now'?'Run strategies':(ts.length>1?'Schedule '+ts.length+' runs':'Schedule strategies');
btn.disabled=c===0||(w!=='now'&&!ts.length);
var who=c+' selected strateg'+(c===1?'y':'ies'),at=ts.join(', ')+' '+tz.textContent.split(' ·')[0];
sum.textContent=w==='now'?who+' · runs now':!ts.length?who+' · pick a time':w==='once'?who+' · '+date.value+' at '+at
:who+' · every weekday at '+at;}
boxes.forEach(function(b){b.addEventListener('change',function(){dirty=true;upd()})});
if(all)all.addEventListener('change',function(){boxes.forEach(function(b){b.checked=all.checked});dirty=true;upd()});
[when,date].forEach(function(x){if(x)x.addEventListener('input',function(){dirty=true;upd()})});
if(tlist){tlist.addEventListener('input',function(){dirty=true;upd()});
tlist.addEventListener('click',function(ev){var b=ev.target.closest('.tdel');if(!b||slots().length<2)return;
b.closest('.tslot').remove();if(!document.getElementById('time')){tlist.querySelector('input').id='time';}
time=document.getElementById('time');dirty=true;upd();});}
if(addt)addt.addEventListener('click',function(){var s=slots();if(s.length>=12)return;
var c2=s[0].cloneNode(true),inp=c2.querySelector('input');inp.removeAttribute('id');inp.value='';
inp.setAttribute('aria-label','Time '+(s.length+1));tlist.appendChild(c2);dirty=true;upd();inp.focus();});
f.querySelectorAll('select').forEach(function(x){x.addEventListener('change',function(){dirty=true})});
f.querySelectorAll('.run-filters input').forEach(function(x){x.addEventListener('input',function(){dirty=true})});
var auto=document.getElementById('auto');if(auto)auto.addEventListener('change',function(){
document.getElementById('autobtn').click()});
var up=document.getElementById('upfile');if(up)up.addEventListener('change',function(){if(up.files.length)
document.getElementById('up').submit()});
var dlg=document.getElementById('tvdlg'),tvn=document.getElementById('tvname'),tvu=document.getElementById('tvurl');
[].slice.call(document.querySelectorAll('button.tvedit')).forEach(function(b){b.hidden=false;
b.addEventListener('click',function(ev){ev.preventDefault();ev.stopPropagation();tvn.value=b.dataset.name;
tvu.value=b.dataset.url||'';document.getElementById('tvdlg-for').textContent='For '+b.dataset.label;
if(dlg.showModal){dlg.showModal();}else{dlg.setAttribute('open','');}tvu.focus();});});
var tvc=document.getElementById('tvcancel');if(tvc)tvc.addEventListener('click',function(){dlg.close?dlg.close():
dlg.removeAttribute('open');});
[].slice.call(document.querySelectorAll('a.tvlink')).forEach(function(a){a.addEventListener('click',function(ev){
ev.stopPropagation();try{if(navigator.clipboard)navigator.clipboard.writeText(a.href);}catch(e){}});});
var nd=document.getElementById('strategy-note-dialog'),nt=document.getElementById('strategy-note-text');
function noteCount(){document.getElementById('strategy-note-count').textContent=Array.from(nt.value).length+' / 1000 characters';}
if(nt)nt.addEventListener('input',noteCount);
[].slice.call(document.querySelectorAll('button.strategy-note')).forEach(function(b){b.hidden=false;
b.addEventListener('click',function(ev){ev.preventDefault();ev.stopPropagation();
document.getElementById('strategy-note-name').value=b.dataset.name;
document.getElementById('strategy-note-label').textContent='For '+b.dataset.label;
nt.value=b.dataset.note||'';noteCount();if(nd.showModal)nd.showModal();else nd.setAttribute('open','');nt.focus();});});
var nc=document.getElementById('strategy-note-cancel');if(nc)nc.addEventListener('click',function(){nd.close?nd.close():nd.removeAttribute('open');});
var df=document.getElementById('delform'),dn=document.getElementById('delname');
[].slice.call(document.querySelectorAll('button.del')).forEach(function(b){b.hidden=false;
b.addEventListener('click',function(ev){ev.preventDefault();ev.stopPropagation();
if(confirm('Move '+b.dataset.label+' to the recycle bin? You can restore it for 30 days. Past results are kept.')){dn.value=b.dataset.name;df.submit();}})});
upd();if(__BUSY__)setTimeout(function(){if(!dirty)location.reload()},10000);
"""


# ── Result page ("Signal desk" design) ────────────────────────────────────────
# One page for every kind of result. Rows are rendered on the server (so the page works without
# JavaScript); the script below only filters, searches and re-orders them.
STATUS_GROUP = {
    "BUY": "buy", "BUY SIGNAL": "buy", "SIGNAL": "buy",
    "BLOCKED BY REGIME": "blocked",
    "NO SIGNAL": "none", "NO SCANNER FIRED": "none", "FILTERED OUT": "none", "EXCLUDED": "none",
    "ERROR": "error", "NOT IN PRICE CACHE": "error", "CHECK": "error",
}
SEGMENTS = (("all", "All results"), ("buy", "Buy"), ("blocked", "Blocked"), ("none", "No signal"),
            ("error", "Errors"), ("other", "Other"))
SEGMENT_NOUN = {"all": "All results", "buy": "Buy signals", "blocked": "Blocked by regime", "none": "No signal",
                "error": "Errors", "other": "Other"}
BADGE = {"buy": "↗ BUY", "blocked": "BLOCKED", "none": "NO SIGNAL", "error": "ERROR", "other": ""}
DETAIL_FIELDS = ("Detail", "Error", "Note")  # shown under the ticker, not as columns


def _num(v) -> Optional[float]:
    try:
        x = float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"${x:,.3f}" if abs(x) < 1 else f"${x:,.2f}"


def _fmt_value(kind: str, raw: str) -> Tuple[str, str]:
    """(text, css class) for one cell."""
    x = _num(raw)
    if kind == "text":
        return (raw or "—"), ""
    if kind == "yesno":
        t = str(raw).strip().lower()
        return ("Yes" if t in ("true", "yes", "1") else "No" if t in ("false", "no", "0") else "—"), ""
    if x is None:
        return "—", "dim"
    if kind == "price":
        return _fmt_price(x), ""
    if kind == "money":
        return f"${x:,.0f}", ""
    if kind == "signed":
        return f"{x:+.2f}", ("up" if x > 0 else "down" if x < 0 else "")
    if kind == "pct":
        return f"{x:+.2f}%", ("up" if x > 0 else "down" if x < 0 else "")
    return f"{x:,.2f}", ""


def _looks_numeric(rows: List[Dict[str, str]], col: str) -> bool:
    vals = [r.get(col, "") for r in rows if (r.get(col) or "").strip()]
    return bool(vals) and sum(_num(v) is not None for v in vals) >= 0.8 * len(vals)


def result_layout(fields: List[str], rows: List[Dict[str, str]], kind: str) -> dict:
    """Which columns to show, how to format them, and which filters apply, for this kind of result.
    Each column: (label, kind, source field or None for a computed value)."""
    has = set(fields)
    lay = {"cols": [], "price": None, "metric": None, "latest": None, "sort": None, "note": "",
           "gap": None, "status": "Status" in has}
    price = next((c for c in ("Close", "Price") if c in has), None)
    if {"Original_MFI", "Fourier_MF"} <= has:  # Fourier Money Flow (fmfi_scanner)
        lay["cols"] = [("Close", "price", price), ("% High", "pct", "High_%"),
                       ("Original MFI", "num", "Original_MFI"),
                       ("Fourier MF", "num", "Fourier_MF"), ("MFI gap", "signed", None)]
        if "Buy_Latest" in has:
            lay["cols"].append(("Latest candle", "yesno", "Buy_Latest"))
            lay["latest"] = "Buy_Latest"
        lay.update(price=price, metric=("Original_MFI", "Min original MFI"), gap=("Original_MFI", "Fourier_MF"),
                   sort=4, note="% High = (day’s high / previous trading close − 1) × 100 · "
                   "MFI gap = Original MFI − Fourier MF · All prices in USD")
        return lay
    if kind == "v5" or {"Tier", "TradeState", "Detail"} <= has:  # MACDgen V5-style
        spec = [("Price", "price", "Price"), ("Day change", "pct", "Chg_%"), ("Week change", "pct", "Wk_%"),
                ("Volume ratio", "num", "VR"), ("Tier", "text", "Tier"), ("Size", "money", "Size_$"),
                ("Trade", "text", "TradeState"), ("Return", "pct", "EffRet_%")]
        lay["cols"] = [c for c in spec if c[2] in has]
        lay.update(price="Price" if "Price" in has else None,
                   metric=("Chg_%", "Min day change %") if "Chg_%" in has else None,
                   sort=next((i for i, c in enumerate(lay["cols"]) if c[2] == "Chg_%"), None),
                   note="All prices in USD")
        return lay
    skip = {"Ticker", "Status", "CacheTicker", *DETAIL_FIELDS}
    for f in fields:
        if f in skip:
            continue
        if f == price:
            lay["cols"].append((f, "price", f))
        elif _looks_numeric(rows, f):
            lay["cols"].append((f.replace("_", " "), "pct" if f.endswith("%") else "num", f))
        else:
            lay["cols"].append((f.replace("_", " "), "text", f))
    lay["price"] = price
    firstnum = next((i for i, c in enumerate(lay["cols"]) if c[1] in ("num", "pct", "signed")), None)
    lay["sort"] = firstnum
    if price:
        lay["note"] = "All prices in USD"
    return lay


def _cell_value(r: Dict[str, str], col, lay) -> Optional[float]:
    label, kind, src = col
    if src is None and lay.get("gap"):
        a, b = (_num(r.get(x)) for x in lay["gap"])
        return None if a is None or b is None else a - b
    return _num(r.get(src, ""))


def _scan_when(stamp: str, tz) -> str:
    try:
        when = dt.datetime.strptime(stamp, "%Y-%m-%d_%H%M%S").replace(tzinfo=tz)
    except ValueError:
        return stamp
    return f"{when:%b} {when.day} · {when:%I:%M %p %Z}".replace(" 0", " ")


def _day_text(iso: str) -> str:
    """'2026-10-30' -> 'Oct 30, 2026'; a minute candle '2026-10-30 15:45' -> 'Oct 30, 2026 · 3:45 PM ET'."""
    iso = (iso or "").strip()
    try:
        d = dt.date.fromisoformat(iso[:10])
    except (TypeError, ValueError):
        return iso or "—"
    text = f"{d:%b} {d.day}, {d.year}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", iso):
        h, m = int(iso[11:13]), int(iso[14:16])
        text += f" · {h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'} ET"
    return text


def _candles_text(spec: Optional[str]) -> str:
    try:
        return sm.candles_label(spec or "1d")
    except ValueError:
        return "Daily"


def _candles_picker(spec: str, supported: List[str]) -> str:
    """Number + days/minutes. Only strategies that opt in (CANDLE_DAYS) use it; the rest stay daily."""
    e = html.escape
    try:
        n, unit = sm.parse_candles(spec or "1d")
    except ValueError:
        n, unit = 1, "d"
    return ("<div class='cpick'><span>Candles</span><div class='row'>"
            f"<input type='number' name='cn' min='1' max='390' step='1' value='{n}' aria-label='Candle size' required>"
            "<select name='cu' aria-label='Candle unit'>"
            f"<option value='days'{' selected' if unit == 'd' else ''}>days</option>"
            f"<option value='minutes'{' selected' if unit == 'm' else ''}>minutes</option></select></div></div>")


def _candles_hint(supported: List[str]) -> str:
    who = ", ".join(_pretty_name(x) for x in supported) or "none of your strategies yet"
    return ("<div class='chint'>Candles: 1 day = daily; 5 days = each candle covers 5 trading days; minutes = "
            "intraday candles from 9:30 to 4:00 ET (FinViz keeps about 3 weeks of 1–15-minute bars and 9 months of "
            f"30-minute ones). Used by: {html.escape(who)}. Other strategies run on daily candles.</div>")


def add_high_percent(rows: List[Dict[str, str]], result_dir: Path, asof: str) -> List[Dict[str, str]]:
    """Use this run's saved bars, without fetching prices or changing its CSV."""
    history = {}
    path = result_dir / "history_cache.json"
    try:
        if sm.regular_file(path, result_dir):
            with path.open(encoding="utf-8") as fh:
                cache = json.load(fh)
            if isinstance(cache, dict) and isinstance(cache.get("history"), dict):
                history = cache["history"]
    except (OSError, ValueError):
        pass
    by_symbol = {}
    for day, bars in sorted(history.items()):
        if not isinstance(bars, dict):
            continue
        for symbol, bar in bars.items():
            if isinstance(bar, dict):
                by_symbol.setdefault(symbol, []).append((day, bar))
    enriched = []
    for row in rows:
        result = dict(row, **{"High_%": ""})
        symbol = (row.get("CacheTicker") or row.get("Ticker") or "").strip()
        day = row.get("As_Of") or row.get("AsOf") or asof
        bars = by_symbol.get(symbol, [])
        for i, (bar_day, bar) in enumerate(bars):
            if bar_day != day or i == 0:
                continue
            high, previous = _num(bar.get("h")), _num(bars[i - 1][1].get("c"))
            if high is not None and high > 0 and previous is not None and previous > 0:
                result["High_%"] = str((high / previous - 1) * 100)
            break
        enriched.append(result)
    return enriched


def render_result(app: App, rid: str) -> Optional[str]:
    e = html.escape
    s, st = app.settings, app.strategies
    d = st.results_dir / rid
    if not d.is_dir():
        return None
    meta = sm.read_meta(d)
    fields, rows = sm.read_csv(d / "signals.csv", d)
    st_fields, st_rows = sm.read_csv(d / "ticker_status.csv", d)
    output = sm.read_text(d / "output.txt", d)
    status = meta.get("status", "")
    running = status == "running"
    scan = meta.get("scan", "")
    name = meta.get("strategy", "")
    smeta = st.meta().get(name, {})
    kind = smeta.get("kind", "")
    script_view = bool(st_rows) and "Status" not in fields  # whole-script dashboard: one row per scan ticker
    if script_view:  # each scan ticker's status, plus its row from the script's own CSV when it signalled
        own = {(r.get("Ticker") or "").strip(): r for r in rows}
        table_fields = list(st_fields) + [f for f in fields if f not in st_fields]
        table_rows = [{**r, **{k: v for k, v in own.get((r.get("Ticker") or "").strip(), {}).items()
                               if k != "Ticker" and v not in (None, "")}} for r in st_rows]
    else:
        table_fields, table_rows = fields, rows
    if {"Original_MFI", "Fourier_MF"} <= set(table_fields):
        table_rows = add_high_percent(table_rows, d, meta.get("asof", ""))
    lay = result_layout(table_fields, table_rows, kind)
    title = smeta.get("title") or _pretty_name(name)
    desc = smeta.get("description") or ""
    sub = " · ".join(x for x in (_pretty_name(name) if smeta.get("title") else (smeta.get("file") or f"{name}.py"),
                                 desc) if x)

    # header facts
    mode_text = "Intraday" if meta.get("mode") == "intraday" else "Latest close"
    candle_text = _candles_text(meta.get("candles"))
    if meta.get("candles") and meta["candles"] != "1d":
        mode_text = (f"{candle_text} candles · " + ("newest may still be forming" if meta.get("mode") == "intraday"
                                                     else "completed candles")) if meta["candles"].endswith("m") \
            else f"{mode_text} · {candle_text} candles"
    universe = None
    tfile = d / "tickers.txt"
    if sm.regular_file(tfile, d):
        lines = sm.read_text(tfile, d).splitlines()
        universe = len([t for t in (lines[1] if len(lines) > 1 else "").split(",") if t.strip()])
    universe = universe if universe is not None else len(table_rows)
    pill = {"ok": ("done", "Completed"), "error": ("fail", "Failed"), "running": ("run", "Running")}.get(
        status, ("", status or "Unknown"))

    # rows → groups and counts
    def group_of(r):
        if not lay["status"]:
            return "buy" if not script_view else "other"
        return STATUS_GROUP.get((r.get("Status") or "").strip().upper(), "other")
    groups = [group_of(r) for r in table_rows]
    counts = {"all": len(table_rows)}
    for g in groups:
        counts[g] = counts.get(g, 0) + 1
    default_seg = "buy" if counts.get("buy") and lay["status"] else "all"
    buy_dates = sorted({(r.get("Signal_Dates") or r.get("As_Of") or r.get("AsOf") or "").split(";")[-1]
                        for r, g in zip(table_rows, groups) if g == "buy"} - {""})
    date_note = (f"Signal {'candle' if len(buy_dates[0]) > 10 else 'date'} · {_day_text(buy_dates[0])}"
                 if len(buy_dates) == 1
                 else f"As of · {_day_text(meta.get('asof', ''))}" if meta.get("asof") else "")

    out = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{e(title)} · Signal desk</title>",
        "<meta http-equiv='refresh' content='5'>" if running else "",
        _THEME_INIT + f"<style>{_CSS}{_RESULT_CSS}</style></head><body class='rp'>",
        "<header class='topbar'><div class='tb-in'><a class='brand' href='/strategies'><span aria-hidden='true'>◆</span>"
        " Signal desk</a><nav aria-label='Main'><a href='/'>Scans</a><a class='on' href='/strategies' "
        f"aria-current='page'>Strategies</a><a href='/paper?research={e(rid)}'>Paper Trading</a><a href='/journal'>Journal</a><a href='/latest/report.html'>Latest report</a></nav>"
        + account_controls() + "</div></header>",
        "<main class='rmain'>",
        "<div class='crumb'><a href='/strategies#activity'>Strategies</a> / Scan results</div>",
        f"<div class='titlerow'><h1>{e(title)}</h1><span class='pill {pill[0]}'><span class='dot'></span>"
        f"{e(pill[1])}</span>"
        + (f"<a class='tvbtn tvlink' href='{e(smeta['tv_url'], quote=True)}' target='_blank' rel='noopener noreferrer' "
           "title='Open in TradingView (link also copied for TradingView Desktop: Open link from clipboard)'>"
           "Open in TradingView ↗</a>" if smeta.get("tv_url") else "") + "</div>",
        f"<div class='sub'>{e(sub)}</div>" if sub else "",
        "<div class='facts'>"
        f"<span><span class='k'>Market data</span> {e(_day_text(meta.get('asof', '')))} · {e(mode_text)}</span>"
        + (f"<span><span class='k'>Scan</span> <a href='/runs/{e(scan)}/report.html'>{e(_scan_when(scan, s.tz))}</a>"
           "</span>" if STAMP_RE.match(scan or "") else "")
        + f"<span><span class='k'>Universe</span> {universe:,} tickers</span></div>",
    ]
    notes = []
    if running:
        notes.append("Still running. This page refreshes every 5 seconds.")
    if status == "error":
        notes.append("The strategy stopped with an error. See Strategy output below for the reason.")
    if kind == "v5" and meta.get("mcap_rule") is False:
        notes.append("Market-cap filter wasn't applied to this scan. No market-cap list was uploaded.")
    for n in notes:
        out.append(f"<div class='notice'>{e(n)}</div>")

    if table_rows:
        cols = lay["cols"]
        segs = [(k, lbl) for k, lbl in SEGMENTS if k == "all" or counts.get(k)] if lay["status"] or script_view \
            else [("all", "All results")]
        heading = "Signals" if lay["status"] or script_view else "Results"
        out.append(f"<section class='sigcard' id='sig'><div class='sc-head'><h2>{heading}</h2></div>"
                   "<div class='segs' role='tablist' aria-label='Show'>")
        for k, lbl in segs:
            on = k == default_seg
            out.append(f"<button type='button' class='seg{' on' if on else ''}' role='tab' aria-selected="
                       f"'{'true' if on else 'false'}' data-seg='{k}'>{e(lbl)} <span class='cnt'>{counts.get(k, 0)}"
                       "</span></button>")
        out.append("</div>")
        sort_opts = [("t:asc", "Ticker · A–Z")]
        for i, c in enumerate(cols):
            if c[1] not in ("text", "yesno"):
                sort_opts += [(f"{i}:desc", f"{c[0]} · Highest first"), (f"{i}:asc", f"{c[0]} · Lowest first")]
        default_sort = f"{lay['sort']}:desc" if lay["sort"] is not None else "t:asc"
        out.append("<div class='tools'><input type='search' id='q' placeholder='Search tickers…' "
                   "aria-label='Search tickers' autocomplete='off'>"
                   "<select id='sort' aria-label='Sort'>" + "".join(
                       f"<option value='{v}'{' selected' if v == default_sort else ''}>{e(t)}</option>"
                       for v, t in sort_opts) + "</select>")
        has_filters = bool(lay["price"] or lay["metric"] or lay["latest"])
        if has_filters:
            out.append("<button type='button' id='ftog' class='fbtn' aria-expanded='false' aria-controls='fpanel'>"
                       "Filters</button>")
        out.append("</div>")
        if has_filters:
            out.append("<div class='fpanel' id='fpanel' hidden>")
            if lay["price"]:
                out.append("<label>Min price ($)<input type='number' id='fmin' step='any' min='0' placeholder='Any'>"
                           "</label><label>Max price ($)<input type='number' id='fmax' step='any' min='0' "
                           "placeholder='Any'></label>")
            if lay["metric"]:
                out.append(f"<label>{e(lay['metric'][1])}<input type='number' id='fmet' step='any' "
                           "placeholder='Any'></label>")
            if lay["latest"]:
                out.append("<label class='chk'><input type='checkbox' id='flat'> Latest candle only</label>")
            out.append("<button type='button' id='freset' class='linkbtn'>Reset filters</button></div>")
        out.append("<div class='infoline'><span id='shown'>"
                   f"{counts.get(default_seg, 0)} of {counts.get(default_seg, 0)} rows · "
                   f"{e(SEGMENT_NOUN[default_seg])}</span>"
                   f"<span>{e(date_note)}</span></div>")
        # table
        head = ["<th class='sortable' data-col='t' aria-sort='none'><button type='button'>Ticker <span class='ar'>↕"
                "</span></button></th>"]
        if lay["status"]:
            head.append("<th>Signal</th>")
        for i, c in enumerate(cols):
            num = c[1] not in ("text", "yesno")
            if num:
                arrow = "↓" if lay["sort"] == i else "↕"
                head.append(f"<th class='n sortable' data-col='{i}' aria-sort="
                            f"'{'descending' if lay['sort'] == i else 'none'}'><button type='button'>{e(c[0])} "
                            f"<span class='ar'>{arrow}</span></button></th>")
            else:
                head.append(f"<th class='{'n' if c[1] == 'yesno' else ''}'>{e(c[0])}</th>")
        body = []
        order = list(range(len(table_rows)))
        if lay["sort"] is not None:  # server-side default order matches the default sort
            order.sort(key=lambda i: (_cell_value(table_rows[i], cols[lay["sort"]], lay) is None,
                                      -(_cell_value(table_rows[i], cols[lay["sort"]], lay) or 0),
                                      table_rows[i].get("Ticker", "")))
        else:
            order.sort(key=lambda i: table_rows[i].get("Ticker", ""))
        for i in order[:5000]:
            r, g = table_rows[i], groups[i]
            t = (r.get("Ticker") or "").strip()
            pv = _num(r.get(lay["price"], "")) if lay["price"] else None
            mv = _num(r.get(lay["metric"][0], "")) if lay["metric"] else None
            lt = str(r.get(lay["latest"], "")).strip().lower() in ("true", "yes", "1") if lay["latest"] else False
            attrs = [f"data-g='{g}'", f"data-t='{e(t.upper(), quote=True)}'",
                     f"data-p='{'' if pv is None else pv}'", f"data-m='{'' if mv is None else mv}'",
                     f"data-l='{1 if lt else 0}'"]
            cells = []
            for j, c in enumerate(cols):
                v = _cell_value(r, c, lay) if c[1] not in ("text", "yesno") else None
                if c[1] not in ("text", "yesno"):
                    attrs.append(f"data-s{j}='{'' if v is None else v}'")
                raw = (f"{v}" if c[2] is None and v is not None else r.get(c[2], "") if c[2] else "")
                text, cls = _fmt_value(c[1], raw)
                cells.append(f"<td class='{'n ' if c[1] != 'text' else ''}{cls}'>{e(text)}</td>")
            detail = next(((r.get(f) or "").strip() for f in DETAIL_FIELDS if (r.get(f) or "").strip()), "")
            tlink = (f"<a href='https://finviz.com/quote.ashx?t={urllib.parse.quote(t)}' target='_blank' "
                     f"rel='noopener noreferrer'>{e(t)}</a>" if re.fullmatch(r"[A-Z0-9.\-]{1,10}", t) else e(t or "—"))
            logt = ""
            if g == "buy" and re.fullmatch(r"[A-Z0-9.\-]{1,10}", t):
                sig_day = ((r.get("Signal_Dates") or "").split(";")[-1] or r.get("As_Of") or r.get("AsOf")
                           or meta.get("asof") or "")[:10]
                logq = {"symbol": t, "strategy": name, "date": sig_day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", sig_day)
                        else "", "price": "" if pv is None else f"{pv:g}"}
                logt = (f" <a class='logt' href='/journal/add?{e(urllib.parse.urlencode({k: v for k, v in logq.items() if v}), quote=True)}' "
                        f"title='Add this signal to your trade journal'>Log trade</a>")
            notelink = (f" <a class='notet' href='/notes?{e(urllib.parse.urlencode({'symbol':t,'new':'1','source':name}), quote=True)}'>Trading note</a>"
                        if re.fullmatch(r"[A-Z0-9.\-]{1,10}", t) else "")
            tcell = (f"<td class='tk'>{tlink}{logt}{notelink}" + (f"<div class='det'>{e(detail)}</div>" if detail else "")
                     + "</td>")
            badge = ""
            if lay["status"]:
                status_txt = (r.get("Status") or "").strip()
                label = BADGE.get(g) or status_txt
                if (r.get("Status") or "").strip().upper() == "NOT IN PRICE CACHE":
                    label = "NO DATA"
                badge = f"<td><span class='badge {g}' title='{e(status_txt, quote=True)}'>{e(label)}</span></td>"
            hide = default_seg != "all" and g != default_seg
            body.append(f"<tr {' '.join(attrs)}{' hidden' if hide else ''}>{tcell}{badge}{''.join(cells)}</tr>")
        out.append("<div class='twrap'><table class='sigt' id='sigt'><thead><tr>" + "".join(head)
                   + "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
                   "<p class='empty' id='empty' hidden>No rows match. Try another tab or reset the filters.</p></div>")
        if len(order) > 5000:
            out.append(f"<p class='foot'>Showing the first 5,000 of {len(order):,} rows; the CSV has them all.</p>")
        if lay["note"]:
            out.append(f"<p class='foot'>{e(lay['note'])}</p>")
        out.append("</section>")
        if script_view and rows:
            out.append(f"<p class='muted small'>Your script's own result table ({len(rows)} rows) is in "
                       "signals.csv under Files.</p>")
    elif not running:
        out.append("<section class='sigcard'><div class='sc-head'><h2>Signals</h2></div>"
                   "<p class='empty'>No result table was produced. See Strategy output below.</p></section>")

    if (os.environ.get("VBT_SERVICE_URL") and os.environ.get("VBT_SERVICE_TOKEN") and
            status == "ok" and meta.get("mode") == "close" and meta.get("candles", "1d") == "1d"):
        out.append(f"<section class='runcard'><h2>VectorBT PRO backtest</h2><form method='post' action='/strategies/backtest'>"
                   f"<input type='hidden' name='rid' value='{e(rid)}'><label>Symbol <input name='symbol' required maxlength='15'></label> "
                   + ("<label>Signals <select name='source'><option value='archive'>Saved signals</option>"
                      "<option value='replay'>Replay current Python strategy on this symbol</option></select></label> "
                      "<label>Replay sessions <input type='number' name='sessions' min='4' max='252' value='60'></label> "
                      if kind == "generic" else "") +
                   "<label>Holding periods (sessions) <input name='holds' value='1,5,10' required></label> "
                   "<label>Fee per side (basis points) <input type='number' name='fee' min='0' max='100' step='any' value='0'></label> "
                   "<button>Run backtest</button></form><p>Replay archived buy signals from matching strategy versions. Older unversioned runs are grouped by strategy name. "
                   "Python replay uses one fixed symbol and daily bars available through each evaluation date. "
                   "Enter at the following open and exit after the selected number of sessions. Days without a saved buy stay flat when out of position. "
                   "Compare holding periods on the first 70% of bars and validate the best on the final 30%. "
                   "Each period includes only trades with both fills inside it.</p></section>")

    # run details: when, trigger, prices, files, raw output
    names = [f for f in sm.RESULT_FILES if sm.regular_file(d / f, d)]
    names += sorted(p.name for p in d.glob("*.csv") if p.name not in sm.RESULT_FILES
                    and sm.EXTRA_CSV_RE.match(p.name) and sm.regular_file(p, d))
    if sm.regular_file(d / "signals.csv", d):
        names.insert(1, SIGNAL_TXT)
    links = " · ".join(f"<a href='/strategies/results/{e(rid)}/{urllib.parse.quote(f)}'>{e(f)}</a>" for f in names)
    src = {"finviz": "FinViz Elite", "massive": "Massive"}.get(meta.get("price_source", ""), "—")
    out.append("<section class='runcard'><h2>Run details</h2><dl>"
               f"<div><dt>Started</dt><dd>{e(_fmt_local(meta.get('started_utc'), s.tz))}</dd></div>"
               f"<div><dt>Trigger</dt><dd>{e(meta.get('reason', '') or '—')}</dd></div>"
               f"<div><dt>Prices</dt><dd>{e(src)} · {e(mode_text.lower())}"
               + (f" · {meta['price_days']} days" if meta.get("price_days") else "") + "</dd></div>"
               f"<div><dt>Files</dt><dd class='files'>{links or '—'}</dd></div></dl>"
               f"<details{' open' if status == 'error' or not table_rows else ''}><summary>Strategy output</summary>"
               f"<pre class='out'>{e(output[-40000:])}</pre></details></section>")
    out.append("<p class='muted small'>Information only, not investment advice.</p></main>")
    out.append("<script>(function(){" + _RESULT_JS + "})();</script></body></html>")
    return "\n".join(x for x in out if x)


_RESULT_CSS = """
.notet,.logt{font-size:11.5px;font-weight:600;margin-left:6px;padding:1px 8px;border:1px solid var(--rline);border-radius:999px;white-space:nowrap;color:var(--racc)}.notet:hover,.logt:hover{text-decoration:none;background:var(--rsoft)}
body.rp{background:var(--rbg)}:root{--rbg:#f5f6f8;--rcard:#fff;--rline:#e3e6ea;--rink:#14181f;--rmuted:#5f6875;
--racc:#1f5fbf;--rsoft:#eef4fd;--rgood:#13804b;--rgoodbg:#e6f4ec;--rbad:#b3261e;--rbadbg:#fdecea;--rwarn:#8a5a00;
--rwarnbg:#fff4dc}
:root[data-theme=dark]{--rbg:#121417;--rcard:#1b1e23;--rline:#2e333b;--rink:#e9ecf1;
--rmuted:#9aa3ae;--racc:#8db7f0;--rsoft:#1f2a3a;--rgood:#5fd08f;--rgoodbg:#16301f;--rbad:#ff8a80;--rbadbg:#3a1e1c;
--rwarn:#f0c26b;--rwarnbg:#3a2f14}
.topbar{background:var(--rcard);border-bottom:1px solid var(--rline)}
.tb-in{max-width:1180px;margin:0 auto;padding:0 24px;display:flex;align-items:center;gap:36px;min-height:64px;
flex-wrap:wrap}.brand{font-weight:800;font-size:19px;color:var(--rink);letter-spacing:-.01em}
.brand:hover{text-decoration:none}.topbar nav{display:flex;gap:26px}.topbar nav a{color:var(--rmuted);font-size:15px}
.topbar nav a.on{color:var(--racc);font-weight:600}.topbar nav a:hover{text-decoration:none;color:var(--rink)}
.rmain{max-width:1180px;margin:0 auto;padding:26px 24px 60px;color:var(--rink)}
.crumb{color:var(--rmuted);font-size:14px}.crumb a{color:var(--rmuted)}
.titlerow{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:18px 0 6px}
.titlerow h1{font-size:30px;letter-spacing:-.02em;margin:0;font-weight:800}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 11px;font-size:13px;
font-weight:600;background:var(--rsoft);color:var(--racc)}.pill .dot{width:8px;height:8px;border-radius:50%;
background:currentColor}.pill.done{background:var(--rgoodbg);color:var(--rgood)}
.pill.fail{background:var(--rbadbg);color:var(--rbad)}
.tvbtn{margin-left:auto;border:1px solid var(--rline);background:var(--rcard);color:var(--racc);border-radius:8px;
padding:8px 14px;font-weight:600;font-size:14px}.tvbtn:hover{text-decoration:none;border-color:var(--racc)}.sub{color:var(--rmuted);font-size:16px}
.facts{display:flex;flex-wrap:wrap;gap:8px 30px;margin:16px 0 20px;font-size:15px;font-weight:600}
.facts .k{color:var(--rmuted);font-weight:400;font-size:15px;text-transform:none;letter-spacing:0}
.facts a{color:var(--rink)}
.notice{background:var(--rcard);border:1px solid var(--rline);border-radius:10px;padding:14px 20px;
color:var(--rmuted);font-size:14px;margin:0 0 14px}
.sigcard,.runcard{background:var(--rcard);border:1px solid var(--rline);border-radius:14px;margin:18px 0;
overflow:hidden}.sc-head{display:flex;justify-content:space-between;align-items:center;padding:22px 22px 0}
.sigcard h2,.runcard h2{font-size:20px;margin:0;color:var(--rink)}
.segs{display:flex;flex-wrap:wrap;gap:10px;padding:16px 22px 0}
.seg{background:var(--rcard);color:var(--rmuted);border:1px solid var(--rline);border-radius:8px;padding:9px 16px;
font-weight:500;font-size:15px}.seg .cnt{margin-left:8px;opacity:.8}
.seg.on{background:var(--rsoft);color:var(--racc);border-color:var(--racc)}
.tools{display:flex;gap:12px;padding:16px 22px;flex-wrap:wrap}
.tools input[type=search]{flex:1 1 260px;min-width:0;font:inherit;font-size:15px;color:var(--rink);
background:var(--rcard);border:1px solid var(--rline);border-radius:8px;padding:10px 14px}
.tools select{font-size:15px;padding:9px 12px;border-radius:8px;border:1px solid var(--rline);
background:var(--rcard);color:var(--rink)}.fbtn{background:var(--rcard);color:var(--rink);
border:1px solid var(--rline);font-weight:500}.fbtn[aria-expanded=true]{border-color:var(--racc);color:var(--racc)}
.fpanel{display:flex;flex-wrap:wrap;gap:14px 18px;align-items:flex-end;padding:18px 22px;
background:var(--rbg);border-top:1px solid var(--rline)}.fpanel[hidden]{display:none}
.fpanel label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:var(--rmuted)}
.fpanel input[type=number]{width:140px;font:inherit;font-size:14px;color:var(--rink);background:var(--rcard);
border:1px solid var(--rline);border-radius:8px;padding:9px 12px}
.fpanel label.chk{flex-direction:row;align-items:center;gap:8px;font-size:14px;padding-bottom:10px}
.linkbtn{background:none;border:0;color:var(--racc);padding:0 0 10px;font-weight:500}
.infoline{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:14px 22px;
color:var(--rmuted);font-size:14px;border-top:1px solid var(--rline)}
.twrap{overflow-x:auto}table.sigt{border-collapse:collapse;width:100%;font-size:15px}
.sigt th{background:var(--rbg);color:var(--rmuted);font-size:13px;font-weight:500;text-transform:none;
letter-spacing:0;padding:12px 22px;border-top:1px solid var(--rline);border-bottom:1px solid var(--rline);
white-space:nowrap;text-align:left}.sigt th.n,.sigt td.n{text-align:right}
.sigt th button{background:none;border:0;padding:0;font:inherit;color:inherit;cursor:pointer;font-weight:500}
.sigt th .ar{opacity:.7}.sigt td{padding:13px 22px;border-bottom:1px solid var(--rline);
font-variant-numeric:tabular-nums;white-space:nowrap;vertical-align:middle}
.sigt td.tk{font-weight:700;font-size:16px;white-space:normal;min-width:240px}.sigt td.tk a{color:var(--racc)}
.sigt .det{font-weight:400;font-size:12.5px;color:var(--rmuted);max-width:360px;line-height:1.35}
.sigt td.up{color:var(--rgood)}.sigt td.down{color:var(--rbad)}.sigt td.dim{color:var(--rmuted)}
.badge{display:inline-block;border-radius:5px;padding:3px 8px;font-size:12px;font-weight:700;letter-spacing:.02em;
background:var(--rbg);color:var(--rmuted)}.badge.buy{background:var(--rgoodbg);color:var(--rgood)}
.badge.blocked{background:var(--rwarnbg);color:var(--rwarn)}.badge.error{background:var(--rbadbg);color:var(--rbad)}
.empty{padding:22px;color:var(--rmuted)}.foot{padding:14px 22px;margin:0;color:var(--rmuted);font-size:14px}
.runcard{padding:20px 22px}.runcard dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
gap:12px 24px;margin:14px 0}.runcard dt{color:var(--rmuted);font-size:13px}.runcard dd{margin:2px 0 0;font-weight:500}
.runcard dd.files a{margin-right:4px}.runcard summary{color:var(--racc)}.small{font-size:13px}
@media (max-width:640px){.rmain,.tb-in{padding-left:16px;padding-right:16px}.tb-in{gap:14px;padding-top:10px;
padding-bottom:10px}.titlerow h1{font-size:24px}.sigt td,.sigt th{padding:11px 14px}.segs,.tools,.infoline,
.foot,.fpanel{padding-left:16px;padding-right:16px}.sc-head{padding:18px 16px 0}.tools select,.fbtn{flex:1 1 auto}}
"""

_RESULT_JS = r"""
[].slice.call(document.querySelectorAll('a.tvlink')).forEach(function(a){a.addEventListener('click',function(){
try{if(navigator.clipboard)navigator.clipboard.writeText(a.href);}catch(e){}});});
var tb=document.getElementById('sigt');if(!tb)return;var body=tb.tBodies[0];
var rows=[].slice.call(body.rows),seg=(document.querySelector('.seg.on')||{}).dataset||{seg:'all'};seg=seg.seg||'all';
var q=document.getElementById('q'),sortSel=document.getElementById('sort'),shown=document.getElementById('shown'),
empty=document.getElementById('empty'),fmin=document.getElementById('fmin'),fmax=document.getElementById('fmax'),
fmet=document.getElementById('fmet'),flat=document.getElementById('flat'),ftog=document.getElementById('ftog'),
fpanel=document.getElementById('fpanel'),freset=document.getElementById('freset');
var nouns={all:'All results',buy:'Buy signals',blocked:'Blocked by regime',none:'No signal',error:'Errors',other:'Other'};
function num(x){return x===''||x==null?null:parseFloat(x);}
function val(el){return el&&el.value!==''?parseFloat(el.value):null;}
function apply(){var term=(q.value||'').trim().toUpperCase(),lo=val(fmin),hi=val(fmax),mm=val(fmet),
lat=flat&&flat.checked,total=0,vis=0;
rows.forEach(function(r){var d=r.dataset,inSeg=seg==='all'||d.g===seg;if(inSeg)total++;
var ok=inSeg&&(!term||d.t.indexOf(term)!==-1);var p=num(d.p),m=num(d.m);
if(ok&&lo!==null)ok=p!==null&&p>=lo;if(ok&&hi!==null)ok=p!==null&&p<=hi;if(ok&&mm!==null)ok=m!==null&&m>=mm;
if(ok&&lat)ok=d.l==='1';r.hidden=!ok;if(ok)vis++;});
shown.textContent=vis+' of '+total+' rows · '+(nouns[seg]||'');empty.hidden=vis>0;}
function sortBy(v){var p=v.split(':'),col=p[0],dir=p[1]==='asc'?1:-1;
rows.sort(function(a,b){if(col==='t'){return dir*a.dataset.t.localeCompare(b.dataset.t);}
var x=num(a.dataset['s'+col]),y=num(b.dataset['s'+col]);if(x===null&&y===null)return a.dataset.t.localeCompare(b.dataset.t);
if(x===null)return 1;if(y===null)return -1;return x===y?a.dataset.t.localeCompare(b.dataset.t):dir*(x-y);});
rows.forEach(function(r){body.appendChild(r);});
[].slice.call(tb.querySelectorAll('th.sortable')).forEach(function(th){var on=th.dataset.col===col;
th.setAttribute('aria-sort',on?(dir>0?'ascending':'descending'):'none');
th.querySelector('.ar').textContent=on?(dir>0?'↑':'↓'):'↕';});}
[].slice.call(document.querySelectorAll('.seg')).forEach(function(b){b.addEventListener('click',function(){
seg=b.dataset.seg;[].slice.call(document.querySelectorAll('.seg')).forEach(function(x){
x.classList.toggle('on',x===b);x.setAttribute('aria-selected',x===b?'true':'false');});apply();});});
q.addEventListener('input',apply);[fmin,fmax,fmet].forEach(function(x){if(x)x.addEventListener('input',apply);});
if(flat)flat.addEventListener('change',apply);
sortSel.addEventListener('change',function(){sortBy(sortSel.value);});
[].slice.call(tb.querySelectorAll('th.sortable button')).forEach(function(b){b.addEventListener('click',function(){
var col=b.parentNode.dataset.col,cur=sortSel.value.split(':'),dir=cur[0]===col&&cur[1]==='desc'?'asc':
(cur[0]===col?'desc':(col==='t'?'asc':'desc'));var v=col+':'+dir;
if(![].some.call(sortSel.options,function(o){return o.value===v;})){var o=document.createElement('option');
o.value=v;o.textContent=v;o.hidden=true;sortSel.appendChild(o);}sortSel.value=v;sortBy(v);});});
if(ftog)ftog.addEventListener('click',function(){var open=fpanel.hidden;fpanel.hidden=!open;
ftog.setAttribute('aria-expanded',open?'true':'false');if(open){var f=fpanel.querySelector('input');if(f)f.focus();}});
if(freset)freset.addEventListener('click',function(){[fmin,fmax,fmet].forEach(function(x){if(x)x.value='';});
if(flat)flat.checked=false;q.value='';apply();});
sortBy(sortSel.value);apply();
"""


_BROWSER_SESSIONS: Dict[str, float] = {}
_SESSION_LOCK = threading.Lock()
_SESSION_SECONDS = 12 * 60 * 60  # Absolute lifetime from sign-in; activity does not extend it.
_SESSION_COOKIE = "finviz_session"
_CHANGES = cg.PendingChanges()


def render_archive(app: App) -> str:
    e = html.escape
    st = app.strategies
    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width,initial-scale=1'><title>Strategy recovery · Signal desk</title>",
           _THEME_INIT + f"<style>{_CSS}{_STRAT_CSS}{_JOURNAL_DESIGN_CSS}{_DASHBOARD_THEME_CSS}{_STRATEGIES_THEME_CSS}</style></head><body class='journal-app dashboard-app strategies-app'><main>", render_tabs("strategies"),
           "<p><a href='/strategies'>← Strategies</a></p><h1>Recycle bin &amp; version history</h1>",
           "<p class='muted'>Restore requires a fresh email OTP. All past scan results stay available.</p>"]
    out.append(_flash_html(app, "archive"))
    for bucket, title, description in (
        ("trash", "Recycle bin", "Deleted strategies are recoverable for 30 days, then automatically removed. Restoring never overwrites an existing strategy."),
        ("versions", "Version history", "Each replacement saves the previous version. Versions have no automatic expiry. Restoring a version also preserves the current one.")):
        rows = st.archive_entries(bucket)
        out.append(f"<h2>{title}</h2><p class='muted'>{description}</p><div class='card'>")
        if not rows:
            out.append("<p class='muted'>No deleted strategies.</p>" if bucket == "trash" else
                       "<p class='muted'>No saved versions yet. The next replacement will save the current strategy.</p>")
        else:
            out.append("<div class='wrap'><table><thead><tr><th>Strategy</th><th>Saved</th>"
                       + ("<th>Recover until</th>" if bucket == "trash" else "<th>Version</th>")
                       + "<th></th></tr></thead><tbody>")
            for r in rows:
                saved = _fmt_local(r.get("saved_utc"), app.settings.tz)
                extra = (_fmt_local(dt.datetime.fromtimestamp(r["expires"], dt.timezone.utc).isoformat(), app.settings.tz)
                         if bucket == "trash" else r["sha256"][:12])
                out.append(f"<tr><td class='name'>{e(_pretty_name(r['name']))}<div class='muted small'>{e(r['name'])}.py</div></td>"
                           f"<td>{e(saved)}</td><td>{e(extra)}</td><td><form method='post' action='/strategies/restore'>"
                           f"<input type='hidden' name='bucket' value='{bucket}'>"
                           f"<input type='hidden' name='entry' value='{e(r['id'], quote=True)}'>"
                           "<button type='submit'>Restore</button></form></td></tr>")
            out.append("</tbody></table></div>")
        out.append("</div>")
    out.append("</main></body></html>")
    return "\n".join(out)


def protected_change_label(path: str, raw: bytes, content_type: str) -> Optional[str]:
    labels = {
        "/strategies/delete": "Move strategy to recycle bin", "/strategies/upload": "Upload or replace strategy",
        "/strategies/mcap": "Replace market-cap configuration", "/strategies/mcap/delete": "Remove market-cap configuration",
        "/strategies/autoset": "Change automatic strategy settings", "/strategies/auto": "Change automatic strategy settings",
        "/strategies/schedule/cancel": "Cancel strategy schedule", "/scans/schedule/cancel": "Cancel scan schedule",
        "/presets/add": "Add screener preset", "/presets/remove": "Remove screener preset",
        "/strategies/restore": "Restore strategy archive",
        "/strategies/tvlink": "Change strategy TradingView link", "/presets/keep": "Change preset Keep setting",
        "/presets/topn": "Change usual top per screen", "/journal/clear": "Delete all journal trades, notes and "
        "import history", "/journal/delete": "Delete journal trade",
        "/strategies/results/delete": "Delete run history, result files and log",
    }
    if path in ("/strategies/upload", "/strategies/mcap"):
        files = parse_multipart(content_type, raw)
        filename, _ = files.get("file", (None, b""))
        if not filename:
            raise ValueError("Choose a file first.")
        return (labels[path] + ": " + filename)[:1500]
    form = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    if path in ("/scans/go", "/strategies/go"):
        if form.get("when", ["now"])[0] == "now":
            return None
        label = "Create or change " + ("scan schedule" if path == "/scans/go" else "strategy schedule")
    else:
        label = labels.get(path)
    if label:
        detail = []
        if path in ("/strategies/autoset", "/strategies/auto"):
            detail.append("Automatic runs: " + ("enabled" if form.get("auto", [""])[0] == "on" else "disabled"))
        names = {"name": "Name", "s": "Strategies", "id": "Trade" if path == "/journal/delete" else "Run" if path == "/strategies/results/delete" else "Schedule", "when": "Timing", "date": "Date",
                 "time": "Time", "scan": "Ticker source", "mode": "Price basis", "url": "Link" if path ==
                 "/strategies/tvlink" else "Preset URL", "keep": "Keep", "top_n": "Top per screen"}
        if path in ("/strategies/go", "/strategies/autoset") and form.get("cn"):
            detail.append(f"Candles: {form.get('cn', [''])[0]} {form.get('cu', ['days'])[0]}")
        if path == "/strategies/go":
            for key, field_label in sm.UNIVERSE_FIELDS:
                if form.get(key, [""])[0]:
                    detail.append(field_label + ": " + form[key][0])
        for key in ("name", "s", "id", "when", "date", "time", "scan", "mode", "auto", "url", "keep", "top_n"):
            values = [v for v in form.get(key, []) if v]
            if values and key in names:
                detail.append(names[key] + ": " + ", ".join(values))
        if detail:
            label += " · " + " · ".join(detail)
        return label[:1500]
    return None


def render_change(change=None, message: str = "") -> str:
    e = html.escape
    out = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'><title>Confirm change · Signal desk</title>"
           + _THEME_INIT + f"<style>{_CSS}{_STRAT_CSS}{_LOGIN_CSS}</style></head><body><main>" + render_tabs("")
           + "<section class='card' style='max-width:600px;margin:40px auto;padding:28px'>"
           "<h1>Confirm this change</h1><p class='muted'>No changes have been applied.</p>")
    if message:
        out += f"<p class='warn' role='alert'>{e(message)}</p>"
    if change:
        id = e(change.id, quote=True)
        disabled = "" if change.challenge else " disabled"
        delivery = ("Enter the fresh 6-digit code sent to " if change.challenge
                    else "A fresh 6-digit code is required. Send a code to ")
        out += (f"<div class='notice' style='overflow-wrap:anywhere'>{e(change.label)}</div>"
                f"<p>{delivery}<strong>{e(otp.masked_recipient())}</strong>.</p>"
                "<p class='muted'>This code approves only the change shown above and expires in 5 minutes.</p>"
                f"<form class='login-form' method='post' action='/confirm-change'><input type='hidden' name='id' value='{id}'>"
                "<label>Verification code<input name='code' inputmode='numeric' autocomplete='one-time-code' "
                f"pattern='[0-9]{{6}}' minlength='6' maxlength='6' required autofocus placeholder='000000'{disabled}></label>"
                f"<button type='submit'{disabled}>Verify and apply change</button></form>"
                "<div class='forms'>"
                f"<form method='post' action='/confirm-change/resend'><input type='hidden' name='id' value='{id}'>"
                f"<button type='submit' class='btn2'>{'Resend code' if change.challenge else 'Send code'}</button></form>"
                f"<form method='post' action='/confirm-change/cancel'><input type='hidden' name='id' value='{id}'>"
                "<button type='submit' class='btn2'>Cancel change</button></form></div>"
                "<p class='muted small'>Wait 60 seconds between code requests. A new pending change replaces any earlier one.</p>")
    out += "<p><a href='/strategies'>Back to strategies</a> · <a href='/'>Back to scans</a></p></section></main></body></html>"
    return out


_LOGIN_CSS = """
.login-page{--login-accent:#12604d;--login-soft:#e7f2ed;--login-shadow:0 20px 70px #182c2510;
min-height:100svh;display:flex;flex-direction:column;background:var(--bg)}
:root[data-theme=dark] .login-page{--login-accent:#80dfb4;--login-soft:#22362d;--login-shadow:0 20px 70px #0003}
.login-header,.login-footer{width:100%;max-width:1240px;margin:0 auto;padding:24px 32px;
display:flex;align-items:center;justify-content:space-between;gap:16px}
.login-brand{display:flex;align-items:center;gap:10px;font-weight:750;font-size:19px;letter-spacing:-.5px}
.login-mark{display:grid;place-items:center;width:32px;height:32px;border-radius:8px;
background:var(--login-soft);color:var(--login-accent);font-size:19px}
.login-brand small{font-size:11px;display:block;font-weight:400;letter-spacing:.6px;color:var(--muted)}
.login-main{max-width:1120px;width:calc(100% - 64px);margin:auto;padding:0;display:grid;
grid-template-columns:1.12fr 1fr;background:var(--card);border:1px solid var(--line);
border-radius:22px;overflow:hidden;box-shadow:var(--login-shadow)}
.market-scene{position:relative;overflow:hidden;background:#112421;color:#f0f8f3;padding:45px 42px;
display:flex;flex-direction:column;justify-content:space-between;min-height:600px}
.market-scene:before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse at 70% 70%,#26644755,transparent 60%);pointer-events:none}
.market-copy,.market-chart,.market-caption{position:relative}
.market-eyebrow{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#a8c7b9;display:flex;align-items:center;gap:8px}
.market-eyebrow:before{content:'';width:6px;height:6px;border-radius:50%;background:#8ce0b7}
.market-scene h1{font-size:43px;line-height:1.12;letter-spacing:-1.6px;font-weight:600;margin:24px 0 16px}
.market-scene h1 span{color:#8ce0b7}.market-copy p{color:#acc4b9;font-size:14px;max-width:330px;line-height:1.7;margin:0}
.market-chart{margin:20px -10px 0}.market-chart svg{display:block;width:100%;height:auto}
.market-caption{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.market-caption span{font-size:11px;
padding:5px 9px;border:1px solid #8ab59c40;border-radius:20px;color:#c0d5c9}
.illustration-label{position:relative;color:#91ac9e;font-size:11px;margin:12px 0 0}
.login-content{padding:54px 48px;display:flex;flex-direction:column;justify-content:center}
.login-kicker{color:var(--login-accent);text-transform:uppercase;letter-spacing:1.7px;font-size:11px;font-weight:650;margin:0 0 12px}
.login-content h2{font-size:29px;letter-spacing:-.9px;line-height:1.2;font-weight:650;margin:0 0 12px}
.login-description{color:var(--muted);margin:0 0 30px;font-size:14px;line-height:1.7}
.login-form label{display:block;font-size:13px;font-weight:500;margin-bottom:18px}
.login-form input{display:block;width:100%;font:inherit;font-size:15px;color:var(--ink);background:var(--bg);
border:1px solid var(--line);border-radius:9px;padding:12px 14px;margin-top:7px;min-height:47px}
.login-form input:focus{outline:2px solid var(--login-accent);outline-offset:2px}
.password-wrap{position:relative;display:block}.login-form .password-wrap input{padding-right:65px}
.password-toggle{position:absolute;right:5px;top:5px;bottom:5px;padding:5px 9px;background:transparent;
color:var(--muted);font-size:12px;font-weight:500}
.login-submit{display:flex;align-items:center;justify-content:space-between;width:100%;border-radius:9px;
background:var(--login-accent);color:var(--card);padding:13px 16px;margin-top:8px;font-size:14px;font-weight:600}
.login-help{font-size:12px;color:var(--muted);text-align:center;margin:20px 0 0;line-height:1.7}
.login-notice{border-radius:8px;background:var(--login-soft);color:var(--login-accent);font-size:13px;padding:10px 12px;margin:0 0 18px}
.login-error{background:var(--warnbg);color:var(--warn)}
.login-footer{font-size:11px;color:var(--muted);padding-top:24px;padding-bottom:24px;flex-wrap:wrap}
@media(max-width:800px){.market-scene{padding:32px 26px;min-height:560px}.market-scene h1{font-size:34px}
.login-content{padding:36px 28px}.login-main{width:calc(100% - 32px)}.login-header,.login-footer{padding:20px}}
@media(max-width:600px){.login-main{grid-template-columns:1fr;border-radius:16px}.market-scene{padding:26px;min-height:0}
.market-scene h1{font-size:30px;margin:15px 0 10px}.market-chart,.market-caption,.illustration-label{display:none}
.market-copy p{font-size:13px;max-width:none}.login-content{padding:30px 26px}.login-content h2{font-size:26px}
.login-description{margin-bottom:23px}.login-form input{font-size:16px}.login-header{padding:16px}
.login-brand{font-size:17px}.login-brand small{font-size:10px}.login-header .theme-switch button{padding:6px 8px}
.login-footer{padding:20px 16px;font-size:11px}}
"""


def render_login(message: str = "", signed_out: bool = False, verify: bool = False) -> str:
    # Decorative chart only: no fabricated quotes or claims of live market activity.
    candles = "".join(
        f"<line x1='{x}' y1='{top - 9}' x2='{x}' y2='{bottom + 9}' stroke='{color}'/>"
        f"<rect x='{x - 5}' y='{top}' width='10' height='{bottom - top}' rx='2' fill='{color}'/>"
        for x, top, bottom, color in [
            (20, 169, 195, '#698d7c'), (45, 156, 180, '#8ce0b7'), (70, 167, 187, '#698d7c'),
            (95, 141, 171, '#8ce0b7'), (120, 124, 151, '#8ce0b7'), (145, 136, 154, '#698d7c'),
            (170, 113, 142, '#8ce0b7'), (195, 95, 120, '#8ce0b7'), (220, 104, 126, '#698d7c'),
            (245, 86, 114, '#8ce0b7'), (270, 62, 96, '#8ce0b7'), (295, 74, 95, '#698d7c'),
            (320, 50, 81, '#8ce0b7'), (345, 36, 61, '#8ce0b7'), (370, 44, 67, '#698d7c'),
            (395, 23, 50, '#8ce0b7'), (420, 10, 36, '#8ce0b7')])
    form = ("<h2 id='login-title'>Check your email.</h2><p class='login-description'>Enter the 6-digit code sent to "
            + html.escape(otp.masked_recipient()) + ". It expires in 5 minutes.</p>"
            + (f"<p role='alert' class='login-notice login-error'>{html.escape(message)}</p>" if message else "")
            + "<form class='login-form' method='post' action='/verify'><label>Verification code"
            "<input name='code' inputmode='numeric' autocomplete='one-time-code' pattern='[0-9]{6}' "
            "minlength='6' maxlength='6' placeholder='000000' required autofocus></label>"
            "<button class='login-submit' type='submit'><span>Verify and sign in</span>"
            "<span aria-hidden='true'>→</span></button></form>"
            "<form method='post' action='/verify/resend'><button class='linkbtn' type='submit' "
            "style='padding-top:18px'>Resend code</button></form>"
            "<p class='login-help'>Wait 60 seconds between requests. Check your spam folder.</p>"
            "<p class='login-help'><a href='/login'>Back to sign in</a></p>") if verify else None
    result = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'><title>Sign in · FinViz Agent</title>"
            + _THEME_INIT + f"<style>{_CSS}{_LOGIN_CSS}</style></head><body class='login-page'>"
            "<header class='login-header'><div class='login-brand'><span class='login-mark' aria-hidden='true'>◆</span>"
            "<div>Signal desk<small>FINVIZ AGENT</small></div></div>" + theme_control() + "</header>"
            "<main class='login-main'><section class='market-scene' aria-label='Your market workspace'>"
            "<div class='market-copy'><div class='market-eyebrow'>Your market workspace</div>"
            "<h1>A clearer view.<br><span>A sharper scan.</span></h1>"
            "<p>Your screens, strategies, and signals.<br>One focused place to follow the market.</p></div>"
            "<div><div class='market-chart'><svg viewBox='0 0 440 240' aria-hidden='true' focusable='false'>"
            "<defs><linearGradient id='chart-fill' x1='0' y1='0' x2='0' y2='1'>"
            "<stop stop-color='#8ce0b7' stop-opacity='.12'/><stop offset='1' stop-color='#8ce0b7' stop-opacity='0'/>"
            "</linearGradient></defs><path d='M0 50H440M0 100H440M0 150H440M0 200H440M70 0V240M170 0V240M270 0V240M370 0V240' "
            "stroke='#8ab59c' stroke-opacity='.12' fill='none'/>"
            "<path d='M0 209C60 203 65 181 110 174S174 146 215 145S285 102 322 104S385 61 440 48V240H0Z' "
            "fill='url(#chart-fill)'/>" + candles +
            "<path d='M0 209C60 203 65 181 110 174S174 146 215 145S285 102 322 104S385 61 440 48' "
            "stroke='#c0e9d2' stroke-opacity='.75' stroke-width='2' fill='none'/></svg></div>"
            "<div class='market-caption'><span>Screen the market</span><span>Run your strategies</span>"
            "<span>Review your signals</span></div><p class='illustration-label'>Illustrative market chart</p></div></section>"
            "<section class='login-content' aria-labelledby='login-title'><p class='login-kicker'>Private dashboard</p>"
            "<h2 id='login-title'>Welcome back, Sri.</h2><p class='login-description'>Sign in to pick up where you left off.</p>"
            + ("<p class='login-notice' role='status'>You’ve signed out. See you at the next signal.</p>" if signed_out else "")
            + (f"<p role='alert' class='login-notice login-error'>{html.escape(message)}</p>" if message else "")
            + "<form class='login-form' method='post' action='/login'><label>Username"
            "<input name='username' autocomplete='username' placeholder='Your username' required></label>"
            "<label>Password<span class='password-wrap'><input id='login-password' aria-label='Password' name='password' type='password' "
            "autocomplete='current-password' placeholder='Your password' required>"
            "<button class='password-toggle' type='button' aria-label='Show password' aria-pressed='false' "
            "aria-controls='login-password'>Show</button></span></label>"
            "<button class='login-submit' type='submit'><span>Sign in</span><span aria-hidden='true'>→</span></button>"
            "</form><p class='login-help'>Use your existing dashboard username and password.</p></section></main>"
            "<footer class='login-footer'><span>FinViz Agent · Your personal market workspace</span>"
            "<span>Information only. Not investment advice.</span></footer>"
            "<script>var toggle=document.querySelector('.password-toggle');if(toggle)toggle.addEventListener('click',function(){"
            "var p=document.getElementById('login-password'),show=p.type==='password';p.type=show?'text':'password';"
            "this.textContent=show?'Hide':'Show';this.setAttribute('aria-label',show?'Hide password':'Show password');"
            "this.setAttribute('aria-pressed',String(show));});</script></body></html>")
    if verify:
        start = result.index("<h2 id='login-title'>")
        end = result.index("</section></main>", start)
        result = result[:start] + form + result[end:]
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "FinvizAgent"
    sys_version = ""
    CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; "
           "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", self.CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _session_token(self, name: str = _SESSION_COOKIE) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return cookie[name].value if name in cookie else ""

    def _session_cookie(self, token: str, name: str = _SESSION_COOKIE, age: int = _SESSION_SECONDS) -> str:
        # Railway terminates HTTPS and supplies the original request protocol.
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https" else ""
        return f"{name}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}"

    def _issue_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with _SESSION_LOCK:
            for old in list(_BROWSER_SESSIONS):
                if _BROWSER_SESSIONS[old] <= now:
                    del _BROWSER_SESSIONS[old]
            _BROWSER_SESSIONS[token] = now + _SESSION_SECONDS
        return token

    def _credentials_valid(self, user: str, password: str) -> bool:
        s = self.app.settings
        return (bool(s.password) and hmac.compare_digest(user.encode(), s.user.encode())
                and hmac.compare_digest(password.encode(), s.password.encode()))

    def _authorized(self) -> bool:
        s = self.app.settings
        if not s.password:
            self._text(503, "Locked: set the DASHBOARD_PASSWORD variable to open this page.")
            return False
        token = self._session_token()
        with _SESSION_LOCK:
            valid_session = _BROWSER_SESSIONS.get(token, 0) > time.time()
        if valid_session:
            return True
        header = self.headers.get("Authorization", "")
        # Keep Basic access for scripts. Browsers use sessions so cached Basic credentials
        # cannot silently log the user back in after logout or session expiration.
        browser_request = bool(self.headers.get("Sec-Fetch-Mode")) or "text/html" in self.headers.get("Accept", "")
        if not otp.enabled() and not token and not browser_request and header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                user, pw = "", ""
            if self._credentials_valid(user, pw):
                return True
            time.sleep(0.5)  # slow down password guessing
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": "/login"})
        return False

    def _login_post(self) -> None:
        if not self._same_origin():
            return self._text(403, "Forbidden: request did not come from this page.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._text(400, "Bad request.")
        if length < 0 or length > 16384:
            self.close_connection = True
            return self._text(413, "Request too large.")
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        if self._credentials_valid(form.get("username", [""])[0], form.get("password", [""])[0]):
            if otp.enabled():
                try:
                    token = otp.start(self._session_token("finviz_otp"))
                except otp.OTPError as err:
                    return self._send(503, render_login(str(err)).encode(), "text/html; charset=utf-8")
                return self._send(303, b"", "text/plain; charset=utf-8",
                                  {"Location": "/verify", "Set-Cookie": self._session_cookie(token, "finviz_otp", otp.TTL)})
            with _SESSION_LOCK:
                _BROWSER_SESSIONS.pop(self._session_token(), None)
            token = self._issue_session()
            return self._send(303, b"", "text/plain; charset=utf-8",
                              {"Location": "/", "Set-Cookie": self._session_cookie(token)})
        time.sleep(0.5)
        self._send(401, render_login("Incorrect username or password.").encode(), "text/html; charset=utf-8")

    def _verify_post(self, resend: bool = False) -> None:
        if not self._same_origin():
            return self._text(403, "Forbidden: request did not come from this page.")
        token = self._session_token("finviz_otp")
        if not otp.enabled() or not otp.pending(token):
            return self._send(303, b"", "text/plain; charset=utf-8", {"Location": "/login"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._text(400, "Bad request.")
        if length < 0 or length > 1024:
            self.close_connection = True
            return self._text(413, "Request too large.")
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        try:
            if resend:
                new_token = otp.start(token)
                return self._send(303, b"", "text/plain; charset=utf-8",
                                  {"Location": "/verify", "Set-Cookie": self._session_cookie(new_token, "finviz_otp", otp.TTL)})
            otp.verify(token, form.get("code", [""])[0])
        except otp.OTPError as err:
            return self._send(400, render_login(str(err), verify=True).encode(), "text/html; charset=utf-8")
        with _SESSION_LOCK:
            _BROWSER_SESSIONS.pop(self._session_token(), None)
        session = self._issue_session()
        self._send(303, b"", "text/plain; charset=utf-8",
                   {"Location": "/", "Set-Cookie": self._session_cookie(session)})

    def _begin_change(self, path: str, raw: bytes, label: str) -> None:
        session = self._session_token()
        with _SESSION_LOCK:
            valid = _BROWSER_SESSIONS.get(session, 0) > time.time()
        if not valid:
            return self._send(303, b"", "text/plain; charset=utf-8", {"Location": "/login"})
        change = None
        try:
            if path == "/strategies/restore":
                form = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
                bucket, entry = form.get("bucket", [""])[0], form.get("entry", [""])[0]
                record = self.app.strategies.archive_record(bucket, entry)
                label = ("Restore deleted strategy" if bucket == "trash" else "Restore previous strategy version")
                label += ": " + record["name"] + " · saved " + record["saved_utc"]
            change = _CHANGES.reserve(session, path, raw, self.headers.get("Content-Type", ""), label)
            token = otp.start(purpose="change:" + change.id, description=label)
            if not _CHANGES.attach(change.id, session, token):
                raise ValueError("This request was replaced by a newer change. Start again.")
        except (otp.OTPError, ValueError, OSError) as err:
            active = _CHANGES.get(change.id, session) if change else None
            return self._send(503, render_change(active, str(err)).encode(), "text/html; charset=utf-8")
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": "/confirm-change?id=" + change.id})

    def _change_post(self, path: str) -> None:
        if not self._same_origin():
            return self._text(403, "Forbidden: request did not come from this page.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._text(400, "Bad request.")
        if length < 0 or length > 2048:
            self.close_connection = True
            return self._text(413, "Request too large.")
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        session = self._session_token()
        id = form.get("id", [""])[0]
        change = _CHANGES.get(id, session)
        if not change:
            return self._send(400, render_change(message="This request expired, was cancelled, or belongs to another session. Start again.").encode(),
                              "text/html; charset=utf-8")
        if path.endswith("/cancel"):
            _CHANGES.take(id, session)
            return self._redirect("/strategies" if change.path.startswith("/strategies/") else "/")
        if not change.challenge and not path.endswith("/resend"):
            return self._send(400, render_change(change, "Send a verification code first.").encode(), "text/html; charset=utf-8")
        try:
            if path.endswith("/resend"):
                token = otp.start(previous=change.challenge, purpose="change:" + id, description=change.label)
                if not _CHANGES.attach(id, session, token):
                    raise ValueError("This request is no longer available. Start again.")
                return self._redirect("/confirm-change?id=" + id)
            otp.verify(change.challenge, form.get("code", [""])[0], purpose="change:" + id)
        except (otp.OTPError, ValueError) as err:
            active = change if not change.challenge or otp.pending(change.challenge, purpose="change:" + id) else None
            if not active:
                _CHANGES.take(id, session)
            return self._send(400, render_change(active, str(err)).encode(), "text/html; charset=utf-8")
        # Consume the exact saved request once. Submitted fields cannot alter its target or payload.
        change = _CHANGES.take(id, session)
        if not change:
            return self._send(400, render_change(message="This change was already used or expired.").encode(), "text/html; charset=utf-8")
        self._apply_post(change.path, change.body, change.content_type)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if not origin:
            return True  # non-browser client (e.g. curl); still password-protected
        return urllib.parse.urlsplit(origin).netloc == (self.headers.get("Host") or "")

    def _send_file(self, path: Path, ctype: str, nav: bool = False) -> None:
        root = self.app.settings.data_dir.resolve()
        try:
            real = path.resolve()
            real.relative_to(root)  # never serve anything outside the data folder
        except (OSError, ValueError):
            return self._text(404, "Not found.")
        if not real.is_file():
            return self._text(404, "Not found.")
        body = real.read_bytes()
        if nav and b"<body>" in body:  # links back to the app's tabs on report pages
            body = themed_report(body)
        self._send(200, body, ctype)

    def _redirect(self, location: str) -> None:
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})

    # -- routes -------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    # -- TradingView watchlists (tv_sync.py) ---------------------------------
    def _tv_redirect(self, location: str) -> None:
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})

    def _tradingview_get(self, path: str, query: str) -> None:
        tv = self.app.tradingview
        if path == "/tradingview/connect":
            try:
                url = tv.connect_url(self.headers.get("Host", ""), self.headers.get("X-Forwarded-Proto", ""))
            except tvs.TVError as err:
                tv.set_flash("error", str(err))
                return self._tv_redirect("/tradingview")
            return self._tv_redirect(url)
        if path == "/tradingview/callback":
            q = {k: v[0][:4000] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items() if v}
            try:
                tv.set_flash("ok", tv.finish_connect(q))
            except tvs.TVError as err:
                tv.set_flash("error", str(err))
            return self._tv_redirect("/tradingview")
        g = globals()
        tabs = g["render_tabs"]("tradingview") if callable(g.get("render_tabs")) else ""
        page = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>TradingView · FinViz Agent</title>" + str(g.get("_THEME_INIT", ""))
                + "<style>" + str(g.get("_CSS", "")) + str(g.get("_STRAT_CSS", "")) + str(g.get("_JOURNAL_DESIGN_CSS", "")) + "</style></head><body class='journal-app'><main>"
                + tabs + "<p><a href='/strategies'>← Strategies</a></p>" + tv.page_body()
                + "<p class='muted'>Information only, not investment advice.</p></main></body></html>")
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _tradingview_post(self, path: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= 65536:
            self.rfile.read(length)  # the forms send no data that's needed; read it so the connection stays clean
        if hasattr(self, "_same_origin") and not self._same_origin():
            return self._send(403, b"Forbidden: request did not come from this page.", "text/plain; charset=utf-8")
        tv = self.app.tradingview
        try:
            tv.set_flash("ok", tv.disconnect() if path == "/tradingview/disconnect" else tv.sync_now())
        except tvs.TVError as err:
            tv.set_flash("error", str(err))
        self._tv_redirect("/tradingview")

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        if path == "/healthz":
            return self._text(200, "ok")
        if path == "/login":
            signed_out = urllib.parse.parse_qs(parts.query).get("signed_out") == ["1"]
            return self._send(200, render_login(signed_out=signed_out).encode(), "text/html; charset=utf-8")
        if path == "/verify":
            if not otp.enabled() or not otp.pending(self._session_token("finviz_otp")):
                return self._send(303, b"", "text/plain; charset=utf-8", {"Location": "/login"})
            return self._send(200, render_login(verify=True).encode(), "text/html; charset=utf-8")
        if path == "/paper" or path.startswith("/paper/"):
            from paper_trading.web import authorized_dispatch
            return authorized_dispatch(self, path, urllib.parse.urlsplit(self.path).query)
        if not self._authorized():
            return
        _tv = urllib.parse.urlsplit(self.path)
        if _tv.path in ("/tradingview", "/tradingview/connect", "/tradingview/callback") \
                and getattr(self.app, "tradingview", None) is not None:
            return self._tradingview_get(_tv.path, _tv.query)
        if path == "/confirm-change":
            id = urllib.parse.parse_qs(parts.query).get("id", [""])[0]
            change = _CHANGES.get(id, self._session_token())
            return self._send(200 if change else 400,
                              render_change(change, "" if change else "This change is no longer available. Start again.").encode(),
                              "text/html; charset=utf-8")
        s = self.app.settings
        if path == "/":
            q = urllib.parse.parse_qs(parts.query)
            notice = ("Run started — this page refreshes until it finishes." if "started" in q else
                      "A run is already in progress." if "busy" in q else "")
            return self._send(200, render_dashboard(self.app, notice).encode("utf-8"), "text/html; charset=utf-8")
        m = re.fullmatch(r"/runs/([^/]+)/([^/]+)", path)
        if m:
            stamp, name = m.groups()
            if STAMP_RE.match(stamp) and name in RUN_FILES:
                return self._send_file(s.reports_dir / stamp / name, RUN_FILES[name], nav=name == "report.html")
            return self._text(404, "Not found.")
        if path == "/latest/report.html":
            return self._send_file(s.reports_dir / "latest_report.html", RUN_FILES["report.html"], nav=True)
        if path == "/notebook":
            return self._html(200, render_notebook(self.app, saved="saved" in urllib.parse.parse_qs(parts.query)))
        if path == "/notes":
            q = {k:v[0] for k,v in urllib.parse.parse_qs(parts.query).items() if v}
            return self._html(200, render_notes(self.app, q))
        if path == "/journal" or path.startswith("/journal/"):
            return self._journal_get(path, parts.query)
        st = self.app.strategies
        if st is not None:
            if path == "/strategies/backtests" and os.environ.get("VBT_SERVICE_URL") and os.environ.get("VBT_SERVICE_TOKEN"):
                try:
                    jobs = backtests.service("/jobs")
                except ValueError as exc:
                    return self._text(503, str(exc))
                links = "".join(f"<li><a href='/strategies/backtests/{html.escape(j['id'])}'>"
                                f"{html.escape(j['strategy'])} · {html.escape(j['symbol'])}</a> · "
                                f"{html.escape(j['status'])} · {html.escape(j['created'])}</li>" for j in jobs)
                return self._html(200, "<!doctype html><meta charset='utf-8'><title>Backtest history</title>"
                                  "<main><a href='/strategies'>Strategies</a><h1>VectorBT PRO backtests</h1>"
                                  "<p>Open a completed daily strategy result to start a backtest.</p><ul>"+links+"</ul></main>")
            match = re.fullmatch(r"/strategies/backtests/([0-9a-f-]{36})", path)
            if match and os.environ.get("VBT_SERVICE_URL") and os.environ.get("VBT_SERVICE_TOKEN"):
                try:
                    job = backtests.service("/jobs/" + match[1])
                    return self._html(200, backtests.result_html(job, path))
                except ValueError as exc:
                    return self._text(503, str(exc))
            if path == "/strategies/archive":
                return self._send(200, render_archive(self.app).encode(), "text/html; charset=utf-8")
            if path == "/strategies":
                return self._send(200, render_strategies(self.app).encode("utf-8"), "text/html; charset=utf-8")
            m = re.fullmatch(r"/strategies/results/([^/]+)/?([^/]*)", path)
            if m:
                rid, name = m.groups()
                if not sm.RESULT_RE.match(rid):
                    return self._text(404, "Not found.")
                if not name:
                    page = render_result(self.app, rid)
                    return (self._send(200, page.encode("utf-8"), "text/html; charset=utf-8") if page
                            else self._text(404, "Not found."))
                d = st.results_dir / rid
                if name == SIGNAL_TXT:  # made from signals.csv each time it is opened
                    if not sm.regular_file(d / "signals.csv", d):
                        return self._text(404, "Not found.")
                    body = (",".join(signal_tickers(d)) + "\n").encode("utf-8")
                    return self._send(200, body, "text/plain; charset=utf-8",
                                      {"Content-Disposition": f"inline; filename=\"{rid}_signals.txt\""})
                ctype = sm.result_file_type(name)
                if ctype and sm.regular_file(d / name, d):
                    return self._send_file(d / name, ctype)
                return self._text(404, "Not found.")
        if path == "/latest/tickers.txt":
            return self._send_file(s.reports_dir / "latest_tickers.txt", RUN_FILES["tickers.txt"])
        m = re.fullmatch(r"/logs/([^/]+)", path)
        if m and LOG_RE.match(urllib.parse.unquote(m.group(1))):
            return self._send_file(s.logs_dir / urllib.parse.unquote(m.group(1)), "text/plain; charset=utf-8")
        return self._text(404, "Not found.")

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self._text(405, "Method not allowed.")
        if path == "/login":
            return self._login_post()
        if path in ("/verify", "/verify/resend"):
            return self._verify_post(resend=path.endswith("/resend"))
        if path == "/paper" or path.startswith("/paper/"):
            from paper_trading.web import authorized_dispatch
            return authorized_dispatch(self, path, urllib.parse.urlsplit(self.path).query)
        if not self._authorized():
            return
        _tv = urllib.parse.urlsplit(self.path)
        if _tv.path in ("/tradingview/sync", "/tradingview/disconnect") \
                and getattr(self.app, "tradingview", None) is not None:
            return self._tradingview_post(_tv.path)
        if path in ("/confirm-change", "/confirm-change/resend", "/confirm-change/cancel"):
            return self._change_post(path)
        if path == "/logout":
            if not self._same_origin():
                return self._text(403, "Forbidden: request did not come from this page.")
            with _SESSION_LOCK:
                _BROWSER_SESSIONS.pop(self._session_token(), None)
            otp.cancel(self._session_token("finviz_otp"))
            _CHANGES.cancel_session(self._session_token())
            # This marker also blocks credentials cached by the browser's old Basic login.
            return self._send(303, b"", "text/plain; charset=utf-8",
                              {"Location": "/login?signed_out=1", "Set-Cookie": self._session_cookie("logged-out")})
        upload_routes = ("/strategies/upload", "/strategies/mcap", "/journal/import")
        form_routes = ("/notebook/save", "/notes/save", "/run", "/scans/go", "/scans/schedule/cancel", "/presets/add", "/presets/remove", "/presets/keep",
                       "/presets/topn",
                       "/strategies/delete",
                       "/strategies/backtest", "/strategies/results/delete", "/strategies/tvlink", "/strategies/cancel", "/strategies/note", "/strategies/run",
                       "/strategies/auto", "/strategies/mcap/delete", "/strategies/prices/update",
                       "/strategies/prices/test", "/strategies/prices/test-finviz",
                       "/strategies/go", "/strategies/autoset", "/strategies/schedule/cancel", "/strategies/restore",
                       "/journal/add", "/journal/edit", "/journal/notes", "/journal/delete", "/journal/clear")
        if path not in upload_routes + form_routes:
            return self._text(404, "Not found.")
        if not self._same_origin():
            return self._text(403, "Forbidden: request did not come from this page.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._text(400, "Bad request.")
        limit = 160000 if path == "/notebook/save" else 12_000_000 if path in upload_routes else (65536 if path == "/notes/save" else 16384)
        if length < 0 or length > limit:
            self.close_connection = True
            return self._text(413, "Request too large.")
        raw = self.rfile.read(length) if length else b""
        try:
            label = protected_change_label(path, raw, self.headers.get("Content-Type", ""))
        except ValueError as err:
            return self._send(400, render_change(message=str(err)).encode(), "text/html; charset=utf-8")
        if label:
            return self._begin_change(path, raw, label)
        return self._apply_post(path, raw)

    def _apply_post(self, path: str, raw: bytes, content_type: Optional[str] = None) -> None:
        if path == "/notebook/save":
            form = {k:v[0] for k,v in urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True).items()}
            try:
                self.app.notebook.save(form.get("text", ""), form.get("revision", ""))
            except (nb.NotebookError, OSError) as err:
                return self._html(400, render_notebook(self.app, submitted=form, error=str(err)))
            return self._redirect("/notebook?saved=1")
        if path == "/notes/save":
            form = {k:v[0] for k,v in urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True).items()}
            try:
                ident = self.app.notes.save(form)
            except (tn.NoteError, OSError) as err:
                return self._html(400, render_notes(self.app, {}, form, str(err)))
            return self._redirect("/notes?id=" + urllib.parse.quote(ident))
        if path.startswith("/strategies/"):
            return self._strategies_post(path, raw, content_type)
        if path.startswith("/journal/"):
            return self._journal_post(path, raw, content_type)
        body = raw.decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}

        if path == "/run":
            started = self.app.runner.start("manual")
            return self._send(303, b"", "text/plain; charset=utf-8",
                              {"Location": "/?started=1" if started else "/?busy=1"})
        if path in ("/scans/go", "/scans/schedule/cancel"):
            try:
                if path == "/scans/go":
                    message = self._scan_go(form)
                else:
                    message = self._scan_cancel(form.get("id", ""))
                self.app.set_flash("ok", message, "scans")
            except (ValueError, OSError) as err:
                self.app.set_flash("error", str(err), "scans")
            return self._redirect("/#scan")

        store = self.app.presets
        if store is None:
            return self._text(404, "Not found.")
        try:
            if path == "/presets/topn":
                message = store.set_top_n(form.get("top_n", ""))
            elif path == "/presets/add":
                message = store.add(form.get("name", ""), form.get("url", ""), form.get("keep", ""))
            else:
                try:
                    index = int(form.get("index", "-1"))
                except ValueError:
                    index = -1
                if path == "/presets/keep":
                    message = store.set_keep(index, form.get("name", ""), form.get("keep", ""))
                else:
                    message = store.remove(index, form.get("name", ""))
            self.app.set_flash("ok", message)
        except (ValueError, fa.AgentError, OSError) as err:
            self.app.set_flash("error", str(err))
        self.app.refresh_config()
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": "/#presets"})

    # -- journal ------------------------------------------------------------
    def _html(self, status: int, page: str) -> None:
        self._send(status, page.encode("utf-8"), "text/html; charset=utf-8")

    def _journal_get(self, path: str, query: str) -> None:
        app = self.app
        if app.journal is None:
            return self._text(404, "Not found.")
        q = {k: v[0][:200] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items() if v}
        if path in ("/journal", "/journal/"):
            return self._html(200, render_journal(app, q))
        if path == "/journal/trades":
            return self._html(200, render_journal_trades(app, q))
        if path == "/journal/add":
            return self._html(200, render_journal_add(app, q))
        if path == "/journal/import":
            return self._html(200, render_journal_import(app))
        if path == "/journal/trade":
            try:
                page = render_journal_trade(app, q.get("id", ""))
            except jn.JournalError as err:
                return self._text(500, str(err))
            return self._html(200, page) if page else self._text(404, "That trade no longer exists.")
        if path == "/journal/export.csv":
            try:
                body = app.journal.export_csv().encode("utf-8-sig")
            except jn.JournalError as err:
                return self._text(500, str(err))
            stamp = dt.datetime.now(dt.timezone.utc).astimezone(app.settings.tz).strftime("%Y-%m-%d")
            return self._send(200, body, "text/csv; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename=\"journal_{stamp}.csv\""})
        return self._text(404, "Not found.")

    def _journal_post(self, path: str, raw: bytes, content_type: Optional[str] = None) -> None:
        app, store = self.app, self.app.journal
        if store is None:
            return self._text(404, "Not found.")
        if path == "/journal/import":
            try:
                files = parse_multipart(content_type if content_type is not None
                                        else self.headers.get("Content-Type", ""), raw)
                filename, content = files.get("file", (None, b""))
                if not filename:
                    raise jn.JournalError("Choose a CSV file first.")
                r = store.import_csv(filename, content)
                msg = (f"Imported {filename} ({r['broker']} format): {r['added']} new execution"
                       f"{'s' if r['added'] != 1 else ''} added"
                       + (f", {r['duplicates']} already in the journal" if r["duplicates"] else "")
                       + (f", {r['skipped']} rows skipped" if r["skipped"] else "") + ". "
                       + (f"Skipped, for example: {'; '.join(r['skipped_examples'][:3])}." if r["skipped"] else ""))
                app.set_flash("ok", msg.strip(), "journal")
            except (jn.JournalError, OSError) as err:
                app.set_flash("error", str(err), "journal")
                return self._redirect("/journal/import")
            return self._redirect("/journal/trades")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"),
                                                          keep_blank_values=True).items()}
        tid = form.get("id", "")
        try:
            if path == "/journal/add":
                rec = store.add_manual(form)
                app.set_flash("ok", f"Saved the {rec['symbol']} trade.", "journal")
                return self._redirect(f"/journal/trade?id={rec['id']}")
            if path == "/journal/edit":
                store.update_manual(tid, form)
                app.set_flash("ok", "Trade updated.", "journal")
                return self._redirect(f"/journal/trade?id={urllib.parse.quote(tid)}")
            if path == "/journal/notes":
                store.save_notes(tid, form)
                app.set_flash("ok", "Notes saved.", "journal")
                return self._redirect(f"/journal/trade?id={urllib.parse.quote(tid)}")
            if path == "/journal/delete":
                app.set_flash("ok", store.delete(tid), "journal")
                return self._redirect("/journal/trades")
            if path == "/journal/clear":
                app.set_flash("ok", store.clear(), "journal")
                return self._redirect("/journal/import")
        except jn.JournalError as err:
            if path == "/journal/add":
                return self._html(400, render_journal_add(app, {}, values=form, error=str(err)))
            if path == "/journal/edit" and store.trade(tid):
                return self._html(400, render_journal_trade(app, tid, error=str(err), values=form) or "")
            if path in ("/journal/notes", "/journal/delete") and store.trade(tid):
                return self._html(400, render_journal_trade(app, tid, error=str(err)) or "")
            app.set_flash("error", str(err), "journal")
            return self._redirect("/journal/import" if path == "/journal/clear" else "/journal/trades")
        except OSError as err:
            app.set_flash("error", f"Couldn't save: {err}", "journal")
            return self._redirect("/journal/trades")
        return self._text(404, "Not found.")

    def _scan_go(self, form: Dict[str, str]) -> str:
        """Scans tab: Run now, Schedule for later, or Every weekday."""
        app = self.app
        when = form.get("when", "now")
        if when == "now":
            if not app.runner.start("manual"):
                raise ValueError("A scan is already running. It will appear under All runs when it finishes.")
            return "Scan started. This page refreshes until it finishes."
        if when not in ("once", "weekdays"):
            raise ValueError("Unknown schedule type.")
        if app.schedules is None:
            raise ValueError("Scheduling is not available.")
        job = app.schedules.add([], TICKER_FRESH, "close", when, form.get("date", ""), form.get("time", ""),
                                now=app.clock(), kind="scan")
        return "Scheduled: " + _scan_job_text(app, job) + "."

    def _scan_cancel(self, job_id: str) -> str:
        if self.app.schedules is None or not any(j["id"] == job_id for j in self.app.schedules.jobs("scan")):
            raise ValueError("That schedule no longer exists. Reload the page.")
        j = self.app.schedules.cancel(job_id)
        return "Scheduled scan cancelled." if j["status"] == "cancelled" else "Removed."

    def _go(self, picked: List[str], form: Dict[str, str], times: Optional[List[str]] = None) -> str:
        """Run now, once later, or every weekday — the selected strategies on one ticker source.
        Later/weekday runs can have several times: each becomes its own scheduled run."""
        st, app = self.app.strategies, self.app
        scan, mode, when = form.get("scan", "latest"), form.get("mode", "close"), form.get("when", "now")
        known = {n for n, _ in st.list()}
        picked = [n for n in dict.fromkeys(picked) if n in known]
        if not picked:
            raise ValueError("Select at least one strategy.")
        if mode not in ("close", "intraday"):
            raise ValueError("Unknown price basis.")
        if not (scan in ("latest", TICKER_FRESH) or STAMP_RE.match(scan)):
            raise ValueError("Unknown ticker source.")
        candles = sm.candles_spec(form.get("cn", "1"), form.get("cu", "days"))
        filters = sm.universe_filters(form)
        st.save_selection(picked, scan, mode, when, candles, filters)
        n = len(picked)
        who = f"{n} strateg{'y' if n == 1 else 'ies'}"
        if when == "now":
            if scan == TICKER_FRESH:
                job = {"id": "now", "names": picked, "scan": scan, "mode": mode, "candles": candles, "filters": filters}
                threading.Thread(target=sched.dispatch, args=(job, st.enqueue_many, lambda: run_fresh_scan(app),
                                                              lambda *_: None, lambda m: None), daemon=True).start()
                return f"Running a new FinViz scan first, then {who}. This page updates as they run."
            problems = (st.enqueue_many(picked, scan, mode, "manual", candles, filters=filters)
                        if filters else st.enqueue_many(picked, scan, mode, "manual", candles))
            if len(problems) == n:
                raise ValueError(problems[0])
            return (f"{who} queued" + (f" ({sm.candles_label(candles)} candles)" if candles != "1d" else "") + "."
                    + (f" ({len(problems)} skipped: {problems[0]})" if problems else ""))
        if when not in ("once", "weekdays"):
            raise ValueError("Unknown schedule type.")
        jobs = app.schedules.add_many(picked, scan, mode, when, form.get("date", ""),
                                      times if times is not None else [form.get("time", "")], now=app.clock(),
                                      candles=candles, **({"filters": filters} if filters else {}))
        if len(jobs) == 1:
            return "Scheduled: " + _job_text(app, jobs[0]) + "."
        first = app.schedules.next_fire(jobs[0])
        zone = f" {first.astimezone(app.settings.tz):%Z}" if first else ""
        day = ("every weekday" if when == "weekdays"
               else f"{dt.datetime.fromisoformat(jobs[0]['at_utc']).astimezone(app.settings.tz):%a %b %d}")
        src = {"latest": "latest scan", TICKER_FRESH: "new scan first"}.get(scan, f"scan {_stamp_label(scan)}")
        return (f"Scheduled {len(jobs)} runs: {who} · {day} at {', '.join(j['time'] for j in jobs)}{zone} · {src} · "
                f"{'intraday' if mode == 'intraday' else 'latest close'}. Each time is listed below with its own Cancel.")

    def _strategies_post(self, path: str, raw: bytes, content_type: Optional[str] = None) -> None:
        st = self.app.strategies
        if st is None:
            return self._text(404, "Not found.")
        anchor = ""
        try:
            if path in ("/strategies/upload", "/strategies/mcap"):
                files = parse_multipart(content_type if content_type is not None else self.headers.get("Content-Type", ""), raw)
                filename, content = files.get("file", (None, b""))
                if not filename:
                    raise ValueError("Choose a file first.")
                if path == "/strategies/upload":
                    message, anchor = st.upload_strategy(filename, content), "#strategies"
                else:
                    message, anchor = st.upload_mcap(content), "#data"
            else:
                multi = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
                form = {k: v[0] for k, v in multi.items()}
                name = form.get("name", "")
                picked = [n for n in multi.get("s", []) if n]
                if path == "/strategies/backtest":
                    if not os.environ.get("VBT_SERVICE_URL") or not os.environ.get("VBT_SERVICE_TOKEN"):
                        raise ValueError("VectorBT PRO is not configured.")
                    data = backtests.dataset(st.results_dir, form.get("rid", ""), form.get("symbol", "").upper().strip(),
                        [int(h.strip()) for h in form.get("holds", "").split(",")], float(form.get("fee", "0")), signal_tickers, st,
                        int(form.get("sessions", "60")) if form.get("source") == "replay" else None)
                    job = backtests.service("/jobs", data)
                    return self._redirect("/strategies/backtests/" + job["id"])
                if path == "/strategies/go":
                    message, anchor = self._go(picked, form, multi.get("time", [])), "#activity"
                elif path == "/strategies/autoset":
                    candles = sm.candles_spec(form.get("cn", "1"), form.get("cu", "days"))
                    st.save_selection(picked, form.get("scan", "latest"), form.get("mode", "close"),
                                      form.get("when", "now"), candles)
                    message = st.set_auto_set(picked, form.get("auto") == "on", form.get("mode", "close"), candles)
                    anchor = "#go"
                elif path == "/strategies/schedule/cancel":
                    j = self.app.schedules.cancel(form.get("id", ""))
                    message = "Schedule cancelled." if j["status"] == "cancelled" else "Removed."
                    anchor = "#activity"
                elif path == "/strategies/results/delete":
                    message, anchor = st.delete_result(form.get("id", "")), "#activity"
                elif path == "/strategies/cancel":
                    message, anchor = st.cancel(form.get("id", "")), "#activity"
                elif path == "/strategies/note":
                    message, anchor = st.set_note(name, form.get("note", "")), "#strategies"
                elif path == "/strategies/tvlink":
                    message, anchor = st.set_tv_link(name, form.get("url", "")), "#strategies"
                elif path == "/strategies/delete":
                    message, anchor = st.delete_strategy(name), "#strategies"
                    sched_jobs = [j for j in (self.app.schedules.jobs("strategies") if self.app.schedules else [])
                                  if j["status"] == "scheduled" and name in j["names"]]
                    if sched_jobs:
                        one = len(sched_jobs) == 1
                        message += (f" {len(sched_jobs)} scheduled run{'' if one else 's'} still "
                                    f"list{'s' if one else ''} it and will skip it; you can cancel or re-create "
                                    f"{'it' if one else 'them'} under Run activity.")
                elif path == "/strategies/restore":
                    message = st.restore_strategy(form.get("bucket", ""), form.get("entry", ""))
                    self.app.set_flash("ok", message, "archive")
                    return self._redirect("/strategies/archive")
                elif path == "/strategies/run":
                    message, anchor = st.enqueue(name, form.get("scan", "latest"), form.get("mode", "close"),
                                                 "manual"), "#results"
                elif path == "/strategies/auto":
                    message, anchor = st.set_auto(name, form.get("auto") == "on", form.get("mode", "close")), \
                        "#strategies"
                elif path == "/strategies/mcap/delete":
                    message, anchor = st.delete_mcap(), "#data"
                elif path == "/strategies/prices/test-finviz":
                    if st.finviz is None:
                        raise ValueError("FINVIZ_API_TOKEN is not set.")
                    result = st.finviz.test()
                    if result.startswith("❌"):
                        raise ValueError(result)
                    message, anchor = result, "#data"
                elif path == "/strategies/prices/test":
                    if st.client is None:
                        raise ValueError("MASSIVE_API_KEY is not set. Add it in Railway → Variables, then Deploy.")
                    result = st.client.test()
                    if result.startswith("❌"):
                        raise ValueError(result)
                    message, anchor = result, "#data"
                else:  # /strategies/prices/update
                    if st.client is None:
                        raise ValueError("Add MASSIVE_API_KEY in Railway → Variables first.")
                    message = ("Price update started." if st.update_prices_async()
                               else "A price update is already running.")
                    anchor = "#data"
            section = {"#activity": "results", "#go": "strategies", "#results": "results",
                       "#data": "data"}.get(anchor, "strategies")
            self.app.set_flash("ok", message, section)
        except (ValueError, OSError) as err:
            if path == "/strategies/restore":
                self.app.set_flash("error", str(err), "archive")
                return self._redirect("/strategies/archive")
            section = {"/strategies/upload": "strategies", "/strategies/mcap": "data", "/strategies/go": "strategies",
                       "/strategies/autoset": "strategies", "/strategies/schedule/cancel": "results",
                       "/strategies/mcap/delete": "data", "/strategies/prices/update": "data",
                       "/strategies/prices/test": "data", "/strategies/prices/test-finviz": "data",
                       "/strategies/run": "strategies"}.get(path, "strategies")
            self.app.set_flash("error", str(err), section)
            anchor = "#" + section
        self._redirect("/strategies" + anchor)


# --------------------------------------------------------------------------
# Journal tab
# --------------------------------------------------------------------------
_JOURNAL_CSS = """
.jhead{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.jacts{display:flex;gap:8px;flex-wrap:wrap}.btnlink{display:inline-block;background:var(--btn);color:var(--btnink);
border-radius:8px;padding:8px 14px;font-weight:600;font-size:14px}.btnlink:hover{text-decoration:none;opacity:.92}
.btnlink.alt{background:transparent;color:var(--accent);border:1px solid var(--line)}
.jsub{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:14px 0 4px;overflow-x:auto}
.jsub a{padding:6px 12px;border-bottom:2px solid transparent;color:var(--muted);font-weight:600;font-size:14px;
white-space:nowrap}.jsub a.on{color:var(--ink);border-bottom-color:var(--accent)}.jsub a:hover{text-decoration:none}
.jfilter{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:flex-end;margin:12px 0}
.jfilter label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);text-transform:uppercase;
letter-spacing:.03em;gap:3px}.jfilter input,.jfilter select{font:inherit;font-size:14px;color:var(--ink);
background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 8px;min-width:0}
.jfilter input[type=date]{width:150px}.jfilter input.sym{width:90px;text-transform:uppercase}
.jfilter .clear{font-size:14px;align-self:center}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kpi .v{font-size:20px;font-variant-numeric:tabular-nums}.kpi .s{font-size:12.5px;color:var(--muted)}
.money{font-variant-numeric:tabular-nums;white-space:nowrap}
.eq{width:100%;height:auto;display:block}.eq .ln{fill:none;stroke:var(--accent);stroke-width:2}
.eq .ar{fill:var(--accent);opacity:.08}.eq .zero{stroke:var(--muted);stroke-dasharray:4 4;stroke-width:1}
.eq{height:200px}.eqtop{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin-bottom:6px}.eqtop strong{color:var(--ink)}.eqtop strong.ok{color:var(--up)}.eqtop strong.bad{color:var(--down)}.eqdates{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:var(--muted);margin-top:4px}
.cal{display:grid;grid-template-columns:repeat(7,minmax(0,1fr)) minmax(0,1.1fr);gap:4px;font-size:13px}
.cal .h{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;text-align:center;padding:2px}
.cal .d{min-height:62px;border:1px solid var(--line);border-radius:8px;padding:4px 6px;background:var(--card);
display:flex;flex-direction:column;gap:1px;color:var(--ink)}.cal .d.out{opacity:.35}
.cal a.d:hover{text-decoration:none;border-color:var(--accent)}.cal .dn{font-size:11px;color:var(--muted)}
.cal .p{font-weight:700;font-variant-numeric:tabular-nums;font-size:12.5px;overflow:hidden;white-space:nowrap}.cal .p .short{display:none}.cal .c{font-size:11px;color:var(--muted)}
.cal .d.win{background:color-mix(in srgb,var(--up) 12%,var(--card))}
.cal .d.loss{background:color-mix(in srgb,var(--down) 12%,var(--card))}
.cal .wk{border-style:dashed}.calnav{display:flex;align-items:center;gap:12px;margin:6px 0 10px;flex-wrap:wrap}
.calnav strong{font-size:16px}
.bd{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.bd .card{margin:0}.bd h3{font-size:14px;margin:0 0 6px}.bd table{font-size:13px}.bd td,.bd th{padding:5px 6px}.bd th{white-space:nowrap}.bd td.n{white-space:nowrap}
.bar{height:4px;border-radius:2px;background:var(--line);overflow:hidden;margin-top:4px}.bd td.bn{min-width:6.5em;overflow-wrap:break-word}
.bar i{display:block;height:100%}.bar i.ok{background:var(--up)}.bar i.bad{background:var(--down)}
.jt td,.jt th{white-space:nowrap}.jt td.wrapc{white-space:normal;min-width:120px}
.jt tr.click:hover td{background:color-mix(in srgb,var(--accent) 6%,transparent)}
.chip{display:inline-block;font-size:12px;border:1px solid var(--line);border-radius:999px;padding:0 7px;
margin:1px 3px 1px 0;color:var(--muted)}.chip.m{border-color:var(--down);color:var(--down)}
.side{font-size:12px;font-weight:700;letter-spacing:.03em}.side.long{color:var(--up)}.side.short{color:var(--down)}
.jform{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:8px}.jform .half{grid-column:span 2}.jform .sec{grid-column:1/-1;font-size:13px;font-weight:700;color:var(--ink);text-transform:none;letter-spacing:0;margin:6px 0 -4px;border-top:1px solid var(--line);padding-top:12px}.jform .sec:first-child{border-top:0;padding-top:0;margin-top:0}@media (max-width:640px){.jform{grid-template-columns:repeat(2,minmax(0,1fr))}.jform .half{grid-column:1/-1}}
.jform label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted);text-transform:uppercase;
letter-spacing:.03em}.jform input,.jform select,.jform textarea{font:inherit;font-size:14px;color:var(--ink);
background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;text-transform:none;
letter-spacing:0}.jform .wide{grid-column:1/-1}.jform textarea{min-height:110px;resize:vertical}
.jform input:focus,.jform select:focus,.jform textarea:focus{outline:2px solid var(--accent);outline-offset:1px}
.formact{display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap}
.hint{font-size:13px;color:var(--muted);margin:4px 0 0}fieldset{border:0;padding:0;margin:0}
.facts2{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:10px 0}
.facts2>div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px}.facts2 .hint{font-weight:400;margin:0}
.danger{background:transparent;color:var(--down);border:1px solid var(--line)}
.empty-j{text-align:center;padding:36px 16px}.empty-j p{max-width:520px;margin:8px auto}
@media (max-width:640px){.cal{grid-template-columns:repeat(7,minmax(0,1fr));gap:3px}.cal .wk,.cal .hw{display:none}
.cal .d{min-height:46px;padding:3px}.cal .p{font-size:10.5px;letter-spacing:-.02em}.cal .p .full{display:none}.cal .p .short{display:inline}.cal .c{display:none}.kpi .v{font-size:18px}}
"""

_JOURNAL_SUB = (("overview", "/journal", "Overview"), ("trades", "/journal/trades", "Trades & Portfolio"),
                ("add", "/journal/add", "Add trade"), ("import", "/journal/import", "Import"))

_JOURNAL_DESIGN_CSS = """
.journal-app{--bg:#f3f6f8;--card:#ffffff;--ink:#182c37;--muted:#657985;--line:#dfe7eb;
--accent:#137761;--btn:#147c65;--btnink:#fff;--up:#12805c;--down:#c74b59;--jsoft:#e8f4ee;--jshadow:0 4px 22px #14344805;
background:var(--bg)}
:root[data-theme=dark] .journal-app{--bg:#10191f;--card:#18242c;--ink:#e7f0f3;--muted:#96acb9;--line:#2a3a44;
--accent:#83dfc0;--btn:#83dfc0;--btnink:#10251f;--up:#76d7b2;--down:#f38c98;--jsoft:#203a34;--jshadow:none}
.journal-app main{max-width:1280px;padding:26px 30px 48px}.journal-app .app-nav{gap:20px;margin-bottom:26px}
.journal-app .tabs a{font-size:14px;padding:10px 13px}.journal-app .tabs a.on{color:var(--accent)}
.journal-app .account-controls{gap:10px}.journal-app .welcome{color:var(--muted);font-size:12px}
.journal-app .theme-switch{background:var(--card)}.journal-app .theme-switch button{font-size:12px}
.journal-app .logout-form button{font-size:12px}.journal-app .jsub{display:inline-flex;gap:4px;padding:5px;
border:1px solid var(--line);background:var(--card);border-radius:11px;margin:0 0 28px;max-width:100%}
.journal-app .jsub a{border:0;border-radius:7px;padding:7px 15px;font-size:13px;font-weight:500}
.journal-app .jsub a.on{background:var(--jsoft);color:var(--accent)}
.journal-app .jhead{margin-bottom:24px;align-items:center;gap:18px}.journal-app .jhead h1{font-size:34px;
letter-spacing:-1.2px;line-height:1.2;margin:4px 0 8px}.journal-app .jhead .muted{font-size:14px}
.journal-app .jeyebrow{font-size:11px;letter-spacing:1.7px;text-transform:uppercase;color:var(--accent);font-weight:600}
.journal-app .btnlink{padding:10px 16px;border-radius:9px;font-size:13px;box-shadow:var(--jshadow)}
.journal-app .btnlink.alt{background:var(--card);color:var(--ink)}.journal-app h2{font-size:18px;letter-spacing:-.3px}
.journal-app .card{border-radius:15px;padding:22px;border-color:var(--line);box-shadow:var(--jshadow)}
.journal-app .jfilter{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:15px 18px;
gap:12px;margin:0 0 20px}.journal-app .jfilter label{font-size:11px;text-transform:none;letter-spacing:0;gap:6px}
.journal-app .jfilter input,.journal-app .jfilter select{background:var(--bg);font-size:13px;padding:8px 10px}
.journal-app .jfilter button{padding:8px 18px;font-size:13px}.journal-app .jfilter select{max-width:200px}
.journal-app .kpis{grid-template-columns:repeat(4,minmax(0,1fr));gap:13px;margin:20px 0 26px}
.journal-app .kpi{padding:20px;border-radius:13px;box-shadow:var(--jshadow)}
.journal-app .kpi:first-child{background:var(--jsoft);border-color:color-mix(in srgb,var(--accent) 20%,var(--line))}
.journal-app .kpi .k{font-size:11px;text-transform:none;letter-spacing:0;margin-bottom:10px;color:var(--muted)}
.journal-app .kpi .v{font-size:25px;letter-spacing:-.7px;font-weight:600;overflow-wrap:anywhere}
.journal-app .kpi .s{font-size:11px;margin-top:7px;line-height:1.6}
.journal-panels{display:grid;grid-template-columns:1fr 1.3fr;gap:20px;align-items:stretch;margin:20px 0 30px}
.journal-panels>.card{margin:0;min-width:0}.journal-section-head{display:flex;justify-content:space-between;gap:12px;
align-items:center;margin-bottom:20px}.journal-section-head h2{margin:0}.journal-section-head span{font-size:11px;color:var(--muted)}
.journal-app .eq{height:260px;margin-top:16px}.journal-app .eq .ln{stroke-width:2.5}.journal-app .eq .ar{opacity:.1}
.journal-app .eqtop{gap:16px;font-size:11px;padding-bottom:12px;border-bottom:1px solid var(--line)}
.journal-app .eqtop strong{display:block;font-size:17px;margin-top:5px}.journal-app .eqdates{font-size:11px}
.journal-app .cal{gap:5px}.journal-app .cal .h{font-size:11px;text-transform:none;letter-spacing:0;padding-bottom:7px}
.journal-app .cal .d{min-height:66px;padding:6px;border-radius:8px;border-color:color-mix(in srgb,var(--line) 65%,transparent)}
.journal-app .cal .d.win{background:color-mix(in srgb,var(--up) 11%,var(--card))}
.journal-app .cal .d.loss{background:color-mix(in srgb,var(--down) 10%,var(--card))}
.journal-app .cal .p .full{display:none}.journal-app .cal .p .short{display:inline}
.journal-app .cal .p{font-size:12px}.journal-app .cal .dn{font-size:11px}.journal-app .cal .c{font-size:11px}
.journal-app .calnav{justify-content:center;margin-bottom:16px;font-size:13px}.journal-app .calnav strong{font-size:15px}
.journal-app .hint{font-size:12px;line-height:1.6}.journal-app .bd{grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.journal-app .bd h3{font-size:14px;margin-bottom:16px}.journal-app .bd td,.journal-app .bd th{padding:10px 6px}
.journal-app th{font-size:11px;text-transform:none;letter-spacing:0}.journal-app td{padding-top:12px;padding-bottom:12px}
.journal-app .jt{font-size:13px}.journal-app .jt th{background:var(--bg);padding-top:12px;padding-bottom:12px}
.journal-app .chip{border:0;background:var(--bg);padding:3px 8px;font-size:11px}
.journal-app .jform label{font-size:11px;text-transform:none;letter-spacing:0;gap:7px}
.journal-app .jform input,.journal-app .jform select,.journal-app .jform textarea{padding:11px 12px;font-size:14px}
.journal-app .empty-j{padding:46px 24px;border-style:solid;margin:0;background:var(--card)}
.journal-app .empty-j h2{font-size:25px;letter-spacing:-.6px;margin:20px 0 12px}
.journal-app .empty-j p{color:var(--muted);font-size:14px;line-height:1.8;max-width:510px}
.empty-market-art{display:flex;gap:7px;align-items:flex-end;justify-content:center;height:84px;margin:0 auto 12px;
width:144px;padding:14px 15px 8px;border-radius:18px;background:var(--jsoft)}
.empty-market-art span{display:block;width:12px;border-radius:3px;background:var(--accent);opacity:.8;position:relative}
.empty-market-art span:nth-child(2n){opacity:.35}.empty-market-art span:before{content:'';position:absolute;left:5px;top:-5px;
bottom:-5px;width:1px;background:var(--accent)}
.journal-start{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:20px}
.journal-start a{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:20px;color:var(--ink)}
.journal-start a:hover{text-decoration:none;border-color:var(--accent)}.journal-start .step{color:var(--accent);font-size:12px}
.journal-start strong{display:block;font-size:14px;font-weight:600;margin:10px 0 6px}.journal-start p{font-size:12px;line-height:1.7;
color:var(--muted);margin:0}.journal-app .danger{color:var(--down)}
@media(max-width:1050px){.journal-panels{grid-template-columns:1fr}.journal-app .cal .p .full{display:inline}
.journal-app .cal .p .short{display:none}.journal-app .bd{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:700px){.journal-app main{padding:18px 16px 36px}.journal-app .app-nav{gap:12px}
.journal-app .app-nav .tabs{flex-basis:100%}.journal-app .tabs a{font-size:12px;padding:8px}.journal-app .account-controls{margin-left:0;width:100%;display:grid;grid-template-columns:1fr auto;gap:9px}
.journal-app .account-controls .welcome{grid-column:1/-1}.journal-app .account-controls .theme-switch{margin-left:0;justify-self:start}
.journal-app .jhead h1{font-size:28px}.journal-app .jhead{margin-bottom:20px}.journal-app .jsub{margin-bottom:22px}
.journal-app .jsub{display:flex;width:100%}.journal-app .jsub a{padding:7px 6px;flex:1;text-align:center;font-size:12px}.journal-app .kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.journal-app .kpi{padding:16px}.journal-app .kpi .v{font-size:22px}.journal-app .card{padding:17px}
.journal-app .bd,.journal-start{grid-template-columns:1fr}.journal-app .jfilter{gap:12px;padding:15px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}
.journal-app .jfilter label:last-of-type{grid-column:1/-1}.journal-app .jfilter button{justify-self:start}
.journal-app .jfilter label{flex:1 1 110px}.journal-app .jfilter input[type=date],.journal-app .jfilter input.sym,
.journal-app .jfilter select{width:100%;max-width:100%}.journal-app .jfilter input,.journal-app .jfilter select,
.journal-app .jform input,.journal-app .jform select,.journal-app .jform textarea{font-size:16px}
.journal-app .cal .p .full{display:none}.journal-app .cal .p .short{display:inline}.journal-app .cal .d{padding:4px;min-height:52px}
.journal-app .cal{gap:3px}.journal-app .cal .p{font-size:11px;letter-spacing:-.3px}.journal-app .cal .d{padding:3px}
.journal-app .cal .c{display:none}.journal-app .empty-j{padding:30px 20px}.journal-start{gap:10px}
.journal-start a{padding:17px}.journal-start strong{margin:6px 0}.journal-app .journal-section-head span{max-width:140px;text-align:right}}
@media(max-width:380px){.journal-app .kpi .v{font-size:18px}}
"""
_FILTER_KEYS = ("from", "to", "symbol", "side", "status", "result", "strategy", "setup", "tag", "day")


def _money(v: Optional[float], sign: bool = True) -> str:
    if v is None:
        return "—"
    if abs(v) < 0.005:
        return "$0.00"
    return f"{('+' if v > 0 else '-') if sign else ('-' if v < 0 else '')}${abs(v):,.2f}"


def _price(v: Optional[float]) -> str:
    """Share price: at least 2 decimals, up to 4 when needed ($61.20, $1.2345)."""
    if v is None:
        return "—"
    txt = f"{v:,.4f}"
    whole, frac = txt.split(".")
    frac = frac.rstrip("0")
    return f"{whole}.{frac.ljust(2, '0')}"


def _short_money(v: float) -> str:
    """Compact dollar amount for small calendar cells: +85, -1.2k."""
    a = abs(v)
    body = f"{a / 1000:.1f}k" if a >= 1000 else f"{a:.0f}"
    return ("+" if v > 0.005 else "-" if v < -0.005 else "") + body


def _pnl_cls(v: Optional[float]) -> str:
    return "" if v is None or abs(v) < 0.005 else "ok" if v > 0 else "bad"


def _qs(q: Dict[str, str], **change) -> str:
    merged = {k: v for k, v in {**q, **change}.items() if v not in (None, "")}
    return ("?" + urllib.parse.urlencode(merged)) if merged else ""


def _journal_page(title: str, active: str, body: List[str]) -> str:
    sub = "".join(f"<a href='{href}' class='{'on' if k == active else ''}'"
                  + (" aria-current='page'" if k == active else "") + f">{label}</a>"
                  for k, href, label in _JOURNAL_SUB)
    return "\n".join(["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
                      "<meta name='viewport' content='width=device-width,initial-scale=1'>",
                      f"<title>{html.escape(title)} · Journal</title>",
                      _THEME_INIT + f"<style>{_CSS}{_JOURNAL_CSS}{_JOURNAL_DESIGN_CSS}</style></head><body class='journal-app'><main>",
                      render_tabs("journal"), f"<nav class='jsub' aria-label='Journal'>{sub}</nav>"] + body
                     + ["<p class='muted' style='font-size:13px;margin-top:28px'>Your own records. "
                        "Information only, not investment advice.</p></main>",
                        "<script>(function(){document.addEventListener('submit',function(e){var f=e.target;"
                        "if(f.dataset.confirm&&!confirm(f.dataset.confirm))e.preventDefault();});"
                        "document.querySelectorAll('tr[data-href]').forEach(function(r){r.addEventListener('click',"
                        "function(e){if(e.target.closest('a,button'))return;location.href=r.dataset.href;});});"
                        "})();</script></body></html>"])


def _journal_choices(app: App, trades: List[dict]) -> Tuple[List[str], List[str], List[str]]:
    strategies = {t["strategy"] for t in trades if t["strategy"]}
    if app.strategies is not None:
        strategies |= {n for n, _ in app.strategies.list()}
    setups = sorted({t["setup"] for t in trades if t["setup"]}, key=str.lower)
    tags = sorted({x for t in trades for x in t["tags"] + t["mistakes"]}, key=str.lower)
    return sorted(strategies, key=str.lower), setups, tags


def _filter_form(action: str, q: Dict[str, str], strategies: List[str], keep: Sequence[str] = ()) -> str:
    e = html.escape
    hidden = "".join(f"<input type='hidden' name='{k}' value='{e(q[k], quote=True)}'>" for k in keep if q.get(k) and not (k == "status" and action == "/journal/trades"))
    opts = "".join(f"<option value='{e(s, quote=True)}'{' selected' if q.get('strategy') == s else ''}>{e(s)}"
                   "</option>" for s in strategies)
    side = "".join(f"<option value='{v}'{' selected' if q.get('side') == v else ''}>{t}</option>"
                   for v, t in (("", "Any"), ("long", "Long"), ("short", "Short")))
    status_filter = ""
    if action == "/journal/trades":
        choices = "".join("<option value='" + value + "'" + (" selected" if q.get('status','') == value else "") + ">" + label + "</option>"
                          for value,label in (("","All trades"),("open","Open Trades"),("closed","Closed Trades")))
        status_filter = "<label>Status<select name='status'>" + choices + "</select></label>"
    active = any(q.get(k) for k in _FILTER_KEYS)
    return (f"<form class='jfilter' method='get' action='{action}' role='search' aria-label='Filter trades'>{hidden}"
            f"<label>From<input type='date' name='from' value='{e(q.get('from', ''), quote=True)}'></label>"
            f"<label>To<input type='date' name='to' value='{e(q.get('to', ''), quote=True)}'></label>"
            f"<label>Symbol<input class='sym' name='symbol' maxlength='10' value='{e(q.get('symbol', ''), quote=True)}'"
            " autocomplete='off'></label>"
            f"<label>Side<select name='side'>{side}</select></label>"
            f"<label>Strategy<select name='strategy'><option value=''>Any</option>{opts}</select></label>"
            + status_filter + "<button type='submit'>Apply</button>"
            + (f"<a class='clear' href='{action}'>Clear filters</a>" if active else "") + "</form>")


def _equity_svg(curve: List[Tuple[str, float]]) -> str:
    if len(curve) < 2:
        return "<p class='muted'>The equity curve appears once you have two closed trades.</p>"
    W, H, L, R, T, B = 900, 220, 2, 2, 8, 8
    vals = [0.0] + [v for _, v in curve]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        hi, lo = hi + 1, lo - 1
    n = len(vals) - 1
    xs = [L + (W - L - R) * i / n for i in range(len(vals))]
    ys = [T + (H - T - B) * (hi - v) / (hi - lo) for v in vals]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    zero_y = T + (H - T - B) * hi / (hi - lo)
    area = f"{xs[0]:.1f},{zero_y:.1f} {pts} {xs[-1]:.1f},{zero_y:.1f}"
    last = vals[-1]
    label = (f"Cumulative net P&L over {n} closed trades, from {curve[0][0][:10]} to {curve[-1][0][:10]}: "
             f"ends at {_money(last)}, highest {_money(hi)}, lowest {_money(lo)}.")
    e = html.escape
    return (f"<div class='eqtop'><span>Now <strong class='money {_pnl_cls(last)}'>{e(_money(last))}</strong></span>"
            f"<span>High <strong class='money'>{e(_money(hi))}</strong></span>"
            f"<span>Low <strong class='money'>{e(_money(lo))}</strong></span></div>"
            f"<svg class='eq' viewBox='0 0 {W} {H}' preserveAspectRatio='none' role='img' "
            f"aria-label='{e(label, quote=True)}'>"
            f"<polygon class='ar' points='{area}'/>"
            f"<line class='zero' x1='{L}' x2='{W - R}' y1='{zero_y:.1f}' y2='{zero_y:.1f}'/>"
            f"<polyline class='ln' points='{pts}' vector-effect='non-scaling-stroke'/></svg>"
            f"<div class='eqdates'><span>{e(curve[0][0][:10])}</span><span>dashed line = $0</span>"
            f"<span>{e(curve[-1][0][:10])}</span></div>")


def _calendar(trades: List[dict], month: str, q: Dict[str, str]) -> str:
    import calendar as _cal
    e = html.escape
    days = jn.daily(trades)
    try:
        first = dt.date.fromisoformat(month + "-01")
    except ValueError:
        first = dt.date.today().replace(day=1)
    prev_m = (first - dt.timedelta(days=1)).strftime("%Y-%m")
    next_m = (first + dt.timedelta(days=32)).replace(day=1).strftime("%Y-%m")
    month_days = {d: v for d, v in days.items() if d.startswith(first.strftime("%Y-%m"))}
    m_net = sum(v[0] for v in month_days.values())
    m_n = sum(v[1] for v in month_days.values())
    green = sum(1 for v in month_days.values() if v[0] > 0.005)
    red = sum(1 for v in month_days.values() if v[0] < -0.005)
    out = [f"<div class='calnav'><a href='/journal{e(_qs(q, month=prev_m))}#calendar' aria-label='Previous month'>←</a>"
           f"<strong>{first:%B %Y}</strong>"
           f"<a href='/journal{e(_qs(q, month=next_m))}#calendar' aria-label='Next month'>→</a>"
           f"<span class='money {_pnl_cls(m_net)}'>{e(_money(m_net))}</span>"
           f"<span class='muted' style='font-size:13px'>{m_n} trade{'s' if m_n != 1 else ''} · "
           f"{green} green day{'s' if green != 1 else ''} · {red} red day{'s' if red != 1 else ''}</span></div>",
           "<div class='cal'>" + "".join(f"<div class='h'>{d}</div>" for d in jn.WEEKDAYS)
           + "<div class='h hw'>Week</div>"]
    for week in _cal.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        w_net, w_n = 0.0, 0
        for d in week:
            iso = d.isoformat()
            p, n = days.get(iso, (None, 0))
            inside = d.month == first.month
            if inside and n:
                w_net, w_n = w_net + p, w_n + n
            cls = "d" + ("" if inside else " out") + (" win" if inside and n and p > 0.005 else
                                                       " loss" if inside and n and p < -0.005 else "")
            shown = n and inside
            inner = (f"<span class='dn'>{d.day}</span>"
                     + (f"<span class='p {_pnl_cls(p)}'><span class='full'>{e(_money(p))}</span>"
                        f"<span class='short'>{e(_short_money(p))}</span></span>"
                        f"<span class='c'>{n} trade{'s' if n != 1 else ''}</span>" if shown else ""))
            if n and inside:
                out.append(f"<a class='{cls}' href='/journal/trades?day={iso}' "
                           f"aria-label='{d:%b %d}: {e(_money(p), quote=True)}, {n} trades'>{inner}</a>")
            else:
                out.append(f"<div class='{cls}'>{inner}</div>")
        out.append(f"<div class='d wk'><span class='dn'>Week</span>"
                   + (f"<span class='p {_pnl_cls(w_net)}'>{e(_money(w_net))}</span><span class='c'>{w_n} trades</span>"
                      if w_n else "") + "</div>")
    out.append("</div>")
    return "".join(out)


def _breakdown_card(title: str, rows: List[dict], link_key: Optional[str] = None, limit: int = 12) -> str:
    e = html.escape
    if not rows:
        return ""
    top = max(abs(r["net"]) for r in rows) or 1.0
    body = []
    for r in rows[:limit]:
        name = e(r["name"])
        if link_key:
            name = f"<a href='/journal/trades?{urllib.parse.urlencode({link_key: r['name']})}'>{name}</a>"
        width = abs(r["net"]) / top * 100
        win = "—" if r["win_rate"] is None else f"{r['win_rate']:.0f}%"
        body.append(f"<tr><td class='bn'>{name}<div class='bar'><i class='{_pnl_cls(r['net']) or 'ok'}' "
                    f"style='width:{width:.1f}%'></i></div></td><td class='n'>{r['trades']}</td>"
                    f"<td class='n'>{win}</td>"
                    f"<td class='n money {_pnl_cls(r['net'])}'>{e(_money(r['net']))}</td></tr>")
    more = f"<p class='hint'>Top {limit} of {len(rows)} by net P&amp;L.</p>" if len(rows) > limit else ""
    return (f"<div class='card'><h3>{e(title)}</h3><div class='wrap'><table><thead><tr><th>Name</th>"
            "<th class='n' title='Trades'>#</th><th class='n'>Win</th><th class='n'>Net P&amp;L</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>{more}</div>")


def render_journal(app: App, q: Dict[str, str]) -> str:
    q = dict(q)
    today = app.clock().astimezone(app.settings.tz).date()
    q.setdefault("from", today.replace(day=1).isoformat())
    q.setdefault("to", today.isoformat())
    e = html.escape
    store = app.journal
    out = ["<div class='jhead'><div><div class='jeyebrow'>Trading performance</div><h1>Journal</h1>"
           "<div class='muted'>Track your trades. Understand your edge.</div></div>"
           "<div class='jacts'><a class='btnlink' href='/journal/add'>+ Add trade</a>"
           "<a class='btnlink alt' href='/journal/import'>Import CSV</a>"
           "<a class='btnlink alt' href='/journal/export.csv'>Export CSV</a></div></div>",
           _flash_html(app, "journal")]
    try:
        all_trades = store.trades()
    except jn.JournalError as err:
        out.append(f"<div class='warn'>{e(str(err))}</div>")
        return _journal_page("Overview", "overview", out)
    if not all_trades:
        out.append("<div class='card empty-j'><div class='empty-market-art' aria-hidden='true'>"
                   "<span style='height:17px'></span><span style='height:27px'></span><span style='height:23px'></span>"
                   "<span style='height:36px'></span><span style='height:43px'></span><span style='height:53px'></span></div>"
                   "<h2>No trades yet</h2>"
                   "<p>Import your broker's trade history as a CSV file, add a trade by hand, or use "
                   "<strong>Log trade</strong> next to a BUY signal on a strategy result.</p>"
                   "<p class='formact' style='justify-content:center'><a class='btnlink' href='/journal/import'>"
                   "Import CSV</a><a class='btnlink alt' href='/journal/add'>Add a trade</a></p></div>"
                   "<div class='journal-start'><a href='/journal/import'><span class='step'>01 · IMPORT</span>"
                   "<strong>Bring your history together →</strong><p>Upload your broker CSV to review your executions and completed trades.</p></a>"
                   "<a href='/journal/add'><span class='step'>02 · RECORD</span><strong>Capture the complete trade →</strong>"
                   "<p>Record entries, exits, fees, setups, and the lessons you want to remember.</p></a>"
                   "<a href='/strategies'><span class='step'>03 · CONNECT</span><strong>From signal to trading journal →</strong>"
                   "<p>Use Log trade on a strategy signal to prefill your next journal entry.</p></a></div>")
        out.append(jc.render([]))
        return _journal_page("Overview", "overview", out)
    strategies, _, _ = _journal_choices(app, all_trades)
    out.append(_filter_form("/journal", q, strategies, keep=("month",)))
    trades = jn.filter_trades(all_trades, q)
    s = jn.stats(trades)
    if q and any(q.get(k) for k in _FILTER_KEYS):
        out.append(f"<p class='hint'>Showing {len(trades)} of {len(all_trades)} trades.</p>")
    wr = "—" if s["win_rate"] is None else f"{s['win_rate']:.1f}%"
    pf = "—" if s["profit_factor"] is None else ("∞" if s["profit_factor"] > 1e9 else f"{s['profit_factor']:.2f}")
    kp = [("Net P&L", f"<span class='money {_pnl_cls(s['net'])}'>{e(_money(s['net']))}</span>",
           f"after {e(_money(s['fees'], sign=False))} fees"),
          ("Win rate", wr, f"{s['wins']} wins · {s['losses']} losses" + (f" · {s['even']} even" if s["even"] else "")),
          ("Profit factor", pf, "gross profit ÷ gross loss"),
          ("Expectancy", f"<span class='money {_pnl_cls(s['expectancy'])}'>{e(_money(s['expectancy']))}</span>",
           "average net per trade"),
          ("Avg win / loss", f"<span class='money ok'>{e(_money(s['avg_win']))}</span>",
           f"<span class='money bad'>{e(_money(s['avg_loss']))}</span>"
           + (f" · ratio {s['payoff']:.2f}" if s["payoff"] else "")),
          ("Max drawdown", f"<span class='money {'bad' if s['max_drawdown'] > 0.005 else ''}'>"
           f"{e(_money(-s['max_drawdown'] if s['max_drawdown'] > 0.005 else 0))}</span>", "largest drop from a peak"),
          ("Trades", f"{s['trades']:,}", f"{s['open']} open" + f" · avg hold {jn.fmt_hold(s['avg_hold_sec'])}"),
          ("Best / worst", f"<span class='money ok'>{e(_money(s['best']))}</span>",
           f"<span class='money bad'>{e(_money(s['worst']))}</span> · streaks {s['streak_win']}W / "
           f"{s['streak_loss']}L")]
    out.append("<div class='kpis'>" + "".join(
        f"<div class='kpi'><div class='k'>{k}</div><div class='v'>{v}</div><div class='s'>{sub}</div></div>"
        for k, v, sub in kp) + "</div>")
    out.append("<div class='journal-panels'><section class='card'><div class='journal-section-head'>"
               "<h2>Equity curve</h2><span>Cumulative net performance</span></div>" + _equity_svg(s["curve"]) + "</section>")
    days = sorted(jn.daily(trades))
    month = q.get("month") or (days[-1][:7] if days else dt.date.today().strftime("%Y-%m"))
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        month = dt.date.today().strftime("%Y-%m")
    out.append("<section class='card'><div class='journal-section-head'><h2 id='calendar'>P&amp;L calendar</h2>"
               "<span>Your trading days, at a glance</span></div>" + _calendar(trades, month, q)
               + "<p class='hint'>Each closed trade counts on the day it was closed. Click a day to see its trades."
               "</p></section></div>")
    cards = [_breakdown_card("By strategy", jn.breakdown(trades, lambda t: t["strategy"]), "strategy"),
             _breakdown_card("By setup", jn.breakdown(trades, lambda t: t["setup"]), "setup"),
             _breakdown_card("By tag", jn.breakdown(trades, lambda t: t["tags"]), "tag"),
             _breakdown_card("Mistakes", jn.breakdown(trades, lambda t: t["mistakes"]), "tag"),
             _breakdown_card("By symbol", jn.breakdown(trades, lambda t: t["symbol"]), "symbol"),
             _breakdown_card("Long vs short", jn.breakdown(trades, lambda t: t["side"].title())),
             _breakdown_card("By weekday (entry)", sorted(jn.breakdown(trades, jn.weekday_of),
                                                          key=lambda r: jn.WEEKDAYS.index(r["name"]))),
             _breakdown_card("By hour (entry)", sorted(jn.breakdown(trades, jn.hour_of), key=lambda r: r["name"])),
             _breakdown_card("By holding time", sorted(jn.breakdown(trades, jn.hold_bucket),
                                                       key=lambda r: jn.HOLD_ORDER.index(r["name"])))]
    cards = [c for c in cards if c]
    if cards:
        out.append("<h2>Breakdowns</h2><div class='bd'>" + "".join(cards) + "</div>")
    out.append(jc.render(trades))
    if not any(t["strategy"] for t in trades):
        out.append("<p class='hint'>Tip: set a trade's <strong>Strategy</strong> (or use Log trade on a BUY signal) "
                   "to see which of your strategies make money.</p>")
    return _journal_page("Overview", "overview", out)


def render_journal_trades(app: App, q: Dict[str, str]) -> str:
    e = html.escape
    q = dict(q)
    q.setdefault("status", "open")
    if not q.get("day"):
        today = app.clock().astimezone(app.settings.tz).date()
        q.setdefault("from", (today - dt.timedelta(days=40)).isoformat())
        q.setdefault("to", today.isoformat())
    out = ["<div class='jhead'><h1>Trades &amp; Portfolio</h1><div class='jacts'><a class='btnlink' href='/journal/add'>"
           "+ Add trade</a><a class='btnlink alt' href='/journal/export.csv'>Export CSV</a></div></div>",
           _flash_html(app, "journal")]
    try:
        all_trades = app.journal.trades()
    except jn.JournalError as err:
        out.append(f"<div class='warn'>{e(str(err))}</div>")
        return _journal_page("Trades & Portfolio", "trades", out)
    strategies, _, _ = _journal_choices(app, all_trades)
    out.append(_filter_form("/journal/trades", q, strategies, keep=("status", "result", "tag", "setup", "day", "sort", "direction")))
    trades = jn.filter_trades(all_trades, q)
    quotes, updating = app.open_marks.snapshot(trades) if app.open_marks else ({}, False)
    for t in trades:
        t["today_pct"] = quotes.get(t["symbol"], {}).get("today_pct")
        t["display_pnl"], t["display_return"] = otm.valuation(t, quotes.get(t["symbol"])) if t["status"] == "open" else (t["pnl"], t["return_pct"])
    sort = q.get("sort", "entry")
    direction = q.get("direction", "desc" if sort == "entry" else "asc")
    keys = {"today_pct": lambda t:t["today_pct"], "sold": lambda t:t.get("exit_time"), "entry": lambda t:t["entry_time"], "symbol": lambda t:t["symbol"].casefold(),
            "side": lambda t:t["side"], "shares": lambda t:t["qty"], "entry_price": lambda t:t["entry_price"],
            "exit_price": lambda t:t["exit_price"], "pnl": lambda t:t["display_pnl"], "return": lambda t:t["display_return"],
            "labels": lambda t:" ".join([t["strategy"],t["setup"]]+t["tags"]+t["mistakes"]).casefold(),
            "account": lambda t:t.get("account_suffix") or None}
    if sort not in keys:
        sort = "entry"
    key = keys[sort]
    present = [t for t in trades if key(t) is not None]
    missing = [t for t in trades if key(t) is None]
    trades = sorted(present, key=key, reverse=direction == "desc") + missing
    def heading(name, label, numeric=False):
        next_dir = "asc" if sort == name and direction == "desc" else "desc" if sort == name else "asc"
        params = dict(q, sort=name, direction=next_dir)
        state = ("descending" if direction == "desc" else "ascending") if sort == name else "none"
        arrow = " ↓" if state == "descending" else " ↑" if state == "ascending" else " ↕"
        return ("<th" + (" class='n'" if numeric else "") + " aria-sort='" + state + "'><a href='/journal/trades?"
                + e(urllib.parse.urlencode(params), quote=True) + "' style='color:inherit' aria-label='Sort by "
                + e(label,quote=True) + "'>" + e(label) + arrow + "</a></th>")
    extra = [f"{k}: {q[k]}" for k in ("day", "tag", "setup", "status", "result") if q.get(k)]
    s = jn.stats(trades)
    out.append(f"<p class='hint'>{len(trades)} trade{'s' if len(trades) != 1 else ''}"
               + (f" ({e(', '.join(extra))})" if extra else "")
               + f" · net <span class='money {_pnl_cls(s['net'])}'>{e(_money(s['net']))}</span>"
               + (f" · win rate {s['win_rate']:.0f}%" if s["win_rate"] is not None else "") + "</p>")
    if not trades:
        out.append("<div class='card empty-j'><p>No trades match.</p></div>")
        return _journal_page("Trades & Portfolio", "trades", out)
    if trades:
        out.append("<script>setTimeout(function(){location.reload();}," + ("5000" if updating else "60000") + ");</script>")
    rows = []
    for t in trades[:2000]:
        href = f"/journal/trade?id={t['id']}"
        chips = "".join(f"<span class='chip'>{e(x)}</span>" for x in t["tags"]) + "".join(
            f"<span class='chip m'>{e(x)}</span>" for x in t["mistakes"])
        label = " · ".join(x for x in (t["strategy"], t["setup"]) if x)
        shares = f"{t['qty']:,.0f}" if t["qty"] % 1 == 0 else f"{t['qty']:,.4f}".rstrip("0")
        exit_txt = "open" if t["status"] == "open" else _price(t["exit_price"])
        mark = quotes.get(t["symbol"]) if t["status"] == "open" else None
        mark_note = ""
        if t["status"] == "open":
            if mark:
                fetched = dt.datetime.fromtimestamp(mark['fetched'],dt.timezone.utc).astimezone(app.settings.tz).strftime('%b %d %H:%M %Z')
                mark_note = ("<div class='hint' style='font-size:10px;white-space:normal'>" + ("Stale · " if mark['stale'] else "")
                             + "Fetched " + e(fetched) + "</div>")
            else:
                mark_note = "<div class='hint' style='font-size:10px'>" + ("Loading quote…" if updating else "Finviz quote unavailable") + "</div>"
        today_mark = quotes.get(t["symbol"])
        today_title = ("Latest available Finviz session " + today_mark['day'] + " versus previous trading close"
                       + (" · stale quote" if today_mark.get('stale') else "")) if today_mark else "Finviz quote unavailable"
        today_value = "—" if t["today_pct"] is None else f"{t['today_pct']:+.2f}%"
        rows.append(
            f"<tr class='click' data-href='{href}'><td><a href='{href}'>{e(t['entry_time'][:16])}</a></td>"
            f"<td><strong>{e(t['symbol'])}</strong></td>"
            f"<td class='n {_pnl_cls(t['today_pct'])}' title='{e(today_title, quote=True)}'>{today_value}</td>"
            f"<td class='n'>{shares}</td><td class='n'>{_price(t['entry_price'])}</td><td class='n'>{exit_txt}</td>"
            f"<td class='n money {_pnl_cls(t['display_pnl'])}'>{e(_money(t['display_pnl']))}{mark_note}</td>"
            f"<td class='n {_pnl_cls(t['display_return'])}'>"
            + ("—" if t["display_return"] is None else f"{t['display_return']:+.2f}%") + "</td>"
            f"<td class='wrapc'>{e(label)}{'<br>' if label and chips else ''}{chips}</td>"
            f"<td>{e((t.get('exit_time') or '—')[:10])}</td>"
            f"<td>{('•••• ' + e(t.get('account_suffix',''))) if t.get('account_suffix') else '—'}</td></tr>")
    columns = [("entry","Entry",False),("symbol","Symbol",False),("today_pct","Today %",True),
               ("shares","Shares",True),("entry_price","Entry $",True),("exit_price","Exit $",True),
               ("pnl","Net P&L",True),("return","Return",True),("labels","Strategy · setup · tags",False),
               ("sold","Sold",False),("account","Account",False)]
    out.append("<div class='card wrap'><table class='jt'><thead><tr>" + "".join(heading(*c) for c in columns)
               + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    if len(trades) > 2000:
        out.append(f"<p class='hint'>Showing the first 2,000 in this sort order of {len(trades):,}. Narrow the dates or export the CSV."
                   "</p>")
    return _journal_page("Trades & Portfolio", "trades", out)


def _datalist(id_: str, values: List[str]) -> str:
    return f"<datalist id='{id_}'>" + "".join(f"<option value='{html.escape(v, quote=True)}'>" for v in values) \
        + "</datalist>"


def _notes_fields(t: Dict[str, object], strategies: List[str], setups: List[str], tags: List[str]) -> str:
    e = html.escape
    return (f"<div class='sec'>Review</div><label class='half'>Strategy<input name='strategy' list='dl-strat' maxlength='80' "
            f"value='{e(str(t.get('strategy') or ''), quote=True)}'></label>"
            f"<label class='half'>Setup<input name='setup' list='dl-setup' maxlength='60' placeholder='e.g. Breakout' "
            f"value='{e(str(t.get('setup') or ''), quote=True)}'></label>"
            f"<label class='wide'>Tags<input name='tags' list='dl-tags' maxlength='500' placeholder='Comma-separated, "
            f"e.g. gap up, earnings' value='{e(', '.join(t.get('tags') or []), quote=True)}'></label>"
            f"<label class='wide'>Mistakes<input name='mistakes' maxlength='500' placeholder='Comma-separated, "
            f"e.g. chased entry, no stop' value='{e(', '.join(t.get('mistakes') or []), quote=True)}'></label>"
            f"<label class='wide'>Notes<textarea name='notes' maxlength='5000'>{e(str(t.get('notes') or ''))}"
            "</textarea></label>" + _datalist("dl-strat", strategies) + _datalist("dl-setup", setups)
            + _datalist("dl-tags", tags))


def _trade_fields(v: Dict[str, str]) -> str:
    e = html.escape

    def inp(name, label, typ="text", extra=""):
        return (f"<label>{label}<input type='{typ}' name='{name}' value='{e(v.get(name, ''), quote=True)}' "
                f"{extra}></label>")
    side = "".join(f"<option value='{k}'{' selected' if v.get('side', 'long') == k else ''}>{t}</option>"
                   for k, t in (("long", "Long (buy first)"), ("short", "Short (sell first)")))
    return ("<div class='sec'>Trade</div><label>Symbol<input name='symbol' required maxlength='10' autocomplete='off' "
            f"style='text-transform:uppercase' value='{e(v.get('symbol', ''), quote=True)}'></label>"
            f"<label>Side<select name='side'>{side}</select></label>"
            + inp("qty", "Shares", "number", "required min='0' step='any'")
            + inp("fees", "Fees ($)", "number", "min='0' step='any' placeholder='0'")
            + "<div class='sec'>Entry</div>"
            + inp("entry_date", "Date", "date", "required")
            + inp("entry_time", "Time (ET, optional)", "time", "step='1'")
            + inp("entry_price", "Price ($)", "number", "required min='0' step='any'")
            + "<span></span><div class='sec'>Exit <span class='muted' style='font-weight:400'>· leave empty while "
            "the trade is open</span></div>"
            + inp("exit_date", "Date", "date")
            + inp("exit_time", "Time (ET, optional)", "time", "step='1'")
            + inp("exit_price", "Price ($)", "number", "min='0' step='any'") + "<span></span>")


def render_journal_add(app: App, q: Dict[str, str], values: Optional[Dict[str, str]] = None,
                       error: str = "") -> str:
    e = html.escape
    v = dict(values or {})
    if values is None:  # prefill from the Log trade link
        v = {"symbol": jn.clean_symbol(q.get("symbol", ""))[:10], "strategy": q.get("strategy", "")[:80],
             "entry_price": q.get("price", "")[:20] if jn.parse_number(q.get("price")) else "",
             "entry_date": q.get("date", "")[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", q.get("date", "")[:10])
             else dt.datetime.now(dt.timezone.utc).astimezone(app.settings.tz).date().isoformat(),
             "side": "long"}
    try:
        trades = app.journal.trades()
    except jn.JournalError:
        trades = []
    strategies, setups, tags = _journal_choices(app, trades)
    note_vals = {"strategy": v.get("strategy", ""), "setup": v.get("setup", ""),
                 "tags": jn.parse_tags(v.get("tags", "")), "mistakes": jn.parse_tags(v.get("mistakes", "")),
                 "notes": v.get("notes", "")}
    out = ["<h1>Add trade</h1>"]
    if q.get("strategy") and values is None:
        out.append(f"<p class='hint'>From a {e(q['strategy'])} BUY signal. The entry price is the signal close; "
                   "change it to your actual fill.</p>")
    if error:
        out.append(f"<div class='warn' role='alert'>{e(error)}</div>")
    out.append("<form method='post' action='/journal/add' class='card'><div class='jform'>" + _trade_fields(v)
               + _notes_fields(note_vals, strategies, setups, tags)
               + "</div><div class='formact'><button type='submit'>Save trade</button>"
               "<a href='/journal/trades'>Cancel</a></div></form>")
    return _journal_page("Add trade", "add", out)


def render_journal_trade(app: App, trade_id: str, error: str = "", values: Optional[Dict[str, str]] = None) -> Optional[str]:
    e = html.escape
    store = app.journal
    t = store.trade(trade_id)
    if t is None:
        return None
    trades = store.trades()
    strategies, setups, tags = _journal_choices(app, trades)
    status = ("Open" + (f" · {t['open_qty']:g} shares" if t["open_qty"] else "")) if t["status"] == "open" \
        else ("Win" if t["pnl"] > 0.005 else "Loss" if t["pnl"] < -0.005 else "Break-even")
    hold = jn.fmt_hold((dt.datetime.fromisoformat(t["exit_time"]) - dt.datetime.fromisoformat(t["entry_time"]))
                       .total_seconds()) if t["status"] == "closed" and t["has_time"] else "—"
    out = [f"<p><a href='/journal/trades'>← Trades &amp; Portfolio</a></p>",
           f"<div class='jhead'><div><h1>{e(t['symbol'])} <span class='side {t['side']}'>{t['side'].upper()}</span>"
           f"</h1><div class='muted'>{e(status)} · {'entered by hand' if t['source'] == 'manual' else 'imported'}"
           "</div></div>"
           f"<div class='jacts'><a class='btnlink alt' href='https://finviz.com/quote.ashx?t={urllib.parse.quote(t['symbol'])}' "
           "target='_blank' rel='noopener noreferrer'>FinViz chart ↗</a></div></div>",
           _flash_html(app, "journal")]
    if error:
        out.append(f"<div class='warn' role='alert'>{e(error)}</div>")
    facts = [("Net P&L", f"<span class='money {_pnl_cls(t['pnl'])}'>{e(_money(t['pnl']))}</span>"),
             ("Return", "—" if t["return_pct"] is None else f"<span class='{_pnl_cls(t['return_pct'])}'>"
              f"{t['return_pct']:+.2f}%</span>"),
             ("Shares", f"{t['qty']:,.6g}"),
             ("Entry", f"${_price(t['entry_price'])}<div class='hint'>{e(t['entry_time'])}</div>"),
             ("Exit", ("open" if t["exit_price"] is None else f"${_price(t['exit_price'])}")
              + (f"<div class='hint'>{e(t['exit_time'])}</div>" if t["exit_time"] else "")),
             ("Fees", e(_money(t["fees"], sign=False))), ("Gross P&L", e(_money(t["gross"]))), ("Held", e(hold))]
    out.append("<div class='facts2'>" + "".join(f"<div><div class='k'>{k}</div><div class='v'>{v}</div></div>"
                                                for k, v in facts) + "</div>")
    if t["source"] == "manual":
        rec = store.manual_record(trade_id) or {}
        v = values or {
            "symbol": rec.get("symbol", ""), "side": rec.get("side", "long"), "qty": f"{rec.get('qty', 0):g}",
            "fees": f"{rec.get('fees', 0):g}" if rec.get("fees") else "",
            "entry_date": rec.get("entry_time", "")[:10], "entry_time": rec.get("entry_time", "")[11:19],
            "entry_price": f"{rec.get('entry_price', 0):g}",
            "exit_date": (rec.get("exit_time") or "")[:10], "exit_time": (rec.get("exit_time") or "")[11:19],
            "exit_price": f"{rec['exit_price']:g}" if rec.get("exit_price") is not None else ""}
        nv = values if values is not None else {**t, "tags": t["tags"], "mistakes": t["mistakes"]}
        if values is not None:
            nv = {"strategy": values.get("strategy", ""), "setup": values.get("setup", ""),
                  "tags": jn.parse_tags(values.get("tags", "")), "mistakes": jn.parse_tags(values.get("mistakes", "")),
                  "notes": values.get("notes", "")}
        out.append("<h2>Edit trade</h2><form method='post' action='/journal/edit' class='card'>"
                   f"<input type='hidden' name='id' value='{e(trade_id, quote=True)}'><div class='jform'>"
                   + _trade_fields(v) + _notes_fields(nv, strategies, setups, tags)
                   + "</div><div class='formact'><button type='submit'>Save changes</button></div></form>")
    else:
        out.append("<h2>Notes</h2><form method='post' action='/journal/notes' class='card'>"
                   f"<input type='hidden' name='id' value='{e(trade_id, quote=True)}'><div class='jform'>"
                   + _notes_fields(t, strategies, setups, tags)
                   + "</div><div class='formact'><button type='submit'>Save notes</button></div></form>")
        fills = store.fills_for(t["fill_ids"])
        mine = set(t["fill_ids"])
        shared = any(mine & set(o["fill_ids"]) for o in trades if o["id"] != t["id"])
        if fills:
            out.append("<h2>Executions</h2><div class='card wrap'><table class='jt'><thead><tr><th>Time</th>"
                       "<th>Side</th><th class='n'>Shares</th><th class='n'>Price</th><th class='n'>Fees</th>"
                       "<th>File</th></tr></thead><tbody>" + "".join(
                           f"<tr><td>{e(f['when'])}</td><td>{e(f['side'].upper())}</td><td class='n'>{f['qty']:,.6g}"
                           f"</td><td class='n'>${_price(f['price'])}</td><td class='n'>{e(_money(f['fees'], sign=False))}"
                           f"</td><td class='muted'>{e(f.get('file', ''))} · row {f.get('row', '')}</td></tr>"
                           for f in fills) + "</tbody></table>"
                       + ("<p class='hint'>One execution here reversed the position (long to short or back), so it "
                          "is shared with the neighboring trade; its shares and fees are split between the two.</p>"
                          if shared else "") + "</div>")
    note = ("Deleting removes this trade's imported fills. Importing the same file again brings it back."
            if t["source"] == "import" else "Deleting can't be undone.")
    out.append("<form method='post' action='/journal/delete' style='margin-top:22px' "
               f"data-confirm='Delete this {e(t['symbol'], quote=True)} trade? {e(note, quote=True)}'>"
               f"<input type='hidden' name='id' value='{e(trade_id, quote=True)}'>"
               f"<button type='submit' class='danger'>Delete trade</button> <span class='hint'>{e(note)}</span></form>")
    return _journal_page(t["symbol"], "trades", out)


def render_journal_import(app: App) -> str:
    e = html.escape
    out = ["<h1>Import trades</h1>", _flash_html(app, "journal"),
           "<div class='card'><form method='post' action='/journal/import' enctype='multipart/form-data' "
           "class='forms' style='margin-top:0'><input type='file' name='file' accept='.csv,text/csv' required "
           "aria-label='Broker CSV file'><button type='submit'>Import</button></form>"
           "<p class='hint'>Upload your broker's trade history (executions or transactions) exported as a CSV file. "
           "Recognized automatically: Schwab / thinkorswim transactions, Interactive Brokers activity statements and "
           "trade exports, Webull orders, Robinhood account activity, Fidelity account history, and any CSV with "
           "date/time, symbol, buy/sell (or signed quantity), quantity and price columns.</p>"
           "<p class='hint'>Importing the same file again, or a file that overlaps an earlier one, adds only the new "
           "executions. Buys and sells are matched into trades per symbol: a trade starts when you open a position "
           "and ends when it is back to zero, with partial entries and exits averaged. Stocks and ETFs only; options "
           "and futures rows are skipped and counted.</p></div>"]
    try:
        log = list(reversed(app.journal.imports()))
    except jn.JournalError as err:
        log = []
        out.append(f"<div class='warn'>{e(str(err))}</div>")
    if log:
        rows = "".join(
            f"<tr><td>{e(_fmt_local(r.get('when_utc'), app.settings.tz))}</td><td>{e(r.get('file', ''))}</td>"
            f"<td>{e(r.get('broker', ''))}</td><td class='n'>{r.get('rows', 0)}</td><td class='n'>{r.get('added', 0)}"
            f"</td><td class='n'>{r.get('duplicates', 0)}</td><td class='n'>{r.get('skipped', 0)}</td></tr>"
            for r in log[:50])
        out.append("<h2>Import history</h2><div class='card wrap'><table><thead><tr><th>When</th><th>File</th>"
                   "<th>Format</th><th class='n'>Executions</th><th class='n'>Added</th><th class='n'>Already in</th>"
                   f"<th class='n'>Skipped</th></tr></thead><tbody>{rows}</tbody></table></div>")
    out.append("<h2>Start over</h2><div class='card'><form method='post' action='/journal/clear' "
               "data-confirm='Delete every trade, note and the import history? This needs an email code and "
               "cannot be undone.'><button type='submit' class='danger'>Delete all journal data</button> "
               "<span class='hint'>Needs a fresh email code. Export a CSV first if you want a copy.</span></form></div>")
    return _journal_page("Import", "import", out)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def _make_tradingview(app, env, log=print):
    """TradingView watchlist sync (tv_sync.py); None if it can't start. Never stops the agent."""
    if tvs is None or getattr(app, "strategies", None) is None:
        return None
    try:
        return tvs.TVSync(app.settings.data_dir / "tradingview", app.settings.reports_dir,
                          app.strategies.results_dir,
                          signal_fn=globals().get("signal_tickers") or tvs.signal_tickers,
                          read_meta=getattr(sm, "read_meta", None) or tvs.read_meta,
                          pretty_fn=globals().get("_pretty_name") or (lambda s: s),
                          tz=app.settings.tz, env=dict(env), log=log,
                          busy_fn=lambda: bool(getattr(app.runner, "running", False)))
    except Exception as err:  # noqa: BLE001
        log(f"TradingView sync unavailable: {type(err).__name__}: {err}")
        return None


def build_app(env: Mapping[str, str], log: Callable[[str], None] = print) -> App:
    settings = Settings.from_env(env)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    state = State(settings.data_dir / "state.json")
    config_path = settings.data_dir / "screens.json"
    seed_error = seed_config(settings)
    config_error, count = check_config(config_path)
    if seed_error:
        config_error = seed_error
    runner = Runner(settings, state, config_path, log=log)
    strategies = sm.StrategyManager(settings.data_dir, settings.reports_dir, settings.tz, dict(env), log=log)

    def after_scan(code: int) -> None:
        if code != 0:
            return
        runs = list_runs(settings)
        if runs and runs[0].tickers is not None:
            n = strategies.enqueue_auto(runs[0].stamp)
            if n:
                log(f"queued {n} strategy run(s) after the scan")

    runner.on_finished = after_scan
    schedules = sched.ScheduleStore(settings.data_dir / "strategies" / "schedules.json", settings.tz)
    seed_scan_schedule(settings, state, schedules, utc_now(), log)
    app = App(settings, state, runner, config_error, count, presets=PresetStore(config_path),
               strategies=strategies, schedules=schedules, journal=jn.JournalStore(settings.data_dir / "journal"))
    app.notes = tn.NoteStore(settings.data_dir / "trading_notes")
    app.notebook = nb.Notebook(settings.data_dir / "notes")
    app.open_marks = otm.OpenTradeMarks(strategies.finviz.client if strategies.finviz else None)
    app.tradingview = _make_tradingview(app, env, log)
    return app


def seed_scan_schedule(settings: Settings, state: State, schedules: "sched.ScheduleStore",
                       now: dt.datetime, log: Callable[[str], None]) -> int:
    """First start of this version: SCHEDULE_TIMES (default 12:00, 13:30) becomes 'Every weekday'
    scans on the Scans tab. Counted from the last scheduled scan if that was within the catch-up
    window (so it isn't repeated), otherwise from 30 minutes ago (so a slot missed by this very
    redeploy still runs). Never again after that, even if the setting changes."""
    since = now - sched.GRACE
    try:
        last_slot = dt.datetime.fromisoformat(state.get("last_slot") or "")
        if last_slot.tzinfo is not None and since <= last_slot <= now:
            since = last_slot
    except ValueError:
        pass
    n = schedules.seed_scan_times(settings.times, settings.days, since)
    if n:
        log(f"scan schedule: added {n} 'every weekday' scan(s) from SCHEDULE_TIMES "
            f"({', '.join(f'{t:%H:%M}' for t in settings.times)}); manage them on the Scans tab from now on")
    return n


def run_scheduled_scan(app: App, poll: float = 2.0) -> str:
    """A scheduled scan job: run a scan (or join the one already running) and describe the outcome."""
    before = {r.stamp for r in list_runs(app.settings)}
    joined = not app.runner.start("schedule")
    if joined:
        while app.runner.running:
            time.sleep(poll)
    else:
        app.runner.wait()
    last = app.state.get("last_run") or {}
    runs = list_runs(app.settings)
    new = runs[0] if runs and runs[0].stamp not in before else None
    parts = ["ok" if last.get("exit_code") == 0 else str(last.get("status") or "did not finish")]
    if new is not None and new.tickers is not None:
        parts.append(f"{len(new.tickers)} tickers")
    if joined:
        parts.append("joined the scan already running")
    return " · ".join(parts)


def run_fresh_scan(app: App, poll: float = 2.0) -> Tuple[Optional[str], str]:
    """Run a FinViz scan now (or wait for the one already running) and return its folder name."""
    before = {r.stamp for r in list_runs(app.settings)}
    if app.runner.start("for-strategies"):
        app.runner.wait()
    else:
        while app.runner.running:
            time.sleep(poll)
    last = app.state.get("last_run") or {}
    if last.get("exit_code") != 0:
        return None, str(last.get("status") or "the scan did not finish")
    runs = list_runs(app.settings)
    if not runs or runs[0].stamp in before or runs[0].tickers is None:
        return None, "the scan produced no new tickers file"
    return runs[0].stamp, "ok"


def schedule_tick(app: App, now: Optional[dt.datetime] = None, log: Callable[[str], None] = print,
                  background: bool = True) -> int:
    """Start every scheduled job that is due. Returns how many were started."""
    if app.schedules is None:
        return 0
    enqueue = (app.strategies.enqueue_many if app.strategies is not None
               else lambda names, *_: ["Strategies are not available."] * len(names))
    started = 0
    for job, _when in app.schedules.due(now):
        args = (job, enqueue, lambda: run_fresh_scan(app), app.schedules.note_result, log,
                lambda: run_scheduled_scan(app))
        if background:
            threading.Thread(target=sched.dispatch, args=args, daemon=True).start()
        else:
            sched.dispatch(*args)
        started += 1
    return started


def schedule_loop(app: App, stop: threading.Event, log: Callable[[str], None]) -> None:
    next_archive_cleanup = 0.0
    while not stop.is_set():
        try:
            schedule_tick(app, log=log)
            if app.strategies is not None and time.monotonic() >= next_archive_cleanup:
                next_archive_cleanup = time.monotonic() + 3600
                app.strategies.purge_expired_strategies()
        except Exception as err:  # keep the loop alive
            log(f"schedule check failed: {type(err).__name__}: {err}")
        stop.wait(15)


def price_refresh_loop(app: App, stop: threading.Event, log: Callable[[str], None]) -> None:
    """Keep Massive daily prices current (cheap when nothing is missing)."""
    while not stop.is_set():
        st = app.strategies
        if st is not None and st.client is not None:
            st.update_prices_async()
        stop.wait(30 * 60)


def main() -> int:
    def log(msg: str) -> None:
        print(f"[{utc_now():%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)

    try:
        app = build_app(os.environ, log=log)
    except (ValueError, OSError) as err:
        print(f"CONFIG ERROR: {err}", file=sys.stderr, flush=True)
        return 2
    s = app.settings
    log(f"data folder: {s.data_dir}")
    log(f"scan schedule: {scan_schedule_summary(app)}")
    log(f"screens: {app.screen_count}" + (f"; CONFIG PROBLEM: {app.config_error}" if app.config_error else ""))
    if not s.token_present:
        log("WARNING: FINVIZ_API_TOKEN is not set; runs will fail")
    if not s.password:
        log("WARNING: DASHBOARD_PASSWORD is not set; the web page stays locked")
    if not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") and not os.environ.get("DATA_DIR"):
        log("WARNING: no volume attached; runs are lost on every redeploy")

    stop = threading.Event()
    st = app.strategies
    log(f"strategies: {len(st.list())}; price data: {len(st.prices.trading_days())} days; "
        + ("MASSIVE_API_KEY set" if st.client else "MASSIVE_API_KEY not set"))
    threading.Thread(target=price_refresh_loop, args=(app, stop, log), daemon=True).start()
    threading.Thread(target=schedule_loop, args=(app, stop, log), daemon=True).start()
    log(f"scheduled jobs: {sum(1 for j in app.schedules.jobs('scan') if j['status'] == 'scheduled')} scan, "
        f"{sum(1 for j in app.schedules.jobs('strategies') if j['status'] == 'scheduled')} strategy")
    httpd = ThreadingHTTPServer(("0.0.0.0", s.port), Handler)
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]

    def shutdown(signum, _frame) -> None:
        log(f"signal {signum}: shutting down")
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log(f"web page on port {s.port}")
    if getattr(app, "tradingview", None) is not None:
        threading.Thread(target=app.tradingview.loop, args=(stop,), daemon=True).start()
    from paper_trading.analytics import snapshot_loop
    threading.Thread(target=snapshot_loop, args=(app, stop, log), daemon=True).start()
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
