"""Coverage for `qumidi record`/`qumidi replay` and `FakeQu(replay=...)` (PRD 13.1, 13.5).

Recording every hardware session and committing the captures makes them the
regression corpus -- record captures the raw inbound byte stream to a file,
and replay serves that file back out through FakeQu so CI can regress against
real traffic shapes without owning a console.
"""

from __future__ import annotations

import asyncio

from qumidi.cli import main
from qumidi.client_async import AsyncQuMixer
from qumidi.protocol.channels import Input


async def test_record_captures_inbound_bytes(fake_qu_factory, tmp_path) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={(0x25, 0x17, 0x07): 0x62})
    out = tmp_path / "capture.bin"
    rc = await asyncio.get_running_loop().run_in_executor(
        None, main,
        ["record", "--host", "127.0.0.1", "--port", str(fake.port),
         "--out", str(out), "--seconds", "0.5"],
    )
    assert rc == 0
    data = out.read_bytes()
    assert len(data) > 0
    status = 0xB0 | fake.midi_channel
    assert bytes([status, 0x63, 0x25]) in data  # the state burst


async def test_record_frames_stops_after_n_meter_frames(fake_qu_factory, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # PRD 13.5: --frames lets a user capture exactly the meter frames needed
    # to derive a corrected layout, rather than guessing at --seconds.
    from qumidi.protocol import constants as c
    from qumidi.protocol.meters import LAYOUTS, Meter, MeterSlot

    def frame_for(model: str, slot: MeterSlot, raw_u16: int) -> bytes:
        layout = LAYOUTS[model]
        payload = bytearray(layout.payload_bytes)
        offset = layout.offset_of(slot) * 2
        payload[offset : offset + 2] = raw_u16.to_bytes(2, "big")
        return bytes(payload)

    slot = MeterSlot.input(6, Meter.POST_PREAMP)
    frames = [frame_for("qu32", slot, 0x1000 + i) for i in range(5)]
    fake = await fake_qu_factory(model="qu32", meter_frames=frames, meter_rate=100.0)
    out = tmp_path / "capture.bin"

    loop = asyncio.get_running_loop()
    start = loop.time()
    rc = await loop.run_in_executor(
        None, main,
        ["record", "--host", "127.0.0.1", "--port", str(fake.port),
         "--out", str(out), "--frames", "3"],
    )
    elapsed = loop.time() - start

    assert rc == 0
    # --frames stops as soon as 3 meter frames have been decoded, well before
    # record's own default --seconds=5.0 sleep would elapse -- proving the
    # --frames branch, not the --seconds one, is what actually ran.
    assert elapsed < 4.0

    data = out.read_bytes()
    marker = c.QU_SYSEX_PREFIX + bytes([fake.midi_channel, c.CMD_METER_DATA])
    assert data.count(marker) >= 3


async def test_replay_feeds_a_capture_through_fake_qu(fake_qu_factory, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # PRD 13.1: replay mode lets CI regress against real traffic shapes.
    from qumidi.midi.nrpn import encode_nrpn
    from qumidi.protocol import constants as c
    from qumidi.protocol.sysex import build_sysex
    from qumidi.testing.fake_qu import FakeQu

    capture = tmp_path / "capture.bin"
    capture.write_bytes(
        build_sysex(0, bytes([c.CMD_SYSTEM_STATE_REPLY, 3, 1, 9]))
        + encode_nrpn(0, 0x25, 0x17, 0x62, 0x07)
        + build_sysex(0, bytes([c.CMD_END_OF_SYNC]))
    )
    fake = FakeQu(replay=capture)
    await fake.start()
    try:
        # A replay only ever plays back what was captured -- it never answers
        # a live name request the way the synthesised state machine does --
        # so sync_names=False here, or the name sweep would retry every
        # nameable channel to exhaustion against a console that can't reply.
        async with AsyncQuMixer("127.0.0.1", port=fake.port, sync_names=False) as mixer:
            await mixer.wait_synced()
            assert mixer.get(Input(6), "fader").raw == 0x62
    finally:
        await fake.stop()
