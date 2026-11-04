#!/usr/bin/env python3
"""Publish BLISS firmware images to GitHub Releases and offer them in manifest.json.

Standard library only (Python 3.8+). Run from anywhere; the hosting repo is the
parent of this script's folder.

  publish   release + promote in one go (the usual path)
  release   create GitHub Release fw-<version> and upload one image per board;
            nothing is offered to the app yet (staging)
  promote   download a release's assets, verify them, add them to manifest.json,
            commit and push
  withdraw  remove a version from manifest.json for some boards (release stays)
  check     validate manifest.json; --download also fetches and verifies every image

Board images are given as BOARD=PATH, where BOARD is the exact DIS Model Number
string the device reports ("3.3", "3.4") and PATH is a west build directory, a
dfu_application.zip or a signed .bin. Every image's MCUboot header must carry
--version, and a build directory's devicetree must set hw-model = BOARD.

Auth: GITHUB_TOKEN or GH_TOKEN, else the github.com login stored in git's
credential helper. The token is sent only to api.github.com / uploads.github.com.
"""

import argparse
import datetime
import glob
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

OWNER = "frazier-zh"
REPO = "bliss_firmware"
BRANCH = "main"
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
RAW_MANIFEST = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}/manifest.json"

HOST_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(HOST_ROOT, "manifest.json")
DEFAULT_SOURCE = os.environ.get(
    "BLISS_FW_SRC", os.path.join(os.path.dirname(HOST_ROOT), "bliss_ble_peripheral"))

