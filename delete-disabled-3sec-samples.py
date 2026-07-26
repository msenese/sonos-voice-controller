#!/usr/bin/env python3
"""One-time follow-up to fix-3sec-samples.py: deletes the 61 originals
that script disabled (rather than deleted, for rollback safety) once
their 1.3s replacement segments were confirmed uploaded. Rollback
safety is no longer wanted, so this removes them outright.

Targets samples that are BOTH disabled AND longer than ~2 seconds --
the same two conditions fix-3sec-samples.py used to find them in the
first place -- rather than a hardcoded list of IDs, so this stays
correct if re-run.

Usage: python3 delete-disabled-3sec-samples.py [--yes]
"""
import sys

import requests

import config as cfg

EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"
PAGE_SIZE = 100
MIN_LENGTH_MS = 2000


def ei_headers():
    return {"x-api-key": cfg.EI_API_KEY}


def ei_admin_headers():
    return {"x-api-key": cfg.EI_ADMIN_API_KEY}


def list_disabled_long_samples():
    matches = []
    offset = 0
    while True:
        r = requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data",
            headers=ei_headers(),
            params={"category": "all", "limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        page = r.json().get("samples", [])
        for s in page:
            if s.get("totalLengthMs", 0) > MIN_LENGTH_MS and s.get("isDisabled"):
                matches.append(s)
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
    if not getattr(cfg, "EI_ADMIN_API_KEY", None) or cfg.EI_ADMIN_API_KEY == "your-edge-impulse-admin-api-key-here":
        print("EI_ADMIN_API_KEY is not configured in config.py (deleting samples requires an Admin-role key). Aborting.")
        sys.exit(1)

    print(f"Looking up disabled samples longer than {MIN_LENGTH_MS}ms, all labels, project-wide...")
    samples = list_disabled_long_samples()

    if not samples:
        print("No matching samples found. Nothing to do.")
        return

    print(f"\nFound {len(samples)} disabled samples to delete:")
    for s in samples:
        print(f"  - [{s['id']}] [{s['label']}] {s['filename']} ({s['totalLengthMs']}ms)")

    if "--yes" not in sys.argv:
        confirm = input(f"\nPermanently delete all {len(samples)} samples above? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted, nothing deleted.")
            return

    deleted = failed = 0
    for s in samples:
        r = delete_sample(s["id"])
        if r.status_code >= 300:
            print(f"  FAILED to delete [{s['id']}] {s['filename']}: HTTP {r.status_code} {r.text[:200]}")
            failed += 1
        else:
            print(f"  Deleted [{s['id']}] {s['filename']}")
            deleted += 1

    print(f"\nDone. {deleted} deleted, {failed} failed.")


if __name__ == "__main__":
    main()
