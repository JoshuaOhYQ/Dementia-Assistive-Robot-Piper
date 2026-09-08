/*
 * PIPER — Dementia Assistive Robot
 * Smart Home Node (house01)
 *
 * Runs on the "smart house" ESP32. Subscribes to piper/cmd/# and drives the
 * appliance outputs. Publishes retained state on piper/state/# so the robot's
 * Python backend always knows the true state of the house.
 *
 * Implements MQTT contract v1.0 (see docs/MQTT_CONTRACT.md).
 *
 * Libraries (Arduino IDE -> Library Manager):
 *   PubSubClient  by Nick O'Leary   >= 2.8
 *   ArduinoJson   by Benoit Blanchon >= 7.0
 *
 * Board: "ESP32 Dev Module"
 *
 * ---------------------------------------------------------------------------
 * WIRING (all outputs active-HIGH; an LED + 220R to GND is enough to demo)
 *   GPIO 26 -> living room light   (LED / relay IN1)
 *   GPIO 27 -> bedroom light       (LED / relay IN2)
 *   GPIO 25 -> fan                 (LED via PWM / motor driver)
 *   GPIO 33 -> stove indicator     (RED LED / relay IN3)
 *   GPIO 14 <- occupancy input     (PIR OUT, or a pushbutton to 3V3 for testing)
 *   GPIO  2 -> onboard LED, used as the "MQTT connected" heartbeat
 * ---------------------------------------------------------------------------
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ===========================================================================
// 1. CONFIGURATION  — the only block you need to edit
// ===========================================================================

// --- Choose ONE build target -----------------------------------------------
// 0 = real hardware on your home Wi-Fi, talking to Mosquitto on your PC
// 1 = Wokwi simulator, talking to a public test broker
#define BUILD_FOR_WOKWI 0

#if BUILD_FOR_WOKWI
  const char *WIFI_SSID     = "Wokwi-GUEST";
  const char *WIFI_PASS     = "";
  const char *MQTT_HOST     = "broker.emqx.io";   // public, unencrypted, demo only
  const uint16_t MQTT_PORT  = 1883;
#else
  const char *WIFI_SSID     = "YOUR_WIFI_SSID";
  const char *WIFI_PASS     = "YOUR_WIFI_PASSWORD";
  const char *MQTT_HOST     = "192.168.1.100";    // <-- your PC's LAN IP (ipconfig)
  const uint16_t MQTT_PORT  = 1883;
#endif

// If you run more than one copy of this node (real + Wokwi) at the same time on
// a PUBLIC broker, give each a different NODE_ID and TOPIC_ROOT so you do not
// collide with other students on the internet.
const char *NODE_ID    = "house01";
const char *TOPIC_ROOT = "piper";      // e.g. "piper-g2-24013567" on a public broker

// Broker credentials — leave empty for an open local Mosquitto
const char *MQTT_USER = "";
const char *MQTT_PASS = "";

// ===========================================================================
// 2. DEVICE TABLE
// ===========================================================================

enum DevType { SWITCH_DEV, DIMMABLE_DEV };

struct Device {
  const char *id;
  uint8_t     pin;
  DevType     type;
  bool        allowOn;      // false => the AI may only ever turn this OFF
  bool        state;        // current on/off
  uint8_t     value;        // 0-100, dimmables only
  char        reqId[12];
  char        source[10];
};

Device devices[] = {
  { "living_light",  26, SWITCH_DEV,   true,  false, 0, "boot", "boot" },
  { "bedroom_light", 27, SWITCH_DEV,   true,  false, 0, "boot", "boot" },
  { "fan",           25, DIMMABLE_DEV, true,  false, 0, "boot", "boot" },
  { "stove",         33, SWITCH_DEV,   false, false, 0, "boot", "boot" },  // safety: off-only
};
const uint8_t DEVICE_COUNT = sizeof(devices) / sizeof(devices[0]);

const uint8_t PIN_OCCUPANCY = 14;
const uint8_t PIN_HEARTBEAT = 2;

// LEDC (PWM) settings for the fan
const uint8_t FAN_CHANNEL   = 0;
const uint32_t FAN_FREQ_HZ  = 5000;
const uint8_t FAN_RES_BITS  = 8;      // 0-255

// ===========================================================================
// 3. GLOBALS
// ===========================================================================

WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);

char topicCmdWildcard[64];
char topicAvailability[64];

bool     lastOccupancy      = false;
uint32_t lastOccupancyPub   = 0;
uint32_t stoveOnSince       = 0;      // millis() when the stove went on, 0 = off
uint32_t lastStoveTimerPub  = 0;
uint32_t mqttRetryAt        = 0;
uint16_t mqttBackoffMs      = 1000;   // grows to 30 s

// ===========================================================================
// 4. HELPERS
// ===========================================================================

Device *findDevice(const char *id) {
  for (uint8_t i = 0; i < DEVICE_COUNT; i++) {
    if (strcmp(devices[i].id, id) == 0) return &devices[i];
  }
  return nullptr;
}

/* The LEDC (PWM) API changed between ESP32 Arduino core 2.x and 3.x.
   These two wrappers let the same sketch compile on either. */
