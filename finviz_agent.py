#!/usr/bin/env python3
"""FinViz Screener Gainers Agent.

Scans FinViz screener presets (your saved presets plus FinViz's built-in
signal screens), pulls each one through the FinViz Elite CSV export, and
reports the top N stocks by price % increase for every screen.

Design rules
------------
* Numbers are never estimated: every ranking is computed from the exported CSV.
* The whole screen is exported and ranked locally. The request also asks
  FinViz to sort by the same metric, so the answer stays correct even if an
  export were ever truncated.
* Percent units are checked, not assumed: today's Change is cross-checked
  against Price / Previous Close on every screen, and the evidence is written
  into the report.
* The API token is read from the environment (or a file), sent only to
  FinViz, and redacted from every log line, error message and output file.
* Standard library only (Python 3.9+), so it runs anywhere without installs.

Run ``python finviz_agent.py --help`` for usage.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import shutil
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__version__ = "1.6.0"

DEFAULT_BASE_URL = "https://elite.finviz.com"
# FinViz serves the Elite export at /export.ashx (long-standing) and /export
# (newer). The client tries them in order and remembers whichever works.
EXPORT_PATHS = ("/export.ashx", "/export")
QUOTE_PATHS = ("/quote_export.ashx", "/quote_export")  # Elite daily/intraday OHLCV per ticker
CUSTOM_VIEW = "152"  # FinViz "Custom" screener view; honours the c= column list
USER_AGENT = f"finviz-gainers-agent/{__version__}"
TOKEN_ENV_VARS = ("FINVIZ_API_TOKEN", "FINVIZ_API_KEY")
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
# URL parameters that only change how a screen is displayed, never which
# stocks it contains. Everything else in a preset URL is passed through.
DISPLAY_ONLY_PARAMS = {"v", "o", "c", "r", "auth", "ar", "p"}

EXIT_OK, EXIT_CONFIG, EXIT_AUTH, EXIT_ALL_FAILED, EXIT_TLS = 0, 2, 3, 4, 5


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class AgentError(Exception):
    """Base error for this agent."""


class ConfigError(AgentError):
    """Problem in screens.json or command-line options."""


class CertificateError(AgentError):
    """Python cannot verify HTTPS certificates (missing CA bundle). Aborts the run."""


class AuthError(AgentError):
    """FinViz rejected the token or returned a login page. Aborts the run."""


class FetchError(AgentError):
    """A single screen could not be downloaded."""


class ParseError(AgentError):
    """A single screen's CSV could not be interpreted."""


class _NotFound(Exception):
    """Internal: HTTP 404 on one export path (try the next one)."""


# --------------------------------------------------------------------------
# FinViz catalogue: columns, timeframes, built-in signal screens
# --------------------------------------------------------------------------
def norm_header(text: str) -> str:
    """Normalise a CSV header for matching ('Perf Week' -> 'perfweek')."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass(frozen=True)
class Column:
    key: str
    finviz_id: int  # id used in the export's c= parameter
    aliases: Tuple[str, ...]  # normalised header names accepted in the CSV
    kind: str  # "text" (kept as exported), "num" (parsed), "pct" (parsed, unit-checked)


# Column ids follow FinViz's custom-screener numbering (cross-checked against
# finvizfinance's CUSTOM_SCREENER_COLUMNS table and published Elite-export code).
COLUMNS: Tuple[Column, ...] = (
    Column("ticker", 1, ("ticker",), "text"),
    Column("company", 2, ("company",), "text"),
    Column("sector", 3, ("sector",), "text"),
    Column("industry", 4, ("industry",), "text"),
    Column("market_cap", 6, ("marketcap",), "text"),
    Column("perf_week", 42, ("perfweek", "performanceweek"), "pct"),
    Column("perf_month", 43, ("perfmonth", "performancemonth"), "pct"),
    Column("perf_quarter", 44, ("perfquarter", "perfquart", "performancequarter"), "pct"),
    Column("perf_half", 45, ("perfhalf", "perfhalfy", "perfhalfyear", "performancehalfyear"), "pct"),
    Column("perf_year", 46, ("perfyear", "performanceyear"), "pct"),
    Column("perf_ytd", 47, ("perfytd", "performanceytd", "performanceyeartodate"), "pct"),
    Column("change_from_open", 60, ("changefromopen", "fromopen"), "pct"),
    Column("gap", 61, ("gap",), "pct"),
    Column("avg_volume", 63, ("averagevolume", "avgvolume", "averagevolume3month"), "text"),
    Column("rel_volume", 64, ("relativevolume", "relvolume"), "text"),
    Column("price", 65, ("price",), "num"),
    Column("change", 66, ("change",), "pct"),
    Column("volume", 67, ("volume",), "text"),
    Column("prev_close", 81, ("prevclose", "previousclose"), "num"),
)
COLUMN_BY_KEY: Dict[str, Column] = {c.key: c for c in COLUMNS}


@dataclass(frozen=True)
class Timeframe:
    key: str
    label: str
    column_key: str
    sort_code: str  # FinViz o= code (descending is "-" + code)


TIMEFRAMES: Dict[str, Timeframe] = {
    "change": Timeframe("change", "Today % change", "change", "change"),
    "week": Timeframe("week", "Performance (Week)", "perf_week", "perf1w"),
    "month": Timeframe("month", "Performance (Month)", "perf_month", "perf4w"),
    "quarter": Timeframe("quarter", "Performance (Quarter)", "perf_quarter", "perf13w"),
    "half": Timeframe("half", "Performance (Half Year)", "perf_half", "perf26w"),
    "year": Timeframe("year", "Performance (Year)", "perf_year", "perf52w"),
    "ytd": Timeframe("ytd", "Performance (YTD)", "perf_ytd", "perfytd"),
}

# FinViz built-in "Signal" screens (s= codes).
SIGNALS: Dict[str, str] = {
    "Top Gainers": "ta_topgainers",
    "Top Losers": "ta_toplosers",
    "New High": "ta_newhigh",
    "New Low": "ta_newlow",
    "Most Volatile": "ta_mostvolatile",
    "Most Active": "ta_mostactive",
    "Unusual Volume": "ta_unusualvolume",
    "Overbought": "ta_overbought",
    "Oversold": "ta_oversold",
    "Downgrades": "n_downgrades",
    "Upgrades": "n_upgrades",
    "Earnings Before": "n_earningsbefore",
    "Earnings After": "n_earningsafter",
    "Recent Insider Buying": "it_latestbuys",
    "Recent Insider Selling": "it_latestsales",
    "Major News": "n_majornews",
    "Horizontal S/R": "ta_p_horizontal",
    "TL Resistance": "ta_p_tlresistance",
    "TL Support": "ta_p_tlsupport",
    "Wedge Up": "ta_p_wedgeup",
    "Wedge Down": "ta_p_wedgedown",
    "Triangle Ascending": "ta_p_wedgeresistance",
    "Triangle Descending": "ta_p_wedgesupport",
    "Wedge": "ta_p_wedge",
    "Channel Up": "ta_p_channelup",
    "Channel Down": "ta_p_channeldown",
    "Channel": "ta_p_channel",
    "Double Top": "ta_p_doubletop",
    "Double Bottom": "ta_p_doublebottom",
    "Multiple Top": "ta_p_multipletop",
    "Multiple Bottom": "ta_p_multiplebottom",
    "Head & Shoulders": "ta_p_headandshoulders",
    "Head & Shoulders Inverse": "ta_p_headandshouldersinv",
}


# --------------------------------------------------------------------------
# Secret handling
# --------------------------------------------------------------------------
class Redactor:
    """Removes the API token (raw and URL-encoded) from any text."""

    def __init__(self, secrets: Sequence[str] = ()):
        found: List[str] = []
        for s in secrets:
            if s:
                found.extend({s, urllib.parse.quote(s, safe=""), urllib.parse.quote_plus(s)})
        self._secrets = sorted(set(found), key=len, reverse=True)

    def __call__(self, text: object) -> str:
        out = str(text)
        for s in self._secrets:
            out = out.replace(s, "***")
        return re.sub(r"(auth=)[^&\s'\"<>]+", r"\1***", out)


def read_token(token_file: Optional[str]) -> Optional[str]:
    if token_file:
        path = Path(token_file).expanduser()
        if not path.is_file():
            raise ConfigError(f"Token file not found: {path}")
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ConfigError(f"Token file is empty: {path}")
        return token
    for var in TOKEN_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return None


# --------------------------------------------------------------------------
# HTTPS trust store
# --------------------------------------------------------------------------
# macOS ships Apple's root-certificate bundle here. The python.org installer
# for macOS does not use it until "Install Certificates.command" is run, which
# causes CERTIFICATE_VERIFY_FAILED on every HTTPS request.
MACOS_SYSTEM_CA_FILE = "/etc/ssl/cert.pem"


def make_ssl_context() -> Tuple[ssl.SSLContext, str]:
    """Verified TLS context: Python's default trust store, plus the macOS system
    bundle and certifi's bundle when available. Verification is never disabled."""
    ctx = ssl.create_default_context()
    sources = ["Python default"]
    if sys.platform == "darwin" and os.path.isfile(MACOS_SYSTEM_CA_FILE):
        try:
            ctx.load_verify_locations(cafile=MACOS_SYSTEM_CA_FILE)
            sources.append(f"macOS system ({MACOS_SYSTEM_CA_FILE})")
        except (ssl.SSLError, OSError):
            pass
    try:
        import certifi  # optional; installed by "Install Certificates.command"

        ctx.load_verify_locations(cafile=certifi.where())
        sources.append("certifi")
    except Exception:
        pass
    return ctx, " + ".join(sources)


