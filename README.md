# Plesk Migration Toolkit

A collection of Python scripts to automate domain migrations from Plesk to Cloudflare, update nameservers at registrars, and manage SSL certificates.

## Prerequisites

- **Python 3.7+**
- **Plesk API access** (API key or username/password)
- **Cloudflare API token** (with Zone:Read, Zone:Edit, DNS:Edit permissions)
- **Network access** to Plesk server on port 8443 (HTTPS)

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Credentials are loaded from environment variables or a `.env` file.

### Plesk

| Variable | Description |
|---|---|
| `PLESK_HOST` | Plesk server hostname or IP (required) |
| `PLESK_PORT` | Plesk API port (default: 8443) |
| `PLESK_API_KEY` | Plesk API key (recommended) |
| `PLESK_USERNAME` | Plesk username (alternative to API key) |
| `PLESK_PASSWORD` | Plesk password |

### Cloudflare

| Variable | Description |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token (required for direct API scripts) |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID (optional, auto-detected) |

### Registrar Credentials (for nameserver updates)

| Variable | Description |
|---|---|
| `REGWEB_USERNAME` | Regweb EPP username (.gr domains) |
| `REGWEB_PASSWORD` | Regweb EPP password |
| `REGWEB_EPP_HOST` | Regweb EPP host (default: regepp.ics.forth.gr) |
| `REGWEB_EPP_PORT` | Regweb EPP port (default: 700) |
| `NAMECHEAP_API_USER` | Namecheap API username |
| `NAMECHEAP_API_KEY` | Namecheap API key |
| `NAMECHEAP_USERNAME` | Namecheap account username |
| `NAMECHEAP_CLIENT_IP` | Your whitelisted IP for Namecheap API |

### Option A: Environment Variables

```bash
export PLESK_HOST="plesk.example.com"
export PLESK_API_KEY="your-api-key"
export CLOUDFLARE_API_TOKEN="your-cloudflare-token"
export CLOUDFLARE_ACCOUNT_ID="your-account-id"
```

### Option B: .env File

Create a `.env` file in the project root:

```
PLESK_HOST=plesk.example.com
PLESK_API_KEY=your-api-key
CLOUDFLARE_API_TOKEN=your-cloudflare-token
CLOUDFLARE_ACCOUNT_ID=your-account-id
```

## Scripts

### Primary Migration

| Script | Description |
|---|---|
| `plesk_to_cloudflare.py domains.txt` | Fetches DNS records from Plesk and creates them in Cloudflare (no extension needed) |
| `migrate_to_cloudflare.py domains.txt` | Uses the Plesk Cloudflare DNS extension to enable Cloudflare per-domain |
| `full_migration.py domains.txt` | Runs DNS migration + nameserver update in one workflow |

### Nameserver Management

| Script | Description |
|---|---|
| `update_nameservers.py domains.txt` | Updates nameservers at registrars: Regweb EPP for .gr, Namecheap API for others |
| `check_update_ns_records.py` | Checks/updates NS records within Cloudflare zones |
| `cf_domain_sync.py domains.txt --token TOKEN` | Creates missing domains on Cloudflare free plan and exports nameservers to CSV |

### DNS & SSL Utilities

| Script | Description |
|---|---|
| `renew_expired_ssl.py` | Finds expired SSL certs in Plesk and renews via Let's Encrypt |
| `alias_mail_sync.py input.csv --token TOKEN` | Syncs DNS records from domain zones to alias zones in Cloudflare |
| `cf_unproxy.py domains.txt --token TOKEN` | Disables Cloudflare proxy (orange cloud) on all DNS records |

### Test Scripts

| Script | Description |
|---|---|
| `python test/test_api_connection.py` | Debug Plesk API connection and Cloudflare extension endpoints |
| `python test/test_api_connection.py domain.gr` | Test with a specific domain |
| `test/test_connections.py` | Verify both Plesk and Cloudflare connections |

## Usage Examples

### Direct DNS Migration (Recommended)

```bash
python plesk_to_cloudflare.py domains.txt
```

### Nameserver Update

```bash
python update_nameservers.py domains.txt
```

### Full Migration (DNS + Nameservers)

```bash
python full_migration.py domains.txt
```

### Cloudflare Zone Sync

```bash
python cf_domain_sync.py domains.txt --token "$CLOUDFLARE_API_TOKEN"
```

### SSL Renewal

```bash
python renew_expired_ssl.py
python renew_expired_ssl.py --dry-run
python renew_expired_ssl.py --domain example.com
```

## Domain File Format

Create a text file with one domain per line:

```
example.com
mywebsite.com
anotherdomain.org
```

Lines starting with `#` are treated as comments.

## Domain File Format (CSV)

For `alias_mail_sync.py`, use a CSV with header:

```csv
alias,domain
alias-domain.gr,main-domain.gr
```

## Output

- **Console output**: Real-time progress and status messages
- **Log files**: `migration.log`, `nameserver_update.log`, `ssl_renewal.log`, etc.

## Troubleshooting

### Connection Issues

- Verify your Plesk hostname/IP is correct
- Ensure port 8443 is accessible
- Check that your API key or credentials are valid

### Cloudflare API Issues

- Verify `CLOUDFLARE_API_TOKEN` is set and has proper permissions
- Token needs Zone:Read, Zone:Edit, and DNS:Edit permissions

## Security Notes

- **Never commit credentials to version control**
- Use environment variables or a `.env` file (`.env` is gitignored)
- Consider IP restrictions for your Plesk API key
- The scripts disable SSL verification for self-signed certificates

## License

Provided as-is for automation purposes.
