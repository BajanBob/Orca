# Orca ESP32 field node

A low-cost ESP32 node that extends an Orca site: it reads a GNSS receiver, reports
position and signal health to the Orca server, and flags GNSS jamming locally. Over LoRa it relays short burst-chat and event frames to other Orca nodes off-grid.

## Hardware
- Any ESP32 dev board.
- A GNSS receiver with NMEA UART (u-blox NEO-6/7/8/9, etc.) on GPIO 16/17.
- An SX127x LoRa module (e.g. TTGO LoRa32 or Heltec, which integrate it) for the mesh.

## Flash it
- **Arduino IDE:** install the ESP32 boards package and the `TinyGPSPlus` library,
  open `orca_node/orca_node.ino`, set the WiFi and Orca server details at the top,
  select "ESP32 Dev Module", and upload.
- **PlatformIO:** `cd esp32 && pio run -t upload`.

## LoRa mesh
LoRa is built in. The node broadcasts its own events and relays frames it hears
from other nodes (dedup by id, hop limit), so detections and burst-chat cross a
grid-down area. Set `LORA_FREQ` for your ITU region (915 MHz Americas, 868 MHz
Europe/Africa, 433 MHz where allowed) and match SF/bandwidth across your nodes.
Transmitting LoRa is the node's own licensed ISM operation.

## What it sends
The node posts Orca Event Format (OEF) JSON over UDP to the Orca server's ingest
port (default 5514): `rf.gnss.health` normally, and `rf.gnss.jamming` when the
satellite signal drops sharply. Point the server's `syslog`/UDP ingest at this port.

## Boundaries
This node does not encrypt over the air. Secure communications live in the Orca
server's government/defence module, on licensed non-amateur frequencies. Keep any
beacon/PTT output on bands you are licensed for.
