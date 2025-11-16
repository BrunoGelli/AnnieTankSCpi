#!/usr/bin/env python3
import argparse, glob, os, socket, time, json, sys
from typing import Dict, List, Tuple, Optional

# InfluxDB v2 client
try:
    from influxdb_client import InfluxDBClient, Point, WriteOptions
    from influxdb_client.client.write_api import SYNCHRONOUS
except ImportError:
    print("Missing dependency: pip install influxdb-client", file=sys.stderr)
    sys.exit(1)

SYSFS_BASE = "/sys/bus/w1/devices"

def read_temp(sensor_path: str) -> Optional[float]:
    """Read a DS18B20 sensor via w1_therm sysfs, return temperature in °C or None if invalid."""
    slave = os.path.join(sensor_path, "w1_slave")
    try:
        with open(slave, "r") as f:
            lines = f.read().strip().splitlines()
        if not lines or "YES" not in lines[0]:
            return None
        tpos = lines[1].rfind("t=")
        if tpos == -1:
            return None
        mc = int(lines[1][tpos+2:])  # milli-deg C
        return mc / 1000.0
    except Exception:
        return None

def load_map(path: Optional[str]) -> Dict[str, str]:
    """Load optional mapping of sensor_id -> location from JSON or TSV."""
    if not path:
        return {}
    if not os.path.exists(path):
        print(f"[warn] mapping file not found: {path}", file=sys.stderr)
        return {}
    try:
        if path.endswith(".json"):
            with open(path, "r") as f:
                return json.load(f)
        # TSV/space-separated: "<sensor_id>  <location>"
        m = {}
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                parts = line.split()
                if len(parts) >= 2:
                    m[parts[0]] = parts[1]
        return m
    except Exception as e:
        print(f"[warn] failed to load map: {e}", file=sys.stderr)
        return {}

def parse_tags(tag_list: List[str]) -> Dict[str, str]:
    out = {}
    for t in tag_list:
        if "=" in t:
            k, v = t.split("=", 1)
            # sanitize commas/spaces per line protocol rules
            out[k.strip()] = v.strip()
    return out

def collect() -> List[Tuple[str, float]]:
    """Return list of (sensor_id, temp_c) for all 28-* devices."""
    out = []
    for d in glob.glob(os.path.join(SYSFS_BASE, "28-*")):
        if not os.path.isdir(d): 
            continue
        sid = os.path.basename(d)
        temp = read_temp(d)
        if temp is not None:
            out.append((sid, temp))
    return out

def main():
    p = argparse.ArgumentParser(description="Read DS18B20 temps and write to InfluxDB v2.")
    p.add_argument("--influx-url", default=os.environ.get("INFLUX_URL", "http://localhost:8086"))
    p.add_argument("--influx-token", default=os.environ.get("INFLUX_TOKEN"))
    p.add_argument("--influx-org", default=os.environ.get("INFLUX_ORG", "default"))
    p.add_argument("--influx-bucket", default=os.environ.get("INFLUX_BUCKET", "sensors"))
    p.add_argument("--measurement", default=os.environ.get("MEASUREMENT", "ds18b20"))
    p.add_argument("--map-file", help="Optional JSON or TSV mapping of sensor_id -> location")
    p.add_argument("--tag", action="append", default=[], help="Extra tag key=value (repeatable)")
    p.add_argument("--host-tag", default=socket.gethostname(), help="Value for tag 'host'")
    p.add_argument("--interval", type=float, default=0.0, help="Seconds between writes (0 = run once)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if not args.influx_token:
        print("ERROR: --influx-token (or INFLUX_TOKEN) is required", file=sys.stderr)
        sys.exit(2)

    extra_tags = parse_tags(args.tag)
    id2loc = load_map(args.map_file)

    client = InfluxDBClient(url=args.influx_url, token=args.influx_token, org=args.influx_org)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    def do_write():
        rows = collect()
        if args.verbose:
            print(f"[info] found {len(rows)} sensors")

        points = []
        for sid, temp_c in rows:
            pt = Point(args.measurement).tag("host", args.host_tag).tag("sensor", sid)
            if sid in id2loc:
                pt = pt.tag("location", id2loc[sid])
            for k, v in extra_tags.items():
                pt = pt.tag(k, v)
            pt = pt.field("temp_c", float(temp_c))
            points.append(pt)
            if args.verbose:
                print(f"[debug] {sid} -> {temp_c:.3f} °C")

        if points:
            try:
                write_api.write(bucket=args.influx_bucket, record=points)
                if args.verbose:
                    print(f"[info] wrote {len(points)} points to {args.influx_bucket}")
            except Exception as e:
                print(f"[error] write failed: {e}", file=sys.stderr)

    if args.interval > 0:
        try:
            while True:
                t0 = time.time()
                do_write()
                dt = time.time() - t0
                sleep_s = max(args.interval - dt, 0.0)
                time.sleep(sleep_s)
        except KeyboardInterrupt:
            pass
    else:
        do_write()

    client.close()

if __name__ == "__main__":
    main()
