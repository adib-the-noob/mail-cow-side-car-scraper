"""Mailcow scraper sidecar.

Reads domain, DKIM and DNS information directly out of a running mailcow
installation (Redis for DKIM keys, MariaDB for domains/mailboxes/aliases) and
serves it over a small FastAPI app.
"""

__version__ = "1.0.0"
