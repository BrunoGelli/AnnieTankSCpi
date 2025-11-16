# Temperature Monitoring – ANNIE

Lightweight, secure stack for environmental monitoring at ANNIE using **Telegraf → InfluxDB 2.x → Grafana**, plus Python scripts for **Govee BLE** and **DS18B20** sensors.

## Features

- Dockerized stack: Telegraf, InfluxDB 2.x, Grafana
- Govee BLE and DS18B20 sensor ingestion
- System metrics via Telegraf
- Reproducible Grafana dashboards

## Quick start

```bash
git clone https://github.com/<your-username>/temp-monitoring-annie.git
cd temp-monitoring-annie
cp .env.example .env  # edit credentials
docker compose up -d
```

Grafana → http://localhost:3000  
InfluxDB → http://localhost:8086

### Optional Python environment

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r scripts/python/requirements.txt
python scripts/python/govee_push.py --device AA:BB:CC:DD:EE:FF --room LabA
```

### Directory layout (recommended)

```
docker-compose.yml
.env.example
README.md
.gitignore
telegraf/
grafana/
scripts/
  python/
  bash/
cpp/
```

### Security checklist

- Keep host on a private subnet
- Expose Grafana/Influx only through hardened reverse proxy
- Use write‑only Influx tokens for agents/scripts
- Disable SSH password logins and restrict ports
- Regularly update containers:
  ```bash
  docker compose pull && docker compose up -d
  ```

---

MIT License © 2025 ANNIE Experiment
