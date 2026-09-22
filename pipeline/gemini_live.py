"""Provider-neutral realtime bridge plus a lazy Gemini Live SDK transport."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pipeline.realtime_tools import RealtimeToolDispatcher, ToolRejected


@dataclass(slots=True)
class LivePacket:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    binary: bytes | None = None


class LiveTransport(Protocol):
    async def send_audio(self, data: bytes) -> None: ...
    async def send_media(self, data: bytes, mime_type: str) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def send_tool_response(self, call_id: str, name: str, response: dict[str, Any]) -> None: ...
    def receive(self) -> AsyncIterator[LivePacket]: ...
    async def close(self) -> None: ...


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
AudioCallback = Callable[[bytes], Awaitable[None]]


class GeminiLiveBridge:
    def __init__(
        self,
        transport: LiveTransport,
        tools: RealtimeToolDispatcher,
        *,
        on_event: EventCallback,
        on_audio: AudioCallback,
    ) -> None:
        self.transport = transport
        self.tools = tools
        self.on_event = on_event
        self.on_audio = on_audio
        self._tool_tasks: set[asyncio.Task[None]] = set()

    async def send_audio(self, data: bytes) -> None:
        await self.transport.send_audio(data)

    async def send_media(self, data: bytes, mime_type: str) -> None:
        await self.transport.send_media(data, mime_type)

    async def send_text(self, text: str) -> None:
        await self.transport.send_text(text)

    async def run(self) -> None:
        async for packet in self.transport.receive():
            if packet.kind == "audio" and packet.binary is not None:
                await self.on_audio(packet.binary)
            elif packet.kind == "input_transcript":
                await self.on_event("transcript.input", packet.data)
            elif packet.kind == "output_transcript":
                await self.on_event("transcript.output", packet.data)
            elif packet.kind == "interrupted":
                # Interruption is not cancellation. In-flight retrieval is left
                # running so the next revision can reuse what already completed.
                await self.on_event("audio.interrupted", packet.data)
                await self.tools.note_barge_in(
                    reason=str(packet.data.get("reason") or "barge_in")
                )
            elif packet.kind == "tool_call":
                task = asyncio.create_task(self._handle_tool_call(packet.data))
                self._tool_tasks.add(task)
                task.add_done_callback(self._tool_tasks.discard)
            elif packet.kind in {"session_resumption", "go_away", "error"}:
                await self.on_event(f"gemini.{packet.kind}", packet.data)
        if self._tool_tasks:
            await asyncio.gather(*tuple(self._tool_tasks), return_exceptions=True)

    async def close(self) -> None:
        await self.transport.close()

    async def _handle_tool_call(self, data: dict[str, Any]) -> None:
        call_id = str(data.get("call_id") or "")
        name = str(data.get("name") or "")
        arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
        await self.on_event("tool.started", {"call_id": call_id, "name": name, "arguments": arguments})
        try:
            result = await self.tools.dispatch(name, arguments)
        except asyncio.CancelledError:
            result = {"cancelled": True, "reason": "superseded_or_interrupted"}
            await self.on_event("tool.cancelled", {"call_id": call_id, "name": name, **result})
        except ToolRejected as exc:
            result = {"error": str(exc), "rejected": True}
            await self.on_event("tool.rejected", {"call_id": call_id, "name": name, **result})
        except Exception as exc:
            result = {"error": str(exc), "failed": True}
            await self.on_event("tool.failed", {"call_id": call_id, "name": name, **result})
        else:
            await self.on_event("tool.completed", {"call_id": call_id, "name": name, "result": result})
        await self.transport.send_tool_response(call_id, name, result)


class GoogleGenAILiveTransport:
    """Thin adapter around google-genai; imported only for real voice sessions."""

    def __init__(self, client: Any, context_manager: Any, session: Any) -> None:
        self._client = client
        self._context_manager = context_manager
        self._session = session

    @classmethod
    async def connect(
        cls,
        *,
        api_key: str,
        model: str,
        system_instruction: str,
        tools: list[dict[str, Any]],
        vad_silence_ms: int = 1200,
        vad_prefix_ms: int = 200,
    ) -> "GoogleGenAILiveTransport":
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=system_instruction,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            tools=[types.Tool(function_declarations=tools)],
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    prefix_padding_ms=vad_prefix_ms,
                    silence_duration_ms=vad_silence_ms,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                )
            ),
            session_resumption=types.SessionResumptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=25000,
                sliding_window=types.SlidingWindow(target_tokens=8000),
            ),
        )
        context_manager = client.aio.live.connect(model=model, config=config)
        session = await context_manager.__aenter__()
        return cls(client, context_manager, session)

    async def send_audio(self, data: bytes) -> None:
        from google.genai import types

        await self._session.send_realtime_input(
            audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
        )

    async def send_media(self, data: bytes, mime_type: str) -> None:
        from google.genai import types

        blob = types.Blob(data=data, mime_type=mime_type)
        if mime_type.startswith("image/"):
            await self._session.send_realtime_input(video=blob)
        elif mime_type in {"audio/wav", "audio/x-wav"}:
            await self._session.send_realtime_input(media=blob)
        else:
            raise ValueError(f"Unsupported Live media type: {mime_type}")

    async def send_text(self, text: str) -> None:
        await self._session.send_realtime_input(text=text)

    async def send_tool_response(self, call_id: str, name: str, response: dict[str, Any]) -> None:
        from google.genai import types

        await self._session.send_tool_response(
            function_responses=[
                types.FunctionResponse(id=call_id, name=name, response={"result": response})
            ]
        )

    async def receive(self) -> AsyncIterator[LivePacket]:
        # google-genai's AsyncSession.receive() yields one complete model turn
        # and then stops. A long-lived voice socket must call it again for the
        # next turn; otherwise the first exchange works and later user audio is
        # uploaded but never processed.
        while True:
            async for response in self._session.receive():
                for packet in _packets_from_response(response):
                    yield packet

    async def close(self) -> None:
        await self._context_manager.__aexit__(None, None, None)
        close = getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


def _packets_from_response(response: Any) -> list[LivePacket]:
    """Translate one google-genai LiveServerMessage into provider-neutral packets."""
    packets: list[LivePacket] = []
    server_content = getattr(response, "server_content", None)
    if server_content is not None:
        input_tx = getattr(server_content, "input_transcription", None)
        if input_tx and getattr(input_tx, "text", None):
            packets.append(LivePacket(
                kind="input_transcript",
                data={"text": input_tx.text, "final": bool(getattr(server_content, "turn_complete", False))},
            ))
        output_tx = getattr(server_content, "output_transcription", None)
        if output_tx and getattr(output_tx, "text", None):
            packets.append(LivePacket(kind="output_transcript", data={"text": output_tx.text}))
        if getattr(server_content, "interrupted", False):
            packets.append(LivePacket(kind="interrupted", data={"reason": "barge_in"}))
        model_turn = getattr(server_content, "model_turn", None)
        for part in getattr(model_turn, "parts", None) or []:
            inline_data = getattr(part, "inline_data", None)
            if inline_data and getattr(inline_data, "data", None):
                packets.append(LivePacket(kind="audio", binary=inline_data.data))

    tool_call = getattr(response, "tool_call", None)
    for function_call in getattr(tool_call, "function_calls", None) or []:
        packets.append(LivePacket(
            kind="tool_call",
            data={
                "call_id": str(getattr(function_call, "id", "")),
                "name": str(getattr(function_call, "name", "")),
                "arguments": dict(getattr(function_call, "args", None) or {}),
            },
        ))
    resumption = getattr(response, "session_resumption_update", None)
    if resumption and getattr(resumption, "new_handle", None):
        packets.append(LivePacket(
            kind="session_resumption",
            data={"handle": resumption.new_handle},
        ))
    go_away = getattr(response, "go_away", None)
    if go_away is not None:
        packets.append(LivePacket(
            kind="go_away",
            data={"time_left": str(getattr(go_away, "time_left", ""))},
        ))
    return packets
