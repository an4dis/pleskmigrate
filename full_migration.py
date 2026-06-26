#!/usr/bin/env python3
"""
Complete Migration Workflow

This script performs a full migration:
1. Migrate DNS from Plesk to Cloudflare
2. Update nameservers at registrars (Regweb for .gr, Namecheap for others)
"""

import sys
import os
import logging
from plesk_to_cloudflare import (
    DomainMigrator, load_config as load_plesk_cf_config,
    load_domains_from_file
)
from update_nameservers import (
    NameserverUpdater,
    load_config as load_ns_config
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('full_migration.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def main():
    """Main function"""
    logger.info("="*60)
    logger.info("FULL MIGRATION: Plesk → Cloudflare → Registrars")
    logger.info("="*60)

    # Check arguments
    if len(sys.argv) < 2:
        print("Usage: python3 full_migration.py <domains_file>")
        print("\nExample:")
        print("  python3 full_migration.py domains.txt")
        print("\nThis will:")
        print("  1. Migrate DNS records from Plesk to Cloudflare")
        print("  2. Update nameservers at registrars")
        sys.exit(1)

    domains_file = sys.argv[1]

    # Load domains
    logger.info(f"\nLoading domains from {domains_file}...")
    domains = load_domains_from_file(domains_file)

    if not domains:
        logger.error("No domains found in the file")
        sys.exit(1)

    # PHASE 1: Migrate DNS to Cloudflare
    logger.info("\n" + "="*60)
    logger.info("PHASE 1: Migrating DNS to Cloudflare")
    logger.info("="*60)

    try:
        plesk_config, cloudflare_config = load_plesk_cf_config()
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
        migration_results = migrator.migrate_domains(domains)
        migrator.print_summary(migration_results)

        # Check if any domains failed
        failed_migrations = [d for d, success in migration_results.items() if not success]
        if failed_migrations:
            logger.warning(f"\n⚠ {len(failed_migrations)} domain(s) failed DNS migration")
            logger.info("Continuing with nameserver update for successful migrations only...")

    except Exception as e:
        logger.error(f"Error during DNS migration: {e}")
        sys.exit(1)

    # PHASE 2: Update Nameservers at Registrars
    logger.info("\n" + "="*60)
    logger.info("PHASE 2: Updating Nameservers at Registrars")
    logger.info("="*60)

    try:
        # Only update nameservers for successfully migrated domains
        successful_domains = [d for d, success in migration_results.items() if success]

        if not successful_domains:
            logger.error("No domains were successfully migrated. Skipping nameserver update.")
            sys.exit(1)

        logger.info(f"\nUpdating nameservers for {len(successful_domains)} domain(s)...")

        cloudflare_config, regweb_config, namecheap_config = load_ns_config()
        updater = NameserverUpdater(cloudflare_config, regweb_config, namecheap_config)

        # Update nameservers
        ns_results = updater.update_domains(successful_domains)
        updater.print_summary(ns_results)

    except Exception as e:
        logger.error(f"Error during nameserver update: {e}")
        logger.info("DNS records were migrated successfully, but nameserver update failed.")
        logger.info("You can manually update nameservers or run update_nameservers.py separately.")
        sys.exit(1)

    # FINAL SUMMARY
    logger.info("\n" + "="*60)
    logger.info("COMPLETE MIGRATION SUMMARY")
    logger.info("="*60)

    dns_success = sum(1 for success in migration_results.values() if success)
    ns_success = sum(1 for success in ns_results.values() if success)

    logger.info(f"DNS Migration:        {dns_success}/{len(domains)} successful")
    logger.info(f"Nameserver Updates:   {ns_success}/{len(successful_domains)} successful")

    if dns_success == len(domains) and ns_success == len(successful_domains):
        logger.info("\n✓✓✓ FULL MIGRATION COMPLETED SUCCESSFULLY! ✓✓✓")
        logger.info("\nYour domains are now fully migrated to Cloudflare.")
        logger.info("DNS propagation may take 24-48 hours.")
    else:
        logger.warning("\n⚠ MIGRATION COMPLETED WITH SOME ISSUES")
        logger.info("\nCheck the logs above for details on failed domains.")

    logger.info("="*60)


if __name__ == "__main__":
    main()
