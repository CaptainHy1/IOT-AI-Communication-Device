#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define SCREEN_WIDTH 128   // OLED 1.3 inch ngang
#define SCREEN_HEIGHT 64   // OLED 1.3 inch dọc
#define OLED_SDA  21
#define OLED_SCL  22

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

void setup() {
  Serial.begin(115200);
  Wire.begin(OLED_SDA, OLED_SCL);

  // Địa chỉ I2C thường là 0x3C, nếu không chạy thử đổi thành 0x3D
  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("Không tìm thấy OLED!");
    while (true);
  }

  Serial.println("OLED 1.3 inch OK!");
  display.clearDisplay();
  display.display();
}

void loop() {
  // Test 1: Toàn màn hình trắng
  display.clearDisplay();
  display.fillScreen(SSD1306_WHITE);
  display.display();
  delay(1000);

  // Test 2: Hiển thị chữ
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(10, 10);
  display.print("OLED 1.3\"");
  display.setTextSize(1);
  display.setCursor(10, 40);
  display.print("Test hien thi");
  display.display();
  delay(2000);

  // Test 3: Hình cơ bản
  display.clearDisplay();
  display.drawRect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, SSD1306_WHITE);
  display.drawLine(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, SSD1306_WHITE);
  display.drawCircle(SCREEN_WIDTH/2, SCREEN_HEIGHT/2, 20, SSD1306_WHITE);
  display.display();
  delay(2000);

  // Test 4: Animation cột nhấp nháy
  for (int i = 0; i < 50; i++) {
    display.clearDisplay();
    for (int j = 0; j < 8; j++) {
      int h = 8 + ((millis() / 80 + j * 37) % 40);
      display.fillRect(8 + j * 14, SCREEN_HEIGHT - h, 10, h, SSD1306_WHITE);
    }
    display.display();
    delay(50);
  }
}
