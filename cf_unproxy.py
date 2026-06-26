#!/usr/bin/env python3
"""
Cloudflare Unproxy
==================
For every domain in the input file, disables the Cloudflare proxy (orange cloud)
on all DNS records that currently have it enabled.

Usage:
    python cf_unproxy.py domains.txt --token <CF_API_TOKEN>
    python cf_unproxy.py domains.txt --token <CF_API_TOKEN> --dry-run

Requirements:
    pip install requests
"""

import argparse
import sys
import time
from pathlib import Path

import requests


CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"

# Only these record types support proxying; others are always DNS-only.
PROXYABLE_TYPES = {"A", "AAAA", "CNAME"}


class CloudflareClient:
    def __init__(self, api_token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )

    def _get(self, path: str, params: dict = None) -> dict:
        resp = self.session.get(f"{CLOUDFLARE_API}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: dict) -> dict:
        resp = self.session.patch(f"{CLOUDFLARE_API}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_zone_id(self, domain: str) -> str | None:
        """Return the zone ID for an exact domain name, or None if not found."""
        data = self._get("/zones", params={"name": domain, "per_page": 1})
        if not data.get("success"):
            raise RuntimeError(f"Zone lookup failed for '{domain}': {data.get('errors')}")
        result = data.get("result", [])
        return result[0]["id"] if result else None

    def list_dns_records(self, zone_id: str) -> list[dict]:
        """Return all DNS records for a zone (handles pagination)."""
        records: list[dict] = []
        page = 1
        while True:
            data = self._get(
                f"/zones/{zone_id}/dns_records",
                params={"per_page": 100, "page": page},
            )
            if not data.get("success"):
                raise RuntimeError(f"Failed to list DNS records: {data.get('errors')}")
            records.extend(data["result"])
            info = data.get("result_info", {})
            if page >= info.get("total_pages", 1):
                break
            page += 1
        return records

    def disable_proxy(self, zone_id: str, record: dict) -> dict:
        """Set proxied=False on a single DNS record using PATCH."""
        data = self._patch(
            f"/zones/{zone_id}/dns_records/{record['id']}",
            payload={"proxied": False},
        )
        if not data.get("success"):
            raise RuntimeError(
                f"Failed to update record '{record['name']}': {data.get('errors')}"
            )
        return data["result"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_domains(filepath: str) -> list[str]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    domains = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        domain = raw.strip().lower()
        if domain and not domain.startswith("#"):
            domains.append(domain)
    if not domains:
        raise ValueError(f"No domains found in {filepath}")
    return domains


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove Cloudflare proxy from all DNS records for a list of domains."
    )
    parser.add_argument("domains_file", help="Text file — one domain per line")
    parser.add_argument(
        "--token",
        required=True,
        help="Cloudflare API token (needs Zone:Read + DNS:Edit permissions)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without making any API calls",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Seconds to wait between API calls (default: 0.3)",
    )
    args = parser.parse_args()

    try:
        domains = load_domains(args.domains_file)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    if args.dry_run:
        print("*** DRY RUN — no changes will be made ***\n")

    cf = CloudflareClient(args.token)

    total_updated = 0
    total_skipped = 0
    total_errors = 0

    for idx, domain in enumerate(domains, start=1):
        print(f"[{idx}/{len(domains)}] {domain}")

        # --- Find zone ---
        try:
            zone_id = cf.get_zone_id(domain)
        except (requests.HTTPError, RuntimeError) as exc:
            print(f"  ERROR looking up zone: {exc}")
            total_errors += 1
            continue

        if zone_id is None:
            print("  Zone not found in this account — skipping.")
            total_skipped += 1
            continue

        # --- List DNS records ---
        try:
            records = cf.list_dns_records(zone_id)
        except (requests.HTTPError, RuntimeError) as exc:
            print(f"  ERROR listing DNS records: {exc}")
            total_errors += 1
            continue

        proxied = [r for r in records if r.get("proxied") and r.get("type") in PROXYABLE_TYPES]

        if not proxied:
            print(f"  No proxied records found ({len(records)} record(s) total).")
            total_skipped += 1
            continue

        print(f"  {len(proxied)} proxied record(s) found (out of {len(records)} total):")

        for rec in proxied:
            label = f"    {rec['type']:6} {rec['name']:45} → {rec['content']}"

            if args.dry_run:
                print(f"{label}  [would unproxy]")
                total_updated += 1
                continue

            try:
                cf.disable_proxy(zone_id, rec)
                print(f"{label}  [unproxied]")
                total_updated += 1
            except (requests.HTTPError, RuntimeError) as exc:
                print(f"{label}  [ERROR: {exc}]")
                total_errors += 1

            time.sleep(args.delay)

        time.sleep(args.delay)

    # --- Summary ---
    action = "Would update" if args.dry_run else "Updated"
    print(f"\nDone.")
    print(f"  {action}  : {total_updated} record(s)")
    print(f"  Skipped  : {total_skipped} domain(s) (no proxied records or zone not found)")
    print(f"  Errors   : {total_errors}")


if __name__ == "__main__":
    main()
