# BLISS firmware hosting

Over-the-air images for the BLISS wearable, read by the BLISS Android app
(0.2.11 and later). Source code lives in
[`bliss_ble_peripheral`](https://github.com/frazier-zh/bliss_ble_peripheral);
this repository only hosts signed builds and says which board runs which build.

## What is here

| Path | Purpose |
|---|---|
| `manifest.json` | The only file the app reads from this branch. Lists, per hardware model, the firmware the app may offer. |
| Releases `fw-<version>` | One GitHub Release per firmware version; one signed MCUboot image per board as assets. Binaries are never committed. |
| `tools/publish_firmware.py` | Uploads images, updates the manifest, verifies everything. |
| `AGENTS.md` | The same procedure written for automated agents, with the rules it must not break. |

The app fetches
`https://raw.githubusercontent.com/frazier-zh/bliss_firmware/main/manifest.json`
and downloads from
`https://github.com/frazier-zh/bliss_firmware/releases/download/fw-<version>/bliss-hw<hw>-fw<version>.bin`.

## Hardware and firmware are independent

The device reports two strings over Device Information: its hardware model
(`"3.3"`, `"3.4"`) and its firmware version (`major.minor.patch`). Neither says
anything about the other. The manifest is the only place that says "board X
runs firmware Y": the app looks the hardware model up as a key and offers the
highest listed version it can use. A version listed under one board is never
offered to another.

## Manifest format (schema 1)

```json
{
  "schema": 1,
  "updated": "2026-11-04",
  "hardware": {
    "3.4": {
      "releases": [
        {
          "version": "3.4.1",
          "url": "https://github.com/frazier-zh/bliss_firmware/releases/download/fw-3.4.1/bliss-hw3.4-fw3.4.1.bin",
          "sha256": "64 lowercase hex",
          "size": 255275,
          "released": "2026-11-04",
          "min_app": "0.2.10",
          "min_from": "3.4.0",
          "notes": "One line shown in the app"
        }
      ]
    }
  }
}
```

| Field | Required | Meaning |
|---|---|---|
| `hardware` keys | yes | Exact DIS Model Number string the device reports. |
| `version` | yes | Firmware `major.minor.patch`. The app orders by number only. |
| `url` | yes | `https` download URL of the signed image. |
| `sha256`, `size` | yes | Of that file; the app refuses a download that does not match. |
| `released` | no | `YYYY-MM-DD`. |
| `min_app` | no | Lowest app `versionName` that can talk to this firmware. |
| `min_from` | no | Lowest *installed* firmware this image may be applied over. |
| `notes` | no | One line of release notes. |

Unknown fields are ignored; any `schema` other than 1 makes the app ignore the
whole manifest. A malformed entry is skipped on its own.

## Release checklist

1. In `bliss_ble_peripheral`: bump `VERSION`, update `changelog.md`, commit.
2. Build every board pristine (`west build -p always ... --sysbuild`), one build
   directory per board (`build/hw_3_3`, `build/hw_3_4`).
3. Publish (creates release `fw-X.Y.Z`, uploads `bliss-hw<hw>-fwX.Y.Z.bin` per
   board, checks the public download, adds the entries to `manifest.json`,
   commits and pushes):

   ```bash
   python tools/publish_firmware.py publish --version X.Y.Z \
     --board 3.3=../bliss_ble_peripheral/build/hw_3_3 \
     --board 3.4=../bliss_ble_peripheral/build/hw_3_4 \
     --min-app 0.2.11 --min-from 3.4.0 --notes "One line for the app"
   ```

4. `python tools/publish_firmware.py check --remote --download` after ~5 minutes
   (the raw CDN cache) to confirm what the app now sees.

To stage a build without offering it, use `release` instead of `publish`, then
`promote` (same options, `--board 3.3 --board 3.4` without paths) when ready.
`withdraw --version X.Y.Z --board 3.4` stops offering a bad build; the release
stays so devices already on it keep a reference.

Doing it by hand is the same steps: `sha256sum` each image, create the release,
upload the assets with the names above, add one entry per board to
`manifest.json`, commit to `main`.
