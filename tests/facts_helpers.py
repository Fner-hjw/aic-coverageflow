"""Helpers for building contract-shaped fact dicts in tests."""

import hashlib
import json


def fact(field: str, period: str, value, idx: int = 0, confidence: float = 0.99) -> dict:
    """One facts.json fact entry per docs/contracts/state-and-artifacts.md section 5."""
    return {
        "field": field,
        "period": period,
        "value": value,
        "unit": "USDmm",
        "confidence": confidence,
        "citation": {
            "doc_id": "mini-10q",
            "slice_id": "mini-10q/IS",
            "anchor": "Consolidated Statements of Operations",
            "locator": f"line {idx}",
        },
    }


def payload_sha256(payload: dict) -> str:
    blob = json.dumps(payload["facts"], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
