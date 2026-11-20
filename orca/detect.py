"""Detection rules: turn events into incidents with playbooks."""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque

from .oef import Event

SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

PLAYBOOKS: dict[str, list[dict]] = {
    "gnss_jam": [
        {"text": "Focus spectrum on GPS L1 and record 2 s of IQ as evidence", "action": "capture_iq",
         "args": {"freq": 1575.42e6, "span": 20e6}, "gate": False},
        {"text": "Task nearby nodes to take bearings (direction-finding)", "action": "task_df",
         "args": {"freq": 1575.42e6}, "gate": False},
        {"text": "Send APRS bulletin warning the local EmComm net", "action": "aprs_bulletin",
         "args": {"text": "GNSS jamming reported near {where}. Expect GPS loss. Use manual nav/timing."},
         "gate": True},
        {"text": "Write incident report for the spectrum regulator", "action": "report", "args": {},
         "gate": True},
    ],
    "gnss_spoof": [
        {"text": "Flag GNSS time and position on this node as untrusted", "action": "note", "args": {},
         "gate": False},
        {"text": "Record 2 s of IQ on GPS L1 as evidence", "action": "capture_iq",
         "args": {"freq": 1575.42e6, "span": 20e6}, "gate": False},
        {"text": "Write incident report for the spectrum regulator", "action": "report", "args": {},
         "gate": True},
    ],
    "protected": [
        {"text": "Record 2 s of IQ on the channel as evidence", "action": "capture_iq",
         "args": {"freq": "{freq}", "span": 200e3}, "gate": False},
        {"text": "Warn net control and suggest an alternate channel", "action": "aprs_bulletin",
         "args": {"text": "Interference on {channel}. Net control: move to alternate."}, "gate": True},
        {"text": "Write interference report", "action": "report", "args": {}, "gate": True},
    ],
    "new_emitter": [
        {"text": "Record 2 s of IQ and add the signal to the emitter library as unidentified",
         "action": "capture_iq", "args": {"freq": "{freq}", "span": 1e6}, "gate": False},
        {"text": "Ask local operators to identify it on the net", "action": "aprs_bulletin",
         "args": {"text": "Unidentified signal at {mhz} MHz near {where}. Info to net control."},
         "gate": True},
    ],
    "beacon": [
        {"text": "Ask net control to move traffic to the simplex backup", "action": "aprs_bulletin",
         "args": {"text": "{call} not heard. Net moving to simplex backup."}, "gate": True},
        {"text": "Log a maintenance ticket", "action": "note", "args": {}, "gate": False},
    ],
    "rf_cyber": [
        {"text": "Lock the affected network to known devices (send to network admin)", "action": "report",
         "args": {"kind": "network"}, "gate": True},
    ],
}


def new_incident(rule: str, title: str, severity: str, ev: Event, ctx: dict) -> dict:
    steps = []
    for i, s in enumerate(PLAYBOOKS.get(rule, [])):
        args = {k: (v.format(**ctx) if isinstance(v, str) else v) for k, v in s["args"].items()}
        if args.get("freq") and isinstance(args["freq"], str):
            args["freq"] = float(args["freq"])
        steps.append({"id": i, "text": s["text"].format(**ctx), "action": s["action"], "args": args,
                      "gate": s["gate"], "state": "pending", "result": ""})
    return {"id": "INC-" + uuid.uuid4().hex[:6].upper(), "rule": rule, "title": title,
            "severity": severity, "status": "open", "created": ev.ts, "updated": ev.ts,
            "nodes": [ev.node] if ev.node else [], "geo": ev.geo, "evidence": [ev.to_dict()],
            "summary": "", "steps": steps, "ctx": ctx}


