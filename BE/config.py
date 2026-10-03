import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

# Load .env file from the BE directory
BE_DIR = Path(__file__).resolve().parent
load_dotenv(BE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    # Server configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Storage
    DB_PATH: Path = BE_DIR / "chat_history.db"
    STATIC_DIR: Path = BE_DIR / "static"

    # Audio configurations
    SAMPLE_RATE: int = 16000
    CHANNELS: int = 1
    SAMPLE_WIDTH: int = 2  # 16-bit PCM = 2 bytes
    TTS_CHUNK_SIZE: int = 1024
    SERVER_AUDIO_PLAYBACK: bool = (
        os.getenv("SERVER_AUDIO_PLAYBACK", "false").lower() in ("true", "1", "yes")
    )

    # Whisper STT configuration
    # Options: tiny, base, small, medium, large-v3
    # "base" provides excellent Vietnamese accuracy with fast inference
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base")
    WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "vi")

    # LLM configuration
    # Options: "auto", "gemini", "ollama"
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "auto").lower()
    
    # Google Gemini
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    # Ollama
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "mistral")
    OLLAMA_TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "15.0"))

    # Pipeline optimization
    ENABLE_LLM_NORMALIZATION: bool = (
        os.getenv("ENABLE_LLM_NORMALIZATION", "false").lower() in ("true", "1", "yes")
    )

    # Edge TTS configuration
    # Recommended Vietnamese voices: "vi-VN-HoaiMyNeural" (female), "vi-VN-NamMinhNeural" (male)
    TTS_VOICE: str = os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural")


settings = Settings()
