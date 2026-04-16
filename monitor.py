#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

defaultconfig = Path(__file__).resolve().parent / "config.json"


@dataclass
class settings:
    logpath: Path
    cputhreshold: float
    alertcooldown: float
    dashrefresh: float
    loginterval: float
    webhook: str
    discordinterval: float
    topcount: int

    @classmethod
    def load(cls, path: Path) -> settings:
        raw = json.loads(path.read_text(encoding="utf-8"))
        logpath = Path(raw.get("log_file", "logs/metrics.log"))
        if not logpath.is_absolute():
            logpath = path.parent / logpath
        return cls(
            logpath=logpath,
            cputhreshold=float(raw.get("cpu_alert_threshold_percent", 85)),
            alertcooldown=float(raw.get("alert_repeat_seconds", 120)),
            dashrefresh=float(raw.get("dashboard_refresh_seconds", 1.0)),
            loginterval=float(raw.get("log_interval_seconds", 60)),
            webhook=str(raw.get("discord_webhook_url", "")).strip(),
            discordinterval=float(raw.get("discord_interval_seconds", 300)),
            topcount=int(raw.get("top_processes_count", 8)),
        )


@dataclass
class shared:
    mtx: threading.Lock = field(default_factory=threading.Lock)
    lastcpu: float = 0.0
    alerting: bool = False
    lastalert: float = 0.0
    alerts: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    netprev: tuple[float, int, int] = (0.0, 0, 0)
    halt: threading.Event = field(default_factory=threading.Event)


def utcstamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def warmcpu() -> None:
    psutil.cpu_percent(interval=0.1)
    for p in psutil.process_iter():
        try:
            p.cpu_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.15)


def fmtbytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} PB"


