# Namesilo DNS Updater

A lightweight Python script to dynamically update Namesilo DNS records when your public IP changes. Designed to run via cron.

## Setup

1. Install [uv](https://docs.astral.sh/uv/) if not already available.

2. Copy the example config and edit it:
   ```bash
   cp config.toml.example config.toml
   chmod 600 config.toml
   ```

3. Set your Namesilo API key, domain, and host in `config.toml`.

4. Get your API key from the [Namesilo API Manager](https://www.namesilo.com/account/api-manager).

## Configuration

| Key | Section | Description | Default |
|-----|---------|-------------|---------|
| `api_key` | settings | Your Namesilo API key | (required) |
| `domain` | settings | Domain to update | (required) |
| `host` | settings | Subdomain (`@` for bare domain, `www` for www.example.com) | `@` |
| `ttl` | settings | TTL in seconds (minimum 3600) | `7207` |
| `ipv4` | ip | Enable IPv4 (A record) updates | `true` |
| `ipv6` | ip | Enable IPv6 (AAAA record) updates | `false` |
| `config_dir` | paths | Directory for state files | (required) |
| `log_file` | paths | Path to log file | (required) |

## Usage

```bash
# Using default config path (config.toml in current directory)
uv run main.py

# Using custom config path
uv run main.py --config /path/to/config.toml
```

## Cron Setup

Run every 15 minutes:

```cron
*/15 * * * * /path/to/uv run /path/to/namesilo-dns-updater/main.py --config /path/to/config.toml
```

## How It Works

1. Fetches your current public IP using multiple fallback services (ipify, icanhazip, ifconfig.co)
2. Compares against the last known IP stored in `config_dir`
3. If changed, queries Namesilo API for existing DNS records
4. Updates existing record or creates a new one if none exists
5. Logs the result to both console and the configured log file

## IP Detection Fallbacks

For IPv4: `api.ipify.org` -> `icanhazip.com` -> `ifconfig.co/ip`

For IPv6: `api64.ipify.org` -> `ipv6.icanhazip.com` -> `ifconfig.co/ip`
