#!/usr/bin/env python3
"""Microsoft sign-in for Minecraft, using the OAuth 2.0 device authorization grant.

This is the same flow Prism, MultiMC and ATLauncher use, and the reason it is the right one:

  * **No password ever reaches this application.** You are shown a short code, you type it into
    microsoft.com/link in your own browser, and Microsoft hands this process a token afterwards.
    A launcher that asked for your password directly would be both unsafe and, since the Mojang
    account migration, no longer functional -- legacy Yggdrasil password auth is gone.
  * **It works without an embedded browser**, so there is no webview dependency on either platform.

The exchange is four hops, each of which can fail for its own reason, so each is reported distinctly
rather than as a generic "login failed":

    Microsoft OAuth  ->  Xbox Live  ->  XSTS  ->  Minecraft services  ->  profile

Only the refresh token is persisted. Access tokens live about a day; the refresh token renews them
without another sign-in, so this is a one-time action rather than a daily chore.

The one value that cannot be hardcoded is CLIENT_ID. Microsoft requires an Azure application
registration to identify the app making the request, and shipping some other project's ID would
misrepresent this launcher to Microsoft. Registering one is free: portal.azure.com ->
App registrations -> New registration -> personal Microsoft accounts -> Mobile and desktop
applications -> enable "Allow public client flows" -> copy the Application (client) ID.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
MC_ENTITLEMENTS_URL = "https://api.minecraftservices.com/entitlements/mcstore"

#: Where an unapproved app registration is sent. Since Mojang gated the Minecraft API, a *newly
#: created* Azure app gets HTTP 403 from api.minecraftservices.com until it is approved through this
#: form; registrations predating the change keep working. Approval is a review, then up to 24 hours
#: to propagate -- so a fresh client ID authenticates against Microsoft and Xbox Live successfully
#: and only fails at the Minecraft hop, which is confusing unless it is named.
APPROVAL_FORM = "https://help.minecraft.net/hc/en-us/articles/16254801392141"

SCOPE = "XboxLive.signin offline_access"


class AuthError(RuntimeError):
    """Carries which hop failed, so the user is told what to fix rather than 'login failed'."""

    def __init__(self, stage: str, detail: str):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


def _post_json(url: str, payload: dict, headers: dict | None = None, stage: str = "request") -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as failure:
        raise AuthError(stage, f"HTTP {failure.code}: {failure.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as failure:
        raise AuthError(stage, f"network error: {failure.reason}")


def _post_form(url: str, fields: dict, stage: str) -> dict:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as failure:
        # The token endpoint signals "still waiting" with HTTP 400, so the body must be parsed
        # rather than treated as a failure.
        try:
            return json.loads(failure.read().decode("utf-8"))
        except Exception:
            raise AuthError(stage, f"HTTP {failure.code}")
    except urllib.error.URLError as failure:
        raise AuthError(stage, f"network error: {failure.reason}")


def begin_device_login(client_id: str) -> dict:
    """Ask Microsoft for a device code. Returns the code, the URL to visit, and poll timings."""
    if not client_id:
        raise AuthError("configuration", "no Azure client ID configured")
    result = _post_form(DEVICE_CODE_URL,
                        {"client_id": client_id, "scope": SCOPE},
                        stage="device code")
    if "device_code" not in result:
        raise AuthError("device code", result.get("error_description") or str(result)[:200])
    return result


def poll_for_token(client_id: str, device: dict,
                   on_wait: Callable[[int], None] | None = None) -> dict:
    """Block until the user approves in their browser, then return the Microsoft token pair."""
    interval = int(device.get("interval", 5))
    deadline = time.monotonic() + int(device.get("expires_in", 900))
    while time.monotonic() < deadline:
        time.sleep(interval)
        result = _post_form(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": device["device_code"],
        }, stage="token")
        error = result.get("error")
        if error is None:
            return result
        if error == "authorization_pending":
            if on_wait:
                on_wait(int(deadline - time.monotonic()))
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "expired_token":
            raise AuthError("token", "the code expired before it was approved")
        if error == "authorization_declined":
            raise AuthError("token", "sign-in was declined in the browser")
        raise AuthError("token", result.get("error_description") or error)
    raise AuthError("token", "timed out waiting for browser approval")


def refresh(client_id: str, refresh_token: str) -> dict:
    """Renew without another sign-in. This is why only the refresh token is stored."""
    result = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
        "scope": SCOPE,
    }, stage="refresh")
    if "access_token" not in result:
        raise AuthError("refresh", result.get("error_description") or "refresh rejected")
    return result


def minecraft_session(ms_access_token: str) -> dict:
    """Trade a Microsoft token for a Minecraft session: Xbox Live -> XSTS -> Minecraft -> profile."""
    xbl = _post_json(XBL_URL, {
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": f"d={ms_access_token}"},
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }, stage="xbox live")
    xbl_token = xbl.get("Token")
    user_hash = (xbl.get("DisplayClaims", {}).get("xui") or [{}])[0].get("uhs")
    if not xbl_token or not user_hash:
        raise AuthError("xbox live", "no token in the response")

    try:
        xsts = _post_json(XSTS_URL, {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        }, stage="xsts")
    except AuthError as failure:
        # XSTS refuses with specific, actionable codes; a bare HTTP 401 helps nobody.
        detail = failure.detail
        if "2148916233" in detail:
            raise AuthError("xsts", "this Microsoft account has no Xbox profile. Sign in once at "
                                    "minecraft.net to create one, then try again.")
        if "2148916238" in detail:
            raise AuthError("xsts", "this account is a child account and must be added to a family "
                                    "before it can sign in.")
        raise
    xsts_token = xsts.get("Token")
    if not xsts_token:
        raise AuthError("xsts", "no token in the response")

    try:
        mc = _post_json(MC_LOGIN_URL,
                        {"identityToken": f"XBL3.0 x={user_hash};{xsts_token}"},
                        stage="minecraft")
    except AuthError as failure:
        if "403" in failure.detail:
            raise AuthError("minecraft", (
                "Microsoft accepted the sign-in, but this Azure application is not approved for the "
                "Minecraft API, so api.minecraftservices.com returned 403.\n\n"
                "Newly created Azure apps must be approved before they can be used with Minecraft. "
                f"Apply here: {APPROVAL_FORM}\n"
                "After approval, allow up to 24 hours for it to take effect."))
        raise
    mc_token = mc.get("access_token")
    if not mc_token:
        raise AuthError("minecraft", "no access token in the response")

    # Ownership check before the profile call: an account without Java Edition returns an empty
    # entitlements list, and reporting that plainly beats a bare 404 from the profile endpoint.
    request = urllib.request.Request(MC_ENTITLEMENTS_URL)
    request.add_header("Authorization", f"Bearer {mc_token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            entitlements = json.loads(response.read().decode("utf-8"))
        items = entitlements.get("items") or []
        if not items:
            raise AuthError("entitlements",
                            "this Microsoft account does not own Minecraft: Java Edition. "
                            "Game Pass accounts must launch the game once officially first.")
    except AuthError:
        raise
    except Exception:
        # A failed ownership probe must not block a legitimate sign-in; the profile call decides.
        pass

    request = urllib.request.Request(MC_PROFILE_URL)
    request.add_header("Authorization", f"Bearer {mc_token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            profile = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as failure:
        if failure.code == 404:
            raise AuthError("profile", "this account does not own Minecraft Java Edition.")
        raise AuthError("profile", f"HTTP {failure.code}")

    raw_uuid = profile.get("id") or ""
    formatted = "-".join((raw_uuid[:8], raw_uuid[8:12], raw_uuid[12:16],
                          raw_uuid[16:20], raw_uuid[20:])) if len(raw_uuid) == 32 else raw_uuid
    return {"username": profile.get("name"), "uuid": formatted,
            "accessToken": mc_token, "userType": "msa"}
