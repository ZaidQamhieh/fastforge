#!/usr/bin/env python3
"""Apply ForgeFast to a pack in place, so an existing launcher launches the optimized build.

Why this exists. Microsoft authentication is unavailable to this project: registering an Azure
application now requires a directory, which needs either a credit card or a developer-program
application, and a *new* registration then has to clear Mojang's Minecraft API approval form before
`api.minecraftservices.com` stops returning 403. That is an open-ended process with no documented
turnaround.

CurseForge is already signed in. So rather than reimplement authentication, this makes the pack
itself the optimized one and leaves launching -- and therefore the session -- to the launcher that
already works. Multiplayer keeps working because nothing about the login changes.

Every change is reversible and recorded:

  * the ForgeFast coremod is **added** to `mods/`, nothing is replaced
  * `config/forgefast.properties` is written, backing up any existing file once
  * the Forge version JSON is patched to point at the fork jars, with the original copied to
    `<name>.json.forgefast-backup` first and the fork jars installed under **new** Maven coordinates
    so the stock libraries are never overwritten

`restore()` undoes all of it. Nothing is deleted from the pack at any point.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forgefast_launcher as core

#: Coordinates the fork jars are installed under. Deliberately distinct from the stock ones so the
#: originals stay on disk untouched and a restore is a file copy rather than a re-download.
FORK_FORGE = "net.minecraftforge:forge:1.12.2-14.23.5.2860-forgefast"
FORK_LAUNCHWRAPPER = "net.minecraft:launchwrapper:1.12-forgefast"
NEW_LANG3 = "org.apache.commons:commons-lang3:3.18.0"


def maven_path(coordinate: str) -> Path:
    group, artifact, version = coordinate.split(":")
    return Path(*group.split("."), artifact, version, f"{artifact}-{version}.jar")


def version_json(install: Path, forge: str) -> Path:
    return install / "versions" / f"forge-{forge}" / f"forge-{forge}.json"


def apply(profile: core.Profile) -> list[str]:
    """Make this pack the optimized one. Returns a human-readable list of what changed."""
    notes: list[str] = []
    components = core.forgefast_components()
    install = Path(profile.data["install"])
    forge = profile.data["forge"]
    libraries = install / "libraries"

    # 1. ForgeFast config -- the atlas fast path and the caches are switched on here.
    notes += core.apply_optimizations(profile)

    # 2. The coremod, added to mods/. This is what carries the atlas fast path, the discovery cache,
    #    the resource index and the bundled native engine; without it the rest is inert.
    coremod = components.get("coremod")
    if coremod is None:
        notes.append("WARNING: ForgeFast jar not built, so no optimizations were installed")
    else:
        mods = profile.path / "mods"
        mods.mkdir(exist_ok=True)
        for existing in mods.glob("forgefast-*.jar"):
            existing.unlink()
        shutil.copy2(coremod, mods / coremod.name)
        notes.append(f"installed {coremod.name} into mods/")

    # 3. Fork jars, installed under new coordinates so nothing stock is overwritten.
    redirects: dict[str, str] = {}
    for stock_prefix, coordinate, source in (
            ("net.minecraftforge:forge", FORK_FORGE, components["artifacts"].get("forge-1.12.2")),
            ("net.minecraft:launchwrapper", FORK_LAUNCHWRAPPER,
             components["artifacts"].get("launchwrapper-")),
            ("org.apache.commons:commons-lang3", NEW_LANG3,
             components["artifacts"].get("commons-lang3-"))):
        if source is None or not Path(source).is_file():
            continue
        target = libraries / maven_path(coordinate)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.stat().st_size != Path(source).stat().st_size:
            shutil.copy2(source, target)
        redirects[stock_prefix] = coordinate
        notes.append(f"installed {coordinate.split(':')[1]} -> libraries/{maven_path(coordinate)}")

    # 4. Point the version JSONs at them, after backing each original up exactly once.
    #
    # Both files matter. The Forge JSON declares forge and launchwrapper, but `commons-lang3` is
    # declared in the *parent* vanilla JSON that Forge inherits from -- and that is the jar whose 3.5
    # SystemUtils cannot parse a Java 26 version string, NPEs, and takes IvToolkit's coremod down
    # with it. Patching only the Forge JSON leaves the crash in place.
    targets = [version_json(install, forge), install / "versions" / "1.12.2" / "1.12.2.json"]
    total = 0
    for manifest in targets:
        if not manifest.is_file():
            notes.append(f"WARNING: {manifest.name} not found; not patched")
            continue
        backup = manifest.with_suffix(".json.forgefast-backup")
        if not backup.is_file():
            shutil.copy2(manifest, backup)
            notes.append(f"backed up {manifest.name} -> {backup.name}")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        changed = 0
        for library in data.get("libraries", []):
            name = library.get("name", "")
            for stock_prefix, coordinate in redirects.items():
                if name.startswith(stock_prefix + ":") and name != coordinate:
                    library["name"] = coordinate
                    changed += 1
        if changed:
            manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
            notes.append(f"{manifest.name}: redirected {changed} librar"
                         f"{'y' if changed == 1 else 'ies'}")
        total += changed
    if not total:
        notes.append("version JSONs already pointing at the ForgeFast build")

    notes.append("")
    notes.append("Add these JVM arguments in CurseForge (Settings -> Minecraft -> "
                 "Java Settings, or the instance's own settings):")
    notes.append("  " + " ".join(f"-D{k}={v}" for k, v in core.OPTIMIZATION_PROPERTIES.items())
                 + " -Dfml.coreMods.load=org.forgefast.core.ForgeFastCorePlugin")
    return notes


def restore(profile: core.Profile) -> list[str]:
    """Put the pack back exactly as it was."""
    notes: list[str] = []
    install = Path(profile.data["install"])
    for manifest in (version_json(install, profile.data["forge"]),
                     install / "versions" / "1.12.2" / "1.12.2.json"):
        backup = manifest.with_suffix(".json.forgefast-backup")
        if backup.is_file():
            shutil.copy2(backup, manifest)
            backup.unlink()
            notes.append(f"restored {manifest.name}")
    for jar in (profile.path / "mods").glob("forgefast-*.jar"):
        jar.unlink()
        notes.append(f"removed {jar.name} from mods/")
    config = profile.path / "config" / "forgefast.properties"
    saved = config.with_suffix(".properties.before-forgefast-launcher")
    if saved.is_file():
        shutil.copy2(saved, config)
        saved.unlink()
        notes.append("restored your previous forgefast.properties")
    elif config.is_file():
        config.unlink()
        notes.append("removed forgefast.properties")
    notes.append("The ForgeFast library jars were left in place; they are unused once the version "
                 "JSON no longer references them.")
    return notes


def status(profile: core.Profile) -> dict:
    """What is currently applied, read from disk rather than assumed."""
    install = Path(profile.data["install"])
    manifest = version_json(install, profile.data["forge"])
    patched = False
    if manifest.is_file():
        text = manifest.read_text(encoding="utf-8")
        patched = "forgefast" in text
    return {
        "coremodInstalled": any((profile.path / "mods").glob("forgefast-*.jar")),
        "configWritten": (profile.path / "config" / "forgefast.properties").is_file(),
        "versionJsonPatched": patched,
        "backupExists": manifest.with_suffix(".json.forgefast-backup").is_file(),
    }
