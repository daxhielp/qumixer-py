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
