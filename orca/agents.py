"""Beryl agents: triage incidents, run safe steps, and hold consequential steps for a human.

Rule: a step with gate=True (anything that transmits, notifies outside parties or
blocks devices) only runs after an operator approves it. Everything is audited.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .oef import dumps


class Audit:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.recent: list[dict] = []

    def log(self, actor: str, what: str, **extra) -> dict:
        rec = {"ts": time.time(), "actor": actor, "what": what, **extra}
        with self.path.open("a") as f:
            f.write(dumps(rec) + "\n")
        self.recent = ([rec] + self.recent)[:200]
        return rec


def triage_text(inc: dict, names: dict[str, str]) -> str:
    """Deterministic plain-language summary built only from the evidence."""
    ev = inc["evidence"]
    where = ", ".join(names.get(n, n) for n in inc["nodes"]) or "unknown"
    r = inc["rule"]
    if r == "gnss_jam":
        jams = [e for e in ev if e["class"] == "rf.gnss.jamming"]
        drops = [e["data"].get("drop_db", 0) for e in jams]
        txt = (f"GNSS C/N0 fell up to {max(drops):.0f} dB below its expected level on "
               f"{len(inc['nodes'])} node(s): {where}. ")
        txt += ("Several nodes affected at once points to a local jammer rather than a receiver fault. "
                if len(inc["nodes"]) > 1 else "One node only so far; a receiver or antenna fault is still possible. ")
        auth = [e for e in ev if e["class"].startswith("net.")]
        if auth:
            txt += (f"{len(auth)} network security event(s) arrived in the same window, which is "
                    "consistent with jamming used as cover for an intrusion attempt. ")
        txt += f"Confidence {max((e.get('confidence') or 0) for e in jams):.2f}."
        return txt
    if r == "gnss_spoof":
        return (f"Position or signal pattern on {where} is not physically plausible "
                f"({ev[-1]['data']}). Treat GNSS time and position from this node as untrusted.")
    if r == "protected":
        d = ev[-1]["data"]
        return (f"A signal has occupied {inc['ctx']['channel']} almost continuously for the whole "
                f"observation window ({d.get('occupancy', 0) * 100:.0f} % of frames, "
                f"{d.get('snr_db', 0):.0f} dB SNR, {d.get('bw_occ', 0) / 1e3:.1f} kHz wide). Normal traffic "
                "is bursty, so this looks like a stuck transmitter or interference.")
    if r == "new_emitter":
        d = ev[-1]["data"]
        return (f"A signal at {d['f_center'] / 1e6:.3f} MHz, {d.get('bw_occ', 0) / 1e3:.1f} kHz wide, is "
                f"{d.get('z', 0):.0f} standard deviations above this site's learnt baseline for this hour. "
                "It does not match the emitter library.")
    if r == "beacon":
        d = ev[-1]["data"]
        return (f"{d['call']} has been silent for {d['silent_s'] // 60} min, over three times its "
                "normal beacon interval. Possible power or equipment failure at the site.")
    return "No summary for this rule."


async def llm_triage(inc: dict, base_text: str, cfg: dict) -> str | None:
    """Optional: ask Claude for a richer summary. Off unless configured with an API key."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not (cfg.get("enabled") and key):
        return None
    import httpx
    prompt = ("You are Beryl, the triage agent in Orca, a spectrum and security operations console. "
              "Write a 3-sentence triage note for the operator: what happened, how sure we are, and the "
              "most important next action. Use only the evidence given; do not invent facts.\n\n"
              f"Draft: {base_text}\n\nIncident JSON: {json.dumps({k: inc[k] for k in ('title', 'severity', 'nodes', 'evidence')})[:12000]}")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             json={"model": cfg.get("model", "claude-sonnet-5-5"), "max_tokens": 400,
                                   "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            return r.json()["content"][0]["text"].strip()
    except Exception:
        return None


def write_report(inc: dict, names: dict, root: Path, site: dict, kind: str = "regulator") -> str:
    root.mkdir(parents=True, exist_ok=True)
    name = f"{inc['id']}_{kind}_report.md"
    t = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(inc["created"]))
    lines = [f"# {inc['title']}", "",
             f"- Incident: {inc['id']} ({inc['severity']})",
             f"- First detected: {t}",
             f"- Site: {site.get('name', '')}, nodes: {', '.join(names.get(n, n) for n in inc['nodes'])}",
             f"- Location estimate: {inc.get('geo') or 'not available'}", "",
             "## Summary", "", inc.get("summary", ""), "",
             "## Evidence (most recent first)", "", "| Time (UTC) | Source | Class | Detail |", "|---|---|---|---|"]
    for e in reversed(inc["evidence"][-15:]):
        lines.append(f"| {time.strftime('%H:%M:%S', time.gmtime(e['ts']))} | {e['source']} | {e['class']} | "
                     f"{json.dumps(e['data'])[:120]} |")
    lines += ["", "Every event above is signed with the node's Ed25519 key; "
              "attached IQ recordings are in SigMF format.", "",
              "Prepared by Orca. Reviewed and approved by the operator before sending."]
    (root / name).write_text("\n".join(lines), encoding="utf-8")
    return name