class Rules:
    def __init__(self, protected: list[dict], node_names: dict[str, str]):
        self.protected = protected            # [{name, lo, hi}]
        self.names = node_names
        self.incidents: dict[str, dict] = {}
        self.open_by_key: dict[str, str] = {}
        self._jam = defaultdict(float)        # node -> last jamming ts
        self._prot_seen: dict[str, list[float]] = {}
        self._anom: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))

    def _upsert(self, key, rule, title, sev, ev, ctx) -> tuple[dict, bool]:
        iid = self.open_by_key.get(key)
        inc = self.incidents.get(iid) if iid else None
        if inc and inc["status"] == "open":
            inc["evidence"] = (inc["evidence"] + [ev.to_dict()])[-25:]
            inc["updated"] = ev.ts
            if ev.node and ev.node not in inc["nodes"]:
                inc["nodes"].append(ev.node)
            if SEV_RANK[sev] > SEV_RANK[inc["severity"]]:
                inc["severity"] = sev
            inc["title"] = title
            return inc, False
        inc = new_incident(rule, title, sev, ev, ctx)
        self.incidents[inc["id"]] = inc
        self.open_by_key[key] = inc["id"]
        return inc, True

    def where(self, nodes) -> str:
        return ", ".join(self.names.get(n, n) for n in nodes) or "unknown site"

    def on_event(self, ev: Event) -> list[tuple[dict, bool]]:
        """Returns (incident, created) pairs touched by this event."""
        out = []
        c, now = ev.cls, ev.ts
        if c == "rf.gnss.jamming":
            self._jam[ev.node] = now
            nodes = [n for n, t in self._jam.items() if now - t < 120]
            sev = "high"
            title = (f"GNSS jamming cluster ({len(nodes)} nodes)" if len(nodes) >= 2
                     else f"GNSS jamming at {self.where([ev.node])}")
            key = "gnss_jam"
            prev = self.incidents.get(self.open_by_key.get(key, ""))
            if prev and prev["status"] == "open" and prev["severity"] == "critical":
                sev, title = "critical", title + " + network attack"
            out.append(self._upsert(key, "gnss_jam", title, sev, ev, {"where": self.where(nodes)}))
        elif c == "rf.gnss.spoofing":
            out.append(self._upsert(f"spoof:{ev.node}", "gnss_spoof",
                                    f"GNSS spoofing suspected at {self.where([ev.node])}", "high", ev,
                                    {"where": self.where([ev.node])}))
        elif c == "rf.emission":
            # the spectrum loop sets `steady` once a channel has been occupied
            # >= 95 % of frames for its window (normal bursty traffic never is)
            ch = ev.data.get("channel")
            if ch and ev.data.get("steady"):
                safety = any(p["name"] == ch and p.get("safety") for p in self.protected)
                out.append(self._upsert(f"prot:{ch}", "protected", f"Interference on {ch}",
                                        "high" if safety else "medium", ev,
                                        {"channel": ch, "freq": ev.data["f_center"]}))
        elif c == "rf.emission.anomaly":
            k = int(ev.data["f_center"] // 50e3)
            dq = self._anom[k]
            dq.append(now)
            if len([t for t in dq if now - t < 60]) >= 3 and ev.data.get("z", 0) >= 6:
                out.append(self._upsert(f"new:{k}", "new_emitter",
                                        f"New emitter outside baseline at {ev.data['f_center'] / 1e6:.3f} MHz",
                                        "medium", ev, {"freq": ev.data["f_center"],
                                                       "mhz": f"{ev.data['f_center'] / 1e6:.3f}",
                                                       "where": self.where([ev.node])}))
        elif c == "aprs.beacon.missing":
            out.append(self._upsert(f"beacon:{ev.data['call']}", "beacon",
                                    f"{ev.data['call']} beacon not heard", "medium", ev,
                                    {"call": ev.data["call"]}))
        elif c == "aprs.beacon.restored":
            iid = self.open_by_key.get(f"beacon:{ev.data['call']}")
            if iid and self.incidents[iid]["status"] == "open":
                self.incidents[iid]["status"] = "cleared"
                out.append((self.incidents[iid], False))
        elif c in ("net.auth.fail", "net.rogue_ap"):
            iid = self.open_by_key.get("gnss_jam")
            inc = self.incidents.get(iid) if iid else None
            if inc and inc["status"] == "open" and now - inc["updated"] < 900:
                inc["severity"] = "critical"
                if "network attack" not in inc["title"]:
                    inc["title"] += " + network attack"
                    inc["steps"] += [dict(s, id=len(inc["steps"]) + i, state="pending", result="")
                                     for i, s in enumerate(PLAYBOOKS["rf_cyber"])]
                inc["evidence"] = (inc["evidence"] + [ev.to_dict()])[-25:]
                inc["updated"] = now
                out.append((inc, False))
        return out
