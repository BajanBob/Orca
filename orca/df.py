"""Direction finding: locate an emitter from several nodes.

Two methods, both least-squares so they work with more nodes than the minimum
and degrade gracefully with noise:

- Angle of arrival (AoA): each node reports a bearing to the emitter. Lines are
  intersected for the maximum-likelihood crossing point.
- Time difference of arrival (TDOA): nodes report the arrival time of the same
  burst. Hyperbolic multilateration by Gauss-Newton from a centroid start.

Positions are in metres on a local east-north plane; helpers convert to and from
latitude/longitude for the map. This replaces the Phase 2 "task_df" stub.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

C = 299_792_458.0
_R = 6_371_000.0


def ll_to_en(lat, lon, lat0, lon0):
    x = math.radians(lon - lon0) * _R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * _R
    return x, y


def en_to_ll(x, y, lat0, lon0):
    lat = lat0 + math.degrees(y / _R)
    lon = lon0 + math.degrees(x / (_R * math.cos(math.radians(lat0))))
    return lat, lon


@dataclass
class Fix:
    lat: float
    lon: float
    err_m: float
    method: str
    nodes: int
    residual: float

    def to_dict(self):
        return {"lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "err_m": round(self.err_m), "method": self.method,
                "nodes": self.nodes, "residual": round(self.residual, 2)}


def aoa_fix(nodes: list[dict]) -> Fix | None:
    """nodes: [{lat, lon, bearing_deg}]. Bearing is degrees clockwise from north."""
    if len(nodes) < 2:
        return None
    lat0, lon0 = nodes[0]["lat"], nodes[0]["lon"]
    A, b = [], []
    for n in nodes:
        x, y = ll_to_en(n["lat"], n["lon"], lat0, lon0)
        th = math.radians(n["bearing_deg"])
        # unit vector along the bearing; the perpendicular (a) gives the line a·p = a·node
        ax, ay = math.cos(th), -math.sin(th)      # normal to the bearing direction
        A.append([ax, ay]); b.append(ax * x + ay * y)
    A, b = np.array(A), np.array(b)
    sol, res, *_ = np.linalg.lstsq(A, b, rcond=None)
    # error estimate from the spread of pairwise intersections
    resid = float(np.sqrt(np.mean((A @ sol - b) ** 2)))
    lat, lon = en_to_ll(sol[0], sol[1], lat0, lon0)
    return Fix(lat, lon, max(50.0, resid * 1.5), "AoA", len(nodes), resid)


def tdoa_fix(nodes: list[dict], iters: int = 30) -> Fix | None:
    """nodes: [{lat, lon, t}] where t is the burst arrival time in seconds.
    Needs >= 3 nodes. Solves for position (and a common time offset)."""
    if len(nodes) < 3:
        return None
    lat0, lon0 = nodes[0]["lat"], nodes[0]["lon"]
    P = np.array([ll_to_en(n["lat"], n["lon"], lat0, lon0) for n in nodes], float)
    t = np.array([n["t"] for n in nodes], float)
    t = t - t.min()
    p = P.mean(axis=0).astype(float)                   # start at the centroid
    tau = 0.0                                          # common emission time offset
    for _ in range(iters):
        d = np.linalg.norm(P - p, axis=1) + 1e-6
        pred = d / C + tau
        r = pred - t
        J = np.zeros((len(nodes), 3))
        J[:, 0] = (p[0] - P[:, 0]) / d / C
        J[:, 1] = (p[1] - P[:, 1]) / d / C
        J[:, 2] = 1.0
        try:
            step, *_ = np.linalg.lstsq(J, -r, rcond=None)
        except np.linalg.LinAlgError:
            break
        p = p + step[:2]
        tau += step[2]
        if np.linalg.norm(step[:2]) < 0.5:
            break
    resid = float(np.sqrt(np.mean(((np.linalg.norm(P - p, axis=1) / C + tau) - t) ** 2)) * C)
    lat, lon = en_to_ll(p[0], p[1], lat0, lon0)
    return Fix(lat, lon, max(50.0, resid), "TDOA", len(nodes), resid)
