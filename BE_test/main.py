import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from services.ai_service import AIService
from services.audio_processing import AudioProcessor

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("icd-backend-test")

app = FastAPI(title="ICD Backend (Test)", version="1.0.0")

audio_processor = AudioProcessor(sample_rate=16000, channels=1, sample_width=2)
ai_service = AIService()

DB_PATH = Path("chat_history_test.db")
STATIC_DIR = Path(__file__).parent / "static"


# ================= DB =================
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
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


# ================= STARTUP =================
@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    logger.info("Server started. DB at %s", DB_PATH.resolve())


# ================= STATIC =================
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ================= CORE =================
async def process_audio_message(session_id: str, pcm_bytes: bytes) -> dict[str, Any]:
    wav_bytes = await asyncio.to_thread(
        audio_processor.pcm16_to_wav_bytes, pcm_bytes
    )

    # ===== STT =====
    try:
        transcript = await asyncio.to_thread(
            ai_service.transcribe_audio, wav_bytes
        )
    except Exception as exc:
        return {
            "error": f"stt_error: {exc}",
            "transcript": "",
            "assistant_text": "",
            "assistant_audio": b"",
        }

    if not transcript:
        return {"transcript": "", "assistant_text": "", "assistant_audio": b""}

    await asyncio.to_thread(save_message, session_id, "user", transcript)

    # ===== LLM =====
    try:
        assistant_text = await ai_service.generate_text(transcript)
    except Exception as exc:
        return {
            "error": f"llm_error: {exc}",
            "transcript": transcript,
            "assistant_text": "",
            "assistant_audio": b"",
        }

    await asyncio.to_thread(save_message, session_id, "assistant", assistant_text)

    # ===== TTS =====
    try:
        assistant_audio = await ai_service.text_to_speech(assistant_text)
    except Exception as exc:
        return {
            "error": f"tts_error: {exc}",
            "transcript": transcript,
            "assistant_text": assistant_text,
            "assistant_audio": b"",
        }

    return {
        "transcript": transcript,
        "assistant_text": assistant_text,
        "assistant_audio": assistant_audio,
    }


# ================= HEALTH =================
@app.get("/health")
async def health_check():
    return {"status": "ok"}


# ================= WEBSOCKET =================
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    session_id = websocket.query_params.get("session_id", str(uuid.uuid4()))
    audio_buffer = bytearray()

    logger.info("Client connected: %s", session_id)

    try:
        while True:
            message = await websocket.receive()

            # ===== nhận audio =====
            if message.get("bytes") is not None:
                audio_buffer.extend(message["bytes"])
                continue

            if message.get("text") is None:
                continue

            try:
                payload = json.loads(message["text"])
            except:
                payload = {"event": message["text"]}

            event = (payload.get("event") or "").lower()

            # ===== ping =====
            if event == "ping":
                await websocket.send_json({"event": "pong"})
                continue

            # ===== reset =====
            if event == "reset":
                audio_buffer.clear()
                await websocket.send_json({"event": "buffer_reset"})
                continue

            # ===== kết thúc ghi âm =====
            if event in {"end", "stop_recording"}:
                if not audio_buffer:
                    await websocket.send_json(
                        {"event": "warning", "message": "Audio empty"}
                    )
                    continue

                await websocket.send_json({"event": "processing"})

                response = await process_audio_message(
                    session_id, bytes(audio_buffer)
                )
                audio_buffer.clear()

                if response.get("error"):
                    await websocket.send_json(
                        {"event": "error", "message": response["error"]}
                    )
                    continue

                await websocket.send_json(
                    {"event": "transcript", "text": response["transcript"]}
                )
                await websocket.send_json(
                    {"event": "assistant_text", "text": response["assistant_text"]}
                )

                # ===== gửi audio =====
                audio = response["assistant_audio"]
                await websocket.send_json({"event": "tts_start", "size": len(audio)})

                chunk_size = 2048
                for i in range(0, len(audio), chunk_size):
                    await websocket.send_bytes(audio[i : i + chunk_size])

                await websocket.send_json({"event": "tts_end"})

    except WebSocketDisconnect:
        logger.info("Client disconnected: %s", session_id)