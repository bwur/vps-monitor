# vps-monitor

https://github.com/bwur/vps-monitor

## Install

```bash
git clone https://github.com/bwur/vps-monitor.git
cd vps-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Debian/Ubuntu, if `venv` fails, run `sudo apt install python3-venv`

## Config

Create `config.json` in the project folder (same directory as `monitor.py`), for example:

```bash
nano config.json
```

Use this format and adjust values:

```json
{
  "log_file": "logs/metrics.log",
  "cpu_alert_threshold_percent": 85.0,
  "alert_repeat_seconds": 120,
  "dashboard_refresh_seconds": 1.0,
  "log_interval_seconds": 60,
  "discord_webhook_url": "",
  "discord_interval_seconds": 300,
  "top_processes_count": 8
}
```

## Run

```bash
source .venv/bin/activate
python monitor.py
```

