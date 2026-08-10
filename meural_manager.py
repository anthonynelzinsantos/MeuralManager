#!/usr/bin/env python3
"""MeuralManager. Manage your Netgear Meural library from the command line. See README.md for usage. Requires MEURAL_TOKEN; run any command without it for instructions.
"""

import argparse
import base64
import collections
import datetime
import io
import json
import mimetypes
import os
import stat
import sys
import time

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:                                    # very old urllib3
    from requests.packages.urllib3.util.retry import Retry

API_HOST = "https://api.meural.com"
API_VERSIONS = ("v0", "v1")
AUTH_SCHEMES = ("Token", "Bearer")
PAGE_SIZE = 100
READ_DELAY = 1.0
WRITE_DELAY = 3.0
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}

# (connect, read) seconds. Uploads get a longer read timeout because the server
# resizes the image before answering, but never an unbounded one - a bare call
# with no timeout is what makes this script appear to hang.
TIMEOUT = (10, 30)
UPLOAD_TIMEOUT = (10, 180)
MEMBER_TIMEOUT = (10, 60)      # playlist membership calls can be slow too

# Retry policy applied to every call. Meural's backend fails transiently often
# enough that one attempt is not enough for any request.
RETRY_ATTEMPTS = 4
RETRY_STATUSES = (429, 500, 502, 503, 504)
BACKOFF_BASE = 4.0             # seconds; doubles each attempt

# Per-file attempts for uploads, with exponential backoff between them.
UPLOAD_ATTEMPTS = 3

STATE_FILE = ".meural_upload_state.json"

session = requests.Session()

# Transport-level retries for transient failures. This covers connection resets
# and 5xx on idempotent reads; uploads are retried explicitly in upload_item,
# since POST is not safely retried at this layer.
_retry = Retry(total=3, connect=3, read=2, backoff_factor=1.5,
               status_forcelist=(429, 500, 502, 503, 504),
               allowed_methods=frozenset(["GET", "DELETE"]))
session.mount("https://", HTTPAdapter(max_retries=_retry, pool_maxsize=4))



# ============================================================ authentication ==
#
# Meural has two login paths.
#
# api.meural.com/authenticate takes a username and password, but authenticates
# against an older Meural-native user directory. For a NETGEAR account it
# answers 200 with a token that every data endpoint then refuses, which is the
# trap almost every old Meural script falls into.
#
# accounts.netgear.com is where the app and web portal actually sign in, and it
# is behind bot-detection middleware that signs each request from obfuscated
# in-page JavaScript. There is no honest way to mint one of those from a
# script.
#
# So the token comes from the browser. That is not a workaround; it is the only
# route that works.

CONFIG_DIR = os.path.expanduser("~/.meural-manager")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token.json")

# Assumed lifetime for an opaque token. A JWT carries its own expiry, which
# wins. Either way a 401 is the final authority.
TOKEN_MAX_AGE = 3 * 3600

PORTAL_URL = "https://my.meural.netgear.com"

# Sent by Meural’s own clients on API calls. Not a credential and not used to
# log in. Kept only because some deployments want it alongside the token.
X_CLIENT_ID = "487bd4kvb1fnop6mbgk8gu5ibf"


# ------------------------------------------------------------------ storage

def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, stat.S_IRWXU)                    # 0700
    except OSError:
        pass


def save_token(token):
    _ensure_dir()
    with open(TOKEN_FILE, "w") as handle:
        json.dump({"token": token, "captured_at": time.time()}, handle)
    try:
        os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)     # 0600
    except OSError:
        pass


def invalidate_token():
    """Delete the cached token as soon as the API rejects it, so a dead token
    is never reloaded on the next run."""
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass


def clear_stored_token():
    """Forget the saved token. Returns what was removed, for `logout`."""
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
        return ["saved token"]
    return []


# ------------------------------------------------------------------- tokens

