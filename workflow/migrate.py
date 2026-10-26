"""Explicit MariaDB schema bootstrap for a one-shot deployment unit.

Run this command with temporary DDL credentials, then run the broker with a
different least-privilege account.  Schema mutation during ordinary service
startup would expand the impact of a broker compromise and make rolling
deployment behavior unpredictable.
"""

from __future__ import annotations

from .config import BrokerConfig
from .store import MariaDBStore


def main() -> None:
    config = BrokerConfig.from_env()
    if not config.database_url:
        raise SystemExit("BROKER_DATABASE_URL is required for MariaDB migration")
    MariaDBStore(
        config.database_url,
        config.database_user,
        config.database_password_file,
        ca_file=config.database_ca_file,
        client_cert=config.database_client_cert,
        client_key=config.database_client_key,
        connect_timeout=config.database_connect_timeout,
        initialize_schema=True,
    )
    print("workflow database schema version 1 is ready")


if __name__ == "__main__":
    main()
