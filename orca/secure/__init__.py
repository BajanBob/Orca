"""Orca secure module (government / defence commercial edition).

Provides encryption and decryption of the operator's OWN communications on
licensed, non-amateur frequencies above the configured floor, plus a pluggable
signal-decoder framework. It does not defeat third-party encryption; see
decoder.py and policy.py for the boundaries enforced in code.
"""
from .crypto import KeyStore, SecureChannel, key_id  # noqa: F401
from .decoder import ClearFskDecoder, DecoderBank, fsk_demod  # noqa: F401
from .policy import SecurePolicy  # noqa: F401
