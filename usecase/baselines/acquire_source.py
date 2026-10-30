"""Recover and verify the exact July 2024 NALT RDF release from AgroPortal."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import shutil
from urllib.parse import urlsplit
import zipfile

import requests

from common import HERE, ROOT, file_hash, write_json

SOURCE_DIR = ROOT / "tmp" / "nalt-baseline-source"
MEMBER = "nalt-full_dwn_20240716.rdf"
RDF_SHA256 = "71a3e2e5ca84e5f680e1ebbf3fa7a5892415c58de716334b5add27b54af6d168"
ZIP_SHA256 = "0473c3e6866458e729e4719e3bacbddf3a71880d1669820cad3d7114b08abcea"
SOURCE_PAGE = "https://agroportal.eu/ontologies/NALT"


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.urls.extend(value for key, value in attrs if key == "href")


def main():
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    target = SOURCE_DIR / MEMBER
    if not target.exists():
        page = requests.get(SOURCE_PAGE + "/submissions?invalidate_cache=true", timeout=120)
        if page.status_code != 200:
            raise RuntimeError(f"AgroPortal submission listing returned HTTP {page.status_code}")
        parser = Links()
        parser.feed(page.text)
        # Use only the public download URL supplied by the portal. Do not print
        # its anonymous access key or record query credentials in artifacts.
        urls = [url for url in parser.urls
                if urlsplit(url).hostname == "data.agroportal.eu"
                and urlsplit(url).path == "/ontologies/NALT/submissions/12/download"]
        if not urls:
            raise RuntimeError("July 2024 submission download link unavailable; supply the fingerprinted RDF file")
        archive = SOURCE_DIR / "nalt-20240716.zip"
        response = requests.get(urls[0], stream=True, timeout=180)
        if response.status_code != 200:
            raise RuntimeError(f"AgroPortal archive returned HTTP {response.status_code}")
        with archive.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                handle.write(chunk)
        if file_hash(archive) != ZIP_SHA256:
            raise ValueError("Archive fingerprint differs from the verified July 2024 snapshot")
        with zipfile.ZipFile(archive) as zipped, zipped.open(MEMBER) as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
    if file_hash(target) != RDF_SHA256:
        raise ValueError("NALT RDF fingerprint mismatch")
    write_json(HERE / "data" / "source.json", {
        "source_page": SOURCE_PAGE, "submission": 12, "listed_version": "V-2024-07",
        "listed_modified": "2024-07-16", "archive_member": MEMBER,
        "archive_sha256": ZIP_SHA256, "rdf_sha256": RDF_SHA256,
        "source_path": target.relative_to(ROOT).as_posix(),
        "format": "RDF/XML", "release_bytes": target.stat().st_size,
    })
    print(f"Verified July 2024 source: {target}")


if __name__ == "__main__":
    main()
