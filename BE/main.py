import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import wave
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Set

import numpy as np
import sounddevice as sd
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import settings
from services.ai_service import AIService
from services.audio_processing import AudioProcessor

# ─────────────────────────────────────────────
#  LOGGING CONFIGURATION
# ─────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("icd-backend")

# ─────────────────────────────────────────────
#  SERVICES & GLOBAL INSTANCES
# ─────────────────────────────────────────────
audio_processor = AudioProcessor(
    sample_rate=settings.SAMPLE_RATE,
    channels=settings.CHANNELS,
    sample_width=settings.SAMPLE_WIDTH,
)
ai_service = AIService()

# Connected clients registry
browser_clients: Set[WebSocket] = set()
esp32_clients: Dict[str, WebSocket] = {}


# ─────────────────────────────────────────────
#  DATABASE MANAGEMENT
# ─────────────────────────────────────────────
def init_db() -> None:
    """Initializes SQLite database with WAL mode and indexes for history logging."""
    settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT    NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON chat_history(session_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON chat_history(created_at);")
        conn.commit()
    logger.info("Database initialized at %s", settings.DB_PATH.resolve())


def save_message(session_id: str, role: str, content: str) -> None:
    """Saves a conversation turn to the database."""
    try:
        with sqlite3.connect(settings.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO chat_history (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, datetime.utcnow().isoformat()),
            )
            conn.commit()
    except Exception as exc:
        logger.error("Failed to save message to database: %s", exc)


def get_recent_history(limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieves the most recent messages."""
    try:
        with sqlite3.connect(settings.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, session_id, role, content, created_at FROM chat_history ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in reversed(rows)]
    except Exception as exc:
        logger.error("Failed to query history: %s", exc)
        return []


# ─────────────────────────────────────────────
#  LIFESPAN HANDLER
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Handles startup and shutdown events."""
    logger.info("Starting ICD Voice Backend on %s:%d...", settings.HOST, settings.PORT)
    init_db()

    # Pre-warm Whisper in the background
    asyncio.create_task(asyncio.to_thread(ai_service.warm_up_stt))

    yield

    logger.info("Shutting down ICD Voice Backend...")
    # Clean up connected sockets
    for ws in list(browser_clients):
        try:
            await ws.close()
        except Exception:
            pass
    for ws in list(esp32_clients.values()):
        try:
            await ws.close()
        except Exception:
            pass


# ─────────────────────────────────────────────
#  FASTAPI APPLICATION
# ─────────────────────────────────────────────
app = FastAPI(
    title="ICD Intelligent Communication Device API",
    description="Backend API & WebSocket gateway for ESP32 voice assistant and browser dashboard.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
settings.STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")


# ─────────────────────────────────────────────
#  BROADCAST & LOGGING HELPERS
# ─────────────────────────────────────────────
async def broadcast_browsers(payload: dict) -> None:
    dead: Set[WebSocket] = set()
    for ws in list(browser_clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    browser_clients.difference_update(dead)


async def broadcast_browsers_bytes(data: bytes) -> None:
    dead: Set[WebSocket] = set()
    for ws in list(browser_clients):
        try:
            await ws.send_bytes(data)
        except Exception:
            dead.add(ws)
    browser_clients.difference_update(dead)


async def log_interaction(
    session_id: str,
    direction: str,
    step: str,
    detail: str = "",
    extra: Optional[dict] = None,
) -> None:
    """Logs events to console and broadcasts to active web dashboards."""
    dir_label = {
        "esp32->server": "ESP32 → Server",
        "server->esp32": "Server → ESP32",
        "server": "Server",
    }.get(direction, direction)

    log_line = f"[{session_id[:8]}] {dir_label} | {step}"
    if detail:
        log_line += f" | {detail}"
    logger.info(log_line)

    payload: Dict[str, Any] = {
        "event": "interaction",
        "session_id": session_id,
        "direction": direction,
        "step": step,
        "detail": detail,
        "time": datetime.utcnow().isoformat(),
    }
    if extra:
        payload.update(extra)

    await broadcast_browsers(payload)


# ─────────────────────────────────────────────
#  SERVER AUDIO PLAYBACK
# ─────────────────────────────────────────────
def _play_mp3_blocking(audio_bytes: bytes) -> None:
    """Plays audio through the host machine's audio output."""
    if not audio_bytes or not settings.SERVER_AUDIO_PLAYBACK:
        return

    ffplay = shutil.which("ffplay")
    if ffplay:
        subprocess.run(
            [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", "-"],
            input=audio_bytes,
            check=False,
        )
        return

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("Neither ffplay nor ffmpeg is installed for host audio playback.")
        return

    mp3_path = wav_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio_bytes)
            mp3_path = f.name
        wav_path = mp3_path.replace(".mp3", ".wav")
        subprocess.run(
            [ffmpeg, "-y", "-i", mp3_path, "-ar", "24000", "-ac", "1", "-f", "wav", wav_path],
            check=True,
            capture_output=True,
        )
        with wave.open(wav_path, "rb") as wf:
            pcm = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
        samples = np.frombuffer(pcm, dtype=np.int16)
        sd.play(samples, samplerate=rate)
        sd.wait()
    except Exception as exc:
        logger.warning("Local server audio playback failed: %s", exc)
    finally:
        for p in (mp3_path, wav_path):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


async def play_audio_server(audio_bytes: bytes) -> None:
    if settings.SERVER_AUDIO_PLAYBACK:
        await asyncio.to_thread(_play_mp3_blocking, audio_bytes)


# ─────────────────────────────────────────────
#  TTS STREAMING TO ESP32 & WEB
# ─────────────────────────────────────────────
async def send_tts_stream(
    websocket: WebSocket,
    session_id: str,
    mp3_bytes: bytes,
    pcm_bytes: bytes,
) -> None:
    """
    Streams audio to the connected client.
    - ESP32 receives raw PCM16 16kHz Mono chunks (ready for direct I2S DAC playback).
    - Browser clients receive MP3 chunks (natively playable by web browsers).
    """
    if not mp3_bytes and not pcm_bytes:
        return

    # Broadcast MP3 to browser dashboards
    if mp3_bytes:
        browser_meta = {
            "event": "tts_start",
            "size": len(mp3_bytes),
            "format": "mp3",
            "session_id": session_id,
        }
        await broadcast_browsers(browser_meta)

        for offset in range(0, len(mp3_bytes), settings.TTS_CHUNK_SIZE):
            chunk = mp3_bytes[offset : offset + settings.TTS_CHUNK_SIZE]
            await broadcast_browsers_bytes(chunk)

        await broadcast_browsers({"event": "tts_end", "session_id": session_id})

    # Stream PCM16 to ESP32
    target_bytes = pcm_bytes if pcm_bytes else mp3_bytes
    format_type = "pcm" if pcm_bytes else "mp3"

    esp_meta = {
        "event": "tts_start",
        "size": len(target_bytes),
        "format": format_type,
        "sample_rate": settings.SAMPLE_RATE,
        "session_id": session_id,
    }
    await websocket.send_json(esp_meta)
    await log_interaction(
        session_id,
        "server->esp32",
        "tts_start",
        f"{len(target_bytes)} bytes ({format_type.upper()})",
    )

    sent = 0
    chunk_size = settings.TTS_CHUNK_SIZE
    for offset in range(0, len(target_bytes), chunk_size):
        chunk = target_bytes[offset : offset + chunk_size]
        await websocket.send_bytes(chunk)
        sent += len(chunk)
        await asyncio.sleep(0.005)  # Flow control: avoid DMA overflow on ESP32

    end_meta = {"event": "tts_end", "session_id": session_id}
    await websocket.send_json(end_meta)
    await log_interaction(session_id, "server->esp32", "tts_end", f"Transmitted {sent} bytes")


# ─────────────────────────────────────────────
#  AUDIO PROCESSING PIPELINE
# ─────────────────────────────────────────────
async def process_audio_pipeline(
    session_id: str,
    pcm_bytes: bytes,
) -> Dict[str, Any]:
    """
    Orchestrates the full voice loop:
    PCM16 -> WAV -> Whisper STT -> LLM / Math -> Edge TTS -> PCM16 / MP3
    """
    duration = len(pcm_bytes) / (settings.SAMPLE_RATE * settings.SAMPLE_WIDTH)
    await log_interaction(
        session_id,
        "server",
        "pipeline_start",
        f"{len(pcm_bytes)} bytes (~{duration:.1f}s audio)",
    )

    # 1. Audio cleaning and WAV packaging
    try:
        wav_bytes = await asyncio.to_thread(audio_processor.pcm16_to_wav_bytes, pcm_bytes)
        await log_interaction(session_id, "server", "audio_ok", "PCM16 processed & filtered")
    except Exception as exc:
        logger.warning("Audio validation error: %s", exc)
        await log_interaction(session_id, "server", "audio_error", str(exc))
        return {
            "error": f"audio_error: {exc}",
            "transcript": "",
            "assistant_text": "",
            "mp3_bytes": b"",
            "pcm_bytes": b"",
        }

    # 2. Speech-to-Text (Whisper)
    try:
        await log_interaction(session_id, "server", "stt_start", "Whisper is recognizing speech...")
        transcript = await asyncio.to_thread(ai_service.transcribe_audio, wav_bytes)
        await log_interaction(session_id, "server", "stt_done", transcript or "(No speech detected)")
    except Exception as exc:
        logger.exception("STT recognition error: %s", exc)
        await log_interaction(session_id, "server", "stt_error", str(exc))
        return {
            "error": f"stt_error: {exc}",
            "transcript": "",
            "assistant_text": "",
            "mp3_bytes": b"",
            "pcm_bytes": b"",
        }

    if not transcript:
        return {
            "transcript": "",
            "assistant_text": "",
            "mp3_bytes": b"",
            "pcm_bytes": b"",
        }

    await asyncio.to_thread(save_message, session_id, "user", transcript)

    # 3. LLM / Intent Reasoning
    try:
        await log_interaction(session_id, "server", "llm_start", "AI model is generating response...")
        assistant_text = await ai_service.generate_text(transcript)
        await log_interaction(session_id, "server", "llm_done", assistant_text)
    except Exception as exc:
        logger.exception("LLM generation error: %s", exc)
        await log_interaction(session_id, "server", "llm_error", str(exc))
        return {
            "error": f"llm_error: {exc}",
            "transcript": transcript,
            "assistant_text": "",
            "mp3_bytes": b"",
            "pcm_bytes": b"",
        }

    await asyncio.to_thread(save_message, session_id, "assistant", assistant_text)

    # 4. Text-to-Speech (Edge TTS)
    mp3_bytes = b""
    pcm_transcoded = b""
    try:
        await log_interaction(session_id, "server", "tts_start", "Edge-TTS is synthesizing audio...")
        mp3_bytes = await ai_service.text_to_speech(assistant_text)
        await log_interaction(
            session_id,
            "server",
            "tts_done",
            f"{len(mp3_bytes)} bytes MP3 synthesized",
        )

        # Transcode MP3 to PCM16 for ESP32 I2S output
        if mp3_bytes:
            pcm_transcoded = await asyncio.to_thread(audio_processor.mp3_to_pcm16, mp3_bytes)
    except Exception as exc:
        logger.exception("TTS synthesis error: %s", exc)
        await log_interaction(session_id, "server", "tts_error", str(exc))

    # Optional local host speaker playback
    if mp3_bytes and settings.SERVER_AUDIO_PLAYBACK:
        asyncio.create_task(play_audio_server(mp3_bytes))

    return {
        "transcript": transcript,
        "assistant_text": assistant_text,
        "mp3_bytes": mp3_bytes,
        "pcm_bytes": pcm_transcoded,
    }


# ─────────────────────────────────────────────
#  HTTP ROUTES
# ─────────────────────────────────────────────
@app.get("/", response_class=FileResponse)
async def serve_dashboard() -> FileResponse:
    """Serves the real-time monitoring web dashboard."""
    index_file = settings.STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h2>index.html not found in static directory</h2>", status_code=404)
    return FileResponse(str(index_file))


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint showing system and provider status."""
    return {
        "status": "healthy",
        "whisper_model": settings.WHISPER_MODEL,
        "llm_provider": settings.LLM_PROVIDER,
        "gemini_configured": bool(settings.GEMINI_API_KEY),
        "ollama_url": settings.OLLAMA_BASE_URL,
        "tts_voice": settings.TTS_VOICE,
        "active_esp32": len(esp32_clients),
        "active_browsers": len(browser_clients),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/history")
async def api_history(limit: int = Query(default=50, ge=1, le=200)) -> List[Dict[str, Any]]:
    """Retrieves conversation history logs."""
    return await asyncio.to_thread(get_recent_history, limit)


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


@app.post("/api/chat")
async def api_chat(req: ChatRequest) -> Dict[str, Any]:
    """Allows text-based interaction for testing without ESP32 hardware."""
    session_id = req.session_id or str(uuid.uuid4())
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    await log_interaction(session_id, "server", "web_query", text)
    await asyncio.to_thread(save_message, session_id, "user", text)

    answer = await ai_service.generate_text(text)
    await asyncio.to_thread(save_message, session_id, "assistant", answer)

    return {
        "session_id": session_id,
        "query": text,
        "answer": answer,
    }


# ─────────────────────────────────────────────
#  WEBSOCKET GATEWAY /ws/chat
# ─────────────────────────────────────────────
@app.websocket("/ws/chat")
async def websocket_gateway(websocket: WebSocket) -> None:
    """
    Main WebSocket endpoint supporting both hardware devices (ESP32)
    and web monitoring dashboards.
    """
    await websocket.accept()

    session_id = websocket.query_params.get("session_id", str(uuid.uuid4()))
    client_type = websocket.query_params.get("type", "esp32").strip().lower()

    # ── BROWSER CLIENT ──
    if client_type == "browser":
        browser_clients.add(websocket)
        logger.info("Browser dashboard connected: %s (Total: %d)", session_id[:8], len(browser_clients))
        await websocket.send_json({
            "event": "esp32_status",
            "count": len(esp32_clients),
            "devices": list(esp32_clients.keys()),
        })
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            browser_clients.discard(websocket)
            logger.info("Browser dashboard disconnected (Total: %d)", len(browser_clients))
        return

    # ── ESP32 HARDWARE CLIENT ──
    esp32_clients[session_id] = websocket
    logger.info("ESP32 connected: session_id=%s (Total online: %d)", session_id, len(esp32_clients))
    await log_interaction(session_id, "esp32->server", "connected", "Device connected to WebSocket")

    await broadcast_browsers({
        "event": "esp32_connected",
        "session_id": session_id,
        "count": len(esp32_clients),
    })

    audio_buffer = bytearray()
    chunk_counter = 0

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            # 1. Incoming binary audio chunk
            raw_bytes = msg.get("bytes")
            if raw_bytes is not None:
                audio_buffer.extend(raw_bytes)
                chunk_counter += 1
                if chunk_counter == 1 or chunk_counter % 20 == 0:
                    dur = len(audio_buffer) / (settings.SAMPLE_RATE * settings.SAMPLE_WIDTH)
                    await log_interaction(
                        session_id,
                        "esp32->server",
                        "audio_chunk",
                        f"Chunk #{chunk_counter} ({len(audio_buffer)} bytes, ~{dur:.1f}s)",
                    )
                # Max buffer safeguard (15 seconds)
                max_bytes = settings.SAMPLE_RATE * settings.SAMPLE_WIDTH * 15
                if len(audio_buffer) > max_bytes:
                    logger.warning("Buffer overflow detected; trimming oldest frames.")
                    audio_buffer[:] = audio_buffer[-(settings.SAMPLE_RATE * settings.SAMPLE_WIDTH * 8) :]
                continue

            # 2. Incoming text / control frame
            raw_text = msg.get("text")
            if raw_text is None:
                continue

            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError:
                payload = {"event": raw_text.strip()}

            event = payload.get("event", "").strip().lower()

            if event == "ping":
                await websocket.send_json({"event": "pong"})
                continue

            if event == "reset":
                audio_buffer.clear()
                chunk_counter = 0
                await websocket.send_json({"event": "buffer_reset"})
                await log_interaction(session_id, "server->esp32", "buffer_reset", "Audio buffer cleared")
                continue

            if event in ("end", "end_of_audio", "stop_recording"):
                duration = (
                    len(audio_buffer) / (settings.SAMPLE_RATE * settings.SAMPLE_WIDTH)
                    if audio_buffer
                    else 0.0
                )
                await log_interaction(
                    session_id,
                    "esp32->server",
                    "end_of_audio",
                    f"{chunk_counter} chunks, {len(audio_buffer)} bytes (~{duration:.1f}s)",
                )

                if not audio_buffer:
                    await websocket.send_json({"event": "warning", "message": "Audio buffer is empty."})
                    await log_interaction(session_id, "server->esp32", "warning", "Empty audio buffer")
                    continue

                # Notify clients that pipeline has started
                await websocket.send_json({"event": "processing"})
                await broadcast_browsers({"event": "processing", "session_id": session_id})
                await log_interaction(session_id, "server->esp32", "processing", "STT → LLM → TTS pipeline initiated")

                pcm_snapshot = bytes(audio_buffer)
                audio_buffer.clear()
                chunk_counter = 0

                result = await process_audio_pipeline(
                    session_id=session_id,
                    pcm_bytes=pcm_snapshot,
                )

                if result.get("error"):
                    await websocket.send_json({"event": "error", "message": result["error"]})
                    await broadcast_browsers({
                        "event": "error",
                        "message": result["error"],
                        "session_id": session_id,
                    })
                    await log_interaction(session_id, "server->esp32", "error", result["error"])
                    continue

                if not result.get("transcript"):
                    await websocket.send_json({"event": "stt_empty"})
                    await broadcast_browsers({"event": "stt_empty", "session_id": session_id})
                    await log_interaction(session_id, "server->esp32", "stt_empty", "No voice detected")
                    continue

                # Transmit results to ESP32 & Browser
                await websocket.send_json({"event": "transcript", "text": result["transcript"]})
                await websocket.send_json({"event": "assistant_text", "text": result["assistant_text"]})

                await broadcast_browsers({
                    "event": "transcript",
                    "text": result["transcript"],
                    "session_id": session_id,
                })
                await broadcast_browsers({
                    "event": "assistant_text",
                    "text": result["assistant_text"],
                    "session_id": session_id,
                })

                # Stream audio response to ESP32
                if result.get("mp3_bytes") or result.get("pcm_bytes"):
                    await send_tts_stream(
                        websocket=websocket,
                        session_id=session_id,
                        mp3_bytes=result.get("mp3_bytes", b""),
                        pcm_bytes=result.get("pcm_bytes", b""),
                    )

                await log_interaction(session_id, "server", "turn_complete", "Turn finished successfully")

    except WebSocketDisconnect:
        logger.info("ESP32 disconnected: session_id=%s", session_id)
        await log_interaction(session_id, "esp32->server", "disconnected", "Device disconnected")
    except Exception as exc:
        logger.exception("WebSocket connection error: %s", exc)
        await log_interaction(session_id, "server", "ws_error", str(exc))
        try:
            await websocket.send_json({"event": "error", "message": str(exc)})
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        esp32_clients.pop(session_id, None)
        await broadcast_browsers({
            "event": "esp32_disconnected",
            "session_id": session_id,
            "count": len(esp32_clients),
        })
        logger.info("ESP32 cleanup finished. Remaining online: %d", len(esp32_clients))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        log_level=settings.LOG_LEVEL.lower(),
    )
