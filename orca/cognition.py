"""Cognition: Orca's brain.

Three things sit here:

- SituationEngine: fuses every signal (RF, cyber, GNSS, mesh, HF) into a small
  number of ranked "situations" — correlated clusters across domains — each with a
  plain-language assessment and a prediction of what is likely next. On-device,
  deterministic, no cloud.
- AskOrca: answers natural-language questions about the live picture and, when the
  question implies it, acts (tunes the spectrum, opens an incident). Rule-based so
  it works offline; can defer to Claude when configured.
- reason(): an optional bridge to Claude for richer assessment and course-of-action
  options on a situation, with a clean fall-back to the on-device assessment.
"""
from __future__ import annotations

import os
import re
import time
from collections import deque


FAMILIES = {
    "rf.gnss": "GNSS", "rf.emission": "spectrum", "net": "network",
    "aprs": "APRS", "hf": "HF", "orca.chat": "chat",
}


def family(cls: str) -> str:
    for k, v in FAMILIES.items():
        if cls.startswith(k):
            return v
    return "other"


def _fmt_hz(hz: float) -> str:
    if hz >= 1e9:
        return f"{hz / 1e9:.3f} GHz"
    if hz >= 1e6:
        return f"{hz / 1e6:.3f} MHz"
    return f"{hz / 1e3:.1f} kHz"


