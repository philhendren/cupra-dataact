# CUPRA (EU Data Act) for Home Assistant

Read-only CUPRA vehicle data in Home Assistant, sourced from the VW Group **EU Data Act portal**.
It replaces the archived `cupra_we_connect`, whose app API now requires device attestation.
[STRATEGY.md](STRATEGY.md) explains the background and the options.

**What you get:** battery level, range, odometer, charge target, charging state, plugged in / charging,
remaining charge time, and the time of the last measurement and the last portal delivery. You can
optionally add a "Battery level (best)" sensor that blends in a live local SoC, for example from an
OBD dongle.

**What you don't get:** remote commands (climate, lock, charging). Live data is not available either:
the portal updates at most every 15 minutes, and often less often.

## 1. One-time setup on the portal (required)

1. Open https://eu-data-act.drivesomethinggreater.com and sign in with your CUPRA ID (My CUPRA app login).
2. Accept the consent screen and link your car. You must be its primary user.
3. Create a **continuous** data request (every 15 minutes). Don't create a one-time export first,
   because only one request can be active at a time.
4. Wait for the first dataset. This takes anywhere from 15 minutes to a day.

## 2. Install

HACS: add this repository as a custom repository (category: Integration), then install **CUPRA (EU Data Act)**.
Manual: copy `custom_components/cupra_dataact` into your HA `config/custom_components/` and restart.

Then go to Settings → Devices & services → Add integration → **CUPRA (EU Data Act)**.

Options: poll interval (default 5 min) and an optional live battery level sensor.

## Check your account without Home Assistant

```sh
pip install aiohttp
python tools/probe.py you@example.com          # add --all to dump every data point
```

## Troubleshooting

- **Repair "No data request"**: step 1.3 is missing or has expired.
- **`Last portal delivery` is recent but `Last measurement` is old**: the portal is running, but the car
  isn't sending data, or VW is delivering empty files (a known issue, Oct 2026).
- **Login stops at a consent page**: sign in on the portal in a browser once and accept.
- **Unmapped fields**: download diagnostics from the device page. They list every raw data point, with
  location values redacted.

## Development

```sh
pip install pytest-homeassistant-custom-component aioresponses
pytest
```

The portal protocol lives in `custom_components/cupra_dataact/api.py`, which has no HA imports.
It mirrors evcc's `vehicle/vw/eudataact`, so check that project when VW changes something.
