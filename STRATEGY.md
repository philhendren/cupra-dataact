# Bringing a CUPRA car back into Home Assistant: findings and strategy

_Researched 1 November 2026._

## 1. What broke, and why it can't be patched

`cupra_we_connect` (archived 13 July 2026) and every other open-source CUPRA/SEAT client called the
same backend the My CUPRA app uses: **OLA** (`ola.prod.code.seat.cloud.vwgroup.io`). VW closed it to
third parties in steps:

| Date (2026) | Change | Effect |
|---|---|---|
| ~20 May | OLA requires app-identifying headers (`app-brand`, `app-version`, …) | 403s everywhere; header patches (CarConnectivity PR #113) bought a few weeks |
| 8 June | OLA requires **device attestation** (Firebase App Check / Play Integrity token plus an AWS WAF token) | `403 Forbidden device detected / missing-device-token`. Only the genuine app on a genuine phone can mint the token |
| June to Oct | OAuth client for the app flow rejects third parties (`invalid_client` at the token exchange) | Even a hand-copied `appcheck_token` no longer helps (pycupra PR #37/#110) |
| 26 Oct | CUPRA/VW identity moves to **Auth0 Universal Login** | Any remaining scrapers using the old `emailPasswordForm` flow break |

People have probed for a bearer-only route: legacy Car-Net MBB device grant, the modern CARIAD BFF,
and header spoofing. Every one ended in 403/404/`invalid_scope`, according to the vwgroup-connect-ha
#464 analysis (Aug 2026). **Do not build on OLA again.** Getting past Play Integrity means forging
device attestation. That breaks the terms of service, can get the account locked, and VW can rotate it
at will.

**Commands (climate, charging start/stop, lock) are gone for third parties.** No open route is known.

## 2. What still works

| Path | Data | Latency | Control | Stability | Verdict |
|---|---|---|---|---|---|
| **EU Data Act portal** (`eu-data-act.drivesomethinggreater.com`) | SoC, range, odometer, charge state/plug/target, ~100+ fields once flowing | 15 min at best; often hours; sometimes empty "no_content" files | none | Legally mandated (EU Data Act, applies since Sep 2025). Buggy, but VW can't simply shut it off | **Primary cloud source** |
| volkswagen.de web login (vwgroup-connect-ha #632) | VW brand only; CUPRA not confirmed | near-live | none | Beta, needs an email OTP to re-login | Not usable for CUPRA |
| Local OBD dongle (WiCAN / ESP32 + MQTT, MEB UDS PIDs) | SoC, 12V, temps, odometer at home | seconds, only while the car is awake | none | Fully under your control | **Live overlay at home** |
| Wallbox / energy provider (evcc, Octopus "Intelligent", OpenWB) | charge power, session energy, sometimes SoC | live | charge start/stop and current **at the charger** | Good | **Replaces charge control** |
| Official app | everything | live | everything | n/a | Keep for climate and lock |

## 3. Strategy

1. **Cloud read path: EU Data Act portal.** A new integration, `cupra_dataact` (this repo). It logs in
   like a browser (OIDC code flow to the portal's own redirect URI, so no attestation is involved), lists
   the datasets, downloads only new ZIPs, merges the points by GUID key and delivery order, and keeps
   the merged state on disk. Every sensor carries `measured_at`, so automations can tell stale from
   fresh.
2. **Live overlay: local sources.** A "Battery level (best)" sensor blends in any local SoC entity
   (OBD dongle, wallbox) and picks whichever reading is newer. This covers the "is it charged yet?"
   case that the portal lag ruins.
3. **Control: move it to the charger.** Do smart charging with evcc or the wallbox integration. Use the
   car's own departure timers and the official app for preconditioning. Don't promise remote climate in
   HA.
4. **Keep the protocol in one place.** `api.py` has no Home Assistant dependency and mirrors evcc's
   `vehicle/vw/eudataact` (the most actively maintained reference: 30 fixes since June). Track its
   commits and port their fixes; `tools/probe.py` verifies an account in one command.
5. **Pressure through the regulators.** Data Act complaints have already been filed with the
   Bundesnetzagentur and data protection authorities, and the Commission is "monitoring" (E-002465/2026).
   Real-time access under RED III battery rules is coming. File your own complaint if the portal stays
   empty, because that is the lever that produces a real API.

### Existing alternatives (for honesty)

- **TommiG1/HA_VAG-EU-Data-Act** and **its-me-prash/vwgroup-connect-ha** already read the same portal.
  They are larger, multi-brand, and in beta. The point of `cupra_dataact` is a small, tested,
  CUPRA-first codebase with on-disk merge state, a freshness/portal-health split and the local SoC
  blend. If you'd rather not maintain code, use one of them, or evcc directly for charging.

## 4. Risks and open questions

- **The portal is unreliable.** Many users get `_no_content_found.zip` for days. VW acknowledged
  "empty files" in October, and a 406 login rejection hit VW/CUPRA in late October. The
  `Last portal delivery` sensor (portal alive) versus `Last measurement` (the car sent data) makes this
  visible.
- **One data request per vehicle.** Creating a one-time export can block the continuous request for
  about 24 h.
- **Login changes.** VW moved to Auth0 on 26 Oct and may add bot protection (captcha) to the web login
  too. That would also break evcc and ioBroker, so fixes will appear there quickly.
- **Field names differ by model.** The Born (MEB) uses dotted names and GUID keys
  (`battery_state_report.soc`). Download diagnostics to map new fields.
- **Not yet verified against a live CUPRA account.** The login page shape was checked live on
  1 Nov 2026 (Auth0 `/u/login` with `state`, `username`, `password`). The full login and data path
  is tested only against mocks.

## Sources

- Archived integration: https://github.com/daernsinstantfortress/cupra_we_connect
- pycupra block notice (appcheck_token): https://github.com/WulfgarW/homeassistant-pycupra and https://github.com/WulfgarW/pycupra/pull/37
- Attestation analysis and the Data Act fallback: https://github.com/its-me-prash/vwgroup-connect-ha/issues/464, /issues/456, /issues/632
- CarConnectivity 403s: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/issues/123
- evcc reference implementation: https://github.com/evcc-io/evcc/tree/master/vehicle/vw/eudataact (Auth0 change: PR #35385)
- evcc CUPRA setup: https://docs.evcc.io/en/vehicles/cupra-eu-data-act
- Portal: https://eu-data-act.drivesomethinggreater.com · CUPRA Data Act page: https://www.cupra.com/en/eu-data-act
- Community issue tracking (campaign site, treat as unverified): https://vw-data-act-watch.surge.sh/
- HA forum Data Act beta thread: https://community.home-assistant.io/t/beta-vw-group-eu-data-act-vehicle-data-for-vw-audi-skoda-seat-cupra-bentley-official-portal/1013514
- WiCAN EV SoC example: https://github.com/meatpiHQ/wican-fw/wiki/EV-Battery-SoC-in-Home-Assistant-%E2%80%90-Example
