"""Client for the VW Group EU Data Act portal (eu-data-act.drivesomethinggreater.com).

Since June 2026, the CUPRA/SEAT app backend (OLA) only accepts requests that carry
a Firebase App Check / Play Integrity device token, which no third-party client can
mint. The EU Data Act portal is a web application with a normal OIDC login and is
therefore still reachable. It is not a live API: once the owner has enabled a
continuous data request in the browser, the portal stores a zipped JSON dataset
roughly every 15 minutes. Reading vehicle data means listing those datasets,
downloading the new ones and merging their data points.

This module has no Home Assistant dependency so it can be tested and probed on its own.
Protocol reference: evcc vehicle/vw/eudataact and ioBroker.vw-connect lib/euDataAct.js.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import string
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
PORTAL_HOST = "eu-data-act.drivesomethinggreater.com"
IDENTITY_URL = "https://identity.vwgroup.io"
IDENTITY_HOST = "identity.vwgroup.io"
AUTHORIZE_URL = f"{IDENTITY_URL}/oidc/v1/authorize"
REDIRECT_URI = f"{BASE_URL}/login"
SCOPE = "openid cars profile"

# CUPRA and SEAT share one identity client; the state suffix selects the brand UI.
BRANDS: dict[str, tuple[str, str]] = {
    "cupra": ("f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com", "CUPRA"),
    "seat": ("f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com", "SEAT"),
    "volkswagen": ("9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com", "VOLKSWAGEN_PASSENGER_CARS"),
    "audi": ("cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com", "AUDI"),
    "skoda": ("3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com", "SKODA"),
}

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

MAX_REDIRECTS = 15
MAX_RETRY_AFTER = 30
# number of datasets downloaded on first start; older ones only hold stale values
MAX_BACKFILL = 8


class DataActError(Exception):
    """Base error."""


class AuthError(DataActError):
    """Credentials were rejected."""


class ConsentRequired(DataActError):
    """The account must finish a step in the browser (terms, consent, vehicle link)."""

    def __init__(self, message: str, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


class NotConfigured(DataActError):
    """No data request exists for the vehicle on the portal."""


# --- login page parsing -------------------------------------------------------


class _FormParser(HTMLParser):
    """Collect forms with their action and input values."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self._current = {"id": a.get("id", ""), "action": a.get("action", ""), "inputs": {}}
            self.forms.append(self._current)
        elif tag == "input" and "name" in a:
            target = self._current["inputs"] if self._current is not None else None
            if target is None:
                # inputs outside a form (Auth0 pages sometimes render them that way)
                self.forms.append({"id": "", "action": "", "inputs": {}})
                self._current = self.forms[-1]
                target = self._current["inputs"]
            target[a["name"]] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def parse_forms(html: str) -> list[dict[str, Any]]:
    parser = _FormParser()
    parser.feed(html)
    return parser.forms


def parse_idk(html: str) -> dict[str, Any]:
    """Parse the `window._IDK = {...}` object of the legacy password page."""
    match = re.search(r"window\._IDK\s*=\s*(.*?)[;<]", html, re.S)
    if not match:
        raise DataActError("IDK form not found on password page")
    raw = match.group(1).replace("'", '"')
    # quote bare keys; the lookahead leaves URLs such as https:// alone
    raw = re.sub(r"(?<=[{,\s])(\w+)\s*:(?!/)", r'"\1":', raw)
    raw = re.sub(r",\s+}", "}", raw)
    return json.loads(raw)


def _nonce() -> str:
    return "".join(secrets.choice(string.ascii_letters) for _ in range(43))


def _is_user_page(url: URL) -> bool:
    return (
        url.scheme == "https"
        and url.host == PORTAL_HOST
        and url.path.startswith("/content/euda/")
        and url.path.endswith("/user.html")
    )


def _is_marketing_consent(url: URL) -> bool:
    return url.host == IDENTITY_HOST and "/consent/marketing/" in url.path


def _allowed_host(host: str | None) -> bool:
    return host in (IDENTITY_HOST, PORTAL_HOST)


# --- dataset handling ---------------------------------------------------------


@dataclass
class Point:
    """A single data point; key is a GUID, name the (possibly ambiguous) field name."""

    key: str
    name: str
    value: str
    timestamp: datetime | None
    seq: int = 0

    @property
    def id(self) -> str:
        return self.key or self.name

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "value": self.value,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Point:
        ts = d.get("timestamp")
        return cls(d.get("key", ""), d.get("name", ""), d["value"], parse_time(ts) if ts else None, d.get("seq", 0))


@dataclass
class Dataset:
    name: str
    created_on: datetime