def token_expiry(token):
    """Expiry timestamp for a JWT, or None if it isn’t one."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    expires = claims.get("exp")
    return float(expires) if isinstance(expires, (int, float)) else None


def describe_expiry(token):
    """How long a token has left, in words. None if it doesn’t say."""
    expires = token_expiry(token)
    if not expires:
        return None
    remaining = expires - time.time()
    if remaining <= 0:
        return "it has already expired"
    hours, minutes = divmod(int(remaining) // 60, 60)
    if hours:
        return "good for about {}h {}m".format(hours, minutes)
    return "good for about {} minutes".format(minutes)


def load_cached_token(max_age=TOKEN_MAX_AGE):
    """The cached token, if it’s still plausibly valid."""
    try:
        with open(TOKEN_FILE) as handle:
            data = json.load(handle)
    except (IOError, ValueError):
        return None

    token = data.get("token")
    if not token:
        return None

    expires = token_expiry(token)
    if expires:
        # A minute of slack, so we never hand over a token about to die.
        return token if expires - 60 > time.time() else None

    if max_age and (time.time() - data.get("captured_at", 0)) > max_age:
        return None
    return token


def extract_token(text):
    """Tidy up a pasted token.

    Tolerates surrounding whitespace and a leading "Token " or "Bearer ", since
    that is what gets copied when you grab the whole header value.
    """
    token = (text or "").strip().strip('"\'')
    parts = token.split()
    if len(parts) == 2 and parts[0].lower() in ("token", "bearer"):
        token = parts[1]
    return token or None


# -------------------------------------------------------------------- probe

def probe_token(token, verbose=False):
    """Find a working (base, headers) for this token, or None.

    Tries each API version, each auth scheme, and both with and without the
    X-ClientId header, since some deployments want it on every call.
    """
    for version in API_VERSIONS:
        base = "{}/{}".format(API_HOST, version)
        for scheme in AUTH_SCHEMES:
            for with_client_id in (True, False):
                headers = {"Authorization": "{} {}".format(scheme, token)}
                if with_client_id:
                    headers["X-ClientId"] = X_CLIENT_ID
                try:
                    response = session.get(base + "/user/items",
                                           params={"count": 1, "page": 1},
                                           headers=headers, timeout=TIMEOUT)
                except (requests.Timeout, requests.ConnectionError):
                    continue
                if verbose:
                    print("      {} {} {}: HTTP {}".format(
                        version, scheme,
                        "+clientid" if with_client_id else "         ",
                        response.status_code))
                if response.status_code == 200:
                    return base, headers
    return None


# --------------------------------------------------------------------- flow

def token_instructions():
    """How to fetch a token and hand it to the script."""
    return """
Meural’s only working login is the one in your browser, so the token has to
come from there:

  1. Log in at {portal}
  2. Open the developer tools (F12, or Cmd-Option-I on a Mac)
  3. Go to the Network tab, then click around the site so requests appear
  4. Click any request whose domain is api.meural.com
  5. Under Request Headers, find the line beginning "authorization:"
  6. Copy what follows the word "Token"

Then set it in your shell and run the command again:

  export MEURAL_TOKEN='paste-the-token-here'
  {prog} login

