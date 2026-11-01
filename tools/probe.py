"""Check an account against the EU Data Act portal without Home Assistant.

    pip install aiohttp
    python tools/probe.py you@example.com            # password is prompted
    python tools/probe.py you@example.com --all      # print every data point

Prints the linked vehicles, the dataset list status and the main values.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import logging
import os
import sys
from pathlib import Path

_path = Path(__file__).parents[1] / "custom_components" / "cupra_dataact" / "api.py"
_spec = importlib.util.spec_from_file_location("dataact_api", _path)
api = importlib.util.module_from_spec(_spec)
sys.modules["dataact_api"] = api
_spec.loader.exec_module(api)

FIELDS = {
    "battery level": ("506cb83e-f99f-3af3-bbeb-0429b69a78d9", "battery_level_HV.value", "state_of_charge", "hv_soc"),
    "range": (
        "cruising_range_secondary_engine",
        "cruising_range_primary_engine",
        "cruising_range_combined",
        "0ca40e18-0564-3eda-bcc0-7aee9ef44f04",
    ),
    "odometer": ("69d437d3-7baa-38bc-b842-f5baf99ddade", "mileage", "mileage.value"),
    "charge target": ("settings.target_soc",),
    "charging state": ("charging_state", "charging_state_report.current_charge_state"),
    "plug state": ("plug_state", "charging_plug1_connectionstate"),
}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    parser.add_argument("--brand", default="cupra", choices=sorted(api.BRANDS))
    parser.add_argument("--all", action="store_true", help="print all data points")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    password = os.environ.get("CUPRA_PASSWORD") or getpass.getpass()
    client = api.DataActClient(args.username, password, args.brand)
    try:
        await client.login()
        print("login ok")
        vehicles = await client.vehicles()
        if not vehicles:
            print("no vehicles linked on the portal - link the car at", api.BASE_URL)
        for v in vehicles:
            print(f"\n{v['name']} ({v['vin'][:11]}...)")
            state = api.VehicleState()
            try:
                merged = await client.update(v["vin"], state)
            except api.NotConfigured:
                print("  no data request - create a continuous (15 min) request on the portal")
                continue
            print(f"  datasets merged: {merged}, newest listed: {state.newest_listed}, points: {len(state.points)}")
            for label, ids in FIELDS.items():
                p = state.lookup(*ids)
                print(f"  {label:15} {p.value if p else '-':>12}  {p.timestamp if p else ''}")
            if args.all:
                for p in sorted(state.points.values(), key=lambda p: p.name or p.key):
                    print(f"    {p.name:60} {p.value:30} {p.timestamp}  {p.key}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
