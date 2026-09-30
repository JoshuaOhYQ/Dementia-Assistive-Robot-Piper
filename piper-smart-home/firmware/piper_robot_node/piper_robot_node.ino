/*
 * PIPER — Dementia Assistive Robot
 * Robot Node (robot01) — smart-home test firmware
 *
 * The robot ESP32's side of the conversation branch, without audio yet.
 * Instead of the INMP441 microphone + Whisper, you TYPE a sentence into the
 * Serial Monitor (or press the BOOT button for a scripted demo). The sentence is
 * sent to the Python backend over HTTP exactly as the Whisper transcript will
 * be later, and the backend's reply is printed instead of being spoken through
 * the MAX98357A.
 *
 *   robot01 --HTTP--> server.py --MQTT--> house01 (other ESP32) --> LEDs
 *
 * When the audio pipeline is ready, replace readSerialLine() with the audio
 * upload and printReply() with audio playback. The HTTP contract stays.
 *
 * Libraries: ArduinoJson >= 7.0   (HTTPClient and WiFi ship with the ESP32 core)
 * Board:     "ESP32 Dev Module"
 *
 * Pins used — deliberately NOT any of the pins the report assigns to the
 * LD2450 (16, 17, 21), the INMP441 (19, 4, 39) or the MAX98357A (23, 22, 15),
 * so this merges into the final robot firmware without rewiring:
 *   GPIO 0  <- onboard BOOT button (demo script, one phrase per press)
 *   GPIO 2  -> onboard LED (status)
 * Nothing needs to be wired. A bare board works.
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ===========================================================================
// 1. CONFIGURATION
// ===========================================================================

const char *WIFI_SSID    = "YOUR_WIFI_SSID";
const char *WIFI_PASS    = "YOUR_WIFI_PASSWORD";

// Your PC's LAN IPv4 — server.py prints the exact value when it starts.
const char *BACKEND_HOST = "192.168.1.100";
const uint16_t BACKEND_PORT = 5000;

const char *NODE_ID = "robot01";

// Each BOOT-button press sends the next line. Covers every behaviour worth
// showing in a demo video, including the two refusals.
const char *DEMO_SCRIPT[] = {
  "turn on the living room light",
  "set the fan to 60",
  "turn on the bedroom light",
  "turn on the stove",               // must be refused — safety interlock
  "open the garage door",            // must be refused — unknown device
  "turn off the living room light",
  "turn off the fan",
  "turn off the bedroom light",
};
const uint8_t DEMO_LEN = sizeof(DEMO_SCRIPT) / sizeof(DEMO_SCRIPT[0]);

const uint8_t PIN_BUTTON = 0;
const uint8_t PIN_LED    = 2;
const uint32_t HTTP_TIMEOUT_MS = 20000;   // live Gemini can take a few seconds

// ===========================================================================
// 2. STATE
// ===========================================================================

String   lineBuf;
uint8_t  demoIndex   = 0;
bool     lastButton  = HIGH;
uint32_t lastPressAt = 0;

// ===========================================================================
// 3. HELPERS
// ===========================================================================

String baseUrl() {
  return String("http://") + BACKEND_HOST + ":" + BACKEND_PORT;
}

void flash(uint8_t times, uint16_t onMs, uint16_t offMs) {
  for (uint8_t i = 0; i < times; i++) {
    digitalWrite(PIN_LED, HIGH); delay(onMs);
    digitalWrite(PIN_LED, LOW);  delay(offMs);
  }
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("[WIFI] connecting to %s ", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(300);
    Serial.print('.');
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf(" ok, IP %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println(" FAILED — check SSID/password, and that it is a 2.4 GHz network");
  }
}

/* GET a JSON endpoint and print it. Used for 'health' and 'state'. */
void getAndPrint(const char *path) {
  HTTPClient http;
  http.begin(baseUrl() + path);
  http.setTimeout(5000);
  int code = http.GET();
  if (code == 200) {
    Serial.printf("[HTTP] %s -> %s\n", path, http.getString().c_str());
  } else {
    Serial.printf("[HTTP] %s failed: %s\n", path,
                  code < 0 ? http.errorToString(code).c_str() : String(code).c_str());
    if (code < 0) {
      Serial.println("       Is server.py running? Is BACKEND_HOST your PC's LAN IP?");
      Serial.println("       Is port 5000 allowed through Windows Firewall?");
    }
  }
  http.end();
}

/* Report the true end-to-end time back to the server so it lands in test_log.csv. */
void reportRtt(const char *id, uint32_t rttMs) {
  HTTPClient http;
  http.begin(baseUrl() + "/api/rtt");
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(3000);
  JsonDocument doc;
  doc["id"] = id;
  doc["rtt_ms"] = rttMs;
  String body;
  serializeJson(doc, body);
  http.POST(body);
  http.end();
}

// ===========================================================================
// 4. THE CONVERSATION CALL
// ===========================================================================

