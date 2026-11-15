#!/usr/bin/env python3
"""Generate a separate SideStore feed from real IPA metadata (stdlib only).
Keeps the existing LiveContainer feed unchanged. Only canonical non-alias apps
are selected, with one latest version to avoid duplicate version/build IDs.
"""
import copy
import hashlib
import json
from pathlib import Path
import plistlib
import re
import struct
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 1024 ** 3


def entitlement_keys(data):
    """Read XML entitlements in thin/universal Mach-O code signatures, without executing code."""
    magic = data[:4]
    fat = {b"\xca\xfe\xba\xbe": (">", 20, False), b"\xbe\xba\xfe\xca": ("<", 20, False),
           b"\xca\xfe\xba\xbf": (">", 32, True), b"\xbf\xba\xfe\xca": ("<", 32, True)}
    if magic in fat:
        endian, stride, wide = fat[magic]
        count = struct.unpack_from(endian + "I", data, 4)[0]
        if count > 32: raise ValueError("Too many Mach-O slices")
        result = set()
        for i in range(count):
            pos = 8 + i * stride + 8
            off, size = struct.unpack_from(endian + ("QQ" if wide else "II"), data, pos)
            if off + size > len(data): raise ValueError("Invalid Mach-O slice")
            result.update(entitlement_keys(data[off:off+size]))
        return result
    thin = {b"\xcf\xfa\xed\xfe": ("<", 32), b"\xce\xfa\xed\xfe": ("<", 28),
            b"\xfe\xed\xfa\xcf": (">", 32), b"\xfe\xed\xfa\xce": (">", 28)}
    if magic not in thin: raise ValueError("App executable is not a supported Mach-O")
    endian, pos = thin[magic]
    count = struct.unpack_from(endian + "I", data, 16)[0]
    if count > 10000: raise ValueError("Too many load commands")
    for _ in range(count):
        cmd, size = struct.unpack_from(endian + "II", data, pos)
        if size < 8 or pos + size > len(data): raise ValueError("Invalid load command")
        if cmd == 0x1d:
            off, length = struct.unpack_from(endian + "II", data, pos + 8)
            blob = data[off:off+length]
            magic, total, slots = struct.unpack_from(">III", blob)
            if magic != 0xfade0cc0 or total > len(blob) or slots > 1000:
                raise ValueError("Invalid code signature")
            der_present = False
            for i in range(slots):
                _, entry = struct.unpack_from(">II", blob, 12 + i * 8)
                kind, length = struct.unpack_from(">II", blob, entry)
                if entry + length > len(blob): raise ValueError("Invalid signature entry")
                if kind == 0xfade7171:
                    ent = plistlib.loads(blob[entry+8:entry+length])
                    if not isinstance(ent, dict): raise ValueError("Invalid entitlements plist")
                    return set(ent)
                if kind == 0xfade7172: der_present = True
            if der_present: raise ValueError("DER-only entitlements need manual review")
            return set()
        pos += size
    return set()  # unsigned executable has no embedded entitlements


def inspect_ipa(path):
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > LIMIT * 4:
            raise ValueError("Expanded IPA too large")
        if z.testzip() is not None: raise ValueError("IPA CRC failed")
        main = [n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
        if len(main) != 1: raise ValueError("Expected exactly one main app")
        info = plistlib.loads(z.read(main[0]))
        for key in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion", "MinimumOSVersion"):
            if not info.get(key): raise ValueError("Missing " + key)
        plists = main + [n for n in z.namelist() if re.fullmatch(
            r"Payload/[^/]+\.app/PlugIns/[^/]+\.appex/Info\.plist", n)]
        entitlements, privacy = set(), {}
        for name in plists:
            p = plistlib.loads(z.read(name))
            executable = p.get("CFBundleExecutable")
            if not executable or "/" in executable: raise ValueError("Invalid executable name")
            entitlements.update(entitlement_keys(z.read(name.rsplit("/", 1)[0] + "/" + executable)))
            for key, value in p.items():
                if key.startswith("NS") and "UsageDescription" in key:
                    privacy.setdefault(key, value if isinstance(value, str) else str(value))
        # Export an actual app PNG only when the source has no usable icon.
        folder = main[0].rsplit("/", 1)[0] + "/"
        icons = [n for n in z.namelist() if n.startswith(folder) and "/" not in n[len(folder):]
                 and re.search(r"(?i)(appicon|icon).*\.png$", n[len(folder):])]
        icon = z.read(max(icons, key=lambda n: z.getinfo(n).file_size)) if icons else None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""): digest.update(chunk)
    return {"bundleIdentifier": info["CFBundleIdentifier"], "version": str(info["CFBundleShortVersionString"]),
            "buildVersion": str(info["CFBundleVersion"]), "minOSVersion": info["MinimumOSVersion"],
            "size": Path(path).stat().st_size, "sha256": digest.hexdigest(),
            "appPermissions": {"entitlements": sorted(entitlements), "privacy": privacy}}, icon


