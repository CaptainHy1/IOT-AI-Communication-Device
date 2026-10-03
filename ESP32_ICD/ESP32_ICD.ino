// ============================================================================
//  PROJECT: INTELLIGENT COMMUNICATION DEVICE (ICD) - VOICE ASSISTANT FIRMWARE
//  Hardware: ESP32 DevKit V1 + INMP441 (Mic) + MAX98357A (Amp) + SSD1306 (OLED)
//  Protocol: WebSocket Client streaming PCM 16-bit 16kHz Mono to Backend Gateway
// ============================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoWebsockets.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "driver/i2s.h"

using namespace websockets;

// ────────────────────────────────────────────────────────────────────────────
//  1. CONFIGURATION (Cấu hình WiFi & Máy chủ Backend)
// ────────────────────────────────────────────────────────────────────────────
// Thay đổi thông tin mạng WiFi và địa chỉ IP máy chủ Backend của bạn
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";

// Địa chỉ WebSocket của Backend FastAPI (cùng mạng LAN với ESP32)
// Thay 192.168.1.x bằng IP máy tính chạy backend:
const char* WS_URL    = "ws://192.168.1.15:8000/ws/chat?type=esp32";

// ────────────────────────────────────────────────────────────────────────────
//  2. PIN MAPPING (Sơ đồ chân chuẩn xác)
// ────────────────────────────────────────────────────────────────────────────
// Nút bấm kích hoạt ghi âm (nối qua GND, dùng PULLUP nội)
#define PIN_BUTTON        4

// I2S Clock Bus dùng chung (Shared I2S Clock Bus)
#define I2S_WS            25   // LRC / WS
#define I2S_SCK           26   // BCLK / SCK

// I2S Data Pins
#define I2S_SD_MIC        33   // SD out của Microphone INMP441
#define I2S_DIN_AMP       27   // DIN in của Loa MAX98357A

// I2C OLED SSD1306 (128x64)
#define OLED_SDA          21
#define OLED_SCL          22
#define SCREEN_WIDTH      128
#define SCREEN_HEIGHT     64
#define OLED_I2C_ADDR     0x3C

// ────────────────────────────────────────────────────────────────────────────
//  3. AUDIO & TIMING CONSTANTS
// ────────────────────────────────────────────────────────────────────────────
#define SAMPLE_RATE       16000
#define RECORD_SECS       6
#define CHUNK_SAMPLES     512
#define TOTAL_BYTES       (SAMPLE_RATE * 2 * RECORD_SECS)
#define LEVEL_BAR_LEN     16
#define PEAK_MAX_LEVEL    8000

// ────────────────────────────────────────────────────────────────────────────
//  4. FINITE STATE MACHINE
// ────────────────────────────────────────────────────────────────────────────
enum DeviceState : uint8_t {
  STATE_CONNECTING,
  STATE_IDLE,
  STATE_RECORDING,
  STATE_SENDING,
  STATE_PROCESSING,
  STATE_SPEAKING,
  STATE_ERROR
};

volatile DeviceState g_state = STATE_CONNECTING;

// ────────────────────────────────────────────────────────────────────────────
//  5. GLOBAL OBJECTS & DRIVERS
// ────────────────────────────────────────────────────────────────────────────
WebsocketsClient ws;
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

bool oledAvailable = false;
bool wsConnected   = false;
bool i2sInstalled  = false;
uint32_t lastReconnectTime = 0;
uint32_t lastHeartbeatTime = 0;

// Button debounce helper
static bool readButtonRaw() {
  return digitalRead(PIN_BUTTON) == LOW;
}

static bool readButtonDebounced() {
  static bool stableState = false;
  static bool lastReading = false;
  static uint32_t lastChangeMs = 0;

  bool currentReading = readButtonRaw();
  if (currentReading != lastReading) {
    lastChangeMs = millis();
    lastReading = currentReading;
  }
  if ((millis() - lastChangeMs) >= 50) {
    stableState = currentReading;
  }
  return stableState;
}