Keep the quotes. The variable lasts for the current shell session, so you will
need to set it again in a new terminal window, or whenever the token expires.
""".format(portal=PORTAL_URL, prog=os.path.basename(sys.argv[0]) or "meural.py")


def establish_token(reason=None):
    """Take the token from MEURAL_TOKEN, verify it, and cache it.

    Returns (base, headers, token). Exits with instructions if the variable is
    unset or the token is refused.
    """
    if reason:
        print("\n{}".format(reason))

    raw = os.environ.get("MEURAL_TOKEN", "").strip()
    if not raw:
        print(token_instructions())
        sys.exit("MEURAL_TOKEN is not set.")

    token = extract_token(raw)
    if not token:
        print(token_instructions())
        sys.exit("MEURAL_TOKEN is set but empty.")

    found = probe_token(token)
    if not found:
        print(token_instructions())
        sys.exit("The token in MEURAL_TOKEN was not accepted. It has probably "
                 "expired, or was copied from a request to "
                 "accounts.netgear.com rather than api.meural.com.")

    save_token(token)
    note = describe_expiry(token)
    print("Token accepted; using {}{}.".format(
        found[0], " - " + note if note else ""))
    return found[0], found[1], token


def get_token():
    """The token from MEURAL_TOKEN, or the cached one. None if neither."""
    from_env = os.environ.get("MEURAL_TOKEN", "").strip()
    if from_env:
        return extract_token(from_env)
    return load_cached_token()


# =================================================================== api ==

def resolve_endpoint(token, token_was_cached=True):
    """Find where this token works. Returns (base, headers, token)."""
    if token:
        found = probe_token(token)
        if found:
            return found[0], found[1], token
        invalidate_token()
        print("Saved token has expired." if token_was_cached
              else "That token was not accepted.")

    return establish_token()


class Meural(object):
    """Thin wrapper over the Meural REST API."""

    def __init__(self, force_login=False):
        if force_login:
            invalidate_token()
            self.base, self.auth, self.token = establish_token()
        else:
            cached = get_token()
            self.base, self.auth, self.token = resolve_endpoint(
                cached, token_was_cached=bool(cached))

        # Lazily filled by the items/galleries properties, so a command that
        # needs both does not walk the API twice.
        self._items = None
        self._galleries = None

    def reauthenticate(self):
        """Called when the API rejects the token mid-run.

        The token comes from MEURAL_TOKEN, which cannot change while the
        process is running, so there is nothing to retry with. The run stops
        cleanly and resumes when re-run with a fresh token.
        """
        print("\n    token expired")
        invalidate_token()
        print(token_instructions())
        print("    Progress has been saved. Set a fresh MEURAL_TOKEN and run "
              "the same command again;\n    completed work is skipped.")
        return False

    # -- transport --------------------------------------------------------

    def request(self, method, path, attempts=RETRY_ATTEMPTS, timeout=TIMEOUT,
                quiet=False, **kwargs):
        """Perform one API call, retrying transient failures.

        Returns a response, or None once the attempts are exhausted. Every
        call in this class goes through here, so timeouts can never escape as
        a traceback.
        """
        headers = dict(self.auth)              # Authorization, and X-ClientId
        headers.update(kwargs.pop("headers", {}))   # when the API wants it
        url = self.base + path

        for attempt in range(1, attempts + 1):
            try:
                response = session.request(method, url, headers=headers,
                                           timeout=timeout, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                kind = "timed out" if isinstance(exc, requests.Timeout) \
                    else "connection lost"
                if attempt == attempts:
                    if not quiet:
                        print("    {} {}: {} after {} attempts".format(
                            method, path, kind, attempts))
                    return None
                pause = BACKOFF_BASE * (2 ** (attempt - 1))
                if not quiet:
                    print("    {} {}: {}, retrying in {:.0f}s".format(
                        method, path, kind, pause))
                time.sleep(pause)
                continue

            if response.status_code == 401:
                self.reauthenticate()
                sys.exit(1)

            if response.status_code in RETRY_STATUSES and attempt < attempts:
                pause = BACKOFF_BASE * (2 ** (attempt - 1))
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    pause = max(pause, int(retry_after))
                if not quiet:
                    print("    {} {}: HTTP {}, retrying in {:.0f}s".format(
                        method, path, response.status_code, pause))
                time.sleep(pause)
                continue

            return response

        return None

    # -- reads ------------------------------------------------------------

    def walk(self, path):
        page = 1
        while True:
            response = self.request("GET", path,
                                    params={"count": PAGE_SIZE, "page": page})
            if response is None:
                sys.exit("Gave up reading {} at page {}. The API is not "
                         "responding; try again shortly.".format(path, page))
            if response.status_code != 200:
                sys.exit("HTTP {} on {} page {}: {}".format(
                    response.status_code, path, page, response.text[:400]))
            try:
                data = response.json()
            except ValueError:
                sys.exit("Non-JSON response on {} page {}".format(path, page))
            for row in data.get("data", []):
                yield row
            if data.get("isLast", True):
                break
            page += 1
            time.sleep(READ_DELAY)

    @property
    def items(self):
        if self._items is None:
            self._items = list(self.walk("/user/items"))
        return self._items

    @property
    def galleries(self):
        if self._galleries is None:
            self._galleries = list(self.walk("/user/galleries"))
        return self._galleries

    def devices(self):
        """Inferred endpoint - the caller shows the raw response on failure."""
        return self.request("GET", "/user/devices")

    # -- writes -----------------------------------------------------------

    def create_gallery(self, name):
        response = self.request("POST", "/galleries", json={"name": name})
        if response is None:
            sys.exit("Could not reach the API to create the playlist.")
        print("Create playlist {!r}: HTTP {}".format(name, response.status_code))
        print("  response: {}".format(response.text[:400]))
        if response.status_code not in (200, 201):
            sys.exit("Could not create the playlist. Create it in the web "
                     "interface instead, then target it with --playlist N.")
        payload = response.json()
        gallery = payload.get("data", payload)
        gallery_id = gallery.get("id")
        if gallery_id is None:
            sys.exit("Playlist created but no id returned; re-run with "
                     "--playlist N.")
        return gallery_id

    def upload_item(self, filepath, payload_bytes=None):
        """POST an image, returning the new item id.

        Retries on timeouts, connection errors, 429 and 5xx, with exponential
        backoff. `payload_bytes` lets the caller supply a downscaled image
        instead of the file on disk.
        """
        name = os.path.basename(filepath)
        mime = mimetypes.guess_type(filepath)[0] or "application/octet-stream"

        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                if payload_bytes is None:
                    with open(filepath, "rb") as handle:
                        files = {"image": (name, handle, mime)}
                        response = session.post(
                            self.base + "/items",
                            headers=dict(self.auth),
                            files=files,
                            timeout=UPLOAD_TIMEOUT)
                else:
                    files = {"image": (name, io.BytesIO(payload_bytes), mime)}
                    response = session.post(
                        self.base + "/items",
                        headers=dict(self.auth),
                        files=files,
                        timeout=UPLOAD_TIMEOUT)

            except (requests.Timeout, requests.ConnectionError) as exc:
                kind = "timed out" if isinstance(exc, requests.Timeout) \
                    else "connection lost"
                if attempt == UPLOAD_ATTEMPTS:
                    print("    {} after {} attempts, giving up".format(
                        kind, UPLOAD_ATTEMPTS))
                    return None
                pause = WRITE_DELAY * (2 ** attempt)
                print("    {} (attempt {}/{}), retrying in {:.0f}s".format(
                    kind, attempt, UPLOAD_ATTEMPTS, pause))
                time.sleep(pause)
                continue

            if response.status_code == 401:
                self.reauthenticate()
                sys.exit(1)

            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == UPLOAD_ATTEMPTS:
                    print("    HTTP {} after {} attempts, giving up".format(
                        response.status_code, UPLOAD_ATTEMPTS))
                    return None
                pause = WRITE_DELAY * (2 ** attempt)
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    pause = max(pause, int(retry_after))
                print("    HTTP {} (attempt {}/{}), retrying in {:.0f}s".format(
                    response.status_code, attempt, UPLOAD_ATTEMPTS, pause))
                time.sleep(pause)
                continue

            if response.status_code not in (200, 201):
                print("    upload failed: HTTP {} {}".format(
                    response.status_code, response.text[:200]))
                return None

            try:
                payload = response.json()
            except ValueError:
                print("    upload returned non-JSON: {}".format(
                    response.text[:200]))
                return None

            item = payload.get("data", payload)
            item_id = item.get("id")
            if item_id is None:
                print("    no item id in response: {}".format(
                    json.dumps(payload)[:200]))
            return item_id

        return None

    def add_to_gallery(self, gallery_id, item_id):
        """Add an item to a playlist. Returns a status code, or None if the
        API never answered - in which case use is_in_gallery to find out
        whether it went through anyway."""
        response = self.request(
            "POST", "/galleries/{}/items/{}".format(gallery_id, item_id),
            timeout=MEMBER_TIMEOUT)
        return None if response is None else response.status_code

    def is_in_gallery(self, gallery_id, item_id):
        """Check membership directly. A write that times out may still have
        been applied server-side, so ask rather than assume."""
        response = self.request(
            "GET", "/galleries/{}/items".format(gallery_id),
            params={"count": PAGE_SIZE, "page": 1}, quiet=True)
        if response is None or response.status_code != 200:
            return None                                # genuinely unknown
        try:
            rows = response.json().get("data", [])
        except ValueError:
            return None
        return any(row.get("id") == item_id for row in rows)

    def add_to_device(self, device_id, item_id):
        response = self.request(
            "POST", "/devices/{}/items/{}".format(device_id, item_id),
            timeout=MEMBER_TIMEOUT)
        return None if response is None else response.status_code

    def delete_item(self, item_id):
        response = self.request(
            "DELETE", "/items/{}".format(item_id),
            headers={"Content-Type": "application/json", "Accept": "*/*"})
        return None if response is None else response.status_code

    def delete_gallery(self, gallery_id):
        response = self.request(
            "DELETE", "/galleries/{}".format(gallery_id),
            headers={"Content-Type": "application/json", "Accept": "*/*"})
        return None if response is None else response.status_code


# ================================================================ helpers ==

def playlist_table(api):
    """Print the numbered playlist table and return orphans and membership."""
    item_ids = {item["id"] for item in api.items}
    membership = collections.Counter()
    for gallery in api.galleries:
        for item_id in gallery.get("itemIds", []):
            membership[item_id] += 1

    print("\n{:>3}  {:<40} {:>7} {:>9}".format("#", "Playlist", "items", "yours"))
    print("-" * 63)
    for index, gallery in enumerate(api.galleries, start=1):
        gallery_ids = set(gallery.get("itemIds", []))
        print("{:>3}  {:<40} {:>7} {:>9}".format(
            index,
            str(gallery.get("name", "(unnamed)"))[:40],
            len(gallery_ids),
            len(gallery_ids & item_ids)))
    orphans = sorted(item_ids - set(membership))
    print("-" * 63)
    print("{:>3}  {:<40} {:>7} {:>9}".format(
        "o", "(items in no playlist)", len(orphans), len(orphans)))
    print("\nLibrary: {} uploaded items across {} playlists".format(
        len(item_ids), len(api.galleries)))

    shared = [i for i in item_ids if membership[i] > 1]
    if shared:
        print("Note: {} item(s) sit in more than one playlist. Deleting one "
              "playlist’s items removes them from the others too.".format(len(shared)))
    return orphans, membership


def image_url(item):
    """Best available original-image URL from an item record."""
    for key in ("originalUrl", "image", "imageUrl", "url",
                "hiResUrl", "previewUrl", "thumbnailUrl"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def prepare_image(filepath, max_edge, quality=90):
    """Downscale an image in memory. Returns bytes, or None to send as-is.

    The Canvas is a 1920x1080 panel, so full-resolution camera files are many
    times larger than anything it can show. Shrinking locally cuts the transfer
    and spares the server the resize that makes uploads crawl.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        print("  (Pillow not installed - sending originals. "
              "pip install Pillow to enable --resize)")
        return None

    try:
        with Image.open(filepath) as image:
            image = ImageOps.exif_transpose(image)     # honour camera rotation
            if max(image.size) <= max_edge:
                return None                            # already small enough
            image.thumbnail((max_edge, max_edge), Image.LANCZOS)
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            return buffer.getvalue()
    except Exception as exc:                           # unreadable, odd format
        print("    could not resize ({}), sending original".format(exc))
        return None


