import asyncio

import pytest

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.events import Source
from qumixer_py.protocol.channels import Input
from qumixer_py.state import Freshness


@pytest.mark.parametrize("echo", [True, False])
async def test_a_write_never_fires_external_change(fake_qu_factory, echo) -> None:  # type: ignore[no-untyped-def]
    # a fader set via the API does not fire external_change,
    # whether or not the console echoes. Both must be exercised, because
    # whether the Qu echoes at all is unverified.
    fake = await fake_qu_factory(echo=echo)
    external: list[object] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        mixer.on_change(lambda e: external.append(e) if e.source is Source.MIXER else None)
        await mixer.set(Input(6), "fader", raw=0x62)
        await asyncio.sleep(0.2)
    assert external == []


async def test_an_echo_confirms_the_optimistic_value(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(echo=True)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.set(Input(6), "fader", raw=0x62)
        await asyncio.sleep(0.15)
        value = mixer.get(Input(6), "fader")
        assert value.raw == 0x62
        assert value.freshness is Freshness.KNOWN


async def test_an_unmatched_expectation_downgrades_to_unconfirmed(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # the library never reports a value as KNOWN on the strength of
    # its own optimistic write alone.
    fake = await fake_qu_factory(echo=False)
    unconfirmed: list[tuple[object, str, object]] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port, echo_window=0.1) as mixer:
        await mixer.wait_synced()
        mixer.on_write_unconfirmed(lambda ch, p, s: unconfirmed.append((ch, p, s)))
        await mixer.set(Input(6), "fader", raw=0x62)
        assert mixer.get(Input(6), "fader").freshness is Freshness.KNOWN  # optimistic
        await asyncio.sleep(0.3)
        assert mixer.get(Input(6), "fader").freshness is Freshness.UNCONFIRMED
        assert mixer.get(Input(6), "fader").raw == 0x62
    assert unconfirmed == [(Input(6), "fader", None)]


async def test_duplicate_expectations_are_consumed_fifo(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # two writes of the same value produce two expectations and
    # possibly one echo. Match one echo per expectation.
    fake = await fake_qu_factory(echo=False)
    external: list[object] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port, echo_window=1.0) as mixer:
        await mixer.wait_synced()
        mixer.on_change(lambda e: external.append(e) if e.source is Source.MIXER else None)
        await mixer.set(Input(6), "fader", raw=0x62)
        await mixer.set(Input(6), "fader", raw=0x62)
        await fake.send_surface_move(0x25, 0x17, 0x62, 0x07)
        await asyncio.sleep(0.1)
        assert external == []  # consumed the first expectation
        await fake.send_surface_move(0x25, 0x17, 0x62, 0x07)
        await asyncio.sleep(0.1)
        assert external == []  # consumed the second
        await fake.send_surface_move(0x25, 0x17, 0x62, 0x07)
        await asyncio.sleep(0.1)
        assert len(external) == 1  # surplus echo is external


async def test_a_different_value_arriving_is_external_not_an_echo(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(echo=False)
    external: list[object] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port, echo_window=1.0) as mixer:
        await mixer.wait_synced()
        mixer.on_change(lambda e: external.append(e) if e.source is Source.MIXER else None)
        await mixer.set(Input(6), "fader", raw=0x62)
        await fake.send_surface_move(0x25, 0x17, 0x40, 0x07)
        await asyncio.sleep(0.1)
    assert len(external) == 1
