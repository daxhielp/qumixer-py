import asyncio

from qumidi.cli import main


async def test_dump_state_prints_the_synced_values(fake_qu_factory, capsys) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={(0x25, 0x17, 0x07): 0x62})
    rc = await asyncio.get_running_loop().run_in_executor(
        None, main, ["dump-state", "--host", "127.0.0.1", "--port", str(fake.port)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Input 6" in out
    assert "fader" in out


async def test_dump_state_json_is_parseable(fake_qu_factory, capsys) -> None:  # type: ignore[no-untyped-def]
    import json

    fake = await fake_qu_factory(scene={(0x25, 0x17, 0x07): 0x62})
    rc = await asyncio.get_running_loop().run_in_executor(
        None, main, ["dump-state", "--host", "127.0.0.1", "--port", str(fake.port), "--json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["box_id"] == 3
    assert any(entry["param"] == "fader" for entry in payload["values"])


#: group_mode (NRPN 5E, VX 00) is the one registered GLOBAL parameter. The
#: console has no documented CH for it, so it is accepted from any CH and
#: stored under the synthetic GLOBAL channel -- whose cache key carries
#: ch.index (0), NOT the wire byte. 0x20 below is just "some real CH the
#: console might send it on"; what lands in the cache is GLOBAL either way.
GLOBAL_SCENE = {(0x20, 0x5E, 0x00): 0x01}


async def test_dump_state_text_labels_a_global_parameter_as_global(  # type: ignore[no-untyped-def]
    fake_qu_factory, capsys
) -> None:
    # Regression (final review, finding 2): the text path used to call
    # channel_from_raw() on the cache key's first element, but
    # StateCache.key() deliberately substitutes ch.index there for GLOBAL --
    # so group_mode printed as "FX Send 1" (channel_from_raw(0)), a wrong but
    # entirely plausible-looking channel name. The --json path was always
    # right because snapshot.py already reconstructed the channel correctly.
    fake = await fake_qu_factory(scene=GLOBAL_SCENE)
    rc = await asyncio.get_running_loop().run_in_executor(
        None, main, ["dump-state", "--host", "127.0.0.1", "--port", str(fake.port)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "group_mode" in out
    global_rows = [line for line in out.splitlines() if "group_mode" in line]
    assert len(global_rows) == 1
    assert "GLOBAL" in global_rows[0]
    assert "FX Send" not in global_rows[0]


async def test_dump_state_text_still_labels_ordinary_channels_correctly(  # type: ignore[no-untyped-def]
    fake_qu_factory, capsys
) -> None:
    # The refactor must not regress the common case: a real wire channel
    # still resolves through channel_from_raw, alongside a GLOBAL row in the
    # very same dump.
    fake = await fake_qu_factory(scene={**GLOBAL_SCENE, (0x25, 0x17, 0x07): 0x62})
    rc = await asyncio.get_running_loop().run_in_executor(
        None, main, ["dump-state", "--host", "127.0.0.1", "--port", str(fake.port)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    fader_rows = [line for line in out.splitlines() if "fader" in line]
    assert len(fader_rows) == 1
    assert "Input 6" in fader_rows[0]
    assert any("GLOBAL" in line and "group_mode" in line for line in out.splitlines())


async def test_dump_state_json_still_reports_a_global_parameter(  # type: ignore[no-untyped-def]
    fake_qu_factory, capsys
) -> None:
    # The already-correct --json path must survive the shared-helper refactor.
    import json

    fake = await fake_qu_factory(scene=GLOBAL_SCENE)
    rc = await asyncio.get_running_loop().run_in_executor(
        None, main, ["dump-state", "--host", "127.0.0.1", "--port", str(fake.port), "--json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    entries = [e for e in payload["values"] if e["param"] == "group_mode"]
    assert len(entries) == 1
    assert entries[0]["channel_class"] == "GLOBAL"
    assert entries[0]["channel_raw"] == 0
