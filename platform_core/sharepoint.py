"""SharePoint Online and OneDrive for Business, through Microsoft Graph.

Links are resolved with the Graph *shares* endpoint rather than by parsing
SharePoint URLs. Pasting any of these therefore works through one code path:

    https://contoso.sharepoint.com/sites/Team/Shared Documents/spec.docx
    https://contoso.sharepoint.com/:w:/r/sites/Team/_layouts/15/Doc.aspx?sourcedoc=...
    https://contoso-my.sharepoint.com/:b:/g/personal/a_contoso_com/EaBc...
    https://contoso.sharepoint.com/sites/Team/Shared Documents/Handbook   (a folder)

SharePoint's own URL formats are undocumented, numerous and change; `/shares/u!{...}`
accepts whatever the user copied out of the browser and hands back the drive item.

**What this can reach.** Authentication is app-only client credentials, so the
permission granted to the app registration is the boundary - not the permission of
the person pasting the link. Under `Sites.Read.All` that is every site in the
tenant, which means a user who can import can obtain a document they could not open
in SharePoint themselves. That is a deliberate deployment choice; `Sites.Selected`
narrows it to sites an administrator grants individually and needs no change here,
because the restriction is applied by Entra ID rather than by this code.

Every request goes through the hardened fetcher, so Graph is subject to the same
address, redirect, size and timeout rules as any other host.
"""

import base64
import json
import time
import urllib.parse

from django.conf import settings

from .fetching import FetchError, fetch

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"

#: Extensions worth importing from a document library. Everything else is binary
#: or an application file that conversion would not produce useful text from.
DOC_SUFFIXES = (
    ".docx", ".doc", ".pdf", ".pptx", ".ppt", ".xlsx", ".xls",
    ".md", ".txt", ".csv", ".json", ".xml", ".html", ".htm",
)

MAX_FILES = 25

#: Tokens last an hour; refreshing a minute early avoids racing the expiry.
_token_cache = {}


def configured():
    return bool(getattr(settings, "SHAREPOINT_TENANT", "") and
                getattr(settings, "SHAREPOINT_CLIENT_ID", ""))


def is_sharepoint(parsed):
    host = (parsed.hostname or "").lower()
    return host.endswith(".sharepoint.com") or host == "graph.microsoft.com"


def access_token(secret):
    """A client-credentials token for Graph, cached until shortly before expiry."""
    tenant = getattr(settings, "SHAREPOINT_TENANT", "")
    client = getattr(settings, "SHAREPOINT_CLIENT_ID", "")
    if not tenant or not client:
        raise FetchError(
            "SharePoint is not configured for this deployment. An operator sets "
            "sharepoint_tenant and sharepoint_client_id, and mounts the client secret."
        )
    cached = _token_cache.get(client)
    if cached and cached[1] > time.time():
        return cached[0]

    body = urllib.parse.urlencode(
        {
            "client_id": client,
            "client_secret": secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
    ).encode()
    payload = _post(f"{LOGIN}/{tenant}/oauth2/v2.0/token", body)
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise FetchError("Microsoft did not return an access token. Check the client secret.")
    expires = payload.get("expires_in")
    lifetime = expires if isinstance(expires, int) and expires > 60 else 3600
    _token_cache[client] = (token, time.time() + lifetime - 60)
    return token


def _post(url, body):
    """A form POST to the token endpoint, through the same hardened client."""
    from .fetching import _connection, normalise, resolve

    parsed = normalise(url)
    family, literal = resolve(parsed.hostname, 443)[0]
    connection = _connection(parsed, family, literal)
    try:
        connection.request(
            "POST",
            parsed.path,
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Digital-Brain",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        data = response.read(1024 * 1024)
        if response.status != 200:
            raise FetchError(
                "Microsoft rejected the SharePoint credential. Check the tenant, "
                "client id, client secret and that admin consent has been granted."
            )
        return json.loads(data)
    except FetchError:
        raise
    except Exception:
        raise FetchError("Microsoft could not be reached to authenticate.") from None
    finally:
        connection.close()


def _graph(url, token):
    body, _, _, _ = fetch(url, headers={"Authorization": f"Bearer {token}"})
    try:
        return json.loads(body)
    except ValueError:
        raise FetchError("Microsoft Graph returned an unexpected response.") from None


def share_id(url):
    """Graph's encoding of a sharing URL: u! + unpadded base64url."""
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii")
    return "u!" + encoded.rstrip("=")


def _item_reference(item):
    """A durable Graph address for an item, rather than an expiring download URL."""
    parent = item.get("parentReference") or {}
    drive = parent.get("driveId")
    item_id = item.get("id")
    if not isinstance(drive, str) or not isinstance(item_id, str):
        raise FetchError("Microsoft Graph did not identify that item.")
    return f"{GRAPH}/drives/{drive}/items/{item_id}"


def plan(parsed, secret):
    """The files a SharePoint link resolves to, as (name, graph item url) pairs."""
    token = access_token(secret)
    item = _graph(f"{GRAPH}/shares/{share_id(parsed.geturl())}/driveItem", token)
    if not isinstance(item, dict) or "id" not in item:
        raise FetchError(
            "That SharePoint item could not be opened. Check the link and that the "
            "application has been granted access to the site."
        )
    name = item.get("name") or "sharepoint-item"

    if "folder" in item:
        children = _graph(_item_reference(item) + "/children?$top=200", token)
        entries = children.get("value") if isinstance(children, dict) else None
        if not isinstance(entries, list):
            raise FetchError("Microsoft Graph returned an unexpected folder listing.")
        found = []
        for child in entries:
            if not isinstance(child, dict) or "file" not in child:
                continue
            child_name = child.get("name") or ""
            if child_name.lower().endswith(DOC_SUFFIXES) and len(found) < MAX_FILES:
                found.append((f"{name}-{child_name}"[:200], _item_reference(child)))
        if not found:
            raise FetchError(
                "No importable documents were found in that folder. This imports "
                + ", ".join(DOC_SUFFIXES)
                + " files."
            )
        return found

    if "file" not in item:
        raise FetchError("That link points at something that is not a file or folder.")
    return [(name[:200], _item_reference(item))]


def download_url(item_url, secret):
    """A fresh pre-authenticated download address for a stored Graph item.

    Graph's /content endpoint answers with a redirect, and redirects are never
    followed, so the download URL is read from the item instead. Reading it at
    download time rather than at import time also means it cannot expire while the
    item sits in the queue.
    """
    token = access_token(secret)
    item = _graph(f"{item_url}?$select=id,name,@microsoft.graph.downloadUrl", token)
    url = item.get("@microsoft.graph.downloadUrl") if isinstance(item, dict) else None
    if not isinstance(url, str) or not url.startswith("https://"):
        raise FetchError("Microsoft Graph did not provide a download link for that document.")
    return url, item.get("name") or ""
