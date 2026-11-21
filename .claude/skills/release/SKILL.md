---
name: release
description: Cut a release vX.Y.Z - version bump, changelog, signed EXE, tag and GitHub release
disable-model-invocation: true
argument-hint: X.Y.Z
---
Release `$ARGUMENTS`:

1. Preconditions: clean tree on `main`, both test suites pass, no running instance and no other build (concurrent PyInstaller runs fail with `WinError 32`).
2. Set `__version__` in `agent_monitor_for_claude/__init__.py` and all four fields in `version_info.py` (`filevers`, `prodvers`, `FileVersion`, `ProductVersion`).
3. `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add an empty `## [Unreleased]` above it, update the compare links.
4. Commit `chore: release vX.Y.Z`.
5. Run `python build.py` with a long timeout. It code-signs the EXE and blocks on the token's PIN dialog - tell the user to enter the PIN. Without `signing.env` it builds unsigned; for a release that is an error. Verify the EXE's `FileVersion` matches.
6. Tag `vX.Y.Z`, push commit and tag.
7. `gh release create vX.Y.Z --title vX.Y.Z` with the EXE attached. Notes: the version's `CHANGELOG.md` section, then: "This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it - see [PRIVACY.md](https://github.com/jens-duttke/agent-monitor-for-claude/blob/vX.Y.Z/PRIVACY.md)."
