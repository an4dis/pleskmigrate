#!/usr/bin/env python3
"""
Test script to debug Plesk API connection and Cloudflare extension endpoints

This script helps troubleshoot API connectivity issues.
"""

import os
import sys
import requests
import json
from migrate_to_cloudflare import PleskConfig, load_config
import logging

# Configure detailed logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def test_basic_connection(config: PleskConfig):
    """Test basic HTTPS connection to Plesk"""
    logger.info("\n=== Testing Basic Connection ===")

    url = f"https://{config.host}:{config.port}"

    try:
        response = requests.get(url, verify=False, timeout=10)
        logger.info(f"✓ Server is reachable at {url}")
        logger.info(f"  Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"✗ Cannot reach server: {e}")
        return False


def test_api_auth(config: PleskConfig):
    """Test API authentication"""
    logger.info("\n=== Testing API Authentication ===")

    session = requests.Session()
    session.verify = False

    if config.api_key:
        session.headers.update({'X-API-Key': config.api_key})
        logger.info("Using API Key authentication")
    else:
        session.auth = (config.username, config.password)
        logger.info(f"Using Basic Auth with username: {config.username}")

    # Test with API v2 server endpoint
    url = f"https://{config.host}:{config.port}/api/v2/server"

    try:
        response = session.get(url)
        logger.info(f"Response status: {response.status_code}")
        logger.debug(f"Response headers: {dict(response.headers)}")

        if response.status_code == 200:
            logger.info("✓ Authentication successful")
            logger.debug(f"Response body: {response.text[:500]}")
            return True
        elif response.status_code == 401:
            logger.error("✗ Authentication failed (401 Unauthorized)")
            logger.error("  Check your API key or username/password")
            return False
        else:
            logger.warning(f"⚠ Unexpected status code: {response.status_code}")
            logger.debug(f"Response: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"✗ Request failed: {e}")
        return False


def test_list_domains(config: PleskConfig):
    """Test listing domains"""
    logger.info("\n=== Testing Domain Listing ===")

    session = requests.Session()
    session.verify = False

    if config.api_key:
        session.headers.update({'X-API-Key': config.api_key})
    else:
        session.auth = (config.username, config.password)

    url = f"https://{config.host}:{config.port}/api/v2/domains"

    try:
        response = session.get(url)
        logger.info(f"Response status: {response.status_code}")

        if response.status_code == 200:
            domains = response.json()
            logger.info(f"✓ Successfully retrieved {len(domains)} domain(s)")

            if domains:
                logger.info("First 5 domains:")
                for domain in domains[:5]:
                    logger.info(f"  - {domain.get('name')} (ID: {domain.get('id')})")
            return True
        else:
            logger.error(f"✗ Failed to list domains")
            logger.debug(f"Response: {response.text}")
            return False
    except Exception as e:
        logger.error(f"✗ Error: {e}")
        return False


def test_cloudflare_extension(config: PleskConfig):
    """Test Cloudflare extension endpoints"""
    logger.info("\n=== Testing Cloudflare Extension Endpoints ===")

    session = requests.Session()
    session.verify = False

    if config.api_key:
        session.headers.update({'X-API-Key': config.api_key})
    else:
        session.auth = (config.username, config.password)

    extension_base = f"https://{config.host}:{config.port}/modules/cloudflaredns/index.php/api"

    endpoints = [
        f"{extension_base}/status",
        f"{extension_base}/settings",
        f"{extension_base}/domain/list",
    ]

    for endpoint in endpoints:
        logger.info(f"\nTrying: {endpoint}")
        try:
            response = session.get(endpoint)
            logger.info(f"  Status: {response.status_code}")

            if response.status_code == 200:
                logger.info("  ✓ Endpoint accessible")
                try:
                    data = response.json()
                    logger.debug(f"  Response: {json.dumps(data, indent=2)[:300]}")
                except:
                    logger.debug(f"  Response (text): {response.text[:200]}")
            else:
                logger.warning(f"  Status: {response.status_code}")
                logger.debug(f"  Response: {response.text[:200]}")
        except Exception as e:
            logger.warning(f"  Error: {e}")


def test_specific_domain(config: PleskConfig, domain_name: str):
    """Test API calls for a specific domain"""
    logger.info(f"\n=== Testing Domain: {domain_name} ===")

    session = requests.Session()
    session.verify = False

    if config.api_key:
        session.headers.update({'X-API-Key': config.api_key})
    else:
        session.auth = (config.username, config.password)

    # Get domain info
    url = f"https://{config.host}:{config.port}/api/v2/domains"
    try:
        response = session.get(url, params={'name': domain_name})
        if response.status_code == 200:
            domains = response.json()
            if domains:
                domain_id = domains[0].get('id')
                logger.info(f"✓ Domain found with ID: {domain_id}")

                # Try Cloudflare export
                extension_url = f"https://{config.host}:{config.port}/modules/cloudflaredns/index.php/api/domain/export"
                payload = {"domainNameList": [domain_name]}

                logger.info(f"\nTrying Cloudflare export for {domain_name}...")
                logger.info(f"URL: {extension_url}")
                logger.info(f"Payload: {json.dumps(payload)}")

                response = session.post(extension_url, json=payload)
                logger.info(f"Response status: {response.status_code}")
                logger.info(f"Response body: {response.text}")

                return True
            else:
                logger.error(f"✗ Domain '{domain_name}' not found in Plesk")
                return False
    except Exception as e:
        logger.error(f"✗ Error: {e}")
        return False


def main():
    """Main test function"""
    logger.info("="*60)
    logger.info("Plesk API Connection Test")
    logger.info("="*60)

    # Load config
    try:
        config = load_config()
        logger.info(f"\nConfiguration loaded:")
        logger.info(f"  Host: {config.host}")
        logger.info(f"  Port: {config.port}")
        logger.info(f"  Auth: {'API Key' if config.api_key else 'Username/Password'}")
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        return

    # Run tests
    test_basic_connection(config)
    test_api_auth(config)
    test_list_domains(config)
    test_cloudflare_extension(config)

    # Test specific domain if provided
    if len(sys.argv) > 1:
        test_domain = sys.argv[1]
        test_specific_domain(config, test_domain)

    logger.info("\n" + "="*60)
    logger.info("Test Complete")
    logger.info("="*60)


if __name__ == "__main__":
    requests.packages.urllib3.disable_warnings()
    main()
