#!/usr/bin/env python3
"""
Manage a Netgear Meural library from the command line: inspect playlists, and
delete their contents selectively or wipe the library entirely.

Netgear’s own app and web interface make bulk deletion tedious, and deleting a
playlist leaves its items behind, silently consuming your upload allowance.

AUTHENTICATION
--------------
Netgear moved Meural logins behind their group SSO, which is protected by
bot-detection middleware, so scripted login is impractical. Supply a session
token taken from your browser:

  1. Log in at https://my.meural.netgear.com in Chrome or Firefox.
  2. Open dev tools, Network tab, and click around so API calls fire.
  3. Select any request to api.meural.com and read its request headers.
  4. Copy the Authorization value, dropping the leading "Token ".
  5. export MEURAL_TOKEN='<the value you copied>'

Tokens are short-lived. If the script stops partway through, fetch a fresh one
and re-run - deletion is idempotent, so completed work is not repeated.

USAGE
-----
    python3 meural_manager.py                      # list playlists, delete nothing
    python3 meural_manager.py --select             # choose playlists interactively
    python3 meural_manager.py --orphans            # target items in no playlist
    python3 meural_manager.py --wipe               # target the entire library

Any of the above is a dry run. Add --delete to actually remove things, which
also requires typing a confirmation. Add --drop-playlists to remove the emptied
playlists themselves as well as their contents.

Before deleting anything, full metadata for the targeted items, including
image URLs, is written to a manifest file. Deletion cannot be undone, so
download anything you want to keep first.
"""

import argparse
import collections
import datetime
import getpass
import json
import os
import requests
import sys
import time

API_HOST = "https://api.meural.com"
API_VERSIONS = ("v0", "v1")
AUTH_SCHEMES = ("Token", "Bearer")
PAGE_SIZE = 100
REQUEST_DELAY = 1.0
DELETE_DELAY = 3.0

session = requests.Session()

# ---------------------------------------------------------------- Connection

def get_token():
    token = os.environ.get("MEURAL_TOKEN")
    if not token:
        print(__doc__.split("AUTHENTICATION")[1].split("USAGE")[0])
        token = getpass.getpass("Paste your Meural token: ")
    token = token.strip()
    if token.lower().startswith("token "):
        token = token[6:].strip()
    if not token:
        sys.exit("A token is required.")
    return token

def resolve_endpoint(token):
    """Find the API version and auth scheme this token works with."""
    last_status = None
    for version in API_VERSIONS:
        for scheme in AUTH_SCHEMES:
            base = "{}/{}".format(API_HOST, version)
            header = "{} {}".format(scheme, token)
            response = session.get(base + "/user/items",
                                   params={"count": 1, "page": 1},
                                   headers={"Authorization": header})
            last_status = response.status_code
            if response.status_code == 200:
                return base, header
    sys.exit("No working endpoint (last status {}). The token has probably "
             "expired - fetch a fresh one from your browser.".format(last_status))

def walk(base, path, authorization):
    """Yield every row from a paginated endpoint."""
    page = 1
    while True:
        response = session.get(base + path,
                               params={"count": PAGE_SIZE, "page": page},
                               headers={"Authorization": authorization})
        if response.status_code == 401:
            sys.exit("Token expired. Fetch a fresh one and re-run.")
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
        time.sleep(REQUEST_DELAY)

# -------------------------------------------------------------------- Report

def describe(items, galleries):
    """Print a table of playlists and how they cover the library."""
    item_ids = {item["id"] for item in items}
    membership = collections.Counter()
    for gallery in galleries:
        for item_id in gallery.get("itemIds", []):
            membership[item_id] += 1

    orphans = sorted(item_ids - set(membership))
    shared = [i for i in item_ids if membership[i] > 1]

    print("\n{:>3}  {:<40} {:>7} {:>9}".format("#", "Playlist", "items", "yours"))
    print("-" * 63)
    for index, gallery in enumerate(galleries, start=1):
        gallery_ids = set(gallery.get("itemIds", []))
        print("{:>3}  {:<40} {:>7} {:>9}".format(
            index,
            str(gallery.get("name", "(unnamed)"))[:40],
            len(gallery_ids),
            len(gallery_ids & item_ids)))
    print("-" * 63)
    print("{:>3}  {:<40} {:>7} {:>9}".format(
        "o", "(items in no playlist)", len(orphans), len(orphans)))
    print("\nLibrary: {} uploaded items".format(len(item_ids)))
    if shared:
        print("Note: {} item(s) appear in more than one playlist. Deleting one "
              "playlist's items removes them from the others too.".format(len(shared)))
    return orphans, membership

# ------------------------------------------------------------------ Deletion

