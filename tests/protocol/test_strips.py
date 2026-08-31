import asyncio

import pytest

from qumidi.client_async import AsyncQuMixer
from qumidi.errors import ParameterRangeError
from qumidi.protocol.channels import Input
from qumidi.protocol.strips import Strip, StripId, StripKey


def test_strip_ids_are_one_based_and_zero_indexed_on_the_wire() -> None:
    assert Strip(1).value == 0x00
    assert Strip(5).value == 0x04
    assert Strip(32).value == 0x1F
    with pytest.raises(ParameterRangeError):
        Strip(0)
    with pytest.raises(ParameterRangeError):
        Strip(33)


def test_strip_id_is_not_a_channel_id() -> None:
    # PRD 14.7: strip key numbers (0x00-0x5F) DO overlap input channel ids.
    # They are different namespaces on different MIDI channels.
    assert not isinstance(Strip(1), type(Input(1)))
    assert not isinstance(Input(1), StripId)


def test_key_numbers_follow_the_documented_blocks() -> None:
    assert StripKey.MUTE.base == 0x00
    assert StripKey.SEL.base == 0x20
    assert StripKey.PAFL.base == 0x40
    assert StripKey.MUTE.note(Strip(1)) == 0x00
    assert StripKey.SEL.note(Strip(1)) == 0x20
    assert StripKey.PAFL.note(Strip(32)) == 0x5F


async def test_strip_fader_goes_out_on_channel_n_plus_one(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(midi_channel=0)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.strips_available is True
        fake.received.clear()
        await mixer.strips.set_fader(Strip(5), 0x40)
        await asyncio.sleep(0.05)
    assert bytes([0xB1, 0x04, 0x40]) in bytes(fake.received)


async def test_strip_keys_are_a_note_on_off_pair(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(midi_channel=0)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.strips.press(Strip(1), StripKey.MUTE)
        await asyncio.sleep(0.05)
    raw = bytes(fake.received)
    assert bytes([0x91, 0x00, 0x7F]) in raw
    assert bytes([0x81, 0x00, 0x00]) in raw


async def test_bank_up_and_down(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(midi_channel=0)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.strips.bank(up=True)
        await mixer.strips.bank(up=False)
        await asyncio.sleep(0.05)
    raw = bytes(fake.received)
    assert bytes([0x91, 0x7E, 0x7F]) in raw
    assert bytes([0x91, 0x7F, 0x7F]) in raw


async def test_midi_channel_16_disables_the_strip_api(fake_qu_factory, caplog) -> None:  # type: ignore[no-untyped-def]
    # PRD 14.11: N+1 does not exist when the console is on MIDI channel 16.
    # Never emit a status byte of B0|16, which corrupts into a system message.
    fake = await fake_qu_factory(midi_channel=15)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.strips_available is False
        assert mixer.strips is None
    assert any("16" in r.message for r in caplog.records)


async def test_strip_traffic_is_not_ingested_as_channel_state(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 6.3: moving a strip emits no channel NRPN, and strip numbers overlap
    # channel ids numerically. Strip traffic must not touch the state cache.
    fake = await fake_qu_factory(midi_channel=0, scene={})
    seen: list[object] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        mixer.strips.on_strip_event(seen.append)
        await fake.send_raw(bytes([0xB1, 0x04, 0x40]))
        await asyncio.sleep(0.1)
        assert len(seen) == 1
        assert mixer.get(Input(1), "fader").raw is None
