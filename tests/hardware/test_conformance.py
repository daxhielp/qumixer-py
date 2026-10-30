"""Conformance against real hardware.

Run: pytest -m hardware --qu-host=192.168.1.60
Each test closes one of the open questions.
"""

from __future__ import annotations

import asyncio

import pytest

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.protocol.channels import GLOBAL, Group, Input
from qumixer_py.protocol.meters import LAYOUTS, QU24_ALTERNATE
from qumixer_py.state import Freshness

pytestmark = pytest.mark.hardware


async def test_model_firmware_and_channel_are_detected(hw: AsyncQuMixer) -> None:
    # Model, firmware and channel detection.
    assert hw.model is not None, f"unrecognised BoxID {hw.raw_box_id}"
    assert hw.firmware is not None and hw.firmware >= (1, 9)
    assert hw.midi_channel is not None
    print(f"\n{hw.model.name} firmware {hw.firmware} on MIDI channel "
          f"{hw.midi_channel_display}")


async def test_the_state_push_is_substantial(hw: AsyncQuMixer) -> None:
    # Record how complete the push actually is -- this is not documented.
    print(f"\nstate push delivered {len(hw.cache)} values")
    assert len(hw.cache) > 10


async def test_a_fader_write_round_trips(hw: AsyncQuMixer) -> None:
    await hw.set(Input(1), "fader", raw=0x40)
    await asyncio.sleep(0.5)
    assert hw.get(Input(1), "fader").raw == 0x40


async def test_whether_the_console_echoes_writes(hw: AsyncQuMixer) -> None:
    # Open question. Not an assertion -- a measurement.
    await hw.set(Input(1), "fader", raw=0x30)
    await asyncio.sleep(1.5)
    value = hw.get(Input(1), "fader")
    print(f"\nafter a write and a 1.5s wait the value is {value.freshness.name}; "
          f"the console {'DOES' if value.freshness is Freshness.KNOWN else 'does NOT'} "
          f"echo commanded changes")


async def test_name_get_set_round_trip(hw: AsyncQuMixer) -> None:
    original = hw.name(Input(1))
    try:
        await hw.set_name(Input(1), "Test")
        assert hw.name(Input(1)) == "Test"
    finally:
        if original:
            await hw.set_name(Input(1), original)


async def test_measure_the_name_length_limit(hw: AsyncQuMixer) -> None:
    # Open question: channel-name length limit. This bypasses
    # qumixer_py.protocol.sysex.set_name()'s own length check (which now
    # enforces constants.MAX_NAME_LENGTH) by building the raw 03 sysex
    # directly -- the whole point of this test is to measure past whatever
    # that provisional constant currently says.
    from qumixer_py.protocol import constants as c
    from qumixer_py.protocol.sysex import build_sysex

    assert hw.midi_channel is not None
    channel = hw.midi_channel
    original = hw.name(Input(1))
    limit = 0
    try:
        for length in range(1, 17):
            candidate = "".join(str(i % 10) for i in range(length))
            payload = bytes([c.CMD_SET_NAME, Input(1).value]) + candidate.encode("ascii")
            await hw._send(build_sysex(channel, payload))
            await asyncio.sleep(0.2)
            observed = await hw._request_name(channel, Input(1))
            if observed != candidate:
                break
            limit = length
    finally:
        if original:
            await hw.set_name(Input(1), original)
    print(f"\nmeasured name length limit: {limit} characters "
          f"(constants.MAX_NAME_LENGTH is currently set to a provisional value)")
    assert limit > 0


async def test_the_meter_frame_size_matches_the_computed_layout(hw: AsyncQuMixer) -> None:
    # Meter frame size, and the open Qu-24 meter layout question.
    errors: list[str] = []
    hw.events.on_meter_layout_error(errors.append)
    async with hw.meters.enabled():
        try:
            frame = await asyncio.wait_for(hw.meters.stream().__anext__(), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail(f"no valid meter frame arrived. Layout errors: {errors}")
    print(f"\nmeter frame validated against the {frame.layout.name} layout "
          f"({frame.layout.total_meters} meters)")
    assert not errors


@pytest.mark.skipif(True, reason="enable manually when a Qu-24 is connected")
async def test_which_qu24_meter_layout_is_correct(hw: AsyncQuMixer) -> None:
    # resolve the 180-vs-100 unused question.
    assert hw.model is not None and hw.model.name == "QU24"
    for candidate in (LAYOUTS["qu24"], QU24_ALTERNATE):
        hw.meters.set_layout(candidate)
        errors: list[str] = []
        hw.events.on_meter_layout_error(errors.append)
        async with hw.meters.enabled():
            try:
                await asyncio.wait_for(hw.meters.stream().__anext__(), timeout=3.0)
                print(f"\nQu-24 meter layout is: {candidate.name} "
                      f"({candidate.total_meters} meters)")
                return
            except asyncio.TimeoutError:
                continue
    pytest.fail("neither Qu-24 candidate layout matched the observed frame size")


@pytest.mark.skipif(True, reason="enable manually when a Qu-24 is connected")
async def test_whether_a_qu24_has_group_5_6(hw: AsyncQuMixer) -> None:
    # The spec's dagger note: write to Group 3 (5-6) and observe whether it responds.
    await hw.set(Group(3), "fader", raw=0x40)
    await asyncio.sleep(1.0)
    value = hw.get(Group(3), "fader")
    print(f"\nQu-24 Group 5-6 responded: {value.freshness is Freshness.KNOWN}")


async def test_the_console_refuses_a_second_connection(qu_host: str, hw: AsyncQuMixer) -> None:
    #, and the number one user support question.
    from qumixer_py.errors import QuConnectionError

    second = AsyncQuMixer(qu_host, reply_timeout=3.0)
    with pytest.raises(QuConnectionError):
        await second.connect()
    await second.close()


async def test_measure_the_scene_recall_settling_time(
    hw: AsyncQuMixer, request: pytest.FixtureRequest
) -> None:
    # Calibrate the delay.
    spare = int(request.config.getoption("--qu-spare-scene"))
    for delay in (0.1, 0.25, 0.5, 1.0, 2.0):
        hw.scene_settle_delay = delay
        before = len(hw.cache)
        await hw.recall_scene(spare)
        if len(hw.cache) >= before:
            print(f"\nscene recall settled within {delay}s "
                  f"({len(hw.cache)} values after resync)")
            return
    pytest.fail("scene recall did not settle within 2s")


async def test_group_mode_reports_its_channel(hw: AsyncQuMixer) -> None:
    # Record the CH observed on hardware.
    value = hw.get(GLOBAL, "group_mode")
    print(f"\ngroup_mode from the state push: {value.freshness.name} raw={value.raw}")


async def test_dsnake_addressing(hw: AsyncQuMixer) -> None:
    # Does CH address anything, or is the socket in VX the
    # sole address? Only meaningful with an AR unit connected.
    patch = hw.get(Input(1), "dsnake_patch")
    print(f"\nInput 1 dsnake_patch: {patch.freshness.name} raw={patch.raw}")


async def test_behaviour_at_midi_channel_16(hw: AsyncQuMixer) -> None:
    # Requires manually setting the console to channel 16.
    if hw.midi_channel != 15:
        pytest.skip("set the console to MIDI channel 16 and re-run to check this")
    assert hw.strips_available is False
