# BE_test

Mục tiêu: clone luồng xử lý của `BE/` nhưng có UI web 1 nút để test trực tiếp trên laptop:

- Bấm nút → thu mic (PCM16 mono 16kHz)
- Gửi qua WebSocket `/ws/chat`
- Backend chạy STT → LLM → TTS (giống BE)
- Trả WAV về browser để phát ra loa laptop

## Chạy

1) Tạo env

- Copy `.env.example` → `.env` và điền key.

2) Cài deps

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

3) Run server

```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8001
```

4) Mở trình duyệt

- `http://127.0.0.1:8001/`
- Bấm **Connect**
- Bấm **Bắt đầu nói** → nói → bấm **Dừng và gửi**

