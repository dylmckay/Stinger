"""The delivery worker: poll -> claim -> sign -> POST -> record, on a loop.

Polling is the correctness floor; a claimed batch is processed concurrently
under a bound on in-flight deliveries. The loop wakes early when a delivery
finishes (to refill a freed slot) or when a LISTEN/NOTIFY ping arrives (fresh
work), but never depends on either for correctness — a plain poll always finds
due rows and reclaimable expired leases.
"""
from __future__ import annotations

import asyncio
import logging
import random
import socket
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.delivery import signing
from app.delivery.claim import DEFAULT_MAX_CONCURRENT_PER_ENDPOINT, claim_deliveries
from app.delivery.http import attempt_delivery
from app.delivery.record import DEFAULT_BREAKER_COOLDOWN, discard_delivery, promote_half_open_endpoints, record_attempt
from app.models import Delivery, Endpoint, Event, EndpointStatus
from app.crypto import get_secret_box

LISTENER_BACKOFF_INITIAL = 1.0
LISTENER_BACKOFF_MAX = 30.0

log = logging.getLogger("stinger.worker")
box = get_secret_box()


class Worker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        *,
        max_concurrency: int = 50,
        endpoint_max_concurrency: int = DEFAULT_MAX_CONCURRENT_PER_ENDPOINT,
        poll_interval: float = 2.0,
        lease_seconds: int = 30,
        allow_private: bool = False,
        cooldown: timedelta = DEFAULT_BREAKER_COOLDOWN,
        recover_interval: float = 15.0
    ) -> None:
        self._sf = session_factory
        self._client = client
        self._max = max_concurrency
        self._endpoint_max = endpoint_max_concurrency
        self._poll = poll_interval
        self._lease = lease_seconds
        self._allow_private = allow_private
        self._cooldown = cooldown
        self._recover_interval = recover_interval
        self._last_recover = 0.0
        self.worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._inflight: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    def wake(self) -> None:
        """Nudge the loop to poll now — called by the NOTIFY listener."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        log.info("worker %s starting", self.worker_id)
        while not self._stop.is_set():
            await self._maybe_recover()
            free = self._max - len(self._inflight)
            if free > 0:
                async with self._sf() as session:
                    claimed = await claim_deliveries(
                        session, worker_id=self.worker_id,
                        limit=free, lease_seconds=self._lease,
                        global_endpoint_cap=self._endpoint_max,
                    )
                if claimed:
                    await self._dispatch(claimed)
                    continue                       # try to claim more at once
            # idle or full: wait for a wake or the (jittered) poll timeout
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._poll * random.uniform(0.8, 1.2),
                )
            except asyncio.TimeoutError:
                pass
        await self._drain()
        log.info("worker %s stopped", self.worker_id)

    async def _dispatch(self, deliveries: Sequence[Delivery]) -> None:
        endpoints, events = await self._load_context(deliveries)
        for d in deliveries:
            ep, ev = endpoints.get(d.endpoint_id), events.get(d.event_id)
            if ep is None or ev is None:           # defensive: row vanished
                log.warning("missing context for delivery %s", d.id)
                continue
            task = asyncio.create_task(self._process(d, ep, ev))
            self._inflight.add(task)
            task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        self._inflight.discard(task)
        self._wake.set()                            # a slot freed; refill soon
        if not task.cancelled() and task.exception() is not None:
            log.error("delivery task crashed: %r", task.exception())

    async def _load_context(self, deliveries: Sequence[Delivery]) -> tuple[dict[uuid.UUID, Endpoint], dict[uuid.UUID, Event]]:
        ep_ids = {d.endpoint_id for d in deliveries}
        ev_ids = {d.event_id for d in deliveries}
        async with self._sf() as s:
            eps = (await s.execute(select(Endpoint).where(Endpoint.id.in_(ep_ids)))).scalars().all()
            evs = (await s.execute(select(Event).where(Event.id.in_(ev_ids)))).scalars().all()
        return {e.id: e for e in eps}, {e.id: e for e in evs}

    async def _process(self, delivery: Delivery, endpoint: Endpoint, event: Event) -> None:
        if endpoint.status == EndpointStatus.DISABLED:
            # Endpoint is disabled (breaker tripped): void the delivery, no POST.
            async with self._sf() as s:
                await discard_delivery(s, delivery=delivery, worker_id=self.worker_id)
            return
        
        previous = None
        if endpoint.previous_secret and endpoint.previous_secret_expires_at:
            if endpoint.previous_secret_expires_at > datetime.now(timezone.utc):
                previous = box.open(endpoint.previous_secret)

        headers = signing.sign(
            event.payload, message_id=str(event.id),
            secret=box.open(endpoint.secret), previous_secret=previous,
        )
        result = await attempt_delivery(
            self._client, url=endpoint.url, payload=event.payload,
            message_id=str(event.id), extra_headers=headers,
            allow_private=self._allow_private,
        )
        async with self._sf() as s:
            await record_attempt(s, delivery=delivery, worker_id=self.worker_id, result=result)

    async def _drain(self) -> None:
        if self._inflight:
            log.info("draining %d in-flight deliveries", len(self._inflight))
            await asyncio.gather(*self._inflight, return_exceptions=True)
    

    async def _maybe_recover(self) -> None:
        """Throttled half-open sweep: promote cooled-down disabled endpoints and
        enqueue their trial deliveries, which the very next claim picks up."""
        now = time.monotonic()
        if now - self._last_recover < self._recover_interval:
            return
        self._last_recover = now
        try:
            async with self._sf() as session:
                await promote_half_open_endpoints(session, cooldown=self._cooldown)
        except Exception:
            log.exception("half-open recovery sweep failed")


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    """Sleep up to `seconds`, returning early if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def listen_for_notifications(dsn: str, channel: str, worker: Worker, stop: asyncio.Event) -> None:
    """Hold a dedicated raw asyncpg connection LISTENing for wake pings, and keep
    it alive across connection loss.

    Polling is the correctness floor, so a dropped LISTEN only costs latency — but
    a platform that silently loses its low-latency path on the first DB blip isn't
    production-grade. This reconnects with capped exponential backoff and, on every
    (re)connect, wakes the worker once: NOTIFYs are lossy, so any that fired while
    we were disconnected were missed and a poll must sweep up the gap.

    Must be a connection OUTSIDE the SQLAlchemy pool — a pooled connection would
    eventually be recycled and silently drop the LISTEN. `dsn` is the raw libpq
    URL (postgresql://...), not the +asyncpg SQLAlchemy form.
    """
    import asyncpg

    backoff = LISTENER_BACKOFF_INITIAL
    while not stop.is_set():
        try:
            conn = await asyncpg.connect(dsn)
        except (OSError, asyncpg.PostgresError) as e:
            log.warning("listener connect failed (%r); retrying in %.1fs", e, backoff)
            await _sleep_unless_stopped(stop, backoff)
            backoff = min(backoff * 2, LISTENER_BACKOFF_MAX)
            continue

        backoff = LISTENER_BACKOFF_INITIAL          # reset after a healthy connect
        lost = asyncio.Event()
        conn.add_termination_listener(lambda _con: lost.set())
        try:
            await conn.add_listener(channel, lambda *_: worker.wake())
            worker.wake()                           # sweep up anything NOTIFY'd while we were down
            stop_task = asyncio.create_task(stop.wait())
            lost_task = asyncio.create_task(lost.wait())
            done, pending = await asyncio.wait(
                {stop_task, lost_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            if lost_task in done and not stop.is_set():
                log.warning("listener connection lost; reconnecting")
        except (OSError, asyncpg.PostgresError) as e:
            log.warning("listener error (%r); reconnecting", e)
        finally:
            await conn.close()
    log.info("listener stopped")