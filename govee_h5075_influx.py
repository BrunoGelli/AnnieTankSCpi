#!/usr/bin/env python3
#DONT FORGET TO EXPORT THE INFLUX TOKEN. 
import asyncio, time, os, sys, math, random, argparse, socket
from typing import Optional, List
from bleak import BleakScanner, BleakError

HOSTNAME = socket.gethostname()
KEY_EC88 = 0xEC88  # Govee manufacturer company ID

# ---------- packed-24 decoder (your H5075 format) ----------
def decode_packed_24(payload: bytes):
    """
    payload[1:4] -> 24-bit V
      temp_c = V/10000
      rh_%   = (V % 1000)/10
    battery = payload[4] (0..100)
    Example: 00 01 3F 91 64 00 => T=8.18°C, RH=80.9%, Batt=100
    """
#    if len(payload) < 5:
#        return None
#    V = int.from_bytes(payload[1:4], "big", signed=False)
#    temp_c = V / 10000.0
#    rh = (V % 1000) / 10.0
#    batt = payload[4] if payload[4] <= 100 else None
#    return temp_c, rh, batt

    """
    Govee packed T/H (H5075):
      raw24 = payload[1:4] (24-bit)
      bit23 = sign for temperature (1 => negative)
      After clearing the sign bit:
        temp_c = raw24 / 10000.0
        rh_%   = (raw24 % 1000) / 10.0
      battery = payload[4] (0..100)
    Example: 00 01 3F 91 64 00 -> raw24=0x013F91 -> T=8.1809°C, RH=80.9%, Batt=100
    """
    if len(payload) < 5:
        return None

    raw24 = int.from_bytes(payload[1:4], "big", signed=False)

    # handle sign: top bit indicates negative temperature
    is_negative = (raw24 & 0x800000) != 0
    raw24 &= 0x7FFFFF  # clear sign bit

    # decode
    temp_c = raw24 / 10000.0
    rh = (raw24 % 1000) / 10.0
    if is_negative:
        temp_c = -temp_c

    # battery
    batt = payload[4] if payload[4] <= 100 else None
    return temp_c, rh, batt



# ---------- Influx line protocol ----------
def lp_escape(s: str) -> str:
    return s.replace("\\","\\\\").replace(" ", r"\ ").replace(",", r"\,").replace("=", r"\=")

def to_line_protocol(measurement: str, tags: dict, fields: dict, ts_ns: int) -> str:
    tpart = ",".join(f"{lp_escape(k)}={lp_escape(str(v))}" for k,v in tags.items() if v is not None)
    fparts = []
    for k,v in fields.items():
        if v is None: 
            continue
        if isinstance(v, bool):
            fparts.append(f"{k}={'true' if v else 'false'}")
        elif isinstance(v, int):
            fparts.append(f"{k}={v}i")
        elif isinstance(v, float):
            if math.isfinite(v):
                fparts.append(f"{k}={v}")
        else:
            fparts.append(f'{k}="{str(v).replace("\"","\\\"")}"')
    if not fparts:
        return ""
    tagsect = f",{tpart}" if tpart else ""
    return f"{lp_escape(measurement)}{tagsect} {','.join(fparts)} {ts_ns}"

# ---------- Influx v2 write with retries ----------
def write_v2(lp_lines: List[str], url: str, bucket: str, org: str, token: str,
             timeout: float = 5.0, max_retries: int = 5):
    import requests
    payload = "\n".join(lp_lines)
    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"{url.rstrip('/')}/api/v2/write",
                params={"org": org, "bucket": bucket, "precision": "ns"},
                headers={"Authorization": f"Token {token}"},
                data=payload,
                timeout=timeout,
            )
            if r.status_code == 204:
                return
            r.raise_for_status()
            return
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"[ERR] Influx v2 write failed after {max_retries} tries: {e}", file=sys.stderr)
                return
            time.sleep(min(30, (2**attempt) + random.random()))

# ---------- scan once and collect points ----------
def build_tags(name: str, mac: str, extras: List[str]):
    # Turn --tag k=v into dict, keep common tags
    extra_tags = {}
    for kv in extras:
        if "=" in kv:
            k, v = kv.split("=", 1)
            extra_tags[k.strip()] = v.strip()
    model = "H5075" if "5075" in (name or "") else None
    return {
        "sensor": name or mac,
        "mac": mac,
        "host": HOSTNAME,
        "model": model,
        **extra_tags,
    }

async def scan_once(seconds: int, measurement: str, extra_tags: List[str], print_raw: bool) -> List[str]:
    lines: List[str] = []

    def cb(dev, adv):
        name = adv.local_name or getattr(dev, "name", "") or ""
        man = adv.manufacturer_data or {}
        if KEY_EC88 not in man:
            return
        payload = bytes(man[KEY_EC88])
        decoded = decode_packed_24(payload)
        if not decoded:
            return
        t_c, rh, batt = decoded

        tags = build_tags(name, dev.address, extra_tags)
        fields = {
            "temp_c": round(t_c, 3),
            "humidity_pct": round(rh, 3),
            "battery_pct": batt if batt is not None else None,
            "rssi_dbm": int(adv.rssi) if adv.rssi is not None else None,
        }
        if print_raw:
            fields["payload_hex"] = payload.hex().upper()

        ts_ns = int(time.time_ns())
        lp = to_line_protocol(measurement, tags, fields, ts_ns)
        if lp:
            lines.append(lp)

    scanner = BleakScanner(cb, timeout=seconds)
    await scanner.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await scanner.stop()

    return lines

# ---------- main ----------
async def main():
    ap = argparse.ArgumentParser(description="Govee H5075 (packed-24) -> InfluxDB v2")
    ap.add_argument("--seconds", type=int, default=8, help="scan duration per cycle")
    ap.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--interval", type=int, default=10, help="sleep seconds between cycles")
    ap.add_argument("--measurement", default="govee_h5075", help="Influx measurement name")
    ap.add_argument("--tag", action="append", default=[], help="extra tag k=v (can repeat)")
    ap.add_argument("--print-raw", action="store_true", help="include payload_hex field")

    # Influx v2 connection
    ap.add_argument("--url", default="http://127.0.0.1:8086", help="InfluxDB v2 URL")
    ap.add_argument("--bucket", default="sensors", help="InfluxDB v2 bucket")
    ap.add_argument("--org", default="annie", help="InfluxDB v2 org")
    ap.add_argument("--token", default=None, help="InfluxDB v2 token (or env INFLUX_TOKEN)")
    args = ap.parse_args()

    token = args.token or os.getenv("INFLUX_TOKEN", "")
    if not token:
        print("[ERR] Missing Influx token. Pass --token or set INFLUX_TOKEN.", file=sys.stderr)
        sys.exit(2)

    # quick adapter sanity
    try:
        prelim = await BleakScanner.discover(timeout=3.0)
        if not prelim:
            print("[WARN] No BLE adverts seen in a pre-scan. If nothing comes in, check bluetoothd/rfkill.")
    except BleakError as e:
        print(f"[ERR] Bluetooth error: {e}", file=sys.stderr)

    if args.loop:
        while True:
            lines = await scan_once(args.seconds, args.measurement, args.tag, args.print_raw)
            if lines:
                write_v2(lines, args.url, args.bucket, args.org, token)
            await asyncio.sleep(args.interval)
    else:
        lines = await scan_once(args.seconds, args.measurement, args.tag, args.print_raw)
        if lines:
            write_v2(lines, args.url, args.bucket, args.org, token)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
