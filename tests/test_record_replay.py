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
    assert bytes([0xB0, 0x63, 0x25]) in data     # the state burst


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
