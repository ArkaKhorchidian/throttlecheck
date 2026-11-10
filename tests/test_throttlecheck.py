import io
import itertools
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from rich.console import Console

from throttlecheck import cli, services, speed
from throttlecheck.services import CONTROL, Service
from throttlecheck.speed import Measurement

BLOCK = b"\0" * 65536


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/missing"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(BLOCK) * 2000))
        self.end_headers()
        limited = self.path.startswith("/slow")  # 1 Mbit/s per connection, like a rate limiter
        try:
            for _ in range(2000):
                if limited:
                    chunk = BLOCK[:12500]
                    self.wfile.write(chunk)
                    time.sleep(0.1)
                else:
                    self.wfile.write(BLOCK)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    s.daemon_threads = True
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def local_open(url, timeout=10):
    return urllib.request.urlopen(url, timeout=timeout)


def test_measure_fast_and_limited(server):
    fast = speed.measure("fast", lambda: itertools.repeat(server + "/fast"), seconds=2.5, streams=2, warmup=0.5,
                         opener=local_open)
    slow = speed.measure("slow", lambda: itertools.repeat(server + "/slow"), seconds=4, streams=2, warmup=1,
                         opener=local_open)
    assert fast.mbps > 50 and not fast.error
    assert 1.2 < slow.mbps < 3.0  # 2 streams × 1 Mbit/s
    assert slow.steadiness < 0.4
    v = cli.judge(fast.mbps, slow)
    assert v.label == cli.THROTTLED


def test_measure_errors(server):
    m = speed.measure("gone", lambda: itertools.repeat(server + "/missing"), seconds=1.5, streams=2,
                      opener=local_open)
    assert m.mbps == 0 and "Not Found" in m.error
    assert speed.measure("empty", lambda: iter([]), seconds=1, opener=local_open).error == "no data"


def test_judge():
    def m(mbps, samples=None):
        return Measurement("x", mbps, samples=samples or [mbps * f for f in (0.7, 1.3, 0.9, 1.2, 0.8, 1.1)])
    assert cli.judge(400, m(380)).label == cli.NORMAL
    assert cli.judge(400, m(150)).label == cli.SLOWER
    assert cli.judge(400, m(40)).label == cli.THROTTLED
    flat = cli.judge(400, m(4.8, [4.8, 4.81, 4.79, 4.8, 4.8, 4.82, 4.8]))
    assert flat.label == cli.THROTTLED and "flat" in flat.why
    assert cli.judge(400, Measurement("x", error="timed out")).label == cli.FAILED


def test_fast_com_parsing():
    assert services.fast_script('<script src="/app-1a2b3c.js"></script>') == "/app-1a2b3c.js"
    assert services.fast_token('...,token:"YXNkZmFzZGxmbnNkYWZoYXNk",urlCount:5') == "YXNkZmFzZGxmbnNkYWZoYXNk"
    pages = {"https://fast.com/": '<script src="/app-9.js">',
             "https://fast.com/app-9.js": 'x={token:"abc"}',
             "https://api.fast.com/netflix/speedtest/v2?https=true&token=abc&urlCount=5":
                 json.dumps({"targets": [{"url": "https://ipv4-c1.oca.nflxvideo.net/speedtest?x=1",
                                          "location": {"city": "New York", "country": "US"}}]})}
    nf = services.Netflix(opener=lambda url, timeout=10: io.BytesIO(pages[url].encode()))
    assert next(nf.urls()).startswith("https://ipv4-c1.oca") and nf.where() == "New York, US"


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
v2/prog_index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080
v9/prog_index.m3u8
"""
MEDIA = """#EXTM3U
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.0,
fileSequence0.mp4
#EXTINF:6.0,
fileSequence1.mp4
#EXT-X-ENDLIST
"""


def test_hls():
    base = "https://cdn.example/stream/master.m3u8"
    assert services.parse_master(MASTER, base) == "https://cdn.example/stream/v9/prog_index.m3u8"
    assert services.parse_media(MEDIA, "https://cdn.example/stream/v9/prog_index.m3u8") == [
        "https://cdn.example/stream/v9/init.mp4", "https://cdn.example/stream/v9/fileSequence0.mp4",
        "https://cdn.example/stream/v9/fileSequence1.mp4"]
    pages = {base: MASTER, "https://cdn.example/stream/v9/prog_index.m3u8": MEDIA}
    urls = services.hls(base, opener=lambda u, timeout=10: io.BytesIO(pages[u].encode()))()
    assert [next(urls) for _ in range(4)][-1].endswith("init.mp4")  # cycles


def test_lazy_swallows_errors():
    def boom():
        raise RuntimeError("no")
    assert list(services.lazy(boom)()) == []


def test_run_render_and_share(server, monkeypatch):
    monkeypatch.setattr(cli, "measure", lambda name, urls, s, n: speed.measure(name, urls, 2.5, 2, 0.5, local_open))
    svcs = [Service(CONTROL, "control", lambda: itertools.repeat(server + "/fast")),
            Service("Netflix", "video", lambda: itertools.repeat(server + "/slow"), lambda: "Test City"),
            Service("Downloads", "download", lambda: itertools.repeat(server + "/fast")),
            Service("Broken", "download", lambda: itertools.repeat(server + "/missing"))]
    control, verdicts = cli.run(svcs, 2.5, 2)
    labels = {v.m.service: v.label for v in verdicts}
    assert control > 50 and labels["Netflix"] == cli.THROTTLED and labels["Broken"] == cli.FAILED
    assert labels["Downloads"] in (cli.NORMAL, cli.SLOWER)
    c = Console(width=110, record=True, file=io.StringIO())
    cli.render(control, verdicts, c)
    out = c.export_text()
    assert "look" in out and "throttled" in out and "Test City" in out and "uvx throttlecheck" in out
    assert "Netflix gets" in cli.share_line(control, verdicts)


def test_share_line_clean():
    v = [cli.Verdict(Measurement(CONTROL, 300), 1, cli.NORMAL, ""), cli.Verdict(Measurement("Netflix", 290), .97,
                                                                                cli.NORMAL, "")]
    assert cli.share_line(300, v).startswith("No throttling on my connection (300 Mbps")
