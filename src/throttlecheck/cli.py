"""throttlecheck: is your internet provider slowing down Netflix and video, compared with everything else?"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .services import CONTROL, Service, default_services
from .speed import Measurement, measure

THROTTLED, SLOWER, NORMAL, FAILED = "Looks throttled", "Slower", "Normal", "Couldn't test"
STYLE = {THROTTLED: "bold #ff5f5f", SLOWER: "#ffaf5f", NORMAL: "#87d787", FAILED: "dim"}


@dataclass
class Verdict:
    m: Measurement
    ratio: float
    label: str
    why: str


def judge(control: float, m: Measurement) -> Verdict:
    if m.error or not m.mbps:
        return Verdict(m, 0, FAILED, m.error or "no data")
    ratio = m.mbps / control if control else 1.0
    flat = m.steadiness < 0.08 and len(m.samples) >= 6
    if ratio < 0.6 and flat:
        return Verdict(m, ratio, THROTTLED, f"held flat at {m.mbps:.1f} Mbps the whole time: a rate limit, not a "
                                             "busy network")
    if ratio < 0.25:
        return Verdict(m, ratio, THROTTLED, f"{1 / ratio:.0f}× slower than the control")
    if ratio < 0.6:
        return Verdict(m, ratio, SLOWER, "could be throttling, or just a server further away")
    return Verdict(m, ratio, NORMAL, "")


def run(services: list[Service], seconds: float, streams: int, progress=lambda s: None) -> tuple[float, list[Verdict]]:
    """Control first and last (so a slow moment doesn't skew things), the services in between."""
    ctrl = [s for s in services if s.kind == "control"]
    rest = [s for s in services if s.kind != "control"]
    results: dict[str, Measurement] = {}
    controls: list[Measurement] = []
    for s in ctrl + rest + ctrl:
        progress(f"Downloading from {s.name}…")
        m = measure(s.name, s.urls, seconds, streams)
        if s.kind == "control":
            controls.append(m)
        else:
            m.where = s.where()
            results[s.name] = m
    good = [c for c in controls if c.mbps]
    control = max((c.mbps for c in good), default=0.0)
    verdicts = [judge(control, results[s.name]) for s in rest]
    best = max(good, key=lambda c: c.mbps) if good else (controls[0] if controls else Measurement(CONTROL))
    return control, [Verdict(best, 1.0, NORMAL if control else FAILED, "")] + verdicts


def share_line(control: float, verdicts: list[Verdict]) -> str:
    bad = [v for v in verdicts[1:] if v.label == THROTTLED]
    if bad:
        worst = min(bad, key=lambda v: v.ratio)
        return (f"My internet does {control:.0f} Mbps, but {worst.m.service} gets {worst.m.mbps:.1f}. Is your ISP "
                "throttling you? uvx throttlecheck")
    return f"No throttling on my connection ({control:.0f} Mbps to everything). Check yours: uvx throttlecheck"


def bar(v: float, top: float, width: int = 24) -> str:
    return "█" * max(1, round(width * min(1.0, v / top))) if v and top else ""


def render(control: float, verdicts: list[Verdict], console: Console) -> None:
    top = max([control] + [v.m.mbps for v in verdicts])
    bad = [v for v in verdicts[1:] if v.label == THROTTLED]
    if not control:
        head = Text("Couldn't reach the control server, so there's nothing to compare against.", "bold #ff5f5f")
    elif bad:
        head = Text.assemble((f"{len(bad)} service{'s' * (len(bad) != 1)} look{'s' * (len(bad) == 1)} throttled",
                              "bold #ff5f5f"), f" on a connection that otherwise does {control:.0f} Mbps")
    else:
        head = Text.assemble(("No sign of throttling", "bold #87d787"), f": {control:.0f} Mbps, give or take, "
                                                                          "to everything")
    t = Table(box=None, header_style="dim", padding=(0, 2))
    t.add_column("Service", no_wrap=True)
    t.add_column("Mbps", justify="right")
    t.add_column("", no_wrap=True)
    t.add_column("Verdict", no_wrap=True)
    for v in verdicts:
        color = STYLE[v.label] if v.label != NORMAL else "#5fd7ff"
        name = Text(v.m.service, "bold" if v.m.service == CONTROL else "")
        if v.m.where:
            name.append(f"  {v.m.where}", "dim")
        t.add_row(name, f"{v.m.mbps:,.1f}" if v.m.mbps else "-", Text(bar(v.m.mbps, top), color.split()[-1]),
                  Text("baseline" if v.m.service == CONTROL else v.label, "dim" if v.m.service == CONTROL
                       else STYLE[v.label]))
    parts: list = [head, Text(""), t, Text("")]
    for v in verdicts[1:]:
        if v.label in (THROTTLED, SLOWER, FAILED) and v.why:
            parts.append(Text.assemble((f"{v.m.service}: ", "bold"), (v.why, "dim" if v.label == FAILED else "")))
    if control and control < 5:
        parts.append(Text("Your connection is slow right now, which makes the comparison less reliable. Try again "
                          "later or closer to the router.", "#ffaf5f"))
    parts += [Text(""), Text("Each service: 4 parallel downloads, timed after a warm-up. Run it a few times (and at "
                             "busy evening hours) before calling your ISP.", "dim")]
    console.print(Panel(Group(*parts), title="[bold]Is my internet throttled?[/]", border_style="#ffaf5f",
                        padding=(1, 2)))
    if control:
        console.print(Text("  Share: ", "dim") + Text(share_line(control, verdicts), "italic"))
    console.print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="throttlecheck", description="Check whether your internet provider slows down "
                                 "Netflix, streaming video or big downloads compared with a neutral control.")
    ap.add_argument("--seconds", type=float, default=8, help="how long to download from each service (default 8)")
    ap.add_argument("--streams", type=int, default=4, help="parallel connections per service (default 4)")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="only these services (e.g. netflix apple)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-V", "--version", action="version", version=f"throttlecheck {__version__}")
    a = ap.parse_args(argv)
    console = Console(stderr=a.json, highlight=False)
    services = default_services()
    if a.only:
        want = [w.lower() for w in a.only]
        services = [s for s in services if s.kind == "control" or any(w in s.name.lower() for w in want)]
    with console.status("Starting…") as status:
        control, verdicts = run(services, a.seconds, a.streams, progress=status.update)
    if a.json:
        print(json.dumps({"control_mbps": round(control, 2), "services": [
            {"service": v.m.service, "mbps": round(v.m.mbps, 2), "ratio": round(v.ratio, 3), "verdict": v.label,
             "why": v.why, "steadiness": round(v.m.steadiness, 3), "megabytes": round(v.m.megabytes, 1),
             "server": v.m.where, "error": v.m.error} for v in verdicts]}, indent=2))
        return 0
    render(control, verdicts, console)
    return 0 if control else 2


if __name__ == "__main__":
    sys.exit(main())
