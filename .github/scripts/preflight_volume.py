"""Check that Fly's volume state can satisfy fly.toml before deploying.

Written because three consecutive releases failed on volume problems that only
surfaced *during* `flyctl deploy`, each with an error that described the symptom
rather than the fix. The preconditions are knowable beforehand, so they are
checked beforehand.

Reads the expected name and region from fly.toml rather than from workflow
environment variables, so editing fly.toml cannot silently drift from what the
pipeline verifies.

Usage: preflight_volume.py <fly.toml> <volumes.json> <machines.json> [app]
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


class Problem(Exception):
    """A precondition that will make `flyctl deploy` fail."""


def expected_mount(config: dict) -> tuple[str, str] | None:
    """The (volume name, region) fly.toml requires, or None if it needs no volume."""
    mounts = config.get("mounts")
    if not mounts:
        return None
    # fly.toml accepts both a single [mounts] table and [[mounts]] array form.
    if isinstance(mounts, list):
        if not mounts:
            return None
        mounts = mounts[0]
    source = mounts.get("source")
    region = config.get("primary_region")
    if not source:
        raise Problem("fly.toml has a [mounts] block with no 'source'.")
    if not region:
        raise Problem("fly.toml declares a mount but no 'primary_region'.")
    return source, region


def field(row: dict, *names: str):
    """flyctl has spelled these differently across versions; accept any."""
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def check(config: dict, volumes: list[dict], machines: list[dict], app: str = "") -> str:
    wanted = expected_mount(config)
    if wanted is None:
        return "fly.toml declares no volume mount; nothing to verify."
    name, region = wanted
    app_hint = f"--app {app}" if app else "--app <app>"

    by_name = [v for v in volumes if field(v, "name") == name]
    if not by_name:
        existing = sorted({str(field(v, "name")) for v in volumes})
        raise Problem(
            f"No volume named '{name}' exists.\n"
            f'  fly.toml [mounts] source = "{name}"\n'
            f"  volumes that do exist: {existing or 'none'}\n"
            f"  Fix: fly volumes create {name} --region {region} --size 1 {app_hint}"
        )

    in_region = [v for v in by_name if field(v, "region") == region]
    if not in_region:
        where = sorted({str(field(v, "region")) for v in by_name})
        raise Problem(
            f"Volume '{name}' exists but not in the deploy region.\n"
            f'  fly.toml primary_region = "{region}"\n'
            f"  volume '{name}' is in: {where}\n"
            f"  A volume in another region is invisible to a machine in "
            f"{region}, and Fly reports it as though no volume existed.\n"
            f"  Fix: fly volumes create {name} --region {region} --size 1 {app_hint}\n"
            f"       (or set primary_region to {where[0]!r} in fly.toml)"
        )

    unattached = [v for v in in_region if not field(v, "attached_machine_id", "AttachedMachine")]

    # A volume attached to an existing machine is correct for a rolling update.
    # It is only a problem when Fly has to create the first machine, because
    # that machine needs a free volume to claim.
    if not machines and not unattached:
        raise Problem(
            f"Volume '{name}' exists in {region} but is attached, and this app "
            f"has no machines.\n"
            f"  Creating the first machine requires an *unattached* volume.\n"
            f"  Fix: fly volumes create {name} --region {region} --size 1 {app_hint}\n"
            f"       (or detach/destroy the orphaned one if it holds nothing)"
        )

    state = "unattached" if unattached else f"attached to {len(machines)} existing machine(s)"
    return f"Volume '{name}' present in {region}, {state}."


def main() -> int:
    fly_toml, volumes_json, machines_json = (Path(a) for a in sys.argv[1:4])
    app = sys.argv[4] if len(sys.argv) > 4 else ""
    config = tomllib.loads(fly_toml.read_text())
    volumes = json.loads(volumes_json.read_text() or "[]") or []
    machines = json.loads(machines_json.read_text() or "[]") or []

    try:
        print(f"  {check(config, volumes, machines, app)}")
    except Problem as problem:
        print("::error::Deploy preconditions not met")
        for line in str(problem).splitlines():
            print(f"  {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