def is_cert_error(err: BaseException) -> bool:
    for e in (err, getattr(err, "reason", None)):
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
    return "CERTIFICATE_VERIFY_FAILED" in str(err)


def certificate_help(err: BaseException, redact: Callable[[object], str]) -> str:
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    fix = (
        f'Fix: run  open "/Applications/Python {ver}/Install Certificates.command"  '
        "(or: python3 -m pip install certifi), then try again."
        if sys.platform == "darwin"
        else "Fix: python3 -m pip install certifi (or install your OS 'ca-certificates' package), then try again."
    )
    return (
        "Python could not verify FinViz's HTTPS certificate because this Python install has no "
        f"trusted root certificates. {fix} Details: {redact(err)}"
    )


# --------------------------------------------------------------------------
# HTTP client for the FinViz Elite export
# --------------------------------------------------------------------------
class FinvizClient:
    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        min_interval: float = 1.0,
        retries: int = 3,
        backoff: float = 2.0,
        timeout: float = 60.0,
        urlopen: Optional[Callable] = None,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        log: Callable[[str], None] = lambda _m: None,
    ):
        if not token:
            raise AuthError(
                "No FinViz Elite API token. Set FINVIZ_API_TOKEN (token is on your FinViz "
                "account page) or pass --token-file."
            )
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.min_interval = max(0.0, min_interval)
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self.timeout = timeout
        if urlopen is None:
            self._ssl_context, self.ca_source = make_ssl_context()
            self._urlopen = lambda req, timeout: urllib.request.urlopen(
                req, timeout=timeout, context=self._ssl_context
            )
        else:  # injected by tests
            self._ssl_context, self.ca_source = None, "injected"
            self._urlopen = urlopen
        self._sleep = sleep or (lambda s: time.sleep(s))
        self._clock = clock or (lambda: time.monotonic())
        self.redact = Redactor([token])
        self._log = lambda m: log(self.redact(m))
        self._paths = list(EXPORT_PATHS)
        self._quote_paths = list(QUOTE_PATHS)
        self._last_request: Optional[float] = None
        self.requests_made = 0

    def build_url(self, path: str, params: Dict[str, str]) -> str:
        query = dict(params)
        query["auth"] = self.token
        return f"{self.base_url}{path}?{urllib.parse.urlencode(query, safe=',|')}"

    def redacted_url(self, params: Dict[str, str]) -> str:
        return self.redact(self.build_url(self._paths[0], params))

    def export_csv(self, params: Dict[str, str]) -> str:
        return self._get_csv(self._paths, EXPORT_PATHS, params, "export")

    def quote_history(self, ticker: str, period: str = "d") -> str:
        """Daily (p=d) OHLCV history for one ticker: CSV with Date, Open, High, Low, Close, Volume."""
        return self._get_csv(self._quote_paths, QUOTE_PATHS, {"t": ticker, "p": period}, "price export")

    def _get_csv(self, paths: List[str], all_paths: Tuple[str, ...], params: Dict[str, str], what: str) -> str:
        """Try each known path (old .ashx and newer style); remember whichever works."""
        for path in list(paths):
            try:
                text = self._get(self.build_url(path, params))
            except _NotFound:
                self._log(f"{what} path {path} returned 404; trying the next one")
                continue
            if path != paths[0]:
                paths.remove(path)
                paths.insert(0, path)
            return text
        tried = ", ".join(self.base_url + p for p in all_paths)
        raise FetchError(f"FinViz {what} endpoint not found (HTTP 404) at: {tried}")

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    @staticmethod
    def _retry_after(err: urllib.error.HTTPError) -> Optional[float]:
        try:
            value = err.headers.get("Retry-After") if err.headers else None
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _get(self, url: str) -> str:
        attempt = 0
        while True:
            self._throttle()
            self.requests_made += 1
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*;q=0.5"}
            )
            try:
                with self._urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    headers = getattr(resp, "headers", None)
                    ctype = headers.get("Content-Type", "") if headers else ""
            except urllib.error.HTTPError as err:
                code = err.code
                if code == 404:
                    raise _NotFound() from None
                if code in (401, 403):
                    raise AuthError(
                        f"FinViz rejected the API token (HTTP {code}). Check FINVIZ_API_TOKEN "
                        "and that your Elite subscription is active."
                    ) from None
                if code in RETRYABLE_HTTP and attempt < self.retries:
                    wait = self._retry_after(err) or self.backoff * (2 ** attempt)
                    self._log(f"HTTP {code} from FinViz; retrying in {wait:.1f}s")
                    self._sleep(wait)
                    attempt += 1
                    continue
                raise FetchError(f"HTTP {code} from FinViz export ({self.redact(err.reason)})") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
                if is_cert_error(err):  # retrying cannot fix a missing trust store
                    raise CertificateError(certificate_help(err, self.redact)) from None
                if attempt < self.retries:
                    wait = self.backoff * (2 ** attempt)
                    self._log(f"network error ({self.redact(err)}); retrying in {wait:.1f}s")
                    self._sleep(wait)
                    attempt += 1
                    continue
                raise FetchError(f"Network error reaching FinViz: {self.redact(err)}") from None

            text = raw.decode("utf-8-sig", errors="replace")
            if looks_like_html(text, ctype):
                raise AuthError(
                    "FinViz returned a web page instead of CSV. This usually means the token is "
                    "invalid or expired, or the Elite subscription is inactive."
                )
            return text


