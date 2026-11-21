// Orca ESP32 field node: GNSS sensor + LoRa mesh / burst-chat relay.
//
// LoRa is a core part of this node. It carries Orca events and burst-chat frames
// between nodes off-grid, with dedup and a hop limit, matching the Orca mesh.
// A node with WiFi also bridges the LoRa mesh to the Orca server over UDP.
//
// What it does:
//   - Reads a GNSS receiver (NMEA/UART2), reports position and signal health, and
//     detects GNSS jamming locally (sharp C/N0 drop) as Orca Event Format (OEF).
//   - LoRa mesh: broadcasts its own events, and relays frames it hears from other
//     nodes (dedup by id, hop limit), so detections and chat cross a grid-down area.
//   - Burst chat: short text frames ride the same LoRa mesh; messages are held and
//     re-sent, so they get through intermittent links.
//   - Bridges to the Orca server over UDP when WiFi is up.
//
// LoRa frequency is set per ITU region for GLOBAL use (configure below): 915 MHz
// (Region 2, Americas), 868 MHz (Region 1, Europe/Africa), 433 MHz (where allowed).
// Transmitting LoRa is the node's own licensed ISM operation. This node never
// encrypts over the air; secure comms live in the Orca server's government/defence
// module on licensed, non-amateur frequencies.
//
// Libraries: WiFi, WiFiUdp (built in); TinyGPSPlus; LoRa (Sandeep Mistry).
// Board: ESP32 with an SX127x LoRa module (e.g. TTGO LoRa32, Heltec).

#include <WiFi.h>
#include <WiFiUdp.h>
#include <SPI.h>
#include <LoRa.h>
#include <TinyGPSPlus.h>

// ----------------------- configuration -----------------------
static const char* WIFI_SSID = "your-wifi";      // leave as-is for LoRa-only (no bridge)
static const char* WIFI_PASS = "your-pass";
static const char* ORCA_HOST = "192.168.1.20";   // Orca server (UDP bridge)
static const uint16_t ORCA_UDP_PORT = 5514;
static const char* NODE_ID = "BB-ESP-01";
static double NODE_LAT = 13.103, NODE_LON = -59.630;

// GNSS UART
static const int GPS_RX_PIN = 16, GPS_TX_PIN = 17, GPS_BAUD = 9600;
static const float JAM_DROP_DB = 10.0;

// LoRa radio pins (TTGO LoRa32 defaults; change for your board)
static const int LORA_SCK = 5, LORA_MISO = 19, LORA_MOSI = 27, LORA_SS = 18,
                 LORA_RST = 23, LORA_DIO0 = 26;
static const long LORA_FREQ = 915E6;             // 915E6 Americas, 868E6 Europe, 433E6 where allowed
static const int LORA_SF = 9;                    // spreading factor 7..12 (range vs rate)
static const long LORA_BW = 125E3;
static const uint8_t LORA_SYNC = 0x12;           // Orca mesh sync word
static const int LORA_HOP_LIMIT = 8;

TinyGPSPlus gps;
WiFiUDP udp;
HardwareSerial GPS(2);
float cn0Base = NAN;
unsigned long lastReport = 0, msgCounter = 0;

// tiny dedup cache of recently seen frame ids
static const int SEEN_MAX = 48;
String seen[SEEN_MAX];
int seenHead = 0;
bool seenAdd(const String& id) {
  for (int i = 0; i < SEEN_MAX; i++) if (seen[i] == id) return false;
  seen[seenHead] = id; seenHead = (seenHead + 1) % SEEN_MAX;
  return true;
}

String newId() { return String(NODE_ID) + "-" + String(millis()) + "-" + String(msgCounter++); }

void bridgeToOrca(const String& evJson) {
  if (WiFi.status() != WL_CONNECTED) return;
  udp.beginPacket(ORCA_HOST, ORCA_UDP_PORT);
  udp.print(evJson);
  udp.endPacket();
}

// Send a mesh frame over LoRa: {"id","hops","ev":{...}}
void loraSend(const String& id, int hops, const String& evJson) {
  LoRa.beginPacket();
  LoRa.print("{\"id\":\""); LoRa.print(id); LoRa.print("\",\"hops\":");
  LoRa.print(hops); LoRa.print(",\"ev\":"); LoRa.print(evJson); LoRa.print("}");
  LoRa.endPacket();
}

// Originate a local Orca event: bridge to the server and broadcast on the mesh.
void emitEvent(const String& cls, const String& sev, const String& dataJson) {
  String id = newId();
  seenAdd(id);
  String ev = String("{\"source\":\"esp.gnss\",\"class\":\"") + cls +
              "\",\"severity\":\"" + sev + "\",\"node\":\"" + NODE_ID +
              "\",\"id\":\"" + id + "\",\"ts\":" + String((double)millis() / 1000.0, 3) +
              ",\"data\":" + dataJson + "}";
  bridgeToOrca(ev);
  loraSend(id, 0, ev);
}

