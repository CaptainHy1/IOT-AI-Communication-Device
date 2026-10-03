import io
import logging
import shutil
import subprocess
import wave
from typing import Optional
import numpy as np

logger = logging.getLogger("icd-backend")


class AudioProcessor:
    """
    Handles audio validation, noise reduction, silence trimming,
    format conversion, and normalization for 16-bit PCM and WAV streams.
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1, sample_width: int = 2):
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width  # 2 bytes = 16 bits
        self._ffmpeg_path: Optional[str] = shutil.which("ffmpeg")

    def validate_and_align_pcm(self, pcm_data: bytes) -> bytes:
        """
        Validates PCM audio. Truncates unaligned bytes, enforces minimum
        duration, and clamps excessive length safely without raising fatal errors.
        """
        if not pcm_data:
            raise ValueError("No PCM audio data received.")

        # Align byte buffer to 16-bit sample boundary (2 bytes per sample)
        remainder = len(pcm_data) % self.sample_width
        if remainder != 0:
            pcm_data = pcm_data[:-remainder]

        # Calculate duration
        duration = len(pcm_data) / (self.sample_rate * self.sample_width)

        if duration < 0.2:
            raise ValueError(f"Audio sample too short ({duration:.2f}s). Minimum 0.2s required.")

        # Clamp max duration to 30 seconds to prevent memory overflow
        max_duration = 30.0
        max_bytes = int(self.sample_rate * self.sample_width * max_duration)
        if len(pcm_data) > max_bytes:
            logger.warning(
                "Audio duration (%.2fs) exceeds maximum allowed (%.2fs). Clamping.",
                duration,
                max_duration,
            )
            pcm_data = pcm_data[:max_bytes]

        return pcm_data

    def remove_silence(self, samples: np.ndarray, threshold: int = 400) -> np.ndarray:
        """
        Removes leading and trailing silence based on amplitude threshold,
        preserving a short margin before and after speech.
        """
        if len(samples) == 0:
            return samples

        mask = np.abs(samples) > threshold
        if not np.any(mask):
            return samples  # Entire audio is quiet, retain as is

        indices = np.where(mask)[0]
        padding = int(self.sample_rate * 0.1)  # 100ms padding
        start = max(indices[0] - padding, 0)
        end = min(indices[-1] + padding, len(samples))

        return samples[start:end]

    def noise_gate(self, samples: np.ndarray, threshold: int = 250) -> np.ndarray:
        """
        Zeroes out background noise below the threshold.
        """
        if len(samples) == 0:
            return samples

        float_samples = samples.astype(np.float32)
        float_samples[np.abs(float_samples) < threshold] = 0
        return float_samples.astype(np.int16)

    def normalize_volume(self, samples: np.ndarray, target_peak_ratio: float = 0.95) -> np.ndarray:
        """
        Scales audio samples to maximize speech clarity while avoiding clipping.
        """
        if len(samples) == 0:
            return samples

        float_samples = samples.astype(np.float32)
        max_val = np.max(np.abs(float_samples))

        if max_val < 1.0:
            return samples

        scale = (32767.0 * target_peak_ratio) / max_val
        float_samples = float_samples * scale
        float_samples = np.clip(float_samples, -32768.0, 32767.0)

        return float_samples.astype(np.int16)

    def is_silent(self, samples: np.ndarray, threshold: float = 180.0) -> bool:
        """
        Checks whether the processed audio is effectively silent.
        """
        if len(samples) == 0:
            return True
        return float(np.mean(np.abs(samples))) < threshold

    def pcm16_to_wav_bytes(self, pcm_data: bytes) -> bytes:
        """
        Complete processing pipeline:
        1. Validate & align PCM buffer
        2. Convert to numpy array
        3. Apply noise reduction & silence trim
        4. Normalize speech volume
        5. Encode to standard 16kHz mono WAV format
        """
        pcm_data = self.validate_and_align_pcm(pcm_data)
        samples = np.frombuffer(pcm_data, dtype=np.int16)

        # Signal processing
        samples = self.remove_silence(samples, threshold=400)
        samples = self.noise_gate(samples, threshold=250)

        if self.is_silent(samples):
            raise ValueError("Audio is silent or volume is below recognition threshold.")

        samples = self.normalize_volume(samples)

        # Write to WAV container
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(self.sample_width)
            wf.setframerate(self.sample_rate)
            wf.writeframes(samples.tobytes())

        buffer.seek(0)
        return buffer.read()

    def mp3_to_pcm16(self, mp3_bytes: bytes) -> bytes:
        """
        Converts MP3 audio (e.g. from Edge-TTS) to raw 16-bit 16kHz mono PCM.
        This allows direct hardware streaming to I2S DACs (such as MAX98357A)
        without requiring client-side MP3 decoding libraries on ESP32.
        """
        if not mp3_bytes:
            return b""

        if not self._ffmpeg_path:
            logger.warning("ffmpeg not detected; returning raw audio bytes without PCM transcoding.")
            return mp3_bytes

        try:
            cmd = [
                self._ffmpeg_path,
                "-y",
                "-i", "pipe:0",
                "-f", "s16le",
                "-ar", str(self.sample_rate),
                "-ac", str(self.channels),
                "pipe:1"
            ]
            proc = subprocess.run(
                cmd,
                input=mp3_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True
            )
            return proc.stdout
        except Exception as exc:
            logger.error("Failed to transcode MP3 to PCM16: %s", exc)
            return b""
