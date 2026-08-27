# Coupon Redemption Service — MVP Plan

Derived from `problemStatement.md`. Every decision below was made explicitly; the
"Why" notes exist so the reasoning survives into the README.

---

## 1. Stack & topology

| Choice | Value |
|---|---|
| Language | Python 3.12 (uv-managed venv, already present) |
| Framework | FastAPI + Pydantic v2 |
| Server | uvicorn, **single process, single worker** |
| Storage | SQLite, file-backed, WAL mode |
| Driver | `aiosqlite` (real async — does not fake concurrency by blocking the loop) |
| Tests | pytest + pytest-asyncio + httpx `ASGITransport` |

Dependencies: `fastapi`, `uvicorn[standard]`, `aiosqlite`, `pydantic`,
`httpx`, `pytest`, `pytest-asyncio`.

**Topology matters:** the correctness argument assumes one process. This is stated
loudly in the README, because a `--workers 4` deploy would break the primary
mechanism (see §3).

---

## 2. Data model

### `coupons`
| Column | Type | Notes |
|---|---|---|
| `code` | TEXT PK | immutable once created |
| `max_redemptions` | INTEGER | `CHECK > 0` |
| `discount_percent` | REAL | `CHECK > 0 AND <= 100` |
| `expires_at` | TEXT | ISO-8601 UTC, always stored normalised to UTC |
| `type` | TEXT | `CHECK IN ('STANDARD','STACKABLE')` |
| `redeemed_count` | INTEGER | default 0, `CHECK >= 0` |
| `created_at` | TEXT | |

### `redemptions`
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `order_id` | TEXT | **UNIQUE across all rows** — an order_id is permanently consumed |
| `code` | TEXT | FK → coupons |
| `customer_id` | TEXT | |
| `coupon_type` | TEXT | denormalised from the coupon, so a partial index can key on it |
| `status` | TEXT | `ACTIVE` \| `CANCELLED` — soft-cancel, rows are never deleted |
| `redeemed_at`, `cancelled_at` | TEXT | |

```sql
CREATE UNIQUE INDEX ux_standard_once_per_customer
  ON redemptions(code, customer_id)
  WHERE coupon_type = 'STANDARD';
```

The partial index spans **all** rows including `CANCELLED` ones. That is the
mechanism behind the "ever" ruling in §5 — the DB enforces it even if the
service-layer check is ever removed.

### `idempotency_keys`
| Column | Type | Notes |
|---|---|---|
| `key` | TEXT PK | global scope, not per-customer |
| `request_hash` | TEXT | SHA-256 of canonical `{code, customer_id, order_id}` |
| `response_body` | TEXT | JSON of the original **success** response |
| `created_at` | TEXT | 24h TTL is documented policy; no reaper in MVP |

Only successful redemptions are recorded. Failures leave no side effect, so a
retry re-evaluates against current state.

---

## 3. Concurrency mechanism

**Primary:** a single module-level `asyncio.Lock` held across the entire
redeem and cancel transactions. One writer, ever. Trivially auditable.

**Secondary (defence in depth):** the counter is still moved with a guarded,
conditional statement, so no read-then-write window exists even without the lock:

```sql
UPDATE coupons
   SET redeemed_count = redeemed_count + 1
 WHERE code = ? AND redeemed_count < max_redemptions;
-- rowcount == 0  →  NO_REDEMPTIONS_LEFT
```

Plus `BEGIN IMMEDIATE` on every write transaction, and the UNIQUE constraints
above as the last line of defence.

**Free win from the lock:** a duplicate `Idempotency-Key` arriving while the
first request is still in flight simply blocks on the lock, then finds the
committed key record and replays it. No 409-in-progress state, no waiter
machinery, no crash-recovery policy for `PENDING` rows.

**Clock discipline:** `now` is captured **once**, inside the lock, at the top of
the transaction, and threaded through every check. No second `datetime.now()`
exists anywhere in the request path. The clock is injectable so tests can pin the
exact expiry microsecond.

---

## 4. Endpoints

