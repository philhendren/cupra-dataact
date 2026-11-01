"""Offline tests for the portal client (no Home Assistant needed)."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest
from aioresponses import aioresponses

# load api.py without importing the HA package __init__
_path = Path(__file__).parents[1] / "custom_components" / "cupra_dataact" / "api.py"
_spec = importlib.util.spec_from_file_location("dataact_api", _path)
api = importlib.util.module_from_spec(_spec)
sys.modules["dataact_api"] = api
_spec.loader.exec_module(api)

VIN = "VSSZZZK1ZRP000001"
IDENT = "req-123"
SOC_KEY = "506cb83e-f99f-3af3-bbeb-0429b69a78d9"


def make_zip(points: list[dict], vin: str = VIN) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.json", json.dumps({"vin": vin, "Data": points}))
    return buf.getvalue()


def dp(name: str, value: str, ts: str, key: str = "") -> dict:
    return {"key": key, "dataFieldName": name, "value": value, "timestampUtc": ts}


def test_parse_dataset_keeps_newest_per_id():
    blob = make_zip(
        [
            dp("mileage", "100", "2026-10-01T10:00:00Z"),
            dp("mileage", "120", "2026-10-01T11:00:00Z"),
            dp("mileage", "90", "2026-10-01T09:00:00Z"),
            dp("battery_state_report.soc", "55", "2026-10-01T11:00:00Z", SOC_KEY),
            dp("battery_state_report.soc", "20", "2026-10-01T12:00:00Z", "other-key"),
            dp("empty", "", "2026-10-01T11:00:00Z"),
        ]
    )
    vin, points = api.parse_dataset(blob)
    by_id = {p.id: p for p in points}
    assert vin == VIN
    assert by_id["mileage"].value == "120"
    assert by_id[SOC_KEY].value == "55"
    assert "empty" not in by_id


def test_missing_timestamp_does_not_override():
    points = api.parse_points([dp("x", "1", "2026-10-01T10:00:00Z"), {"dataFieldName": "x", "value": "2"}])
    assert points[0].value == "1"


def test_content_datasets_skips_placeholders_and_sorts():
    items = [
        {"name": "b.zip", "createdOn": "2026-10-01T10:15:00Z"},
        {"name": "x_no_content_found.zip", "createdOn": "2026-10-01T10:30:00Z"},
        {"name": "a.zip", "createdOn": "2026-10-01T10:00:00Z"},
    ]
    assert [d.name for d in api.content_datasets(items)] == ["a.zip", "b.zip"]


def test_lookup_prefers_latest_delivery_then_priority():
    state = api.VehicleState()
    state.merge(api.parse_points([dp("state_of_charge", "40", "2026-10-01T10:00:00Z")]))
    state.merge(api.parse_points([dp("battery_level_HV.value", "60", "2026-10-01T11:00:00Z")]))
    assert state.lookup(SOC_KEY, "battery_level_HV.value", "state_of_charge").value == "60"
    state.merge(api.parse_points([dp("battery_state_report.soc", "61", "2026-10-01T11:00:00Z", SOC_KEY)]))
    assert state.lookup(SOC_KEY, "battery_level_HV.value", "state_of_charge").value == "61"


def test_state_roundtrip():
    state = api.VehicleState(identifier=IDENT)
    state.merge(api.parse_points([dp("mileage", "1", "2026-10-01T10:00:00Z")]))
    state.after = api.parse_time("2026-10-01T10:00:00Z")
    again = api.VehicleState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert again.identifier == IDENT and again.after == state.after
    assert again.find("mileage").value == "1" and again.seq == 1


def test_pending_backfill_limit():
    ds = [api.Dataset(f"{i}.zip", api.parse_time(f"2026-10-01T{i:02d}:00:00Z")) for i in range(12)]
    assert len(api.pending(ds, None)) == api.MAX_BACKFILL
    assert [d.name for d in api.pending(ds, ds[9].created_on)] == ["10.zip", "11.zip"]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, (None, None)),
        ({"plug_state": "connected"}, (True, False)),
        ({"plug_state": "disconnected"}, (False, False)),
        ({"charging_state": "charging"}, (True, True)),
        ({"charging_state_report.current_charge_state": "CHARGE_STATE_CONSERVATION_CHARGING"}, (True, True)),
        ({"charging_state_report.charging_scenario": "AC_CHARGING_FINISHED"}, (True, False)),
        ({"charging_state_report.charging_scenario": "DC_CHARGING_ACTIVE"}, (True, True)),
    ],
)
def test_charge_status(fields, expected):
    state = api.VehicleState()
    state.merge(api.parse_points([dp(k, v, "2026-10-01T10:00:00Z") for k, v in fields.items()]))
    assert (
        api.charge_status(
            state,
            ("plug_state", "charging_plug1_connectionstate"),
            ("charging_state", "charging_state_report.current_charge_state"),
            ("charging_state_report.charging_scenario",),
        )
        == expected
    )


def test_parse_idk():
    html = """<script>window._IDK = {
        templateModel: {"hmac":"h2","relayState":"r2","postAction":"login/authenticate","identifierUrl":"login/identifier","error":null},
        csrf_token: 'c2',
        currentLocale: 'en',
    };</script>"""
    idk = api.parse_idk(html)
    assert idk["csrf_token"] == "c2"
    assert idk["templateModel"]["postAction"] == "login/authenticate"


# --- mocked login and data flow ----------------------------------------------

PORTAL = api.BASE_URL
USER_PAGE = f"{PORTAL}/content/euda/ie/en/user.html"
AUTH_RE = re.compile(r"^https://identity\.vwgroup\.io/oidc/v1/authorize\?.*$")

ULP_PAGE = '<form method="POST"><input type="hidden" name="state" value="st123"><input name="username"><input name="password" type="password"></form>'
LEGACY_PAGE = """<form id="emailPasswordForm" action="/signin-service/v1/cid/login/identifier">
<input name="_csrf" value="c1"><input name="relayState" value="r1"><input name="hmac" value="h1"><input name="email"></form>"""
LEGACY_PW_PAGE = """<script>window._IDK = {templateModel: {"hmac":"h2","relayState":"r2","postAction":"login/authenticate","identifierUrl":"login/identifier","error":null}, csrf_token: 'c2', };</script>"""


def mock_data(m: aioresponses) -> None:
    m.get(f"{PORTAL}/proxy_api/consent/me/vehicles?viewPosition=FRONT_LEFT", payload=[{"vin": VIN, "nickName": "Born"}])
    m.get(f"{PORTAL}/proxy_api/euda-apim/datarequest/vehicles/{VIN}/metadata/partial", payload={"Identifier": IDENT})
    m.get(
        f"{PORTAL}/proxy_api/euda-apim/datadelivery/vehicles/{VIN}/{IDENT}/list",
        payload=[
            {"name": "1.zip", "createdOn": "2026-10-01T10:00:00Z"},
            {"name": "2_no_content_found.zip", "createdOn": "2026-10-01T10:15:00Z"},
        ],
    )
    m.get(
        f"{PORTAL}/proxy_api/euda-apim/datadelivery/vehicles/{VIN}/{IDENT}/download",
        body=make_zip([dp("battery_state_report.soc", "72", "2026-10-01T09:58:00Z", SOC_KEY)]),
    )


async def test_universal_login_and_update():
    client = api.DataActClient("me@example.com", "pw")
    with aioresponses() as m:
        m.get(AUTH_RE, status=302, headers={"Location": "https://identity.vwgroup.io/u/login?state=st123"})
        m.get("https://identity.vwgroup.io/u/login?state=st123", body=ULP_PAGE)
        m.post(
            "https://identity.vwgroup.io/u/login?state=st123",
            status=302,
            headers={"Location": f"{PORTAL}/login?code=abc"},
        )
        m.get(f"{PORTAL}/login?code=abc", status=302, headers={"Location": USER_PAGE})
        mock_data(m)

        await client.login()
        vehicles = await client.vehicles()
        assert vehicles == [{"vin": VIN, "name": "Born", "model": None}]

        state = api.VehicleState()
        assert await client.update(VIN, state) == 1
        assert state.lookup(SOC_KEY).value == "72"
        assert state.newest_listed == api.parse_time("2026-10-01T10:15:00Z")
    await client.close()


async def test_legacy_login():
    client = api.DataActClient("me@example.com", "pw")
    with aioresponses() as m:
        m.get(
            AUTH_RE,
            status=302,
            headers={"Location": "https://identity.vwgroup.io/signin-service/v1/cid/login/identifier"},
        )
        m.get("https://identity.vwgroup.io/signin-service/v1/cid/login/identifier", body=LEGACY_PAGE)
        m.post("https://identity.vwgroup.io/signin-service/v1/cid/login/identifier", body=LEGACY_PW_PAGE)
        m.post(
            "https://identity.vwgroup.io/signin-service/v1/cid/login/authenticate",
            status=302,
            headers={"Location": USER_PAGE},
        )
        await client.login()
    await client.close()


async def test_marketing_consent_is_skipped():
    client = api.DataActClient("me@example.com", "pw")
    consent = "https://identity.vwgroup.io/consent/marketing/x?callback=https%3A%2F%2Fidentity.vwgroup.io%2Foidc%2Fv1%2Foauth%2Fclient%2Fcallback%3Fa%3D1"
    with aioresponses() as m:
        m.get(AUTH_RE, body=ULP_PAGE)
        m.post("https://identity.vwgroup.io/u/login?state=st123", status=302, headers={"Location": consent})
        m.get(
            "https://identity.vwgroup.io/oidc/v1/oauth/client/callback?a=1",
            status=302,
            headers={"Location": USER_PAGE},
        )
        await client.login()
    await client.close()


async def test_wrong_password():
    client = api.DataActClient("me@example.com", "bad")
    with aioresponses() as m:
        m.get(AUTH_RE, body=ULP_PAGE)
        m.post("https://identity.vwgroup.io/u/login?state=st123", status=400, body=ULP_PAGE)
        with pytest.raises(api.AuthError):
            await client.login()
    await client.close()


async def test_terms_page_requires_consent():
    client = api.DataActClient("me@example.com", "pw")
    with aioresponses() as m:
        m.get(AUTH_RE, body=ULP_PAGE)
        m.post(
            "https://identity.vwgroup.io/u/login?state=st123",
            status=302,
            headers={"Location": "https://identity.vwgroup.io/signin-service/v1/cid/terms-and-conditions"},
        )
        m.get("https://identity.vwgroup.io/signin-service/v1/cid/terms-and-conditions", body="<html>terms</html>")
        with pytest.raises(api.ConsentRequired):
            await client.login()
    await client.close()


async def test_relogin_on_403_and_not_configured():
    client = api.DataActClient("me@example.com", "pw")
    with aioresponses() as m:
        m.get(f"{PORTAL}/proxy_api/euda-apim/datarequest/vehicles/{VIN}/metadata/partial", status=403)
        m.get(AUTH_RE, body=ULP_PAGE)
        m.post("https://identity.vwgroup.io/u/login?state=st123", status=302, headers={"Location": USER_PAGE})
        m.get(f"{PORTAL}/proxy_api/euda-apim/datarequest/vehicles/{VIN}/metadata/partial", status=404)
        with pytest.raises(api.NotConfigured):
            await client.identifier(VIN)
    await client.close()
