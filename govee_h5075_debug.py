#!/usr/bin/env python3
import asyncio, time, sys

KEY_EC88 = 0xEC88  # Govee manufacturer company ID

def now(): return time.strftime("%Y-%m-%d %H:%M:%S")

def decode_packed_24(payload: bytes):
    """
    Packed T/H format used by your H5075:
      payload[1:4] -> 24-bit unsigned integer V
        temp_c = V / 10000.0
        rh_%   = (V % 1000) / 10.0
      battery = payload[4] (0..100)
    Example: 00 01 3F 91 64 00
             V = 0x013F91 = 81809 -> T=8.1809°C, RH=80.9%, Batt=100
    """
    if len(payload) < 5:
        return None
    V = int.from_bytes(payload[1:4], "big", signed=False)
    temp_c = V / 10000.0
    rh = (V % 1000) / 10.0
    batt = payload[4] if payload[4] <= 100 else None
    return temp_c, rh, batt

# -------- imports --------
try:
    from bleak import BleakScanner, BleakError
except Exception as e:
    print(f"[ERR] bleak import failed: {e}. Install with: pip3 install bleak", file=sys.stderr)
    sys.exit(1)

def handle_frame(address, name, rssi, manufacturer_data):
    if not manufacturer_data or KEY_EC88 not in manufacturer_data:
        return
    p = bytes(manufacturer_data[KEY_EC88])
    # Always show raw for auditing:
    print(f"{now()}  {name or 'Govee'}  MAC={address}  RSSI={rssi}  EC88={p.hex().upper()}")
    out = decode_packed_24(p)
    if out:
        t_c, rh, batt = out
        print(f"   [packed24] Temp={t_c:.2f} °C  RH={rh:.1f}%  Batt={batt if batt is not None else 'n/a'}")
    else:
        print("   [packed24] (payload too short)")

# ---- callback scanner (preferred on newer Bleak) ----
def detection_cb(device, advertisement_data):
    handle_frame(
        device.address,
        advertisement_data.local_name or getattr(device, "name", "") or "",
        advertisement_data.rssi,
        advertisement_data.manufacturer_data or {},
    )

async def run_callback_mode():
    print("Scanning (callback)… Ctrl+C to stop\n")
    scanner = BleakScanner(detection_callback=detection_cb)
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await scanner.stop()

# ---- polling scanner (works on all Bleak versions) ----
async def run_poll_mode():
    print("Scanning (poll)… Ctrl+C to stop\n")
    try:
        while True:
            devices = await BleakScanner.discover(timeout=5.0)
            for dev in devices:
                meta = getattr(dev, "metadata", {}) or {}
                man = meta.get("manufacturer_data", {})
                rssi = meta.get("rssi", getattr(dev, "rssi", None))
                handle_frame(dev.address, getattr(dev, "name", "") or "", rssi, man)
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass

async def main():
    # Quick sanity check
    try:
        prelim = await BleakScanner.discover(timeout=3.0)
    except BleakError as e:
        print(f"[ERR] Bluetooth error: {e}")
        prelim = []
    if not prelim:
        print("[WARN] No BLE adverts seen in a quick pre-scan. Check rfkill/bluetoothd/adapter power.")

    # Try callback mode first; fall back if constructor lacks detection_callback
    try:
        _ = BleakScanner(detection_callback=detection_cb)
    except TypeError:
        await run_poll_mode()
        return
    await run_callback_mode()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