MCUBOOT_MAGIC = 0x96F3B83D
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Fail(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def vkey(v):
    return tuple(int(p) for p in v.split("."))


def tag_for(version):
    return f"fw-{version}"


def asset_name(board, version):
    return f"bliss-hw{board}-fw{version}.bin"


def download_url(version, name):
    return f"https://github.com/{OWNER}/{REPO}/releases/download/{tag_for(version)}/{name}"


# ---------------------------------------------------------------- images

def mcuboot_version(data):
    """Version string from an MCUboot image header; raises if not a signed image."""
    if len(data) < 32:
        raise Fail("image too short for an MCUboot header")
    magic, _load, hdr_size, _ptlv, img_size, _flags, major, minor, rev, _build = \
        struct.unpack_from("<IIHHIIBBHI", data, 0)
    if magic != MCUBOOT_MAGIC:
        raise Fail(f"not an MCUboot image (magic 0x{magic:08x}); use the signed .bin")
    if hdr_size + img_size > len(data):
        raise Fail("MCUboot header claims more data than the file holds")
    return f"{major}.{minor}.{rev}"


def from_zip(path):
    with zipfile.ZipFile(path) as z:
        man = json.loads(z.read("manifest.json"))
        apps = [f for f in man.get("files", []) if f.get("type") == "application"
                and str(f.get("image_index", "0")) == "0"]
        if len(apps) != 1:
            raise Fail(f"{path}: expected one application image in manifest.json")
        return z.read(apps[0]["file"])


def load_board_image(board, path):
    """Returns (bytes, provenance note). Checks hw-model when a build dir is given."""
    path = os.path.abspath(path)
    if os.path.isdir(path):
        bins = [p for p in glob.glob(os.path.join(path, "*", "zephyr", "zephyr.signed.bin"))
                if os.path.basename(os.path.dirname(os.path.dirname(p))) != "mcuboot"]
        if len(bins) != 1:
            raise Fail(f"{path}: expected one <app>/zephyr/zephyr.signed.bin, found {len(bins)}")
        dts = os.path.join(os.path.dirname(bins[0]), "zephyr.dts")
        if not os.path.isfile(dts):
            raise Fail(f"{dts} missing; cannot confirm the board revision")
        with open(dts, encoding="utf-8", errors="replace") as f:
            models = re.findall(r'hw-model\s*=\s*"([^"]*)"', f.read())
        if models != [board]:
            raise Fail(f"{path} was built for hw-model {models}, not {board!r}")
        with open(bins[0], "rb") as f:
            return f.read(), f"{bins[0]} (hw-model {board} confirmed)"
    if path.lower().endswith(".zip"):
        return from_zip(path), f"{path} (hw-model not checked)"
    with open(path, "rb") as f:
        return f.read(), f"{path} (hw-model not checked)"


def source_commit(src, allow_dirty):
    if not os.path.isdir(os.path.join(src, ".git")):
        return None
    try:
        head = subprocess.check_output(["git", "-C", src, "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(
            ["git", "-C", src, "status", "--porcelain", "--untracked-files=no"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    if dirty and not allow_dirty:
        raise Fail(f"{src} has uncommitted changes; commit, rebuild, or pass --allow-dirty")
    return head + ("-dirty" if dirty else "")


# ---------------------------------------------------------------- GitHub

_token = None


def token():
    global _token
    if _token:
        return _token
    _token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not _token:
        env = dict(os.environ, GCM_INTERACTIVE="never", GIT_TERMINAL_PROMPT="0")
        try:
            out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                                 capture_output=True, text=True, env=env, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        for line in out.splitlines():
            if line.startswith("password="):
                _token = line[len("password="):]
    if not _token:
        raise Fail("no GitHub token: set GITHUB_TOKEN or log in once with git (git credential manager)")
    return _token


def api(method, url, body=None, data=None, ctype=None, ok=(200, 201, 204)):
    if not url.startswith("http"):
        url = API + url
    headers = {"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "bliss-publish-firmware"}
    if body is not None:
        data = json.dumps(body).encode()
        ctype = "application/json"
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    if status not in ok:
        raise Fail(f"{method} {url} -> HTTP {status}: {raw[:400].decode(errors='replace')}")
    return status, (json.loads(raw) if raw else None)


def fetch_public(url):
    """Unauthenticated GET (follows GitHub's redirect to its object store)."""
    req = urllib.request.Request(url, headers={"User-Agent": "bliss-publish-firmware",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def get_release(version):
    status, rel = api("GET", f"/repos/{OWNER}/{REPO}/releases/tags/{tag_for(version)}", ok=(200, 404))
    return rel if status == 200 else None


def asset_sha(asset):
    d = asset.get("digest") or ""
    return d[len("sha256:"):] if d.startswith("sha256:") else None


# ---------------------------------------------------------------- manifest

def read_manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def write_manifest(m):
    m["updated"] = datetime.date.today().isoformat()
    for b in m["hardware"].values():
        b["releases"].sort(key=lambda r: vkey(r["version"]), reverse=True)
    m["hardware"] = dict(sorted(m["hardware"].items(), key=lambda kv: kv[0]))
    with open(MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
        f.write("\n")


def validate_manifest(m):
    errs = []
    if m.get("schema") != 1:
        errs.append("schema must be 1")
    if not isinstance(m.get("hardware"), dict):
        return errs + ["hardware must be an object"]
    for board, b in m["hardware"].items():
        rels = b.get("releases") if isinstance(b, dict) else None
        if not isinstance(rels, list):
            errs.append(f"{board}: releases must be a list")
            continue
        seen = set()
        for r in rels:
            where = f"{board} {r.get('version')}"
            if not VERSION_RE.match(str(r.get("version", ""))):
                errs.append(f"{where}: version must be major.minor.patch")
            if r.get("version") in seen:
                errs.append(f"{where}: listed twice")
            seen.add(r.get("version"))
            if not str(r.get("url", "")).startswith("https://"):
                errs.append(f"{where}: url must be https")
            if not SHA_RE.match(str(r.get("sha256", ""))):
                errs.append(f"{where}: sha256 must be 64 lowercase hex")
            if not isinstance(r.get("size"), int) or r["size"] <= 0:
                errs.append(f"{where}: size must be a positive integer")
            for k in ("min_app", "min_from"):
                if k in r and not VERSION_RE.match(str(r[k])):
                    errs.append(f"{where}: {k} must be major.minor.patch")
            if "released" in r and not DATE_RE.match(str(r["released"])):
                errs.append(f"{where}: released must be YYYY-MM-DD")
    return errs


def git(*args, capture=False):
    cmd = ["git", "-C", HOST_ROOT, *args]
    if capture:
        return subprocess.check_output(cmd, text=True).strip()
    subprocess.check_call(cmd)


def manifest_txn(message, mutate, push, dry_run):
    """Pull, apply mutate(manifest), validate, write, commit, push."""
    if git("status", "--porcelain", "--", "manifest.json", capture=True):
        raise Fail("manifest.json has local edits; commit or discard them first")
    if not dry_run:
        git("pull", "--ff-only", "--quiet", "origin", BRANCH)
    m = read_manifest()
    mutate(m)
    errs = validate_manifest(m)
    if errs:
        raise Fail("manifest would be invalid:\n  " + "\n  ".join(errs))
    if dry_run:
        log("dry run, manifest would become:\n" + json.dumps(m, indent=2))
        return
    write_manifest(m)
    if not git("status", "--porcelain", "--", "manifest.json", capture=True):
        log("manifest.json unchanged")
        return
    git("add", "manifest.json")
    git("commit", "--quiet", "-m", message)
    if push:
        git("push", "--quiet", "origin", BRANCH)
        log(f"pushed; the app sees it within ~5 min at {RAW_MANIFEST}")
    else:
        log("committed locally, not pushed (--no-push)")


# ---------------------------------------------------------------- commands

def parse_boards(items, need_path):
    out = {}
    for item in items or []:
        board, sep, path = item.partition("=")
        board = board.strip()
        if not board or (need_path and not sep):
            raise Fail(f"--board {item!r}: expected BOARD=PATH" if need_path else f"bad --board {item!r}")
        if board in out:
            raise Fail(f"board {board} given twice")
        out[board] = path.strip()
    if not out:
        raise Fail("give at least one --board")
    return out


def cmd_release(a):
    if not VERSION_RE.match(a.version):
        raise Fail("--version must be major.minor.patch")
    boards = parse_boards(a.board, need_path=True)
    commit = source_commit(a.source, a.allow_dirty)

    images = {}  # board -> (name, bytes, sha)
    by_sha = {}
    for board, path in boards.items():
        data, note = load_board_image(board, path)
        ver = mcuboot_version(data)
        if ver != a.version:
            raise Fail(f"hw {board}: image header says {ver}, not {a.version}; rebuild after bumping VERSION")
        sha = hashlib.sha256(data).hexdigest()
        name = by_sha.get(sha) or asset_name(board, a.version)
        by_sha.setdefault(sha, name)
        images[board] = (name, data, sha)
        log(f"hw {board}: {len(data)} bytes sha256 {sha} <- {note}")

    rows = "\n".join(f"| {b} | `{n}` | {len(d)} | `{s}` |" for b, (n, d, s) in images.items())
    body = (f"{a.notes or ''}\n\n| Hardware | Asset | Bytes | SHA-256 |\n|---|---|---|---|\n{rows}\n\n"
            f"Signed MCUboot application images for OTA (SMP image upload)."
            + (f"\nBuilt from `{OWNER}/bliss_ble_peripheral@{commit}`." if commit else "")
            + "\nOffered to the app only once listed in `manifest.json`.").strip()

    if a.dry_run:
        log(f"dry run: would create {tag_for(a.version)} with " + ", ".join(sorted({n for n, _, _ in images.values()})))
        return images

    rel = get_release(a.version)
    if rel is None:
        _, rel = api("POST", f"/repos/{OWNER}/{REPO}/releases", body={
            "tag_name": tag_for(a.version), "target_commitish": BRANCH,
            "name": f"Firmware {a.version}", "body": body, "draft": False, "prerelease": False})
        log(f"created release {rel['html_url']}")
    else:
        log(f"release {tag_for(a.version)} exists, adding missing assets")

    existing = {x["name"]: x for x in rel.get("assets", [])}
    for name, data, sha in {n: (n, d, s) for n, d, s in images.values()}.values():
        if name in existing:
            have = asset_sha(existing[name]) or hashlib.sha256(
                fetch_public(existing[name]["browser_download_url"])).hexdigest()
            if have != sha:
                raise Fail(f"{name} already published with sha256 {have}; a released version is "
                           f"immutable, bump the version instead")
            log(f"{name} already uploaded, identical")
            continue
        up = rel["upload_url"].split("{")[0] + "?name=" + urllib.parse.quote(name)
        api("POST", up, data=data, ctype="application/octet-stream")
        log(f"uploaded {name}")
    return images


def verify_remote(url, size, sha, version):
    data = fetch_public(url)
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
        raise Fail(f"{url}: downloaded file does not match (size {len(data)})")
    if mcuboot_version(data) != version:
        raise Fail(f"{url}: image header version is not {version}")


def entries_for(a, images):
    released = a.released or datetime.date.today().isoformat()
    out = {}
    for board, (name, data, sha) in images.items():
        e = {"version": a.version, "url": download_url(a.version, name), "sha256": sha,
             "size": len(data), "released": released}
        if a.min_app:
            e["min_app"] = a.min_app
        if a.min_from:
            e["min_from"] = a.min_from
        if a.notes:
            e["notes"] = a.notes
        out[board] = e
    return out


def apply_entries(entries, replace):
    def mutate(m):
        for board, e in entries.items():
            rels = m["hardware"].setdefault(board, {"releases": []})["releases"]
            old = [r for r in rels if r["version"] == e["version"]]
            if old and old[0]["sha256"] != e["sha256"] and not replace:
                raise Fail(f"hw {board} {e['version']} is already listed with another image")
            rels[:] = [r for r in rels if r["version"] != e["version"]] + [e]
    return mutate


def cmd_promote(a, images=None):
    if images is None:  # promote an already published release
        boards = parse_boards(a.board, need_path=False)
        rel = get_release(a.version)
        if rel is None:
            raise Fail(f"no release {tag_for(a.version)}")
        assets = {x["name"]: x for x in rel["assets"]}
        images = {}
        for board in boards:
            name = asset_name(board, a.version)
            if name not in assets:
                raise Fail(f"release has no {name}; assets: {', '.join(assets) or 'none'}")
            data = fetch_public(assets[name]["browser_download_url"])
            images[board] = (name, data, hashlib.sha256(data).hexdigest())
    entries = entries_for(a, images)
    if a.dry_run:
        log("dry run: skipping download check")
    else:
        for board, e in entries.items():
            verify_remote(e["url"], e["size"], e["sha256"], e["version"])
            log(f"hw {board}: {e['url']} downloads and verifies")
    boards = ", ".join(sorted(entries))
    manifest_txn(f"Offer firmware {a.version} for hw {boards}", apply_entries(entries, a.replace),
                 not a.no_push, a.dry_run)


def cmd_publish(a):
    images = cmd_release(a)
    cmd_promote(a, images)


def cmd_withdraw(a):
    boards = parse_boards(a.board, need_path=False)

    def mutate(m):
        for board in boards:
            rels = m["hardware"].get(board, {}).get("releases", [])
            if not any(r["version"] == a.version for r in rels):
                raise Fail(f"hw {board} does not list {a.version}")
            rels[:] = [r for r in rels if r["version"] != a.version]
    manifest_txn(f"Withdraw firmware {a.version} for hw {', '.join(sorted(boards))}", mutate,
                 not a.no_push, a.dry_run)


def cmd_check(a):
    m = json.loads(fetch_public(RAW_MANIFEST)) if a.remote else read_manifest()
    errs = validate_manifest(m)
    for e in errs:
        log(f"ERROR {e}")
    if not errs and a.download:
        for board, b in m["hardware"].items():
            for r in b["releases"]:
                try:
                    verify_remote(r["url"], r["size"], r["sha256"], r["version"])
                    log(f"ok  hw {board} {r['version']}")
                except (Fail, urllib.error.URLError) as e:
                    errs.append(str(e))
                    log(f"ERROR hw {board} {r['version']}: {e}")
    if errs:
        raise Fail(f"{len(errs)} problem(s)")
    n = sum(len(b["releases"]) for b in m["hardware"].values())
    log(f"manifest OK ({'remote' if a.remote else 'local'}): {len(m['hardware'])} board(s), {n} entr(y/ies)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, paths):
        sp.add_argument("--version", required=True, help="firmware version, major.minor.patch")
        sp.add_argument("--board", action="append", metavar="BOARD=PATH" if paths else "BOARD",
                        help="repeat per hardware model")
        sp.add_argument("--dry-run", action="store_true", help="change nothing")

    def offer(sp):
        sp.add_argument("--notes", help="one-line release notes shown in the app")
        sp.add_argument("--min-app", help="lowest app versionName that can run this firmware")
        sp.add_argument("--min-from", help="lowest installed firmware this image may go over")
        sp.add_argument("--released", help="YYYY-MM-DD, default today")
        sp.add_argument("--replace", action="store_true", help="overwrite a manifest entry for the same version")
        sp.add_argument("--no-push", action="store_true", help="commit manifest.json but do not push")

    def build(sp):
        sp.add_argument("--source", default=DEFAULT_SOURCE, help="firmware source repo, for the commit id")
        sp.add_argument("--allow-dirty", action="store_true", help="accept a source tree with local edits")

    sp = sub.add_parser("publish", help="release + promote")
    common(sp, True); offer(sp); build(sp)
    sp.set_defaults(fn=cmd_publish)
    sp = sub.add_parser("release", help="upload images only (staged)")
    common(sp, True); build(sp)
    sp.add_argument("--notes", help="release notes")
    sp.set_defaults(fn=cmd_release)
    sp = sub.add_parser("promote", help="offer an uploaded release in manifest.json")
    common(sp, False); offer(sp)
    sp.set_defaults(fn=cmd_promote)
    sp = sub.add_parser("withdraw", help="stop offering a version")
    common(sp, False)
    sp.add_argument("--no-push", action="store_true")
    sp.set_defaults(fn=cmd_withdraw)
    sp = sub.add_parser("check", help="validate manifest.json")
    sp.add_argument("--remote", action="store_true", help="check the published raw manifest instead of the local file")
    sp.add_argument("--download", action="store_true", help="also download and verify every image")
    sp.set_defaults(fn=cmd_check)

    a = p.parse_args()
    try:
        a.fn(a)
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"error: {' '.join(e.cmd)} failed ({e.returncode})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