def looks_like_html(text: str, content_type: str = "") -> bool:
    head = text.lstrip()[:512].lower()
    return (
        "text/html" in content_type.lower()
        or head.startswith("<!doctype")
        or head.startswith("<html")
        or "<html" in head
    )


# --------------------------------------------------------------------------
# CSV parsing, unit verification, ranking
# --------------------------------------------------------------------------
def parse_number(raw: str) -> Tuple[Optional[float], bool]:
    """Return (value, had_percent_sign). '-', '' and 'N/A' are missing."""
    s = (raw or "").strip()
    if s in {"", "-", "--", "N/A", "n/a", "NA"}:
        return None, False
    had_pct = s.endswith("%")
    s = s.rstrip("%").replace(",", "").replace("$", "").strip()
    try:
        return float(s), had_pct
    except ValueError:
        return None, had_pct


@dataclass
class StockRow:
    fields: Dict[str, str]  # raw exported strings by column key
    values: Dict[str, Optional[float]]  # parsed numbers; pct columns are in percent units

    @property
    def ticker(self) -> str:
        return self.fields.get("ticker", "")


@dataclass
class ParsedExport:
    rows: List[StockRow]
    header: List[str]
    mapping: Dict[str, str]  # column key -> header text used
    unit_scale: float
    unit_evidence: str
    unit_verified: bool
    change_check: Optional[Dict[str, object]]
    duplicates_dropped: int
    unparseable: Dict[str, int]


def _detect_pct_scale(rows_raw: List[Dict[str, str]], pct_keys: Sequence[str]) -> Tuple[float, str, bool]:
    """Decide whether exported percent columns are percents (1.0) or fractions (100.0)."""
    if not rows_raw:
        return 1.0, "no rows to check", True
    if any("%" in r.get(k, "") for r in rows_raw for k in pct_keys):
        return 1.0, "values carry a % sign", True
    ratios: List[float] = []
    for r in rows_raw:
        ch, _ = parse_number(r.get("change", ""))
        price, _ = parse_number(r.get("price", ""))
        prev, _ = parse_number(r.get("prev_close", ""))
        if ch is None or price is None or prev is None or prev <= 0 or abs(ch) < 1e-12:
            continue
        implied = (price / prev - 1.0) * 100.0
        if abs(implied) >= 0.5:  # ignore tiny moves where rounding dominates
            ratios.append(implied / ch)
    if len(ratios) >= 3:
        med = statistics.median(ratios)
        if 50.0 <= med <= 200.0:
            return 100.0, f"no % sign; values are fractions (Price/Prev Close ratio {med:.1f}), converted to %", True
        if 0.5 <= med <= 2.0:
            return 1.0, f"no % sign; values match Price/Prev Close as percents (ratio {med:.2f})", True
        return 1.0, f"UNVERIFIED: no % sign and Price/Prev Close ratio {med:.2f} fits neither unit; assumed %", False
    return 1.0, "UNVERIFIED: no % sign and too few rows to cross-check; assumed %", False


def _change_cross_check(rows: List[StockRow]) -> Optional[Dict[str, object]]:
    """Compare FinViz's Change with (Price / Prev Close - 1) for every row."""
    checked = agree = 0
    worst: Optional[Tuple[float, str]] = None
    for r in rows:
        ch, price, prev = r.values.get("change"), r.values.get("price"), r.values.get("prev_close")
        if ch is None or price is None or prev is None or prev <= 0:
            continue
        implied = (price / prev - 1.0) * 100.0
        # allow for both prices being rounded to the cent, plus display rounding
        tol = max(0.15, 100.0 * 0.011 / prev)
        diff = abs(ch - implied)
        checked += 1
        if diff <= tol:
            agree += 1
        elif worst is None or diff > worst[0]:
            worst = (diff, r.ticker)
    if checked == 0:
        return None
    return {
        "rows_checked": checked,
        "rows_agreeing": agree,
        "agreement_pct": round(100.0 * agree / checked, 1),
        "largest_disagreement": None if worst is None else {"ticker": worst[1], "pct_points": round(worst[0], 2)},
    }


def parse_export(text: str, required_keys: Sequence[str]) -> ParsedExport:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError("FinViz returned an empty export (no header row).") from None
    normalised = [norm_header(h) for h in header]
    index: Dict[str, int] = {}
    for col in COLUMNS:
        for i, h in enumerate(normalised):
            if h in col.aliases:
                index[col.key] = i
                break
    missing = [k for k in required_keys if k not in index]
    if missing:
        raise ParseError(
            f"Export is missing required column(s) {missing}. Headers received: {header}"
        )

    rows_raw: List[Dict[str, str]] = []
    seen: set = set()
    duplicates = 0
    for rec in reader:
        if not any(cell.strip() for cell in rec):
            continue
        fields = {k: (rec[i].strip() if i < len(rec) else "") for k, i in index.items()}
        ticker = fields.get("ticker", "")
        if not ticker:
            continue
        if ticker in seen:
            duplicates += 1
            continue
        seen.add(ticker)
        rows_raw.append(fields)

    pct_keys = [k for k in index if COLUMN_BY_KEY[k].kind == "pct"]
    scale, evidence, verified = _detect_pct_scale(rows_raw, pct_keys)

    unparseable: Dict[str, int] = {}
    rows: List[StockRow] = []
    for fields in rows_raw:
        values: Dict[str, Optional[float]] = {}
        for k in index:
            kind = COLUMN_BY_KEY[k].kind
            if kind == "text":
                continue
            v, _ = parse_number(fields[k])
            if v is None and fields[k].strip() not in {"", "-", "--", "N/A", "n/a", "NA"}:
                unparseable[k] = unparseable.get(k, 0) + 1
            if v is not None and kind == "pct":
                v *= scale
            values[k] = v
        rows.append(StockRow(fields=fields, values=values))

    return ParsedExport(
        rows=rows,
        header=header,
        mapping={k: header[i] for k, i in index.items()},
        unit_scale=scale,
        unit_evidence=evidence,
        unit_verified=verified,
        change_check=_change_cross_check(rows),
        duplicates_dropped=duplicates,
        unparseable=unparseable,
    )