// ────────────────────────────────────────────────────────────────────────────
//  6. OLED DISPLAY FUNCTIONS
// ────────────────────────────────────────────────────────────────────────────
static void initOLED() {
  Wire.begin(OLED_SDA, OLED_SCL);
  if (display.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR)) {
    oledAvailable = true;
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(10, 20);
    display.println("ICD VOICE ASSISTANT");
    display.setCursor(10, 36);
    display.println("Starting up...");
    display.display();
    Serial.println("[OLED] Initialized successfully (0x3C)");
  } else {
    oledAvailable = false;
    Serial.println("[OLED] Display not detected (optional, proceeding without it)");
  }
}

static void updateOLED(const char* title, const char* subtitle = "", int progressPercent = -1) {
  if (!oledAvailable) return;

  display.clearDisplay();
  display.drawRect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, SSD1306_WHITE);

  // Status Title
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(8, 8);
  display.print("ICD-ESP32 ");
  if (wsConnected) {
    display.print("[ONLINE]");
  } else {
    display.print("[OFFLINE]");
  }

  // Main Action Header
  display.setTextSize(2);
  display.setCursor(8, 24);
  display.println(title);

  // Subtitle / Info
  display.setTextSize(1);
  display.setCursor(8, 46);
  display.println(subtitle);

  // Progress Bar if applicable
  if (progressPercent >= 0) {
    int barWidth = map(constrain(progressPercent, 0, 100), 0, 100, 0, SCREEN_WIDTH - 20);
    display.drawRect(8, 56, SCREEN_WIDTH - 16, 5, SSD1306_WHITE);
    display.fillRect(8, 56, barWidth, 5, SSD1306_WHITE);
  }

  display.display();
}

static const char* stateLabel(DeviceState s) {
  switch (s) {
    case STATE_CONNECTING:  return "CONNECTING";
    case STATE_IDLE:        return "STANDBY";
    case STATE_RECORDING:   return "LISTENING";
    case STATE_SENDING:     return "SENDING";
    case STATE_PROCESSING:  return "THINKING";
    case STATE_SPEAKING:    return "SPEAKING";
    case STATE_ERROR:       return "ERROR";
    default:                return "UNKNOWN";
  }
}

static void setState(DeviceState s) {
  if (g_state != s) {
    g_state = s;
    Serial.printf("[STATE] -> %s\n", stateLabel(s));

    switch (s) {
      case STATE_CONNECTING:
        updateOLED("KET NOI", "Dang tim server...");
        break;
      case STATE_IDLE:
        updateOLED("SAN SANG", "Bam nut de noi");
        break;
      case STATE_RECORDING:
        updateOLED("DANG NGHE", "Noi vao micro...", 0);
        break;
      case STATE_SENDING:
        updateOLED("DANG GUI", "Truyen audio...");
        break;
      case STATE_PROCESSING:
        updateOLED("SUY NGHI", "AI dang xu ly...");
        break;
      case STATE_SPEAKING:
        updateOLED("DANG NOI", "Phat loa...");
        break;
      case STATE_ERROR:
        updateOLED("LOI", "Kiem tra mang/server");
        break;
    }
  }
}

// ────────────────────────────────────────────────────────────────────────────
//  7. I2S DUPLEX DRIVER (Micro INMP441 RX + Amp MAX98357A TX)
// ────────────────────────────────────────────────────────────────────────────
static void initI2S() {
  if (i2sInstalled) return;

  // Duplex Mode: RX for Microphone, TX for Speaker with shared BCLK/WS
  i2s_config_t cfg = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_TX),
    .sample_rate          = SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT, // INMP441 outputs 32-bit slot
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 8,
    .dma_buf_len          = 256,
    .use_apll             = false,
    .tx_desc_auto_clear   = true,
    .fixed_mclk           = 0
  };

  i2s_pin_config_t pins = {
    .bck_io_num   = I2S_SCK,
    .ws_io_num    = I2S_WS,
    .data_out_num = I2S_DIN_AMP,
    .data_in_num  = I2S_SD_MIC
  };

  esp_err_t err = i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  if (err != ESP_OK) {
    Serial.printf("[I2S] Driver install error: %d\n", err);
    return;
  }

  err = i2s_set_pin(I2S_NUM_0, &pins);
  if (err != ESP_OK) {
    Serial.printf("[I2S] Set pin error: %d\n", err);
    return;
  }

  // Flush initial microphone noise
  int32_t flushBuffer[256];
  size_t bytesRead = 0;
  for (int i = 0; i < 6; i++) {
    i2s_read(I2S_NUM_0, flushBuffer, sizeof(flushBuffer), &bytesRead, 50 / portTICK_PERIOD_MS);
  }

  i2sInstalled = true;
  Serial.println("[I2S] Hardware audio driver ready (Duplex: RX Mic + TX Amp)");
}

