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
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from services.ai_service import AIService
from services.audio_processing import AudioProcessor

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("icd-backend")

app = FastAPI(title="ICD Backend", version="1.0.0")

SAMPLE_RATE_IN = 16000
TTS_CHUNK_SIZE = 1024

# ─────────────────────────────────────────────
#  CLIENT REGISTRY
# ─────────────────────────────────────────────
browser_clients: set[WebSocket] = set()
esp32_clients: set[str] = set()


async def broadcast_browsers(payload: dict) -> None:
    dead: set[WebSocket] = set()
    for ws_b in list(browser_clients):
        try:
            await ws_b.send_json(payload)
        except Exception:
            dead.add(ws_b)
    browser_clients.difference_update(dead)


async def broadcast_browsers_bytes(data: bytes) -> None:
    dead: set[WebSocket] = set()
    for ws_b in list(browser_clients):
        try:
            await ws_b.send_bytes(data)
        except Exception:
            dead.add(ws_b)
    browser_clients.difference_update(dead)


async def log_interaction(
    session_id: str,
    direction: str,
    step: str,
    detail: str = "",
    extra: dict | None = None,
) -> None:
    """Ghi log console + broadcast lên dashboard."""
    arrow = {"esp32->server": "ESP32 → Server", "server->esp32": "Server → ESP32", "server": "Server"}.get(
        direction, direction
    )
    msg = f"[{session_id[:8]}] {arrow} | {step}"
    if detail:
        msg += f" | {detail}"
    logger.info(msg)

    payload: dict[str, Any] = {
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
#  STATIC FILES
# ─────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=FileResponse)
async def serve_ui() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<h2>index.html not found in /static</h2>", status_code=404)
    return FileResponse(str(index))


# ─────────────────────────────────────────────
#  SERVICES
# ─────────────────────────────────────────────
audio_processor = AudioProcessor(sample_rate=SAMPLE_RATE_IN, channels=1, sample_width=2)
ai_service = AIService()


# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
DB_PATH = Path("chat_history.db")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT    NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            )
            """
        )
        conn.commit()


def save_message(session_id: str, role: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO chat_history (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, datetime.utcnow().isoformat()),
        )
        conn.commit()


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    logger.info("Backend started. DB=%s | UI=http://localhost:8000 | WS=/ws/chat?type=esp32", DB_PATH.resolve())


# ─────────────────────────────────────────────
#  AUDIO PLAYBACK (edge-tts trả về MP3)
# ─────────────────────────────────────────────
def _play_mp3_blocking(audio_bytes: bytes) -> None:
    if not audio_bytes:
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
        logger.error("Không tìm thấy ffplay/ffmpeg — không thể phát TTS. Cài ffmpeg.")
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
    except Exception:
        logger.exception("Phát TTS thất bại")
    finally:
        for path in (mp3_path, wav_path):
            if path and os.path.exists(path):
                os.unlink(path)


async def play_audio(audio_bytes: bytes) -> None:
    await asyncio.to_thread(_play_mp3_blocking, audio_bytes)


async def send_tts_stream(websocket: WebSocket, session_id: str, audio_bytes: bytes) -> None:
    """Gửi audio TTS (MP3) về ESP32 theo chunk + broadcast lên browser."""
    if not audio_bytes:
        return

    meta = {"event": "tts_start", "size": len(audio_bytes), "format": "mp3", "session_id": session_id}
    await websocket.send_json(meta)
    await broadcast_browsers(meta)
    await log_interaction(session_id, "server->esp32", "tts_start", f"{len(audio_bytes)} bytes MP3")

    sent = 0
    for offset in range(0, len(audio_bytes), TTS_CHUNK_SIZE):
        chunk = audio_bytes[offset : offset + TTS_CHUNK_SIZE]
        await websocket.send_bytes(chunk)
        await broadcast_browsers_bytes(chunk)
        sent += len(chunk)

    end_meta = {"event": "tts_end", "session_id": session_id}
    await websocket.send_json(end_meta)
    await broadcast_browsers(end_meta)
    await log_interaction(session_id, "server->esp32", "tts_end", f"Đã gửi {sent} bytes")


# ─────────────────────────────────────────────
#  AUDIO PIPELINE
# ─────────────────────────────────────────────
async def process_audio_message(
    session_id: str,
    pcm_bytes: bytes,
    play_on_server: bool = True,
) -> dict[str, Any]:
    duration = len(pcm_bytes) / (SAMPLE_RATE_IN * 2)
    await log_interaction(session_id, "server", "pipeline_start", f"{len(pcm_bytes)} bytes (~{duration:.1f}s)")

    try:
        wav_bytes = await asyncio.to_thread(audio_processor.pcm16_to_wav_bytes, pcm_bytes)
        await log_interaction(session_id, "server", "audio_ok", "PCM → WAV thành công")
    except Exception as exc:
        logger.exception("Audio processing error")
        await log_interaction(session_id, "server", "audio_error", str(exc))
        return {"error": f"audio_processing_error: {exc}", "transcript": "", "assistant_text": "", "assistant_audio": b""}

    try:
        await log_interaction(session_id, "server", "stt_start", "Whisper đang nhận dạng...")
        transcript = await asyncio.to_thread(ai_service.transcribe_audio, wav_bytes)
        await log_interaction(session_id, "server", "stt_done", transcript or "(rỗng)")
    except Exception as exc:
        logger.exception("STT error")
        await log_interaction(session_id, "server", "stt_error", str(exc))
        return {"error": f"stt_error: {exc}", "transcript": "", "assistant_text": "", "assistant_audio": b""}

    if not transcript:
        return {"transcript": "", "assistant_text": "", "assistant_audio": b""}

    await asyncio.to_thread(save_message, session_id, "user", transcript)

    try:
        await log_interaction(session_id, "server", "llm_start", "Ollama đang trả lời...")
        assistant_text = await ai_service.generate_text(transcript)
        await log_interaction(session_id, "server", "llm_done", assistant_text)
    except Exception as exc:
        logger.exception("LLM error")
        await log_interaction(session_id, "server", "llm_error", str(exc))
        return {"error": f"llm_error: {exc}", "transcript": transcript, "assistant_text": "", "assistant_audio": b""}

    await asyncio.to_thread(save_message, session_id, "assistant", assistant_text)

    try:
        await log_interaction(session_id, "server", "tts_start", "edge-tts đang tạo giọng nói...")
        assistant_audio = await ai_service.text_to_speech(assistant_text)
        await log_interaction(session_id, "server", "tts_done", f"{len(assistant_audio)} bytes MP3")
    except Exception as exc:
        logger.exception("TTS error")
        await log_interaction(session_id, "server", "tts_error", str(exc))
        assistant_audio = b""

    if play_on_server and assistant_audio:
        await log_interaction(session_id, "server", "playback_start", "Đang phát loa máy tính...")
        try:
            await play_audio(assistant_audio)
            await log_interaction(session_id, "server", "playback_done", "Phát xong")
        except Exception:
            logger.exception("Server playback failed")
            await log_interaction(session_id, "server", "playback_error", "Không phát được loa")

    return {
        "transcript": transcript,
        "assistant_text": assistant_text,
        "assistant_audio": assistant_audio,
    }


# ─────────────────────────────────────────────
#  HEALTH
# ─────────────────────────────────────────────
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


# ─────────────────────────────────────────────
#  WEBSOCKET  /ws/chat
# ─────────────────────────────────────────────
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket) -> None:
    await websocket.accept()

    session_id = websocket.query_params.get("session_id", str(uuid.uuid4()))
    client_type = websocket.query_params.get("type", "esp32").lower()

    if client_type == "browser":
        browser_clients.add(websocket)
        logger.info("Browser connected (total=%d)", len(browser_clients))
        await websocket.send_json({
            "event": "esp32_status",
            "count": len(esp32_clients),
            "devices": list(esp32_clients),
        })
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            browser_clients.discard(websocket)
            logger.info("Browser disconnected (total=%d)", len(browser_clients))
        return

    # ── ESP32 ──
    audio_buffer = bytearray()
    chunk_count = 0
    esp32_clients.add(session_id)

    logger.info("ESP32 connected: session_id=%s (total=%d)", session_id, len(esp32_clients))
    await log_interaction(session_id, "esp32->server", "connected", "Thiết bị online")

    await broadcast_browsers({
        "event": "esp32_connected",
        "session_id": session_id,
        "count": len(esp32_clients),
    })

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            raw_bytes = message.get("bytes")
            if raw_bytes is not None:
                audio_buffer.extend(raw_bytes)
                chunk_count += 1
                if chunk_count == 1 or chunk_count % 20 == 0:
                    duration = len(audio_buffer) / (SAMPLE_RATE_IN * 2)
                    await log_interaction(
                        session_id,
                        "esp32->server",
                        "audio_chunk",
                        f"chunk #{chunk_count}, tổng {len(audio_buffer)} bytes (~{duration:.1f}s)",
                    )
                max_bytes = SAMPLE_RATE_IN * 2 * 12
                if len(audio_buffer) > max_bytes:
                    logger.warning("Audio buffer too large, trimming")
                    audio_buffer[:] = audio_buffer[-(SAMPLE_RATE_IN * 2 * 8) :]
                continue

            text_payload = message.get("text")
            if text_payload is None:
                continue

            try:
                payload = json.loads(text_payload)
            except json.JSONDecodeError:
                payload = {"event": text_payload}

            event = payload.get("event", "").lower()

            if event == "ping":
                await websocket.send_json({"event": "pong"})
                await log_interaction(session_id, "server->esp32", "pong", "Phản hồi ping")
                continue

            if event == "reset":
                audio_buffer.clear()
                chunk_count = 0
                await websocket.send_json({"event": "buffer_reset"})
                await log_interaction(session_id, "server->esp32", "buffer_reset", "Đã xóa buffer audio")
                continue

            if event in {"end", "end_of_audio", "stop_recording"}:
                duration = len(audio_buffer) / (SAMPLE_RATE_IN * 2) if audio_buffer else 0
                await log_interaction(
                    session_id,
                    "esp32->server",
                    "end_of_audio",
                    f"{chunk_count} chunks, {len(audio_buffer)} bytes (~{duration:.1f}s)",
                )

                if not audio_buffer:
                    await websocket.send_json({"event": "warning", "message": "Audio buffer is empty."})
                    await log_interaction(session_id, "server->esp32", "warning", "Buffer rỗng")
                    continue

                await websocket.send_json({"event": "processing"})
                await broadcast_browsers({"event": "processing", "session_id": session_id})
                await log_interaction(session_id, "server->esp32", "processing", "Bắt đầu STT → LLM → TTS")

                response = await process_audio_message(
                    session_id=session_id,
                    pcm_bytes=bytes(audio_buffer),
                    play_on_server=True,
                )
                audio_buffer.clear()
                chunk_count = 0

                if response.get("error"):
                    await websocket.send_json({"event": "error", "message": response["error"]})
                    await broadcast_browsers({"event": "error", "message": response["error"], "session_id": session_id})
                    await log_interaction(session_id, "server->esp32", "error", response["error"])
                    continue

                if not response["transcript"]:
                    await websocket.send_json({"event": "stt_empty"})
                    await broadcast_browsers({"event": "stt_empty", "session_id": session_id})
                    await log_interaction(session_id, "server->esp32", "stt_empty", "Không nhận dạng được giọng nói")
                    continue

                await websocket.send_json({"event": "transcript", "text": response["transcript"]})
                await log_interaction(session_id, "server->esp32", "transcript", response["transcript"])

                await websocket.send_json({"event": "assistant_text", "text": response["assistant_text"]})
                await log_interaction(session_id, "server->esp32", "assistant_text", response["assistant_text"])

                await broadcast_browsers({
                    "event": "transcript",
                    "text": response["transcript"],
                    "session_id": session_id,
                })
                await broadcast_browsers({
                    "event": "assistant_text",
                    "text": response["assistant_text"],
                    "session_id": session_id,
                })

                if response["assistant_audio"]:
                    await send_tts_stream(websocket, session_id, response["assistant_audio"])

                await log_interaction(session_id, "server", "turn_complete", "Hoàn tất 1 lượt hội thoại")

    except WebSocketDisconnect:
        logger.info("ESP32 disconnected: session_id=%s", session_id)
        await log_interaction(session_id, "esp32->server", "disconnected", "Thiết bị ngắt kết nối")
    except Exception as exc:
        logger.exception("WebSocket error: %s", exc)
        await log_interaction(session_id, "server", "ws_error", str(exc))
        try:
            await websocket.send_json({"event": "error", "message": str(exc)})
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        esp32_clients.discard(session_id)
        await broadcast_browsers({
            "event": "esp32_disconnected",
            "session_id": session_id,
            "count": len(esp32_clients),
        })
        logger.info("ESP32 cleanup done. Remaining=%d", len(esp32_clients))
