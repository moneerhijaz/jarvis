"""Confirmation broker: lets the async agent loop pause on a risky tool call and
wait for the user's Approve/Deny (delivered via the /api/runs/{id}/confirm endpoint).

The loop calls ``await request(call_id, timeout)``; the API calls ``resolve(call_id,
approved)``. Both run on the same asyncio loop (uvicorn), so the Future resolves
in-process. No answer within the timeout resolves to DENY (fail-safe).
"""
from __future__ import annotations

import asyncio


class ConfirmBroker:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future] = {}

    def resolve(self, call_id: str, approved: bool) -> bool:
        fut = self._pending.get(call_id)
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))
            return True
        return False

    async def request(self, call_id: str, timeout_s: float = 60.0) -> bool:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[call_id] = fut
        try:
            return bool(await asyncio.wait_for(fut, timeout_s))
        except asyncio.TimeoutError:
            return False   # fail-safe: silence == deny
        finally:
            self._pending.pop(call_id, None)

    def cancel_all(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(False)
        self._pending.clear()