### `POST /coupons`
Body: `{ code, max_redemptions, discount_percent, expires_at, type }`
- Validation via Pydantic: `max_redemptions >= 1`, `0 < discount_percent <= 100`,
  `expires_at` parseable and tz-aware (naive input assumed UTC), `type` in enum.
- Duplicate code → **409 `CODE_ALREADY_EXISTS`**. Codes are immutable; allowing a
  re-seed to reset `redeemed_count` would be the nastiest possible bug here.
- → `201 { code, max_redemptions, redeemed_count: 0, remaining, expires_at, type }`

### `POST /redeem` (header `Idempotency-Key`, required)
Body: `{ code, customer_id, order_id }`

Algorithm, entirely under the lock, one `BEGIN IMMEDIATE` transaction:

1. `now = clock.now()`
2. Look up the idempotency key.
   - found, hash matches → return stored body, `replay: true`, `200`
   - found, hash differs → **422 `IDEMPOTENCY_KEY_REUSE`**
3. Load coupon → missing → **404 `UNKNOWN_CODE`**
4. `now >= expires_at` → **410 `COUPON_EXPIRED`**
5. `type == STANDARD` and any redemption exists for `(code, customer_id)`
   *regardless of status* → **409 `CUSTOMER_ALREADY_REDEEMED`**
6. `order_id` already present → **409 `ORDER_ALREADY_HAS_REDEMPTION`**
7. Conditional `UPDATE` (§3); rowcount 0 → **409 `NO_REDEMPTIONS_LEFT`**
8. `INSERT` redemption (`ACTIVE`)
9. `INSERT` idempotency key + serialised response
10. `COMMIT` — steps 7–9 are one transaction. A crash cannot burn a slot without
    a redemption record, or record a key without the increment.

Success → `200 { success: true, remaining, discount_percent }`
Replay → `200 { success: true, remaining: <frozen at first attempt>, replay: true }`
plus an `Idempotency-Replayed: true` header.

**Error precedence** (documented and pinned by a test that builds a
triply-invalid request): permanent conditions before transient ones, so a client
is never told to retry a coupon that is expired.

```
UNKNOWN_CODE → COUPON_EXPIRED → CUSTOMER_ALREADY_REDEEMED
             → ORDER_ALREADY_HAS_REDEMPTION → NO_REDEMPTIONS_LEFT
```

### `POST /orders/{order_id}/cancel`
Under the lock, one transaction. Always `200` — cancel is inherently idempotent
and must never punish a retry.

- no redemption for `order_id` → `{ cancelled: false, reason: "ORDER_NOT_FOUND" }`
- already `CANCELLED` → `{ cancelled: false, reason: "ALREADY_CANCELLED" }`
- otherwise: guarded `UPDATE redemptions SET status='CANCELLED' WHERE order_id=? AND status='ACTIVE'`
  (rowcount 0 ⇒ someone beat us, abort the refund), then
  `UPDATE coupons SET redeemed_count = redeemed_count - 1 WHERE code=? AND redeemed_count > 0`
  → `{ cancelled: true, code, remaining }`

Cancellation works on an **expired** coupon — expiry gates redemption, not refunds.
Cancellation does **not** free the customer's STANDARD eligibility (§5).

### `GET /coupons/{code}`
→ `200 { code, redeemed_count, remaining, max_redemptions }`, or 404.

Read outside the lock. With a single writer committing whole transactions, a
reader sees the state strictly before or strictly after a write — never torn.
`remaining = max_redemptions - redeemed_count`, and `redeemed_count` always
equals `COUNT(*) WHERE status='ACTIVE'`. Both are asserted to agree in tests.

---

## 5. Rulings on ambiguity in the spec

| Question | Ruling |
|---|---|
| `t == expires_at` | **Expired.** Valid strictly while `now < expires_at`. |
| Does cancel restore STANDARD per-customer eligibility? | **No.** "ever" is read as load-bearing. The global slot returns; the customer stays burned. Closes the redeem→cancel→redeem cycle. |
| Can a cancelled `order_id` redeem again? | **No.** `order_id` is permanently consumed, so it always maps to exactly one redemption record. |
| Is a replayed success an error? | **No.** `200` with a replay marker. The spec lists it among failure modes, but a retried success is a success. |
| Does a failed attempt get replayed? | **No.** Only successes are stored; failures re-evaluate against current state. A retry after `NO_REDEMPTIONS_LEFT` can succeed if a cancellation freed a slot. |
| Cancel of an unknown order | `200` no-op with a `reason`. The service only knows coupon-bearing orders, so 404 would be wrong for a legitimate coupon-less order. |

