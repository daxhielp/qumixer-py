import asyncio

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.transport.tcp import TcpTransport


async def test_a_ramp_walks_the_value_to_the_target(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={(0x25, 0x17, 0x07): 0x62})
    async with AsyncQuMixer("127.0.0.1", port=fake.port, max_send_rate=None) as mixer:
        await mixer.wait_synced()
        await mixer.input(6).fader.ramp_to_raw(0x40, duration=0.2)
        assert mixer.input(6).fader.raw == 0x40
        assert fake.state[(0x25, 0x17, 0x07)] == 0x40


async def test_a_128_step_ramp_stays_within_the_send_rate(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    #. Each NRPN write is 12 bytes; 127 steps is ~1524 bytes, which
    # at 1000 B/s cannot complete in under 1.5s no matter what duration says.
    fake = await fake_qu_factory(scene={(0x25, 0x17, 0x07): 0x7F})
    async with AsyncQuMixer("127.0.0.1", port=fake.port, max_send_rate=1000) as mixer:
        await mixer.wait_synced()
        started = asyncio.get_running_loop().time()
        await mixer.input(6).fader.ramp_to_raw(0x00, duration=0.1)
        elapsed = asyncio.get_running_loop().time() - started
    assert elapsed >= 1.4, elapsed


async def test_the_keepalive_still_goes_out_while_the_queue_is_saturated(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # liveness must never be starved by a bulk write.
    fake = await fake_qu_factory(scene={(0x25, 0x17, 0x07): 0x7F})
    # AsyncQuMixer has no keepalive_interval kwarg of its own -- that setting
    # lives on the transport it drives, so a fast keepalive for this test
    # means building the TcpTransport explicitly and handing it in.
    transport = TcpTransport("127.0.0.1", port=fake.port, keepalive_interval=0.1)
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, transport=transport, max_send_rate=500
    ) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        ramp = asyncio.create_task(mixer.input(6).fader.ramp_to_raw(0x00, duration=0.1))
        await asyncio.sleep(0.55)
        ramp.cancel()
    assert bytes(fake.received).count(0xFE) >= 4


async def test_a_second_ramp_cancels_the_first(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={(0x25, 0x17, 0x07): 0x7F})
    async with AsyncQuMixer("127.0.0.1", port=fake.port, max_send_rate=None) as mixer:
        await mixer.wait_synced()
        fader = mixer.input(6).fader
        first = asyncio.create_task(fader.ramp_to_raw(0x00, duration=2.0))
        await asyncio.sleep(0.05)
        await fader.ramp_to_raw(0x62, duration=0.1)
        assert first.cancelled() or first.done()
        assert mixer.input(6).fader.raw == 0x62
