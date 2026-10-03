import asyncio
import logging
import os
import re
import tempfile
from typing import Optional, Union
import httpx
import edge_tts
import whisper
import torch

from config import settings

logger = logging.getLogger("icd-backend")


class AIService:
    """
    Unified AI service providing:
    - Speech-to-Text via OpenAI Whisper (CPU/CUDA, lazy-loaded)
    - LLM Inference via Google Gemini or local Ollama
    - Intent detection (Math / Calculations)
    - Text-to-Speech via Microsoft Edge Neural TTS
    """

    def __init__(self):
        self._whisper_model: Optional[whisper.Whisper] = None
        self._whisper_lock = asyncio.Lock()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("AIService initialized. Execution device: %s", self.device)

    # ================= WHISPER STT =================
    def _get_whisper_model(self) -> whisper.Whisper:
        """
        Lazy-loads the Whisper model on first invocation to avoid blocking startup.
        """
        if self._whisper_model is None:
            logger.info(
                "Loading Whisper model '%s' on %s...",
                settings.WHISPER_MODEL,
                self.device,
            )
            self._whisper_model = whisper.load_model(
                settings.WHISPER_MODEL,
                device=self.device,
            )
            logger.info("Whisper model '%s' loaded successfully.", settings.WHISPER_MODEL)
        return self._whisper_model

    def warm_up_stt(self) -> None:
        """Pre-warms the Whisper model in background during startup."""
        try:
            self._get_whisper_model()
        except Exception as exc:
            logger.error("Failed to pre-warm Whisper model: %s", exc)

    def transcribe_audio(self, wav_bytes: bytes) -> str:
        """
        Transcribes WAV audio bytes to text using Whisper.
        """
        if not wav_bytes:
            return ""

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
                f.write(wav_bytes)
                temp_path = f.name

            model = self._get_whisper_model()
            result = model.transcribe(
                temp_path,
                language=settings.WHISPER_LANGUAGE,
                task="transcribe",
                temperature=0.0,
                fp16=(self.device == "cuda"),
            )

            text = result.get("text", "").strip()
            # Clean up common hallucination artifacts
            text = re.sub(r"\[.*?\]|\(.*?\)", "", text).strip()
            logger.info("STT Transcribed: '%s'", text)
            return text

        except Exception as exc:
            logger.exception("STT Transcription failed: %s", exc)
            raise
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    # ================= LLM INFERENCE =================
    async def _call_gemini(self, prompt: str) -> str:
        """
        Calls Google Gemini 1.5 Flash via official REST API.
        """
        if not settings.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not configured.")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
        
        system_instruction = (
            "Bạn là trợ lý thông minh tiếng Việt trên thiết bị giao tiếp phần cứng (ICD). "
            "Quy tắc trả lời: "
            "1. Trả lời thật ngắn gọn, súc tích (1-2 câu ngắn). "
            "2. Tuyệt đối chính xác, tự nhiên, thân thiện. "
            "3. Không sử dụng ký tự đặc biệt hay emoji vì câu trả lời sẽ được đọc trực tiếp qua loa phát thanh."
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": f"{system_instruction}\n\nNgười dùng: {prompt}\nTrợ lý:"}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 100,
            }
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, json=payload)
            res.raise_for_status()
            data = res.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "").strip()
            return ""

    async def _call_ollama(self, prompt: str, model_name: Optional[str] = None) -> str:
        """
        Calls local Ollama server with configurable timeout.
        """
        target_model = model_name or settings.OLLAMA_MODEL
        url = f"{settings.OLLAMA_BASE_URL}/api/generate"
        payload = {
            "model": target_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.3,
                "num_predict": 100,
            },
        }

        async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT) as client:
            res = await client.post(url, json=payload)
            res.raise_for_status()
            data = res.json()
            return data.get("response", "").strip()

    # ================= INTENT DETECTION =================
    def detect_math(self, text: str) -> Optional[str]:
        """
        Fast local math evaluation for basic arithmetic queries in Vietnamese.
        """
        cleaned = text.lower().strip().rstrip("?").rstrip(".")

        patterns = [
            (r"(\d+)\s*(?:cộng|\+)\s*(\d+)", lambda a, b: f"{a} cộng {b} bằng {int(a) + int(b)}"),
            (r"(\d+)\s*(?:trừ|\-)\s*(\d+)", lambda a, b: f"{a} trừ {b} bằng {int(a) - int(b)}"),
            (r"(\d+)\s*(?:nhân|x|\*)\s*(\d+)", lambda a, b: f"{a} nhân {b} bằng {int(a) * int(b)}"),
            (
                r"(\d+)\s*(?:chia|\/)\s*(\d+)",
                lambda a, b: f"{a} chia {b} bằng {int(a) // int(b)}"
                if int(b) != 0
                else "Không thể chia cho số không",
            ),
        ]

        for pattern, func in patterns:
            match = re.search(pattern, cleaned)
            if match:
                a, b = match.groups()
                return func(a, b)

        return None

    # ================= NORMALIZATION =================
    async def normalize_with_llm(self, text: str) -> str:
        """
        Optional grammar & tone normalization.
        """
        if not settings.ENABLE_LLM_NORMALIZATION or len(text.strip()) == 0:
            return text

        prompt = (
            f"Chuẩn hóa câu tiếng Việt sau từ nhận dạng giọng nói, sửa từ ngữ cho mạch lạc, không trả lời: {text}"
        )
        try:
            if settings.GEMINI_API_KEY:
                normalized = await self._call_gemini(prompt)
            else:
                normalized = await self._call_ollama(prompt)
            if normalized and len(normalized) < 150:
                return normalized
        except Exception:
            pass
        return text

    # ================= ANSWER GENERATION =================
    async def generate_text(self, text: str) -> str:
        """
        Generates an assistant response using detected intent or active LLM provider.
        """
        if not text.strip():
            return "Tôi không nghe rõ, bạn có thể nói lại được không?"

        # 1. Quick local math evaluation
        math_result = self.detect_math(text)
        if math_result:
            logger.info("Math intent detected: %s", math_result)
            return math_result

        # 2. Optional text normalization
        query = await self.normalize_with_llm(text)

        # 3. LLM routing
        provider = settings.LLM_PROVIDER
        prompt = (
            f"Bạn là trợ lý tiếng Việt trên thiết bị IoT. "
            f"Hãy trả lời câu hỏi sau thật ngắn gọn (1-2 câu), chính xác và lịch sự:\n{query}"
        )

        # Priority 1: Gemini if configured or auto
        if provider in ("gemini", "auto") and settings.GEMINI_API_KEY:
            try:
                answer = await self._call_gemini(prompt)
                if answer:
                    logger.info("Gemini Answer: %s", answer)
                    return answer
            except Exception as exc:
                logger.warning("Gemini invocation failed: %s. Falling back to Ollama.", exc)

        # Priority 2: Ollama
        if provider in ("ollama", "auto"):
            try:
                answer = await self._call_ollama(prompt)
                if answer:
                    logger.info("Ollama Answer: %s", answer)
                    return answer
            except Exception as exc:
                logger.warning("Ollama invocation failed: %s.", exc)

        # Graceful fallback message
        fallback = (
            f"Tôi đã nghe bạn nói: '{query}'. "
            "Hiện tại hệ thống AI chưa nhận được phản hồi từ mô hình ngôn ngữ."
        )
        logger.info("Fallback response: %s", fallback)
        return fallback

    # ================= TEXT TO SPEECH =================
    async def text_to_speech(self, text: str) -> bytes:
        """
        Synthesizes Vietnamese speech using Edge Neural TTS. Returns MP3 bytes.
        """
        if not text.strip():
            return b""

        # Clean text for speech synthesis
        clean_text = re.sub(r"[\*\#\_\`\~]", "", text).strip()
        logger.info("Generating TTS for: '%s' with voice '%s'", clean_text, settings.TTS_VOICE)

        communicate = edge_tts.Communicate(clean_text, settings.TTS_VOICE)
        audio_buffer = bytearray()

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_buffer.extend(chunk["data"])

        return bytes(audio_buffer)
