#!/usr/bin/env python3
"""Generate/update an AltStore/SideStore/LiveContainer source from GitHub Releases. stdlib only.
Usage: python3 gen_source.py sources.json source.json   (env GITHUB_TOKEN optional)"""
import urllib.parse, json, os, re, sys, struct, zlib, hashlib, plistlib, urllib.request

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

def req(url, headers=None, api=False):
    h = {"User-Agent": "ipa-source-gen"}
    if api:
        h["Accept"] = "application/vnd.github+json"
        if TOKEN: h["Authorization"] = f"Bearer {TOKEN}"
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120)

def api(path):
    with req("https://api.github.com" + path, api=True) as r:
        return json.load(r)

def rng(url, start, end):
    with req(url, {"Range": f"bytes={start}-{end}"}) as r:
        if r.status != 206: raise RuntimeError("server ignores Range")
        return r.read()

def remote_info_plist(url, size):
    """Read Payload/*.app/Info.plist via HTTP Range, without downloading the IPA."""
    tail = rng(url, max(0, size - 65558), size - 1)
    i = tail.rfind(b"PK\x05\x06")
    cd_size, cd_off = struct.unpack("<II", tail[i+12:i+20])
    if cd_off == 0xFFFFFFFF:  # zip64
        j = tail.rfind(b"PK\x06\x06")
        cd_size, cd_off = struct.unpack("<QQ", tail[j+40:j+56])
    cd = rng(url, cd_off, cd_off + cd_size - 1)
    p = 0
    while p < len(cd) and cd[p:p+4] == b"PK\x01\x02":
        meth, = struct.unpack("<H", cd[p+10:p+12])
        csize, usize = struct.unpack("<II", cd[p+20:p+28])
        nl, el, cl = struct.unpack("<HHH", cd[p+28:p+34])
        off, = struct.unpack("<I", cd[p+42:p+46])
        name = cd[p+46:p+46+nl].decode("utf-8", "replace")
        extra = cd[p+46+nl:p+46+nl+el]
        if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name):
            if 0xFFFFFFFF in (csize, off):  # zip64 extra field
                q = 0
                while q < len(extra):
                    hid, hl = struct.unpack("<HH", extra[q:q+4]); body = extra[q+4:q+4+hl]; k = 0
                    if hid == 1:
                        if usize == 0xFFFFFFFF: k += 8
                        if csize == 0xFFFFFFFF: csize, = struct.unpack("<Q", body[k:k+8]); k += 8
                        if off == 0xFFFFFFFF: off, = struct.unpack("<Q", body[k:k+8])
                    q += 4 + hl
            lh = rng(url, off, off + 29)
            ln, le = struct.unpack("<HH", lh[26:30])
            data = rng(url, off + 30 + ln + le, off + 30 + ln + le + csize - 1)
            if meth == 8: data = zlib.decompress(data, -15)
            return plistlib.loads(data)
        p += 46 + nl + el + cl
    raise RuntimeError("Info.plist not found")

def sha256_of(url):
    h = hashlib.sha256()
    with req(url) as r:
        for chunk in iter(lambda: r.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

def main(cfg_path, out_path):
    cfg = json.load(open(cfg_path))
    old = {}
    if os.path.exists(out_path):
        for a in json.load(open(out_path)).get("apps", []): old[a.get("bundleIdentifier")] = a
    maxv = cfg.get("maxVersions", 5)
    apps = []
    for ac in cfg["apps"]:
        rels = api(f"/repos/{ac['repo']}/releases?per_page={ac.get('scanReleases', 15)}")
        new_versions, bid = [], ac.get("bundleIdentifier")
        sfx = ac.get("bundleIdSuffix", "")
        if bid: bid += sfx
        for rel in rels:
            if rel["draft"]: continue
            pre = ac.get("prerelease")  # True=only prereleases, False=only stable, None=use includePrerelease
            tagname = rel["tag_name"] + " " + (rel.get("name") or "")
            is_beta = rel["prerelease"] or bool(ac.get("betaRegex") and re.search(ac["betaRegex"], tagname, re.I))
            if pre is True and not is_beta: continue
            if pre is False and is_beta: continue
            if pre is None and rel["prerelease"] and not ac.get("includePrerelease"): continue
            if ac.get("tagRegex") and not re.search(ac["tagRegex"], tagname, re.I): continue
            if ac.get("tagExcludeRegex") and re.search(ac["tagExcludeRegex"], tagname, re.I): continue
            asset = next((x for x in rel["assets"] if re.search(ac["assetRegex"], x["name"])), None)
            if not asset: continue
            url = asset["browser_download_url"]
            prev = old.get(bid) if bid else None
            if len(new_versions) >= maxv: break
            if prev and any(v["downloadURL"] == url for v in prev.get("versions", [])):
                continue  # already known, skip network
            # resolve redirect so Range requests hit the CDN directly
            with req(url, {"Range": "bytes=0-0"}) as r: final = r.geturl()
            info = remote_info_plist(final, asset["size"])
            bid = bid or info["CFBundleIdentifier"] + sfx
            v_real_bid = info["CFBundleIdentifier"]
            v = {"version": info.get("CFBundleShortVersionString", "0"),
                 "buildVersion": str(info.get("CFBundleVersion", "")),
                 "date": rel["published_at"], "downloadURL": url, "size": asset["size"],
                 "minOSVersion": info.get("MinimumOSVersion", ""),
                 "localizedDescription": (rel.get("body") or rel["name"] or "").strip()[:2000]}
            if ac.get("sha256"): v["sha256"] = sha256_of(url)
            new_versions.append(v)
            print(f"  + {ac['repo']} {rel['tag_name']} -> {v['version']} ({bid})", file=sys.stderr)
        if not bid: print(f"  ! {ac['repo']}: no matching asset", file=sys.stderr); continue
        prevv = old.get(bid, {}).get("versions", [])
        seen, merged = set(), []
        for v in sorted(new_versions + prevv, key=lambda v: v["date"], reverse=True):
            if v["downloadURL"] in seen: continue
            seen.add(v["downloadURL"]); merged.append(v)
        merged = merged[:maxv]
        if ac.get("versionFrom") == "tag":  # version = release tag (leading v stripped), recomputed for existing items too
            for v in merged:
                tag = urllib.parse.unquote(v["downloadURL"].split("/releases/download/")[1].split("/")[0])
                v["version"] = re.sub(r"^[vV](?=\d)", "", tag)
        app = {k: v for k, v in ac.items() if k not in ("repo", "assetRegex", "includePrerelease", "sha256", "scanReleases", "prerelease", "betaRegex", "tagRegex", "tagExcludeRegex", "bundleIdSuffix", "versionFrom")}
        app["bundleIdentifier"] = bid
        app["versions"] = merged
        app.setdefault("appPermissions", {"entitlements": [], "privacy": {}})
        if merged:  # legacy top-level fields for old clients
            L = merged[0]
            app.update(version=L["version"], versionDate=L["date"], downloadURL=L["downloadURL"], size=L["size"])
        apps.append(app)
    src = {k: v for k, v in cfg.items() if k not in ("apps", "maxVersions")}
    src["apps"] = apps; src.setdefault("news", [])
    with open(out_path, "w") as f: json.dump(src, f, ensure_ascii=False, indent=2); f.write("\n")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sources.json", sys.argv[2] if len(sys.argv) > 2 else "source.json")
