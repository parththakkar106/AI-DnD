"""Downloads the DB-IP Lite country file, which is licensed CC BY 4.0.

The Docker build runs this, and `analytics.country_of` reads the result.

    python tools/fetch_geoip.py /path/to/country.mmdb

DB-IP publishes one file a month. This month's file may not exist yet on the
1st, so a missing file falls back to last month's.
"""
import datetime
import gzip
import sys
import urllib.error
import urllib.request

URL = "https://download.db-ip.com/free/dbip-country-lite-{:%Y-%m}.mmdb.gz"


def fetch(month: datetime.date) -> bytes:
    # DB-IP refuses the default `Python-urllib` user agent with a 403.
    request = urllib.request.Request(
        URL.format(month), headers={"User-Agent": "ai-dnd-image-build"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return gzip.decompress(response.read())


def main(target: str) -> None:
    today = datetime.date.today()
    try:
        data = fetch(today)
    except urllib.error.HTTPError:
        data = fetch(today.replace(day=1) - datetime.timedelta(days=1))
    with open(target, "wb") as out:
        out.write(data)


if __name__ == "__main__":
    main(sys.argv[1])
