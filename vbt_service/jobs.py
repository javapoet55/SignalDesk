"""Durable research jobs. PostgreSQL owns job state; Redis wakes idle workers."""

import os
import time
from uuid import UUID

import psycopg
import redis
from psycopg.types.json import Jsonb


def database():
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5)


def initialize():
    with database() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS vbt_research (
            id uuid PRIMARY KEY, request jsonb NOT NULL, result jsonb, error text,
            created timestamptz NOT NULL DEFAULT now())""")
        db.execute("ALTER TABLE vbt_research ADD COLUMN IF NOT EXISTS elapsed_seconds double precision")


def wake():
    return redis.Redis.from_url(
        os.environ["REDIS_URL"], socket_connect_timeout=2, socket_timeout=8
    )


def submit(key, data):
    with database() as db:
        db.execute(
            "INSERT INTO vbt_research(id, request) VALUES(%s,%s)", (key, Jsonb(data))
        )
    try:
        channel = wake()
        channel.lpush("vbt:jobs", str(key))
        channel.ltrim("vbt:jobs", 0, 99)
    except redis.RedisError:
        pass
    return {"id": str(key)}


def lookup(key: UUID):
    with database() as db:
        row = db.execute(
            "SELECT request-'bars'-'series'-'session_highs'-'target_bars', result, error, elapsed_seconds FROM vbt_research WHERE id=%s",
            (key,),
        ).fetchone()
        return dict(inputs=row[0], result=row[1], error=row[2], elapsed_seconds=row[3]) if row else None


def work():
    from .engine import calculate

    initialize()
    while True:
        with database() as db:
            row = db.execute("""SELECT id, request FROM vbt_research WHERE result IS NULL AND error IS NULL
                              ORDER BY created LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if row:
                key, request = row
                result, error = None, None
                try:
                    result = Jsonb(calculate(request))
                except Exception as exc:
                    error = str(exc)[:300]
                # The transaction starts before calculate(). Use the clock at completion.
                db.execute(
                    """UPDATE vbt_research SET result=%s, error=%s,
                       elapsed_seconds=%s + GREATEST(0, EXTRACT(EPOCH FROM clock_timestamp() - created))
                       WHERE id=%s""",
                    (result, error, request.get("preparation_seconds", 0), key),
                )
                continue
        try:
            wake().brpop("vbt:jobs", timeout=5)
        except redis.RedisError:
            time.sleep(5)


if __name__ == "__main__":
    work()
