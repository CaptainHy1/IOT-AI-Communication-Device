// ============================================================
//  ICD VOICE ASSISTANT – ESP32
//  Bấm nút → ghi 6 giây → gửi lên server
// ============================================================

#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoWebsockets.h>
#include <ArduinoJson.h>
#include "driver/i2s.h"

using namespace websockets;

// ─────────────────────────────────────────────
//  CONFIGURATION
// ─────────────────────────────────────────────
const char* WIFI_SSID = "Huyen";
const char* WIFI_PASS = "09101976";
const char* WS_URL    = "ws://192.168.1.51:8000/ws/chat?type=esp32";

// ─────────────────────────────────────────────
//  PIN MAP
// ─────────────────────────────────────────────
#define PIN_BUTTON   4    // GPIO4 – tránh GPIO0 (nút BOOT, hay lỗi)

#define I2S_WS       25
#define I2S_SCK      26
#define I2S_SD_MIC   33

// ─────────────────────────────────────────────
//  AUDIO SETTINGS
// ─────────────────────────────────────────────
#define SAMPLE_RATE   16000
#define RECORD_SECS   6
#define CHUNK_SAMPLES 512
#define TOTAL_BYTES   (SAMPLE_RATE * 2 * RECORD_SECS)
#define LEVEL_BAR_LEN 20
#define PEAK_FULL     8000   // mức peak coi là "đầy" thanh

// ─────────────────────────────────────────────
//  STATE MACHINE
// ─────────────────────────────────────────────
enum DeviceState : uint8_t {
  STATE_CONNECTING,
  STATE_IDLE,
  STATE_RECORDING,
  STATE_SENDING,
  STATE_PROCESSING,
  STATE_ERROR
};

volatile DeviceState g_state = STATE_CONNECTING;

// ─────────────────────────────────────────────
//  GLOBALS
// ─────────────────────────────────────────────
WebsocketsClient ws;
bool               wsConnected   = false;
uint32_t           lastReconnect = 0;
bool               i2sReady        = false;
uint32_t           lastBtnDbgMs    = 0;

static bool readButtonRaw() {
  return digitalRead(PIN_BUTTON) == LOW;
}

// Chống nhiễu nút bấm (~50ms)
static bool readButtonDebounced() {
  static bool stable = false;
  static bool lastReading = false;
  static uint32_t lastChangeMs = 0;

  bool reading = readButtonRaw();
  if (reading != lastReading) {
    lastChangeMs = millis();
    lastReading = reading;
  }
  if (millis() - lastChangeMs >= 50) {
    stable = reading;
  }
  return stable;
}

static const char* stateLabel(DeviceState s) {
  switch (s) {
    case STATE_CONNECTING:  return "CONNECTING";
    case STATE_IDLE:        return "IDLE";
    case STATE_RECORDING:   return "RECORDING";
    case STATE_SENDING:     return "SENDING";
    case STATE_PROCESSING:  return "PROCESSING";
    case STATE_ERROR:       return "ERROR";
    default:                return "UNKNOWN";
  }
}

static void setState(DeviceState s) {
  if (g_state != s) {
    g_state = s;
    Serial.printf("[STATE] %s\n", stateLabel(s));
  }
}

// ─────────────────────────────────────────────
//  I2S – INMP441 (32-bit frame → PCM16)
// ─────────────────────────────────────────────
static void initI2S() {
  i2s_config_t cfg = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate          = SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 8,
    .dma_buf_len          = 256,
    .use_apll             = false,
    .tx_desc_auto_clear   = false,
    .fixed_mclk           = 0
  };
  i2s_pin_config_t pins = {
    .bck_io_num   = I2S_SCK,
    .ws_io_num    = I2S_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num  = I2S_SD_MIC
  };
  ESP_ERROR_CHECK(i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL));
  ESP_ERROR_CHECK(i2s_set_pin(I2S_NUM_0, &pins));
}

static void ensureI2S() {
  if (i2sReady) return;
  initI2S();
  int32_t junk[256];
  size_t got = 0;
  for (int i = 0; i < 6; i++) {
    i2s_read(I2S_NUM_0, junk, sizeof(junk), &got, 100 / portTICK_PERIOD_MS);
  }
  i2sReady = true;
  Serial.println("[MIC] Ready");
}