def metadata(url, expected_size, expected_sha=None):
    request = urllib.request.Request(url, headers={"User-Agent": "lc-source-sidestore-generator"})
    with tempfile.TemporaryDirectory(prefix="sidestore-ipa-") as d:
        path = Path(d) / "app.ipa"
        size = 0
        with urllib.request.urlopen(request, timeout=180) as response, path.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > LIMIT: raise ValueError("IPA too large")
                out.write(chunk)
        if size != expected_size: raise ValueError("Downloaded size mismatch")
        result, icon = inspect_ipa(path)
        if expected_sha and result["sha256"] != expected_sha: raise ValueError("Downloaded SHA256 mismatch")
        return result, icon


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def generate(root=ROOT):
    source = json.loads((root / "source.json").read_text())
    config = json.loads((root / "sources.json").read_text())
    # Match the configured alias entries exactly, not arbitrary suffixes in real IDs.
    alias_ids = {a["bundleIdentifier"] + a["bundleIdSuffix"] for a in config["apps"]
                 if a.get("bundleIdentifier") and a.get("bundleIdSuffix")}
    alias_names = {a["name"] for a in config["apps"] if a.get("bundleIdSuffix")}
    cache_path = root / "scripts/sidestore-cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    previous_path = root / "sidestore.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {"apps": []}
    previous_by_id = {a["bundleIdentifier"]: a for a in previous["apps"]}
    apps, seen_ids = [], set()
    for item in source["apps"]:
        if item["name"] in alias_names or item["bundleIdentifier"] in alias_ids:
            print("Skip LiveContainer alias:", item["name"]); continue
        if not item.get("versions"): continue
        version = item["versions"][0]
        url = version["downloadURL"]
        try:
            if url not in cache:
                print("Inspect real IPA:", item["name"], flush=True)
                entry, icon = metadata(url, version["size"], version.get("sha256"))
                if entry["bundleIdentifier"] != item["bundleIdentifier"]:
                    raise ValueError("Source ID differs from actual IPA")
                if icon:
                    (root / "icons").mkdir(exist_ok=True)
                    (root / "icons" / (entry["bundleIdentifier"] + ".png")).write_bytes(icon)
                    entry["iconURL"] = "https://raw.githubusercontent.com/beckyeeky/lc-source/main/icons/" + entry["bundleIdentifier"] + ".png"
                cache[url] = entry
            entry = cache[url]
            if entry["size"] != version["size"]: raise ValueError("Cached size differs")
            if version.get("sha256") and entry["sha256"] != version["sha256"]:
                raise ValueError("Cached SHA256 differs")
            app = copy.deepcopy(item)
            app["bundleIdentifier"] = entry["bundleIdentifier"]
            app["appPermissions"] = entry["appPermissions"]
            if not app.get("iconURL"):
                if not entry.get("iconURL"): raise ValueError("No usable app icon")
                app["iconURL"] = entry["iconURL"]
            v = copy.deepcopy(version)
            for k in ("version", "buildVersion", "minOSVersion", "size", "sha256"): v[k] = entry[k]
            app["versions"] = [v]
            app.update(version=v["version"], versionDate=v["date"], downloadURL=url, size=v["size"])
        except Exception as error:
            print("WARNING: retaining previous SideStore app:", item["name"], str(error), file=sys.stderr)
            app = previous_by_id.get(item["bundleIdentifier"])
            if app is None: continue
        if app["bundleIdentifier"] in seen_ids: raise ValueError("Duplicate real bundle identifier")
        seen_ids.add(app["bundleIdentifier"])
        apps.append(app)
    if not apps: raise ValueError("Refusing to publish an empty SideStore source")
    result = {"name": "My SideStore Source", "identifier": "io.github.beckyeeky.sidestore",
              "subtitle": "SideStore 独立源 · 正式版 · 真实 IPA 元数据",
              "websiteURL": "https://github.com/beckyeeky/lc-source", "apps": apps, "news": []}
    write_json(cache_path, cache)
    write_json(root / "sidestore.json", result)
    print("SideStore source generated:", len(apps), "apps")


if __name__ == "__main__": generate()
