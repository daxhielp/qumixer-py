import asyncio

import pytest

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.errors import ParameterRangeError
from qumixer_py.midi.nrpn import encode_nrpn
from qumixer_py.protocol.channels import Input
from qumixer_py.protocol.strips import Strip, StripId, StripKey


def test_strip_ids_are_one_based_and_zero_indexed_on_the_wire() -> None:
    assert Strip(1).value == 0x00
    assert Strip(5).value == 0x04
    assert Strip(32).value == 0x1F
    with pytest.raises(ParameterRangeError):
        Strip(0)
    with pytest.raises(ParameterRangeError):
        Strip(33)


def test_strip_id_is_not_a_channel_id() -> None:
    # strip key numbers (0x00-0x5F) DO overlap input channel ids.
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
    # N+1 does not exist when the console is on MIDI channel 16.
    # Never emit a status byte of B0|16, which corrupts into a system message.
    fake = await fake_qu_factory(midi_channel=15)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.strips_available is False
        assert mixer.strips is None
    assert any("16" in r.message for r in caplog.records)


async def test_strip_traffic_is_not_ingested_as_channel_state(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # moving a strip emits no channel NRPN, and strip numbers overlap
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


async def test_interleaved_strip_traffic_does_not_corrupt_a_held_nrpn(
    fake_qu_factory,  # type: ignore[no-untyped-def]
) -> None:
    # Regression for the _dispatch ordering fix: NrpnDecoder._held
    # (qumixer_py/midi/nrpn.py) is a single instance attribute, not keyed by
    # channel. Every branch of feed() other than the Data-LSB match starts by
    # releasing whatever is currently held, with no VX, i.e. incomplete. If
    # strip traffic (channel N+1) is ever fed into the decoder while a
    # channel-N Data MSB is held awaiting its Data LSB, that CC forces a
    # premature, VX-less flush -- corrupting the in-flight channel-N NRPN
    # assembly. The fix routes N+1 traffic to StripApi._dispatch and returns
    # *before* decoder.feed() ever runs for it, so it can never disturb
    # _held. This test fails under the original ordering (route to
    # strips only after feed() has already run for every message) and
    # passes under the current one.
    fake = await fake_qu_factory(midi_channel=0, scene={})
    seen: list[object] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        mixer.strips.on_strip_event(seen.append)

        # Input 1 (ch=0x20) send_level (id=0x20) to Mix 3 (vx=0x02) = 0x50 --
        # the same wire values test_state_sync.py's SCENE uses for Input 6,
        # chosen (over "fader", whose VX is a fixed filler the cache ignores)
        # because send_level's VX addresses a distinct cache sub-index: a
        # premature, VX-less flush lands in the wrong slot (sub=None) rather
        # than merely losing a filler byte, so this actually discriminates
        # the two dispatch orderings via mixer.get()'s final state.
        # The two sends below are deliberately much shorter than
        # c.PENDING_FLUSH_TIMEOUT (50ms): they only need to give the read
        # loop a chance to process each write before the next one, not
        # invite the *unrelated* tick-driven flush trigger (silence timeout)
        # to release the held update for reasons that have nothing to do
        # with the ordering bug this test targets.
        quad = encode_nrpn(midi_channel=0, ch=0x20, param_id=0x20, value=0x50, vx=0x02)
        await fake.send_raw(quad[:9])  # NRPN MSB, NRPN LSB, Data MSB -- now held.
        await asyncio.sleep(0.01)
        await fake.send_raw(bytes([0xB1, 0x04, 0x40]))  # strip CC on channel N+1
        await asyncio.sleep(0.01)
        await fake.send_raw(quad[9:])  # the real Data LSB, completing the quad.
        await asyncio.sleep(0.05)

        assert len(seen) == 1
        assert mixer.get(Input(1), "send_level", 0x02).raw == 0x50
