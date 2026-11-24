"""SSE transport: short read batches, bounded buffering, durable cursor replay."""

import time
from workbench_agent.application.errors import DomainError
from workbench_agent.persistence.errors import StorageError


async def stream_events(events, auth, run_id, after_seq, *, heartbeat_seconds=15):
    last_sent = time.monotonic()
    while True:
        try:
            batch = await events.read_batch(auth, run_id, after_seq, limit=100)
            # Pull-driven backpressure: no producer/prefetch while network awaits.
            # At most 100 durable records live in this generator at any time.
            for event in batch:
                if hasattr(events, 'project_event'):
                    event = await events.project_event(auth, event)
                yield event.to_sse()
                after_seq = event.seq
                last_sent = time.monotonic()
            del batch
            if await events.is_terminal_and_drained(auth, run_id, after_seq):
                return
            if time.monotonic() - last_sent >= heartbeat_seconds:
                yield b': heartbeat\n\n'
                last_sent = time.monotonic()
            await events.wait_for_hint_or_timeout(
                run_id, min(1, max(0, heartbeat_seconds - (time.monotonic() - last_sent)))
            )
        except (DomainError, StorageError):
            # Headers already sent; revocation/retention/storage failure closes stream.
            # Reconnect performs preflight again and receives its HTTP status.
            return


import asyncio
from fastapi.responses import StreamingResponse


class EventResponse(StreamingResponse):
    """Real ASGI bounded producer/send tasks; overflow interrupts blocked transport."""

    def __init__(self, events, auth, run_id, after_seq, *, heartbeat_seconds=15):
        super().__init__(
            iter(()),
            media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )
        self.events, self.auth, self.run_id, self.after_seq = events, auth, run_id, after_seq
        self.heartbeat_seconds = heartbeat_seconds

    async def stream_response(self, send):
        # 99 queued plus one in-flight send: <=100 retained event records.
        queue = asyncio.Queue(maxsize=99)
        abort = asyncio.Event()
        end = object()

        async def produce():
            cursor = self.after_seq
            try:
                while True:
                    state = await self.events.stream_state(self.auth, self.run_id, cursor)
                    capacity = 99 - queue.qsize()
                    if not capacity:
                        if state['last_event_seq'] > cursor:
                            abort.set()
                            return
                    else:
                        batch = await self.events.read_batch(
                            self.auth, self.run_id, cursor, limit=capacity
                        )
                        for event in batch:
                            queue.put_nowait(event)
                            cursor = event.seq
                        del batch
                        if await self.events.is_terminal_and_drained(
                            self.auth, self.run_id, cursor
                        ):
                            await queue.put(end)
                            return
                        if not queue.empty():
                            # Permit immediate drain of backlog while a client keeps up.
                            await asyncio.sleep(0)
                            state = await self.events.stream_state(self.auth, self.run_id, cursor)
                            if state['last_event_seq'] > cursor:
                                continue
                    await self.events.wait_for_hint_or_timeout(self.run_id, 1)
            except Exception:
                # Invalid/corrupt envelopes must close transport, never strand its sender.
                abort.set()

        async def transmit():
            await send(
                {
                    'type': 'http.response.start',
                    'status': self.status_code,
                    'headers': self.raw_headers,
                }
            )
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), self.heartbeat_seconds)
                except TimeoutError:
                    await send(
                        {
                            'type': 'http.response.body',
                            'body': b': heartbeat\n\n',
                            'more_body': True,
                        }
                    )
                    continue
                if event is end:
                    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                    return
                if hasattr(self.events, 'project_event'):
                    event = await self.events.project_event(self.auth, event)
                await send(
                    {'type': 'http.response.body', 'body': event.to_sse(), 'more_body': True}
                )
                del event

        producer = asyncio.create_task(produce())
        sender = asyncio.create_task(transmit())
        stop = asyncio.create_task(abort.wait())
        try:
            done, _ = await asyncio.wait(
                {producer, sender, stop}, return_when=asyncio.FIRST_COMPLETED
            )
            if producer in done and not abort.is_set():
                # Successful producer completion still drains the committed tail.
                await asyncio.wait({sender, stop}, return_when=asyncio.FIRST_COMPLETED)
                producer.result()
            if sender.done():
                sender.result()
        finally:
            for task in (producer, sender, stop):
                task.cancel()
            await asyncio.gather(producer, sender, stop, return_exceptions=True)


class DeltaMerger:
    """Persist at 256 characters or 250ms, and safely flush at invocation end."""

    def __init__(self, events, guard):
        self.events, self.guard = events, guard
        self.buffer = ''
        self.lock = asyncio.Lock()
        self.timer = None
        self.error = None
        self.inflight = None

    async def __aenter__(self):
        return self

    async def _append(self, text):
        # The merger owns this task through commit and the optional hint wait.
        # Cancelling a scheduler/caller must not make a committed chunk pending again.
        await self.events.append_execution(self.guard, 'assistant.delta', {'text': text})
        self.buffer = ''

    async def _settle_locked(self):
        if self.inflight is not None:
            await asyncio.shield(self.inflight)
            # On failure keep the failed task: an uncertain append is never retried
            # implicitly from the retained buffer during context cleanup.
            self.inflight = None

    async def _flush_locked(self):
        await self._settle_locked()
        if self.buffer:
            self.inflight = asyncio.create_task(self._append(self.buffer))
            await self._settle_locked()

    async def _timer(self):
        try:
            await asyncio.sleep(0.25)
            async with self.lock:
                await self._flush_locked()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = error

    async def add(self, text):
        if not isinstance(text, str):
            raise TypeError('text required')
        async with self.lock:
            if self.error:
                raise self.error
            await self._settle_locked()
            while text:
                if not self.buffer:
                    if self.timer:
                        self.timer.cancel()
                    self.timer = asyncio.create_task(self._timer())
                count = min(256 - len(self.buffer), len(text))
                self.buffer += text[:count]
                text = text[count:]
                if len(self.buffer) == 256:
                    await self._flush_locked()

    async def __aexit__(self, exc_type, exc, tb):
        from workbench_agent.persistence.errors import StaleExecution

        if self.timer:
            # This cancels only the sleep/scheduler. Its owned append is shielded
            # and must settle below before deciding whether any remainder exists.
            self.timer.cancel()
            await asyncio.gather(self.timer, return_exceptions=True)
        try:
            if self.error:
                raise self.error
            async with self.lock:
                await self._flush_locked()
        except StaleExecution:
            if exc_type is None:
                raise
            # Ownership lost while cancelling: never flush using another owner.
        return False


class RedisHints:
    """Optional wakeups only; no locks, durable events, or lifecycle authority."""

    def __init__(self, url):
        from redis.asyncio import Redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        self.client = Redis.from_url(
            url,
            socket_connect_timeout=0.1,
            socket_timeout=1,
            retry=Retry(NoBackoff(), 0),
            retry_on_timeout=False,
        )

    async def wait(self, run_id, seconds):
        deadline = time.monotonic() + seconds
        async with self.client.pubsub() as subscriber:
            await subscriber.subscribe('dod:run:' + run_id)
            while time.monotonic() < deadline:
                message = await subscriber.get_message(
                    ignore_subscribe_messages=True, timeout=max(0, deadline - time.monotonic())
                )
                if message is not None:
                    return

    async def publish(self, run_id):
        await self.client.publish('dod:run:' + run_id, 'committed')

    async def close(self):
        await self.client.aclose()

    @classmethod
    def configured(cls, settings):
        from workbench_agent.settings import resolve_env_reference

        try:
            return cls(resolve_env_reference(settings.redis.url_ref))
        except ValueError:
            return None
