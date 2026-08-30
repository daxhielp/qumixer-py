"""PRD 11.1 quickstart, verbatim -- the async API end to end.

Requires a real Qu console reachable at the host below; this is not exercised
by the test suite (see task-9.2-report.md, Step 5). Watch the GEQ screen for
the last two calls: both must move band 15 (1 kHz), not band 14 (800 Hz) --
the v1.0 PRD correction, and the easiest thing to get subtly wrong.
"""

import asyncio

from qumidi import AsyncQuMixer
from qumidi.protocol.channels import Mix


async def main() -> None:
    async with AsyncQuMixer("192.168.1.60") as mixer:
        await mixer.wait_synced()
        print(mixer.model, mixer.firmware)

        await mixer.set(mixer.input(6).id, "fader", raw=0x62)
        await mixer.set(mixer.input(6).id, "send_level", raw=0x50, vx=Mix.MIX3)
        print(mixer.get(mixer.input(6).id, "fader"))

        ch = mixer.input(6)
        print(ch.name)
        await ch.set_name("Kick")
        await ch.mute(True)
        await ch.mute(False)
        await ch.fader.set_db(-6.0)
        await ch.send(Mix.MIX3).set_level_db(-12.0)
        await ch.send(Mix.MIX3).set_pre(True)
        await ch.peq.hf.set_gain_db(3.0)
        await ch.gate.set_enabled(True)
        await ch.preamp.set_gain_db(35.0)

        await mixer.lr.geq.set_band_db(-2.0, band=15)  # 1 kHz
        await mixer.lr.geq.set_band_db(-2.0, freq=1000)  # the same band


if __name__ == "__main__":
    asyncio.run(main())
