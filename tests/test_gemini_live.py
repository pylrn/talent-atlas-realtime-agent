from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pipeline.gemini_live import GeminiLiveBridge, GoogleGenAILiveTransport, LivePacket
from pipeline.realtime_tools import RealtimeToolDispatcher


class FakeTransport:
    def __init__(self, packets: list[LivePacket]) -> None:
        self.packets: asyncio.Queue[LivePacket | None] = asyncio.Queue()
        for packet in packets:
            self.packets.put_nowait(packet)
        self.packets.put_nowait(None)
        self.audio_sent: list[bytes] = []
        self.text_sent: list[str] = []
        self.tool_responses: list[tuple[str, str, dict]] = []
        self.closed = False

    async def send_audio(self, data: bytes) -> None:
        self.audio_sent.append(data)

    async def send_text(self, text: str) -> None:
        self.text_sent.append(text)

    async def send_tool_response(self, call_id: str, name: str, response: dict) -> None:
        self.tool_responses.append((call_id, name, response))

    async def receive(self):
        while True:
            packet = await self.packets.get()
            if packet is None:
                return
            yield packet

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_bridge_forwards_audio_text_transcripts_and_output_audio() -> None:
    transport = FakeTransport([
        LivePacket(kind="input_transcript", data={"text": "find python", "final": False}),
        LivePacket(kind="output_transcript", data={"text": "I found matches"}),
        LivePacket(kind="audio", binary=b"pcm-output"),
        LivePacket(kind="interrupted", data={"reason": "barge_in"}),
    ])
    events: list[tuple[str, dict]] = []
    audio: list[bytes] = []
    bridge = GeminiLiveBridge(
        transport,
        RealtimeToolDispatcher({}),
        on_event=lambda kind, data: _append(events, kind, data),
        on_audio=lambda data: _append_audio(audio, data),
    )

    await bridge.send_audio(b"pcm-input")
    await bridge.send_text("typed fallback")
    await bridge.run()

    assert transport.audio_sent == [b"pcm-input"]
    assert transport.text_sent == ["typed fallback"]
    assert audio == [b"pcm-output"]
    assert [kind for kind, _ in events] == [
        "transcript.input",
        "transcript.output",
        "audio.interrupted",
    ]


@pytest.mark.asyncio
async def test_bridge_dispatches_correlated_tool_call_and_returns_result() -> None:
    async def search(payload: dict) -> dict:
        return {"revision_id": "rev-2", "candidate_count": payload["top_k"]}

    transport = FakeTransport([
        LivePacket(
            kind="tool_call",
            data={
                "call_id": "call-1",
                "name": "search_candidates",
                "arguments": {"query": "python", "top_k": 6},
            },
        ),
    ])
    events: list[tuple[str, dict]] = []
    bridge = GeminiLiveBridge(
        transport,
        RealtimeToolDispatcher({"search_candidates": search}),
        on_event=lambda kind, data: _append(events, kind, data),
        on_audio=lambda data: _append_audio([], data),
    )

    await bridge.run()

    assert transport.tool_responses == [
        ("call-1", "search_candidates", {"revision_id": "rev-2", "candidate_count": 6})
    ]
    assert [kind for kind, _ in events] == ["tool.started", "tool.completed"]


@pytest.mark.asyncio
async def test_bridge_keeps_receiving_and_cancels_work_on_barge_in() -> None:
    release = asyncio.Event()

    async def search(payload: dict) -> dict:
        await release.wait()
        return {"revision_id": "rev-cancelled"}

    async def cancel(payload: dict) -> dict:
        release.set()
        return {"cancelled": True, "reason": payload["reason"]}

    transport = FakeTransport([
        LivePacket(
            kind="tool_call",
            data={"call_id": "call-2", "name": "search_candidates", "arguments": {"query": "python"}},
        ),
        LivePacket(kind="interrupted", data={"reason": "barge_in"}),
    ])
    events: list[tuple[str, dict]] = []
    bridge = GeminiLiveBridge(
        transport,
        RealtimeToolDispatcher({"search_candidates": search, "cancel_current_action": cancel}),
        on_event=lambda kind, data: _append(events, kind, data),
        on_audio=lambda data: _append_audio([], data),
    )

    await asyncio.wait_for(bridge.run(), timeout=1)

    kinds = [kind for kind, _ in events]
    assert kinds.index("audio.interrupted") < kinds.index("tool.completed")
    assert transport.tool_responses[0][0] == "call-2"


@pytest.mark.asyncio
async def test_bridge_closes_transport() -> None:
    transport = FakeTransport([])
    bridge = GeminiLiveBridge(
        transport,
        RealtimeToolDispatcher({}),
        on_event=lambda kind, data: _append([], kind, data),
        on_audio=lambda data: _append_audio([], data),
    )

    await bridge.close()

    assert transport.closed is True


@pytest.mark.asyncio
async def test_google_transport_reopens_receive_for_each_conversation_turn() -> None:
    class TurnSession:
        def __init__(self) -> None:
            self.calls = 0

        async def receive(self):
            self.calls += 1
            yield SimpleNamespace(
                server_content=SimpleNamespace(
                    input_transcription=None,
                    output_transcription=SimpleNamespace(text=f"turn-{self.calls}"),
                    model_turn=None,
                    interrupted=False,
                    turn_complete=True,
                ),
                tool_call=None,
                session_resumption_update=None,
                go_away=None,
            )

    transcript = GoogleGenAILiveTransport(None, None, TurnSession())
    packets = transcript.receive()

    first = await anext(packets)
    second = await anext(packets)
    await packets.aclose()

    assert first.kind == "output_transcript"
    assert first.data["text"] == "turn-1"
    assert second.data["text"] == "turn-2"


async def _append(target: list, kind: str, data: dict) -> None:
    target.append((kind, data))


async def _append_audio(target: list[bytes], data: bytes) -> None:
    target.append(data)
