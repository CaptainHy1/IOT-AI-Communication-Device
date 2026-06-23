import io
import wave
import numpy as np


class AudioProcessor:
    def __init__(self, sample_rate=16000, channels=1, sample_width=2):
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width

    # ================= VALIDATE =================
    def _validate_audio(self, pcm_data: bytes):
        if not pcm_data:
            raise ValueError("No PCM audio data received.")

        if len(pcm_data) % self.sample_width != 0:
            raise ValueError("PCM not aligned.")

        duration = len(pcm_data) / (self.sample_rate * self.sample_width)

        if duration < 0.3:
            raise ValueError("Audio too short.")
        if duration > 10:
            raise ValueError("Audio too long.")

    # ================= REMOVE SILENCE =================
    def _remove_silence(self, pcm_data: bytes) -> bytes:
        samples = np.frombuffer(pcm_data, dtype=np.int16)

        threshold = 500  # ngưỡng phát hiện giọng nói
        mask = np.abs(samples) > threshold

        if not np.any(mask):
            return pcm_data

        idx = np.where(mask)[0]

        start = max(idx[0] - 800, 0)
        end = min(idx[-1] + 800, len(samples))

        return samples[start:end].astype(np.int16).tobytes()

    # ================= DENOISE =================
    def _simple_denoise(self, pcm_data: bytes) -> bytes:
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)

        noise_threshold = 300
        samples[np.abs(samples) < noise_threshold] = 0

        return samples.astype(np.int16).tobytes()

    # ================= NORMALIZE =================
    def _normalize_audio(self, pcm_data: bytes) -> bytes:
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)

        max_val = np.max(np.abs(samples)) + 1e-6
        samples = samples / max_val

        # boost nhẹ để dễ nghe hơn
        samples = samples * 1.2
        samples = np.clip(samples, -1.0, 1.0)

        samples = (samples * 32767).astype(np.int16)
        return samples.tobytes()

    # ================= SILENCE CHECK =================
    def _is_silent(self, pcm_data: bytes) -> bool:
        samples = np.frombuffer(pcm_data, dtype=np.int16)
        return np.abs(samples).mean() < 200

    # ================= MAIN =================
    def pcm16_to_wav_bytes(self, pcm_data: bytes) -> bytes:
        self._validate_audio(pcm_data)

        # 👉 pipeline xử lý
        pcm_data = self._remove_silence(pcm_data)
        pcm_data = self._simple_denoise(pcm_data)

        if self._is_silent(pcm_data):
            raise ValueError("Audio too silent after processing.")

        pcm_data = self._normalize_audio(pcm_data)

        # 👉 convert sang WAV
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(self.sample_width)
            wf.setframerate(self.sample_rate)
            wf.writeframes(pcm_data)

        buffer.seek(0)
        return buffer.read()