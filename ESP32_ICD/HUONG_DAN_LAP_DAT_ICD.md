# Hướng dẫn lắp đặt hệ thống ICD Voice (ESP32)

Tài liệu này tổng hợp sơ đồ đấu nối và các lưu ý quan trọng để lắp đặt hệ thống ICD Voice ổn định, dễ kiểm tra và hạn chế lỗi nguồn.

---

## 1) Danh sách linh kiện

### Core
- ESP32 Dev Module (USB-C hoặc Micro USB)

### Audio
- INMP441 (I2S Microphone)
- MAX98357A (I2S Amplifier - module hàn sẵn)
- Loa 4 Ohm 3W

### Display
- OLED 1.3 inch (SSD1306/SH1106 - I2C)

### Nguồn
- Pin 18650 x2 (mắc song song)
- Mạch sạc TP4056 (có bảo vệ)
- Module boost MT3608 (tăng lên 5V)
- Công tắc ON/OFF
- Tụ 470-1000uF (10-16V)
- Hộp pin 18650 (khuyến nghị)

### Điều khiển
- Nút nhấn 6x6mm (2-3 cái)

---

## 2) Sơ đồ nguồn (quan trọng nhất)

```text
Pin 18650 (2 viên song song)
        |
        v
TP4056 (B+ / B-)
        |
        v
OUT+ -> Công tắc -> MT3608 VIN+
OUT- --------------> MT3608 VIN-
        |
        v
MT3608 chỉnh 5V
        |
        v
VOUT+ -> VIN ESP32 + VIN MAX98357A
VOUT- -> GND chung
```

**Tụ lọc nguồn:**
- Chân `+` của tụ -> `VOUT+`
- Chân `-` của tụ -> `VOUT-`

> **Bắt buộc**
> - Chỉnh MT3608 đúng **5V** trước khi nối vào ESP32.
> - Tất cả thiết bị phải dùng **chung GND**.

---

## 3) Kết nối MIC (INMP441 - I2S)

> Lưu ý: `GPIO25` (WS/LRCLK) và `GPIO26` (SCK/BCLK) là 2 chân clock I2S dùng chung cho cả MIC và AMP.
> Đây là cấu hình bình thường của bus I2S, không phải xung đột chân.

| INMP441 | ESP32 |
|---|---|
| VDD | 3.3V |
| GND | GND |
| WS | GPIO25 |
| SCK | GPIO26 |
| SD | GPIO33 |
| L/R | GND |

---

## 4) Kết nối loa (MAX98357A)

| MAX98357A | ESP32 |
|---|---|
| VIN | 5V |
| GND | GND |
| LRC | GPIO25 |
| BCLK | GPIO26 |
| DIN | GPIO27 |

> LRC/BCLK của amp phải đi cùng bus clock với mic (`GPIO25/26`) để đồng bộ âm thanh.

**Đấu loa:**
- `SPK+` -> loa `+`
- `SPK-` -> loa `-`

---

## 5) Kết nối OLED (khớp với code hiện tại)

- OLED dùng bus I2C chuẩn của ESP32: `SDA=21`, `SCL=22`.
- Để tránh trùng chân, `DIN` của MAX98357A đã chuyển sang `GPIO27`.

| OLED | ESP32 |
|---|---|
| VCC | 3.3V |
| GND | GND |
| SDA | GPIO21 |
| SCL | GPIO22 |

---

## 6) Nút nhấn

| Nút | ESP32 |
|---|---|
| Button | GPIO0 |

- Dùng pull-up nội (`INPUT_PULLUP`).

---

## 7) Bảng tổng hợp chân kết nối

**Clock I2S dùng chung (không conflict):**
- `GPIO25` = WS/LRCLK cho cả Mic và Amp
- `GPIO26` = SCK/BCLK cho cả Mic và Amp

| Module | Chân ESP32 |
|---|---|
| Mic WS | GPIO25 |
| Mic SCK | GPIO26 |
| Mic SD | GPIO33 |
| Amp LRC | GPIO25 |
| Amp BCLK | GPIO26 |
| Amp DIN | GPIO27 |
| OLED SDA | GPIO21 |
| OLED SCL | GPIO22 |

---

## 8) Lưu ý quan trọng

1. **Không cấp pin trực tiếp vào ESP32**  
   Luôn đi qua MT3608 đã chỉnh 5V.

2. **Bắt buộc có tụ lọc**  
   Giảm sụt áp, tránh reset khi loa hoạt động.

3. **Nguồn cho loa phải đủ mạnh**  
   Nguồn yếu gây rè, méo hoặc tắt tiếng.

4. **GND phải nối chung toàn hệ thống**  
   Nếu không sẽ phát sinh lỗi ngẫu nhiên.

---

## 9) Luồng hoạt động

```text
Mic -> ESP32 -> WebSocket -> Server
                           |
                           v
                      STT -> LLM -> TTS
                           |
                           v
ESP32 <- audio stream <- Server
        |
        v
       Loa
```

---

## 10) Định dạng audio

**ESP32 gửi lên server:**
- PCM 16-bit
- 16kHz
- Mono

**Server trả về:**
- PCM 16kHz

---

## 11) Test sau khi lắp

**Serial monitor cần có:**
- `[WiFi] Connected: ...`
- `[WS] Connected`
- `[I2S] Ready`
- `[OLED] Found at 0x3C` hoặc `[OLED] Found at 0x3D`

**OLED trạng thái:**
- `CHO`
- `NGHE`
- `SUY NGHI`
- `DANG NOI`

**Test nhanh:**
- Giữ nút để gửi mic.
- Thả nút, server phản hồi.
- Loa phát âm thanh trả về.

---

## 12) Checklist hoàn thiện

- [ ] OLED hiển thị đúng trạng thái
- [ ] Mic gửi được dữ liệu
- [ ] Loa phát được âm thanh
- [ ] Không bị reset khi phát loa
- [ ] Chạy ổn định 20-30 phút

---

## Kết luận

Nếu đi dây đúng theo sơ đồ nguồn, dùng chung GND và tránh trùng chân I2C/I2S, hệ thống ICD Voice trên ESP32 sẽ chạy ổn định và dễ mở rộng thêm tính năng.