@dataclass
class Ranked:
    timeframe: Timeframe
    top: List[StockRow]
    total_rows: int
    with_value: int
    eligible: int


def rank_rows(rows: Sequence[StockRow], tf: Timeframe, top_n: int, include_nonpositive: bool) -> Ranked:
    key = tf.column_key
    with_value = [r for r in rows if r.values.get(key) is not None]
    eligible = with_value if include_nonpositive else [r for r in with_value if r.values[key] > 0]
    ordered = sorted(eligible, key=lambda r: (-r.values[key], r.ticker))
    return Ranked(tf, ordered[:top_n], len(rows), len(with_value), len(eligible))


# --------------------------------------------------------------------------
# Screens and configuration
# --------------------------------------------------------------------------
@dataclass
class Screen:
    name: str
    kind: str  # "Saved preset" | "FinViz signal"
    params: Dict[str, str]
    source: str
    slug: str = ""
    keep: Optional[int] = None  # stocks to keep from this screen: None = the run's top N, 0 = all


KEEP_ALL = 0
MAX_KEEP = 10000


def parse_keep(value) -> Optional[int]:
    """A preset's 'keep' setting: missing/'' -> None (use the run's top N), 'all' -> 0 (every stock),
    or a whole number of stocks."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and value.strip().lower() == "all":
        return KEEP_ALL
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ConfigError(f"'keep' must be a number of stocks or \"all\", got {value!r}.") from None
    if not 1 <= n <= MAX_KEEP:
        raise ConfigError(f"'keep' must be between 1 and {MAX_KEEP} (or \"all\"), got {n}.")
    return n


def keep_label(keep: Optional[int], top_n: int) -> str:
    return f"top {top_n}" if keep is None else "all" if keep == KEEP_ALL else f"top {keep}"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "screen"


def screen_from_preset(name: str, url: str, keep=None) -> Screen:
    if not name or not str(name).strip():
        raise ConfigError("Every saved preset needs a non-empty 'name'.")
    name = str(name).strip()
    parts = urllib.parse.urlsplit(str(url).strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc.lower().endswith("finviz.com"):
        raise ConfigError(f"Preset '{name}': URL must be a finviz.com screener link, got {url!r}.")
    # Accept both the classic address (/screener.ashx) and the newer one
    # (/screener, /screener/stocks); only the query parameters are used.
    path = parts.path.lower().rstrip("/")
    if not (path.endswith("screener.ashx") or path == "/screener" or path.startswith("/screener/")):
        raise ConfigError(f"Preset '{name}': URL must be a FinViz screener page, got path {parts.path!r}.")
    params: Dict[str, str] = {}
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=False):
        if key.lower() in DISPLAY_ONLY_PARAMS:
            continue
        params[key] = value
    if not any(params.get(k) for k in ("f", "s", "t")):
        found = ", ".join(sorted(params)) or "none"
        raise ConfigError(
            f"Preset '{name}': the URL has no filters (f=), signal (s=) or tickers (t=), so it "
            "would export the entire market. Load the preset in FinViz first, then copy the "
            f"address-bar URL. (Parameters found in the URL: {found})"
        )
    source = Redactor()(urllib.parse.urlunsplit(parts._replace(fragment="")))
    try:
        keep_n = parse_keep(keep)
    except ConfigError as err:
        raise ConfigError(f"Preset '{name}': {err}") from None
    return Screen(name=name, kind="Saved preset", params=params, source=source, keep=keep_n)


def screen_from_signal(name: str) -> Screen:
    code = SIGNALS[name]
    return Screen(name=name, kind="FinViz signal", params={"s": code}, source=f"s={code}")


def _match_signal(name: str) -> str:
    lookup = {k.lower(): k for k in SIGNALS}
    hit = lookup.get(str(name).strip().lower())
    if hit is None:
        raise ConfigError(f"Unknown FinViz signal {name!r}. Valid names: {', '.join(SIGNALS)}")
    return hit


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ConfigError(f"{path} is not valid JSON: {err}") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} must contain a JSON object.")
    return cfg


def build_screens(cfg: dict, only: Optional[Sequence[str]] = None) -> List[Screen]:
    screens: List[Screen] = []

    for i, item in enumerate(cfg.get("saved_presets") or []):
        if not isinstance(item, dict):
            raise ConfigError(f"saved_presets[{i}] must be an object with 'name' and 'url'.")
        if item.get("enabled", True) is False:
            continue
        if "url" not in item:
            raise ConfigError(f"saved_presets[{i}] ('{item.get('name', '?')}') is missing 'url'.")
        screens.append(screen_from_preset(item.get("name", ""), item["url"], item.get("keep")))

    sig_cfg = cfg.get("builtin_signals", {"enabled": True, "include": "all"})
    if not isinstance(sig_cfg, dict):
        raise ConfigError("builtin_signals must be an object.")
    if sig_cfg.get("enabled", True):
        include = sig_cfg.get("include", "all")
        if include == "all":
            names = list(SIGNALS)
        elif isinstance(include, list):
            names = [_match_signal(n) for n in include]
        else:
            raise ConfigError("builtin_signals.include must be \"all\" or a list of signal names.")
        excluded = {_match_signal(n) for n in sig_cfg.get("exclude", []) or []}
        screens.extend(screen_from_signal(n) for n in names if n not in excluded)

    # unique display names and file slugs
    seen_names: Dict[str, int] = {}
    seen_slugs: set = set()
    for s in screens:
        key = s.name.lower()
        if key in seen_names:
            seen_names[key] += 1
            s.name = f"{s.name} [{s.kind.lower()}]" if seen_names[key] == 2 else f"{s.name} ({seen_names[key]})"
        else:
            seen_names[key] = 1
        base = slugify(s.name)
        slug, n = base, 2
        while slug in seen_slugs:
            slug, n = f"{base}-{n}", n + 1
        seen_slugs.add(slug)
        s.slug = slug

    if only:
        wanted = {o.strip().lower() for o in only if o.strip()}
        available = {s.name.lower() for s in screens}
        unknown = sorted(wanted - available)
        if unknown:
            raise ConfigError(
                f"--only names not found: {unknown}. Available: {', '.join(s.name for s in screens)}"
            )
        screens = [s for s in screens if s.name.lower() in wanted]

    if not screens:
        raise ConfigError("No screens to scan: add saved_presets and/or enable builtin_signals.")
    return screens


def parse_timeframes(spec) -> List[Timeframe]:
    items = spec if isinstance(spec, list) else str(spec).split(",")
    out: List[Timeframe] = []
    for item in items:
        key = str(item).strip().lower()
        if not key:
            continue
        if key not in TIMEFRAMES:
            raise ConfigError(f"Unknown timeframe {item!r}. Choose from: {', '.join(TIMEFRAMES)}")
        if TIMEFRAMES[key] not in out:
            out.append(TIMEFRAMES[key])
    if not out:
        raise ConfigError("At least one timeframe is required.")
    return out


def build_export_params(screen: Screen, primary: Timeframe) -> Dict[str, str]:
    params = dict(screen.params)
    params["v"] = CUSTOM_VIEW
    params["c"] = ",".join(str(c.finviz_id) for c in COLUMNS)
    params["o"] = "-" + primary.sort_code
    return params


# --------------------------------------------------------------------------
# Running the scan
# --------------------------------------------------------------------------
@dataclass
class ScreenResult:
    screen: Screen
    status: str  # "ok" | "error"
    error: Optional[str] = None
    parsed: Optional[ParsedExport] = None
    rankings: Dict[str, Ranked] = field(default_factory=dict)


def run_scan(
    screens: Sequence[Screen],
    fetch: Callable[[Screen], str],
    timeframes: Sequence[Timeframe],
    top_n: int,
    include_nonpositive: bool,
    raw_dir: Optional[Path] = None,
    log: Callable[[str], None] = lambda _m: None,
) -> List[ScreenResult]:
    required = ["ticker"] + [tf.column_key for tf in timeframes]
    results: List[ScreenResult] = []
    for i, screen in enumerate(screens, 1):
        log(f"[{i}/{len(screens)}] {screen.name}")
        try:
            text = fetch(screen)
            if raw_dir is not None:
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / f"{screen.slug}.csv").write_text(text, encoding="utf-8")
            parsed = parse_export(text, required)
        except (AuthError, CertificateError):
            raise  # affects every screen; stop instead of failing them one by one
        except AgentError as err:
            log(f"    error: {err}")
            results.append(ScreenResult(screen, "error", error=str(err)))
            continue
        result = ScreenResult(screen, "ok", parsed=parsed)
        for tf in timeframes:
            limit = top_n if screen.keep is None else (len(parsed.rows) if screen.keep == KEEP_ALL else screen.keep)
            result.rankings[tf.key] = rank_rows(parsed.rows, tf, limit, include_nonpositive)
        primary = result.rankings[timeframes[0].key]
        warn = "" if parsed.unit_verified else "  [units unverified]"
        log(f"    {primary.total_rows} stocks, {primary.eligible} eligible, kept {len(primary.top)}{warn}")
        results.append(result)
    return results


def find_overlap(results: Sequence[ScreenResult], tf: Timeframe) -> List[Dict[str, object]]:
    hits: Dict[str, Dict[str, object]] = {}
    for res in results:
        ranked = res.rankings.get(tf.key)
        if res.status != "ok" or ranked is None:
            continue
        for rank, row in enumerate(ranked.top, 1):
            entry = hits.setdefault(
                row.ticker,
                {"ticker": row.ticker, "company": row.fields.get("company", ""),
                 "value": row.values.get(tf.column_key), "screens": []},
            )
            entry["screens"].append({"screen": res.screen.name, "rank": rank})
    multi = [e for e in hits.values() if len(e["screens"]) >= 2]
    multi.sort(key=lambda e: (-len(e["screens"]), -(e["value"] or 0.0), e["ticker"]))
    return multi


# --------------------------------------------------------------------------
# Output: CSV, JSON, HTML
# --------------------------------------------------------------------------
def _fmt_pct(v: Optional[float]) -> str:
    return "" if v is None else f"{v:+.2f}%"


def _fmt_num(v: Optional[float]) -> str:
    return "" if v is None else f"{v:,.2f}"


def write_csv(results: Sequence[ScreenResult], timeframes: Sequence[Timeframe], meta: dict, path: Path) -> None:
    cols = [
        "run_utc", "timeframe", "screen", "screen_type", "rank", "ticker", "company", "sector",
        "industry", "metric_pct", "metric_raw", "price", "change_today_pct", "volume",
        "rel_volume", "market_cap", "screen_stocks", "screen_eligible",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for tf in timeframes:
            for res in results:
                if res.status != "ok":
                    continue
                ranked = res.rankings[tf.key]
                for rank, row in enumerate(ranked.top, 1):
                    m = row.values.get(tf.column_key)
                    ch = row.values.get("change")
                    pr = row.values.get("price")
                    w.writerow([
                        meta["run_utc"], tf.key, res.screen.name, res.screen.kind, rank, row.ticker,
                        row.fields.get("company", ""), row.fields.get("sector", ""),
                        row.fields.get("industry", ""), "" if m is None else f"{m:.4f}",
                        row.fields.get(tf.column_key, ""), "" if pr is None else f"{pr:.4f}",
                        "" if ch is None else f"{ch:.4f}", row.fields.get("volume", ""),
                        row.fields.get("rel_volume", ""), row.fields.get("market_cap", ""),
                        ranked.total_rows, ranked.eligible,
                    ])


def build_summary(results: Sequence[ScreenResult], timeframes: Sequence[Timeframe], meta: dict) -> dict:
    screens = []
    for res in results:
        entry: Dict[str, object] = {
            "name": res.screen.name, "type": res.screen.kind, "source": res.screen.source,
            "keep": keep_label(res.screen.keep, meta["top_n"]),
            "status": res.status, "error": res.error,
        }
        if res.parsed is not None:
            p = res.parsed
            entry["diagnostics"] = {
                "stocks_in_screen": len(p.rows),
                "header_mapping": p.mapping,
                "percent_units": p.unit_evidence,
                "percent_units_verified": p.unit_verified,
                "change_cross_check": p.change_check,
                "duplicate_tickers_dropped": p.duplicates_dropped,
                "unparseable_values": p.unparseable,
            }
            entry["rankings"] = {
                tf.key: {
                    "label": tf.label,
                    "stocks_with_value": res.rankings[tf.key].with_value,
                    "eligible": res.rankings[tf.key].eligible,
                    "top": [
                        {
                            "rank": i,
                            "ticker": r.ticker,
                            "company": r.fields.get("company", ""),
                            "sector": r.fields.get("sector", ""),
                            "metric_pct": r.values.get(tf.column_key),
                            "price": r.values.get("price"),
                            "change_today_pct": r.values.get("change"),
                        }
                        for i, r in enumerate(res.rankings[tf.key].top, 1)
                    ],
                }
                for tf in timeframes
            }
        screens.append(entry)
    return {
        **meta,
        "screens": screens,
        "overlap": {tf.key: find_overlap(results, tf) for tf in timeframes},
    }


_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--up:#0f7b3f;
--down:#b3261e;--accent:#1f4e79;--warn:#8a5a00;--warnbg:#fff4dc}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#a3a29b;
--line:#33322f;--up:#4cc283;--down:#ff8a80;--accent:#8fb8e0;--warn:#f0c26b;--warnbg:#3a2f14}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:34px 0 10px}
h3{font-size:16px;margin:0}.muted{color:var(--muted)}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--muted);font-size:13.5px;margin-bottom:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.card header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.tag{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 8px}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-weight:600;color:var(--muted);font-size:12.5px;text-transform:uppercase;letter-spacing:.03em}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.up{color:var(--up);font-weight:600}.down{color:var(--down);font-weight:600}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.warn{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:8px 12px;font-size:13.5px;margin:8px 0}
.err{color:var(--down)}details{margin-top:8px;font-size:13px;color:var(--muted)}
summary{cursor:pointer}code{font-size:12.5px}
"""


