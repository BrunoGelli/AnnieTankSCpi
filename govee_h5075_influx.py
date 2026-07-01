#!/usr/bin/env python3
import argparse
import asyncio
import socket
import sys
import time
from typing import List, Optional

from bleak import BleakError, BleakScanner

HOSTNAME = socket.gethostname()
KEY_EC88 = 0xEC88  # Govee manufacturer company ID


def decode_packed_24(payload: bytes):
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


def to_influx_point(measurement: str, tags: dict, fields: dict, ts_ns: int) -> Optional[dict]:
    clean_fields = {k: v for k, v in fields.items() if v is not None}
    if not clean_fields:
        return None
    return {
        "measurement": measurement,
        "tags": {k: str(v) for k, v in tags.items() if v is not None},
        "time": ts_ns,
        "fields": clean_fields,
    }


def write_points(
    points: List[dict],
    host: str,
    port: int,
    database: str,
    timeout: float = 5.0,
):
    from influxdb import InfluxDBClient

    client = InfluxDBClient(host=host, port=port, database=database, timeout=timeout)
    try:
        client.switch_database(database)
        client.write_points(points, time_precision="n")
    except Exception as e:
        print(f"[ERR] InfluxDB v1 write failed: {e}", file=sys.stderr)
    finally:
        client.close()


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


async def scan_once(seconds: int, measurement: str, extra_tags: List[str], print_raw: bool) -> List[dict]:
    points: List[dict] = []

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
        point = to_influx_point(measurement, tags, fields, ts_ns)
        if point:
            points.append(point)

    scanner = BleakScanner(cb, timeout=seconds)
    await scanner.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await scanner.stop()

    return points


# ---------- main ----------
async def main():
    ap = argparse.ArgumentParser(description="Govee H5075 (packed-24) -> InfluxDB v1")
    ap.add_argument("--seconds", type=int, default=8, help="scan duration per cycle")
    ap.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--interval", type=int, default=10, help="sleep seconds between cycles")
    ap.add_argument("--measurement", default="govee_h5075", help="Influx measurement name")
    ap.add_argument("--tag", action="append", default=[], help="extra tag k=v (can repeat)")
    ap.add_argument("--print-raw", action="store_true", help="include payload_hex field")
    ap.add_argument("--host", default="192.168.197.46", help="InfluxDB host")
    ap.add_argument("--port", type=int, default=8086, help="InfluxDB port")
    ap.add_argument("--database", default="AmbientMonitoring", help="InfluxDB database")
    ap.add_argument("--timeout", type=float, default=5.0, help="InfluxDB write timeout in seconds")
    args = ap.parse_args()

    # quick adapter sanity
    try:
        prelim = await BleakScanner.discover(timeout=3.0)
        if not prelim:
            print("[WARN] No BLE adverts seen in a pre-scan. If nothing comes in, check bluetoothd/rfkill.")
    except BleakError as e:
        print(f"[ERR] Bluetooth error: {e}", file=sys.stderr)

    if args.loop:
        while True:
            points = await scan_once(args.seconds, args.measurement, args.tag, args.print_raw)
            if points:
                write_points(points, args.host, args.port, args.database, args.timeout)
            await asyncio.sleep(args.interval)
    else:
        points = await scan_once(args.seconds, args.measurement, args.tag, args.print_raw)
        if points:
            write_points(points, args.host, args.port, args.database, args.timeout)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
