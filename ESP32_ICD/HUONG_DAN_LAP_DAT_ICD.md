# Hướng Dẫn Lắp Đặt & Đấu Nối Phần Cứng ICD Voice (ESP32)

Tài liệu hướng dẫn chi tiết sơ đồ nguyên lý đấu nối, cấu hình phần cứng và quy trình lắp đặt cho hệ thống thiết bị giao tiếp thông minh **Intelligent Communication Device (ICD)**.

---

## 1. Danh Sách Linh Kiện Phần Cứng

| STT | Tên linh kiện | Thông số / Chức năng | Ghi chú |
|:---:|---|---|---|
| 1 | **ESP32 DevKit V1** | Vi điều khiển trung tâm (WiFi + BLE, 30 hoặc 38 chân) | Nguồn cấp 5V qua chân VIN |
| 2 | **INMP441** | Microphone kỹ thuật số chuẩn I2S MEMS | Chỉ dùng nguồn **3.3V** |
| 3 | **MAX98357A** | Mạch khuếch đại âm thanh số chuẩn I2S Class-D (3W) | Nguồn cấp 5V |
| 4 | **Loa toàn dải** | 4Ω 3W hoặc 8Ω 2W | Đấu vào SPK+ / SPK- của MAX98357A |
| 5 | **OLED 0.96" hoặc 1.3"** | Màn hình hiển thị I2C (SSD1306 / SH1106, 128x64) | Nguồn cấp 3.3V |
| 6 | **Nút nhấn (Tactile Push Button)** | 6x6x5mm 2 chân hoặc 4 chân | Nối chân GPIO4 xuống GND |
| 7 | **Module MT3608** | Mạch tăng áp DC-DC Boost (Step-up lên 5V ổn định) | **Chỉnh đúng 5.0V** trước khi cắm tải |
| 8 | **Module TP4056** | Mạch sạc pin Lithium 1S có bảo vệ ngắt xả quá áp | Cổng Type-C hoặc Micro USB |
| 9 | **Pin 18650** | 1 hoặc 2 cell mắc song song (3.7V - 4.2V) | Khuyến nghị dung lượng ≥ 2000mAh |
| 10 | **Tụ hóa (Electrolytic Capacitor)** | 470µF – 1000µF (10V - 16V) | **Bắt buộc** lọc nguồn chống sụt áp khi loa phát |
| 11 | **Công tắc gạt ON/OFF** | 2 chân / 3 chân | Lắp nối tiếp ngõ ra pin trước mạch Boost |

---

## 2. Sơ Đồ Khối Nguồn Điện

> [!CAUTION]
> **Quy tắc an toàn sống còn:**
> 1. Phải chỉnh biến trở trên mạch **MT3608 đạt đúng 5.0V** bằng đồng hồ VOM trước khi nối vào chân VIN của ESP32 và MAX98357A.
> 2. Toàn bộ thiết bị (ESP32, Mic, Loa, OLED, Nguồn) **BẮT BUỘC PHẢI DÙNG CHUNG GND**.

```text
  [Pin 18650 (3.7V)] 
          │
          ▼
   [TP4056 (B+ / B-)] ── (Cổng sạc Type-C)
          │
      (OUT+ / OUT-)
          │
    [Công tắc ON/OFF]
          │
          ▼
    [MT3608 VIN+ / VIN-]
          │ (Chỉnh biến trở)
          ▼
    [MT3608 VOUT = 5.0V] ──┬── [Tụ hóa 1000µF 16V lọc nguồn]
                           ├── ESP32 VIN
                           └── MAX98357A VIN (5V)
```

---

## 3. Sơ Đồ Đấu Nối Chi Tiết Từng Module

### 3.1. Microphone INMP441 (I2S RX)
| Chân INMP441 | Chân ESP32 | Ghi chú |
|---|---|---|
| **VDD** | **3.3V** | *Tuyệt đối không cắm 5V làm cháy mic* |
| **GND** | **GND** | GND chung hệ thống |
| **SD** | **GPIO 33** | Serial Data out |
| **WS** | **GPIO 25** | Word Select (Clock bus dùng chung) |
| **SCK** | **GPIO 26** | Serial Clock (Clock bus dùng chung) |
| **L/R** | **GND** | Kênh Left (chọn kênh âm thanh Mono) |

---