void sendUtterance(const String &text) {
  if (WiFi.status() != WL_CONNECTED) {
    // Matches the report's error handling: no Wi-Fi -> tell the user locally.
    Serial.println("PIPER > I can't reach my brain right now. Please try again in a moment.");
    flash(3, 80, 80);
    return;
  }

  Serial.printf("\nYOU   > %s\n", text.c_str());

  JsonDocument req;
  req["text"] = text;
  req["source"] = NODE_ID;
  String body;
  serializeJson(req, body);

  HTTPClient http;
  http.begin(baseUrl() + "/api/utterance");
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(HTTP_TIMEOUT_MS);

  digitalWrite(PIN_LED, HIGH);                 // LED on = waiting for the brain
  uint32_t t0 = millis();
  int code = http.POST(body);
  uint32_t rtt = millis() - t0;
  digitalWrite(PIN_LED, LOW);

  if (code != 200) {
    Serial.printf("PIPER > (no reply — HTTP %s after %lu ms)\n",
                  code < 0 ? http.errorToString(code).c_str() : String(code).c_str(),
                  (unsigned long)rtt);
    http.end();
    flash(3, 80, 80);
    return;
  }

  String payload = http.getString();
  http.end();

  JsonDocument resp;
  if (deserializeJson(resp, payload)) {
    Serial.println("PIPER > (reply was not valid JSON)");
    flash(3, 80, 80);
    return;
  }

  // ---- what would be spoken through the speaker -------------------------
  Serial.printf("PIPER > %s\n", (const char *)(resp["speech"] | ""));

  // ---- what the system actually did -------------------------------------
  JsonArray accepted = resp["accepted"].as<JsonArray>();
  if (accepted.size()) {
    Serial.print("        did     : ");
    for (JsonObject c : accepted) {
      Serial.printf("%s %s", (const char *)(c["device"] | "?"), (const char *)(c["action"] | "?"));
      if (!c["value"].isNull()) Serial.printf(" %d", c["value"].as<int>());
      Serial.print("   ");
    }
    Serial.println();
  }
  JsonArray rejected = resp["rejected"].as<JsonArray>();
  for (JsonVariant r : rejected) {
    Serial.printf("        refused : %s\n", r.as<const char *>());
  }

  bool allOk = resp["all_ok"] | false;
  Serial.printf("        timing  : llm %.0f ms | mqtt round trip %.0f ms | backend %.0f ms | end-to-end %lu ms\n",
                (float)(resp["timing_ms"]["llm"] | 0.0),
                (float)(resp["timing_ms"]["mqtt_roundtrip"] | 0.0),
                (float)(resp["timing_ms"]["backend_total"] | 0.0),
                (unsigned long)rtt);

  const char *id = resp["id"] | "";
  if (strlen(id)) reportRtt(id, rtt);

  if (allOk) flash(1, 400, 50);                // one long flash = done
  else       flash(3, 80, 80);                 // three short = the house did not confirm
}

// ===========================================================================
// 5. INPUT
// ===========================================================================

/* Non-blocking line reader. Set the Serial Monitor to "Newline" (either
   ending works; empty lines are ignored). */
bool readSerialLine(String &out) {
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (lineBuf.length()) {
        out = lineBuf;
        lineBuf = "";
        out.trim();
        return out.length() > 0;
      }
    } else if (lineBuf.length() < 300) {
      lineBuf += ch;
    }
  }
  return false;
}

bool buttonPressed() {
  bool now = digitalRead(PIN_BUTTON);
  bool pressed = false;
  if (lastButton == HIGH && now == LOW && millis() - lastPressAt > 400) {
    pressed = true;
    lastPressAt = millis();
  }
  lastButton = now;
  return pressed;
}

// ===========================================================================
// 6. SETUP / LOOP
// ===========================================================================

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_BUTTON, INPUT_PULLUP);

  Serial.println("\n=== PIPER robot node (smart-home test) ===");
  connectWiFi();
  getAndPrint("/api/health");

  Serial.println();
  Serial.println("Type a sentence and press Enter, e.g.  turn on the living room light");
  Serial.println("Or press the BOOT button to step through the demo script.");
  Serial.println("Commands:  health   state   demo");
  Serial.println();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  String line;
  if (readSerialLine(line)) {
    if (line.equalsIgnoreCase("health")) {
      getAndPrint("/api/health");
    } else if (line.equalsIgnoreCase("state")) {
      getAndPrint("/api/state");
    } else if (line.equalsIgnoreCase("demo")) {
      for (uint8_t i = 0; i < DEMO_LEN; i++) {
        sendUtterance(DEMO_SCRIPT[i]);
        delay(1500);
      }
    } else {
      sendUtterance(line);
    }
  }

  if (buttonPressed()) {
    sendUtterance(DEMO_SCRIPT[demoIndex]);
    demoIndex = (demoIndex + 1) % DEMO_LEN;
  }
}
