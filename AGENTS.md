# Agent guide: publishing BLISS firmware

This repository hosts over-the-air images for the BLISS wearable. The Android
app reads `manifest.json` on `main` and offers what it lists. Anything you
commit to `manifest.json` reaches users' devices within about 5 minutes, so
follow this guide exactly and stop to ask the user when a check fails.

## Rules that must hold

1. **Hardware model and firmware version are independent.** A board is named by
   its DIS Model Number string (`"3.3"`, `"3.4"`), which comes from
   `zephyr,user/hw-model` in the board overlay of the source repo. A firmware
   version is a plain `major.minor.patch`. Never derive one from the other, and
   never list an image under a board it was not built for.
2. **Binaries are release assets, never commits.** The branch holds only
   `manifest.json`, docs and `tools/`.
3. **A published version is immutable.** Never replace or delete an asset of an
   existing release `fw-X.Y.Z`. A wrong build gets a new patch version. The
   script refuses to overwrite an asset with different bytes; do not work
   around it.
4. **The manifest is the offer switch.** A release that is not in
   `manifest.json` is offered to nobody. Adding an entry is the step that
   reaches users; get the user's go-ahead for it unless they already asked you
   to publish that version.
5. **Images must be signed with the key the deployed MCUboot trusts**
   (`keys/bliss-dev-ecdsa-p256.pem` in the source repo, not in git). The
   script checks the MCUboot header and version; it cannot check the key. Build
   with the source repo's own sysbuild config, which points at that key.
6. **Only the tool edits `manifest.json`.** If you must edit it by hand, run
   `check` afterwards and keep the format in `README.md`.

## Inputs you need

| Input | Where it comes from |
|---|---|
| Version `X.Y.Z` | `VERSION` in `bliss_ble_peripheral` (`VERSION_MAJOR.VERSION_MINOR.PATCHLEVEL`). The script compares it with each image's MCUboot header. |
| Boards | Each board revision built for this version. Today: `3.3` → `build/hw_3_3` (`bliss@3.3.0/nrf52832`), `3.4` → `build/hw_3_4` (`bliss@3.4.0/nrf52832`). `3.1` is deprecated and has no MCUboot; never list it. |
| `--min-from` | Lowest installed firmware the image may be flashed over. `3.4.0` unless the changelog's header for this version says otherwise (3.4.0 is the first firmware with MCUboot and the SMP service; older devices cannot be updated over the air at all). |
| `--min-app` | Lowest Android app `versionName` that can talk to this firmware. Read the version's section in the source repo's `changelog.md` ("A 0.2.9 app works unchanged; 0.2.10 reads ...") and the app's `BleConstants.MAX_SUPPORTED_FIRMWARE`. If the firmware changes the wire protocol in a way older apps cannot handle, set the app version that handles it. When unsure, ask the user. |
| `--notes` | One plain line from the changelog title, e.g. `Link margin, a drain that never blocks, a link report`. |

## Procedure

Paths below are for the user's Windows machine (`D:\data\bliss_ble_peripheral`
next to `D:\data\bliss_firmware`). Python: any 3.8+, standard library only; on
that machine `C:/ncs/toolchains/fd21892d0f/opt/bin/python.exe -I` always works.

1. **Source is committed.** `git -C ../bliss_ble_peripheral status` must be
   clean and `VERSION` must already hold the new version. The script records
   the source commit in the release and refuses a dirty tree.

2. **Build every board pristine.** `west` is not on PATH; run it from the NCS
   workspace `D:/ncs/v3.2.4` with the toolchain environment of
   `C:/ncs/toolchains/fd21892d0f` (see that repo's build notes):

   ```bash
   west build -p always -b bliss@3.4.0/nrf52832 --sysbuild -s D:/data/bliss_ble_peripheral -d D:/data/bliss_ble_peripheral/build/hw_3_4 -- -DBOARD_ROOT=D:/data/bliss_ble_peripheral
   ```

   Same with `bliss@3.3.0/nrf52832` and `build/hw_3_3`. About 4–5 minutes each;
   both can run in parallel. `build/<hw>/dfu_application.zip_manifest.json`
   must show `"version_MCUBOOT": "X.Y.Z+0"`.

3. **Dry run**, from this repository's root:

   ```bash
   python tools/publish_firmware.py publish --dry-run --version X.Y.Z --board 3.3=D:/data/bliss_ble_peripheral/build/hw_3_3 --board 3.4=D:/data/bliss_ble_peripheral/build/hw_3_4 --min-app A.B.C --min-from 3.4.0 --notes "..."
   ```

   It checks for each board that the build directory's devicetree says
   `hw-model = "<board>"`, that the image is a signed MCUboot image whose
   header version is `X.Y.Z`, and prints size and SHA-256, plus the manifest it
   would write.

4. **Publish**: the same command without `--dry-run`. It creates release
   `fw-X.Y.Z` (tag on `main`), uploads `bliss-hw<board>-fwX.Y.Z.bin` per board
   (one shared asset if two boards built byte-identical images), downloads each
   public URL and re-verifies size, hash and header, then adds one entry per
   board to `manifest.json`, commits `Offer firmware X.Y.Z for hw ...` and
   pushes to `main`.

   To stage without offering: `release` with the same arguments (no `--min-*`),
   then later `promote --version X.Y.Z --board 3.3 --board 3.4 --min-app ...
   --min-from ... --notes ...`.

5. **Verify what the app sees** (raw CDN cache, wait ~5 minutes):

   ```bash
   python tools/publish_firmware.py check --remote --download
   ```

   Report to the user: the release URL, each board's size and SHA-256, and the
   `check` result.

## Fixing mistakes

- Bad build already offered: `withdraw --version X.Y.Z --board <hw>` (pushes
  immediately), then publish a fixed `X.Y.Z+1`. Do not delete the release.
- Wrong `min_app` / `min_from` / `notes` on an entry: re-run `promote` for that
  version with corrected options and `--replace`.
- Upload failed half way: re-run the same `publish`; it reuses the release,
  skips identical assets and uploads the rest.
- `manifest.json has local edits`: someone edited it by hand; inspect
  `git diff manifest.json` and ask the user before discarding.

## Authentication

The script uses `GITHUB_TOKEN` / `GH_TOKEN`, else the github.com login stored in
git's credential manager (the account `frazier-zh`). It needs permission to
create releases and push to this repository. Never print, log or commit the
token. `gh` is not installed on the user's machine and is not needed.

## How the app uses the manifest

For reference when choosing `min_app` / `min_from` (implemented in the app's
`FirmwareUpdatePlanner`): look up the device's hardware model; keep entries
with `version` above the installed one, `min_app` at or below the app version
and `min_from` at or below the installed version; drop anything above the
app's `MAX_SUPPORTED_FIRMWARE` (and tell the user to update the app); offer the
highest remaining. The manifest is re-read at most every hour, or when the user
presses "Check for update".
