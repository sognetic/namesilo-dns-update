# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "dnspython>=2.7.0",
#     "requests>=2.25.1",
# ]
# ///

import argparse
import logging
import sys
import tomllib
from pathlib import Path

import dns.resolver
import requests

NAMESILO_API = "https://www.namesilo.com/api/{operation}?version=1&type=json&key={api_key}&domain={domain}"

IP_SERVICES = {
    "ipv4": [
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://ifconfig.co/ip",
    ],
    "ipv6": [
        "https://api64.ipify.org",
        "https://ipv6.icanhazip.com",
        "https://ifconfig.co/ip",
    ],
}


class IPDetectionError(Exception):
    pass


class NamesiloAPIError(Exception):
    pass


class NamesiloDNSUpdater:
    def __init__(self, config_path):
        with open(config_path, "rb") as f:
            self._config = tomllib.load(f)

        self._api_key = self._config["settings"]["api_key"]
        self._domain = self._config["settings"]["domain"]
        self._host = self._config["settings"]["host"]
        self._ttl = self._config["settings"]["ttl"]
        self._ipv6 = self._config["ip"].get("ipv6", False)

        self._config_dir = Path(self._config["paths"]["config_dir"])
        self._config_dir.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger("namesilo_dns_updater")
        self._configure_logging()

    def _configure_logging(self):
        self._logger.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(console_handler)

        if "log_file" in self._config.get("paths", {}):
            log_file = Path(self._config["paths"]["log_file"])
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            self._logger.addHandler(file_handler)

    @property
    def _record_type(self):
        return "AAAA" if self._ipv6 else "A"

    @property
    def _ip_type(self):
        return "ipv6" if self._ipv6 else "ipv4"

    @property
    def _full_hostname(self):
        if self._host == "@":
            return self._domain
        return f"{self._host}.{self._domain}"

    @property
    def _state_file(self):
        return self._config_dir / f"{self._domain}_{self._host}.ip"

    def _get_public_ip(self):
        services = IP_SERVICES[self._ip_type]
        errors = []
        for service in services:
            try:
                response = requests.get(service, timeout=10)
                ip = response.text.strip()
                if ip and not ip.startswith("<"):
                    self._logger.info("Current IP%s: %s", self._ip_type[-1], ip)
                    return ip
            except Exception as e:
                errors.append(f"{service}: {e}")
        raise IPDetectionError(
            f"Unable to obtain {self._ip_type.upper()} address from any service: " + "; ".join(errors)
        )

    def _read_last_ip(self):
        if self._state_file.exists():
            return self._state_file.read_text().strip()
        return None

    def _write_last_ip(self, ip):
        self._state_file.write_text(ip)

    def _namesilo_api_call(self, url):
        response = requests.get(url, timeout=30)
        return response.json()

    def _query_authoritative_dns(self):
        hostname = self._full_hostname
        try:
            ns_answer = dns.resolver.resolve(self._domain, "NS")
            ns_names = [str(rdata.target) for rdata in ns_answer]
            if not ns_names:
                return None

            resolver = dns.resolver.Resolver()
            resolver.nameservers = []
            for ns_name in ns_names:
                try:
                    for rdata in dns.resolver.resolve(ns_name, "A"):
                        resolver.nameservers.append(str(rdata.address))
                    break
                except Exception:
                    continue

            if not resolver.nameservers:
                return None

            answers = resolver.resolve(hostname, self._record_type)
            return [rdata.address for rdata in answers]
        except dns.resolver.NXDOMAIN:
            return []
        except dns.resolver.NoAnswer:
            return []
        except Exception as e:
            self._logger.warning("Authoritative DNS query failed for %s: %s", hostname, e)
            return None

    def _get_dns_records(self):
        url = NAMESILO_API.format(operation="dnsListRecords", api_key=self._api_key, domain=self._domain)
        self._logger.info("Fetching DNS records for %s...", self._domain)
        response = self._namesilo_api_call(url)
        code = response.get("reply", {}).get("code")
        if code == 300:
            return response
        detail = response.get("reply", {}).get("detail", "Unknown error")
        raise NamesiloAPIError(f"API error {code}: {detail}")

    def _find_records(self, records):
        matches = []
        for record in records["reply"].get("resource_record", []):
            if record.get("host") == self._host and record.get("type") == self._record_type:
                matches.append(record)
        return matches

    def _log_initial_status(self, records):
        matches = self._find_records(records)
        record = matches[0] if len(matches) == 1 else None

        namesilo_ip = record.get("value", "-") if record else "-"
        auth_ips = self._query_authoritative_dns()
        auth_str = ",".join(auth_ips) if auth_ips is not None else "?"
        record_id = record.get("record_id", "-") if record else "-"

        self._logger.info(
            "Status %s [%s]: namesilo=%s auth=%s rid=%s",
            self._full_hostname, self._record_type, namesilo_ip, auth_str, record_id,
        )

    def _update_dns_record(self, records, current_ip):
        matches = self._find_records(records)

        if len(matches) == 0:
            raise NamesiloAPIError(
                f"No {self._record_type} record found for {self._full_hostname} — "
                f"create it manually in Namesilo first"
            )
        if len(matches) > 1:
            raise NamesiloAPIError(
                f"Multiple {self._record_type} records found for {self._full_hostname} — "
                f"cannot determine which to update"
            )

        record = matches[0]
        record_id = record["record_id"]
        existing_ip = record.get("value", "")
        if existing_ip == current_ip:
            self._logger.info("DNS record already points to %s, no update needed", current_ip)
            self._write_last_ip(current_ip)
            return
        url = (
            f"https://www.namesilo.com/api/dnsUpdateRecord?version=1&type=json"
            f"&key={self._api_key}&domain={self._domain}&rrid={record_id}"
            f"&rrhost={self._host}&rrvalue={current_ip}&rrttl={self._ttl}"
        )
        self._logger.info("Updating existing record: %s.%s -> %s (was %s)", self._host, self._domain, current_ip, existing_ip)

        response = self._namesilo_api_call(url)
        code = response.get("reply", {}).get("code")
        if code != 300:
            detail = response.get("reply", {}).get("detail", "Unknown error")
            raise NamesiloAPIError(f"API error {code}: {detail}")

    def run(self, use_cache=True):
        records = self._get_dns_records()
        self._log_initial_status(records)

        current_ip = self._get_public_ip()

        if use_cache:
            last_ip = self._read_last_ip()
            if current_ip == last_ip:
                self._logger.info("IP address unchanged: %s", current_ip)
                return
            self._logger.info("IP address changed: %s -> %s", last_ip, current_ip)
        else:
            self._logger.info("Cache disabled, forcing update")

        self._update_dns_record(records, current_ip)
        self._write_last_ip(current_ip)
        self._logger.info("Successfully updated DNS record to %s", current_ip)


def main():
    parser = argparse.ArgumentParser(description="Namesilo DNS Updater - Dynamic DNS update tool")
    parser.add_argument("--config", default="config.toml", help="Path to config file (default: config.toml)")
    parser.add_argument("--no-cache", action="store_true", help="Disable IP cache, always update DNS record")
    args = parser.parse_args()

    try:
        updater = NamesiloDNSUpdater(args.config)
        updater.run(use_cache=not args.no_cache)
    except Exception as e:
        logging.getLogger("namesilo_dns_updater").error("Error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