inline void fanPwmInit(uint8_t pin) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(pin, FAN_FREQ_HZ, FAN_RES_BITS);
#else
  ledcSetup(FAN_CHANNEL, FAN_FREQ_HZ, FAN_RES_BITS);
  ledcAttachPin(pin, FAN_CHANNEL);
#endif
}

inline void fanPwmWrite(uint8_t pin, uint8_t duty) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(pin, duty);
#else
  (void)pin;
  ledcWrite(FAN_CHANNEL, duty);
#endif
}

void applyOutput(Device *d) {
  if (d->type == DIMMABLE_DEV) {
    uint8_t duty = d->state ? map(d->value, 0, 100, 0, 255) : 0;
    fanPwmWrite(d->pin, duty);
  } else {
    digitalWrite(d->pin, d->state ? HIGH : LOW);
  }
}

/* Publish retained state for one device. This is the ONLY place state leaves
   the node, so the backend can never be told something the hardware is not
   actually doing. */
void publishState(Device *d) {
  char topic[80];
  snprintf(topic, sizeof(topic), "%s/state/%s", TOPIC_ROOT, d->id);

  JsonDocument doc;
  doc["state"]  = d->state ? "on" : "off";
  doc["value"]  = d->type == DIMMABLE_DEV ? d->value : (d->state ? 100 : 0);
  doc["req_id"] = d->reqId;
  doc["source"] = d->source;
  doc["ts"]     = millis() / 1000;     // uptime seconds; PC timestamps the diary

  char payload[192];
  size_t n = serializeJson(doc, payload, sizeof(payload));
  mqtt.publish(topic, (const uint8_t *)payload, n, true);   // retain = true

  Serial.printf("[STATE] %s -> %s\n", topic, payload);
}

void publishSensor(const char *sensorId, JsonDocument &doc) {
  char topic[80];
  snprintf(topic, sizeof(topic), "%s/sensor/%s", TOPIC_ROOT, sensorId);
  char payload[128];
  size_t n = serializeJson(doc, payload, sizeof(payload));
  mqtt.publish(topic, (const uint8_t *)payload, n, true);
}

void publishAllStates() {
  for (uint8_t i = 0; i < DEVICE_COUNT; i++) publishState(&devices[i]);
}

// ===========================================================================
// 5. COMMAND HANDLING
// ===========================================================================

void onMessage(char *topic, byte *payload, unsigned int len) {
  // topic looks like  piper/cmd/living_light
  const char *lastSlash = strrchr(topic, '/');
  if (!lastSlash) return;
  const char *deviceId = lastSlash + 1;

  Device *d = findDevice(deviceId);
  if (!d) {
    Serial.printf("[CMD ] unknown device '%s' — ignored\n", deviceId);
    return;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload, len);
  if (err) {
    Serial.printf("[CMD ] bad JSON for %s: %s\n", deviceId, err.c_str());
    return;
  }

  const char *action = doc["action"] | "";
  const char *reqId  = doc["req_id"] | "none";
  const char *source = doc["source"] | "unknown";

  bool newState = d->state;
  uint8_t newValue = d->value;

  if (strcmp(action, "on") == 0) {
    newState = true;
    if (d->type == DIMMABLE_DEV && newValue == 0) newValue = 100;
  } else if (strcmp(action, "off") == 0) {
    newState = false;
  } else if (strcmp(action, "toggle") == 0) {
    newState = !d->state;
    if (newState && d->type == DIMMABLE_DEV && newValue == 0) newValue = 100;
  } else if (strcmp(action, "set") == 0 && d->type == DIMMABLE_DEV) {
    int v = doc["value"] | 0;
    newValue = constrain(v, 0, 100);
    newState = newValue > 0;
  } else {
    Serial.printf("[CMD ] action '%s' not valid for %s\n", action, deviceId);
    return;
  }

  // SAFETY INTERLOCK — enforced on the device, not only in the backend.
  // Two independent layers must both agree before a heating element energises.
  if (newState && !d->allowOn) {
    Serial.printf("[CMD ] REFUSED: '%s' may not be switched ON remotely\n", deviceId);
    publishState(d);           // re-assert the real state so the backend is not confused
    return;
  }

  d->state = newState;
  d->value = newValue;
  strncpy(d->reqId,  reqId,  sizeof(d->reqId) - 1);
  strncpy(d->source, source, sizeof(d->source) - 1);
  d->reqId[sizeof(d->reqId) - 1] = '\0';
  d->source[sizeof(d->source) - 1] = '\0';

  applyOutput(d);
  publishState(d);

  if (strcmp(d->id, "stove") == 0) {
    stoveOnSince = d->state ? millis() : 0;
  }
}