def write_manifest(items, target_ids, label):
    """Dump full metadata for the targeted items before anything is deleted."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = "meural_manifest_{}_{}.json".format(label, stamp)
    targeted = [item for item in items if item["id"] in target_ids]
    with open(filename, "w") as handle:
        json.dump(targeted, handle, indent=2)
    print("Manifest of {} item(s) written to {}".format(len(targeted), filename))
    print("It contains image URLs - download anything you want to keep now.")
    return filename

def delete_items(base, authorization, item_ids):
    headers = {"Authorization": authorization,
               "Content-Type": "application/json",
               "Accept": "*/*"}
    failures = []
    total = len(item_ids)

    for index, item_id in enumerate(item_ids, start=1):
        try:
            response = session.delete("{}/items/{}".format(base, item_id),
                                      headers=headers)
            print("({}/{}) delete item {}: {}".format(
                index, total, item_id, response.status_code))
            if response.status_code == 401:
                sys.exit("\nToken expired mid-run. Fetch a fresh one and re-run; "
                         "items already deleted will not reappear.")
            if response.status_code not in (200, 204):
                failures.append(item_id)
            time.sleep(DELETE_DELAY)
        except requests.RequestException as exc:
            print("({}/{}) delete item {} failed: {}".format(
                index, total, item_id, exc))
            failures.append(item_id)
            time.sleep(10)

    return failures

def delete_galleries(base, authorization, gallery_ids):
    headers = {"Authorization": authorization,
               "Content-Type": "application/json",
               "Accept": "*/*"}
    failures = []
    for gallery_id in gallery_ids:
        response = session.delete("{}/galleries/{}".format(base, gallery_id),
                                  headers=headers)
        print("delete playlist {}: {}".format(gallery_id, response.status_code))
        if response.status_code not in (200, 204):
            failures.append(gallery_id)
        time.sleep(DELETE_DELAY)
    return failures

# ---------------------------------------------------------------- Selection

def choose_galleries(galleries):
    """Prompt for playlist numbers, 'o' for orphans, or 'all'."""
    raw = input("\nPlaylists to target (e.g. 1,3,5 / o / all): ").strip().lower()
    if not raw:
        sys.exit("Nothing selected.")
    if raw == "all":
        return list(range(len(galleries))), True

    chosen, want_orphans = [], False
    for part in raw.replace(" ", "").split(","):
        if part == "o":
            want_orphans = True
            continue
        try:
            number = int(part)
        except ValueError:
            sys.exit("Could not parse selection: {!r}".format(part))
        if not 1 <= number <= len(galleries):
            sys.exit("No playlist numbered {}.".format(number))
        chosen.append(number - 1)
    return chosen, want_orphans

# --------------------------------------------------------------------- Main

def main():
    parser = argparse.ArgumentParser(
        description="Inspect and prune a Netgear Meural library.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--select", action="store_true",
                      help="choose playlists interactively")
    mode.add_argument("--orphans", action="store_true",
                      help="target items belonging to no playlist")
    mode.add_argument("--wipe", action="store_true",
                      help="target every item in the library")
    parser.add_argument("--drop-playlists", action="store_true",
                        help="also delete the targeted playlists themselves")
    parser.add_argument("--delete", action="store_true",
                        help="actually delete (default is a dry run)")
    args = parser.parse_args()

    base, authorization = resolve_endpoint(get_token())
    print("Connected to {}".format(base))

    items = list(walk(base, "/user/items", authorization))
    galleries = list(walk(base, "/user/galleries", authorization))
    orphans, _ = describe(items, galleries)

    item_ids = {item["id"] for item in items}
    target_ids, target_galleries, label = set(), [], "selection"

    if args.wipe:
        target_ids = set(item_ids)
        target_galleries = galleries if args.drop_playlists else []
        label = "wipe"
    elif args.orphans:
        target_ids = set(orphans)
        label = "orphans"
    elif args.select:
        chosen, want_orphans = choose_galleries(galleries)
        for index in chosen:
            gallery = galleries[index]
            target_ids |= set(gallery.get("itemIds", [])) & item_ids
            target_galleries.append(gallery)
        if want_orphans:
            target_ids |= set(orphans)
        if not args.drop_playlists:
            target_galleries = []
    else:
        print("\nListing only. Use --select, --orphans or --wipe to target items.")
        return

    if not target_ids and not target_galleries:
        print("\nNothing targeted.")
        return

    print("\nTargeted: {} item(s)".format(len(target_ids)), end="")
    if target_galleries:
        print(", plus {} playlist(s)".format(len(target_galleries)))
    else:
        print()

    if not args.delete:
        print("Dry run. Add --delete to carry this out.")
        return

    write_manifest(items, target_ids, label)

    print("\nThis permanently deletes {} of your {} uploaded items.".format(
        len(target_ids), len(item_ids)))
    if len(target_ids) == len(item_ids) and item_ids:
        print("That is your ENTIRE library.")
    if input("Type DELETE to confirm: ").strip() != "DELETE":
        sys.exit("Cancelled.")

    failures = delete_items(base, authorization, sorted(target_ids))

    if target_galleries:
        delete_galleries(base, authorization,
                         [g["id"] for g in target_galleries])

    if failures:
        print("\n{} item(s) could not be deleted: {}".format(
            len(failures), failures[:20]))
        print("Re-run to retry; successful deletions are not repeated.")
    else:
        print("\nDone.")

if __name__ == "__main__":
    main()