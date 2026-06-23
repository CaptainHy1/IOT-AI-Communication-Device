# PROJECT: INTELLIGENT COMMUNICATION DEVICE (ICD)
# HARDWARE CONFIGURATION (DO NOT CHANGE PINS)
- MCU: ESP32 Dev Module
- I2S Mic (INMP441): WS=GPIO25, SCK=GPIO26, SD=GPIO33
- I2S Amp (MAX98357A): LRC=GPIO25, BCLK=GPIO26, DIN=GPIO22
- Note: GPIO25/26 are shared I2S clocks (normal bus behavior, not a pin conflict)
- OLED (I2C): SCL=GPIO22, SDA=GPIO21
- Power: 5V via MT3608 Boost

# SOFTWARE ARCHITECTURE
- Backend: FastAPI (Python) + WebSockets + OpenAI (Whisper, GPT-4o, TTS-1)
- Firmware: Arduino/C++ (ESP32) + WebSockets Client + I2S Audio Driver