from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.errors import ParameterRangeError
from qumixer_py.protocol.channels import Input, Mix
from qumixer_py.protocol.registry import send_target_vx
from qumixer_py.testing.fake_qu import FakeQu
from tests.hardware._safety import assert_writable
from tests.hardware.conftest import restoring

FakeQuFactory = Callable[..., Awaitable[FakeQu]]

#: Input 1's fader (CH 0x20, param 0x17, VX 0x07) preloaded, so the console
#: really reports a prior value for restoring() to write back. FakeQu's default
#: scene is empty, which would make every prior value unknown.
_INPUT1_FADER_SCENE = {(0x20, 0x17, 0x07): 0x40}


@pytest.mark.parametrize("param", ["local_48v", "dsnake_gain", "insert", "preamp_source"])
def test_denylisted_params_are_refused(param: str) -> None:
    with pytest.raises(ParameterRangeError):
        assert_writable(param)


def test_dsnake_is_allowed_with_an_ar_rack() -> None:
    assert_writable("dsnake_gain", ar_rack=True)


async def test_restoring_writes_back_a_fixed_vx_param(fake_qu_factory: FakeQuFactory) -> None:
    fake = await fake_qu_factory(scene=dict(_INPUT1_FADER_SCENE))
    mixer = AsyncQuMixer("127.0.0.1", fake.port, sync_names=False)
    await mixer.connect()
    await mixer.wait_synced()
    before = mixer.get(Input(1), "fader").raw
    assert before == 0x40
    async with restoring(mixer):
        await mixer.set(Input(1), "fader", raw=0x22)
        assert mixer.get(Input(1), "fader").raw == 0x22
    assert mixer.get(Input(1), "fader").raw == before
    await mixer.close()


async def test_restoring_writes_back_a_send_target_param(fake_qu_factory: FakeQuFactory) -> None:
    fake = await fake_qu_factory()
    mixer = AsyncQuMixer("127.0.0.1", fake.port, sync_names=False)
    await mixer.connect()
    await mixer.wait_synced()
    await mixer.set(Input(1), "send_level", raw=0x30, vx=Mix.MIX1)
    sub = send_target_vx(Mix.MIX1, mixer.caps)
    sub_before = mixer.get(Input(1), "send_level", sub).raw
    assert sub_before == 0x30
    async with restoring(mixer):
        await mixer.set(Input(1), "send_level", raw=0x50, vx=Mix.MIX1)
    assert mixer.get(Input(1), "send_level", sub).raw == sub_before
    await mixer.close()


async def test_restoring_skips_and_reports_an_unknown_prior_value(
    fake_qu_factory: FakeQuFactory,
) -> None:
    fake = await fake_qu_factory()
    mixer = AsyncQuMixer("127.0.0.1", fake.port, sync_names=False)
    await mixer.connect()
    await mixer.wait_synced()
    prior = mixer.get(Input(7), "hpf_freq").raw
    async with restoring(mixer) as restorer:
        await mixer.set(Input(7), "hpf_freq", raw=0x10)
    if prior is None:
        assert restorer.skipped == [("Input 7", "hpf_freq")]
    else:
        assert restorer.skipped == []
        assert mixer.get(Input(7), "hpf_freq").raw == prior
    await mixer.close()
