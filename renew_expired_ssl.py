#!/usr/bin/env python3
"""
Plesk Expired SSL Renewal Script
Finds expired SSL certificates and renews them for root and www entries only.
"""

import os
import sys
import requests
import json
import urllib3
import re
from datetime import datetime
import logging
from typing import List, Dict, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ssl_renewal.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Suppress insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def load_env():
    """Manually load .env file if it exists"""
    if os.path.exists('.env'):
        with open('.env') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        key, value = parts
                        os.environ[key] = value.strip('"').strip("'")

class PleskSSLManager:
    def __init__(self):
        load_env()
        self.host = os.getenv('PLESK_HOST')
        self.port = os.getenv('PLESK_PORT', '8443')
        self.user = os.getenv('PLESK_USERNAME')
        self.password = os.getenv('PLESK_PASSWORD')
        self.api_key = os.getenv('PLESK_API_KEY')
        self.email = os.getenv('LETSENCRYPT_EMAIL', f"admin@{self.host}")
        
        if not self.host:
            logger.error("PLESK_HOST not found in environment")
            sys.exit(1)
            
        self.base_url = f"https://{self.host}:{self.port}/api/v2"
        self.session = requests.Session()
        self.session.verify = False
        
        if self.api_key:
            self.session.headers.update({'X-API-Key': self.api_key})
        else:
            self.session.auth = (self.user, self.password)

    def test_connection(self) -> bool:
        """Test connection to Plesk API"""
        try:
            # Try a simple endpoint
            response = self.session.get(f"{self.base_url}/server", timeout=10)
            if response.status_code == 200:
                logger.info(f"✓ Connected to Plesk: {self.host}")
                return True
            else:
                logger.error(f"✗ Connection failed with status {response.status_code}")
                # Log a snippet of the response for debugging (might be a 404 or something else)
                logger.debug(f"Response: {response.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"✗ Connection error: {e}")
            return False

    def get_all_domains(self) -> List[Dict]:
        """Fetch all domains from Plesk"""
        try:
            response = self.session.get(f"{self.base_url}/domains", timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching domains: {e}")
            return []

    def get_certificate_expiry(self, domain_name: str) -> Optional[datetime]:
        """Get expiration date for the currently used certificate of a domain"""
        try:
            # 1. First, list all certificates to find which one is "Used"
            payload = {"params": ["--list", "-domain", domain_name]}
            response = self.session.post(
                f"{self.base_url}/cli/certificate/call",
                json=payload,
                timeout=15
            )
            
            if response.status_code != 200:
                logger.error(f"  Error calling certificate --list: {response.status_code}")
                return None
                
            result = response.json()
            stdout = result.get('stdout', '')
            stderr = result.get('stderr', '')
            
            if stderr:
                logger.debug(f"  Stderr from certificate --list: {stderr}")

            # Parse the table format:
            # CSR Priv Cert CA Name Used
            # Y   Y    Y    Y  Name 1
            lines = stdout.strip().split('\n')
            if len(lines) < 2:
                logger.info(f"  No certificates found in table for {domain_name}")
                logger.debug(f"  Stdout: {stdout}")
                return None

            # Skip header
            cert_name = None
            for line in lines[1:]:
                parts = line.split()
                if not parts: continue
                # The last part is 'Used' (count)
                # The columns before it are CSR, Priv, Cert, CA
                # Everything in between is the Name
                try:
                    used_count = int(parts[-1])
                    if used_count > 0:
                        # Certificate name is between column 4 and the last column
                        # Column indices: 0:CSR, 1:Priv, 2:Cert, 3:CA, 4 to -1: Name
                        cert_name = " ".join(parts[4:-1])
                        logger.info(f"  Found active certificate: '{cert_name}' (Used: {used_count})")
                        break
                except (ValueError, IndexError):
                    continue

            if not cert_name:
                logger.info(f"  No active (Used > 0) certificate found for {domain_name}")
                return None

            # 2. Now get details for THIS certificate
            # Unfortunately there isn't a direct --info command in the CLI utility that gives expiry easily
            # But the Let's Encrypt extension can give status if it's a LE cert
            return self.get_le_status_expiry(domain_name)

        except Exception as e:
            logger.error(f"Error checking certificate for {domain_name}: {e}")
            return None

    def get_le_status_expiry(self, domain_name: str) -> Optional[datetime]:
        """Try to get expiry via Let's Encrypt extension status"""
        try:
            payload = {
                "params": ["--exec", "letsencrypt", "cli.php", "--status", "-d", domain_name]
            }
            response = self.session.post(
                f"{self.base_url}/cli/extension/call",
                json=payload,
                timeout=20
            )
            
            if response.status_code == 200:
                result = response.json()
                stdout = result.get('stdout', '')
                # Status output often contains "Valid until: ..."
                match = re.search(r'Valid until:\s+(.*)', stdout)
                if match:
                    date_str = match.group(1).strip()
                    return self.parse_plesk_date(date_str)
                
                # Or sometimes it shows it in a different way
                match = re.search(r'Expiration date:\s+(.*)', stdout)
                if match:
                    date_str = match.group(1).strip()
                    return self.parse_plesk_date(date_str)
            
            # If LE status fails or isn't a LE cert, we might have to assume it's expired
            # if we really want to be safe, but that's aggressive.
            # Instead, let's try to get expiry by connecting to the domain directly via HTTPS
            return self.get_remote_cert_expiry(domain_name)
        except:
            return self.get_remote_cert_expiry(domain_name)

    def get_remote_cert_expiry(self, domain_name: str) -> Optional[datetime]:
        """Get certificate expiry by connecting to the domain's HTTPS port"""
        import ssl
        import socket
        from datetime import datetime

        logger.info(f"  Checking remote certificate for {domain_name}...")
        try:
            # We use a simpler approach that doesn't require complex binary parsing
            # if we can get the dict back.
            context = ssl.create_default_context()
            # We want to be able to read the cert even if it's expired
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            
            with socket.create_connection((domain_name, 443), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=domain_name) as ssock:
                    cert_dict = ssock.getpeercert()
                    # If getpeercert() is empty (binary=False and verify_mode=CERT_NONE),
                    # we need to use binary=True and parse it.
                    if not cert_dict:
                        # Fallback: try to get binary and use a different way or 
                        # just try to verify normally to see if it's expired
                        pass
            
            # Try again WITH verification enabled. 
            # If it fails with "expired", we know it's expired.
            # If it succeeds, we get the date.
            context = ssl.create_default_context()
            try:
                with socket.create_connection((domain_name, 443), timeout=5) as sock:
                    with context.wrap_socket(sock, server_hostname=domain_name) as ssock:
                        cert_dict = ssock.getpeercert()
                        expiry_str = cert_dict.get('notAfter')
                        # Format: 'Oct 10 12:00:00 2026 GMT'
                        return datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
            except ssl.SSLCertVerificationError as e:
                if "certificate has expired" in str(e).lower():
                    logger.warning(f"  Remote check confirmed: Certificate IS EXPIRED")
                    return datetime(2000, 1, 1)
                elif "hostname doesn't match" in str(e).lower():
                    # If hostname doesn't match, we might be hitting a default cert
                    logger.warning(f"  Remote check: Hostname mismatch (using default cert?)")
                    return None
                return None
            except Exception as e:
                logger.debug(f"  Remote verification failed: {e}")
                return None
        except Exception as e:
            logger.debug(f"  Remote connection failed: {e}")
            return None

    def parse_plesk_date(self, date_str: str) -> Optional[datetime]:
        """Parse Plesk date format (e.g., 'Oct 10, 2026' or '2026-10-10')"""
        # Clean up the string - take the first few parts that look like a date
        # (Avoid time parts or timezone info for basic expiry check)
        clean_date = date_str.strip()
        
        formats = [
            "%b %d, %Y",  # Oct 10, 2026
            "%Y-%m-%d",   # 2026-10-10
            "%d %b %Y",   # 10 Oct 2026
            "%m/%d/%Y",   # 10/10/2026
        ]
        
        for fmt in formats:
            try:
                # Try to parse the beginning of the string
                # We use a trick: match only the length of the format
                return datetime.strptime(clean_date, fmt)
            except ValueError:
                # Try to see if the date is at the beginning of a longer string
                try:
                    # This is a bit risky but often works for 'Oct 10, 2026 12:00:00'
                    return datetime.strptime(' '.join(clean_date.split()[:3]), fmt)
                except ValueError:
                    continue
                    
        logger.warning(f"Could not parse date: {date_str}")
        return None

    def renew_with_letsencrypt(self, domain_name: str) -> bool:
        """Renew/Issue Let's Encrypt certificate for root and www only"""
        logger.info(f"Attempting to renew SSL for {domain_name} (root and www only)...")
        
        # Build params for root and www
        # We use -d for each domain
        params = [
            "--exec", "letsencrypt", "cli.php",
            "-d", domain_name,
            "-d", f"www.{domain_name}",
            "-m", self.email,
            "--renew"  # Try to renew first
        ]
        
        payload = {"params": params}
        try:
            response = self.session.post(
                f"{self.base_url}/cli/extension/call",
                json=payload,
                timeout=60
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    logger.info(f"✓ Successfully renewed SSL for {domain_name}")
                    return True
                else:
                    logger.debug(f"  Extension output: {result.get('stdout', '')} {result.get('stderr', '')}")
                    # If renewal fails (e.g. no existing cert), try without --renew to issue new
                    logger.info("  Renewal failed, trying to issue new certificate...")
                    if "--renew" in params:
                        params.remove("--renew")
                    payload["params"] = params
                    response = self.session.post(
                        f"{self.base_url}/cli/extension/call",
                        json=payload,
                        timeout=60
                    )
                    if response.status_code == 200 and response.json().get('code') == 0:
                        logger.info(f"✓ Successfully issued new SSL for {domain_name}")
                        return True
            
            logger.error(f"✗ Failed to renew SSL for {domain_name}. Status: {response.status_code}")
            logger.error(f"  Details: {response.text}")
            return False
        except Exception as e:
            logger.error(f"Error renewing SSL for {domain_name}: {e}")
            return False

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Plesk Expired SSL Renewal Script")
    parser.add_argument("--domain", help="Check only a specific domain")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually renew, just show what would be done")
    parser.add_argument("--force", action="store_true", help="Try to renew even if current certificate info is missing")
    parser.add_argument("--days", type=int, default=0, help="Renew if expiring within this many days (default 0, i.e. only expired)")
    args = parser.parse_args()

    manager = PleskSSLManager()
    
    if not manager.test_connection():
        logger.error("Exiting due to connection failure.")
        # Some servers might not have /server but have other endpoints
        # We'll continue and see if get_all_domains works unless it's a critical error
    
    if args.domain:
        domains = [{'name': args.domain}]
    else:
        domains = manager.get_all_domains()
        
    if not domains:
        logger.warning("No domains found or failed to fetch domains.")
        return

    logger.info(f"Checking {len(domains)} domain(s) for SSL status...")
    
    now = datetime.now()
    expired_count = 0
    renewed_count = 0
    
    for domain in domains:
        name = domain.get('name')
        if not name:
            continue
            
        logger.info(f"Checking {name}...")
        expiry = manager.get_certificate_expiry(name)
        
        should_renew = False
        if expiry:
            logger.info(f"  Current certificate valid until: {expiry.strftime('%Y-%m-%d')}")
            if expiry < now:
                logger.warning(f"  !!! EXPIRED !!! (Expired on {expiry.strftime('%Y-%m-%d')})")
                should_renew = True
            elif args.days > 0 and expiry < now + timedelta(days=args.days):
                logger.warning(f"  Expiring soon (in {(expiry - now).days} days)")
                should_renew = True
        else:
            logger.info("  No active certificate found via internal checks or remote connection.")
            if args.force:
                logger.warning("  [FORCE] No certificate found, but forcing renewal attempt...")
                should_renew = True

        if should_renew:
            expired_count += 1
            if args.dry_run:
                logger.info(f"  [DRY-RUN] Would renew SSL for {name}")
            else:
                if manager.renew_with_letsencrypt(name):
                    renewed_count += 1
                else:
                    logger.error(f"  Failed to renew SSL for {name}. Check ssl_renewal.log for details.")

    logger.info("\n" + "="*30)
    logger.info(f"Summary:")
    logger.info(f"  Total domains checked: {len(domains)}")
    logger.info(f"  Expired/Expiring:      {expired_count}")
    logger.info(f"  Successfully renewed: {renewed_count if not args.dry_run else 0} {'(Dry-run)' if args.dry_run else ''}")
    logger.info("="*30)

if __name__ == "__main__":
    from datetime import timedelta
    main()
