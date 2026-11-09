import asyncio
import json

import pytest
from aiohttp.test_utils import TestServer

from dialup_plugin import simulator

from .helpers import OWNER, RecordingHandler, configure, make_adapter


def test_draw_screen_counts_chars():
    out = simulator.draw_screen("CON Hermes\n1 Ask or reply")
    assert "| Hermes" in out and "21 chars, session open" in out


def test_outbox_watcher_only_new_lines_for_phone(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text(json.dumps({"to": OWNER, "message": "old", "segments": 1}) + "\n")
    lines = []
    watcher = simulator.OutboxWatcher(outbox, OWNER, emit=lines.append)
    with open(outbox, "a") as handle:
        handle.write(json.dumps({"to": "+2340000000000", "message": "not mine", "segments": 1}) + "\n")
        handle.write(json.dumps({"to": OWNER, "message": "new answer", "segments": 1}) + "\n")
    assert watcher.poll() == 1
    assert "new answer" in lines[0] and "old" not in lines[0]


@pytest.mark.asyncio
async def test_script_drives_real_webhook(monkeypatch):
    configure(monkeypatch, DIALUP_WEBHOOK_SECRET=None)  # local transport on loopback: no secret
    adapter = await make_adapter()
    handler = RecordingHandler()
    adapter.handle_message = handler
    server = TestServer(adapter.build_app(), host="127.0.0.1")
    await server.start_server()
    try:
        url = f"http://127.0.0.1:{server.port}/dialup/ussd"
        lines = []
        await asyncio.to_thread(simulator.run_script, url, OWNER, "*384*55#", ["1", "hello *from* sim"],
                                emit=lines.append)
        await asyncio.sleep(0.05)
        assert any("Sent to Hermes" in line for line in lines)
        assert [e.text for e in handler.events] == ["hello *from* sim"]
    finally:
        await server.close()
        await adapter.disconnect()