def parse_time(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def content_datasets(items: Iterable[dict[str, Any]]) -> list[Dataset]:
    """Datasets with content, oldest first. `_no_content_found.zip` files mark a sleeping car."""
    res = []
    for item in items:
        name = item.get("name", "")
        if not name or name.lower().endswith("_no_content_found.zip"):
            continue
        created = item.get("createdOn")
        res.append(Dataset(name, parse_time(created) if created else datetime.min.replace(tzinfo=UTC)))
    res.sort(key=lambda d: d.created_on)
    return res


def parse_points(data: Iterable[dict[str, Any]]) -> list[Point]:
    """Decode the data points of one dataset, keeping the newest entry per id."""
    res: dict[str, Point] = {}
    for dp in data:
        value = dp.get("value")
        if value in (None, ""):
            continue
        ts = dp.get("timestampUtc")
        p = Point(dp.get("key") or "", dp.get("dataFieldName") or "", str(value), parse_time(ts) if ts else None)
        if not p.id:
            continue
        existing = res.get(p.id)
        if existing is None or _not_after(existing.timestamp, p.timestamp):
            res[p.id] = p
    return list(res.values())


def _not_after(a: datetime | None, b: datetime | None) -> bool:
    """Newest wins, on equal timestamps the later entry; a missing timestamp counts as oldest."""
    if a is None:
        return True
    return b is not None and a <= b


def parse_dataset(blob: bytes) -> tuple[str | None, list[Point]]:
    """Extract the JSON document from a dataset zip archive."""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next((n for n in zf.namelist() if n.lower().endswith(".json")), None)
        if name is None:
            raise DataActError("no json document in dataset")
        doc = json.loads(zf.read(name).decode("utf-8-sig"))
    return doc.get("vin"), parse_points(doc.get("Data") or [])


@dataclass
class VehicleState:
    """Merged data points of all datasets delivered for one vehicle."""

    identifier: str | None = None
    after: datetime | None = None
    newest_listed: datetime | None = None
    seq: int = 0
    points: dict[str, Point] = field(default_factory=dict)

    def merge(self, points: Iterable[Point]) -> None:
        self.seq += 1
        for p in points:
            p.seq = self.seq
            self.points[p.id] = p

    def find(self, ident: str) -> Point | None:
        if ident in self.points:
            return self.points[ident]
        for p in self.points.values():
            if p.name == ident:
                return p
        return None

    def lookup(self, *idents: str) -> Point | None:
        """Freshest value among ids ordered most to least authoritative; ties keep the order."""
        best: Point | None = None
        for ident in idents:
            p = self.find(ident)
            if p is not None and (best is None or p.seq > best.seq):
                best = p
        return best

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "after": self.after.isoformat() if self.after else None,
            "seq": self.seq,
            "points": [p.as_dict() for p in self.points.values()],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VehicleState:
        state = cls(identifier=d.get("identifier"), seq=d.get("seq", 0))
        if d.get("after"):
            state.after = parse_time(d["after"])
        for raw in d.get("points", []):
            p = Point.from_dict(raw)
            state.points[p.id] = p
        return state


def pending(datasets: list[Dataset], after: datetime | None) -> list[Dataset]:
    if after is None:
        return datasets[-MAX_BACKFILL:]
    return [d for d in datasets if d.created_on > after]


# --- client -------------------------------------------------------------------


class DataActClient:
    """Authenticated portal session for one account."""

    def __init__(
        self,
        username: str,
        password: str,
        brand: str = "cupra",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if brand not in BRANDS:
            raise ValueError(f"unknown brand {brand}")
        self._username = username
        self._password = password
        self._client_id, self._state = BRANDS[brand]
        self._own_session = session is None
        self._session = session or self._new_session()
        self._login_lock = asyncio.Lock()
        self._login_gen = 0

    @staticmethod
    def _new_session() -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(quote_cookie=False),
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=60),
        )

    async def close(self) -> None:
        if self._own_session:
            await self._session.close()

    # login

    async def login(self, gen: int | None = None) -> None:
        """Run the OIDC authorization-code flow; the portal keeps its session in cookies."""
        async with self._login_lock:
            if gen is not None and gen != self._login_gen:
                return  # another caller logged in meanwhile
            self._session.cookie_jar.clear()

            query = {
                "client_id": self._client_id,
                "response_type": "code",
                "scope": SCOPE,
                "state": f"de__en__{self._state}",
                "redirect_uri": REDIRECT_URI,
                "prompt": "login",
                "nonce": _nonce(),
            }
            url, html = await self._follow(URL(f"{AUTHORIZE_URL}?{urlencode(query)}"))
            if _is_user_page(url):
                self._login_gen += 1
                return

            final = await self._submit_credentials(url, html)
            final = await self._skip_marketing_consent(final)
            self._check_landing(final)
            self._login_gen += 1
            _LOGGER.debug("portal login complete")

    async def _submit_credentials(self, page_url: URL, html: str) -> URL:
        forms = parse_forms(html)
        legacy = next((f for f in forms if f["id"] == "emailPasswordForm"), None)
        if legacy is not None:
            return await self._submit_legacy(legacy)
        state = next((f["inputs"]["state"] for f in forms if f["inputs"].get("state")), None)
        if state is None:
            raise DataActError(f"unknown login page at {page_url.with_query(None)}")
        return await self._submit_universal(state, forms)

    async def _submit_legacy(self, form: dict[str, Any]) -> URL:
        inputs = form["inputs"]
        uri = urljoin(IDENTITY_URL, form["action"])
        async with self._session.post(
            uri,
            data={
                "_csrf": inputs.get("_csrf", ""),
                "relayState": inputs.get("relayState", ""),
                "hmac": inputs.get("hmac", ""),
                "email": self._username,
            },
        ) as resp:
            body = await resp.text()
        idk = parse_idk(body)
        model = idk.get("templateModel", {})
        if model.get("error"):
            raise AuthError(model["error"])
        password_uri = uri.replace(model["identifierUrl"], model["postAction"])
        return await self._post_and_follow(
            password_uri,
            {
                "_csrf": idk.get("csrf_token", ""),
                "relayState": model.get("relayState", ""),
                "hmac": model.get("hmac", ""),
                "email": self._username,
                "password": self._password,
            },
        )

    async def _submit_universal(self, state: str, forms: list[dict[str, Any]]) -> URL:
        """Auth0 Universal Login, live for CUPRA/VW since 2026-10-26."""
        final = await self._post_and_follow(
            f"{IDENTITY_URL}/u/login?{urlencode({'state': state})}",
            {"username": self._username, "password": self._password, "state": state},
        )
        if final.host == IDENTITY_HOST and final.path.startswith("/u/login"):
            # Auth0 re-renders the login page when it rejects the credentials
            raise AuthError("login rejected by identity provider")
        return final

    async def _post_and_follow(self, uri: str, data: dict[str, str]) -> URL:
        async with self._session.post(uri, data=data, allow_redirects=False) as resp:
            # Auth0 answers a wrong password with 400 and the re-rendered login page
            if resp.status in (400, 401, 403):
                raise AuthError(f"login rejected ({resp.status})")
            if resp.status >= 400:
                raise DataActError(f"login failed: {resp.status} {resp.reason}")
            if "Location" not in resp.headers:
                return resp.url
            location = resp.url.join(URL(resp.headers["Location"]))
        url, _ = await self._follow(location, stop=_is_marketing_consent)
        return url

    async def _follow(self, url: URL, stop: Callable[[URL], bool] | None = None) -> tuple[URL, str]:
        """GET along a redirect chain within the identity and portal hosts.

        Stops without fetching at the portal user page, at a non-https target, or where
        `stop` matches. Returns the final URL and, if fetched, its body.
        """
        for _ in range(MAX_REDIRECTS):
            if url.scheme != "https" or _is_user_page(url) or (stop and stop(url)):
                return url, ""
            if not _allowed_host(url.host):
                raise DataActError(f"unexpected login redirect host {url.host}")
            async with self._session.get(url, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    url = resp.url.join(URL(resp.headers["Location"]))
                    continue
                if resp.status >= 400:
                    raise DataActError(f"login step {url.with_query(None)} failed: {resp.status}")
                return resp.url, await resp.text()
        raise DataActError("too many login redirects")

    async def _skip_marketing_consent(self, url: URL) -> URL:
        """Decline the optional marketing consent through its own callback."""
        if not _is_marketing_consent(url):
            return url
        callback = parse_qs(urlparse(str(url)).query).get("callback", [None])[0]
        if not callback:
            raise DataActError("marketing consent page without callback")
        cb = URL(callback)
        if cb.scheme != "https" or cb.host != IDENTITY_HOST or not cb.path.startswith("/oidc/v1/oauth/client/callback"):
            raise DataActError("unexpected marketing consent callback")
        final, _ = await self._follow(cb)
        return final

    @staticmethod
    def _check_landing(url: URL) -> None:
        if error := url.query.get("error"):
            raise AuthError(error)
        path = url.path
        if any(s in path for s in ("signin-service", "/consent", "/error", "/terms")):
            raise ConsentRequired(
                "Login stopped at a consent or terms page. Sign in to the EU Data Act portal "
                "in a browser once and accept it.",
                str(url),
            )
        if url.host != PORTAL_HOST or not _is_user_page(url):
            raise DataActError(f"login did not complete, landed on {url.with_query(None)}")

    # data

    async def _get(self, path: str, headers: dict[str, str] | None = None) -> aiohttp.ClientResponse:
        """GET a portal resource; re-login once on 401/403 and honour short Retry-After waits."""
        hdrs = {"Accept": "application/json", **(headers or {})}
        relogged = False
        for _ in range(4):
            gen = self._login_gen
            resp = await self._session.get(f"{BASE_URL}{path}", headers=hdrs, allow_redirects=False)
            if resp.status in (401, 403) or (resp.status == 302 and not relogged):
                resp.release()
                if relogged:
                    raise AuthError(f"portal rejected session ({resp.status})")
                await self.login(gen)
                relogged = True
                continue
            if resp.status == 429:
                wait = min(int(resp.headers.get("Retry-After", "5") or 5), MAX_RETRY_AFTER)
                resp.release()
                await asyncio.sleep(wait)
                continue
            return resp
        raise DataActError(f"giving up on {path}")

    async def _get_json(self, path: str, headers: dict[str, str] | None = None) -> Any:
        resp = await self._get(path, headers)
        async with resp:
            if resp.status == 404:
                return None
            if 300 <= resp.status < 400:
                raise AuthError(f"portal redirected {path} to login")
            if resp.status >= 400:
                raise DataActError(f"GET {path}: {resp.status} {await resp.text()}")
            return await resp.json(content_type=None)

    async def vehicles(self) -> list[dict[str, Any]]:
        """Vehicles linked to the portal account as dicts with vin and name."""
        res = await self._get_json("/proxy_api/consent/me/vehicles?viewPosition=FRONT_LEFT")
        items = res.get("vehicles", []) if isinstance(res, dict) else (res or [])
        out = []
        for v in items:
            vin = v.get("vin") or v.get("vehicleIdentificationNumber")
            if not vin:
                continue
            name = next(
                (v[k] for k in ("nickName", "vehicleNickname", "nickname", "modelName") if v.get(k)),
                vin,
            )
            out.append({"vin": vin, "name": name, "model": v.get("modelName")})
        return out

    async def identifier(self, vin: str) -> str:
        res = await self._get_json(f"/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial")
        if not res or not res.get("Identifier"):
            raise NotConfigured(vin)
        return res["Identifier"]

    async def datasets(self, vin: str, identifier: str) -> list[dict[str, Any]]:
        # 404 "No files available for this request" until the first delivery
        res = await self._get_json(
            f"/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list",
            {"type": "partial"},
        )
        if isinstance(res, dict):
            return res.get("files", [])
        return res or []

    async def download(self, vin: str, identifier: str, name: str) -> bytes:
        resp = await self._get(
            f"/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download",
            {"filename": name, "type": "partial", "Accept": "*/*"},
        )
        async with resp:
            if resp.status >= 400:
                raise DataActError(f"download {name}: {resp.status}")
            return await resp.read()

    async def update(self, vin: str, state: VehicleState) -> int:
        """Download datasets newer than the state and merge them; returns the number merged."""
        if state.identifier is None:
            state.identifier = await self.identifier(vin)

        listed = await self.datasets(vin, state.identifier)
        for item in listed:
            if item.get("createdOn"):
                ts = parse_time(item["createdOn"])
                if state.newest_listed is None or ts > state.newest_listed:
                    state.newest_listed = ts

        merged = 0
        for ds in pending(content_datasets(listed), state.after):
            _, points = parse_dataset(await self.download(vin, state.identifier, ds.name))
            state.merge(points)
            state.after = ds.created_on
            merged += 1
        return merged


def charge_status(
    state: VehicleState, plug: tuple[str, ...], charging: tuple[str, ...], scenario: tuple[str, ...]
) -> tuple[bool | None, bool | None]:
    """Derive (plugged_in, charging) from the plug, charge state and charging scenario fields."""
    plug_p = state.lookup(*plug)
    charge_p = state.lookup(*charging)
    scenario_p = state.lookup(*scenario)
    if plug_p is None and charge_p is None and scenario_p is None:
        return None, None

    plugged = plug_p is not None and plug_p.value.lower() == "connected"
    is_charging = False
    if charge_p is not None:
        v = charge_p.value
        if (
            "CHARGING_HV" in v.upper()
            or v.lower() in ("charging", "conservationcharging")
            or v.upper() == "CHARGE_STATE_CONSERVATION_CHARGING"
        ):
            is_charging = True
    if scenario_p is not None and not is_charging:
        upper = scenario_p.value.upper()
        if upper.endswith("_ACTIVE"):
            is_charging = True
        elif upper.endswith("_FINISHED"):
            plugged = True  # the car reports finished while plugged in but not charging
    return plugged or is_charging, is_charging