class SituationEngine:
    """Keeps a rolling fused picture. Feed it events and incidents; read picture()."""

    def __init__(self, names: dict[str, str], window_s: float = 900):
        self.names = names
        self.window = window_s
        self.events: deque = deque(maxlen=2000)
        self.incidents: dict[str, dict] = {}
        self.node_health: dict[str, str] = {}

    def observe_event(self, ev: dict):
        self.events.append(ev)
        if ev.get("class") == "rf.gnss.health":
            self.node_health[ev.get("node", "")] = ev.get("data", {}).get("state", "ok")

    def observe_incident(self, inc: dict):
        self.incidents[inc["id"]] = inc

    def _recent(self, now: float) -> list[dict]:
        return [e for e in self.events if now - e["ts"] <= self.window]

    def picture(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        recent = self._recent(now)
        sits: list[dict] = []

        open_inc = [i for i in self.incidents.values() if i.get("status") == "open"]
        jam = [i for i in open_inc if i["rule"] in ("gnss_jam", "gnss_spoof")]
        net = [e for e in recent if family(e["class"]) == "network"]

        # 1) Coordinated electronic + cyber attack (cross-domain correlation)
        if jam and net:
            nodes = sorted({n for i in jam for n in i["nodes"]})
            sits.append(self._situation(
                "coordinated-ew-cyber", "Coordinated jamming and network attack", "critical",
                0.9, nodes, now,
                assessment=(f"GNSS interference on {self._where(nodes)} is coinciding with "
                            f"{len(net)} network security event(s) in the same window. The pairing of "
                            "RF denial with an intrusion attempt is a hallmark of a deliberate, "
                            "coordinated operation rather than two unrelated faults."),
                prediction="Expect the intrusion to escalate while GNSS is degraded. Isolate the "
                           "affected network now and preserve signed evidence.",
                evidence=[i["id"] for i in jam]))

        # 2) Area GNSS denial (multi-node)
        elif jam:
            nodes = sorted({n for i in jam for n in i["nodes"]})
            multi = len(nodes) >= 2
            sits.append(self._situation(
                "gnss-denial", f"GNSS {'denial across an area' if multi else 'interference'}",
                "critical" if multi else "high", 0.85 if multi else 0.7, nodes, now,
                assessment=(f"GNSS signal is degraded on {self._where(nodes)}. "
                            + ("Several nodes at once indicates an area-wide jammer rather than a "
                               "local fault; position and timing in this area should be treated as "
                               "untrusted." if multi else "A single node so far; a receiver or antenna "
                               "fault is still possible.")),
                prediction=("Likely to spread to adjacent nodes; direction-finding will localise the "
                            "source." if multi else "Confirm on a second node before acting."),
                evidence=[i["id"] for i in jam]))

        # 3) Protected-channel / interference situations
        prot = [i for i in open_inc if i["rule"] == "protected"]
        for i in prot:
            sits.append(self._situation(
                f"interference-{i['id']}", i["title"], i["severity"], 0.75, i["nodes"], now,
                assessment=i.get("summary", "A protected or safety channel is being occupied."),
                prediction="If it persists, warn the net and file an interference report.",
                evidence=[i["id"]]))

        # 4) Infrastructure / beacon loss
        beacons = [i for i in open_inc if i["rule"] == "beacon"]
        if beacons:
            calls = [i["ctx"].get("call", "a station") if "ctx" in i else "a station" for i in beacons]
            sits.append(self._situation(
                "infra-loss", "Expected station(s) off the air", "medium", 0.7,
                [n for i in beacons for n in i["nodes"]], now,
                assessment=f"{', '.join(calls)} has gone silent past its normal interval, which points "
                           "to a power or equipment failure at the site.",
                prediction="Move traffic to the simplex backup until the site is restored.",
                evidence=[i["id"] for i in beacons]))

        # 5) New / unidentified emitters trend
        anoms = [e for e in recent if e["class"] == "rf.emission.anomaly"]
        if len(anoms) >= 3:
            sits.append(self._situation(
                "new-emitters", "Unidentified RF activity above baseline", "medium", 0.6, [], now,
                assessment=f"{len(anoms)} signals above this site's learnt baseline in the last "
                           f"{int(self.window/60)} min, including around "
                           f"{_fmt_hz(anoms[-1]['data'].get('f_center', 0))}. New or intermittent "
                           "transmitters are appearing.",
                prediction="Fingerprint and classify them; add to the emitter library and ask local "
                           "operators to identify.",
                evidence=[]))

        if not sits:
            sits.append(self._situation(
                "nominal", "All nominal", "info", 0.9, [], now,
                assessment="No correlated threats. Spectrum and network are within this site's normal "
                           "range and all expected stations are on the air.",
                prediction="Continuing to watch; nothing requires action.",
                evidence=[]))

        rank = {"critical": 0, "high": 1, "medium": 2, "info": 3}
        sits.sort(key=lambda s: (rank.get(s["severity"], 4), -s["confidence"]))
        return sits

    def _where(self, nodes) -> str:
        return ", ".join(self.names.get(n, n) for n in nodes) or "the area"

    def _situation(self, sid, title, severity, confidence, nodes, now, assessment,
                   prediction, evidence):
        return {"id": sid, "title": title, "severity": severity, "confidence": round(confidence, 2),
                "nodes": list(nodes), "ts": now, "assessment": assessment,
                "prediction": prediction, "evidence": evidence}


# ------------------------------------------------------------------ Ask Orca
class AskOrca:
    """Answers natural-language questions about the live picture. Deterministic
    intent + entity parsing so it works offline; returns an answer and an optional
    action for the console to carry out."""

    BANDS = {"gps": (1565e6, 1586e6), "gnss": (1565e6, 1586e6), "l1": (1574e6, 1577e6),
             "2m": (144e6, 148e6), "2 m": (144e6, 148e6), "70cm": (430e6, 450e6),
             "70 cm": (430e6, 450e6), "aprs": (144.3e6, 144.5e6), "marine": (156e6, 162e6),
             "ais": (161.9e6, 162.1e6), "adsb": (1085e6, 1095e6), "ads-b": (1085e6, 1095e6),
             "fm": (88e6, 108e6), "wifi": (2400e6, 2500e6), "ku": (12e9, 18e9), "x band": (8e9, 12e9)}

    def __init__(self, engine: SituationEngine, get_state):
        self.engine = engine
        self.get_state = get_state        # callable -> dict with nodes, incidents, emitters, view, tropo

    def _window(self, q: str) -> float:
        m = re.search(r"(\d+)\s*(min|hour|hr|h\b|day)", q)
        if not m:
            return 3600 if "hour" in q or "today" in q else 900
        n = int(m.group(1))
        return n * (60 if "min" in m.group(2) else 86400 if "day" in m.group(2) else 3600)

    def ask(self, q: str) -> dict:
        ql = q.lower().strip()
        now = time.time()
        st = self.get_state()

        # status / what's happening
        if any(w in ql for w in ("what's happening", "whats happening", "situation", "status",
                                 "sitrep", "summary", "overview")):
            pic = self.engine.picture(now)
            top = pic[0]
            extra = "; also ".join(s["title"].lower() for s in pic[1:3])
            ans = f"{top['title']} ({top['severity']}). {top['assessment']}"
            if extra:
                ans += f" Also tracking: {extra}."
            return {"answer": ans, "situations": pic[:4]}

        # jamming / interference questions
        if "jam" in ql or "spoof" in ql or "interfer" in ql:
            w = self._window(ql)
            evs = [e for e in self.engine.events if now - e["ts"] <= w
                   and ("jam" in e["class"] or "spoof" in e["class"] or e["class"] == "rf.emission")]
            if not evs:
                return {"answer": f"No jamming, spoofing or interference in the last "
                                  f"{int(w/60)} minutes."}
            nodes = sorted({self.engine.names.get(e.get("node"), e.get("node")) for e in evs if e.get("node")})
            gnss = [e for e in evs if "gnss" in e["class"]]
            a = f"{len(evs)} interference event(s) in the last {int(w/60)} min"
            if gnss:
                a += f", including GNSS jamming/spoofing on {', '.join(nodes)}"
            return {"answer": a + ".", "action": {"view": list(self.BANDS["gps"])} if gnss else None}

        # quietest / loudest node
        if "quiet" in ql or "loud" in ql or "noisiest" in ql:
            health = self.engine.node_health
            if not health:
                return {"answer": "No node signal reports yet."}
            order = sorted(health.items(), key=lambda kv: kv[1] != "ok")
            pick = order[0] if "quiet" in ql else order[-1]
            return {"answer": f"{self.engine.names.get(pick[0], pick[0])} looks "
                              f"{'healthiest' if 'quiet' in ql else 'most affected'} "
                              f"(state: {pick[1]})."}

        # strongest signal on a band
        band = next((b for b in self.BANDS if b in ql), None)
        if "strongest" in ql or "loudest signal" in ql or (band and "what" in ql):
            lo, hi = self.BANDS.get(band, (self.engine and 0, 20e9))
            frame = st.get("last_frame") or {}
            ems = [e for e in frame.get("emissions", []) if lo <= e["f_center"] <= hi]
            if not ems:
                return {"answer": f"Nothing notable on {band or 'the current view'} right now.",
                        "action": {"view": [lo, hi]} if band else None}
            s = max(ems, key=lambda e: e["snr_db"])
            return {"answer": f"Strongest on {band or 'view'}: {_fmt_hz(s['f_center'])}, "
                              f"{s['snr_db']:.0f} dB SNR, {s['bw_occ']/1e3:.1f} kHz wide.",
                    "action": {"view": [lo, hi]}}

        # tune to a band
        if band and any(w in ql for w in ("show", "tune", "go to", "look at", "watch")):
            lo, hi = self.BANDS[band]
            return {"answer": f"Tuning to {band}.", "action": {"view": [lo, hi]}}

        # incidents
        if "incident" in ql or "alert" in ql or "threat" in ql:
            open_inc = [i for i in st.get("incidents", []) if i.get("status") == "open"]
            if not open_inc:
                return {"answer": "No open incidents."}
            return {"answer": f"{len(open_inc)} open: " + "; ".join(
                f"{i['title']} ({i['severity']})" for i in open_inc[:4])}

        # propagation / troposcatter
        if "propagation" in ql or "tropo" in ql or "opening" in ql or "band open" in ql:
            t = st.get("tropo")
            if t and t.get("stats"):
                return {"answer": f"Troposcatter path {t['d_km']} km on {_fmt_hz(t['freq_hz'])}: "
                                  f"{t['stats']['availability_pct']:.0f}% available, median "
                                  f"{t['stats']['median_snr']} dB."}
            return {"answer": "No troposcatter experiment running. Set one up in the Troposcatter panel."}

        return {"answer": "I can answer things like: what's the situation, any jamming this hour, "
                          "which node is quietest, strongest signal on 2m, show GPS, open incidents, "
                          "propagation. Try one of those."}


# ------------------------------------------------------------------ Claude bridge
async def reason(situation: dict, cfg: dict) -> dict | None:
    """Ask Claude for a richer assessment and course-of-action options. Returns
    None (and the caller keeps the on-device assessment) when no key is configured
    or the call fails."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not (cfg.get("enabled") and key):
        return None
    import httpx
    prompt = ("You are Orca's cognitive analyst for a spectrum and security operations console. "
              "Given this fused situation, write (1) a 2-3 sentence assessment and (2) up to three "
              "concrete courses of action, each one line. Use only the facts given; do not invent "
              "detail. Respond as plain text.\n\n" + str(situation)[:6000])
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             json={"model": cfg.get("model", "claude-sonnet-5-5"), "max_tokens": 500,
                                   "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            return {"source": "claude", "text": r.json()["content"][0]["text"].strip()}
    except Exception:
        return None
