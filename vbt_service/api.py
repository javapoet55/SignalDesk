import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, FiniteFloat, model_validator

from . import indicators as indicator_rules
from . import jobs
from .performance import performance


class Bar(BaseModel):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    open: FiniteFloat = Field(gt=0)
    high: FiniteFloat | None = Field(default=None, gt=0)
    low: FiniteFloat | None = Field(default=None, gt=0)
    close: FiniteFloat = Field(gt=0)
    signal: bool


class BacktestRequest(BaseModel):
    run: str = Field(max_length=200)
    strategy: str = Field(max_length=150)
    version: str = Field(max_length=64)
    symbol: str = Field(max_length=15)
    holds: list[int] = Field(min_length=1, max_length=10)
    fee_bps: FiniteFloat = Field(ge=0, le=100)
    preparation_seconds: FiniteFloat = Field(default=0, ge=0)
    entry_basis: Literal["next_open", "signal_close"] = "next_open"
    exit_on_target: bool = False
    profit_target_pct: FiniteFloat | None = Field(default=None, gt=0)
    stop_loss_pct: FiniteFloat | None = Field(default=None, gt=0, lt=100)


class Replay(BacktestRequest):
    source: str = Field(default="Archived signals", max_length=64)
    warmup_sessions: dict[str, Annotated[int, Field(ge=1, le=252)]] = Field(default_factory=dict, max_length=5000)
    observed_days: int = Field(ge=1, le=3000)
    skipped_symbols: dict[str, Annotated[str, Field(max_length=10)]] = Field(default_factory=dict, max_length=5000)
    candle_days: int = Field(default=1, ge=1, le=5)


class Research(Replay):
    bars: list[Bar] = Field(min_length=4, max_length=3000)


class BasketResearch(Replay):
    series: dict[
        Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")],
        Annotated[list[Bar], Field(min_length=4, max_length=252)],
    ] = Field(min_length=2, max_length=5000)


class SessionPrices(BaseModel):
    open: FiniteFloat | None
    high: FiniteFloat | None
    low: FiniteFloat | None = None
    close: FiniteFloat | None


class Cohort(BacktestRequest):
    target_days: list[str] = Field(default_factory=list, max_length=60)
    completed_sessions: int = Field(default=0, ge=0, le=60)
    target_bars: dict[str, Annotated[list[SessionPrices], Field(max_length=60)]] = Field(default_factory=dict, max_length=5000)
    source: str
    session_highs: dict[str, Annotated[list[FiniteFloat | None], Field(max_length=252)]] = Field(default_factory=dict, max_length=5000)
    close_marks: dict[str, FiniteFloat | None] = Field(
        default_factory=dict, max_length=253
    )
    signal_day: str
    dates: list[str] = Field(min_length=2, max_length=11)
    elapsed: list[bool] = Field(min_length=2, max_length=11)
    prices: dict[str, list[FiniteFloat | None]] = Field(min_length=1, max_length=5000)


class IndicatorBar(BaseModel):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    open: FiniteFloat | None = Field(default=None, gt=0)
    high: FiniteFloat | None = Field(default=None, gt=0)
    low: FiniteFloat | None = Field(default=None, gt=0)
    close: FiniteFloat = Field(gt=0)
    volume: FiniteFloat | None = Field(default=None, ge=0)


class IndicatorSpec(BaseModel):
    name: str = Field(max_length=20)
    params: dict[Annotated[str, Field(max_length=20)], FiniteFloat] = Field(default_factory=dict, max_length=4)

    @model_validator(mode="after")
    def known(self):
        self.params = indicator_rules.normalize(self.name, self.params)
        return self


class IndicatorRequest(BaseModel):
    """Indicators on saved daily prices. Stored beside backtests but never listed or run as one."""

    kind: Literal["indicators"]
    price_run: str = Field(max_length=200)
    source: str = Field(max_length=64)
    display_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    preparation_seconds: FiniteFloat = Field(default=0, ge=0)
    indicators: list[IndicatorSpec] = Field(min_length=1, max_length=indicator_rules.MAX_INDICATORS)
    series: dict[
        Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")],
        Annotated[list[IndicatorBar], Field(min_length=2, max_length=indicator_rules.MAX_BARS)],
    ] = Field(min_length=1, max_length=indicator_rules.MAX_SYMBOLS)


def authorized(authorization: str = Header(default="")):
    if not secrets.compare_digest(
        authorization.encode(), ("Bearer " + os.environ["VBT_SERVICE_TOKEN"]).encode()
    ):
        raise HTTPException(403)


@asynccontextmanager
async def lifespan(app):
    if not os.environ.get("VBT_SERVICE_TOKEN"):
        raise RuntimeError("Set the private service token.")
    jobs.initialize()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def health():
    return {"ok": True}


@app.post("/jobs", dependencies=[Depends(authorized)], status_code=202)
def submit(data: Research | BasketResearch | Cohort):
    request = data.model_dump()
    if isinstance(data, BasketResearch):
        request["symbols"] = list(data.series)
    return jobs.submit(uuid4(), request)


@app.post("/indicators", dependencies=[Depends(authorized)], status_code=202)
def submit_indicators(data: IndicatorRequest):
    request = data.model_dump()
    request["symbols"] = list(data.series)
    return jobs.submit(uuid4(), request)


@app.get("/jobs", dependencies=[Depends(authorized)])
def history():
    with jobs.database() as db:
        # Project only the request fields used for scoring. The historical price series stays in Postgres.
        rows = db.execute("""
            SELECT id, created, cfg.strategy, cfg.symbol, to_jsonb(cfg),
                   CASE WHEN error IS NOT NULL THEN 'Failed' WHEN result IS NULL THEN 'Queued or running'
                        ELSE 'Completed' END, CASE WHEN error IS NULL THEN result END
            FROM (SELECT id, created, request, result, error FROM vbt_research
                  WHERE request->>'kind' IS DISTINCT FROM 'indicators'
                  ORDER BY created DESC, id DESC LIMIT 100) recent
            CROSS JOIN LATERAL jsonb_to_record(recent.request) AS cfg(
                strategy text, symbol text, profit_target_pct float8, exit_on_target boolean,
                dates jsonb, close_marks jsonb, fee_bps float8)
            ORDER BY created DESC, id DESC
        """).fetchall()
    items = []
    for key, created, strategy, symbol, info, status, result in rows:
        summary = performance(info, result) if result else None
        items.append(dict(id=str(key), strategy=strategy, symbol=symbol, created=created, status=status,
                          win_rate_pct=summary.win_rate_pct if summary else None,
                          total_return_pct=summary.total_return_pct if summary else None))
    return items


@app.get("/runs/{run}/latest-backtest", dependencies=[Depends(authorized)])
def latest_backtest(run: str):
    with jobs.database() as db:
        row = db.execute(
            """SELECT id, request-'bars'-'series'-'session_highs'-'target_bars', result, elapsed_seconds FROM vbt_research
               WHERE request->>'run' = %s AND result IS NOT NULL AND error IS NULL
               ORDER BY created DESC, id DESC LIMIT 1""",
            (run,),
        ).fetchone()
    return {"id": str(row[0]), "inputs": row[1], "result": row[2], "error": None, "elapsed_seconds": row[3]} if row else None


@app.get("/jobs/{key}", dependencies=[Depends(authorized)])
def result(key: UUID):
    item = jobs.lookup(key)
    if item is None:
        raise HTTPException(404)
    return item