---

## 6. Error response shape

Every failure returns the same envelope; clients branch on `error`, not status.

```json
{ "error": "NO_REDEMPTIONS_LEFT", "message": "Coupon SAVE20 is fully redeemed." }
```

| Code | HTTP |
|---|---|
| `UNKNOWN_CODE` | 404 |
| `COUPON_EXPIRED` | 410 |
| `CUSTOMER_ALREADY_REDEEMED` | 409 |
| `ORDER_ALREADY_HAS_REDEMPTION` | 409 |
| `NO_REDEMPTIONS_LEFT` | 409 |
| `CODE_ALREADY_EXISTS` | 409 |
| `IDEMPOTENCY_KEY_REUSE` | 422 |
| missing `Idempotency-Key` | 422 |

---

## 7. Layout

```
app/
  main.py        FastAPI app, routes, exception handlers
  schemas.py     Pydantic request/response models
  service.py     redeem() / cancel() — the lock and the transactions live here
  db.py          aiosqlite connection, schema DDL, WAL pragmas
  errors.py      CouponError hierarchy → error code + HTTP status
  clock.py       injectable now()
tests/
  test_redeem.py         happy path, each failure mode, precedence
  test_idempotency.py    replay, key reuse, in-flight duplicate
  test_cancel.py         refund, double-cancel, unknown order, expired coupon
  test_concurrency.py    burst tests
  conftest.py            temp-file DB per test, frozen clock, httpx client
```

---

## 8. Test plan — the part that proves the claims

Driven with `asyncio.gather` over `httpx.AsyncClient(ASGITransport)`.

1. **Oversubscription burst** — 200 concurrent redeems (distinct customers and
   order_ids) against a 50-slot coupon. Assert exactly 50 successes, 150
   `NO_REDEMPTIONS_LEFT`, `redeemed_count == 50`, and stored counter ==
   `COUNT(active)`.
2. **Idempotent burst** — 50 concurrent redeems sharing one `Idempotency-Key`.
   Assert exactly one real redemption, 49 replays with identical bodies,
   `redeemed_count == 1`.
3. **Double-cancel burst** — N concurrent cancels of one order. Assert exactly
   one `cancelled: true` and the counter drops by exactly 1.
4. **Expiry boundary** — frozen clock at `expires_at - 1µs` (succeeds),
   `expires_at` exactly (rejected), `+1µs` (rejected).
5. **STANDARD after cancel** — redeem, cancel, re-redeem with a new order_id →
   `CUSTOMER_ALREADY_REDEEMED`, while `remaining` shows the slot returned.
6. **STACKABLE** — same customer redeems N times across N orders, up to the cap.
7. **Precedence** — expired + already-used + zero slots → asserts `COUPON_EXPIRED`.
8. **Global invariant** — after every test, `0 <= redeemed_count <= max_redemptions`
   and `redeemed_count == COUNT(*) WHERE status='ACTIVE'`.

---

## 9. Build order

1. `db.py` + schema DDL + WAL pragmas + connection lifespan
2. `errors.py`, `schemas.py`, `clock.py`
3. `POST /coupons` and `GET /coupons/{code}` — smallest end-to-end slice
4. `service.redeem()` with the lock, transaction, and full precedence chain
5. Idempotency layer on top of redeem
6. `cancel()`
7. Test suite, concurrency tests last
8. README: topology assumption, invariant argument, ruling table from §5

---

## 10. Explicitly out of scope for MVP

Auth, rate limiting, multi-worker deployment, migrations (DDL is `CREATE TABLE IF
NOT EXISTS` at startup), idempotency-key reaping, coupon update/delete,
minimum-spend or category rules, currency/amount handling, observability beyond
structured logs.
