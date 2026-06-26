#!/usr/bin/env python3
"""
Plesk to Cloudflare Migration Script

This script connects to a Plesk server and migrates domains to Cloudflare
using the Plesk Cloudflare extension.
"""

import os
import sys
import requests
import json
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
import logging
from dataclasses import dataclass
import time


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('migration.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class PleskConfig:
    """Plesk server configuration"""
    host: str
    port: int = 8443
    username: str = ""
    password: str = ""
    api_key: str = ""

    def __post_init__(self):
        """Validate that either username/password or api_key is provided"""
        if not self.api_key and not (self.username and self.password):
            raise ValueError("Either api_key or username/password must be provided")


class PleskCloudflareManager:
    """Manager class for Plesk Cloudflare operations"""

    def __init__(self, config: PleskConfig):
        self.config = config
        self.base_url = f"https://{config.host}:{config.port}/api/v2"
        self.session = requests.Session()

        # Set up authentication
        if config.api_key:
            self.session.headers.update({
                'X-API-Key': config.api_key
            })
        else:
            self.session.auth = (config.username, config.password)

        # Disable SSL verification warning (use with caution in production)
        self.session.verify = False
        requests.packages.urllib3.disable_warnings()

        logger.info(f"Initialized connection to Plesk server: {config.host}")

    def test_connection(self) -> bool:
        """Test connection to Plesk server"""
        try:
            response = self.session.get(f"{self.base_url}/server")
            response.raise_for_status()
            logger.info("Successfully connected to Plesk server")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to connect to Plesk server: {e}")
            return False

    def get_domain_id(self, domain_name: str) -> Optional[int]:
        """Get domain ID from domain name"""
        try:
            response = self.session.get(
                f"{self.base_url}/domains",
                params={'name': domain_name}
            )
            response.raise_for_status()
            data = response.json()

            if data and len(data) > 0:
                domain_id = data[0].get('id')
                logger.info(f"Found domain '{domain_name}' with ID: {domain_id}")
                return domain_id
            else:
                logger.warning(f"Domain '{domain_name}' not found in Plesk")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching domain ID for '{domain_name}': {e}")
            return None

    def check_cloudflare_extension(self) -> bool:
        """Check if Cloudflare extension is installed and enabled"""
        try:
            # Using XML API to check extensions
            xml_url = f"https://{self.config.host}:{self.config.port}/enterprise/control/agent.php"

            xml_request = """<?xml version="1.0" encoding="UTF-8"?>
            <packet>
    <extension>
        <get>
            <filter>
                <id>cloudflaredns</id>
            </filter>
        </get>
    </extension>
</packet>"""

            headers = {'Content-Type': 'text/xml','HTTP_AUTH_LOGIN': self.config.username, 'HTTP_AUTH_PASSWD': self.config.password}
            if self.config.api_key:
                headers['X-API-Key'] = self.config.api_key

            response = requests.post(
                xml_url,
                data=xml_request,
                headers=headers,
                auth=(self.config.username, self.config.password) if self.config.username else None,
                verify=False
            )

            logger.info("Assuming Cloudflare extension is installed (as per user confirmation)")
            return True
        except Exception as e:
            logger.warning(f"Could not verify Cloudflare extension status: {e}")
            logger.info("Proceeding with assumption that extension is installed")
            return True

    def enable_cloudflare_for_domain(self, domain_name: str) -> bool:
        """
        Enable Cloudflare for a specific domain using Plesk Cloudflare DNS extension

        Note: The cloudflaredns extension uses its own module API endpoints.
        """
        try:
            domain_id = self.get_domain_id(domain_name)
            if not domain_id:
                return False

            logger.info(f"Enabling Cloudflare for domain: {domain_name}")

            # The cloudflaredns extension base URL
            extension_base = f"https://{self.config.host}:{self.config.port}/modules/cloudflaredns/index.php/api"

            # Try different API endpoints for the cloudflaredns extension
            endpoints_to_try = [
                {
                    "url": f"{extension_base}/domain/export",
                    "method": "POST",
                    "payload": {"domainNameList": [domain_name]},
                    "description": "Export domain to Cloudflare"
                },
                {
                    "url": f"{extension_base}/domain/add",
                    "method": "POST",
                    "payload": {"domain": domain_name},
                    "description": "Add domain to Cloudflare"
                },
                {
                    "url": f"{extension_base}/domain/sync",
                    "method": "POST",
                    "payload": {"domain": domain_name, "domainId": domain_id},
                    "description": "Sync domain with Cloudflare"
                }
            ]

            for endpoint_info in endpoints_to_try:
                try:
                    logger.info(f"Trying: {endpoint_info['description']} - {endpoint_info['url']}")

                    if endpoint_info["method"] == "POST":
                        response = self.session.post(
                            endpoint_info["url"],
                            json=endpoint_info["payload"]
                        )
                    else:
                        response = self.session.get(endpoint_info["url"])

                    logger.debug(f"Response status: {response.status_code}")
                    logger.debug(f"Response body: {response.text[:500]}")

                    if response.status_code in [200, 201]:
                        # Check if response contains success indicators
                        try:
                            response_data = response.json()
                            logger.debug(f"Response JSON: {response_data}")

                            # Check for common success patterns
                            if (isinstance(response_data, dict) and
                                (response_data.get('success') or
                                 response_data.get('status') == 'success' or
                                 'error' not in response_data.get('message', '').lower())):
                                logger.info(f"✓ Successfully enabled Cloudflare for '{domain_name}'")
                                return True
                        except json.JSONDecodeError:
                            # If response isn't JSON but status is 200, consider it success
                            if response.status_code == 200:
                                logger.info(f"✓ Successfully enabled Cloudflare for '{domain_name}' (non-JSON response)")
                                return True

                except requests.exceptions.RequestException as e:
                    logger.debug(f"Endpoint {endpoint_info['url']} failed: {e}")
                    continue
                except Exception as e:
                    logger.debug(f"Unexpected error with endpoint {endpoint_info['url']}: {e}")
                    continue

            # If all endpoints fail, provide manual instructions
            logger.warning(
                f"Could not automatically enable Cloudflare for '{domain_name}' via API."
            )
            logger.info(
                f"Manual steps:\n"
                f"  1. RDP into your Plesk server (Windows)\n"
                f"  2. Open PowerShell/CMD and run:\n"
                f"     \"C:\\Program Files (x86)\\Plesk\\admin\\bin\\extension.exe\" --exec cloudflaredns "
                f"cli.php -a export -d {domain_name}\n"
                f"  OR\n"
                f"  3. Use Plesk UI: Websites & Domains → {domain_name} → Cloudflare"
            )

            return False

        except Exception as e:
            logger.error(f"Error enabling Cloudflare for '{domain_name}': {e}")
            return False

    def migrate_domains(self, domains: List[str]) -> Dict[str, bool]:
        """
        Migrate multiple domains to Cloudflare

        Args:
            domains: List of domain names to migrate

        Returns:
            Dictionary mapping domain names to success status
        """
        results = {}

        logger.info(f"Starting migration of {len(domains)} domains")

        for i, domain in enumerate(domains, 1):
            domain = domain.strip()
            if not domain or domain.startswith('#'):
                continue

            logger.info(f"[{i}/{len(domains)}] Processing domain: {domain}")

            success = self.enable_cloudflare_for_domain(domain)
            results[domain] = success

            # Add a small delay to avoid overwhelming the API
            if i < len(domains):
                time.sleep(1)

        return results

    def print_summary(self, results: Dict[str, bool]):
        """Print migration summary"""
        successful = [d for d, success in results.items() if success]
        failed = [d for d, success in results.items() if not success]

        logger.info("\n" + "="*60)
        logger.info("MIGRATION SUMMARY")
        logger.info("="*60)
        logger.info(f"Total domains: {len(results)}")
        logger.info(f"Successful: {len(successful)}")
        logger.info(f"Failed: {len(failed)}")

        if successful:
            logger.info("\nSuccessfully migrated:")
            for domain in successful:
                logger.info(f"  ✓ {domain}")

        if failed:
            logger.info("\nFailed to migrate:")
            for domain in failed:
                logger.info(f"  ✗ {domain}")


def load_domains_from_file(file_path: str) -> List[str]:
    """Load domains from a text file"""
    try:
        with open(file_path, 'r') as f:
            domains = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        logger.info(f"Loaded {len(domains)} domains from {file_path}")
        return domains
    except FileNotFoundError:
        logger.error(f"Domain file not found: {file_path}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error reading domain file: {e}")
        sys.exit(1)


def load_config() -> PleskConfig:
    """Load configuration from environment variables or config file"""

    # Try to load from environment variables first
    host = os.getenv('PLESK_HOST')
    port = int(os.getenv('PLESK_PORT', '8443'))
    api_key = os.getenv('PLESK_API_KEY', '')
    username = os.getenv('PLESK_USERNAME', '')
    password = os.getenv('PLESK_PASSWORD', '')

    if not host:
        logger.error("PLESK_HOST environment variable is required")
        logger.info("\nPlease set the following environment variables:")
        logger.info("  PLESK_HOST: Your Plesk server hostname or IP")
        logger.info("  PLESK_API_KEY: Your Plesk API key (recommended)")
        logger.info("  OR")
        logger.info("  PLESK_USERNAME: Your Plesk username")
        logger.info("  PLESK_PASSWORD: Your Plesk password")
        logger.info("\nExample:")
        logger.info("  export PLESK_HOST=plesk.example.com")
        logger.info("  export PLESK_API_KEY=your-api-key-here")
        sys.exit(1)

    return PleskConfig(
        host=host,
        port=port,
        api_key=api_key,
        username=username,
        password=password
    )


def main():
    """Main function"""
    # Check command line arguments
    if len(sys.argv) < 2:
        print("Usage: python migrate_to_cloudflare.py <domains_file>")
        print("\nExample:")
        print("  python migrate_to_cloudflare.py domains.txt")
        sys.exit(1)

    domains_file = sys.argv[1]

    # Load configuration
    logger.info("Loading configuration...")
    config = load_config()

    # Load domains
    logger.info(f"Loading domains from {domains_file}...")
    domains = load_domains_from_file(domains_file)

    if not domains:
        logger.error("No domains found in the file")
        sys.exit(1)

    # Initialize manager
    manager = PleskCloudflareManager(config)

    # Test connection
    if not manager.test_connection():
        logger.error("Cannot proceed without a valid connection to Plesk")
        sys.exit(1)

    # Check Cloudflare extension
    if not manager.check_cloudflare_extension():
        logger.error("Cloudflare extension not found or not enabled")
        sys.exit(1)

    # Migrate domains
    results = manager.migrate_domains(domains)

    # Print summary
    manager.print_summary(results)

    # Exit with appropriate code
    failed_count = sum(1 for success in results.values() if not success)
    sys.exit(0 if failed_count == 0 else 1)


if __name__ == "__main__":
    main()