static size_t readMicChunk(int16_t* out, size_t maxSamples, int16_t* outPeak, int32_t* outRms) {
  int32_t raw32[CHUNK_SAMPLES];
  size_t batch = min(maxSamples, (size_t)CHUNK_SAMPLES);
  size_t bytesRead = 0;

  i2s_read(I2S_NUM_0, raw32, batch * sizeof(int32_t), &bytesRead, portMAX_DELAY);
  size_t samplesRead = bytesRead / sizeof(int32_t);

  int16_t peak = 0;
  int64_t sumAbs = 0;

  for (size_t i = 0; i < samplesRead; i++) {
    int32_t s = raw32[i] >> 14;
    if (s > 32767)  s = 32767;
    if (s < -32768) s = -32768;
    out[i] = (int16_t)s;

    int16_t a = (int16_t)abs((int)s);
    if (a > peak) peak = a;
    sumAbs += a;
  }

  if (outPeak) *outPeak = peak;
  if (outRms)  *outRms  = samplesRead ? (int32_t)(sumAbs / samplesRead) : 0;

  return samplesRead * sizeof(int16_t);
}

static void printLevelBar(uint8_t pct, int16_t peak, int32_t rms) {
  uint8_t filled = map(constrain(peak, 0, PEAK_FULL), 0, PEAK_FULL, 0, LEVEL_BAR_LEN);

  Serial.printf("[REC %3u%%] [", pct);
  for (uint8_t i = 0; i < LEVEL_BAR_LEN; i++) {
    Serial.print(i < filled ? '|' : ' ');
  }
  Serial.printf("] peak=%5d avg=%4ld\n", peak, (long)rms);
}

// ─────────────────────────────────────────────
//  WiFi
// ─────────────────────────────────────────────
static void connectWiFi() {
  Serial.printf("[WiFi] Connecting to %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  setState(STATE_CONNECTING);

  uint32_t t = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(100);
    if (millis() - t > 15000) {
      Serial.println("[WiFi] Timeout – restarting");
      ESP.restart();
    }
  }
  Serial.printf("[WiFi] IP: %s\n", WiFi.localIP().toString().c_str());
}

// ─────────────────────────────────────────────
//  WebSocket
// ─────────────────────────────────────────────
static void onWsMessage(WebsocketsMessage msg) {
  if (msg.isBinary()) return;

  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, msg.data())) return;

  const char* event = doc["event"] | "";
  Serial.printf("[WS] Event: %s\n", event);

  if      (strcmp(event, "processing")     == 0) setState(STATE_PROCESSING);
  else if (strcmp(event, "transcript")     == 0) Serial.printf("[STT] %s\n", doc["text"] | "");
  else if (strcmp(event, "assistant_text") == 0) Serial.printf("[AI] %s\n", doc["text"] | "");
  else if (strcmp(event, "tts_start")      == 0) setState(STATE_PROCESSING);
  else if (strcmp(event, "tts_end")        == 0) setState(STATE_IDLE);
  else if (strcmp(event, "stt_empty")      == 0) setState(STATE_IDLE);
  else if (strcmp(event, "error")          == 0) {
    Serial.printf("[WS] Error: %s\n", doc["message"] | "");
    setState(STATE_IDLE);
  }
}

static bool connectWS() {
  Serial.printf("[WS] Connecting to %s\n", WS_URL);
  ws.onMessage(onWsMessage);
  ws.onEvent([](WebsocketsEvent ev, String) {
    if (ev == WebsocketsEvent::ConnectionClosed) {
      wsConnected = false;
      Serial.println("[WS] Connection closed by server");
    }
  });
  if (ws.connect(WS_URL)) {
    Serial.println("[WS] Connected");
    wsConnected = true;
    setState(STATE_IDLE);
    return true;
  }
  Serial.println("[WS] Failed");
  wsConnected = false;
  setState(STATE_ERROR);
  return false;
}

static void pollWebSocket() {
  if (!wsConnected) return;
  ws.poll();
}