// ===========================================================================
// 6. CONNECTION MANAGEMENT
// ===========================================================================

void connectWiFi() {
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
    Serial.println(" FAILED — will retry");
  }
}

/* Non-blocking reconnect with exponential backoff. A blocking while(!connected)
   loop would freeze the appliances during a broker outage — unacceptable in a
   safety-adjacent system. */
bool connectMQTT() {
  if (millis() < mqttRetryAt) return false;

  char clientId[40];
  snprintf(clientId, sizeof(clientId), "piper-%s-%04X", NODE_ID, (uint16_t)(ESP.getEfuseMac() & 0xFFFF));

  Serial.printf("[MQTT] connecting to %s:%u as %s ... ", MQTT_HOST, MQTT_PORT, clientId);

  bool ok = mqtt.connect(
      clientId,
      strlen(MQTT_USER) ? MQTT_USER : nullptr,
      strlen(MQTT_PASS) ? MQTT_PASS : nullptr,
      topicAvailability,   // will topic
      1,                   // will QoS
      true,                // will retain
      "offline");          // will payload

  if (ok) {
    Serial.println("connected");
    mqttBackoffMs = 1000;
    mqtt.publish(topicAvailability, "online", true);
    mqtt.subscribe(topicCmdWildcard, 1);
    Serial.printf("[MQTT] subscribed to %s\n", topicCmdWildcard);
    publishAllStates();          // re-assert truth after any outage
    digitalWrite(PIN_HEARTBEAT, HIGH);
  } else {
    Serial.printf("failed, rc=%d, retry in %u ms\n", mqtt.state(), mqttBackoffMs);
    mqttRetryAt = millis() + mqttBackoffMs;
    mqttBackoffMs = min<uint16_t>(mqttBackoffMs * 2, 30000);
    digitalWrite(PIN_HEARTBEAT, LOW);
  }
  return ok;
}

// ===========================================================================
// 7. SETUP / LOOP
// ===========================================================================

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== PIPER smart home node ===");

  pinMode(PIN_HEARTBEAT, OUTPUT);
  pinMode(PIN_OCCUPANCY, INPUT);          // use INPUT_PULLDOWN with a button

  for (uint8_t i = 0; i < DEVICE_COUNT; i++) {
    if (devices[i].type == DIMMABLE_DEV) {
      fanPwmInit(devices[i].pin);
      fanPwmWrite(devices[i].pin, 0);
    } else {
      pinMode(devices[i].pin, OUTPUT);
      digitalWrite(devices[i].pin, LOW);
    }
  }

  snprintf(topicCmdWildcard,  sizeof(topicCmdWildcard),  "%s/cmd/+", TOPIC_ROOT);
  snprintf(topicAvailability, sizeof(topicAvailability), "%s/availability/%s", TOPIC_ROOT, NODE_ID);

  connectWiFi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(15);          // broker declares us dead ~22 s after a drop
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    digitalWrite(PIN_HEARTBEAT, LOW);
    connectWiFi();
    return;
  }
  if (!mqtt.connected()) {
    connectMQTT();
    return;
  }
  mqtt.loop();

  uint32_t now = millis();

  // --- occupancy: publish on change, and a keep-alive every 30 s -----------
  bool occ = digitalRead(PIN_OCCUPANCY);
  if (occ != lastOccupancy || now - lastOccupancyPub > 30000) {
    lastOccupancy = occ;
    lastOccupancyPub = now;
    JsonDocument doc;
    doc["value"] = occ;
    doc["ts"] = now / 1000;
    publishSensor("living_occupancy", doc);
    Serial.printf("[SENS] occupancy = %s\n", occ ? "true" : "false");
  }

  // --- stove-on timer: the backend's unattended-stove rule feeds on this ---
  if (now - lastStoveTimerPub > 10000) {
    lastStoveTimerPub = now;
    JsonDocument doc;
    doc["value"] = stoveOnSince ? (now - stoveOnSince) / 1000 : 0;
    doc["ts"] = now / 1000;
    publishSensor("stove_on_seconds", doc);
  }
}
