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

Edit `config.json` (set your Discord webhook URL if you want alerts), then:

```bash
python monitor.py
```