// ─────────────────────────────────────────────
//  Bấm nút → ghi 6s → gửi (stream, không cần RAM lớn)
// ─────────────────────────────────────────────
static void recordAndSend() {
  Serial.println("[BTN] >>> Da nhan nut – bat dau ghi am!");
  Serial.flush();

  ensureI2S();

  ws.send("{\"event\":\"reset\"}");
  ws.poll();

  setState(STATE_RECORDING);
  Serial.printf("[REC] Ghi am %d giay – noi vao mic!\n", RECORD_SECS);
  Serial.println("[REC] Thanh | = am luong (peak), cang dai = cang to");

  int16_t chunk[CHUNK_SAMPLES];
  size_t sent = 0;
  uint32_t lastLevelMs = 0;
  int16_t sessionPeak = 0;

  while (sent < TOTAL_BYTES) {
    int16_t peak = 0;
    int32_t rms = 0;
    size_t got = readMicChunk(chunk, CHUNK_SAMPLES, &peak, &rms);
    if (got > 0) {
      size_t toSend = min(got, TOTAL_BYTES - sent);
      ws.sendBinary((const char*)chunk, toSend);
      sent += toSend;

      if (peak > sessionPeak) sessionPeak = peak;

      uint32_t now = millis();
      if (now - lastLevelMs >= 150) {
        lastLevelMs = now;
        uint8_t pct = (uint8_t)((sent * 100UL) / TOTAL_BYTES);
        printLevelBar(pct, peak, rms);
      }
    }
    ws.poll();
  }

  Serial.printf("[REC] Xong – peak max=%d\n", sessionPeak);

  setState(STATE_SENDING);
  ws.send("{\"event\":\"end\"}");
  ws.poll();
  Serial.printf("[SEND] Da gui %u bytes – cho server xu ly\n", sent);
  setState(STATE_PROCESSING);
}

// ─────────────────────────────────────────────
//  SETUP
// ─────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n[BOOT] ICD Voice – Mic only");

  pinMode(PIN_BUTTON, INPUT_PULLUP);
  Serial.printf("[BTN] GPIO%d – bam 1 lan de ghi %d giay\n", PIN_BUTTON, RECORD_SECS);
  Serial.printf("[BTN] Trang thai GPIO%d hien tai: %s\n",
                PIN_BUTTON, readButtonRaw() ? "LOW (dang nhan)" : "HIGH (chua nhan)");
  Serial.printf("[BTN] Day noi: GPIO%d -- nut -- GND\n", PIN_BUTTON);
  Serial.printf("[MEM] Free heap: %u KB\n", ESP.getFreeHeap() / 1024);

  connectWiFi();
  connectWS();

  Serial.println("[READY] Bam nut de bat dau ghi am");
}

// ─────────────────────────────────────────────
//  LOOP
// ─────────────────────────────────────────────
void loop() {
  pollWebSocket();

  if (!wsConnected) {
    uint32_t now = millis();
    if (now - lastReconnect > 5000) {
      lastReconnect = now;
      Serial.println("[WS] Reconnecting...");
      ws.close();
      connectWS();
    }
  }

  if (g_state == STATE_ERROR && wsConnected) {
    setState(STATE_IDLE);
  }

  static bool prevPressed = false;
  bool pressed = readButtonDebounced();

  // In trang thai nut moi 2 giay khi IDLE
  if (g_state == STATE_IDLE && millis() - lastBtnDbgMs > 2000) {
    lastBtnDbgMs = millis();
    Serial.printf("[BTN] GPIO%d=%s | state=IDLE | ws=%s\n",
                  PIN_BUTTON,
                  readButtonRaw() ? "LOW" : "HIGH",
                  wsConnected ? "OK" : "NO");
  }

  // Bat moi lan nhan / tha nut
  if (pressed != prevPressed) {
    Serial.printf("[BTN] %s\n", pressed ? "NUT NHAN (LOW)" : "NUT THA (HIGH)");
    Serial.flush();
  }

  if (pressed && !prevPressed) {
    if (!wsConnected) {
      Serial.println("[BTN] Chua ket noi server");
    } else if (g_state != STATE_IDLE) {
      Serial.printf("[BTN] Dang %s – vui long doi\n", stateLabel(g_state));
    } else {
      recordAndSend();
      delay(300);
    }
  }

  prevPressed = pressed;
  delay(10);
}
