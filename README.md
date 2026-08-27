# Coupon Redemption Service

FastAPI + aiosqlite implementation of the coupon redemption service described in
[`mdFiles/problemStatement.md`](mdFiles/problemStatement.md), built to the plan in
[`mdFiles/plan.md`](mdFiles/plan.md).

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
```

## Running

**Single process, single worker.** The correctness argument (plan §3) rests on a
module-level `asyncio.Lock`, which only serialises writers within one process. A
multi-worker or gunicorn deployment would break it.

```bash
uv run uvicorn app.main:app --workers 1
```

Interactive docs: <http://127.0.0.1:8000/docs>

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `COUPONS_DB_PATH` | `coupons.db` | Path to the SQLite database file |

## Tests

```bash
uv run pytest
```

---

The full write-up — topology assumption, the invariant argument and the ruling
table from plan §5 — lands with issue #15.
