"""On-device modulation classifier: a small neural net in NumPy.

Deliberately tiny (one hidden layer) so it trains in a second on synthetic data
and runs with no GPU, no PyTorch and no cloud. It reports a class and a
confidence; low confidence is surfaced as "unknown" rather than a wrong guess.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .features import FEATURE_NAMES, extract
from .synth import CLASSES, dataset


class MLP:
    """Input -> ReLU hidden -> softmax. Standardises inputs with stored mean/std."""

    def __init__(self, n_in: int, n_hidden: int, n_out: int, seed: int = 0):
        rng = np.random.default_rng(seed)
        self.W1 = rng.standard_normal((n_in, n_hidden)) * np.sqrt(2 / n_in)
        self.b1 = np.zeros(n_hidden)
        self.W2 = rng.standard_normal((n_hidden, n_out)) * np.sqrt(2 / n_hidden)
        self.b2 = np.zeros(n_out)
        self.mu = np.zeros(n_in)
        self.sd = np.ones(n_in)

    def _fwd(self, X):
        h = np.maximum(0, X @ self.W1 + self.b1)
        z = h @ self.W2 + self.b2
        z -= z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return h, e / e.sum(axis=1, keepdims=True)

    def fit(self, X, y, epochs=300, lr=0.05, l2=1e-4):
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-6
        Xs = (X - self.mu) / self.sd
        Y = np.eye(self.W2.shape[1])[y]
        n = len(X)
        for _ in range(epochs):
            h, p = self._fwd(Xs)
            g = (p - Y) / n
            gW2 = h.T @ g + l2 * self.W2
            gb2 = g.sum(0)
            gh = (g @ self.W2.T) * (h > 0)
            gW1 = Xs.T @ gh + l2 * self.W1
            gb1 = gh.sum(0)
            self.W2 -= lr * gW2; self.b2 -= lr * gb2
            self.W1 -= lr * gW1; self.b1 -= lr * gb1
        return self

    def predict_proba(self, X):
        Xs = (np.atleast_2d(X) - self.mu) / self.sd
        return self._fwd(Xs)[1]

    def to_dict(self):
        return {k: getattr(self, k).tolist() for k in ("W1", "b1", "W2", "b2", "mu", "sd")}

    @classmethod
    def from_dict(cls, d):
        m = cls.__new__(cls)
        for k, v in d.items():
            setattr(m, k, np.array(v))
        return m


class Classifier:
    def __init__(self, model: MLP, classes=CLASSES, min_conf=0.5):
        self.model, self.classes, self.min_conf = model, classes, min_conf

    def classify(self, iq, rate: float) -> dict:
        feats = extract(iq, rate)
        p = self.model.predict_proba(feats)[0]
        i = int(p.argmax())
        conf = float(p[i])
        label = self.classes[i] if conf >= self.min_conf else "unknown"
        return {"label": label, "top": self.classes[i], "confidence": round(conf, 3),
                "probs": {c: round(float(x), 3) for c, x in zip(self.classes, p)},
                "features": dict(zip(FEATURE_NAMES, [round(float(x), 4) for x in feats]))}

    def save(self, path):
        Path(path).write_text(json.dumps({"classes": self.classes, "model": self.model.to_dict()}))

    @classmethod
    def load_or_train(cls, path=None, rate=2.0e6, n=4096, per_class=120, seed=0):
        if path and Path(path).exists():
            d = json.loads(Path(path).read_text())
            return cls(MLP.from_dict(d["model"]), d["classes"])
        X, y = dataset(per_class, n, rate, seed)
        model = MLP(X.shape[1], 32, len(CLASSES), seed).fit(X, y, epochs=450, lr=0.08)
        c = cls(model)
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            c.save(path)
        return c
