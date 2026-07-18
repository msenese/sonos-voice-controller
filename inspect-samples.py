#!/usr/bin/env python3
"""Read-only inspection: print the raw JSON for a few samples of a given
label, so we can see what date/metadata fields Edge Impulse actually
exposes before writing any delete logic based on them.

Usage: python3 inspect-samples.py <label> [count]
"""
import json
import sys

import requests

import config as cfg

EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 inspect-samples.py <label> [count]")
        sys.exit(1)
    label = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    response = requests.get(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data",
        headers={"x-api-key": cfg.EI_API_KEY},
        params={"category": "all", "labels": f'["{label}"]', "limit": count, "offset": 0},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    samples = data.get("samples", [])
    print(f"Got {len(samples)} samples for label {label!r} (showing raw fields):\n")
    for s in samples:
        print(json.dumps(s, indent=2, default=str))
        print("---")


if __name__ == "__main__":
    main()