def load_state():
    """Filenames already uploaded successfully in a previous run."""
    try:
        with open(STATE_FILE) as handle:
            return set(json.load(handle))
    except (IOError, ValueError):
        return set()


def save_state(done):
    try:
        with open(STATE_FILE, "w") as handle:
            json.dump(sorted(done), handle, indent=2)
    except IOError:
        pass                                           # best effort only


def collect_files(paths):
    found = []
    for path in paths:
        if os.path.isdir(path):
            for entry in sorted(os.listdir(path)):
                full = os.path.join(path, entry)
                if os.path.isfile(full) and \
                        os.path.splitext(entry)[1].lower() in IMAGE_SUFFIXES:
                    found.append(full)
        elif os.path.isfile(path):
            found.append(path)
        else:
            sys.exit("No such file or directory: {}".format(path))
    if not found:
        sys.exit("No image files found.")
    return found


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def confirm(prompt="Type DELETE to confirm: ", word="DELETE"):
    if input(prompt).strip() != word:
        sys.exit("Cancelled.")


# =============================================================== commands ==

def cmd_login(api, args):
    print("Token cached; other commands will use it until it expires.")
    playlist_table(api)


def cmd_logout(api, args):
    removed = clear_stored_token()
    print("Removed: {}".format(", ".join(removed) if removed else "nothing"))


