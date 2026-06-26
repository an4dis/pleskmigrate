#!/usr/bin/env python3
"""
Alias DNS Sync
==============
Reads a CSV with (alias, domain) columns and mirrors DNS records from each
domain to its alias zone in Cloudflare.

Modes
-----
  --subdomains mail,webmail   (default) Sync A records for specific subdomains only.
  --all                       Sync every DNS record from the domain zone to the alias zone.

Usage
-----
    # Specific subdomains (default: mail, webmail)
    python alias_mail_sync.py input.csv --token <CF_TOKEN>
    python alias_mail_sync.py input.csv --token <CF_TOKEN> --subdomains mail,webmail,ftp,smtp

    # All records
    python alias_mail_sync.py input.csv --token <CF_TOKEN> --all

    # Dry run (no changes)
    python alias_mail_sync.py input.csv --token <CF_TOKEN> --all --dry-run

CSV format (header required):
    alias,domain
    alias-domain.gr,main-domain.gr

Notes
-----
  - NS and SOA records are always skipped (managed by Cloudflare).
  - In --all mode, record names are translated: sub.domain → sub.alias.
  - MX priority is preserved.
  - Proxied status is copied from the source record.
"""

import argparse
import csv
import sys
import time
from pathlib import Path

import requests

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
SKIP_TYPES = {"NS", "SOA"}


# ---------------------------------------------------------------------------
# Cloudflare client
# ---------------------------------------------------------------------------

