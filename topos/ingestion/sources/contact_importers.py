from __future__ import annotations

import json
import logging
import platform
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_PEOPLE_CONNECTIONS_URL = "https://people.googleapis.com/v1/people/me/connections"

logger = logging.getLogger("topos.ingestion.sources.contact_importers")


def _build_ssl_context() -> ssl.SSLContext:
    """
    Build a verified TLS context for outbound Google API calls.

    On some macOS Python runtimes, default trust roots are missing and
    certificate validation fails. Prefer certifi when available.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http_post_form(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    payload = urllib.parse.urlencode({k: v for k, v in body.items() if v is not None}).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_build_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"error": f"http_{e.code}", "error_description": raw}


def _http_get_json(url: str, bearer_token: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_build_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            raise RuntimeError(f"Google API request failed: HTTP {e.code}: {raw[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Google API TLS/network error: {e}. "
            "If this is a certificate verify failure, ensure certifi is installed in the engine environment."
        ) from e


def _normalize_phone(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    # Preserve a leading + where present, drop formatting characters.
    plus = s.startswith("+")
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return ""
    return f"+{digits}" if plus else digits


def import_apple_contacts_local() -> List[Dict[str, Any]]:
    """
    Read Apple Contacts locally on macOS via JXA (osascript JavaScript bridge).
    Returns normalized records: [{"display_name": str, "identifiers": [{"type","identifier"}]}]
    """
    current_platform = platform.system().lower()
    logger.info("[CONTACT_IMPORT] Apple import start: platform=%s", current_platform)
    if current_platform != "darwin":
        raise RuntimeError("Apple Contacts import is only available on macOS")
    osascript_bin = shutil.which("osascript")
    logger.info("[CONTACT_IMPORT] Apple import environment: osascript=%s", osascript_bin or "missing")
    if not osascript_bin:
        raise RuntimeError("osascript not found in PATH; Apple Contacts import requires macOS host runtime")

    jxa_script = r"""
const app = Application("Contacts");
const people = app.people();
const out = [];
for (let i = 0; i < people.length; i++) {
  const p = people[i];
  const name = (() => { try { return String(p.name() || "").trim(); } catch (_) { return ""; } })();
  const phones = [];
  const emails = [];
  try {
    const ph = p.phones();
    for (let j = 0; j < ph.length; j++) {
      const val = String(ph[j].value() || "").trim();
      if (val) phones.push(val);
    }
  } catch (_) {}
  try {
    const em = p.emails();
    for (let j = 0; j < em.length; j++) {
      const val = String(em[j].value() || "").trim();
      if (val) emails.push(val);
    }
  } catch (_) {}
  if (name || phones.length || emails.length) out.push({ name, phones, emails });
}
JSON.stringify(out);
"""
    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa_script],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        logger.error(
            "[CONTACT_IMPORT] Apple import osascript failed: returncode=%s stderr=%s stdout=%s",
            proc.returncode,
            stderr[:500],
            stdout[:500],
        )
        raise RuntimeError((stderr or stdout or "Failed to read Apple Contacts").strip())
    try:
        raw_items = json.loads(proc.stdout or "[]")
    except Exception as e:
        raise RuntimeError(f"Failed to parse Apple Contacts output: {e}") from e

    imported: List[Dict[str, Any]] = []
    for item in raw_items:
        name = str((item or {}).get("name") or "").strip()
        identifiers: List[Dict[str, str]] = []
        for p in (item or {}).get("phones") or []:
            phone = _normalize_phone(p)
            if phone:
                identifiers.append({"type": "phone", "identifier": phone})
        for e in (item or {}).get("emails") or []:
            email = str(e or "").strip().lower()
            if email:
                identifiers.append({"type": "email", "identifier": email})
        if identifiers:
            imported.append({"display_name": name or None, "identifiers": identifiers})
    logger.info(
        "[CONTACT_IMPORT] Apple import complete: raw_contacts=%d imported_contacts=%d",
        len(raw_items),
        len(imported),
    )
    return imported


def start_google_device_auth(client_id: str) -> Dict[str, Any]:
    if not str(client_id or "").strip():
        raise RuntimeError("google_client_id is required")
    logger.info("[CONTACT_IMPORT] Google device auth start requested")
    result = _http_post_form(
        GOOGLE_DEVICE_CODE_URL,
        {
            "client_id": client_id.strip(),
            "scope": "openid https://www.googleapis.com/auth/contacts.readonly",
        },
    )
    if result.get("error"):
        logger.warning(
            "[CONTACT_IMPORT] Google device auth start failed: error=%s description=%s",
            result.get("error"),
            result.get("error_description"),
        )
    else:
        logger.info("[CONTACT_IMPORT] Google device auth start succeeded")
    return result


def finish_google_device_auth(
    *,
    client_id: str,
    device_code: str,
    interval_seconds: int = 5,
    timeout_seconds: int = 120,
) -> Dict[str, Any]:
    logger.info(
        "[CONTACT_IMPORT] Google device auth finish polling start: interval=%s timeout=%s",
        interval_seconds,
        timeout_seconds,
    )
    started = time.time()
    interval = max(2, int(interval_seconds or 5))
    while True:
        result = _http_post_form(
            GOOGLE_TOKEN_URL,
            {
                "client_id": client_id.strip(),
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if result.get("access_token"):
            logger.info("[CONTACT_IMPORT] Google device auth finish succeeded")
            return result
        if result.get("error") not in {"authorization_pending", "slow_down"}:
            logger.warning(
                "[CONTACT_IMPORT] Google device auth finish failed: error=%s description=%s",
                result.get("error"),
                result.get("error_description"),
            )
            return result
        if time.time() - started > max(30, int(timeout_seconds)):
            return {"error": "authorization_timeout", "error_description": "Timed out waiting for Google authorization"}
        if result.get("error") == "slow_down":
            interval += 2
        time.sleep(interval)


def import_google_contacts(access_token: str) -> List[Dict[str, Any]]:
    logger.info("[CONTACT_IMPORT] Google contacts fetch start")
    imported: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    for _ in range(20):
        params = {
            "pageSize": "1000",
            "personFields": "names,emailAddresses,phoneNumbers",
            "sortOrder": "LAST_MODIFIED_ASCENDING",
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{GOOGLE_PEOPLE_CONNECTIONS_URL}?{urllib.parse.urlencode(params)}"
        data = _http_get_json(url, access_token)
        for person in data.get("connections") or []:
            name = None
            for n in person.get("names") or []:
                disp = str(n.get("displayName") or "").strip()
                if disp:
                    name = disp
                    break
            identifiers: List[Dict[str, str]] = []
            for p in person.get("phoneNumbers") or []:
                phone = _normalize_phone(p.get("value"))
                if phone:
                    identifiers.append({"type": "phone", "identifier": phone})
            for e in person.get("emailAddresses") or []:
                email = str(e.get("value") or "").strip().lower()
                if email:
                    identifiers.append({"type": "email", "identifier": email})
            if identifiers:
                imported.append({"display_name": name, "identifiers": identifiers})
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    logger.info("[CONTACT_IMPORT] Google contacts fetch complete: imported_contacts=%d", len(imported))
    return imported
