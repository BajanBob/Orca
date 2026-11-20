"""Network adapter: UDP syslog in (routers, firewalls, VPN, Wi-Fi controllers)."""
from __future__ import annotations

import asyncio
import random
import re

from ..oef import Event

PATTERNS = [
    (re.compile(r"(failed password|authentication fail|login fail|invalid user|auth.*denied)", re.I),
     "net.auth.fail", "medium"),
    (re.compile(r"(rogue ap|unauthori[sz]ed (ap|access point)|evil twin)", re.I), "net.rogue_ap", "high"),
    (re.compile(r"(port scan|portscan|syn flood)", re.I), "net.scan", "medium"),
]
SRC_IP = re.compile(r"(?:from|src[=:]?)\s*(\d{1,3}(?:\.\d{1,3}){3})", re.I)


def classify(line: str) -> tuple[str, str] | None:
    for rx, cls, sev in PATTERNS:
        if rx.search(line):
            return cls, sev
    return None


class SyslogAdapter:
    def __init__(self, node: str, mode: str = "sim", port: int = 5514, scenarios: set | None = None):
        self.node, self.mode, self.port = node, mode, port
        self.scenarios = scenarios if scenarios is not None else set()
        self.health = "starting"

    def handle(self, line: str, emit):
        c = classify(line)
        if c:
            ip = SRC_IP.search(line)
            emit(Event("net.syslog", c[0], c[1], self.node,
                       {"line": line[:300], "src_ip": ip.group(1) if ip else None}))

    async def run(self, emit):
        if self.mode == "sim":
            self.health = "online (sim)"
            while True:
                await asyncio.sleep(random.uniform(8, 20))
                if "gnss_jam" in self.scenarios and random.random() < 0.6:
                    self.handle("vpn: authentication failure for user svc-port from 203.0.113.7", emit)
        adapter = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                adapter.handle(data.decode("utf-8", "replace"), emit)

        await asyncio.get_running_loop().create_datagram_endpoint(Proto, local_addr=("0.0.0.0", self.port))
        self.health = f"listening UDP {self.port}"
        while True:
            await asyncio.sleep(3600)
