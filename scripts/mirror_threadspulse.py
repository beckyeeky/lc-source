#!/usr/bin/env python3
"""Mirror only verified ThreadsPulse sideload artifacts. Requires authenticated gh.
Read-only by default; --apply publishes a Release and requests the existing source workflow.
No credentials are copied to CI, and no R2 resources are used.
"""
import argparse
import hashlib
import json
import base64
import fcntl
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import zipfile

UPSTREAM = "mumuisdog/ThreadsPulse"
DEST = "beckyeeky/lc-source"
MAX_BYTES = 1024 ** 3
ARTIFACT = re.compile(r"^Threads_[0-9.]+_Pulse_[0-9.]+_sideload$")


def gh(*args, output=None):
    result = subprocess.run(["gh", *args], check=True, stdout=output or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=300)
    return result.stdout.decode() if output is None else None


def api(path):
    return json.loads(gh("api", path))


def select_artifact(items):
    eligible = [a for a in items if not a.get("expired") and ARTIFACT.fullmatch(a["name"])
                and 0 < a["size_in_bytes"] <= MAX_BYTES]
    if not eligible:
        return None
    # The workflow must produce exactly one sideload variant; don't silently pick a fork variant.
    if len(eligible) != 1:
        raise ValueError("Ambiguous sideload artifacts")
    return eligible[0]


def marker_matches(release, artifact_id):
    return not release.get("draft") and bool(re.search(
        rf"(?m)^Artifact: {artifact_id}\s*$", release.get("body") or ""))


def validate_ipa(path):
    path = Path(path)
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("Invalid IPA size")
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > 4 * MAX_BYTES:
            raise ValueError("Excessive expanded IPA size")
        if z.testzip() is not None:
            raise ValueError("IPA CRC failed")
        plists = [n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
        if len(plists) != 1:
            raise ValueError("Expected exactly one top-level application")
        info = plistlib.loads(z.read(plists[0]))
        if info.get("CFBundleIdentifier") != "com.burbn.barcelona":
            raise ValueError("Unexpected bundle identifier")
        if not info.get("CFBundleShortVersionString") or not info.get("MinimumOSVersion"):
            raise ValueError("Missing app version or minimum OS")
        extensions = [n for n in z.namelist() if re.fullmatch(
            r"Payload/[^/]+\.app/PlugIns/[^/]+\.appex/Info\.plist", n)]
        if not extensions:
            raise ValueError("Sideload artifact has no extensions")
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(data)
    return {"version": info["CFBundleShortVersionString"],
            "minOSVersion": info["MinimumOSVersion"], "size": path.stat().st_size,
            "sha256": digest.hexdigest(), "extensions": len(extensions)}


def ensure_source(asset_url, apply):
    data = api(f"repos/{DEST}/contents/source.json?ref=main")
    source = json.loads(base64.b64decode(data["content"]))
    present = any(v.get("downloadURL") == asset_url for a in source.get("apps", [])
                  for v in a.get("versions", []))
    if not present and apply:
        gh("workflow", "run", "update-source.yml", "--repo", DEST, "--ref", "main")
    return "present" if present else "update_requested" if apply else "missing"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    runs = api(f"repos/{UPSTREAM}/actions/workflows/build-ipa.yml/runs?status=success&branch=main&per_page=20")["workflow_runs"]
    # Never roll back to an older run when the newest run has no usable artifact.
    runs = [r for r in runs if r.get("conclusion") == "success" and r.get("head_branch") == "main"
            and r.get("head_repository", {}).get("full_name") == UPSTREAM]
    if not runs:
        print(json.dumps({"status": "no_successful_run"})); return
    run = max(runs, key=lambda r: (r["run_number"], r.get("run_attempt", 1)))
    artifacts = api(f"repos/{UPSTREAM}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    artifact = select_artifact(artifacts)
    if artifact is None:
        print(json.dumps({"status": "no_available_sideload", "run": run["id"]})); return
    releases = json.loads(gh("api", "--paginate", "--slurp", f"repos/{DEST}/releases?per_page=100"))
    releases = [r for page in releases for r in page]
    for release in releases:
        if marker_matches(release, artifact["id"]):
            assets = [a for a in release["assets"] if a["name"] == artifact["name"] + ".ipa"]
            if len(assets) != 1 or assets[0]["size"] <= 0:
                raise ValueError("Published marker has missing/mismatched asset")
            print(json.dumps({"status": "already_mirrored", "artifact": artifact["id"],
                              "release": release["html_url"],
                              "source": ensure_source(assets[0]["browser_download_url"], args.apply)})); return
    if not args.apply:
        print(json.dumps({"status": "new_artifact", "artifact": artifact["id"], "name": artifact["name"]})); return
    with tempfile.TemporaryDirectory(prefix="threadspulse-") as temp:
        temp = Path(temp)
        archive = temp / "artifact.zip"
        with archive.open("wb") as f:
            gh("api", f"repos/{UPSTREAM}/actions/artifacts/{artifact['id']}/zip", output=f)
        if archive.stat().st_size != artifact["size_in_bytes"]:
            raise ValueError("Artifact download size mismatch")
        ipa = temp / (artifact["name"] + ".ipa")
        with zipfile.ZipFile(archive) as z:
            names = [n for n in z.namelist() if n.endswith(".ipa")]
            if len(names) != 1 or Path(names[0]).name != ipa.name:
                raise ValueError("Unexpected artifact IPA layout")
            if z.getinfo(names[0]).file_size > MAX_BYTES:
                raise ValueError("IPA too large")
            import shutil
            with z.open(names[0]) as src, ipa.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        metadata = validate_ipa(ipa)
        if artifact["name"].split("_")[1] != metadata["version"]:
            raise ValueError("Artifact name and app version differ")
        tag = f"threadspulse-artifact-{artifact['id']}"
        existing = next((r for r in releases if r["tag_name"] == tag), None)
        if existing and not existing["draft"]:
            raise ValueError("Published release tag already exists without matching marker")
        notes = temp / "notes.md"
        notes.write_text(f"Unmodified sideload artifact from {run['html_url']}\n"
                         f"Artifact: {artifact['id']}\n"
                         f"SHA-256: {metadata['sha256']}\n"
                         f"Minimum iOS: {metadata['minOSVersion']}\n"
                         "Includes extensions. Not device-tested. No noplugins variant.\n")
        if not existing:
            gh("release", "create", tag, "--repo", DEST, "--target", "main", "--draft",
               "--title", artifact["name"], "--notes-file", str(notes))
        gh("release", "upload", tag, str(ipa), "--repo", DEST, "--clobber")
        verify = temp / "verify"
        verify.mkdir()
        gh("release", "download", tag, "--repo", DEST, "--pattern", ipa.name, "--dir", str(verify))
        if validate_ipa(verify / ipa.name)["sha256"] != metadata["sha256"]:
            raise ValueError("Uploaded asset hash differs; keeping draft and old source")
        gh("release", "edit", tag, "--repo", DEST, "--notes-file", str(notes), "--draft=false", "--latest=false")
        gh("workflow", "run", "update-source.yml", "--repo", DEST, "--ref", "main")
        print(json.dumps({"status": "published", "artifact": artifact["id"], "tag": tag, **metadata}))


if __name__ == "__main__":
    with open(Path(tempfile.gettempdir()) / "threadspulse-mirror.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
