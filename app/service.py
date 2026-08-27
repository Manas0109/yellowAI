"""Redemption and cancellation logic.

Placeholder — implemented in issues #8 (redeem), #9 (idempotency) and #10
(cancel). The module-level lock lives here so both write paths share one
writer; it is declared now so #8 and #10 do not each invent their own.
"""

from __future__ import annotations

import asyncio

#: The single writer. Held across the whole redeem/cancel transaction so no two
#: write paths can interleave (plan §3). Correct only within one process.
write_lock = asyncio.Lock()