def _cls(v: Optional[float]) -> str:
    if v is None:
        return ""
    return "up" if v > 0 else ("down" if v < 0 else "")


def render_html(results: Sequence[ScreenResult], timeframes: Sequence[Timeframe], meta: dict) -> str:
    e = html.escape
    ok = [r for r in results if r.status == "ok"]
    failed = [r for r in results if r.status != "ok"]
    out: List[str] = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>FinViz Screener Gainers</title>",
        f"<style>{_CSS}</style></head><body><main>",
        "<h1>FinViz screener gainers</h1>",
        "<div class='meta'>",
        f"<span>Run: {e(meta['run_local'])}</span>",
        f"<span>Top {meta['top_n']} per screen"
        + "".join(f"; {e(k)}: {e(v)}" for k, v in (meta.get("keep") or {}).items()) + "</span>",
        f"<span>Ranked by: {e(', '.join(tf.label for tf in timeframes))}</span>",
        f"<span>{len(ok)} of {len(results)} screens scanned</span>",
        "</div>",
        "<p class='muted'>Source: FinViz Elite CSV export. Each screen is exported in full and ranked "
        "here; only stocks with a positive move are listed"
        + (" (negative moves included by request)" if meta["include_nonpositive"] else "")
        + ". FinViz &ldquo;Change&rdquo; is today's % change vs. the previous close.</p>",
    ]
    unverified = [r.screen.name for r in ok if not r.parsed.unit_verified and r.parsed.rows]
    if unverified:
        out.append(
            "<div class='warn'>Percent units could not be cross-checked for: "
            + e(", ".join(unverified)) + ". See diagnostics under those screens.</div>"
        )
    if failed:
        out.append("<div class='warn'>Screens that failed: " + e(", ".join(r.screen.name for r in failed))
                   + ". Details at the bottom.</div>")

    for tf in timeframes:
        key = tf.column_key
        # summary table
        out.append(f"<h2>Summary &mdash; {e(tf.label)}</h2><div class='card wrap'><table><thead><tr>"
                   "<th>Screen</th><th>Type</th><th class='n'>Stocks</th><th class='n'>Gainers</th>"
                   "<th>#1</th><th class='n'>#1 move</th></tr></thead><tbody>")
        for res in results:
            if res.status != "ok":
                out.append(f"<tr><td>{e(res.screen.name)}</td><td>{e(res.screen.kind)}</td>"
                           "<td class='n err' colspan='4'>failed</td></tr>")
                continue
            rk = res.rankings[tf.key]
            top = rk.top[0] if rk.top else None
            out.append(
                f"<tr><td><a href='#{e(res.screen.slug)}-{tf.key}'>{e(res.screen.name)}</a></td>"
                f"<td>{e(res.screen.kind)}</td><td class='n'>{rk.total_rows}</td>"
                f"<td class='n'>{rk.eligible}</td>"
                f"<td>{e(top.ticker) if top else '&mdash;'}</td>"
                f"<td class='n {_cls(top.values.get(key)) if top else ''}'>"
                f"{e(_fmt_pct(top.values.get(key))) if top else ''}</td></tr>"
            )
        out.append("</tbody></table></div>")

        overlap = find_overlap(results, tf)
        out.append(f"<h2>In more than one screen</h2>")
        if overlap:
            out.append("<div class='card wrap'><table><thead><tr><th>Ticker</th><th>Company</th>"
                       f"<th class='n'>{e(tf.label)}</th><th class='n'>Screens</th><th>Where (rank)</th>"
                       "</tr></thead><tbody>")
            for o in overlap:
                where = ", ".join(f"{s['screen']} (#{s['rank']})" for s in o["screens"])
                out.append(
                    f"<tr><td><a href='https://finviz.com/quote.ashx?t={urllib.parse.quote(o['ticker'])}'>"
                    f"{e(o['ticker'])}</a></td><td>{e(o['company'])}</td>"
                    f"<td class='n {_cls(o['value'])}'>{e(_fmt_pct(o['value']))}</td>"
                    f"<td class='n'>{len(o['screens'])}</td><td>{e(where)}</td></tr>"
                )
            out.append("</tbody></table></div>")
        else:
            out.append("<p class='muted'>No ticker made the top list of more than one screen.</p>")

        out.append(f"<h2>Top {meta['top_n']} by screen &mdash; {e(tf.label)}</h2>"
                   + (f"<p class='muted'>Except: " + ", ".join(f"{e(k)} keeps {e(v)}" for k, v in meta["keep"].items())
                      + "</p>" if meta.get("keep") else ""))
        for res in ok:
            rk = res.rankings[tf.key]
            p = res.parsed
            out.append(f"<section class='card' id='{e(res.screen.slug)}-{tf.key}'><header>"
                       f"<h3>{e(res.screen.name)}</h3><span class='tag'>{e(res.screen.kind)} &middot; "
                       f"{rk.total_rows} stocks &middot; {rk.eligible} eligible &middot; keeping "
                       f"{e(keep_label(res.screen.keep, meta['top_n']))}</span></header>")
            if not rk.top:
                out.append("<p class='muted'>No stock in this screen has a positive move for this timeframe.</p>")
            else:
                wanted = meta["top_n"] if res.screen.keep is None else res.screen.keep
                if wanted != KEEP_ALL and len(rk.top) < wanted:
                    out.append(f"<p class='muted'>Only {len(rk.top)} eligible stock(s) in this screen.</p>")
                show_today = tf.key != "change"
                out.append("<div class='wrap'><table><thead><tr><th class='n'>#</th><th>Ticker</th>"
                           "<th>Company</th><th>Sector</th><th class='n'>Price</th>"
                           f"<th class='n'>{e(tf.label)}</th>"
                           + ("<th class='n'>Today</th>" if show_today else "")
                           + "<th class='n'>Volume</th><th class='n'>Rel vol</th><th class='n'>Mkt cap</th>"
                           "</tr></thead><tbody>")
                for i, row in enumerate(rk.top, 1):
                    m = row.values.get(key)
                    ch = row.values.get("change")
                    out.append(
                        f"<tr><td class='n'>{i}</td><td><a href='https://finviz.com/quote.ashx?t="
                        f"{urllib.parse.quote(row.ticker)}'>{e(row.ticker)}</a></td>"
                        f"<td>{e(row.fields.get('company', ''))}</td><td>{e(row.fields.get('sector', ''))}</td>"
                        f"<td class='n'>{e(_fmt_num(row.values.get('price')))}</td>"
                        f"<td class='n {_cls(m)}'>{e(_fmt_pct(m))}</td>"
                        + (f"<td class='n {_cls(ch)}'>{e(_fmt_pct(ch))}</td>" if show_today else "")
                        + f"<td class='n'>{e(row.fields.get('volume', ''))}</td>"
                        f"<td class='n'>{e(row.fields.get('rel_volume', ''))}</td>"
                        f"<td class='n'>{e(row.fields.get('market_cap', ''))}</td></tr>"
                    )
                out.append("</tbody></table></div>")
            cc = p.change_check
            cc_text = ("not available (no Prev Close column)" if cc is None else
                       f"{cc['rows_agreeing']}/{cc['rows_checked']} rows agree ({cc['agreement_pct']}%)")
            out.append(
                "<details><summary>Diagnostics</summary>"
                f"<div>Source: <code>{e(res.screen.source)}</code></div>"
                f"<div>Percent units: {e(p.unit_evidence)}</div>"
                f"<div>Change vs Price/Prev Close: {e(cc_text)}</div>"
                f"<div>Columns used: {e(json.dumps(p.mapping))}</div>"
                + (f"<div>Duplicate tickers dropped: {p.duplicates_dropped}</div>" if p.duplicates_dropped else "")
                + (f"<div>Unparseable values: {e(json.dumps(p.unparseable))}</div>" if p.unparseable else "")
                + "</details></section>"
            )

    if failed:
        out.append("<h2>Failed screens</h2><div class='card'>")
        for res in failed:
            out.append(f"<p><strong>{e(res.screen.name)}</strong> <span class='muted'>({e(res.screen.source)})"
                       f"</span><br><span class='err'>{e(res.error or '')}</span></p>")
        out.append("</div>")
    out.append(f"<p class='muted'>finviz-gainers-agent {e(__version__)} &middot; not investment advice.</p>")
    out.append("</main></body></html>")
    return "\n".join(out)


