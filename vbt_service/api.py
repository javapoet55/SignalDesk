import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, FiniteFloat
from . import jobs


class Bar(BaseModel):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    open: FiniteFloat = Field(gt=0)
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


class Replay(BacktestRequest):
    source: str = Field(default="Archived signals", max_length=64)
    observed_days: int = Field(ge=1, le=3000)


class Research(Replay):
    bars: list[Bar] = Field(min_length=4, max_length=3000)


class BasketResearch(Replay):
    series: dict[
        Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")],
        Annotated[list[Bar], Field(min_length=4, max_length=252)],
    ] = Field(min_length=2, max_length=5000)


class Cohort(BacktestRequest):
    source: str
    close_marks: dict[str, FiniteFloat | None] = Field(
        default_factory=dict, max_length=253
    )
    signal_day: str
    dates: list[str] = Field(min_length=2, max_length=11)
    elapsed: list[bool] = Field(min_length=2, max_length=11)
    prices: dict[str, list[FiniteFloat | None]] = Field(min_length=1, max_length=5000)


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


@app.get("/jobs", dependencies=[Depends(authorized)])
def history():
    with jobs.database() as db:
        rows = db.execute("""SELECT id, request->>'strategy', request->>'symbol', created,
                           CASE WHEN error IS NOT NULL THEN 'Failed' WHEN result IS NULL THEN 'Queued or running'
                           ELSE 'Completed' END FROM vbt_research ORDER BY created DESC LIMIT 100""").fetchall()
    return [
        dict(
            id=str(key),
            strategy=strategy,
            symbol=symbol,
            created=created,
            status=status,
        )
        for key, strategy, symbol, created, status in rows
    ]


@app.get("/runs/{run}/latest-backtest", dependencies=[Depends(authorized)])
def latest_backtest(run: str):
    with jobs.database() as db:
        row = db.execute(
            """SELECT id, request-'bars'-'series', result, elapsed_seconds FROM vbt_research
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
