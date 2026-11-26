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
            "SELECT request-'bars', result, error FROM vbt_research WHERE id=%s", (key,)
        ).fetchone()
        return dict(inputs=row[0], result=row[1], error=row[2]) if row else None


def work():
    from .engine import calculate

    initialize()
    while True:
        with database() as db:
            row = db.execute("""SELECT id, request FROM vbt_research WHERE result IS NULL AND error IS NULL
                              ORDER BY created LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if row:
                try:
                    result = calculate(row[1])
                except Exception as exc:
                    db.execute(
                        "UPDATE vbt_research SET error=%s WHERE id=%s",
                        (str(exc)[:300], row[0]),
                    )
                else:
                    db.execute(
                        "UPDATE vbt_research SET result=%s WHERE id=%s",
                        (Jsonb(result), row[0]),
                    )
                continue
        try:
            wake().brpop("vbt:jobs", timeout=5)
        except redis.RedisError:
            time.sleep(5)


if __name__ == "__main__":
    work()
