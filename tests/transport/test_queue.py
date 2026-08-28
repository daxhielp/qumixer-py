import asyncio

from qumidi.transport.queue import OutboundQueue


class Sink:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.times: list[float] = []

    async def __call__(self, data: bytes) -> None:
        self.writes.append(data)
        self.times.append(asyncio.get_running_loop().time())

    @property
    def joined(self) -> bytes:
        return b"".join(self.writes)


async def test_send_resolves_only_after_the_sink_has_seen_the_bytes() -> None:
    sink = Sink()
    q = OutboundQueue(sink, max_send_rate=None)
    await q.start()
    try:
        await q.send(b"\x01\x02")
        # PRD 4.5: resolves when handed to the socket and drained, not queued.
        assert sink.joined == b"\x01\x02"
    finally:
        await q.stop()


async def test_writes_are_transmitted_in_issue_order() -> None:
    sink = Sink()
    q = OutboundQueue(sink, max_send_rate=None)
    await q.start()
    try:
        await asyncio.gather(*(q.send(bytes([i])) for i in range(20)))
        assert sink.joined == bytes(range(20))
    finally:
        await q.stop()


async def test_concurrent_sends_never_interleave_a_multi_byte_payload() -> None:
    # PRD 4.5: concurrent set() calls from different tasks never interleave
    # the four CCs of an NRPN triplet.
    sink = Sink()
    q = OutboundQueue(sink, max_send_rate=None)
    await q.start()
    try:
        a = bytes([0xB0, 0x63, 0x25, 0xB0, 0x62, 0x17])
        b = bytes([0xB0, 0x63, 0x26, 0xB0, 0x62, 0x17])
        await asyncio.gather(*(q.send(a if i % 2 else b) for i in range(20)))
        for write in sink.writes:
            assert write in (a, b)
    finally:
        await q.stop()


async def test_throttle_limits_the_byte_rate() -> None:
    sink = Sink()
    q = OutboundQueue(sink, max_send_rate=1000)
    await q.start()
    try:
        start = asyncio.get_running_loop().time()
        for _ in range(20):
            await q.send(bytes(20))  # 400 bytes at 1000 B/s -> ~0.4s
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed >= 0.3, elapsed
    finally:
        await q.stop()


async def test_priority_send_bypasses_the_throttle() -> None:
    # PRD 4.5: keepalive and meter enable/disable must never be starved by a
    # bulk write.
    sink = Sink()
    q = OutboundQueue(sink, max_send_rate=200)
    await q.start()
    try:

        async def _bulk_sends() -> None:
            await asyncio.gather(*(q.send(bytes(50)) for _ in range(10)))

        bulk = asyncio.create_task(_bulk_sends())
        await asyncio.sleep(0.05)
        start = asyncio.get_running_loop().time()
        await q.send_priority(b"\xfe")
        assert asyncio.get_running_loop().time() - start < 0.05
        assert b"\xfe" in sink.writes
        bulk.cancel()
    finally:
        await q.stop()


async def test_pending_reports_the_backlog() -> None:
    sink = Sink()
    q = OutboundQueue(sink, max_send_rate=100)
    await q.start()
    try:
        tasks = [asyncio.create_task(q.send(bytes(20))) for _ in range(8)]
        await asyncio.sleep(0.02)
        assert q.pending > 0
        for t in tasks:
            t.cancel()
    finally:
        await q.stop()


async def test_queue_is_bounded_and_applies_backpressure() -> None:
    # PRD 18: no unbounded queues anywhere; the outbound queue applies
    # backpressure to set() rather than growing.
    sink = Sink()
    q = OutboundQueue(sink, max_send_rate=50, maxsize=4)
    await q.start()
    try:
        tasks = [asyncio.create_task(q.send(bytes(10))) for _ in range(12)]
        await asyncio.sleep(0.05)
        assert q.pending <= 4
        for t in tasks:
            t.cancel()
    finally:
        await q.stop()


async def test_clear_drops_the_backlog_and_fails_its_waiters() -> None:
    # PRD 4.4: drop the outbound queue on disconnect.
    sink = Sink()
    q = OutboundQueue(sink, max_send_rate=50)
    await q.start()
    try:
        tasks = [asyncio.create_task(q.send(bytes(20))) for _ in range(6)]
        await asyncio.sleep(0.02)
        q.clear()
        await asyncio.sleep(0.05)
        assert q.pending == 0
        for t in tasks:
            t.cancel()
    finally:
        await q.stop()


async def test_stop_leaves_no_tasks() -> None:
    before = len(asyncio.all_tasks())
    q = OutboundQueue(Sink(), max_send_rate=None)
    await q.start()
    await q.stop()
    await asyncio.sleep(0.02)
    assert len(asyncio.all_tasks()) <= before
