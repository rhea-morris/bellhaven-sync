"""Scrape every Bellhaven community from the public website.

Discovery is deliberately belt-and-braces: the paginated directory is the main
source, but we also collect any /communities/<slug> link found on the home and
About pages. (At the time of writing the newest community, Findlay, is linked
from the home page but missing from the directory: 34 listed vs "35
communities" in the hero copy.)
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import config

SLUG_RE = re.compile(r"^/communities/[a-z0-9-]+/?$")
CITY_LINE_RE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$")


@dataclass
class Location:
    slug: str
    url: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list[str] = field(default_factory=list)
    administrator: str = ""
    phone: str = ""
    found_via: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Scraper:
    def __init__(self, base: str | None = None, session: requests.Session | None = None):
        self.base = (base or config.SITE_BASE).rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.setdefault("User-Agent", "bellhaven-sync/1.0 (CRM hygiene bot)")

    def _get(self, path: str) -> BeautifulSoup:
        r = self.s.get(urljoin(self.base + "/", path.lstrip("/")), timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")

    @staticmethod
    def _slugs(soup: BeautifulSoup) -> list[str]:
        out = []
        for a in soup.find_all("a", href=True):
            href = a["href"].split("?")[0].split("#")[0]
            if href.startswith("http"):
                href = "/" + href.split("/", 3)[-1]
            if SLUG_RE.match(href):
                out.append(href.rstrip("/"))
        return out

    def discover(self) -> dict[str, list[str]]:
        """Return {slug_path: [where it was found]}."""
        found: dict[str, list[str]] = {}
        page = 1
        while page < 50:  # hard stop against a runaway pager
            soup = self._get(f"/communities?page={page}")
            slugs = self._slugs(soup)
            if not slugs:
                break
            for s in slugs:
                found.setdefault(s, []).append(f"directory p{page}")
            nxt = [a for a in soup.find_all("a", href=True) if f"page={page + 1}" in a["href"]]
            if not nxt:
                break
            page += 1
        for extra in ("/", "/about"):
            try:
                for s in self._slugs(self._get(extra)):
                    found.setdefault(s, []).append(f"linked from {extra}")
            except requests.HTTPError:
                pass
        return found

    def parse_detail(self, path: str) -> Location:
        soup = self._get(path)
        name = soup.find("h1").get_text(strip=True)
        fields: dict[str, object] = {}
        for dt in soup.select("dl.detail dt"):
            dd = dt.find_next_sibling("dd")
            key = dt.get_text(strip=True).lower()
            if key == "address":
                lines = [t.strip() for t in dd.stripped_strings]
                fields["address_lines"] = lines
            elif key.startswith("care"):
                badges = [b.get_text(strip=True) for b in dd.select(".badge")]
                fields["care"] = badges or [dd.get_text(strip=True)]
            else:
                fields[key] = dd.get_text(" ", strip=True)
        lines = fields.get("address_lines", [])
        street, city, state, zip_ = (lines[0] if lines else ""), "", "", ""
        if len(lines) >= 2:
            m = CITY_LINE_RE.match(lines[-1])
            if m:
                city, state, zip_ = m["city"], m["state"], m["zip"]
            street = " ".join(lines[:-1])
        return Location(
            slug=path.rsplit("/", 1)[-1], url=self.base + path, name=name,
            street=street, city=city, state=state, zip=zip_,
            care_offerings=list(fields.get("care", [])),
            administrator=str(fields.get("administrator", "")),
            phone=str(fields.get("phone", "")),
        )

    def scrape(self) -> list[Location]:
        locs = []
        for path, via in sorted(self.discover().items()):
            loc = self.parse_detail(path)
            loc.found_via = via
            locs.append(loc)
        return locs