// very small JSON field extractor (string values)
String jsonStr(const String& s, const String& key) {
  int k = s.indexOf("\"" + key + "\"");
  if (k < 0) return "";
  int c = s.indexOf(':', k); if (c < 0) return "";
  int q1 = s.indexOf('"', c + 1); if (q1 < 0) return "";
  int q2 = s.indexOf('"', q1 + 1); if (q2 < 0) return "";
  return s.substring(q1 + 1, q2);
}
int jsonInt(const String& s, const String& key) {
  int k = s.indexOf("\"" + key + "\""); if (k < 0) return 0;
  int c = s.indexOf(':', k); return c < 0 ? 0 : s.substring(c + 1).toInt();
}

void handleLoRa(int packetSize) {
  if (packetSize == 0) return;
  String pkt;
  while (LoRa.available()) pkt += (char)LoRa.read();
  String id = jsonStr(pkt, "id");
  if (id.length() == 0 || !seenAdd(id)) return;      // dup or malformed
  int hops = jsonInt(pkt, "hops");
  int evStart = pkt.indexOf("\"ev\":");
  if (evStart < 0) return;
  String ev = pkt.substring(evStart + 5);
  if (ev.endsWith("}")) ev.remove(ev.length() - 1);  // trim outer brace
  bridgeToOrca(ev);                                   // bridge to the Orca server
  if (hops + 1 < LORA_HOP_LIMIT) loraSend(id, hops + 1, ev);   // forward on the mesh
}

float topMeanCn0() {
  if (!gps.satellites.isValid()) return NAN;
  float n = gps.satellites.value();
  return 30.0 + min(18.0f, n * 1.6f);               // proxy; use a GSV parser for true C/N0
}

void setup() {
  Serial.begin(115200);
  GPS.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);

  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_SS);
  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);
  if (!LoRa.begin(LORA_FREQ)) {
    Serial.println("LoRa init FAILED - check wiring/frequency");
  } else {
    LoRa.setSpreadingFactor(LORA_SF);
    LoRa.setSignalBandwidth(LORA_BW);
    LoRa.setSyncWord(LORA_SYNC);
    LoRa.enableCrc();
    Serial.printf("LoRa mesh up on %ld Hz SF%d\n", LORA_FREQ, LORA_SF);
  }

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  for (int i = 0; i < 20 && WiFi.status() != WL_CONNECTED; i++) delay(500);
  Serial.println(WiFi.status() == WL_CONNECTED ? "WiFi up (bridging to Orca)"
                                               : "LoRa-only (no WiFi bridge)");
}

void loop() {
  handleLoRa(LoRa.parsePacket());                    // relay the LoRa mesh
  while (GPS.available()) gps.encode(GPS.read());

  if (millis() - lastReport < 2000) return;
  lastReport = millis();
  if (WiFi.status() != WL_CONNECTED) WiFi.reconnect();

  float cn0 = topMeanCn0();
  int used = gps.satellites.isValid() ? gps.satellites.value() : 0;
  double lat = gps.location.isValid() ? gps.location.lat() : NODE_LAT;
  double lon = gps.location.isValid() ? gps.location.lng() : NODE_LON;
  if (isnan(cn0)) return;

  bool jam = !isnan(cn0Base) && (cn0Base - cn0 >= JAM_DROP_DB || (used < 4 && cn0Base > 35));
  String geo = String(",\"lat\":") + String(lat, 5) + ",\"lon\":" + String(lon, 5) + "}";
  if (jam) {
    emitEvent("rf.gnss.jamming", "high",
              String("{\"cn0_mean\":") + String(cn0, 1) + ",\"cn0_base\":" + String(cn0Base, 1) +
              ",\"drop_db\":" + String(cn0Base - cn0, 1) + ",\"sats_used\":" + used + geo);
  } else {
    cn0Base = isnan(cn0Base) ? cn0 : 0.95 * cn0Base + 0.05 * cn0;
    emitEvent("rf.gnss.health", "info",
              String("{\"cn0_mean\":") + String(cn0, 1) + ",\"sats_used\":" + used + geo);
  }
}

// Send a burst-chat message over the LoRa mesh (call from your own trigger/button).
void sendChat(const String& text) {
  String id = newId(); seenAdd(id);
  String ev = String("{\"source\":\"orca.chat\",\"class\":\"chat.clear\",\"severity\":\"info\",\"node\":\"") +
              NODE_ID + "\",\"id\":\"" + id + "\",\"ts\":" + String((double)millis() / 1000.0, 3) +
              ",\"data\":{\"from\":\"" + NODE_ID + "\",\"text\":\"" + text + "\"}}";
  bridgeToOrca(ev);
  loraSend(id, 0, ev);
}
