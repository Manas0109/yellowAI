# Coupon Redemption Service

A coupon redemption service for e-commerce checkout, built around one question:
**where does `redeemed_count <= max_redemptions` get enforced atomically?**
Idempotency, cancel-once, and expiry-at-the-instant all fall out of that answer.

FastAPI + SQLite (file-backed, WAL), single process. 129 tests.

---

## Running it

```bash
uv sync
uv run uvicorn app.main:app --workers 1
```

The database file defaults to `coupons.db`; override with `COUPONS_DB_PATH`.
Schema is created at startup — no migration step.

> **`--workers 1` is not a suggestion.** See [Scope and honest limits](#scope-and-honest-limits).

### The acceptance run

```bash
uv run pytest tests/test_acceptance.py -v -s
```

`-s` matters — without it pytest swallows the report and you get bare `PASSED`
lines. This is the run to read: it prints the numbers each assertion rests on,
plus live in-flight concurrency while each burst executes.

### Everything

```bash
uv run pytest -q                       # full suite, 129 tests
uv run pytest tests/test_concurrency.py -q   # burst tests only
```

---

## API

| Endpoint | Behaviour |
|---|---|
| `POST /coupons` | Seed. `{code, max_redemptions, discount_percent, expires_at, type}` → 201. Duplicate code → 409 `CODE_ALREADY_EXISTS`. |
| `POST /redeem` | Header `Idempotency-Key` required. `{code, customer_id, order_id}` → `{success, remaining, discount_percent}`. |
| `POST /orders/{order_id}/cancel` | Returns the slot. **Always 200.** |
| `GET /coupons/{code}` | `{code, redeemed_count, remaining, max_redemptions}`. |

Every failure returns the same two-key envelope — clients branch on `error`,
never on the status:

```json
{ "error": "NO_REDEMPTIONS_LEFT", "message": "Coupon SAVE20 is fully redeemed." }
```

| Error | HTTP |
|---|---|
| `UNKNOWN_CODE` | 404 |
| `COUPON_EXPIRED` | 410 |
| `CUSTOMER_ALREADY_REDEEMED` | 409 |
| `ORDER_ALREADY_HAS_REDEMPTION` | 409 |
| `NO_REDEMPTIONS_LEFT` | 409 |
| `CODE_ALREADY_EXISTS` | 409 |
| `IDEMPOTENCY_KEY_REUSE` | 422 |

**Error precedence is a correctness property, not cosmetics.** A request can be
expired *and* already-used *and* out of slots at once. Checks run
permanent-before-transient:

```
UNKNOWN_CODE → COUPON_EXPIRED → CUSTOMER_ALREADY_REDEEMED
             → ORDER_ALREADY_HAS_REDEMPTION → NO_REDEMPTIONS_LEFT
```

Report the condition the client can do least about. A coupon that is both
expired and empty says *expired* — otherwise the client retries forever.

---

## How each rule was verified

| Rule | Mechanism | Test |
|---|---|---|
| Never exceeds `max_redemptions`, even under burst | `asyncio.Lock` + `UPDATE … WHERE redeemed_count < max_redemptions`, rejection driven off `rowcount` | `test_acceptance.py::test_scenario_1` — 200 concurrent redeems, 50 slots → exactly 50/150 |
| STANDARD once per customer, **ever** | Partial unique index spanning cancelled rows | `test_db.py::test_standard_is_once_per_customer_even_after_cancellation` |
| STACKABLE has no per-customer limit, still capped globally | `coupon_type` predicate on the index | `test_redeem.py::test_stackable_has_no_per_customer_limit` |
| Expiry resolves consistently at the instant | `clock.now()` read **once** inside the lock | `test_redeem.py::test_expiry_boundary` — −1µs / exactly / +1µs |
| Cancel returns the slot once, even called twice | Guarded soft-cancel; refund only follows a state change we made | `test_scenario_3` and `3b` (40 concurrent cancels) |
| Retry must not double-redeem | Key record + counter in one transaction | `test_scenario_2` and `2b` (50 simultaneous retries) |
| Count correct at all times | Single writer, whole transactions, stored counter | Autouse invariant after every service test |

### The invariant

After **every** test that drove the service, for every coupon:

```
0 <= redeemed_count <= max_redemptions
redeemed_count == COUNT(*) FROM redemptions WHERE code = ? AND status = 'ACTIVE'
```

Verified to actually fire: a probe that corrupts `redeemed_count` behind the
service's back makes the suite error. An assertion that never fails is worse
than no assertion.

### Proving the concurrency tests have teeth

A burst test that passes proves nothing until you've seen it fail. Two checks:

**Do requests actually overlap?** The acceptance run reports peak in-flight
calls and *asserts* `peak > 1`:

```
  Calls entered service         200
  Peak concurrent in service    200
  Overlap confirmed             yes — requests genuinely interleaved
```

A serialised implementation prints the identical 50/150 summary. Peak in-flight
is what distinguishes "handled 200 concurrent requests correctly" from "handled
them one at a time."

**Does the suite catch a naive implementation?** Replacing the conditional
`UPDATE` with a read-then-write and disabling the lock makes all six burst
tests fail. Restoring them makes all 129 pass.

---

## Rulings on ambiguity in the spec

The spec is under-determined in places. Each call is deliberate.

| Question | Ruling |
|---|---|
| `t == expires_at` | **Expired.** Valid strictly while `now < expires_at`. |
| Does cancelling restore a STANDARD coupon's per-customer eligibility? | **No.** "ever" is read as load-bearing: the global slot returns, the customer stays burned. Closes the redeem→cancel→redeem loop. |
| Can a cancelled `order_id` redeem again? | **No.** Permanently consumed, so an `order_id` always maps to exactly one redemption record. |
| Is a replayed success an error? | **No.** 200 with a `replay` marker. The spec lists replay among the failure modes, but a retried success is a success. |
| Is a *failed* attempt replayed? | **No.** Only successes are stored. A retry after `NO_REDEMPTIONS_LEFT` can legitimately succeed if a cancellation freed a slot. |
| Cancel of an unknown order | 200 no-op with a `reason`. This service only knows coupon-bearing orders, so 404 would be wrong for a legitimate coupon-less one. |

The sharpest of these is the second. `test_cancel_returns_the_slot_but_not_the_customers_eligibility`
pins it: after a cancel, `remaining` goes back up and a *different* customer can
take the freed slot, while the original customer still gets
`CUSTOMER_ALREADY_REDEEMED`.

---

## How it works

**One writer.** A module-level `asyncio.Lock` is held across the whole redeem or
cancel transaction. `now` is read **once**, inside the lock, and threaded
through every check — a second clock read could straddle the expiry instant and
let one request resolve two ways.

**The counter moves conditionally.** `UPDATE … WHERE redeemed_count <
max_redemptions`, with the rejection driven off `rowcount`. The check and the
increment are one statement, so there is no read-then-write window.

**Failures leave nothing behind.** Any error raises, rolling the transaction
back. That is what lets a retry re-evaluate against current state instead of
replaying a rejection that is no longer true.

**No `PENDING` state for in-flight duplicates.** A duplicate `Idempotency-Key`
arriving while the first request is still running blocks on the lock; by the
time it acquires it, the first transaction has committed and its record is
visible, so it replays. That deletes a whole subsystem — no 409-in-progress, no
waiter machinery, no crash-recovery policy for half-written rows. It is the
concrete payoff of holding one lock across the transaction.

**Reads don't take the lock.** With a single writer committing whole
transactions, a reader sees state strictly before or strictly after a write,
never torn — so `GET` is correct at all times without serialising behind
in-flight checkouts.

---

## Scope and honest limits

**Correctness here lives in an in-memory lock.** It is a property of the
process, not of the data. Run this with `--workers 4`, or as two instances
against a shared database, and the redemption cap no longer holds.

The database-level defences — `BEGIN IMMEDIATE`, the conditional `UPDATE`, the
UNIQUE constraints — are real and tested, but they are **not** what makes today's
code correct. All requests share one SQLite connection, and SQLite cannot nest
transactions on it: with the lock removed, overlapping redeems don't degrade
into a race, they fail outright with *"cannot start a transaction within a
transaction"*. That is verified, not assumed. Those guards are a second line of
defence against a *different* failure — a future connection pool or multi-worker
deploy, where overlapping transactions become possible and a read-then-write
would silently lose updates.

So: the acceptance results demonstrate the rules hold under genuine concurrency
**within one process**. They are not evidence for a multi-instance deployment.
Getting there means moving the guarantee into the database — row locks
(`SELECT … FOR UPDATE`) plus the conditional `UPDATE` already present — and, on
MySQL, replacing the partial unique index (SQLite/Postgres only) with a
generated column that is `CONCAT(code, ':', customer_id)` for STANDARD and
`NULL` for STACKABLE, since MySQL permits duplicate NULLs in a unique index.

Also out of scope: auth, rate limiting, migrations, idempotency-key reaping
(24h TTL is documented policy; `created_at` is stored, nothing sweeps),
minimum-spend rules, and currency handling.

---

## Layout

```
app/
  main.py      routes, exception handlers, lifespan
  service.py   redeem() / cancel() — the lock and the transactions
  db.py        connection, pragmas, schema DDL
  schemas.py   Pydantic v2 models
  errors.py    CouponError hierarchy → error code + HTTP status
  clock.py     injectable UTC clock
tests/
  test_acceptance.py    the three headline scenarios, instrumented
  test_concurrency.py   burst tests
  test_redeem.py        precedence chain, expiry boundary
  test_cancel.py        refund-once, the "ever" ruling
  test_idempotency.py   replay, key reuse
  test_db.py            constraints and pragmas
  conftest.py           temp-file DB, frozen clock, invariant fixture
```
