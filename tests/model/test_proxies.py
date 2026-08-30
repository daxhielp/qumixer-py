import asyncio

import pytest

from qumidi.client_async import AsyncQuMixer
from qumidi.errors import ValueUnknownError
from qumidi.protocol.channels import Mix
from qumidi.state import Freshness

SCENE = {(0x25, 0x17, 0x07): 0x62, (0x25, 0x11, 0x00): 0x01}


async def test_set_db_and_read_back(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        ch = mixer.input(6)
        assert ch.fader.raw == 0x62
        assert ch.fader.db == pytest.approx(0.0, abs=0.5)
        await ch.fader.set_db(-10.0)
        assert ch.fader.raw == 0x3F


async def test_reading_an_unknown_value_raises_rather_than_guessing(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 11.1: a read of an UNKNOWN value raises rather than returning a
    # plausible-looking number.
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ValueUnknownError) as excinfo:
            _ = mixer.input(6).fader.db
        assert "Input 6" in str(excinfo.value)
        assert "fader" in str(excinfo.value)


async def test_value_exposes_freshness_without_raising(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.input(6).fader.value.freshness is Freshness.UNKNOWN


async def test_bool_proxy(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.input(6).peq.enabled is True
        await mixer.input(6).peq.set_enabled(False)
        assert mixer.input(6).peq.enabled is False


async def test_enum_proxy_uses_labels(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.input(6).comp.set_type("manual_peak")
        assert mixer.input(6).comp.type.label == "manual_peak"


async def test_membership_proxy_keeps_two_dcas_independent(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 16.10.
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        ch = mixer.input(6)
        await ch.dca_assign.add(1)
        await ch.dca_assign.add(3)
        with pytest.raises(ValueUnknownError):
            _ = ch.dca_assign.groups     # 2 and 4 are still unknown
        await ch.dca_assign.set(2, False)
        await ch.dca_assign.set(4, False)
        assert ch.dca_assign.groups == frozenset({1, 3})


async def test_send_proxy(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        send = mixer.input(6).send(Mix.MIX3)
        await send.set_level_db(-12.0)
        await send.set_pre(True)
        # The write completing (the future the outbound queue awaits) only
        # promises the bytes were handed to the socket, not that the fake
        # server's own read loop has been scheduled to consume and decode
        # them yet -- same convention as tests/test_set_param.py.
        await asyncio.sleep(0.05)
        assert fake.state[(0x25, 0x50, 0x02)] == 0x01


async def test_ch_pan_is_sugar_for_the_lr_send(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 7.2: there is no separate "channel pan" parameter.
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.input(6).pan.set_raw(0x25)
        await asyncio.sleep(0.05)
        assert fake.state[(0x25, 0x16, 0x07)] == 0x25
    assert "send(Mix.LR)" in (type(mixer.input(6)).pan.__doc__ or "")


async def test_the_facade_contains_no_protocol_constants() -> None:
    # The critical invariant from PRD 3: layer 5 is a thin proxy over layer 4.
    import pathlib
    import re

    for path in (pathlib.Path("qumidi/model")).rglob("*.py"):
        source = path.read_text()
        hex_literals = re.findall(r"0x[0-9A-Fa-f]{2}\b", source)
        assert not hex_literals, f"{path} contains protocol constants: {hex_literals}"
