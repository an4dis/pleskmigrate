#!/usr/bin/env python3
"""
Update Nameservers at Registrars

This script:
1. Gets nameservers from Cloudflare for each domain
2. Updates nameservers at the appropriate registrar:
   - .gr domains → Regweb EPP
   - Other TLDs → Namecheap API
"""

import os
import sys
import xml.etree.ElementTree as ET
from xml.dom import minidom
import requests
import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import time

# Disable SSL warnings for EPP connections
requests.packages.urllib3.disable_warnings()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('nameserver_update.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class CloudflareConfig:
    """Cloudflare configuration"""
    api_token: str


@dataclass
class RegwebConfig:
    """Regweb EPP configuration for .gr domains"""
    host: str = ""
    port: int = 700
    username: str = ""
    password: str = ""


@dataclass
class NamecheapConfig:
    """Namecheap API configuration"""
    api_user: str = ""
    api_key: str = ""
    username: str = ""
    client_ip: str = ""  # Your whitelisted IP


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

    def get_nameservers(self, domain_name: str) -> Optional[List[str]]:
        """Get nameservers for a domain from Cloudflare"""
        try:
            # Get zone ID first
            response = self.session.get(
                f"{self.base_url}/zones",
                params={'name': domain_name}
            )
            response.raise_for_status()
            data = response.json()

            if not data.get('success') or not data.get('result'):
                logger.error(f"Domain '{domain_name}' not found in Cloudflare")
                return None

            zone = data['result'][0]
            nameservers = zone.get('name_servers', [])

            if nameservers:
                logger.info(f"✓ Got nameservers for '{domain_name}': {', '.join(nameservers)}")
                return nameservers
            else:
                logger.error(f"No nameservers found for '{domain_name}'")
                return None

        except Exception as e:
            logger.error(f"Error getting nameservers for '{domain_name}': {e}")
            return None


class RegwebEPP:
    """Regweb EPP client for .gr domains (HTTP-based)"""

    def __init__(self, config: RegwebConfig):
        self.config = config
        self.session = requests.Session()
        self.session.verify = False  # Disable SSL verification
        self.base_url = f"https://{config.host}:{config.port}/epp/proxy"
        self.clTRID_counter = 0
        self.logged_in = False
        logger.info(f"Initialized Regweb EPP client for {self.base_url}")

    def connect(self) -> bool:
        """Connect to EPP server (HTTP-based)"""
        try:
            logger.info(f"Connecting to EPP server via HTTPS: {self.base_url}")

            # Login via HTTP
            if self._login():
                logger.info("✓ Successfully connected and logged in to EPP server")
                self.logged_in = True
                return True
            else:
                logger.error("✗ EPP login failed")
                return False

        except Exception as e:
            logger.error(f"✗ Failed to connect to EPP server: {e}")
            return False

    def disconnect(self):
        """Disconnect from EPP server"""
        try:
            if self.logged_in:
                self._logout()
                self.logged_in = False
                logger.info("Disconnected from EPP server")
        except Exception as e:
            logger.error(f"Error disconnecting: {e}")

    def _send_command(self, xml_data: str) -> str:
        """Send EPP command via HTTP and receive response"""
        try:
            logger.debug(f"Sending EPP command: {xml_data[:200]}...")

            response = self.session.post(
                self.base_url,
                data=xml_data,
                headers={
                    'Content-Type': 'application/epp+xml',
                    'Accept': 'application/epp+xml'
                },
                timeout=30
            )

            response.raise_for_status()
            response_text = response.text

            logger.debug(f"Received EPP response: {response_text[:200]}...")
            return response_text

        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP request failed: {e}")
            raise

    def _get_clTRID(self) -> str:
        """Generate unique transaction ID"""
        self.clTRID_counter += 1
        return f"CLTR-{int(time.time())}-{self.clTRID_counter}"

    def _login(self) -> bool:
        """Login to EPP server"""
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<epp xmlns="urn:ietf:params:xml:ns:epp-1.0">
  <command>
    <login>
      <clID>{self.config.username}</clID>
      <pw>{self.config.password}</pw>
      <options>
        <version>1.0</version>
        <lang>en</lang>
      </options>
      <svcs>
        <objURI>urn:ietf:params:xml:ns:domain-1.0</objURI>
      </svcs>
    </login>
    <clTRID>{self._get_clTRID()}</clTRID>
  </command>
</epp>"""

        response = self._send_command(xml)

        # Parse response to check for success
        return 'result code="1000"' in response

    def _logout(self):
        """Logout from EPP server"""
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<epp xmlns="urn:ietf:params:xml:ns:epp-1.0">
  <command>
    <logout/>
    <clTRID>{self._get_clTRID()}</clTRID>
  </command>
</epp>"""

        try:
            self._send_command(xml)
        except:
            pass

    def update_nameservers(self, domain_name: str, nameservers: List[str]) -> bool:
        """Update nameservers for a domain"""
        try:
            logger.info(f"Updating nameservers for '{domain_name}' via EPP...")

            # First, get current nameservers to remove them
            info_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<epp xmlns="urn:ietf:params:xml:ns:epp-1.0">
  <command>
    <info>
      <domain:info xmlns:domain="urn:ietf:params:xml:ns:domain-1.0">
        <domain:name>{domain_name}</domain:name>
      </domain:info>
    </info>
    <clTRID>{self._get_clTRID()}</clTRID>
  </command>
</epp>"""

            info_response = self._send_command(info_xml)

            # Parse existing nameservers
            existing_ns = []
            try:
                root = ET.fromstring(info_response)
                for ns in root.findall(".//{urn:ietf:params:xml:ns:domain-1.0}hostObj"):
                    if ns.text:
                        existing_ns.append(ns.text)
            except:
                logger.warning("Could not parse existing nameservers")

            # Build update command
            ns_remove = ""
            if existing_ns:
                ns_remove_list = "\n        ".join([f"<domain:hostObj>{ns}</domain:hostObj>" for ns in existing_ns])
                ns_remove = f"""
      <domain:rem>
        <domain:ns>
          {ns_remove_list}
        </domain:ns>
      </domain:rem>"""

            ns_add_list = "\n        ".join([f"<domain:hostObj>{ns}</domain:hostObj>" for ns in nameservers])
            ns_add = f"""
      <domain:add>
        <domain:ns>
          {ns_add_list}
        </domain:ns>
      </domain:add>"""

            update_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<epp xmlns="urn:ietf:params:xml:ns:epp-1.0">
  <command>
    <update>
      <domain:update xmlns:domain="urn:ietf:params:xml:ns:domain-1.0">
        <domain:name>{domain_name}</domain:name>{ns_remove}{ns_add}
      </domain:update>
    </update>
    <clTRID>{self._get_clTRID()}</clTRID>
  </command>
</epp>"""

            response = self._send_command(update_xml)

            # Check for success
            if 'result code="1000"' in response:
                logger.info(f"  ✓ Successfully updated nameservers for '{domain_name}'")
                return True
            else:
                logger.error(f"  ✗ Failed to update nameservers for '{domain_name}'")
                logger.debug(f"Response: {response}")
                return False

        except Exception as e:
            logger.error(f"  ✗ Error updating nameservers for '{domain_name}': {e}")
            return False


class NamecheapAPI:
    """Namecheap API client"""

    def __init__(self, config: NamecheapConfig):
        self.config = config
        self.base_url = "https://api.namecheap.com/xml.response"
        logger.info("Initialized Namecheap API client")

    def update_nameservers(self, domain_name: str, nameservers: List[str]) -> bool:
        """Update nameservers for a domain"""
        try:
            logger.info(f"Updating nameservers for '{domain_name}' via Namecheap API...")

            # Split domain into SLD and TLD
            parts = domain_name.split('.')
            if len(parts) < 2:
                logger.error(f"Invalid domain name: {domain_name}")
                return False

            sld = '.'.join(parts[:-1])
            tld = parts[-1]

            # Build nameserver parameters
            ns_params = {}
            for i, ns in enumerate(nameservers, 1):
                ns_params[f'Nameservers'] = f"{i}"
                ns_params[f'Nameserver{i}'] = ns

            # API request parameters
            params = {
                'ApiUser': self.config.api_user,
                'ApiKey': self.config.api_key,
                'UserName': self.config.username,
                'ClientIp': self.config.client_ip,
                'Command': 'namecheap.domains.dns.setCustom',
                'SLD': sld,
                'TLD': tld,
                **ns_params
            }

            response = requests.get(self.base_url, params=params, timeout=30)
            response.raise_for_status()

            # Parse XML response
            root = ET.fromstring(response.text)
            status = root.get('Status')

            if status == 'OK':
                logger.info(f"  ✓ Successfully updated nameservers for '{domain_name}'")
                return True
            else:
                errors = root.findall('.//{http://api.namecheap.com/xml.response}Error')
                error_msg = ', '.join([e.text for e in errors]) if errors else 'Unknown error'
                logger.error(f"  ✗ Failed to update nameservers for '{domain_name}': {error_msg}")
                return False

        except Exception as e:
            logger.error(f"  ✗ Error updating nameservers for '{domain_name}': {e}")
            return False


class NameserverUpdater:
    """Main nameserver update orchestrator"""

    def __init__(self, cloudflare_config: CloudflareConfig,
                 regweb_config: Optional[RegwebConfig] = None,
                 namecheap_config: Optional[NamecheapConfig] = None):
        self.cloudflare = CloudflareAPI(cloudflare_config)
        self.regweb_config = regweb_config
        self.namecheap_config = namecheap_config
        self.regweb_client = None

    def update_domain(self, domain_name: str) -> bool:
        """Update nameservers for a single domain"""
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing domain: {domain_name}")
        logger.info(f"{'='*60}")

        # Step 1: Get nameservers from Cloudflare
        nameservers = self.cloudflare.get_nameservers(domain_name)
        if not nameservers:
            logger.error(f"Skipping '{domain_name}' - no nameservers found")
            return False

        # Step 2: Determine registrar based on TLD
        tld = domain_name.split('.')[-1].lower()

        if tld == 'gr':
            # Use Regweb EPP
            if not self.regweb_config:
                logger.error("Regweb EPP credentials not configured")
                return False

            # Connect to EPP if not already connected
            if not self.regweb_client:
                self.regweb_client = RegwebEPP(self.regweb_config)
                if not self.regweb_client.connect():
                    return False

            return self.regweb_client.update_nameservers(domain_name, nameservers)

        else:
            # Use Namecheap API
            if not self.namecheap_config:
                logger.error("Namecheap API credentials not configured")
                return False

            namecheap = NamecheapAPI(self.namecheap_config)
            return namecheap.update_nameservers(domain_name, nameservers)

    def update_domains(self, domains: List[str]) -> Dict[str, bool]:
        """Update nameservers for multiple domains"""
        results = {}

        logger.info(f"\n{'='*60}")
        logger.info(f"Starting nameserver update for {len(domains)} domain(s)")
        logger.info(f"{'='*60}")

        try:
            for i, domain in enumerate(domains, 1):
                domain = domain.strip()
                if not domain or domain.startswith('#'):
                    continue

                logger.info(f"\n[{i}/{len(domains)}] Processing: {domain}")

                success = self.update_domain(domain)
                results[domain] = success

                # Delay between domains
                if i < len(domains):
                    time.sleep(2)

        finally:
            # Disconnect EPP if connected
            if self.regweb_client:
                self.regweb_client.disconnect()

        return results

    def print_summary(self, results: Dict[str, bool]):
        """Print update summary"""
        successful = [d for d, success in results.items() if success]
        failed = [d for d, success in results.items() if not success]

        logger.info("\n" + "="*60)
        logger.info("NAMESERVER UPDATE SUMMARY")
        logger.info("="*60)
        logger.info(f"Total domains: {len(results)}")
        logger.info(f"Successful: {len(successful)}")
        logger.info(f"Failed: {len(failed)}")

        if successful:
            logger.info("\n✓ Successfully updated:")
            for domain in successful:
                logger.info(f"  • {domain}")

        if failed:
            logger.info("\n✗ Failed to update:")
            for domain in failed:
                logger.info(f"  • {domain}")


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


def load_config() -> Tuple[CloudflareConfig, Optional[RegwebConfig], Optional[NamecheapConfig]]:
    """Load configuration from environment variables"""

    # Cloudflare
    cf_token = os.getenv('CLOUDFLARE_API_TOKEN', '')

    # Regweb EPP
    regweb_host = os.getenv('REGWEB_EPP_HOST', 'epp.registry.gr')
    regweb_port = int(os.getenv('REGWEB_EPP_PORT', '700'))
    regweb_user = os.getenv('REGWEB_USERNAME', '')
    regweb_pass = os.getenv('REGWEB_PASSWORD', '')

    # Namecheap
    nc_api_user = os.getenv('NAMECHEAP_API_USER', '')
    nc_api_key = os.getenv('NAMECHEAP_API_KEY', '')
    nc_username = os.getenv('NAMECHEAP_USERNAME', '')
    nc_client_ip = os.getenv('NAMECHEAP_CLIENT_IP', '')

    # Validate
    if not cf_token:
        logger.error("CLOUDFLARE_API_TOKEN is required")
        logger.info("\nRequired environment variables:")
        logger.info("  CLOUDFLARE_API_TOKEN: Your Cloudflare API token")
        logger.info("\nFor .gr domains (Regweb EPP):")
        logger.info("  REGWEB_USERNAME: Your Regweb username")
        logger.info("  REGWEB_PASSWORD: Your Regweb password")
        logger.info("\nFor other domains (Namecheap):")
        logger.info("  NAMECHEAP_API_USER: Your Namecheap API username")
        logger.info("  NAMECHEAP_API_KEY: Your Namecheap API key")
        logger.info("  NAMECHEAP_USERNAME: Your Namecheap username")
        logger.info("  NAMECHEAP_CLIENT_IP: Your whitelisted IP address")
        sys.exit(1)

    cloudflare_config = CloudflareConfig(api_token=cf_token)

    regweb_config = None
    if regweb_user and regweb_pass:
        regweb_config = RegwebConfig(
            host=regweb_host,
            port=regweb_port,
            username=regweb_user,
            password=regweb_pass
        )

    namecheap_config = None
    if nc_api_user and nc_api_key:
        namecheap_config = NamecheapConfig(
            api_user=nc_api_user,
            api_key=nc_api_key,
            username=nc_username,
            client_ip=nc_client_ip
        )

    return cloudflare_config, regweb_config, namecheap_config


def main():
    """Main function"""
    logger.info("="*60)
    logger.info("Nameserver Update - Cloudflare to Registrars")
    logger.info("="*60)

    # Check arguments
    if len(sys.argv) < 2:
        print("Usage: python3 update_nameservers.py <domains_file>")
        print("\nExample:")
        print("  python3 update_nameservers.py domains.txt")
        sys.exit(1)

    domains_file = sys.argv[1]

    # Load configuration
    logger.info("\nLoading configuration...")
    cloudflare_config, regweb_config, namecheap_config = load_config()

    # Load domains
    logger.info(f"Loading domains from {domains_file}...")
    domains = load_domains_from_file(domains_file)

    if not domains:
        logger.error("No domains found in the file")
        sys.exit(1)

    # Initialize updater
    updater = NameserverUpdater(cloudflare_config, regweb_config, namecheap_config)

    # Update nameservers
    results = updater.update_domains(domains)

    # Print summary
    updater.print_summary(results)

    # Exit with appropriate code
    failed_count = sum(1 for success in results.values() if not success)
    sys.exit(0 if failed_count == 0 else 1)


if __name__ == "__main__":
    main()
