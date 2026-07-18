#!/usr/bin/env python3
"""One-off cleanup: delete every "noise"-labeled sample uploaded by
collect-noise.py, identified by its timestamp-based filename pattern
(YYYY-MM-DD-HH-MM-SS-noise), leaving any other "noise" samples in the
project (e.g. ones recorded manually via the dashboard) untouched.

Meant to be run once after fixing a bug in collect-noise.py that baked a
recording-startup pop into every clip -- lets you wipe the contaminated
batches before re-collecting with the fixed script.

Usage: python3 cleanup-noise-samples.py [--yes]
"""
import re
import sys

import requests

import config as cfg

EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"
LABEL = "noise"
FILENAME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-noise$")
PAGE_SIZE = 100


def ei_headers():
    return {"x-api-key": cfg.EI_API_KEY}


def ei_admin_headers():
    return {"x-api-key": cfg.EI_ADMIN_API_KEY}


def list_matching_samples():
    matches = []
    offset = 0
    while True:
        response = requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data",
            headers=ei_headers(),
            params={"category": "all", "labels": f'["{LABEL}"]', "limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            raise RuntimeError(data.get("error", "Edge Impulse API call failed"))
        page = data.get("samples", [])
        matches.extend(s for s in page if FILENAME_PATTERN.match(s.get("filename", "")))
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return matches


def delete_sample(sample_id):
    return requests.delete(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample_id}",
        headers=ei_admin_headers(),
        timeout=15,
    )


def main():
    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        print("EI_API_KEY is not configured in config.py. Aborting.")
        sys.exit(1)
    if not getattr(cfg, "EI_ADMIN_API_KEY", None) or cfg.EI_ADMIN_API_KEY == "your-edge-impulse-admin-api-key-here":
        print("EI_ADMIN_API_KEY is not configured in config.py (deleting samples requires an Admin-role key). Aborting.")
        sys.exit(1)

    print("Looking up noise samples uploaded by collect-noise.py...")
    matches = list_matching_samples()
    if not matches:
        print("No matching samples found. Nothing to delete.")
        return

    print(f"Found {len(matches)} samples to delete:")
    for s in matches:
        print(f"  - {s['filename']} (id {s['id']})")

    if "--yes" not in sys.argv:
        confirm = input(f"\nDelete all {len(matches)} samples above? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted, nothing deleted.")
            return

    deleted = 0
    failed = 0
    for s in matches:
        response = delete_sample(s["id"])
        if response.status_code >= 300:
            print(f"Failed to delete {s['filename']} (id {s['id']}): {response.status_code} {response.text[:200]}")
            failed += 1
        else:
            deleted += 1

    print(f"Done. {deleted} deleted, {failed} failed.")


if __name__ == "__main__":
    main()