// Read PCM samples from INMP441, convert 32-bit slot to 16-bit PCM Mono
static size_t readMicChunk(int16_t* out, size_t maxSamples, int16_t* outPeak) {
  int32_t raw32[CHUNK_SAMPLES];
  size_t batch = min(maxSamples, (size_t)CHUNK_SAMPLES);
  size_t bytesRead = 0;

  i2s_read(I2S_NUM_0, raw32, batch * sizeof(int32_t), &bytesRead, portMAX_DELAY);
  size_t samplesRead = bytesRead / sizeof(int32_t);

  int16_t peak = 0;
  for (size_t i = 0; i < samplesRead; i++) {
    // INMP441 puts 24-bit audio in top bits; shift right 14 bits for clean 16-bit audio
    int32_t sample = raw32[i] >> 14;
    if (sample > 32767)  sample = 32767;
    if (sample < -32768) sample = -32768;
    out[i] = (int16_t)sample;

    int16_t absSample = (int16_t)abs((int)sample);
    if (absSample > peak) peak = absSample;
  }

  if (outPeak) *outPeak = peak;
  return samplesRead * sizeof(int16_t);
}

// ────────────────────────────────────────────────────────────────────────────
//  8. WEBSOCKET EVENT HANDLING
// ────────────────────────────────────────────────────────────────────────────
static void onWsMessage(WebsocketsMessage msg) {
  // Binary audio frame from server (TTS speech to play on Speaker)
  if (msg.isBinary()) {
    setState(STATE_SPEAKING);
    size_t bytesWritten = 0;
    const char* rawBytes = msg.c_str();
    size_t length = msg.length();
    
    // Play raw PCM chunk through I2S MAX98357A
    i2s_write(I2S_NUM_0, rawBytes, length, &bytesWritten, portMAX_DELAY);
    return;
  }

  // JSON Control frame
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, msg.data());
  if (err) return;

  const char* event = doc["event"] | "";
  Serial.printf("[WS] Event: %s\n", event);

  if (strcmp(event, "processing") == 0) {
    setState(STATE_PROCESSING);
  } else if (strcmp(event, "transcript") == 0) {
    const char* text = doc["text"] | "";
    Serial.printf("[STT] \"%s\"\n", text);
    if (oledAvailable) updateOLED("STT", text);
  } else if (strcmp(event, "assistant_text") == 0) {
    const char* text = doc["text"] | "";
    Serial.printf("[AI] \"%s\"\n", text);
    if (oledAvailable) updateOLED("TRA LOI", text);
  } else if (strcmp(event, "tts_start") == 0) {
    setState(STATE_SPEAKING);
  } else if (strcmp(event, "tts_end") == 0) {
    setState(STATE_IDLE);
  } else if (strcmp(event, "stt_empty") == 0) {
    Serial.println("[STT] Empty / No speech detected");
    setState(STATE_IDLE);
  } else if (strcmp(event, "error") == 0) {
    Serial.printf("[SERVER ERROR] %s\n", doc["message"] | "");
    setState(STATE_ERROR);
    delay(1500);
    setState(STATE_IDLE);
  }
}

static bool connectWS() {
  Serial.printf("[WS] Connecting to %s ...\n", WS_URL);
  ws.onMessage(onWsMessage);
  ws.onEvent([](WebsocketsEvent ev, String) {
    if (ev == WebsocketsEvent::ConnectionClosed) {
      wsConnected = false;
      Serial.println("[WS] Disconnected from server");
      setState(STATE_CONNECTING);
    }
  });

  if (ws.connect(WS_URL)) {
    Serial.println("[WS] Connected successfully!");
    wsConnected = true;
    setState(STATE_IDLE);
    return true;
  }

  Serial.println("[WS] Connection failed");
  wsConnected = false;
  setState(STATE_ERROR);
  return false;
}

