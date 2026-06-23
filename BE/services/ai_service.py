import os
import re
import tempfile
import asyncio
import httpx
import whisper
import edge_tts
from dotenv import load_dotenv

load_dotenv()

model = whisper.load_model("large-v3")


class AIService:
    def __init__(self):
        self.ollama_url = "http://localhost:11434/api/generate"
        self.model_answer = "mistral"
        self.model_normalize = "mistral"

    # ================= RETRY =================
    async def _retry(self, func, retries=2):
        for i in range(retries):
            try:
                return await func()
            except Exception as e:
                if i == retries - 1:
                    raise e
                await asyncio.sleep(1)

    # ================= STT =================
    def transcribe_audio(self, wav_bytes: bytes) -> str:
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
                f.write(wav_bytes)
                temp_path = f.name

            result = model.transcribe(
                temp_path,
                language="vi",
                task="transcribe",
                temperature=0
            )

            text = result.get("text", "").strip()
            print("🎤 STT:", text)
            return text

        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    # ================= OLLAMA =================
    async def _call_ollama(self, prompt: str, model_name: str) -> str:
        payload = {
            "model": model_name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.2,
                "num_predict": 80,
            },
        }

        async def call():
            async with httpx.AsyncClient(timeout=60) as client:
                res = await client.post(self.ollama_url, json=payload)
                res.raise_for_status()
                return res.json()

        data = await self._retry(call)
        return data.get("response", "").strip()

    # ================= NORMALIZE =================
    async def normalize_with_llm(self, text: str) -> str:
        prompt = f"""
Sửa câu tiếng Việt từ STT.

- Thêm dấu
- Sửa từ sai theo phát âm
- Không trả lời
- Làm cho câu hỏi trở nên rõ ràng và dễ hiểu
- Đưa ra câu hỏi có thể trả lời được
- Chỉ trả về 1 câu

Input: {text}
Output:
"""
        result = await self._call_ollama(prompt, self.model_normalize)

        if len(result) > 100:
            return text

        print("🔧 Normalized:", result)
        return result

    # ================= INTENT DETECT =================
    def detect_math(self, text: str):
        text = text.lower()

        patterns = [
            (r"(\d+)\s*cộng\s*(\d+)", lambda a, b: int(a) + int(b)),
            (r"(\d+)\s*trừ\s*(\d+)", lambda a, b: int(a) - int(b)),
            (r"(\d+)\s*nhân\s*(\d+)", lambda a, b: int(a) * int(b)),
            (r"(\d+)\s*chia\s*(\d+)", lambda a, b: int(a) // int(b) if int(b) != 0 else "không thể chia"),
        ]

        for pattern, func in patterns:
            match = re.search(pattern, text)
            if match:
                a, b = match.groups()
                return str(func(a, b))

        return None

    # ================= ANSWER =================
    async def generate_text(self, text: str) -> str:
        if not text.strip():
            return "Tôi không nghe rõ."

        print("🧠 Raw:", text)

        normalized = await self.normalize_with_llm(text)

        math_result = self.detect_math(normalized)
        if math_result:
            print("🧮 Math detected:", math_result)
            return math_result

        prompt = f"""
Bạn là trợ lý tiếng Việt.

YÊU CẦU:
- Trả lời NGẮN (1 câu)
- PHẢI ĐÚNG SỰ THẬT

KHÔNG được bịa.

Câu hỏi: {normalized}
"""

        answer = await self._call_ollama(prompt, self.model_answer)

        print("🤖 Answer:", answer)
        return answer or "Tôi chưa hiểu."

    # ================= TTS =================
    async def text_to_speech(self, text: str) -> bytes:
        communicate = edge_tts.Communicate(text, "vi-VN-HoaiMyNeural")

        audio = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]

        return audio
