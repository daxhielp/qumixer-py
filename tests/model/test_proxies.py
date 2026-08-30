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


async def test_send_level_and_pan_reads_mirror_their_own_writes(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # Regression: send_level/pan use SendTarget VX mode, whose sub_index()
    # always resolves to the target's wire VX byte, never None -- a read
    # that looked under sub=None could never see its own write land.
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        send = mixer.input(6).send(Mix.MIX3)
        await send.set_level_db(-12.0)
        assert send.level.value.freshness is Freshness.KNOWN
        assert send.level.db == pytest.approx(-12.0, abs=0.5)

        await send.set_pre(True)  # a second send-scoped write on the same target

        pan = mixer.input(6).pan  # PRD 7.2 sugar for send(Mix.LR).pan
        await pan.set_raw(0x10)
        assert pan.value.freshness is Freshness.KNOWN
        assert pan.raw == 0x10


async def test_preamp_set_gain_db_routes_to_local_when_known(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={(0x25, 0x57, 0x00): 0x00})  # local preamp
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.input(6).preamp.set_gain_db(10.0)
        await asyncio.sleep(0.05)
        assert (0x25, 0x19, 0x07) in fake.state  # local_gain


async def test_preamp_set_gain_db_raises_when_routing_unknown(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ValueUnknownError):
            await mixer.input(6).preamp.set_gain_db(10.0)


async def test_preamp_set_phantom_routes_to_dsnake_when_known(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(
        scene={(0x25, 0x57, 0x00): 0x01, (0x25, 0x5D, 0x00): 0x05}  # dSNAKE, socket 5
    )
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.input(6).preamp.set_phantom(True)
        await asyncio.sleep(0.05)
        assert fake.state[(0x25, 0x5A, 0x05)] == 0x01  # dsnake_48v, socket 5


async def test_preamp_set_phantom_raises_rather_than_defaulting_to_local(  # type: ignore[no-untyped-def]
    fake_qu_factory,
) -> None:
    # PRD 7.2 / the plan's "never synthesize a default value": phantom power
    # is safety-relevant, so an unknown routing must raise, not silently fall
    # back to firing 48V into whichever preamp happens to be local.
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ValueUnknownError):
            await mixer.input(6).preamp.set_phantom(True)
        await asyncio.sleep(0.05)
        assert (0x25, 0x69, 0x07) not in fake.state  # local_48v was never written


async def test_geq_band_round_trip(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.lr.geq.set_band_db(3.0, band=5)
        assert mixer.lr.geq.band(5).db == pytest.approx(3.0, abs=0.5)


async def test_gate_and_peq_band_round_trip(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        ch = mixer.input(6)
        await ch.gate.threshold.set_raw(0x30)
        assert ch.gate.threshold.raw == 0x30
        await ch.peq.lm.gain.set_raw(0x50)
        assert ch.peq.lm.gain.raw == 0x50


async def test_group_fader_round_trip(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # A new mixer accessor beyond input().
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.group(1).fader.set_raw(0x50)
        assert mixer.group(1).fader.raw == 0x50


async def test_stereo_input_wires_to_the_right_channel(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        st = mixer.stereo_input(1)
        assert st.id.label == "ST1"
        await st.fader.set_raw(0x50)
        assert st.fader.raw == 0x50
        # And a plain input on the same index stays a separate channel.
        assert mixer.input(1).fader.value.freshness is Freshness.UNKNOWN


async def test_mix_accessor_takes_an_arbitrary_mix_target(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.mix(Mix.MIX3).fader.set_raw(0x40)
        assert mixer.mix(Mix.MIX3).fader.raw == 0x40


async def test_matrix_fader_round_trip(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.matrix(1).fader.set_raw(0x33)
        assert mixer.matrix(1).fader.raw == 0x33


async def test_dca_master_mutes(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.dca(1).mute(True)
        await asyncio.sleep(0.05)
        assert fake.mutes[0x10] is True


async def test_mute_group_master_fire_is_identical_to_mute(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 7.2: firing a mute group has no dedicated message -- it's the same
    # Note On a plain mute() sends.
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.mute_group(2).fire(True)
        await asyncio.sleep(0.05)
    assert bytes([0x90, 0x51, 0x7F, 0x90, 0x51, 0x00]) in bytes(fake.received)
    assert fake.mutes[0x51] is True


async def test_fx_send_accessor_mutes(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.fx_send(1).mute(True)
        await asyncio.sleep(0.05)
        assert fake.mutes[0x00] is True


async def test_fx_return_is_a_send_source(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        ret = mixer.fx_return(1)
        await ret.dca_assign.add(2)
        for n in (1, 3, 4):
            await ret.dca_assign.set(n, False)
        assert ret.dca_assign.groups == frozenset({2})


async def test_all_fade_out_ramps_lr_fader_to_zero(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={(0x67, 0x17, 0x07): 0x50})  # LR fader
    async with AsyncQuMixer("127.0.0.1", port=fake.port, max_send_rate=None) as mixer:
        await mixer.wait_synced()
        await mixer.all_fade_out(duration=0.05)
        assert mixer.lr.fader.raw == 0x00


async def test_the_facade_contains_no_protocol_constants() -> None:
    # The critical invariant from PRD 3: layer 5 is a thin proxy over layer 4.
    import pathlib
    import re

    for path in (pathlib.Path("qumidi/model")).rglob("*.py"):
        source = path.read_text()
        hex_literals = re.findall(r"0x[0-9A-Fa-f]{2}\b", source)
        assert not hex_literals, f"{path} contains protocol constants: {hex_literals}"
