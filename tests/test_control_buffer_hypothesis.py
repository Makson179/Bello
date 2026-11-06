"""Synthetic upstream mechanism check, not evidence of the live failure cause."""
import asyncio
import json

import pytest
from claude_agent_sdk._internal.query import Query


class SyntheticTransport:
    def __init__(self, count):
        self.count = count
        self.frames = asyncio.Queue()

    async def read_messages(self):
        while True:
            yield await self.frames.get()

    async def write(self, raw):
        request = json.loads(raw)
        for _ in range(self.count):
            self.frames.put_nowait({"type": "system", "subtype": "synthetic_notice"})
        self.frames.put_nowait({"type": "control_response", "response": {
            "subtype": "success", "request_id": request["request_id"], "response": {}}})

    async def close(self):
        pass


@pytest.mark.parametrize("count,blocked", [(100, False), (101, True)])
async def test_prequery_control_can_be_starved_by_undrained_regular_frames(count, blocked):
    query = Query(SyntheticTransport(count), is_streaming_mode=True, initialize_timeout=0.1)
    await query.start()
    try:
        if blocked:
            with pytest.raises(Exception, match="Control request timeout: initialize"):
                await query.initialize()
            stats = query._message_receive.statistics()
            assert stats.current_buffer_used == 100 and stats.tasks_waiting_send == 1
        else:
            await query.initialize()
    finally:
        await query.close()
