# MeuralManager

Manage your Netgear Meural library from the command line: inspect playlists, delete their contents selectively, or wipe the library entirely. Netgear’s own app and web interface make bulk deletion tedious, and deleting a playlist leaves its items behind, silently consuming your upload allowance. This script fixes that.

## Authentification

Netgear moved Meural logins behind their group SSO, which is protected by bot-detection middleware, so scripted login is impractical. Supply a session token taken from your browser:

  1. Log in at `https://my.meural.netgear.com` in Chrome or Firefox.
  2. Open dev tools, Network tab, and click around so API calls fire.
  3. Select any request to `api.meural.com` and read its request headers.
  4. Copy the Authorization value, dropping the leading "Token ".
  5. export `MEURAL_TOKEN='<the value you copied>'`

Tokens are short-lived. If the script stops partway through, fetch a fresh one and re-run - deletion is idempotent, so completed work is not repeated.

## Usage

    python3 meural_manager.py                      # list playlists, delete nothing
    python3 meural_manager.py --select             # choose playlists interactively
    python3 meural_manager.py --orphans            # target items in no playlist
    python3 meural_manager.py --wipe               # target the entire library

Any of the above is a dry run. Add `--delete` to actually remove things, which also requires typing a confirmation. Add `--drop-playlists` to remove the emptied playlists themselves as well as their contents.

Before deleting anything, full metadata for the targeted items, including image URLs, is written to a manifest file. Deletion cannot be undone, so download anything you want to keep first.
