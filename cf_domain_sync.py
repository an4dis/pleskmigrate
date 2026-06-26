#!/usr/bin/env python3
"""
Cloudflare Domain Sync
======================
Reads domains from a text file, checks if they exist in the Cloudflare account,
creates missing ones on the free plan with DNS auto-scan, and exports each
domain with its assigned nameservers to a CSV file.

Usage:
    python cf_domain_sync.py domains.txt --token <CF_API_TOKEN>
    python cf_domain_sync.py domains.txt --token <CF_API_TOKEN> --output results.csv

Requirements:
    pip install requests
"""

import argparse
import csv
import sys
import time
from pathlib import Path

import requests


CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"


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

    def _post(self, path: str, payload: dict = None) -> dict:
        resp = self.session.post(f"{CLOUDFLARE_API}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account_id(self) -> str:
        """
        Try /accounts first. If the token lacks Account:Read permission,
        fall back to reading the account ID from an existing zone.
        Raises RuntimeError if neither method works.
        """
        try:
            data = self._get("/accounts", params={"per_page": 1})
            if data.get("success") and data.get("result"):
                return data["result"][0]["id"]
        except requests.HTTPError:
            pass  # token may not have Account:Read — try zones fallback

        # Fallback: grab account ID from any existing zone
        try:
            data = self._get("/zones", params={"per_page": 1})
            if data.get("success") and data.get("result"):
                account = data["result"][0].get("account", {})
                if account.get("id"):
                    return account["id"]
        except requests.HTTPError:
            pass

        raise RuntimeError(
            "Could not determine account ID.\n"
            "  • Make sure your token has Zone:Read + Zone:Edit + DNS:Edit permissions.\n"
            "  • Or pass --account-id directly to skip auto-detection."
        )

    # ------------------------------------------------------------------
    # Zones
    # ------------------------------------------------------------------

    def list_all_zones(self) -> list[dict]:
        """Return every zone in the account (handles pagination)."""
        zones: list[dict] = []
        page = 1
        while True:
            data = self._get("/zones", params={"per_page": 50, "page": page})
            if not data.get("success"):
                raise RuntimeError(f"Failed to list zones: {data.get('errors')}")
            zones.extend(data["result"])
            info = data.get("result_info", {})
            if page >= info.get("total_pages", 1):
                break
            page += 1
        return zones

    def get_zone(self, domain: str) -> dict | None:
        """Return a zone by exact name, or None if not found."""
        data = self._get("/zones", params={"name": domain, "per_page": 1})
        if not data.get("success"):
            raise RuntimeError(f"Failed to query zone '{domain}': {data.get('errors')}")
        result = data.get("result", [])
        return result[0] if result else None

    def create_zone(self, domain: str, account_id: str) -> dict:
        """
        Create a zone on the free plan.
        jump_start=True tells Cloudflare to automatically scan and import
        existing DNS records (same as 'Quick Scan' in the dashboard).
        """
        payload = {
            "name": domain,
            "account": {"id": account_id},
            "jump_start": True,  # triggers automatic DNS import scan
        }
        data = self._post("/zones", payload=payload)
        if not data.get("success"):
            errors = data.get("errors", [])
            raise RuntimeError(f"Failed to create zone '{domain}': {errors}")
        return data["result"]

    def scan_dns(self, zone_id: str) -> bool:
        """
        Trigger a DNS scan on an existing zone.
        Called after creation when jump_start may have already done this,
        but also useful for zones that were just added.
        """
        try:
            data = self._post(f"/zones/{zone_id}/dns_records/scan")
            return bool(data.get("success"))
        except requests.HTTPError as exc:
            print(f"    Warning: DNS scan request failed — {exc}")
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_domains(filepath: str) -> list[str]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Domains file not found: {filepath}")
    domains = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        domain = raw.strip().lower()
        if domain and not domain.startswith("#"):
            domains.append(domain)
    if not domains:
        raise ValueError(f"No domains found in {filepath}")
    return domains


def write_csv(results: list[dict], output_path: str) -> None:
    fieldnames = ["domain", "status", "ns1", "ns2", "all_nameservers"]
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync domains to Cloudflare (free plan) and export nameservers to CSV."
    )
    parser.add_argument("domains_file", help="Path to .txt file — one domain per line")
    parser.add_argument(
        "--token",
        required=True,
        help="Cloudflare API token (needs Zone:Read + Zone:Edit + DNS:Edit permissions)",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help="Cloudflare account ID (optional — auto-detected if omitted)",
    )
    parser.add_argument(
        "--output",
        default="cloudflare_domains.csv",
        help="Output CSV file path (default: cloudflare_domains.csv)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between API calls to respect rate limits (default: 0.5)",
    )
    args = parser.parse_args()

    cf = CloudflareClient(args.token)

    # --- Account ---
    if args.account_id:
        account_id = args.account_id
        print(f"Using provided account ID: {account_id}")
    else:
        print("Auto-detecting Cloudflare account ID...")
        try:
            account_id = cf.get_account_id()
        except (requests.HTTPError, RuntimeError) as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)
        print(f"  Account ID : {account_id}")

    # --- Existing zones (fetch once to minimise API calls) ---
    print("Fetching existing zones...")
    try:
        existing_zones = cf.list_all_zones()
    except (requests.HTTPError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    existing_by_name = {z["name"]: z for z in existing_zones}
    print(f"  Found {len(existing_zones)} zone(s) in account.\n")

    # --- Domain list ---
    try:
        domains = load_domains(args.domains_file)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    print(f"Loaded {len(domains)} domain(s) from '{args.domains_file}'.\n")

    results: list[dict] = []

    for idx, domain in enumerate(domains, start=1):
        prefix = f"[{idx}/{len(domains)}] {domain}"
        print(prefix)

        nameservers: list[str] = []
        status = "error"

        if domain in existing_by_name:
            # Domain already in the account — skip creation
            zone = existing_by_name[domain]
            nameservers = zone.get("name_servers", [])
            status = "existing"
            print("  Already in account — skipping.")
        else:
            # Domain not found — create it
            print("  Not found. Creating on free plan (jump_start=True)...")
            try:
                zone = cf.create_zone(domain, account_id)
                nameservers = zone.get("name_servers", [])
                status = "created"
                print(f"  Created successfully. Zone ID: {zone['id']}")

                # The jump_start flag triggers an automatic DNS scan during creation.
                # We also call the scan endpoint explicitly as a safety net.
                time.sleep(args.delay)
                print("  Triggering DNS auto-scan...")
                ok = cf.scan_dns(zone["id"])
                print("  DNS scan " + ("queued." if ok else "skipped/unavailable."))

            except requests.HTTPError as exc:
                print(f"  HTTP ERROR: {exc.response.status_code} — {exc.response.text}")
            except RuntimeError as exc:
                print(f"  ERROR: {exc}")

        results.append(
            {
                "domain": domain,
                "status": status,
                "ns1": nameservers[0] if len(nameservers) > 0 else "",
                "ns2": nameservers[1] if len(nameservers) > 1 else "",
                "all_nameservers": ", ".join(nameservers),
            }
        )

        time.sleep(args.delay)

    # --- Export CSV ---
    write_csv(results, args.output)

    # --- Summary ---
    created  = sum(1 for r in results if r["status"] == "created")
    existing = sum(1 for r in results if r["status"] == "existing")
    errors   = sum(1 for r in results if r["status"] == "error")

    print(f"\nDone! Results saved to '{args.output}'")
    print(f"  Created : {created}")
    print(f"  Existed : {existing}")
    print(f"  Errors  : {errors}")


if __name__ == "__main__":
    main()
