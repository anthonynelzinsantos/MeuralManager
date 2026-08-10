# MeuralManager

**Manage your Netgear Meural library from the command line**

The Meural app and web interface make bulk work painful. There’s no way to delete many items at once, deleting a playlist silently leaves its contents behind, and uploads are slow and unreliable. The script fixes all three problems.

## Installation

MeuralManager requires Python 3.6 or later.

```bash
pip install requests          # required
pip install Pillow            # optional: enables --resize
```

## Getting started

```bash
export MEURAL_TOKEN='...'     # token from your browser — see below
python3 meural.py login       # verify it and cache it
python3 meural.py             # see what you have
```

The token comes from your browser session. Run any command without `MEURAL_TOKEN` set and the script prints where to find it. See [How authentication works](#how-authentication-works) for why a username and password won’t do.

## Commands

Nothing that changes your library happens without `--execute`. Every destructive or write command runs as a dry run by default and tells you exactly what it would do. In addition, deletions require typing `DELETE` at the prompt.

### login / logout

```bash
python3 meural.py login
python3 meural.py logout
```

Neither takes flags. `login` verifies the token in `MEURAL_TOKEN`, caches it, reports how long it is good for, and prints your playlists. `logout` removes the cached token. It works without a network connection.

### list

```bash
python3 meural.py list
python3 meural.py             # list is the default
```

No flags. Prints the numbered playlist table used by every other command:

```
  #  Playlist                                   items     yours
---------------------------------------------------------------
  1  Holiday 2025                                  84        84
  2  Black and white                               31        31
---------------------------------------------------------------
  o  (items in no playlist)                        12        12

Library: 127 uploaded items across 2 playlists
```

`items` is everything the playlist references, including Meural's own artwork. `yours` counts only your uploads. The `o` row is items belonging to no playlist. If any item appears in more than one playlist, a warning says so, because deleting one playlist’s contents removes those items everywhere.

The numbers in the first column are what `--playlist N` refers to throughout.

### devices

```bash
python3 meural.py devices
```

No flags. Lists the frames on your account with their ids, for use with `upload --device`.

### diagnose

```bash
python3 meural.py diagnose
```

No flags. Reports:

- the token’s length, shape (JWT or opaque) and expiry;
- every combination of API version, auth scheme and the `X-ClientId` header, with the status returned for each;
- once a working combination is found, the field names and a sample record for both an item and a playlist.

It reveals no secrets, so the output is safe to share.

This is what to run when things stop working. The token section separates an expired token from a changed API, and the record shapes show whether the API’s responses still look the way the script expects, which is how you’d confirm a field has been renamed.

### export

Save your library before you change anything.

| Flag | Meaning |
|---|---|
| `--playlist N` | Restrict to playlist `N`. Omit for the whole library. |
| `--download DIR` | Also fetch the image files into `DIR`. Without it, only metadata is written. |

```bash
# metadata only, to a timestamped JSON file
python3 meural.py export

# metadata plus every image
python3 meural.py export --download ~/meural-backup

# one playlist
python3 meural.py export --playlist 2 --download ~/holiday
```

Files are named `<item-id>_<name>.<ext>`. Existing files are skipped, so an interrupted export resumes if you run it again. If items report no usable image URL, the count is shown at the end — run `diagnose` to see the real field names. `export` needs no `--execute` as it only reads.

### upload

| Flag | Meaning |
|---|---|
| `--playlist N` | Add to playlist `N`. Mutually exclusive with `--new`. |
| `--new NAME` | Create a playlist called `NAME` and add to that. |
| `--device ID` | Also push each image to a frame, so it appears immediately. |
| `--resize [PX]` | Downscale to `PX` on the longest edge first. Defaults to 2560 if given without a value. Needs Pillow. |
| `--quality Q` | JPEG quality when resizing. Default 90. |
| `--allow-duplicates` | Upload even when an item of the same name already exists. |
| `--execute` | Actually upload. Without it, nothing is sent. |

Positional arguments are files, directories, or a mixture. Directories are scanned one level deep for `.jpg .jpeg .png .gif .bmp .tif .tiff .webp`.

```bash
# what would happen?
python3 meural.py upload ~/Pictures/frame --playlist 1

# do it
python3 meural.py upload ~/Pictures/frame --playlist 1 --execute

# create the playlist in the same run
python3 meural.py upload ~/Pictures/frame --new "Summer 2026" --execute

# downscale first
python3 meural.py upload ~/Pictures --playlist 1 --resize --execute

# smaller still
python3 meural.py upload ~/Pictures --playlist 1 --resize 1920 --quality 82 --execute

# named files, pushed to a frame as well
python3 meural.py upload a.jpg b.jpg --playlist 3 --device 12345 --execute
```

**Resize.** The Canvas is a 1920×1080 px panel, so a 24 Mpx original is many times larger than anything it can display, and the server resizes each upload on receipt. Downscaling locally cuts both the transfer and that server-side work. It is the single biggest speed improvement available. EXIF orientation is applied, so rotated phone photos arrive the right way up, and files already under the threshold are sent untouched.

**Resume.** Successful uploads are recorded in `.meural_upload_state.json` in the working directory as the run proceeds. If a run is interrupted, re-running the same command continues from where it stopped. The file is removed on a clean finish.

**Duplicate detection.** Compares your filenames against the `name` field of existing items. This is a reasonable guess at how Meural derives names, not a certainty. Use `--allow-duplicates` if it skips things it shouldn’t.

### delete

Exactly one target flag is required.

| Flag | Meaning |
|---|---|
| `--playlist N` | Delete the items in playlist `N`. |
| `--select` | Prompt for playlists. Accepts `1,3`, `o` for orphans, or `all`. |
| `--orphans` | Delete only items belonging to no playlist. |
| `--wipe` | Delete every item in the library. |
| `--drop-playlists` | Also delete the targeted playlists themselves, not just their contents. |
| `--execute` | Actually delete. Still prompts for confirmation. |

```bash
# what is sitting outside every playlist?
python3 meural.py delete --orphans

# remove it
python3 meural.py delete --orphans --execute

# empty one playlist, keeping the playlist
python3 meural.py delete --playlist 3 --execute

# empty it and remove the playlist too
python3 meural.py delete --playlist 3 --drop-playlists --execute

# pick interactively
python3 meural.py delete --select --execute

# start completely fresh
python3 meural.py delete --wipe --drop-playlists --execute
```

**Safeguards.** Before the first deletion, full metadata for every targeted item — including image URLs — is written to
`meural_manifest_<label>_<timestamp>.json`. You then have to type `DELETE` to proceed, and `--wipe` says in as many words that it is taking the entire library. The script also refuses outright if you have uploads but zero gallery items, since that would mark everything for deletion off the back of a bad read.

**Deletion is permanent.** Run `export --download` first if the pictures exist nowhere else.

## Common workflows

**Reclaim space without losing anything**

```bash
python3 meural.py delete --orphans
python3 meural.py delete --orphans --execute
```

**Back up, then start fresh**

```bash
python3 meural.py export --download ~/meural-backup
python3 meural.py delete --wipe --drop-playlists --execute
```

**Seasonal refresh**

```bash
python3 meural.py delete --playlist 2 --execute
python3 meural.py upload ~/Pictures/autumn --playlist 2 --resize --execute
```

**Move a folder onto the frame immediately**

```bash
python3 meural.py devices
python3 meural.py upload ~/Pictures/new --new "Latest" --device 12345 --resize --execute
```

**Scripted, non-interactive**

```bash
export MEURAL_TOKEN='...'
python3 meural.py upload /srv/photos --playlist 1 --resize --execute
```

## How authentication works

The token comes from your browser and is passed in the environment. Run any command without it set and the script prints these steps:

1. Log in at `my.meural.netgear.com`
2. Open the developer tools (F12, or Cmd-Option-I on a Mac)
3. Go to the Network tab, then click around the site so requests appear
4. Click any request whose domain is `api.meural.com`
5. Under Request Headers, find the line beginning `authorization:`
6. Copy what follows the word `Token`

```bash
export MEURAL_TOKEN='...'
python3 meural.py login
```

The variable lasts for the current shell session, so it needs setting again in a new terminal window or when the token expires.

### Why not a username and password?

Meural has two login paths:

- `api.meural.com/authenticate` takes a username and password, but authenticates against an older Meural-native user directory. For a Netgear account it answers 200 with a token that every data endpoint then refuses. This is the trap most older Meural scripts fall into, and it surfaces as a confusing “Invalid token” much further down the line.
- `accounts.netgear.com` is where the app and web portal actually sign in. It sits behind bot-detection middleware that signs each login request with headers generated by obfuscated in-page JavaScript. There is no honest way to mint one of those from a script.

### Expiry

The token is verified against a real endpoint before being cached, so a wrong value fails immediately rather than three minutes into an upload.

If the token is a JWT, its own `exp` claim is read and honoured, and `login` reports how long you have. Otherwise a three-hour cache is assumed. Either way a `401` is the final authority.

Check the remaining time before starting a large upload. If a token expires part-way through, the command stops cleanly and keeps its progress. Set a fresh `MEURAL_TOKEN` and run the same command again. Completed work is skipped.

## Files written

| Path | Contents |
|---|---|
| `~/.meural-manager/token.json` | Cached API token. Mode 0600. |
| `./.meural_upload_state.json` | Upload resume state. Removed on clean completion. |
| `./meural_manifest_*.json` | Metadata for items about to be deleted. |
| `./meural_export_*.json` | Metadata from `export`. |

`logout` removes the first.

## Tuning

Constants near the top of the script, worth changing when the API misbehaves.

| Constant | Default | Purpose |
|---|---|---|
| `WRITE_DELAY` | `3.0` | Seconds between writes. Raise to 8–10 if you see constant retries. |
| `READ_DELAY` | `1.0` | Seconds between paginated reads. |
| `TIMEOUT` | `(10, 30)` | Connect and read timeouts for ordinary calls. |
| `UPLOAD_TIMEOUT` | `(10, 180)` | Longer read timeout: the server resizes before replying. |
| `MEMBER_TIMEOUT` | `(10, 60)` | Playlist membership calls. |
| `RETRY_ATTEMPTS` | `4` | Attempts per call. |
| `BACKOFF_BASE` | `4.0` | Seconds before the first retry; doubles each time. |
| `UPLOAD_ATTEMPTS` | `3` | Attempts per file upload. |
| `PAGE_SIZE` | `100` | Items per page. The API rejects larger values. |
| `TOKEN_MAX_AGE` | `10800` | Seconds before an opaque cached token is considered stale. A JWT’s own expiry wins over this. |

If retries fire constantly rather than occasionally, the backend is struggling and the answer is to slow down, not push harder.

## Troubleshooting

**`MEURAL_TOKEN is not set`.** Export it first. Mind the quotes: a token can contain characters your shell would otherwise interpret.

**`The token in MEURAL_TOKEN was not accepted`.** It has expired, or it came from a request to `accounts.netgear.com` rather than `api.meural.com`. Copy a request to the API host specifically.

**`Saved token has expired`.** Not an error. Fetch a fresh token and set `MEURAL_TOKEN` again.

**Uploads time out repeatedly.** Try `--resize`, then raise `WRITE_DELAY`.

**`N item(s) uploaded but not confirmed in the playlist`.** The upload succeeded but the playlist call could not be verified, so those items may be orphaned. Run `delete --orphans` to check, clear them, and upload again.

**`Nothing to do` from `delete --orphans`.** Every item genuinely is in a playlist. Nothing is wrong; there is simply nothing to reclaim.

**Duplicates skipped that shouldn't be.** Pass `--allow-duplicates`.

**Everything fails after working yesterday.** Run `diagnose`. It distinguishes an expired token from a changed API.

## Caveats

Meural publishes no supported public API, so parts of this are inferred from observed behaviour and third-party integrations. Specifically:

- Item upload, playlist membership and deletion are attested by working third-party code.
- Playlist creation and the device listing are inferred from REST conventions. Both print their raw response so you can see what actually came back.
- The image-URL field used by `export --download` is guessed from several plausible names; `diagnose` shows the real ones.

Netgear may change any of this without notice. If something breaks, `diagnose` is the first place to look.

An item can belong to several playlists. Deleting a playlist’s items deletes those items outright — they disappear from the other playlists too. `list` warns when this applies to you.
