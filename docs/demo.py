"""Run throttlecheck with made-up results (no real downloads): a connection where Netflix and video are capped.
Used to record the README GIF."""
import random
import sys
import time

from throttlecheck import cli, services
from throttlecheck.speed import Measurement

rnd = random.Random(5)
FAKE = {"Cloudflare (control)": (412, 0.12), "Netflix": (4.9, 0.01), "Streaming video (HLS)": (5.0, 0.012),
        "Google downloads": (388, 0.15), "Microsoft downloads": (361, 0.18), "Amazon CloudFront": (397, 0.14)}


def fake_measure(name, urls, seconds, streams):
    time.sleep(0.9)
    mbps, jitter = FAKE[name]
    return Measurement(name, mbps * rnd.uniform(0.98, 1.02), 8, mbps * 8 / 8,
                       [mbps * rnd.uniform(1 - jitter, 1 + jitter) for _ in range(12)])


cli.measure = fake_measure
real = services.default_services
cli.default_services = lambda: [s if s.name != "Netflix" else services.Service(s.name, s.kind, s.urls, lambda: "New York, US")
                                for s in real()]
sys.exit(cli.main(sys.argv[1:]))
