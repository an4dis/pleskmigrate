#!/usr/bin/env python3
"""
Test script to verify Plesk and Cloudflare API connections
"""

import os
import sys
from plesk_to_cloudflare import (
    PleskConfig, CloudflareConfig, PleskAPI, CloudflareAPI,
    load_config, logger
)

def test_plesk(plesk_config):
    """Test Plesk API connection and list domains"""
    logger.info("\n" + "="*60)
    logger.info("Testing Plesk API Connection")
    logger.info("="*60)

    plesk = PleskAPI(plesk_config)

    # Test connection
    if not plesk.test_connection():
        return False

    # Try to list domains
    try:
        response = plesk.session.get(f"{plesk.base_url}/domains")
        response.raise_for_status()
        domains = response.json()

        logger.info(f"\n✓ Found {len(domains)} domain(s) in Plesk:")
        for domain in domains[:10]:  # Show first 10
            logger.info(f"  • {domain.get('name')} (ID: {domain.get('id')})")

        if len(domains) > 10:
            logger.info(f"  ... and {len(domains) - 10} more")

        return True
    except Exception as e:
        logger.error(f"✗ Error listing domains: {e}")
        return False


def test_cloudflare(cloudflare_config):
    """Test Cloudflare API connection"""
    logger.info("\n" + "="*60)
    logger.info("Testing Cloudflare API Connection")
    logger.info("="*60)

    cloudflare = CloudflareAPI(cloudflare_config)

    # Test connection
    if not cloudflare.test_connection():
        return False

    # Get user info
    try:
        response = cloudflare.session.get(f"{cloudflare.base_url}/user")
        response.raise_for_status()
        data = response.json()

        if data.get('success'):
            user = data['result']
            logger.info(f"\n✓ Authenticated as: {user.get('email')}")
            logger.info(f"  Account ID: {user.get('id')}")

        # List zones
        response = cloudflare.session.get(f"{cloudflare.base_url}/zones")
        response.raise_for_status()
        data = response.json()

        if data.get('success'):
            zones = data['result']
            logger.info(f"\n✓ Found {len(zones)} zone(s) in Cloudflare:")
            for zone in zones[:10]:  # Show first 10
                logger.info(f"  • {zone.get('name')} (Status: {zone.get('status')})")

            if len(zones) > 10:
                logger.info(f"  ... and {len(zones) - 10} more")

        return True
    except Exception as e:
        logger.error(f"✗ Error getting user info: {e}")
        return False


def test_domain_dns(domain_name):
    """Test fetching DNS records for a specific domain"""
    logger.info("\n" + "="*60)
    logger.info(f"Testing DNS Fetch for: {domain_name}")
    logger.info("="*60)

    plesk_config, _ = load_config()
    plesk = PleskAPI(plesk_config)

    dns_records = plesk.get_dns_records(domain_name)

    if dns_records:
        logger.info(f"\n✓ Found {len(dns_records)} DNS record(s):")
        for record in dns_records:
            priority_str = f" [Priority: {record.priority}]" if record.priority else ""
            logger.info(f"  • {record.type:6} {record.name:30} → {record.content}{priority_str}")
        return True
    else:
        logger.warning(f"No DNS records found for {domain_name}")
        return False


def main():
    """Main test function"""
    logger.info("="*60)
    logger.info("Plesk to Cloudflare - Connection Test")
    logger.info("="*60)

    # Load configuration
    try:
        plesk_config, cloudflare_config = load_config()
    except SystemExit:
        logger.error("\nPlease set the required environment variables:")
        logger.error("  PLESK_HOST")
        logger.error("  PLESK_API_KEY (or PLESK_USERNAME/PLESK_PASSWORD)")
        logger.error("  CLOUDFLARE_API_TOKEN")
        return

    # Test Plesk
    plesk_ok = test_plesk(plesk_config)

    # Test Cloudflare
    cloudflare_ok = test_cloudflare(cloudflare_config)

    # Test specific domain if provided
    if len(sys.argv) > 1:
        domain_name = sys.argv[1]
        test_domain_dns(domain_name)

    # Summary
    logger.info("\n" + "="*60)
    logger.info("Test Summary")
    logger.info("="*60)
    logger.info(f"Plesk API:      {'✓ OK' if plesk_ok else '✗ FAILED'}")
    logger.info(f"Cloudflare API: {'✓ OK' if cloudflare_ok else '✗ FAILED'}")

    if plesk_ok and cloudflare_ok:
        logger.info("\n✓ All tests passed! You're ready to migrate.")
        logger.info("\nRun the migration with:")
        logger.info("  python plesk_to_cloudflare.py domains.txt")
    else:
        logger.error("\n✗ Some tests failed. Please check your configuration.")

    logger.info("="*60)


if __name__ == "__main__":
    import requests
    requests.packages.urllib3.disable_warnings()
    main()