// ────────────────────────────────────────────────────────────────────────────
//  9. WIFI INITIALIZATION & MANAGEMENT
// ────────────────────────────────────────────────────────────────────────────
static void connectWiFi() {
  Serial.printf("[WiFi] Connecting to SSID: %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  setState(STATE_CONNECTING);

  uint32_t startMs = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(200);
    Serial.print(".");
    if (millis() - startMs > 15000) {
      Serial.println("\n[WiFi] Connection timeout. Restarting...");
      ESP.restart();
    }
  }

  Serial.println("\n[WiFi] Connected!");
  Serial.printf("[WiFi] Assigned IP: %s\n", WiFi.localIP().toString().c_str());
  if (oledAvailable) {
    char ipBuf[32];
    snprintf(ipBuf, sizeof(ipBuf), "IP: %s", WiFi.localIP().toString().c_str());
    updateOLED("WIFI OK", ipBuf);
  }
}

// ────────────────────────────────────────────────────────────────────────────
//  10. AUDIO RECORDING & STREAMING
// ────────────────────────────────────────────────────────────────────────────
static void recordAndSendVoice() {
  Serial.println("[REC] Button pressed! Recording voice stream...");
  initI2S();

  // Reset backend buffer
  ws.send("{\"event\":\"reset\"}");
  ws.poll();

  setState(STATE_RECORDING);

  int16_t chunkBuffer[CHUNK_SAMPLES];
  size_t totalBytesSent = 0;
  uint32_t lastDisplayUpdateMs = 0;

  while (totalBytesSent < TOTAL_BYTES) {
    int16_t peak = 0;
    size_t samplesGot = readMicChunk(chunkBuffer, CHUNK_SAMPLES, &peak);

    if (samplesGot > 0) {
      size_t bytesToSend = min(samplesGot, (size_t)(TOTAL_BYTES - totalBytesSent));
      ws.sendBinary((const char*)chunkBuffer, bytesToSend);
      totalBytesSent += bytesToSend;

      // Update OLED progress & level bar
      uint32_t now = millis();
      if (now - lastDisplayUpdateMs >= 150) {
        lastDisplayUpdateMs = now;
        int percent = (int)((totalBytesSent * 100UL) / TOTAL_BYTES);
        updateOLED("DANG NGHE", "Noi vao micro...", percent);
      }
    }
    ws.poll();
  }

  Serial.printf("[REC] Finished recording %u bytes. Finalizing...\n", totalBytesSent);
  setState(STATE_SENDING);

  // Send end-of-audio frame
  ws.send("{\"event\":\"end\"}");
  ws.poll();

  setState(STATE_PROCESSING);
}

// ────────────────────────────────────────────────────────────────────────────
//  11. SETUP
// ────────────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n==========================================");
  Serial.println("  ICD VOICE ASSISTANT - ESP32 FIRMWARE    ");
  Serial.println("==========================================");

  pinMode(PIN_BUTTON, INPUT_PULLUP);
  Serial.printf("[BTN] Push Button on GPIO %d (Active LOW)\n", PIN_BUTTON);

  initOLED();
  initI2S();
  connectWiFi();
  connectWS();

  Serial.println("[SYSTEM] Ready! Press button to speak.");
}

// ────────────────────────────────────────────────────────────────────────────
//  12. MAIN LOOP
// ────────────────────────────────────────────────────────────────────────────
void loop() {
  if (wsConnected) {
    ws.poll();
  }

  // Auto-reconnect WebSocket if disconnected
  if (!wsConnected && WiFi.status() == WL_CONNECTED) {
    uint32_t now = millis();
    if (now - lastReconnectTime > 5000) {
      lastReconnectTime = now;
      Serial.println("[WS] Attempting to reconnect...");
      ws.close();
      connectWS();
    }
  }

  // Ping server heartbeat every 15 seconds
  if (wsConnected && (millis() - lastHeartbeatTime > 15000)) {
    lastHeartbeatTime = millis();
    ws.send("{\"event\":\"ping\"}");
  }

  // Handle Button Press
  static bool prevPressed = false;
  bool isPressed = readButtonDebounced();

  if (isPressed && !prevPressed) {
    if (!wsConnected) {
      Serial.println("[BTN] Cannot record: Server offline.");
      if (oledAvailable) updateOLED("MAT KET NOI", "Chua noi server");
    } else if (g_state == STATE_IDLE) {
      recordAndSendVoice();
    } else {
      Serial.printf("[BTN] Device busy (%s), please wait.\n", stateLabel(g_state));
    }
  }

  prevPressed = isPressed;
  delay(10);
}