def snapshot(topn: int) -> dict[str, Any]:
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    procs: list[dict[str, Any]] = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
            if info.get("cpu_percent") is None:
                continue
            procs.append(
                {
                    "pid": info["pid"],
                    "name": (info.get("name") or "")[:32],
                    "cpu_percent": round(float(info["cpu_percent"] or 0), 1),
                    "memory_percent": round(float(info.get("memory_percent") or 0), 1),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x["cpu_percent"], reverse=True)
    return {
        "ts": utcstamp(),
        "cpu_percent": round(cpu, 1),
        "memory": {
            "percent": round(mem.percent, 1),
            "used_bytes": mem.used,
            "total_bytes": mem.total,
        },
        "swap": {"percent": round(swap.percent, 1), "used_bytes": swap.used},
        "disk_root": {
            "percent": round(disk.percent, 1),
            "used_bytes": disk.used,
            "total_bytes": disk.total,
        },
        "network": {"bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv},
        "top_processes": procs[:topn],
    }


def appendlog(logpath: Path, row: dict[str, Any]) -> None:
    logpath.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with open(logpath, "a", encoding="utf-8") as f:
        f.write(line)


def throttlealert(cfg: settings, bag: shared, cpu: float, row: dict[str, Any]) -> None:
    now = time.monotonic()
    over = cpu >= cfg.cputhreshold
    with bag.mtx:
        bag.lastcpu = cpu
        bag.alerting = over
        if not over:
            return
        if now - bag.lastalert < cfg.alertcooldown:
            return
        bag.lastalert = now
        msg = (
            f"CPU {cpu:.1f}% >= threshold {cfg.cputhreshold:.1f}% "
            f"(ts {row['ts']})"
        )
        bag.alerts.append(msg)
    alertrow = {
        "ts": row["ts"],
        "level": "ALERT",
        "message": msg,
        "cpu_percent": cpu,
        "threshold": cfg.cputhreshold,
    }
    appendlog(cfg.logpath, alertrow)


def posthook(url: str, body: dict[str, Any]) -> None:
    if not url:
        return
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 204):
                pass
    except urllib.error.HTTPError as e:
        errbody = e.read().decode("utf-8", errors="replace")[:500]
        print(f"Discord webhook HTTP {e.code}: {errbody}")
    except urllib.error.URLError as e:
        print(f"Discord webhook error: {e}")


def hookpayload(snap: dict[str, Any], recent: list[str]) -> dict[str, Any]:
    cpu = snap["cpu_percent"]
    mem = snap["memory"]
    swap = snap["swap"]
    disk = snap["disk_root"]
    top = snap.get("top_processes") or []
    lines = [
        f"**CPU** {cpu}%",
        f"**RAM** {mem['percent']}% ({fmtbytes(float(mem['used_bytes']))} / {fmtbytes(float(mem['total_bytes']))})",
        f"**Swap** {swap['percent']}%",
        f"**Disk /** {disk['percent']}%",
        "",
        "**Top processes (CPU%)**",
    ]
    for p in top[:6]:
        lines.append(f"• `{p['pid']}` {p['name']}: {p['cpu_percent']}% CPU, {p['memory_percent']}% RAM")
    desc = "\n".join(lines)[:4000]
    embed: dict[str, Any] = {
        "title": "System monitor snapshot",
        "description": desc,
        "color": 0x5865F2,
    }
    if recent:
        embed["fields"] = [
            {
                "name": "Recent alerts",
                "value": "\n".join(f"• {a}" for a in recent[-5:])[:1000],
            }
        ]
    return {"embeds": [embed]}


def logloop(cfg: settings, bag: shared) -> None:
    while not bag.halt.wait(timeout=cfg.loginterval):
        try:
            snap = snapshot(cfg.topcount)
            appendlog(cfg.logpath, snap)
            throttlealert(cfg, bag, snap["cpu_percent"], snap)
        except Exception as e:
            appendlog(
                cfg.logpath,
                {"ts": utcstamp(), "level": "ERROR", "message": f"logloop: {e}"},
            )


def discordloop(cfg: settings, bag: shared) -> None:
    if not cfg.webhook:
        return
    tick = max(30.0, cfg.discordinterval)
    first = True
    while not bag.halt.is_set():
        if not first and bag.halt.wait(timeout=tick):
            break
        first = False
        try:
            snap = snapshot(cfg.topcount)
            with bag.mtx:
                recent = list(bag.alerts)
            posthook(cfg.webhook, hookpayload(snap, recent))
        except Exception as e:
            print(f"discordloop: {e}")


def netrate(bag: shared, snap: dict[str, Any]) -> tuple[str, str]:
    now = time.monotonic()
    n = snap["network"]
    sent, recv = int(n["bytes_sent"]), int(n["bytes_recv"])
    with bag.mtx:
        t0, s0, r0 = bag.netprev
        bag.netprev = (now, sent, recv)
    dt = now - t0
    if dt <= 0 or t0 == 0.0:
        return ("—", "—")
    up = (sent - s0) / dt
    down = (recv - r0) / dt
    return (f"{fmtbytes(up)}/s", f"{fmtbytes(down)}/s")


def dashboard(cfg: settings, bag: shared) -> Group:
    snap = snapshot(cfg.topcount)
    up, down = netrate(bag, snap)
    cpu = snap["cpu_percent"]
    mem = snap["memory"]
    title = Text()
    title.append("linux-system-monitor", style="bold cyan")
    title.append(f"  •  {snap['ts']}", style="dim")

    with bag.mtx:
        fired = bag.alerting

    headstyle = "bold red" if fired or cpu >= cfg.cputhreshold else "bold green"
    stats = Table.grid(expand=True)
    stats.add_column(ratio=1)
    stats.add_column(ratio=1)
    stats.add_row(
        Panel(
            f"[{headstyle}]{cpu}%[/]\n[dim]threshold {cfg.cputhreshold}%[/]",
            title="CPU",
            border_style="cyan",
        ),
        Panel(
            f"[bold]{mem['percent']}%[/]\n[dim]{fmtbytes(float(mem['used_bytes']))} / "
            f"{fmtbytes(float(mem['total_bytes']))}[/]",
            title="Memory",
            border_style="magenta",
        ),
    )
    swap = snap["swap"]
    disk = snap["disk_root"]
    net = snap["network"]
    stats.add_row(
        Panel(
            f"[bold]{swap['percent']}% swap[/]\n[dim]{fmtbytes(float(swap['used_bytes']))} used[/]",
            title="Swap",
            border_style="yellow",
        ),
        Panel(
            f"[bold]{disk['percent']}%[/] on /\n[dim]{fmtbytes(float(disk['used_bytes']))} / "
            f"{fmtbytes(float(disk['total_bytes']))}[/]",
            title="Disk /",
            border_style="blue",
        ),
    )
    netpanel = Panel(
        f"[bold]↑[/] {up} [bold]↓[/] {down}\n"
        f"[dim]total ↑ {fmtbytes(float(net['bytes_sent']))}  "
        f"↓ {fmtbytes(float(net['bytes_recv']))}[/]",
        title="Network",
        border_style="green",
    )

    proctable = Table(box=box.SIMPLE_HEAD, expand=True)
    proctable.add_column("PID", style="dim", no_wrap=True)
    proctable.add_column("Name")
    proctable.add_column("CPU%", justify="right")
    proctable.add_column("MEM%", justify="right")
    for p in snap["top_processes"]:
        proctable.add_row(str(p["pid"]), p["name"], f"{p['cpu_percent']}", f"{p['memory_percent']}")

    procpanel = Panel(proctable, title=f"Top {cfg.topcount} processes by CPU", border_style="white")

    footer = Text()
    footer.append(f"log → {cfg.logpath}  ", style="dim")
    if cfg.webhook:
        footer.append(f"discord every {cfg.discordinterval:.0f}s", style="dim")
    else:
        footer.append("discord disabled (empty webhook URL)", style="dim")

    return Group(title, stats, netpanel, procpanel, footer)


def main() -> None:
    cfgpath = Path(os.environ.get("MONITOR_CONFIG", defaultconfig))
    if not cfgpath.is_file():
        print(
            f"Missing config: {cfgpath}\n"
            f"Copy config.example.json to config.json and edit (especially discord_webhook_url)."
        )
        raise SystemExit(1)
    cfg = settings.load(cfgpath)
    bag = shared()
    console = Console()
    warmcpu()

    logthread = threading.Thread(target=logloop, args=(cfg, bag), daemon=True)
    discordthread = threading.Thread(target=discordloop, args=(cfg, bag), daemon=True)
    logthread.start()
    discordthread.start()

    tick = max(0.25, cfg.dashrefresh)
    try:
        with Live(
            dashboard(cfg, bag),
            console=console,
            refresh_per_second=1.0 / tick,
            screen=False,
        ) as live:
            while True:
                live.update(dashboard(cfg, bag))
                time.sleep(tick)
    except KeyboardInterrupt:
        bag.halt.set()
        console.print("[dim]Stopped.[/]")


if __name__ == "__main__":
    main()