class CloudflareClient:
    def __init__(self, api_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, params: dict = None) -> dict:
        resp = self.session.get(f"{CLOUDFLARE_API}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        resp = self.session.post(f"{CLOUDFLARE_API}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, payload: dict) -> dict:
        resp = self.session.put(f"{CLOUDFLARE_API}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_zone_id(self, domain: str) -> str | None:
        data = self._get("/zones", params={"name": domain, "per_page": 1})
        if not data.get("success"):
            raise RuntimeError(f"Failed to query zone '{domain}': {data.get('errors')}")
        result = data.get("result", [])
        return result[0]["id"] if result else None

    def list_all_records(self, zone_id: str) -> list[dict]:
        """Fetch every DNS record in a zone (handles pagination)."""
        records = []
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

    def get_records_by_name(self, zone_id: str, name: str, record_type: str = None) -> list[dict]:
        params = {"name": name, "per_page": 100}
        if record_type:
            params["type"] = record_type
        data = self._get(f"/zones/{zone_id}/dns_records", params=params)
        if not data.get("success"):
            raise RuntimeError(f"Failed to query records for '{name}': {data.get('errors')}")
        return data.get("result", [])

    def upsert_record(self, zone_id: str, record: dict, dry_run: bool) -> str:
        """
        Create or update a DNS record. Returns an action string.
        `record` must have: type, name, content (and optionally priority, proxied, ttl, data).
        """
        rtype = record["type"]
        name  = record["name"]

        existing = self.get_records_by_name(zone_id, name, record_type=rtype)
        # For types that allow multiple values (MX, TXT) match on content too
        if rtype in ("MX", "TXT", "CNAME"):
            match = next((r for r in existing if r["content"] == record.get("content", "")), None)
        else:
            match = existing[0] if existing else None

        payload = _build_payload(record)

        if dry_run:
            if not match:
                return "would-create"
            if _needs_update(match, record):
                return "would-update"
            return "would-skip"

        if not match:
            data = self._post(f"/zones/{zone_id}/dns_records", payload)
            if not data.get("success"):
                raise RuntimeError(f"Create failed for {rtype} '{name}': {data.get('errors')}")
            return "created"

        if not _needs_update(match, record):
            return "unchanged"

        data = self._put(f"/zones/{zone_id}/dns_records/{match['id']}", payload)
        if not data.get("success"):
            raise RuntimeError(f"Update failed for {rtype} '{name}': {data.get('errors')}")
        return "updated"


# ---------------------------------------------------------------------------
# Record helpers
# ---------------------------------------------------------------------------

def _build_payload(record: dict) -> dict:
    """Build the Cloudflare API payload from a normalised record dict."""
    payload = {
        "type":    record["type"],
        "name":    record["name"],
        "ttl":     record.get("ttl", 1),
        "proxied": record.get("proxied", False),
    }
    # SRV / CAA / SSHFP / TLSA / DNSKEY / DS use a nested `data` object
    if "data" in record:
        payload["data"] = record["data"]
    else:
        payload["content"] = record.get("content", "")

    if "priority" in record:
        payload["priority"] = record["priority"]

    return payload


def _needs_update(existing: dict, desired: dict) -> bool:
    if existing.get("content") != desired.get("content"):
        return True
    if "priority" in desired and existing.get("priority") != desired["priority"]:
        return True
    if existing.get("proxied") != desired.get("proxied", False):
        return True
    return False


def translate_name(name: str, domain: str, alias: str) -> str:
    """Rewrite record name by substituting domain → alias."""
    if name == domain:
        return alias
    if name.endswith(f".{domain}"):
        return name[: -(len(domain))] + alias
    return name


def normalise_record(raw: dict, domain: str, alias: str) -> dict:
    """
    Convert a raw Cloudflare record into a normalised dict ready for upsert,
    translating the name from domain to alias.
    """
    rec = {
        "type":    raw["type"],
        "name":    translate_name(raw["name"], domain, alias),
        "ttl":     raw.get("ttl", 1),
        "proxied": raw.get("proxied", False),
    }
    if "data" in raw:
        rec["data"] = raw["data"]
    else:
        rec["content"] = raw.get("content", "")
    if "priority" in raw:
        rec["priority"] = raw["priority"]
    return rec


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def load_csv(filepath: str) -> list[dict]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {filepath}")
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            alias  = row.get("alias",  "").strip().lower()
            domain = row.get("domain", "").strip().lower()
            if alias and domain:
                rows.append({"alias": alias, "domain": domain})
    if not rows:
        raise ValueError("No valid rows found. CSV must have 'alias' and 'domain' columns.")
    return rows


# ---------------------------------------------------------------------------
# Sync modes
# ---------------------------------------------------------------------------

def sync_subdomains(
    cf: CloudflareClient,
    domain: str,
    domain_zone_id: str,
    alias: str,
    alias_zone_id: str,
    subdomains: list[str],
    dry_run: bool,
    delay: float,
) -> list[str]:
    """Sync A records for specific subdomains only."""
    statuses = []
    for sub in subdomains:
        src_fqdn = f"{sub}.{domain}"
        dst_fqdn = f"{sub}.{alias}"

        # Fetch source A record
        try:
            src_records = cf.get_records_by_name(domain_zone_id, src_fqdn, record_type="A")
            time.sleep(delay)
        except (requests.HTTPError, RuntimeError) as exc:
            print(f"  ERROR reading {src_fqdn}: {exc}")
            statuses.append(f"{sub}:read-error")
            continue

        if not src_records:
            print(f"  {src_fqdn}: no A record — skipped")
            statuses.append(f"{sub}:no-record")
            continue

        ip = src_records[0]["content"]
        print(f"  {src_fqdn} → {ip}")

        record = {
            "type":    "A",
            "name":    dst_fqdn,
            "content": ip,
            "proxied": src_records[0].get("proxied", False),
        }
        try:
            action = cf.upsert_record(alias_zone_id, record, dry_run)
            time.sleep(delay)
        except (requests.HTTPError, RuntimeError) as exc:
            print(f"  ERROR writing {dst_fqdn}: {exc}")
            statuses.append(f"{sub}:write-error")
            continue

        print(f"  {dst_fqdn} → {ip}  [{action}]")
        statuses.append(f"{sub}:{action}")

    return statuses


def sync_all(
    cf: CloudflareClient,
    domain: str,
    domain_zone_id: str,
    alias: str,
    alias_zone_id: str,
    dry_run: bool,
    delay: float,
) -> list[str]:
    """Sync every DNS record from domain zone to alias zone."""
    try:
        source_records = cf.list_all_records(domain_zone_id)
        time.sleep(delay)
    except (requests.HTTPError, RuntimeError) as exc:
        print(f"  ERROR fetching records for {domain}: {exc}")
        return ["fetch-error"]

    skipped_types = set()
    statuses = []

    for raw in source_records:
        rtype = raw["type"]
        if rtype in SKIP_TYPES:
            skipped_types.add(rtype)
            continue

        record = normalise_record(raw, domain, alias)
        label  = f"{rtype} {record['name']}"
        content_display = record.get("content") or str(record.get("data", ""))

        try:
            action = cf.upsert_record(alias_zone_id, record, dry_run)
            time.sleep(delay)
        except (requests.HTTPError, RuntimeError) as exc:
            print(f"  ERROR {label}: {exc}")
            statuses.append(f"{rtype}:write-error")
            continue

        if action not in ("unchanged", "would-skip"):
            print(f"  [{action}] {label} → {content_display}")
        statuses.append(f"{rtype}:{action}")

    if skipped_types:
        print(f"  Skipped record types (auto-managed): {', '.join(sorted(skipped_types))}")

    return statuses


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync DNS records from domain zones to alias zones in Cloudflare.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Sync mail + webmail A records (default)
  python alias_mail_sync.py input.csv --token TOKEN

  # Sync specific subdomains
  python alias_mail_sync.py input.csv --token TOKEN --subdomains mail,webmail,ftp,smtp

  # Sync ALL records
  python alias_mail_sync.py input.csv --token TOKEN --all

  # Dry run
  python alias_mail_sync.py input.csv --token TOKEN --all --dry-run
        """,
    )
    parser.add_argument("csv_file", help="CSV file with 'alias' and 'domain' columns")
    parser.add_argument(
        "--token", required=True,
        help="Cloudflare API token (Zone:Read + DNS:Read + DNS:Edit)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--all", action="store_true",
        help="Sync every DNS record from the domain zone to the alias zone",
    )
    mode.add_argument(
        "--subdomains", default="mail,webmail",
        metavar="SUB1,SUB2,...",
        help="Comma-separated subdomains to sync as A records (default: mail,webmail)",
    )

    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without making any modifications",
    )
    parser.add_argument(
        "--delay", type=float, default=0.3,
        help="Seconds between API calls to respect rate limits (default: 0.3)",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("[DRY RUN] No changes will be made.\n")

    cf = CloudflareClient(args.token)

    try:
        rows = load_csv(args.csv_file)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    subdomains = [s.strip() for s in args.subdomains.split(",") if s.strip()]
    mode_label = "all records" if args.all else f"subdomains: {', '.join(subdomains)}"
    print(f"Mode      : {mode_label}")
    print(f"Rows      : {len(rows)}")
    print(f"Dry run   : {args.dry_run}\n")

    results = []

    for idx, row in enumerate(rows, start=1):
        alias  = row["alias"]
        domain = row["domain"]
        print(f"[{idx}/{len(rows)}] {alias}  ←  {domain}")

        # Resolve zone IDs
        try:
            domain_zone_id = cf.get_zone_id(domain)
            time.sleep(args.delay)
            alias_zone_id  = cf.get_zone_id(alias)
            time.sleep(args.delay)
        except (requests.HTTPError, RuntimeError) as exc:
            print(f"  ERROR resolving zones: {exc}")
            results.append({"alias": alias, "domain": domain, "status": "zone-error"})
            continue

        if not domain_zone_id:
            print(f"  SKIP: '{domain}' not found in Cloudflare account")
            results.append({"alias": alias, "domain": domain, "status": "domain-zone-missing"})
            continue

        if not alias_zone_id:
            print(f"  SKIP: '{alias}' not found in Cloudflare account")
            results.append({"alias": alias, "domain": domain, "status": "alias-zone-missing"})
            continue

        if args.all:
            statuses = sync_all(
                cf, domain, domain_zone_id, alias, alias_zone_id, args.dry_run, args.delay
            )
        else:
            statuses = sync_subdomains(
                cf, domain, domain_zone_id, alias, alias_zone_id,
                subdomains, args.dry_run, args.delay
            )

        results.append({
            "alias":  alias,
            "domain": domain,
            "status": ", ".join(statuses) if statuses else "no-action",
        })

    # Summary
    print("\n--- Summary ---")
    errors = sum(1 for r in results if "error" in r["status"] or "missing" in r["status"])
    print(f"  Processed : {len(results)}")
    print(f"  OK        : {len(results) - errors}")
    print(f"  Errors    : {errors}")
    for r in results:
        print(f"  {r['alias']:<35} <- {r['domain']:<35}  {r['status']}")


if __name__ == "__main__":
    main()
