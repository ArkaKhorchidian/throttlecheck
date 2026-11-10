"""Where to download from: a neutral control and the servers of services ISPs are known to slow down."""
from __future__ import annotations

import itertools
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Iterator

from .speed import open_url

CONTROL = "Cloudflare (control)"


@dataclass
class Service:
    name: str
    kind: str  # "control", "video", "download"
    urls: Callable[[], Iterator[str]]
    where: Callable[[], str] = lambda: ""


def read(url: str, limit: int = 2_000_000, opener=open_url) -> str:
    with opener(url) as r:
        return r.read(limit).decode("utf-8", "replace")


def repeat(url: str) -> Callable[[], Iterator[str]]:
    return lambda: itertools.repeat(url)


# --- Netflix: fast.com hands out its speed-test URLs on Netflix's own video servers (Open Connect) ---
def fast_token(app_js: str) -> str:
    m = re.search(r'token\s*:\s*"([A-Za-z0-9]+)"', app_js)
    return m.group(1) if m else ""


def fast_script(html: str) -> str:
    m = re.search(r'src="(/app-[\w.-]+\.js)"', html)
    return m.group(1) if m else ""


class Netflix:
    def __init__(self, opener=open_url):
        self.opener = opener
        self.targets: list[dict] = []

    def resolve(self) -> list[dict]:
        if not self.targets:
            script = fast_script(read("https://fast.com/", opener=self.opener))
            token = fast_token(read("https://fast.com" + script, opener=self.opener)) if script else ""
            if not token:
                raise RuntimeError("couldn't get a fast.com token")
            data = json.loads(read(f"https://api.fast.com/netflix/speedtest/v2?https=true&token={token}&urlCount=5",
                                   opener=self.opener))
            self.targets = data.get("targets") or []
            if not self.targets:
                raise RuntimeError("fast.com returned no servers")
        return self.targets

    def urls(self) -> Iterator[str]:
        return itertools.cycle(t["url"] for t in self.resolve())

    def where(self) -> str:
        try:
            loc = self.resolve()[0].get("location") or {}
        except Exception:
            return ""
        return ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)


# --- HLS video: the segments of a public test stream, highest quality, played back as fast as possible ---
def parse_master(text: str, base: str) -> str:
    """URL of the highest-bandwidth variant in a master playlist (or base itself if it's already a media playlist)."""
    best, bw = "", -1
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            nxt = next((x.strip() for x in lines[i + 1:] if x.strip() and not x.startswith("#")), "")
            if m and nxt and int(m.group(1)) > bw:
                best, bw = urllib.parse.urljoin(base, nxt), int(m.group(1))
    return best or base


def parse_media(text: str, base: str) -> list[str]:
    segs = []
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', line)
        if m:
            segs.append(urllib.parse.urljoin(base, m.group(1)))
        elif line and not line.startswith("#"):
            segs.append(urllib.parse.urljoin(base, line))
    return segs


def hls(master: str, opener=open_url) -> Callable[[], Iterator[str]]:
    def urls() -> Iterator[str]:
        media = parse_master(read(master, opener=opener), master)
        segs = parse_media(read(media, opener=opener), media) if media != master else []
        if not segs:
            segs = parse_media(read(master, opener=opener), master)
        if not segs:
            raise RuntimeError("empty playlist")
        return itertools.cycle(segs)
    return urls


def lazy(make: Callable[[], Iterator[str]]) -> Callable[[], Iterator[str]]:
    """Resolve the URL list in the first worker that asks; an error becomes an empty iterator (reported as no data)."""
    def urls() -> Iterator[str]:
        try:
            yield from make()
        except Exception:
            return
    return urls


def default_services(opener=open_url) -> list[Service]:
    nf = Netflix(opener)
    return [
        Service(CONTROL, "control", repeat("https://speed.cloudflare.com/__down?bytes=250000000")),
        Service("Netflix", "video", lazy(nf.urls), nf.where),
        Service("Streaming video (HLS)", "video", lazy(hls(
            "https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_ts/master.m3u8",
            opener))),
        Service("Google downloads", "download",
                repeat("https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb")),
        Service("Microsoft downloads", "download",
                repeat("https://update.code.visualstudio.com/latest/linux-x64/stable")),
        Service("Amazon CloudFront", "download", repeat("https://awscli.amazonaws.com/AWSCLIV2.pkg")),
    ]
