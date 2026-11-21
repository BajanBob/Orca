"""Orca Phase 2 server: sensors in, incidents and live spectrum out to the touch console."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

from . import __version__
from .adapters import AprsAdapter, GpsdAdapter, NmeaSerialAdapter, SimGnssAdapter, UranusAdapter
from .adapters.network_syslog import SyslogAdapter
from .agents import Audit, llm_triage, triage_text, write_report
from .bands import BANDS, FRONTENDS, RECEIVERS, describe_plan, plan
from .baseline import BINS, Baseline
from .bus import Bus
from .capture import CaptureStore
from .chat import BurstChat
from .detect import Rules
from .df import aoa_fix, tdoa_fix
from .intel import Classifier, EmitterLibrary
from .mesh import MeshNode, NullTransport, RadioTransport, anonymise
from . import tropo as tropo_mod
from .oef import Event, Signer
from .sdr import make_source
from .secure import ClearFskDecoder, DecoderBank, KeyStore, SecureChannel, SecurePolicy
from .spectrum import Spectrum

CONSOLE = Path(__file__).parent / "console" / "index.html"


class Orca:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        d = Path(cfg["data_dir"])
        site = cfg["site"]
        self.node = site["node"]
        self.signer = Signer(d / "keys" / f"{self.node}.pem")
        self.bus = Bus()
        self.src = make_source(cfg["sdr"])
        self.scenarios: set = getattr(self.src, "scenarios", set())
        inv = cfg["inventory"]
        self.spectrum = Spectrum(self.src, inv["receivers"], inv["frontends"], cfg["display_bins"])
        self.baseline = Baseline(d / "baseline.sqlite")
        self.captures = CaptureStore(d / "captures", self.node)
        self.audit = Audit(d / "audit.jsonl")
        self.names = {n["id"]: n["name"] for n in cfg["nodes"]}
        self.names.setdefault(self.node, site["name"])
        self.rules = Rules(cfg["protected"], self.names)
        self.view = {"lo": float(cfg["view"]["lo"]), "hi": float(cfg["view"]["hi"])}
        self.last_frame: dict | None = None
        self.events: deque = deque(maxlen=400)
        self._throttle: dict[str, float] = {}
        self._occ: dict[str, deque] = {}
        self._ev_times: deque = deque(maxlen=5000)
        self.adapters: dict[str, object] = {}
        self.paused = False
        self._build_adapters()
        self._build_phase34(cfg, d)

    # ------------------------------------------------------------ phase 3 & 4
    def _build_phase34(self, cfg, d):
        # Phase 3: on-device classifier (lazy-trained, cached) and emitter library
        self.classifier = Classifier.load_or_train(d / "modrec.json")
        self.emitters = EmitterLibrary(d / "emitters.json")
        # Phase 4: mesh. Over a radio (troposcatter/VHF/LoRa) KISS link if
        # configured, else a mesh of one.
        radio = cfg.get("mesh", {}).get("radio")
        self.mesh_radio = None
        if radio and radio.get("enabled"):
            self.mesh_radio = RadioTransport(radio.get("host", "127.0.0.1"),
                                             int(radio.get("port", 8001)),
                                             float(radio.get("min_quality", 0.15)))
            self.mesh = MeshNode(self.node, self.signer, self.mesh_radio)
        else:
            self.mesh = MeshNode(self.node, self.signer, NullTransport())
        self.fixes: dict[str, dict] = {}        # incident id -> last DF fix
        # Troposcatter experiment (Phase 4 off-grid link planning + measurement)
        self.tropo = None
        tp = cfg.get("tropo", {})
        if tp.get("enabled"):
            self.start_tropo(tp)
        # Secure module (government/defence edition), off unless configured
        sc = cfg.get("secure", {})
        self.secure_on = bool(sc.get("enabled"))
        self.keystore = KeyStore()
        self.secure = None
        if self.secure_on:
            pol = SecurePolicy(region=int(cfg["site"].get("region", "itu2")[-1] or 2),
                               floor_hz=float(sc.get("floor_hz", 148e6)),
                               licensed_bands=[tuple(map(float, b)) for b in sc.get("licensed_bands", [])],
                               authority=sc.get("authority", ""))
            for hx in sc.get("keys_hex", []):
                try:
                    self.keystore.add(bytes.fromhex(hx))
                except ValueError:
                    pass
            bank = DecoderBank(pol, SecureChannel(self.keystore))
            bank.register(ClearFskDecoder(baud=float(sc.get("baud", 1200))))
            self.secure = {"policy": pol, "bank": bank}
        # Burst chat over the mesh: clear on ham, encrypted on the secure module
        self.chat = BurstChat(
            self.node, cfg.get("aprs", {}).get("callsign", "") or self.node, self.signer,
            self.mesh.publish,
            SecureChannel(self.keystore) if self.secure else None,
            self.secure["policy"] if self.secure else None)
        # route chat events arriving from mesh peers into the chat log
        self.mesh.on_event = self._on_mesh_event

    def _on_mesh_event(self, ev: dict):
        if not self.chat.on_event(ev):
            # non-chat peer events: surface for the shared picture, don't re-run rules
            self.events.append(ev)
            self.bus.publish("event", ev)
        else:
            self.bus.publish("chat", self.chat.recent(1)[0])

    # ------------------------------------------------------------ adapters
    def _build_adapters(self):
        for n in self.cfg["nodes"]:
            g = n.get("gnss", {"kind": "sim"})
            if g["kind"] == "gpsd":
                a = GpsdAdapter(n["id"], g.get("host", "127.0.0.1"), g.get("port", 2947))
            elif g["kind"] == "nmea":
                a = NmeaSerialAdapter(n["id"], g["device"], g.get("baud", 9600))
            else:
                a = SimGnssAdapter(n["id"], n["lat"], n["lon"], self.scenarios)
            self.adapters[f"gnss:{n['id']}"] = a
        ap = self.cfg["aprs"]
        self.aprs = AprsAdapter(self.node, ap["mode"], ap.get("host", "127.0.0.1"), ap.get("port", 8001),
                                ap.get("callsign", ""), ap.get("transmit", False),
                                ap.get("aprsis_filter", ""), ap.get("watch", []), self.scenarios)
        self.adapters["aprs"] = self.aprs
        u = self.cfg["uranus"]
        self.adapters["uranus"] = UranusAdapter(self.node, "sim" if u["mode"] == "sim" else "udp", u.get("port", 2237))
        s = self.cfg["syslog"]
        self.adapters["syslog"] = SyslogAdapter(self.node, s["mode"], s.get("port", 5514), self.scenarios)

    # ------------------------------------------------------------ events
    def emit(self, ev: Event):
        self.signer.sign(ev)
        d = ev.to_dict()
        self.events.append(d)
        self._ev_times.append(time.time())
        self.bus.publish("event", d)
        self.mesh.publish(d)          # gossip to any mesh peers
        for inc, created in self.rules.on_event(ev):
            if created:
                asyncio.get_running_loop().create_task(self._on_new_incident(inc))
            self._pub_incident(inc)

    def _pub_incident(self, inc):
        self.bus.publish("incident", {k: v for k, v in inc.items() if k != "ctx"})

    async def _on_new_incident(self, inc):
        inc["summary"] = triage_text(inc, self.names)
        self.audit.log("Orca rules", f"raised {inc['id']}: {inc['title']}", incident=inc["id"])
        self.bus.publish("audit", self.audit.recent[0])
        self._pub_incident(inc)
        rich = await llm_triage(inc, inc["summary"], self.cfg["agent"]["llm"])
        if rich:
            inc["summary"] = rich
        self.audit.log("Beryl agent", f"triaged {inc['id']}", incident=inc["id"])
        self.bus.publish("audit", self.audit.recent[0])
        if self.cfg["agent"]["autorun_safe_steps"]:
            for st in inc["steps"]:
                if not st["gate"] and st["state"] == "pending":
                    await self.run_step(inc["id"], st["id"], "Beryl agent")
        self._pub_incident(inc)

    # ------------------------------------------------------------ actions
    async def run_step(self, iid: str, sid: int, actor: str, skip: bool = False) -> dict:
        inc = self.rules.incidents.get(iid)
        if not inc:
            raise KeyError(iid)
        st = next(s for s in inc["steps"] if s["id"] == sid)
        if st["state"] != "pending":
            return st
        if skip:
            st["state"], st["result"] = "skipped", f"skipped by {actor}"
        elif st["gate"] and actor != "Operator":
            raise PermissionError("this step needs operator approval")
        else:
            st["result"] = await self._do(inc, st)
            st["state"] = "blocked" if st["result"].startswith("not sent") else "done"
        self.audit.log(actor, f"{'skipped' if skip else 'approved' if st['gate'] else 'ran'}: {st['text']}",
                       incident=iid, result=st["result"])
        self.bus.publish("audit", self.audit.recent[0])
        if all(s["state"] != "pending" for s in inc["steps"]) and inc["status"] == "open":
            inc["status"] = "contained"
            self.audit.log("Beryl agent", f"{iid} playbook complete: contained", incident=iid)
            self.bus.publish("audit", self.audit.recent[0])
        self._pub_incident(inc)
        return st

    async def _do(self, inc, st) -> str:
        a, args = st["action"], st["args"]
        if a == "capture_iq":
            f, span = float(args["freq"]), float(args.get("span", 1e6))
            name = await self.capture_iq(f, span, 2.0, f"Evidence for {inc['id']}: {inc['title']}")
            return f"saved {name}.sigmf-data"
        if a == "task_df":
            fix = self.direction_find(inc)
            if fix:
                self.fixes[inc["id"]] = fix
                inc["geo"] = {"lat": fix["lat"], "lon": fix["lon"], "err_m": fix["err_m"]}
                self.bus.publish("fix", {"incident": inc["id"], **fix})
                return (f"{fix['method']} fix from {fix['nodes']} nodes: "
                        f"{fix['lat']:.4f}, {fix['lon']:.4f} (±{fix['err_m']} m)")
            return "direction-finding needs 3+ nodes reporting the same signal; not enough yet"
        if a == "aprs_bulletin":
            return await self.aprs.send_bulletin(args["text"])
        if a == "report":
            name = write_report(inc, self.names, Path(self.cfg["data_dir"]) / "captures",
                                self.cfg["site"], args.get("kind", "regulator"))
            return f"wrote {name} (send it from your own email; Orca does not send it)"
        return "logged"

    def direction_find(self, inc: dict) -> dict | None:
        """Locate an incident's emitter from node reports.

        With real hardware each node supplies a bearing (AoA) or burst time
        (TDOA). In the simulator we synthesise those from the known source so the
        console can show a located fix; the solver itself is the real one."""
        import math
        import numpy as np
        src = inc.get("geo") or {}
        slat = src.get("lat")
        nodes = [n for n in self.cfg["nodes"] if "lat" in n][:5]
        if slat is None or len(nodes) < 3:
            return None
        rng = np.random.default_rng(hash(inc["id"]) & 0xffff)
        reports = []
        from .df import C, ll_to_en
        for n in nodes:
            x1, y1 = ll_to_en(n["lat"], n["lon"], slat, src["lon"])
            brg = (math.degrees(math.atan2(-x1, -y1))) % 360 + rng.normal(0, 1.5)
            reports.append({"lat": n["lat"], "lon": n["lon"], "bearing_deg": brg})
        fix = aoa_fix(reports)
        return fix.to_dict() if fix else None

    def tropo_budget(self, params: dict) -> dict:
        b = tropo_mod.link_budget(
            freq_hz=float(params.get("freq", 144.2e6)),
            d_km=float(params.get("distance_km", 150)),
            ptx_w=float(params.get("power_w", 100)),
            gain_t_dbi=float(params.get("gain_t", 13)),
            gain_r_dbi=float(params.get("gain_r", 13)),
            h1_m=float(params.get("height_t", 30)),
            h2_m=float(params.get("height_r", 30)),
            mode=params.get("mode", "ssb"),
            nf_db=float(params.get("nf", 3)))
        return b.to_dict()

    def start_tropo(self, params: dict) -> dict:
        b = tropo_mod.link_budget(
            freq_hz=float(params.get("freq", 144.2e6)),
            d_km=float(params.get("distance_km", 150)),
            ptx_w=float(params.get("power_w", 100)),
            gain_t_dbi=float(params.get("gain_t", 13)),
            gain_r_dbi=float(params.get("gain_r", 13)),
            h1_m=float(params.get("height_t", 30)),
            h2_m=float(params.get("height_r", 30)),
            mode=params.get("mode", "ssb"))
        self.tropo = tropo_mod.TropoExperiment(
            params.get("name", "Troposcatter experiment"), b.freq_hz, b.d_km,
            params.get("mode", "ssb"), b)
        self.audit.log("Operator", f"started troposcatter experiment on {b.freq_hz/1e6:.3f} MHz "
                       f"over {b.d_km:.0f} km (predicted {b.verdict})")
        self.bus.publish("audit", self.audit.recent[0])
        return {"predicted": b.to_dict()}

    def stop_tropo(self):
        self.tropo = None

    async def tropo_loop(self):
        import numpy as np
        rng = np.random.default_rng()
        import time as _t
        while True:
            await asyncio.sleep(1.0)
            exp = self.tropo
            if exp is None:
                continue
            if hasattr(self.src, "modulated_iq") or not hasattr(self.src, "capture"):
                # simulation: realistic fading around the predicted median SNR
                pred = exp.predicted.snr_db if exp.predicted else 10.0
                snr = tropo_mod.sim_fading_snr(pred, _t.time(), rng)
            else:
                # real hardware: measure SNR at the experiment frequency
                from .spectrum import psd, noise_floor
                iq = await self.src.capture(exp.freq_hz, 250e3, 4096)
                f, db = psd(iq, 250e3, 1024)
                snr = float(db.max() - noise_floor(db))
            exp.add(_t.time(), snr)
            if self.mesh_radio is not None:
                self.mesh_radio.link_quality = tropo_mod.link_quality(snr, exp.threshold)
                self.mesh_radio.flush()        # path opened: send any queued bursts
            st = exp.stats()
            self.bus.publish("tropo", {"freq_hz": exp.freq_hz, "d_km": exp.d_km,
                                       "mode": exp.mode, "snr_db": round(snr, 1),
                                       "predicted": exp.predicted.to_dict() if exp.predicted else None,
                                       "stats": st.to_dict() if st else None})

    def classify_signal(self, freq: float, span: float = 0) -> dict:
        """Classify the modulation at a frequency and update the emitter library."""
        import numpy as np
        f = self.last_frame
        ems = (f or {}).get("emissions", [])
        em = min(ems, key=lambda e: abs(e["f_center"] - freq), default=None) if ems else None
        span = span or (max(em["bw_occ"] * 3, 50e3) if em else 200e3)
        rate = max(span * 1.25, 250e3)
        truth = None
        # average probabilities over a few windows: one snapshot is noisy
        import numpy as np
        probs = None
        last = None
        for _ in range(4):
            if hasattr(self.src, "modulated_iq"):          # simulator: true modulation
                iq, truth, _ = self.src.modulated_iq(freq, rate, 4096)
            elif hasattr(self.src, "iq"):
                iq = self.src.iq(freq, rate, 4096)
            else:
                return {"error": "live classification needs an IQ-capable source"}
            last = self.classifier.classify(iq, rate)
            p = np.array([last["probs"][c] for c in self.classifier.classes])
            probs = p if probs is None else probs + p
        probs /= 4
        i = int(probs.argmax())
        conf = float(probs[i])
        res = {"label": self.classifier.classes[i] if conf >= self.classifier.min_conf else "unknown",
               "top": self.classifier.classes[i], "confidence": round(conf, 3),
               "probs": {c: round(float(x), 3) for c, x in zip(self.classifier.classes, probs)},
               "features": last["features"]}
        if truth:
            res["sim_truth"] = truth
        if em:
            fp = self.emitters.observe(iq, rate, em["f_center"], em["bw_occ"],
                                       res["label"], self.node)
            res["emitter"] = fp
        self.audit.log("Beryl agent", f"classified {freq/1e6:.3f} MHz as {res['label']} "
                       f"({res['confidence']:.0%})")
        self.bus.publish("audit", self.audit.recent[0])
        return res

    async def capture_iq(self, f: float, span: float, seconds: float, note: str = "") -> str:
        recv = self.spectrum.receivers
        p = plan(f - span / 2, f + span / 2, recv, self.spectrum.frontends)
        if not p.segments:
            raise ValueError("no receiver covers that frequency")
        seg = p.segments[0]
        rate = min(RECEIVERS[seg.receiver].rate, max(250e3, span * 1.25))
        n = int(min(rate * seconds, 1 << 22))
        iq = await self.src.capture(f, rate, n, seg.frontend)
        anns = [e for e in (self.last_frame or {}).get("emissions", [])
                if f - rate / 2 <= e["f_center"] <= f + rate / 2]
        hw = RECEIVERS[seg.receiver].name + ("" if seg.frontend == "none" else f" + {FRONTENDS[seg.frontend].name}")
        name = self.captures.save_iq(iq, f, rate, time.time(), anns, note, hw)
        self.bus.publish("captures", self.captures.list())
        return name

    def set_view(self, lo: float, hi: float):
        lo, hi = max(0.0, lo), min(20e9, hi)
        if hi - lo < 10e3:
            c = (lo + hi) / 2
            lo, hi = c - 5e3, c + 5e3
        self.view = {"lo": lo, "hi": hi}
        self.bus.publish("view", self.view_info())

    def view_info(self) -> dict:
        p = plan(self.view["lo"], self.view["hi"], self.spectrum.receivers, self.spectrum.frontends)
        return {"type": "view", **self.view, "chains": describe_plan(p),
                "gaps": [{"lo": a, "hi": b} for a, b in p.gaps], "steps": len(p.segments)}

    # ------------------------------------------------------------ loops
    async def spectrum_loop(self):
        n = 0
        while True:
            t0 = time.time()
            if self.paused:
                await asyncio.sleep(0.2)
                continue
            lo, hi = self.view["lo"], self.view["hi"]
            try:
                fr = await self.spectrum.frame(lo, hi)
                key = f"{lo:.0f}:{hi:.0f}"
                z = self.baseline.score(key, fr.db, fr.ts)
                if z.max() < 4:          # don't learn anomalies into the baseline
                    self.baseline.update(key, fr.db, fr.ts)
                msg = fr.to_msg()
                msg["baseline_n"] = self.baseline.samples(key, fr.ts)
                msg["z"] = np.round(z, 1).tolist()
                self.last_frame = msg
                self.bus.publish("frame", msg)
                self._check_emissions(fr, z, lo, hi)
            except Exception as ex:   # hardware hiccup or bad input: report and keep going
                self.bus.publish("health", {"sdr_error": f"{ex.__class__.__name__}: {ex}"})
                await asyncio.sleep(1)
                continue
            n += 1
            if n % 300 == 0:
                await asyncio.get_running_loop().run_in_executor(None, self.baseline.save)
            await asyncio.sleep(max(0.02, 0.12 - (time.time() - t0)))

    def _throttled(self, key: str, every: float) -> bool:
        now = time.time()
        if now - self._throttle.get(key, 0) < every:
            return True
        self._throttle[key] = now
        return False

    def _check_emissions(self, fr, z, lo, hi):
        span = hi - lo
        thr = self.cfg["anomaly_z"]
        for e in fr.emissions:
            zi = float(z[min(BINS - 1, int((e.f_center - lo) / span * BINS))]) if span > 0 else 0
            if zi >= thr and not self._throttled(f"anom:{int(e.f_center // 50e3)}", 5):
                self.emit(Event("orca.spectrum", "rf.emission.anomaly", "low", self.node,
                                {**e.to_dict(), "z": round(zi, 1)}, confidence=min(0.95, zi / 12)))
        # protected-channel occupancy over a 30 s window
        now = time.time()
        for ch in self.cfg["protected"]:
            if ch["hi"] < lo or ch["lo"] > hi or (hi - lo) > 25e6:   # too coarse to resolve a channel
                continue
            hit = next((e for e in fr.emissions if ch["lo"] <= e.f_center <= ch["hi"] and e.snr_db > 15), None)
            dq = self._occ.setdefault(ch["name"], deque())
            dq.append((now, hit is not None))
            while dq and now - dq[0][0] > 30:
                dq.popleft()
            occ = sum(h for _, h in dq) / len(dq)
            if hit and len(dq) >= 40 and now - dq[0][0] > 25 and occ >= 0.95 \
                    and not self._throttled(f"prot:{ch['name']}", 5):
                self.emit(Event("orca.spectrum", "rf.emission", "medium", self.node,
                                {**hit.to_dict(), "channel": ch["name"], "occupancy": round(occ, 2),
                                 "steady": True}))

    async def health_loop(self):
        while True:
            now = time.time()
            while self._ev_times and now - self._ev_times[0] > 60:
                self._ev_times.popleft()
            self.bus.publish("health", {
                "adapters": {k: getattr(a, "health", "?") for k, a in self.adapters.items()},
                "sdr": self.cfg["sdr"].get("kind"), "scenarios": sorted(self.scenarios),
                "events_per_min": len(self._ev_times), "public_key": self.signer.public_hex,
                "uranus": self.adapters["uranus"].propagation(), "mesh": self.mesh_stats()})
            await asyncio.sleep(3)

    async def start(self):
        tasks = [asyncio.create_task(self.spectrum_loop()), asyncio.create_task(self.health_loop()),
                 asyncio.create_task(self.tropo_loop())]
        if self.mesh_radio is not None:
            tasks.append(asyncio.create_task(self.mesh_radio.run()))
        for a in self.adapters.values():
            tasks.append(asyncio.create_task(a.run(self.emit)))
        self.audit.log("Orca", f"node {self.node} started (Orca {__version__})")
        return tasks

    def snapshot(self) -> dict:
        return {"type": "hello", "version": __version__, "site": self.cfg["site"],
                "region": self.cfg["region_pack"], "nodes": self.cfg["nodes"],
                "bands": [b.__dict__ for b in BANDS], "protected": self.cfg["protected"],
                "view": self.view_info(),
                "incidents": [{k: v for k, v in i.items() if k != "ctx"} for i in self.rules.incidents.values()],
                "events": list(self.events)[-200:], "audit": self.audit.recent[:100],
                "captures": self.captures.list(), "scenarios": sorted(self.scenarios),
                "sim": self.cfg["sdr"]["kind"] == "sim",
                "transmit": dict(zip(("ok", "why"), self.aprs.can_transmit())),
                "intel": {"classes": self.classifier.classes, "decoders": ["clear-fsk"],
                          "emitters": self.emitters.list()[:30]},
                "mesh": self.mesh_stats(),
                "tropo": self.tropo_snapshot(),
                "chat": {"callsign": self.chat.callsign, "secure_available": self.secure is not None,
                         "log": self.chat.recent(60)},
                "secure": self.secure_status()}

    def mesh_stats(self) -> dict:
        s = self.mesh.stats()
        if self.mesh_radio is not None:
            s["transport"] = "radio (troposcatter/VHF/LoRa, KISS)"
            s["radio"] = {"sent": self.mesh_radio.sent, "recv": self.mesh_radio.recv,
                          "pending": self.mesh_radio.pending, "dropped_q": self.mesh_radio.dropped_q,
                          "link_quality": round(self.mesh_radio.link_quality, 2)}
        else:
            s["transport"] = "single node"
        return s

    def tropo_snapshot(self) -> dict | None:
        if self.tropo is None:
            return None
        st = self.tropo.stats()
        return {"name": self.tropo.name, "freq_hz": self.tropo.freq_hz, "d_km": self.tropo.d_km,
                "mode": self.tropo.mode, "predicted": self.tropo.predicted.to_dict() if self.tropo.predicted else None,
                "stats": st.to_dict() if st else None}

    def secure_status(self) -> dict:
        if not self.secure:
            return {"enabled": False,
                    "note": "Encrypts and decrypts the operator's own communications on licensed, "
                            "non-amateur frequencies above the floor; it does not defeat third-party "
                            "encryption."}
        p = self.secure["policy"]
        return {"enabled": True, "authority": p.authority, "floor_mhz": p.floor_hz / 1e6,
                "licensed_bands": [[lo, hi] for lo, hi in p.licensed_bands],
                "keys": len(self.keystore.ids()), "decoders": self.secure["bank"].decoders(),
                "boundary": "own-comms crypto only; amateur bands excluded; no third-party decryption"}


def create_app(cfg: dict) -> FastAPI:
    orca = Orca(cfg)

    @asynccontextmanager
    async def life(app):
        tasks = await orca.start()
        yield
        for t in tasks:
            t.cancel()
        orca.baseline.save()
        orca.src.close()

    app = FastAPI(title="Orca", version=__version__, lifespan=life)
    app.state.orca = orca

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return CONSOLE.read_text()

    @app.get("/api/state")
    async def state():
        return orca.snapshot()

    @app.get("/api/coverage")
    async def coverage(lo: float = 0, hi: float = 18e9):
        p = plan(lo, hi, orca.spectrum.receivers, orca.spectrum.frontends)
        return {"chains": describe_plan(p), "gaps": p.gaps}

    @app.get("/api/captures")
    async def captures():
        return orca.captures.list()

    @app.get("/api/captures/{name}")
    async def get_capture(name: str):
        try:
            return FileResponse(orca.captures.path(name), filename=name)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404)

    @app.post("/api/capture/screen")
    async def screen(body: dict):
        try:
            name = orca.captures.save_screen(body.get("png", ""), body.get("meta", {}))
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        orca.audit.log("Operator", f"screen capture {name}")
        orca.bus.publish("audit", orca.audit.recent[0])
        orca.bus.publish("captures", orca.captures.list())
        return {"name": name}

    async def command(msg: dict) -> dict:
        c = msg.get("cmd")
        if c == "view":
            orca.set_view(float(msg["lo"]), float(msg["hi"]))
            return {"ok": True}
        if c == "capture_iq":
            name = await orca.capture_iq(float(msg["freq"]), float(msg.get("span", 1e6)),
                                         float(msg.get("seconds", 1.0)), msg.get("note", "operator capture"))
            orca.audit.log("Operator", f"IQ capture {name}")
            orca.bus.publish("audit", orca.audit.recent[0])
            return {"ok": True, "name": name}
        if c == "capture_spectrum":
            if not orca.last_frame:
                return {"ok": False}
            name = orca.captures.save_spectrum(orca.last_frame)
            orca.bus.publish("captures", orca.captures.list())
            return {"ok": True, "name": name}
        if c in ("approve", "skip"):
            st = await orca.run_step(msg["incident"], int(msg["step"]), "Operator", skip=(c == "skip"))
            return {"ok": True, "step": st}
        if c == "close":
            inc = orca.rules.incidents[msg["incident"]]
            inc["status"] = "closed"
            orca.audit.log("Operator", f"closed {inc['id']}")
            orca.bus.publish("audit", orca.audit.recent[0])
            orca._pub_incident(inc)
            return {"ok": True}
        if c == "scenario" and cfg["sdr"]["kind"] == "sim":
            (orca.scenarios.add if msg.get("on") else orca.scenarios.discard)(msg["name"])
            orca.audit.log("Operator", f"simulator scenario {msg['name']} {'on' if msg.get('on') else 'off'}")
            orca.bus.publish("audit", orca.audit.recent[0])
            return {"ok": True}
        if c == "pause":
            orca.paused = bool(msg.get("on"))
            return {"ok": True}
        if c == "classify":
            f = float(msg.get("freq") or (orca.view["lo"] + orca.view["hi"]) / 2)
            return {"ok": True, "result": orca.classify_signal(f, float(msg.get("span", 0)))}
        if c == "tropo_budget":
            return {"ok": True, "budget": orca.tropo_budget(msg)}
        if c == "tropo_start":
            return {"ok": True, **orca.start_tropo(msg)}
        if c == "tropo_stop":
            orca.stop_tropo()
            return {"ok": True}
        if c == "chat_send":
            freq = float(msg.get("freq") or (orca.tropo.freq_hz if orca.tropo else 144.2e6))
            res = orca.chat.send(str(msg.get("text", "")), freq, bool(msg.get("secure")),
                                 msg.get("key_id"))
            if res.get("ok"):
                orca.bus.publish("chat", orca.chat.recent(1)[0])
            return res
        if c == "label_emitter":
            return {"ok": orca.emitters.label(msg["id"], msg["label"])}
        if c == "df":
            inc = orca.rules.incidents.get(msg["incident"])
            if not inc:
                return {"ok": False, "error": "no such incident"}
            fix = orca.direction_find(inc)
            if fix:
                orca.fixes[inc["id"]] = fix
                inc["geo"] = {"lat": fix["lat"], "lon": fix["lon"], "err_m": fix["err_m"]}
                orca._pub_incident(inc)
            return {"ok": bool(fix), "fix": fix}
        if c == "secure_decode":
            if not orca.secure:
                return {"ok": False, "error": "secure module not enabled"}
            import numpy as np
            freq = float(msg["freq"])
            span = float(msg.get("span", 50e3))
            if not hasattr(orca.src, "iq"):
                return {"ok": False, "error": "needs an IQ-capable source"}
            iq = orca.src.iq(freq, max(span * 1.25, 250e3), 8192)
            res = orca.secure["bank"].decode(freq, iq, max(span * 1.25, 250e3), msg.get("want", "decrypt"))
            orca.audit.log("Operator", f"secure decode {freq/1e6:.3f} MHz: "
                           f"{'allowed' if res.get('allowed') else res.get('reason')}",
                           authority=orca.secure["policy"].authority)
            orca.bus.publish("audit", orca.audit.recent[0])
            return {"ok": True, "result": res}
        return {"ok": False, "error": "unknown command"}

    @app.post("/api/command")
    async def http_command(msg: dict):
        try:
            return await command(msg)
        except (KeyError, ValueError, PermissionError, StopIteration) as ex:
            raise HTTPException(400, str(ex))

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q = orca.bus.subscribe(maxsize=200)
        await sock.send_text(json.dumps(orca.snapshot()))

        async def pump():
            last_frame = 0.0
            while True:
                topic, msg = await q.get()
                if topic == "frame":
                    if time.time() - last_frame < 0.1:
                        continue
                    last_frame = time.time()
                    await sock.send_text(json.dumps(msg))
                else:
                    await sock.send_text(json.dumps({"type": topic, "data": msg}))

        pt = asyncio.create_task(pump())
        try:
            while True:
                msg = json.loads(await sock.receive_text())
                try:
                    res = await command(msg)
                except Exception as ex:
                    res = {"ok": False, "error": str(ex)}
                await sock.send_text(json.dumps({"type": "result", "req": msg.get("req"), "data": res}))
        except WebSocketDisconnect:
            pass
        finally:
            pt.cancel()
            orca.bus.unsubscribe(q)

    return app