### 3.2. Mạch Khuếch Đại MAX98357A & Loa (I2S TX)
| Chân MAX98357A | Chân ESP32 / Nguồn | Ghi chú |
|---|---|---|
| **VIN** | **5V (từ MT3608)** | Cấp nguồn công suất cho loa |
| **GND** | **GND** | GND chung hệ thống |
| **DIN** | **GPIO 27** | Data in cho Amp |
| **LRC** | **GPIO 25** | Word Select (Clock bus dùng chung với Mic) |
| **BCLK** | **GPIO 26** | Bit Clock (Clock bus dùng chung với Mic) |
| **GAIN** | *Để trống hoặc GND* | Mặc định +9dB |
| **SD_MODE** | *Để trống* | Tự động kích hoạt |

> [!NOTE]
> `GPIO 25` (LRC/WS) và `GPIO 26` (BCLK/SCK) là 2 đường xung clock I2S dùng chung giữa Mic và Amp. Đây là thiết kế bus chuẩn trên vi điều khiển, giúp tiết kiệm chân GPIO và đồng bộ tần số lấy mẫu 16kHz.

---

### 3.3. Màn Hình OLED SSD1306 (I2C)
| Chân OLED | Chân ESP32 | Ghi chú |
|---|---|---|
| **VCC** | **3.3V** | Dùng nguồn 3.3V từ chân 3V3 của ESP32 |
| **GND** | **GND** | GND chung hệ thống |
| **SDA** | **GPIO 21** | I2C Data chuẩn ESP32 |
| **SCL** | **GPIO 22** | I2C Clock chuẩn ESP32 |

---

### 3.4. Nút Nhấn Ghi Âm (Push Button)
| Chân Nút Nhấn | Chân ESP32 | Ghi chú |
|---|---|---|
| Chân 1 | **GPIO 4** | Cấu hình `INPUT_PULLUP` nội trong code |
| Chân 2 | **GND** | Khi nhấn, chân GPIO4 kéo về mức LOW |

---

## 4. Bảng Tổng Hợp Chân Kết Nối (Pinout Summary)

| Thiết bị ngoại vi | Chân ESP32 | Giao tiếp | Điện áp hoạt động |
|---|---|---|---|
| **Mic WS** | `GPIO 25` | I2S Clock | 3.3V |
| **Mic SCK** | `GPIO 26` | I2S Clock | 3.3V |
| **Mic SD** | `GPIO 33` | I2S Data In | 3.3V |
| **Amp LRC** | `GPIO 25` | I2S Clock | 5.0V |
| **Amp BCLK** | `GPIO 26` | I2S Clock | 5.0V |
| **Amp DIN** | `GPIO 27` | I2S Data Out | 5.0V |
| **OLED SDA** | `GPIO 21` | I2C Data | 3.3V |
| **OLED SCL** | `GPIO 22` | I2C Clock | 3.3V |
| **Button** | `GPIO 4` | Digital Input | 3.3V (Pull-up) |

---

## 5. Quy Trình Kiểm Tra Sau Lắp Đặt

1. **Kiểm tra nguội (chưa cấp điện):**
   - Dùng đồng hồ đo thông mạch kiểm tra xem đường 5V và GND có bị chập không.
   - Kiểm tra đường 3.3V và GND có bị chập không.

2. **Cấp nguồn lần đầu:**
   - Bật công tắc nguồn. Đo điện áp ngõ ra của MT3608 đảm bảo đạt **5.0V ± 0.1V**.
   - Đo chân 3V3 của ESP32 đạt **3.3V**.

3. **Nạp Firmware & Mở Serial Monitor (Baudrate 115200):**
   - `[OLED] Initialized successfully (0x3C)`
   - `[I2S] Hardware audio driver ready`
   - `[WiFi] Connected! Assigned IP: 192.168.1.xxx`
   - `[WS] Connected successfully!`
   - `[SYSTEM] Ready! Press button to speak.`

4. **Kiểm tra tương tác:**
   - Nhấn nút: OLED hiển thị `DANG NGHE`, Serial hiển thị thanh level âm lượng micro.
   - Thả nút: Server nhận diện giọng nói (STT), hiển thị transcript và câu trả lời.
   - Loa phát âm thanh câu trả lời từ trợ lý ảo AI qua mạch MAX98357A mà không bị khởi động lại thiết bị (nhờ tụ lọc nguồn).