def cmd_diagnose(api, args):
    """Show exactly how the API responds to your token. Reveals no secrets."""
    token = get_token()
    if not token:
        print(token_instructions())
        return

    print("\n=== token ===")
    print("  length: {} characters".format(len(token)))
    print("  starts: {!r}".format(token[:8]))
    looks_jwt = token.count(".") == 2
    print("  shape:  {}".format("JWT" if looks_jwt else "opaque"))
    note = describe_expiry(token)
    if note:
        print("  expiry: {}".format(note))
    elif looks_jwt:
        print("  expiry: JWT with no readable exp claim")
    else:
        print("  expiry: not stated by the token")

    print("\n=== data endpoints, every header combination ===")
    print("      (v)   (scheme)  (clientid)   status")
    found = probe_token(token, verbose=True)

    print("\n=== summary ===")
    if not found:
        print("  Nothing accepted. The token has most likely expired -")
        print("  fetch a fresh one and set MEURAL_TOKEN again.")
        return

    print("  Working: {}".format(found[0]))
    print("  Headers: {}".format(", ".join(sorted(found[1]))))

    base, headers = found
    print("\n=== record shapes ===")
    print("  What the API actually returns, so a renamed field is obvious.")

    for label, path in (("item", "/user/items"),
                        ("playlist", "/user/galleries")):
        try:
            response = session.get(base + path, params={"count": 1, "page": 1},
                                   headers=headers, timeout=TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            print("\n  {}: could not fetch ({})".format(label, exc))
            continue

        if response.status_code != 200:
            print("\n  {}: HTTP {}".format(label, response.status_code))
            continue

        try:
            rows = response.json().get("data", [])
        except ValueError:
            print("\n  {}: non-JSON response".format(label))
            continue

        if not rows:
            print("\n  {}: none on this account".format(label))
            continue

        record = rows[0]
        print("\n  {} fields: {}".format(label, sorted(record.keys())))
        print("  sample {}:".format(label))
        for line in json.dumps(record, indent=2)[:700].splitlines():
            print("    {}".format(line))


def cmd_list(api, args):
    playlist_table(api)


def cmd_devices(api, args):
    response = api.devices()
    print("GET /user/devices: HTTP {}".format(response.status_code))
    if response.status_code != 200:
        print("Raw response: {}".format(response.text[:400]))
        print("\nThis endpoint is inferred rather than verified. If it is "
              "wrong, find the device id in a browser request to "
              "api.meural.com while viewing your frame’s settings.")
        return
    devices = response.json().get("data", [])
    if not devices:
        print("No frames found on this account.")
        return
    print("\n{:<20} {:<30} {}".format("id", "name", "status"))
    print("-" * 60)
    for device in devices:
        print("{:<20} {:<30} {}".format(
            device.get("id", "?"),
            str(device.get("alias") or device.get("name", "?"))[:30],
            device.get("status", "")))


def cmd_export(api, args):
    items = api.items
    if args.playlist is not None:
        if not 1 <= args.playlist <= len(api.galleries):
            sys.exit("No playlist numbered {}.".format(args.playlist))
        wanted = set(api.galleries[args.playlist - 1].get("itemIds", []))
        items = [item for item in items if item["id"] in wanted]

    filename = "meural_export_{}.json".format(stamp())
    with open(filename, "w") as handle:
        json.dump(items, handle, indent=2)
    print("Metadata for {} item(s) written to {}".format(len(items), filename))

    if not args.download:
        print("Add --download DIR to also fetch the image files.")
        return

    directory = os.path.expanduser(args.download)
    os.makedirs(directory, exist_ok=True)
    missing, saved = 0, 0

    for index, item in enumerate(items, start=1):
        url = image_url(item)
        if not url:
            missing += 1
            continue
        suffix = os.path.splitext(url.split("?")[0])[1] or ".jpg"
        name = "{}_{}{}".format(
            item["id"],
            "".join(c for c in str(item.get("name", "")) if c.isalnum() or c in "-_")[:60],
            suffix)
        target = os.path.join(directory, name)
        if os.path.exists(target):
            saved += 1
            continue
        try:
            response = session.get(url, timeout=60)
            if response.status_code == 200:
                with open(target, "wb") as handle:
                    handle.write(response.content)
                saved += 1
                print("({}/{}) saved {}".format(index, len(items), name))
            else:
                print("({}/{}) HTTP {} for item {}".format(
                    index, len(items), response.status_code, item["id"]))
        except requests.RequestException as exc:
            print("({}/{}) failed for item {}: {}".format(
                index, len(items), item["id"], exc))
        time.sleep(0.5)

    print("\n{} file(s) in {}".format(saved, directory))
    if missing:
        print("{} item(s) had no usable image URL - check the metadata file "
              "for the correct field name.".format(missing))


def cmd_upload(api, args):
    files = collect_files(args.paths)
    playlist_table(api)
    print("\n{} image file(s) found locally.".format(len(files)))

    if not args.allow_duplicates:
        existing = set()
        for item in api.items:
            name = str(item.get("name", "")).lower()
            if name:
                existing.add(name)
        kept = []
        for path in files:
            base = os.path.basename(path).lower()
            stem = os.path.splitext(base)[0]
            if base in existing or stem in existing:
                print("  skipping duplicate: {}".format(os.path.basename(path)))
            else:
                kept.append(path)
        files = kept
        if not files:
            print("\nEverything is already in your library. Nothing to do.")
            return
        print("{} file(s) after removing duplicates.".format(len(files)))

    if args.playlist is None and args.new is None:
        sys.exit("Choose a target with --playlist N or --new NAME.")

    if args.playlist is not None:
        if not 1 <= args.playlist <= len(api.galleries):
            sys.exit("No playlist numbered {}.".format(args.playlist))
        print("\nTarget: {}".format(
            api.galleries[args.playlist - 1].get("name", "(unnamed)")))
    else:
        print("\nTarget: new playlist {!r}".format(args.new))

    if not args.execute:
        print("Dry run. Add --execute to carry this out.")
        return

    gallery_id = (api.create_gallery(args.new) if args.new
                  else api.galleries[args.playlist - 1]["id"])

    done = load_state()
    if done:
        remaining = [p for p in files if os.path.abspath(p) not in done]
        if len(remaining) < len(files):
            print("Resuming: {} file(s) already uploaded in a previous run."
                  .format(len(files) - len(remaining)))
        files = remaining
        if not files:
            print("Nothing left to upload.")
            return

    failures = []
    stranded = []          # uploaded, but playlist membership unconfirmed
    started = time.time()

    for index, path in enumerate(files, start=1):
        name = os.path.basename(path)
        original_mb = os.path.getsize(path) / (1024 * 1024)

        payload = None
        if args.resize:
            payload = prepare_image(path, args.resize, args.quality)

        sent_mb = (len(payload) / (1024 * 1024)) if payload else original_mb
        note = " -> {:.1f} MB".format(sent_mb) if payload else ""
        print("({}/{}) {} ({:.1f} MB{})".format(
            index, len(files), name, original_mb, note))

        clock = time.time()
        item_id = api.upload_item(path, payload_bytes=payload)
        elapsed = time.time() - clock

        if item_id is None:
            failures.append(name)
            time.sleep(WRITE_DELAY)
            continue

        rate = sent_mb / elapsed if elapsed > 0 else 0
        print("    uploaded in {:.1f}s ({:.2f} MB/s)".format(elapsed, rate))

        status = api.add_to_gallery(gallery_id, item_id)

        if status is None:
            # The API never answered. The add may still have been applied, so
            # ask rather than guess.
            print("    item {} -> playlist: no answer, verifying".format(item_id))
            present = api.is_in_gallery(gallery_id, item_id)
            if present is True:
                print("    verified: it went through after all")
                status = 200
            elif present is False:
                print("    verified: not added - retrying once")
                status = api.add_to_gallery(gallery_id, item_id)
            else:
                print("    could not verify; item {} may be orphaned"
                      .format(item_id))
                stranded.append(item_id)

        print("    item {} -> playlist: {}".format(item_id, status))
        if status not in (200, 201, 204):
            failures.append(name)
        else:
            done.add(os.path.abspath(path))
            save_state(done)

        if args.device:
            print("    item {} -> frame: {}".format(
                item_id, api.add_to_device(args.device, item_id)))

        time.sleep(WRITE_DELAY)

    total = time.time() - started
    print("\nFinished in {:.0f}m {:.0f}s.".format(total // 60, total % 60))

    if stranded:
        print("\n{} item(s) uploaded but not confirmed in the playlist: {}"
              .format(len(stranded), stranded[:20]))
        print("Run 'delete --orphans' to see whether they ended up outside "
              "every playlist, and to clear them before uploading again.")

    if failures:
        print("{} file(s) had problems: {}".format(len(failures), failures[:20]))
        print("Re-run the same command to retry only those.")
    else:
        print("{} image(s) uploaded.".format(len(files)))
        if os.path.exists(STATE_FILE) and not stranded:
            os.remove(STATE_FILE)
            print("Resume state cleared.")


def cmd_delete(api, args):
    orphans, _ = playlist_table(api)
    item_ids = {item["id"] for item in api.items}
    targets, target_galleries, label = set(), [], "selection"

    if args.wipe:
        targets = set(item_ids)
        target_galleries = list(api.galleries) if args.drop_playlists else []
        label = "wipe"
    elif args.orphans:
        targets = set(orphans)
        label = "orphans"
    elif args.playlist is not None:
        if not 1 <= args.playlist <= len(api.galleries):
            sys.exit("No playlist numbered {}.".format(args.playlist))
        gallery = api.galleries[args.playlist - 1]
        targets = set(gallery.get("itemIds", [])) & item_ids
        target_galleries = [gallery] if args.drop_playlists else []
        label = "playlist{}".format(args.playlist)
    elif args.select:
        raw = input("\nPlaylists to target (e.g. 1,3 / o / all): ").strip().lower()
        if not raw:
            sys.exit("Nothing selected.")
        if raw == "all":
            targets = set(item_ids)
            target_galleries = list(api.galleries) if args.drop_playlists else []
        else:
            for part in raw.replace(" ", "").split(","):
                if part == "o":
                    targets |= set(orphans)
                    continue
                try:
                    number = int(part)
                except ValueError:
                    sys.exit("Could not parse: {!r}".format(part))
                if not 1 <= number <= len(api.galleries):
                    sys.exit("No playlist numbered {}.".format(number))
                gallery = api.galleries[number - 1]
                targets |= set(gallery.get("itemIds", [])) & item_ids
                if args.drop_playlists:
                    target_galleries.append(gallery)
    else:
        sys.exit("Choose what to delete: --playlist N, --select, --orphans "
                 "or --wipe.")

    if not targets and not target_galleries:
        print("\nNothing targeted.")
        return

    print("\nTargeted: {} item(s)".format(len(targets)), end="")
    print(", plus {} playlist(s)".format(len(target_galleries))
          if target_galleries else "")

    if not args.execute:
        print("Dry run. Add --execute to carry this out.")
        return

    # Manifest first: deletion cannot be undone.
    filename = "meural_manifest_{}_{}.json".format(label, stamp())
    with open(filename, "w") as handle:
        json.dump([i for i in api.items if i["id"] in targets], handle, indent=2)
    print("\nManifest of {} item(s) written to {}".format(len(targets), filename))
    print("It contains image URLs. To keep the pictures, cancel now and run "
          "'export --download DIR' first.")

    print("\nThis permanently deletes {} of your {} uploaded items.".format(
        len(targets), len(item_ids)))
    if targets == item_ids and item_ids:
        print("That is your ENTIRE library.")
    confirm()

    failures = []
    ordered = sorted(targets)
    for index, item_id in enumerate(ordered, start=1):
        status = api.delete_item(item_id)
        print("({}/{}) delete item {}: {}".format(
            index, len(ordered), item_id, status))
        if status == 401:
            sys.exit("\nToken expired mid-run. Fetch a fresh one and re-run; "
                     "items already deleted will not reappear.")
        if status not in (200, 204):
            failures.append(item_id)
        time.sleep(WRITE_DELAY)

    for gallery in target_galleries:
        print("delete playlist {}: {}".format(
            gallery.get("name", gallery["id"]), api.delete_gallery(gallery["id"])))
        time.sleep(WRITE_DELAY)

    if failures:
        print("\n{} item(s) could not be deleted: {}".format(
            len(failures), failures[:20]))
        print("Re-run to retry; successful deletions are not repeated.")
    else:
        print("\nDone.")


# =================================================================== main ==

def build_parser():
    parser = argparse.ArgumentParser(
        prog="meural.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Manage a Netgear Meural library from the command line.",
        epilog="""\
Run without a command to list your playlists.

  export MEURAL_TOKEN='...'                  token from your browser
  meural.py login                            check it and cache it
  meural.py                                  what is in my library?
  meural.py export --download ~/backup       save everything first
  meural.py upload ~/Pictures --new "May" --resize --execute
  meural.py delete --orphans --execute       tidy up

Nothing is changed without --execute. `meural.py <command> --help` has
the detail for each command.""")

    def sub(name, help_text, examples=None):
        parser_kwargs = {"help": help_text}
        if examples:
            parser_kwargs["epilog"] = "Examples:\n" + examples
            parser_kwargs["formatter_class"] = argparse.RawDescriptionHelpFormatter
        return subparsers.add_parser(name, **parser_kwargs)

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "login", help="check MEURAL_TOKEN and cache it")
    subparsers.add_parser("logout", help="forget the saved token")
    subparsers.add_parser("list", help="show playlists and totals")
    subparsers.add_parser("devices", help="show frames on your account")
    subparsers.add_parser("diagnose",
                          help="check the saved token against the API")

    export = sub("export", "download originals and metadata", """\
  meural.py export
      write metadata for the whole library to a timestamped JSON file

  meural.py export --download ~/meural-backup
      the same, plus every image file

  meural.py export --playlist 2 --download ~/holiday
      just the second playlist""")
    export.add_argument("--playlist", type=int, metavar="N",
                        help="restrict to the playlist numbered N in `list`; "
                             "omit to export the whole library")
    export.add_argument("--download", metavar="DIR",
                        help="also fetch the image files into DIR, named "
                             "<item-id>_<name>.<ext>; existing files are "
                             "skipped so an interrupted export resumes")

    upload = sub("upload", "add local images to a playlist", """\
  meural.py upload ~/Pictures/frame --playlist 1
      dry run: what would be uploaded, and what is a duplicate

  meural.py upload ~/Pictures/frame --playlist 1 --execute
      do it

  meural.py upload ~/Pictures/frame --new "Summer 2026" --execute
      create the playlist as part of the same run

  meural.py upload ~/Pictures --resize --execute --playlist 1
      downscale to 2560px first - much faster on large photos

  meural.py upload a.jpg b.jpg --playlist 3 --device 12345 --execute
      two named files, pushed to a frame as well

  meural.py upload ~/Pictures --resize 1920 --quality 82 --execute --playlist 1
      smaller still, for a slow connection""")
    upload.add_argument("paths", nargs="+", help="image files or directories")
    target = upload.add_mutually_exclusive_group()
    target.add_argument("--playlist", type=int, metavar="N",
                        help="add to the playlist numbered N in `list`")
    target.add_argument("--new", metavar="NAME",
                        help="create a playlist called NAME and add to that")
    upload.add_argument("--device", metavar="ID",
                        help="also push each image to this frame so it appears "
                             "straight away; see `devices` for the id")
    upload.add_argument("--resize", type=int, nargs="?", const=2560, metavar="PX",
                        help="downscale to this longest edge before uploading "
                             "(default 2560 if given without a value); needs Pillow")
    upload.add_argument("--quality", type=int, default=90, metavar="Q",
                        help="JPEG quality when resizing (default 90)")
    upload.add_argument("--allow-duplicates", action="store_true",
                        help="upload even when an item of the same name is "
                             "already in the library")
    upload.add_argument("--execute", action="store_true",
                        help="actually upload; without this it only reports "
                             "what it would do")

    delete = sub("delete", "remove items", """\
  meural.py delete --orphans
      dry run: what is sitting outside every playlist

  meural.py delete --orphans --execute
      remove it

  meural.py delete --playlist 3 --execute
      empty one playlist, keeping the playlist itself

  meural.py delete --playlist 3 --drop-playlists --execute
      empty it and remove the playlist too

  meural.py delete --select --execute
      pick playlists at a prompt (1,3 / o / all)

  meural.py delete --wipe --drop-playlists --execute
      start completely fresh""")
    what = delete.add_mutually_exclusive_group()
    what.add_argument("--playlist", type=int, metavar="N",
                      help="delete the items in the playlist numbered N")
    what.add_argument("--select", action="store_true",
                      help="prompt for playlists to target; accepts a list "
                           "such as 1,3, the letter o for orphans, or all")
    what.add_argument("--orphans", action="store_true",
                      help="delete only items that belong to no playlist")
    what.add_argument("--wipe", action="store_true",
                      help="delete every item in the library")
    delete.add_argument("--drop-playlists", action="store_true",
                        help="also delete the targeted playlists themselves, "
                             "not just their contents")
    delete.add_argument("--execute", action="store_true",
                        help="actually delete; without this it only reports "
                             "what it would do. Still asks for confirmation")

    return parser


COMMANDS = {
    "login": cmd_login,
    "logout": cmd_logout,
    "list": cmd_list,
    "devices": cmd_devices,
    "diagnose": cmd_diagnose,
    "export": cmd_export,
    "upload": cmd_upload,
    "delete": cmd_delete,
}


def main():
    args = build_parser().parse_args()

    # logout needs no session, and login forces a fresh one.
    if args.command == "logout":
        removed = clear_stored_token()
        print("Removed: {}".format(", ".join(removed) if removed else "nothing"))
        return

    if args.command == "diagnose":
        cmd_diagnose(None, args)
        return

    api = Meural(force_login=(args.command == "login"))
    print("Connected to {}".format(api.base))
    try:
        COMMANDS[args.command or "list"](api, args)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress has been saved; re-run the same "
              "command to carry on.")
        sys.exit(130)
    except requests.RequestException as exc:
        print("\nNetwork trouble: {}".format(exc))
        print("Progress has been saved; re-run the same command to carry on.")
        sys.exit(1)


if __name__ == "__main__":
    main()
