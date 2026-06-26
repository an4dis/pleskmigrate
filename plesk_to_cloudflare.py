#!/usr/bin/env python3
"""
Plesk to Cloudflare DNS Migration Script

This script:
1. Fetches DNS records from Plesk using Plesk API
2. Creates domains (zones) in Cloudflare using Cloudflare API
3. Creates all DNS records in Cloudflare

No Plesk extensions required!
"""

import os
import sys
import requests
import json
from typing import List, Dict, Optional, Any
import logging
from dataclasses import dataclass, asdict
import time
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

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
    api_key: str = ""
    username: str = ""
    password: str = ""

    def __post_init__(self):
        if not self.api_key and not (self.username and self.password):
            raise ValueError("Either api_key or username/password must be provided")


@dataclass
class CloudflareConfig:
    """Cloudflare configuration"""
    api_token: str
    account_id: str = ""  # Optional, but recommended


@dataclass
class DNSRecord:
    """DNS Record structure"""
    type: str
    name: str
    content: str
    ttl: int = 3600
    priority: Optional[int] = None
    proxied: bool = False
    weight: Optional[int] = None
    port: Optional[int] = None
    target: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API calls"""
        data = {
            'type': self.type,
            'name': self.name,
            'ttl': self.ttl,
        }

        # SRV records need special handling for Cloudflare
        if self.type == 'SRV':
            # Cloudflare expects SRV records with a data object
            data['data'] = {
                'priority': int(self.priority) if self.priority is not None else 0,
                'weight': int(self.weight) if self.weight is not None else 0,
                'port': int(self.port) if self.port is not None else 0,
                'target': self.target if self.target else self.content,
                'name': self.name
            }
            # Don't set proxied for SRV records
        else:
            # For all other record types
            data['content'] = self.content
            data['proxied'] = self.proxied

            # MX records need priority
            if self.priority is not None and self.type == 'MX':
                data['priority'] = int(self.priority)

        return data


class PleskAPI:
    """Plesk API client"""

    def __init__(self, config: PleskConfig):
        self.config = config
        self.base_url = f"https://{config.host}:{config.port}/api/v2"
        self.session = requests.Session()
        self.session.verify = False

        # Set up authentication
        if config.api_key:
            self.session.headers.update({'X-API-Key': config.api_key})
        else:
            self.session.auth = (config.username, config.password)

        requests.packages.urllib3.disable_warnings()
        logger.info(f"Initialized Plesk API client for {config.host}")

    def test_connection(self) -> bool:
        """Test connection to Plesk server"""
        try:
            response = self.session.get(f"{self.base_url}/server")
            response.raise_for_status()
            logger.info("✓ Successfully connected to Plesk server")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"✗ Failed to connect to Plesk server: {e}")
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

    def get_dns_records(self, domain_name: str) -> List[DNSRecord]:
        """Get all DNS records for a domain from Plesk"""
        try:
            domain_id = self.get_domain_id(domain_name)
            if not domain_id:
                return []

            logger.info(f"Fetching DNS records for '{domain_name}'...")

            response = self.session.get(f"{self.base_url}/dns/records?domain={domain_name}")
            response.raise_for_status()
            records_data = response.json()

            dns_records = []
            for record in records_data:
                record_type = record.get('type', '').upper()
                host = record.get('host', '')
                opt = record.get('opt', '')

                # Skip certain record types that shouldn't be migrated
                if record_type in ['SOA', 'NS']:
                    logger.debug(f"Skipping {record_type} record: {host}")
                    continue

                # Parse the record based on type
                dns_record = self._parse_plesk_record(record, domain_name)
                if dns_record:
                    dns_records.append(dns_record)

            logger.info(f"✓ Retrieved {len(dns_records)} DNS records from Plesk")
            return dns_records

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching DNS records for '{domain_name}': {e}")
            return []

    def _parse_plesk_record(self, record: Dict, domain_name: str) -> Optional[DNSRecord]:
        """Parse a Plesk DNS record into our DNSRecord format"""
        try:
            record_type = record.get('type', '').upper()
            host = record.get('host', '').rstrip('.')
            opt = record.get('opt', '')
            ttl = int(record.get('ttl', 3600))
            value = record.get('value', '')

            # Debug logging for complex record types
            if record_type in ['SRV', 'MX', 'CAA']:
                logger.debug(f"Parsing {record_type} record: {record}")

            # Normalize the hostname
            if host == domain_name or host == f"{domain_name}.":
                name = domain_name
            elif host.endswith(f".{domain_name}"):
                name = host
            elif host == '' or host == '@':
                name = domain_name
            else:
                name = f"{host}.{domain_name}" if host else domain_name

            # Parse based on record type
            if record_type == 'A':
                return DNSRecord(
                    type='A',
                    name=name,
                    content=value,
                    ttl=ttl
                )
            elif record_type == 'AAAA':
                return DNSRecord(
                    type='AAAA',
                    name=name,
                    content=value,
                    ttl=ttl
                )
            elif record_type == 'CNAME':
                content = opt.rstrip('.')
                return DNSRecord(
                    type='CNAME',
                    name=name,
                    content=value,
                    ttl=ttl
                )
            elif record_type == 'MX':
                # MX records have priority field and target in value
                priority = int(record.get('priority', 10))
                # The mail server is in the value field, strip trailing dot
                mail_server = value.rstrip('.')

                return DNSRecord(
                    type='MX',
                    name=domain_name,
                    content=mail_server,
                    ttl=ttl,
                    priority=priority
                )
            elif record_type == 'TXT':
                # Remove surrounding quotes if present
                content = opt.strip('"')
                return DNSRecord(
                    type='TXT',
                    name=name,
                    content=value,
                    ttl=ttl
                )
            elif record_type == 'SRV':
                # SRV record format in Plesk opt: "priority weight port"
                # Example: "0 5 5269 xmpp-server.example.com."
                opt_parts = record.get('opt', '').split()

                if len(opt_parts) >= 3:
                    priority = int(opt_parts[0]) if opt_parts[0].isdigit() else 0
                    weight = int(opt_parts[1]) if opt_parts[1].isdigit() else 0
                    port = int(opt_parts[2]) if opt_parts[2].isdigit() else 0
                    # Target might be in opt_parts[3] or in value
                    target = opt_parts[3].rstrip('.') if len(opt_parts) > 3 else value.rstrip('.')
                else:
                    logger.warning(f"Invalid SRV record format: {opt_parts}")
                    priority = 0
                    weight = 0
                    port = 0
                    target = value.rstrip('.')

                # Content for SRV must be in format: "priority weight port target"
                content = f"{priority} {weight} {port} {target}"

                return DNSRecord(
                    type='SRV',
                    name=host,
                    content=content,
                    ttl=ttl,
                    priority=priority,
                    weight=weight,
                    port=port,
                    target=target
                )
            elif record_type == 'CAA':
                return DNSRecord(
                    type='CAA',
                    name=name,
                    content=value,
                    ttl=ttl
                )
            else:
                logger.debug(f"Unsupported record type: {record_type}")
                return None

        except Exception as e:
            logger.error(f"Error parsing DNS record: {record} - {e}")
            return None


class CloudflareAPI:
    """Cloudflare API client"""

    def __init__(self, config: CloudflareConfig):
        self.config = config
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {config.api_token}',
            'Content-Type': 'application/json'
        })
        logger.info("Initialized Cloudflare API client")

    def test_connection(self) -> bool:
        """Test connection to Cloudflare API"""
        try:
            response = self.session.get(f"{self.base_url}/user/tokens/verify")
            response.raise_for_status()
            data = response.json()

            if data.get('success'):
                logger.info("✓ Successfully connected to Cloudflare API")
                return True
            else:
                logger.error("✗ Cloudflare API token verification failed")
                return False
        except requests.exceptions.RequestException as e:
            logger.error(f"✗ Failed to connect to Cloudflare API: {e}")
            return False

    def get_zone_id(self, domain_name: str) -> Optional[str]:
        """Get zone ID for a domain, returns None if not found"""
        try:
            response = self.session.get(
                f"{self.base_url}/zones",
                params={'name': domain_name}
            )
            response.raise_for_status()
            data = response.json()

            if data.get('success') and data.get('result'):
                zone_id = data['result'][0]['id']
                logger.info(f"Found existing zone for '{domain_name}': {zone_id}")
                return zone_id
            return None
        except Exception as e:
            logger.debug(f"Error checking for existing zone: {e}")
            return None

    def create_zone(self, domain_name: str) -> Optional[str]:
        """Create a new zone (domain) in Cloudflare"""
        try:
            # Check if zone already exists
            existing_zone_id = self.get_zone_id(domain_name)
            if existing_zone_id:
                logger.info(f"Zone '{domain_name}' already exists")
                return existing_zone_id

            logger.info(f"Creating zone for '{domain_name}'...")

            payload = {
                'name': domain_name,
                'jump_start': False  # Don't auto-scan DNS records
            }

            if self.config.account_id:
                payload['account'] = {'id': self.config.account_id}

            response = self.session.post(f"{self.base_url}/zones", json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get('success'):
                zone_id = data['result']['id']
                nameservers = data['result']['name_servers']
                logger.info(f"✓ Successfully created zone '{domain_name}'")
                logger.info(f"  Zone ID: {zone_id}")
                logger.info(f"  Nameservers: {', '.join(nameservers)}")
                return zone_id
            else:
                errors = data.get('errors', [])
                logger.error(f"Failed to create zone: {errors}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Error creating zone for '{domain_name}': {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return None

    def create_dns_record(self, zone_id: str, record: DNSRecord) -> bool:
        """Create a DNS record in Cloudflare"""
        try:
            response = self.session.post(
                f"{self.base_url}/zones/{zone_id}/dns_records",
                json=record.to_dict()
            )

            data = response.json()

            if data.get('success'):
                logger.info(f"  ✓ Created {record.type} record: {record.name} → {record.content}")
                return True
            else:
                errors = data.get('errors', [])
                # Check if it's a duplicate record error
                if any('already exists' in str(err).lower() for err in errors):
                    logger.warning(f"  ⚠ Record already exists: {record.type} {record.name}")
                    return True
                else:
                    logger.error(f"  ✗ Failed to create record {record.name}: {errors}")
                    return False

        except requests.exceptions.RequestException as e:
            logger.error(f"  ✗ Error creating DNS record {record.name}: {e}")
            return False

    def migrate_dns_records(self, zone_id: str, records: List[DNSRecord]) -> Dict[str, int]:
        """Migrate multiple DNS records to Cloudflare"""
        results = {'success': 0, 'failed': 0, 'skipped': 0}

        logger.info(f"Migrating {len(records)} DNS records...")

        for record in records:
            success = self.create_dns_record(zone_id, record)
            if success:
                results['success'] += 1
            else:
                results['failed'] += 1

            # Small delay to avoid rate limiting
            time.sleep(0.1)

        return results


class DomainMigrator:
    """Main migration orchestrator"""

    def __init__(self, plesk_config: PleskConfig, cloudflare_config: CloudflareConfig):
        self.plesk = PleskAPI(plesk_config)
        self.cloudflare = CloudflareAPI(cloudflare_config)

    def migrate_domain(self, domain_name: str) -> bool:
        """Migrate a single domain from Plesk to Cloudflare"""
        logger.info(f"\n{'='*60}")
        logger.info(f"Migrating domain: {domain_name}")
        logger.info(f"{'='*60}")

        try:
            # Step 1: Get DNS records from Plesk
            logger.info("\n[1/3] Fetching DNS records from Plesk...")
            dns_records = self.plesk.get_dns_records(domain_name)

            if not dns_records:
                logger.warning(f"No DNS records found for '{domain_name}'")
                return False

            logger.info(f"Found {len(dns_records)} DNS records")

            # Step 2: Create zone in Cloudflare
            logger.info("\n[2/3] Creating zone in Cloudflare...")
            zone_id = self.cloudflare.create_zone(domain_name)

            if not zone_id:
                logger.error(f"Failed to create zone for '{domain_name}'")
                return False

            # Step 3: Create DNS records in Cloudflare
            logger.info("\n[3/3] Creating DNS records in Cloudflare...")
            results = self.cloudflare.migrate_dns_records(zone_id, dns_records)

            logger.info(f"\n✓ Migration complete for '{domain_name}':")
            logger.info(f"  - Successfully created: {results['success']} records")
            logger.info(f"  - Failed: {results['failed']} records")

            return results['failed'] == 0

        except Exception as e:
            logger.error(f"Error migrating domain '{domain_name}': {e}")
            return False

    def migrate_domains(self, domain_list: List[str]) -> Dict[str, bool]:
        """Migrate multiple domains"""
        results = {}

        logger.info(f"\n{'='*60}")
        logger.info(f"Starting migration of {len(domain_list)} domain(s)")
        logger.info(f"{'='*60}")

        for i, domain in enumerate(domain_list, 1):
            domain = domain.strip()
            if not domain or domain.startswith('#'):
                continue

            logger.info(f"\n[{i}/{len(domain_list)}] Processing: {domain}")

            success = self.migrate_domain(domain)
            results[domain] = success

            # Delay between domains
            if i < len(domain_list):
                time.sleep(2)

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
            logger.info("\n✓ Successfully migrated:")
            for domain in successful:
                logger.info(f"  • {domain}")

        if failed:
            logger.info("\n✗ Failed to migrate:")
            for domain in failed:
                logger.info(f"  • {domain}")

        logger.info("\n" + "="*60)
        logger.info("IMPORTANT: Update Nameservers")
        logger.info("="*60)
        logger.info("To complete the migration, update the nameservers at your")
        logger.info("domain registrar to point to Cloudflare nameservers.")
        logger.info("\nYou can find the nameservers for each domain in the")
        logger.info("Cloudflare dashboard or in the logs above.")


def load_domains_from_file(file_path: str) -> List[str]:
    """Load domains from a text file"""
    try:
        with open(file_path, 'r') as f:
            domains = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        logger.info(f"Loaded {len(domains)} domain(s) from {file_path}")
        return domains
    except FileNotFoundError:
        logger.error(f"Domain file not found: {file_path}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error reading domain file: {e}")
        sys.exit(1)


def load_config() -> tuple[PleskConfig, CloudflareConfig]:
    """Load configuration from environment variables"""

    # Plesk configuration
    plesk_host = os.getenv('PLESK_HOST')
    plesk_port = int(os.getenv('PLESK_PORT', '8443'))
    plesk_api_key = os.getenv('PLESK_API_KEY', '')
    plesk_username = os.getenv('PLESK_USERNAME', '')
    plesk_password = os.getenv('PLESK_PASSWORD', '')

    # Cloudflare configuration
    cloudflare_token = os.getenv('CLOUDFLARE_API_TOKEN', '')
    cloudflare_account_id = os.getenv('CLOUDFLARE_ACCOUNT_ID', '')

    # Validate required variables
    if not plesk_host:
        logger.error("PLESK_HOST environment variable is required")
        logger.info("\nRequired environment variables:")
        logger.info("  PLESK_HOST: Your Plesk server hostname or IP")
        logger.info("  PLESK_API_KEY: Your Plesk API key (or PLESK_USERNAME/PLESK_PASSWORD)")
        logger.info("  CLOUDFLARE_API_TOKEN: Your Cloudflare API token")
        logger.info("\nOptional:")
        logger.info("  CLOUDFLARE_ACCOUNT_ID: Your Cloudflare account ID")
        sys.exit(1)

    if not cloudflare_token:
        logger.error("CLOUDFLARE_API_TOKEN environment variable is required")
        logger.info("\nTo get your Cloudflare API token:")
        logger.info("  1. Log in to Cloudflare dashboard")
        logger.info("  2. Go to My Profile → API Tokens")
        logger.info("  3. Create a token with 'Edit Zone DNS' permissions")
        sys.exit(1)

    plesk_config = PleskConfig(
        host=plesk_host,
        port=plesk_port,
        api_key=plesk_api_key,
        username=plesk_username,
        password=plesk_password
    )

    cloudflare_config = CloudflareConfig(
        api_token=cloudflare_token,
        account_id=cloudflare_account_id
    )

    return plesk_config, cloudflare_config


def main():
    """Main function"""
    logger.info("="*60)
    logger.info("Plesk to Cloudflare DNS Migration")
    logger.info("="*60)

    # Check command line arguments
    if len(sys.argv) < 2:
        print("Usage: python plesk_to_cloudflare.py <domains_file>")
        print("\nExample:")
        print("  python plesk_to_cloudflare.py domains.txt")
        sys.exit(1)

    domains_file = sys.argv[1]

    # Load configuration
    logger.info("\nLoading configuration...")
    plesk_config, cloudflare_config = load_config()

    # Load domains
    logger.info(f"Loading domains from {domains_file}...")
    domains = load_domains_from_file(domains_file)

    if not domains:
        logger.error("No domains found in the file")
        sys.exit(1)

    # Initialize migrator
    migrator = DomainMigrator(plesk_config, cloudflare_config)

    # Test connections
    logger.info("\nTesting connections...")
    if not migrator.plesk.test_connection():
        logger.error("Cannot connect to Plesk server")
        sys.exit(1)

    if not migrator.cloudflare.test_connection():
        logger.error("Cannot connect to Cloudflare API")
        sys.exit(1)

    # Migrate domains
    results = migrator.migrate_domains(domains)

    # Print summary
    migrator.print_summary(results)

    # Exit with appropriate code
    failed_count = sum(1 for success in results.values() if not success)
    sys.exit(0 if failed_count == 0 else 1)


if __name__ == "__main__":
    main()
