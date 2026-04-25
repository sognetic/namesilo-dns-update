# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests>=2.25.1",
# ]
# ///

import argparse
import json
import logging
import sys
import tomllib
from pathlib import Path

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
        self._ipv4 = self._config["ip"].get("ipv4", True)
        self._ipv6 = self._config["ip"].get("ipv6", False)

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

    def _state_file_path(self):
        config_dir = Path(self._config["paths"]["config_dir"])
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / f"{self._domain}_{self._host}.ip"

    def _read_last_ip(self):
        state_file = self._state_file_path()
        if state_file.exists():
            return state_file.read_text().strip()
        return None

    def _write_last_ip(self, ip):
        self._state_file_path().write_text(ip)

    def _namesilo_api_call(self, url):
        response = requests.get(url, timeout=30)
        return response.json()

    def _get_dns_records(self):
        url = NAMESILO_API.format(operation="dnsListRecords", api_key=self._api_key, domain=self._domain)
        self._logger.info("Fetching DNS records for %s...", self._domain)
        response = self._namesilo_api_call(url)
        code = response.get("reply", {}).get("code")
        if code == "300":
            return response
        detail = response.get("reply", {}).get("detail", "Unknown error")
        raise NamesiloAPIError(f"API error {code}: {detail}")

    def _find_record(self, records):
        if self._host == "@":
            full_host = self._domain
        else:
            full_host = f"{self._host}.{self._domain}"
        for record in records["reply"].get("resource_record", []):
            if record.get("host") == full_host and record.get("type") == self._record_type:
                return record
        return None

    def _update_dns_record(self, current_ip):
        records = self._get_dns_records()
        record = self._find_record(records)

        if record:
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
        else:
            url = (
                f"https://www.namesilo.com/api/dnsAddRecord?version=1&type=json"
                f"&key={self._api_key}&domain={self._domain}&rrtype={self._record_type}"
                f"&rrhost={self._host}&rrvalue={current_ip}&rrttl={self._ttl}"
            )
            self._logger.info("Creating new record: %s.%s -> %s", self._host, self._domain, current_ip)

        response = self._namesilo_api_call(url)
        code = response.get("reply", {}).get("code")
        if code != "300":
            detail = response.get("reply", {}).get("detail", "Unknown error")
            raise NamesiloAPIError(f"API error {code}: {detail}")

    def run(self):
        current_ip = self._get_public_ip()
        last_ip = self._read_last_ip()

        if current_ip == last_ip:
            self._logger.info("IP address unchanged: %s", current_ip)
            return

        self._logger.info("IP address changed: %s -> %s", last_ip, current_ip)
        self._update_dns_record(current_ip)
        self._write_last_ip(current_ip)
        self._logger.info("Successfully updated DNS record to %s", current_ip)


def main():
    parser = argparse.ArgumentParser(description="Namesilo DNS Updater - Dynamic DNS update tool")
    parser.add_argument("--config", default="config.toml", help="Path to config file (default: config.toml)")
    args = parser.parse_args()

    try:
        updater = NamesiloDNSUpdater(args.config)
        updater.run()
    except Exception as e:
        logging.getLogger("namesilo_dns_updater").error("Error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