def collect_tickers(results: Sequence[ScreenResult], timeframes: Sequence[Timeframe]) -> List[str]:
    """Every ticker listed in the report, once each, in report order
    (screen by screen, best rank first)."""
    tickers: List[str] = []
    seen: set = set()
    for tf in timeframes:
        for res in results:
            if res.status != "ok":
                continue
            for row in res.rankings[tf.key].top:
                if row.ticker not in seen:
                    seen.add(row.ticker)
                    tickers.append(row.ticker)
    return tickers


def resolve_timezone(name: Optional[str]):
    """None/'' -> None (this computer's time zone); otherwise an IANA zone such as
    'America/Los_Angeles'. Needs the system tz database or the 'tzdata' package."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        raise ConfigError(
            f"Unknown time zone {name!r}. Use an IANA name like America/Los_Angeles "
            "(on servers without a time-zone database: pip install tzdata)."
        ) from None


def render_tickers_txt(tickers: Sequence[str], run_utc: str, tz=None) -> str:
    """Line 1: run date and time (given zone, else this computer's). Line 2: tickers, comma-separated."""
    when = dt.datetime.fromisoformat(run_utc).astimezone(tz)
    return f"Run: {when:%Y-%m-%d %H:%M %Z}\n{','.join(tickers)}\n"


def write_outputs(results, timeframes, meta, out_dir: Path) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "html": out_dir / "report.html",
        "csv": out_dir / "top_gainers.csv",
        "json": out_dir / "summary.json",
        "tickers": out_dir / "tickers.txt",
    }
    paths["html"].write_text(render_html(results, timeframes, meta), encoding="utf-8")
    write_csv(results, timeframes, meta, paths["csv"])
    paths["json"].write_text(json.dumps(build_summary(results, timeframes, meta), indent=2), encoding="utf-8")
    tz = resolve_timezone(meta.get("timezone"))
    paths["tickers"].write_text(render_tickers_txt(collect_tickers(results, timeframes), meta["run_utc"], tz),
                                encoding="utf-8")
    latest = out_dir.parent / "latest_report.html"
    shutil.copyfile(paths["html"], latest)
    paths["latest"] = latest
    latest_tickers = out_dir.parent / "latest_tickers.txt"
    shutil.copyfile(paths["tickers"], latest_tickers)
    paths["latest_tickers"] = latest_tickers
    return paths


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _now_strings(tz=None) -> Tuple[str, str, str]:
    """(run time UTC ISO, human-readable run time, run-folder stamp in `tz` or local time)."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+

        et = now_utc.astimezone(ZoneInfo("America/New_York"))
        if tz is not None:
            mine = now_utc.astimezone(tz)
            local = f"{mine:%a %Y-%m-%d %H:%M %Z} ({et:%H:%M} ET)"
        else:
            local = f"{et:%a %Y-%m-%d %H:%M} ET ({now_utc:%H:%M} UTC)"
    except Exception:  # tz database missing (some Windows installs)
        local = f"{now_utc:%a %Y-%m-%d %H:%M} UTC"
    return now_utc.isoformat(timespec="seconds"), local, now_utc.astimezone(tz).strftime("%Y-%m-%d_%H%M%S")


def make_logger(quiet: bool, redact: Callable[[object], str]) -> Callable[[str], None]:
    def log(msg: str) -> None:
        if not quiet:
            print(redact(msg), file=sys.stderr, flush=True)
    return log


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="finviz_agent.py",
        description="Find the top N gainers in each FinViz screener preset (FinViz Elite export).",
    )
    ap.add_argument("--config", help="Path to screens.json (default: screens.json next to this script).")
    ap.add_argument("--timeframe", help=f"Comma-separated: {', '.join(TIMEFRAMES)} (default from config, else 'change').")
    ap.add_argument("--top", type=int, help="How many stocks per screen (default from config, else 10).")
    ap.add_argument("--only", help="Comma-separated screen names to run (others skipped).")
    ap.add_argument("--include-nonpositive", action="store_true",
                    help="Also rank stocks with zero/negative moves (default: gainers only).")
    ap.add_argument("--out", default="reports", help="Output folder (default: ./reports).")
    ap.add_argument("--token-file", help="Read the FinViz token from this file instead of FINVIZ_API_TOKEN.")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    ap.add_argument("--delay", type=float, default=1.0, help="Minimum seconds between FinViz requests (default 1.0).")
    ap.add_argument("--save-raw", action="store_true", help="Keep each screen's raw CSV in the run folder.")
    ap.add_argument("--offline", metavar="DIR", help="Read <screen-slug>.csv files from DIR instead of FinViz.")
    ap.add_argument("--dry-run", action="store_true", help="List screens and export URLs (token hidden), then exit.")
    ap.add_argument("--verify", action="store_true",
                    help="One-request health check: token, columns, units. Run this first.")
    ap.add_argument("--list-signals", action="store_true", help="List FinViz built-in signal screens and exit.")
    ap.add_argument("--quiet", action="store_true", help="Only print the final summary.")
    ap.add_argument("--timezone", metavar="ZONE",
                    help="Time zone for run times and folder names, e.g. America/Los_Angeles "
                         "(default: this computer's).")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap.parse_args(argv)


def _verify(client: FinvizClient, timeframes: Sequence[Timeframe]) -> int:
    screen = screen_from_signal("Top Gainers")
    print(f"Requesting: {client.redacted_url(build_export_params(screen, timeframes[0]))}")
    text = client.export_csv(build_export_params(screen, timeframes[0]))
    parsed = parse_export(text, ["ticker"] + [tf.column_key for tf in timeframes])
    print(f"OK: token accepted, {len(parsed.rows)} rows returned. HTTPS trust store: {client.ca_source}")
    print(f"Headers received: {parsed.header}")
    unmapped = [k for k in COLUMN_BY_KEY if k not in parsed.mapping]
    print(f"Columns mapped: {len(parsed.mapping)}/{len(COLUMNS)}" + (f" (missing: {unmapped})" if unmapped else ""))
    print(f"Percent units: {parsed.unit_evidence}")
    cc = parsed.change_check
    if cc:
        print(f"Change vs Price/Prev Close: {cc['rows_agreeing']}/{cc['rows_checked']} agree ({cc['agreement_pct']}%)")
    for tf in timeframes:
        rk = rank_rows(parsed.rows, tf, 5, False)
        print(f"Top 5 Top-Gainers by {tf.label}: "
              + ", ".join(f"{r.ticker} {_fmt_pct(r.values[tf.column_key])}" for r in rk.top))
    healthy = parsed.unit_verified and (cc is None or cc["agreement_pct"] >= 90.0)
    print("RESULT: healthy" if healthy else "RESULT: check the warnings above before relying on results")
    return EXIT_OK if healthy else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_signals:
        for name, code in SIGNALS.items():
            print(f"{name:28s} s={code}")
        return EXIT_OK

    redact: Callable[[object], str] = Redactor()
    try:
        cfg_path = Path(args.config) if args.config else Path(__file__).resolve().with_name("screens.json")
        cfg = load_config(cfg_path)
        timeframes = parse_timeframes(args.timeframe or cfg.get("timeframes", ["change"]))
        top_n = args.top if args.top is not None else int(cfg.get("top_n", 10))
        if top_n < 1:
            raise ConfigError("--top must be at least 1.")
        include_nonpositive = args.include_nonpositive or bool(cfg.get("include_nonpositive", False))
        only = args.only.split(",") if args.only else None
        screens = build_screens(cfg, only)
        display_tz = resolve_timezone(args.timezone)  # fail fast on a bad zone name

        token = None if (args.offline or args.dry_run) else read_token(args.token_file)
        redact = Redactor([token] if token else [])
        log = make_logger(args.quiet, redact)

        if args.dry_run:
            preview = FinvizClient("TOKEN", base_url=args.base_url)
            for s in screens:
                print(f"{s.name:30s} {s.kind:14s} {preview.redacted_url(build_export_params(s, timeframes[0]))}")
            return EXIT_OK

        client = None
        if not args.offline:
            client = FinvizClient(token or "", base_url=args.base_url, min_interval=args.delay, log=log)
        if args.verify:
            if client is None:
                raise ConfigError("--verify needs a live connection; drop --offline.")
            return _verify(client, timeframes)

        if args.offline:
            offline_dir = Path(args.offline)

            def fetch(screen: Screen) -> str:
                path = offline_dir / f"{screen.slug}.csv"
                if not path.is_file():
                    raise FetchError(f"Offline file not found: {path}")
                return path.read_text(encoding="utf-8-sig")
        else:
            def fetch(screen: Screen) -> str:
                return client.export_csv(build_export_params(screen, timeframes[0]))

        run_utc, run_local, stamp = _now_strings(display_tz)
        out_dir = Path(args.out) / stamp
        while out_dir.exists():  # another scan started this same second: never overwrite it
            time.sleep(0.25)
            run_utc, run_local, stamp = _now_strings(display_tz)
            out_dir = Path(args.out) / stamp
        log(f"Scanning {len(screens)} screen(s); ranking by {', '.join(tf.label for tf in timeframes)}")
        results = run_scan(
            screens, fetch, timeframes, top_n, include_nonpositive,
            raw_dir=(out_dir / "raw") if args.save_raw else None, log=log,
        )
        meta = {
            "agent_version": __version__,
            "run_utc": run_utc,
            "run_local": run_local,
            "timezone": args.timezone or "",
            "source": "offline:" + str(args.offline) if args.offline else redact(args.base_url),
            "timeframes": [tf.key for tf in timeframes],
            "top_n": top_n,
            "keep": {s.name: keep_label(s.keep, top_n) for s in screens if s.keep is not None},
            "include_nonpositive": include_nonpositive,
            "screens_ok": sum(r.status == "ok" for r in results),
            "screens_failed": sum(r.status != "ok" for r in results),
            "requests_made": client.requests_made if client else 0,
        }
        paths = write_outputs(results, timeframes, meta, out_dir)
    except CertificateError as err:
        print(f"CERTIFICATE ERROR: {redact(err)}", file=sys.stderr)
        return EXIT_TLS
    except AuthError as err:
        print(f"AUTH ERROR: {redact(err)}", file=sys.stderr)
        return EXIT_AUTH
    except ConfigError as err:
        print(f"CONFIG ERROR: {redact(err)}", file=sys.stderr)
        return EXIT_CONFIG
    except AgentError as err:  # e.g. --verify fetch/parse failure
        print(f"ERROR: {redact(err)}", file=sys.stderr)
        return 1

    ok = meta["screens_ok"]
    print(f"Done: {ok}/{len(results)} screens scanned.")
    print(f"Report: {paths['html']}")
    print(f"CSV:    {paths['csv']}")
    print(f"JSON:   {paths['json']}")
    print(f"Tickers: {paths['tickers']}")
    if meta["screens_failed"]:
        print(f"{meta['screens_failed']} screen(s) failed; see the report.", file=sys.stderr)
    return EXIT_OK if ok else EXIT_ALL_FAILED


if __name__ == "__main__":
    sys.exit(main())
