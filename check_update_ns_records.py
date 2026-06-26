#!/usr/bin/env python3
"""
Cloudflare NS Records Checker and Updater

This script connects to the Cloudflare API and:
1. Lists all domains in your Cloudflare account
2. Checks if the NS records in each zone match the assigned Cloudflare nameservers
3. Updates NS records automatically if they don't match

Requirements:
- CLOUDFLARE_API_TOKEN in .env file with Zone.Zone (Read) and Zone.DNS (Edit) permissions
"""

import os
import sys
import requests
import logging
from typing import List, Dict, Optional
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f'ns_check_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
    ]
)
logger = logging.getLogger(__name__)


class CloudflareNSManager:
    """Manage NS records for Cloudflare zones"""

    def __init__(self, api_token: str):
        """Initialize with Cloudflare API token"""
        self.api_token = api_token
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        })

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict]:
        """Make API request with error handling"""
        url = f"{self.base_url}/{endpoint}"

        try:
            response = self.session.request(method, url, **kwargs)

            # Try to get JSON response even on error
            try:
                data = response.json()
            except:
                data = None

            # Log detailed error information
            if response.status_code >= 400:
                logger.error(f"API request failed: {method} {endpoint}")
                logger.error(f"Status code: {response.status_code}")
                if data:
                    logger.error(f"Error response: {data}")
                else:
                    logger.error(f"Response text: {response.text[:500]}")
                return None

            response.raise_for_status()

            if data and not data.get('success', False):
                errors = data.get('errors', [])
                messages = data.get('messages', [])
                logger.error(f"API errors: {errors}")
                logger.error(f"API messages: {messages}")
                return None

            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for {endpoint}: {e}")
            return None

    def get_all_zones(self) -> List[Dict]:
        """Get all zones (domains) from Cloudflare account"""
        logger.info("Fetching all zones from Cloudflare...")

        zones = []
        page = 1
        per_page = 50

        while True:
            data = self._make_request(
                'GET',
                'zones',
                params={'page': page, 'per_page': per_page}
            )

            if not data:
                break

            result_info = data.get('result_info', {})
            zones.extend(data.get('result', []))

            # Check if there are more pages
            if page >= result_info.get('total_pages', 1):
                break

            page += 1

        logger.info(f"Found {len(zones)} zones")
        return zones

    def get_cloudflare_nameservers(self, zone_id: str) -> List[str]:
        """Get assigned Cloudflare nameservers for a zone"""
        data = self._make_request('GET', f'zones/{zone_id}')

        if not data:
            return []

        zone_info = data.get('result', {})
        nameservers = zone_info.get('name_servers', [])

        return sorted(nameservers)

    def get_ns_records(self, zone_id: str, zone_name: str) -> List[Dict]:
        """Get all NS records for a zone"""
        # Get all NS type records for the zone
        logger.debug(f"Fetching NS records for zone {zone_name} (ID: {zone_id})")

        data = self._make_request(
            'GET',
            f'zones/{zone_id}/dns_records',
            params={'type': 'NS', 'per_page': 100}
        )

        if not data:
            logger.warning(f"Failed to retrieve DNS records for {zone_name}")
            return []

        # Filter for records matching the zone name (apex records)
        all_records = data.get('result', [])
        logger.debug(f"Found {len(all_records)} NS records total")

        ns_records = [
            record for record in all_records
            if record.get('name') == zone_name and record.get('type') == 'NS'
        ]

        logger.debug(f"Found {len(ns_records)} NS records at apex")
        return ns_records

    def delete_ns_record(self, zone_id: str, record_id: str) -> bool:
        """Delete an NS record"""
        data = self._make_request(
            'DELETE',
            f'zones/{zone_id}/dns_records/{record_id}'
        )
        return data is not None

    def create_ns_record(self, zone_id: str, zone_name: str, nameserver: str) -> bool:
        """Create an NS record"""
        payload = {
            'type': 'NS',
            'name': zone_name,
            'content': nameserver,
            'ttl': 86400  # 24 hours
        }

        data = self._make_request(
            'POST',
            f'zones/{zone_id}/dns_records',
            json=payload
        )
        return data is not None

    def check_and_update_ns_records(self, zone: Dict, dry_run: bool = False) -> bool:
        """Check if NS records match Cloudflare nameservers and update if needed"""
        zone_id = zone['id']
        zone_name = zone['name']

        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {zone_name}")
        logger.info(f"{'='*60}")

        # Get Cloudflare assigned nameservers
        cf_nameservers = self.get_cloudflare_nameservers(zone_id)
        if not cf_nameservers:
            logger.error(f"Could not retrieve Cloudflare nameservers for {zone_name}")
            return False

        logger.info(f"Cloudflare nameservers: {', '.join(cf_nameservers)}")

        # Get current NS records
        current_ns_records = self.get_ns_records(zone_id, zone_name)
        current_ns_values = sorted([record['content'] for record in current_ns_records])

        if current_ns_records:
            logger.info(f"Current NS records: {', '.join(current_ns_values)}")
        else:
            logger.info("Current NS records: None")

        # Compare nameservers
        if current_ns_values == cf_nameservers:
            logger.info("✓ NS records match Cloudflare nameservers - no update needed")
            return True

        logger.warning("✗ NS records do NOT match Cloudflare nameservers")

        if dry_run:
            logger.info("[DRY RUN] Would update NS records")
            return True

        # Update NS records
        logger.info("Updating NS records...")

        # Delete existing NS records
        for record in current_ns_records:
            logger.info(f"  Deleting NS record: {record['content']}")
            if not self.delete_ns_record(zone_id, record['id']):
                logger.error(f"  Failed to delete NS record: {record['content']}")
                return False

        # Create new NS records
        for nameserver in cf_nameservers:
            logger.info(f"  Creating NS record: {nameserver}")
            if not self.create_ns_record(zone_id, zone_name, nameserver):
                logger.error(f"  Failed to create NS record: {nameserver}")
                return False

        logger.info("✓ NS records updated successfully")
        return True

    def process_all_zones(self, dry_run: bool = False):
        """Process all zones and check/update NS records"""
        zones = self.get_all_zones()

        if not zones:
            logger.error("No zones found or failed to retrieve zones")
            return

        logger.info(f"\n{'='*60}")
        logger.info(f"Processing {len(zones)} zones")
        logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE UPDATE'}")
        logger.info(f"{'='*60}")

        success_count = 0
        error_count = 0
        updated_count = 0

        for zone in zones:
            try:
                result = self.check_and_update_ns_records(zone, dry_run)
                if result:
                    success_count += 1
                else:
                    error_count += 1
            except Exception as e:
                logger.error(f"Error processing {zone['name']}: {e}")
                error_count += 1

        # Summary
        logger.info(f"\n{'='*60}")
        logger.info("SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"Total zones processed: {len(zones)}")
        logger.info(f"Successful: {success_count}")
        logger.info(f"Errors: {error_count}")
        logger.info(f"{'='*60}")


def load_env():
    """Load environment variables from .env file"""
    env_path = os.path.join(os.path.dirname(__file__), '.env')

    if not os.path.exists(env_path):
        logger.error(f".env file not found at {env_path}")
        sys.exit(1)

    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                # Remove quotes if present
                value = value.strip().strip('"').strip("'")
                os.environ[key.strip()] = value


def main():
    """Main function"""
    # Check for debug flag
    if '--debug' in sys.argv or '-d' in sys.argv:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")

    logger.info("="*60)
    logger.info("Cloudflare NS Records Checker and Updater")
    logger.info("="*60)

    # Load environment variables
    load_env()

    # Get Cloudflare API token
    api_token = os.environ.get('CLOUDFLARE_API_TOKEN')
    if not api_token:
        logger.error("CLOUDFLARE_API_TOKEN not found in .env file")
        sys.exit(1)

    # Check for dry-run flag
    dry_run = '--dry-run' in sys.argv or '-n' in sys.argv

    if dry_run:
        logger.info("Running in DRY RUN mode - no changes will be made")
    else:
        logger.info("Running in LIVE mode - NS records will be updated")
        response = input("\nAre you sure you want to update NS records? (yes/no): ")
        if response.lower() != 'yes':
            logger.info("Operation cancelled by user")
            sys.exit(0)

    # Initialize manager and process zones
    manager = CloudflareNSManager(api_token)
    manager.process_all_zones(dry_run)


if __name__ == "__main__":
    main()
