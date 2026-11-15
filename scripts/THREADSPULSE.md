# ThreadsPulse sideload mirror

`mirror_threadspulse.py` is a standard-library-only script for an already authenticated GitHub CLI on the bot computer. It does not copy tokens into GitHub Actions and never uses R2.

- Read-only check: `python3 scripts/mirror_threadspulse.py`
- Authorized mirror: `python3 scripts/mirror_threadspulse.py --apply`
- Tests: `python3 -m unittest discover -s tests -v`

A Prime Bot routine invokes the approved local copy approximately every six hours (00:00, 06:00, 12:00, 18:00 Asia/Shanghai). This is not a GitHub Actions schedule: the existing `update-source.yml` still generates the subscription after a mirror is published.

Only the newest successful main-branch build of `mumuisdog/ThreadsPulse/build-ipa.yml` is considered. Expired artifacts and noplugins are excluded. Artifact IDs recorded in Release bodies are the durable deduplication key; the initial manual Release is recognized. No newer available artifact means no mutation.

For a new artifact the script downloads only its sideload ZIP, checks its IPA layout/CRC, actual bundle identifier, version, minimum OS, and presence of extensions, uploads to a draft Release, then downloads it again and checks SHA-256 before publishing. Validation failures leave the old published source intact; an incomplete draft is reusable. A process lock prevents overlapping local invocations.

After publishing, the existing source workflow is requested. A published artifact absent from the subscription is repaired by requesting that workflow again on a later run. Notification must wait until the workflow succeeds and the subscription contains the asset. No-change runs are silent. Authentication failures require attention in a private chat; do not start device login in a group. The current app is not device-tested, and these checks do not prove the modified app is safe.
