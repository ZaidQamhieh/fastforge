#!/usr/bin/env python3
"""ForgeFast Launcher -- run an existing Forge 1.12.2 modpack under managed Java 26.

Scope for v1 is deliberately narrow: Minecraft 1.12.2, Forge 14.23.5.x, Java 26. Nothing else.

Why this is built on the benchmark harness rather than fresh code: constructing a correct 1.12.2
classpath is the part that actually breaks, and `scripts/integration/forgefast_harness.py` already
does it correctly -- version-chain walking, library rule filtering, native extraction, jar
substitution for the fork -- validated by dozens of successful 390-mod launches. Re-deriving that
from the version JSON would reintroduce bugs this project already paid for, including the
hand-built-classpath failure recorded in PERFORMANCE.md. So the launcher imports it.

The GUI is Tkinter because it is in the standard library: no pip install, no packaging toolchain, and
it runs on a clean machine that has only this application's own Python. That is a deliberate trade of
visual polish for working-today.

Honest limits of this version, all listed in the README rather than hidden:
  * Windows code paths are written but have NOT been executed -- no Windows host is available here.
  * Java 26 is located, not provisioned. Automatic JDK download is not implemented.
  * No authentication. Launches in offline mode, which is sufficient for singleplayer testing.
  * Only one pack (MC Eternal) has been tested end to end.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

# The proven launch machinery. Imported, never reimplemented.
_HARNESS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "integration"
sys.path.insert(0, str(_HARNESS_DIR))
try:
    import forgefast_harness as harness
except Exception as import_failure:  # pragma: no cover - surfaced in the GUI
    harness = None
    _HARNESS_IMPORT_ERROR = import_failure
else:
    _HARNESS_IMPORT_ERROR = None

APP_NAME = "ForgeFastLauncher"
FORGE_VERSION = "1.12.2-14.23.5.2860"
READY_MARKER = re.compile(r"Forge Mod Loader has successfully loaded (\d+) mods")

#: Java 8-era flags that Java 26 either removed or that actively harm this workload. Modpack
#: launchers still ship these in their instance configs, and appending Java 26 flags after them does
#: not undo them -- the JVM either refuses to start or silently keeps the bad setting.
#:
#: `-XX:+UseZGC` is in this list because it was measured on this project: 209.73 s on G1 against
#: 233.61 s on ZGC, a 23.88 s regression. It removes nearly all stop-the-world pause and loses far
#: more to load-barrier overhead on the single saturated critical-path thread.
OBSOLETE_JVM_ARGS = (
    "-XX:+UseConcMarkSweepGC",      # removed in Java 14
    "-XX:+CMSIncrementalMode",      # removed
    "-XX:+UseParNewGC",             # removed in Java 10
    "-XX:+AggressiveOpts",          # removed in Java 12
    "-XX:+UseZGC",                  # measured regression on this workload
    "-XX:+UseShenandoahGC",         # untested here; G1 is the validated default
    "-Xverify:none",                # measured no benefit, higher variance
    "-XX:+UseCompactObjectHeaders",  # unresolved: peak RSS rose 11.97 -> 13.09 GB
    "-XX:MaxPermSize",              # Java 7 concept
    "-XX:PermSize",
)


def forgefast_components() -> dict:
    """The ForgeFast jars to substitute, if this checkout has built them.

    Absent components are not an error: the launcher then runs stock Forge on Java 26, which still
    works and is a useful fallback. What must never happen is claiming ForgeFast is active when the
    jars are missing, so this returns what it actually found.
    """
    repo = Path(__file__).resolve().parent.parent
    fork = repo.parent / "files-pasted-by-the-user-forgefast" / "work" / "MinecraftForge"
    forge_jar = (fork / "projects/forge/build/libs"
                 / "forge-1.12.2-14.23.5.2860-forgefast.0.1.0-dev-universal.jar")
    lw_jar = fork / "launchwrapper/build/libs/launchwrapper-1.12-forgefast.1.jar"
    artifacts = {}
    if forge_jar.is_file():
        artifacts["forge-1.12.2"] = forge_jar
    if lw_jar.is_file():
        artifacts["launchwrapper-"] = lw_jar

    extra = []
    # ForgeFast's own jar carries the coremod: the atlas fast path, discovery cache, resource index
    # and the bundled native engine all live here, not in the Forge fork. Without it on the classpath
    # the launcher would substitute two jars and silently deliver none of those -- which is exactly
    # the class of bug that left fastStitcher disabled for the project's whole history.
    coremod = sorted(repo.glob("build/libs/forgefast-*.jar"))
    coremod = [j for j in coremod if "sources" not in j.name and "javadoc" not in j.name]
    forgefast_jar = coremod[-1] if coremod else None
    if forgefast_jar is not None:
        extra.append(forgefast_jar)

    gradle = Path.home() / ".gradle/caches/modules-2/files-2.1"
    for pattern in ("org.glavo/pack200/*/*/pack200-*.jar",
                    "javax.annotation/javax.annotation-api/*/*/javax.annotation-api-*.jar"):
        extra += sorted(gradle.glob(pattern))[:1]

    # commons-lang3 must be *replaced*, not appended. Stock 1.12.2 ships 3.5, whose SystemUtils
    # cannot parse a Java 26 version string and throws NullPointerException during startup. Adding a
    # newer jar does nothing because the old one appears earlier on the classpath. This is a hard
    # Java 26 requirement rather than an optimization: without it the pack does not boot.
    newer_lang3 = sorted(gradle.glob("org.apache.commons/commons-lang3/3.1[0-9]*/*/commons-lang3-*.jar"))
    if newer_lang3:
        artifacts["commons-lang3-"] = newer_lang3[-1]

    return {"artifacts": artifacts, "extra_jars": extra, "coremod": forgefast_jar}


#: Every ForgeFast optimization the launcher turns on, and the evidence for each.
#:
#: These are stated explicitly rather than left to defaults. Defaults have silently regressed in this
#: project before -- the atlas fast path shipped disabled for its entire history because a config
#: template overrode the field default -- so the launcher asserts the configuration it intends and
#: `verify_optimizations()` reports what actually took effect.
OPTIMIZATION_PROPERTIES = {
    # launchwrapper: 123,767 -> 360 JDK manifest clones on a 390-mod pack
    "forgefast.manifestCache": "true",
    # one-pass annotation ownership index, replacing ~69M File.equals comparisons
    "forgefast.asmDataIndex": "true",
    # exact constant-pool fast rejects, byte-identical over 120,464 real classes
    "forgefast.transformerPrefilter": "true",
    # primitive registry side indexes; differential-verified with zero disagreements
    "forgefast.registryFastIndex": "true",
}

#: forgefast.properties entries written into the pack. The atlas path is the big one: observe mode
#: validated it on a real pack (observedValid=1) and vanilla packing measured 19.60 s against 44 ms.
OPTIMIZATION_CONFIG = {
    "enabled": "true",
    "nativeEnabled": "true",
    "fastStitcher": "accelerate",
    "archiveIndex": "true",
    "resourceIndex": "true",
    "discoveryFastPath": "true",
    "discoveryCache": "true",
    "discoveryRequireValidation": "true",
    # Measured regressions and unresolved results stay off, deliberately.
    "texturePreprocessing": "false",
    "discoveryShadowMode": "false",
    "profiler": "false",
    "transformerTiming": "false",
    "safeMode": "false",
}


def apply_optimizations(profile: "Profile", safe_mode: bool = False) -> list[str]:
    """Write the ForgeFast configuration into the pack, and report what was done.

    ForgeFast reads `config/forgefast.properties` from the game directory, so this is the one place
    the launcher must write inside the user's pack. It is additive -- a new file in `config/` -- and
    any pre-existing file is backed up once before being replaced, so the original is recoverable.
    """
    notes: list[str] = []
    config_dir = profile.path / "config"
    config_dir.mkdir(exist_ok=True)
    target = config_dir / "forgefast.properties"

    values = dict(OPTIMIZATION_CONFIG)
    if safe_mode:
        values["safeMode"] = "true"

    if target.is_file():
        backup = config_dir / "forgefast.properties.before-forgefast-launcher"
        if not backup.is_file():
            backup.write_bytes(target.read_bytes())
            notes.append(f"backed up your existing config to {backup.name}")

    body = "# Written by ForgeFast Launcher. Delete this file to return to ForgeFast defaults.\n"
    body += "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
    target.write_text(body, encoding="utf-8")
    notes.append(f"atlas fast path: {values['fastStitcher']}"
                 + (" (SAFE MODE: all fast paths disabled)" if safe_mode else ""))
    return notes


def verify_optimizations(log_text: str) -> list[str]:
    """Read back, from the game's own log, which optimizations actually engaged.

    Asserting a configuration is not the same as it taking effect. This reports observed evidence so a
    silently-disabled optimization cannot pass unnoticed again.
    """
    observed: list[str] = []
    if "accelerated=1" in log_text or "ForgeFast: fast stitcher" in log_text:
        observed.append("atlas fast path engaged")
    if "Discovery fast path" in log_text:
        observed.append("discovery cache engaged")
    if "Resource index" in log_text:
        observed.append("resource index built")
    if "Native engine loaded" in log_text:
        observed.append("native engine loaded")
    elif "Native engine resource missing" in log_text:
        observed.append("native engine MISSING (fell back to Java)")
    if "Early state:" in log_text:
        observed.append("ForgeFast core plugin active")
    return observed

def auth_file() -> Path:
    """Session credentials, kept out of profiles.json.

    Separate file, 0600, and never written to any log or diagnostic report. An access token is a
    bearer credential for the user's Microsoft account: anyone holding it can act as them until it
    expires. Storing it at all is a real risk, which is why it is opt-in, isolated, and why the
    proper fix is a Microsoft device-code OAuth flow that this launcher does not yet implement.
    """
    return app_data_dir() / "auth.json"


def client_id() -> str:
    """The Azure application ID, from config or environment.

    Kept out of the source because it identifies *your* registration, not this project's. Set it
    once with `ffl.py login --set-client-id <id>`, or export FORGEFAST_CLIENT_ID.
    """
    import os as _os
    env = _os.environ.get("FORGEFAST_CLIENT_ID")
    if env:
        return env.strip()
    path = app_data_dir() / "client_id.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def set_client_id(value: str) -> Path:
    path = app_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    target = path / "client_id.txt"
    target.write_text(value.strip(), encoding="utf-8")
    return target


def save_refresh(profile_id: str, refresh_token: str) -> None:
    """Refresh tokens renew a session without another sign-in, so this is what gets persisted."""
    path = auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    entry = data.setdefault(profile_id, {})
    entry["refreshToken"] = refresh_token
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def refresh_session(profile_id: str) -> dict | None:
    """Renew a stored session. Returns fresh credentials, or None if re-login is needed."""
    try:
        data = json.loads(auth_file().read_text(encoding="utf-8"))
    except Exception:
        return None
    token = (data.get(profile_id) or {}).get("refreshToken")
    if not token:
        return None
    try:
        import msauth
        renewed = msauth.refresh(client_id(), token)
        session = msauth.minecraft_session(renewed["access_token"])
        if renewed.get("refresh_token"):
            save_refresh(profile_id, renewed["refresh_token"])
        save_auth(profile_id, session["username"], session["uuid"], session["accessToken"])
        return session
    except Exception:
        return None


def load_auth(profile_id: str) -> dict | None:
    try:
        data = json.loads(auth_file().read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = data.get(profile_id) or data.get("default")
    if not entry or not entry.get("accessToken"):
        return None
    return entry


def save_auth(profile_id: str, username: str, uuid_value: str, access_token: str) -> None:
    path = auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data[profile_id] = {"username": username, "uuid": uuid_value, "accessToken": access_token}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def redact(command: list[str]) -> list[str]:
    """A copy of the launch argv safe to print or log: the token is replaced, never shown."""
    out = list(command)
    for index, part in enumerate(out):
        if part == "--accessToken" and index + 1 < len(out):
            out[index + 1] = "<redacted>"
    return out


#: Windows and Linux on x86_64 only, matching what the native accelerator is built for. macOS is
#: deliberately out of scope: 1.12.2 on Apple Silicon needs Rosetta and an x86_64 LWJGL, none of
#: which can be tested here, and shipping untested support is worse than declining it.
SUPPORTED_PLATFORMS = ("win32", "linux")


def require_supported_platform() -> None:
    """Fail clearly on an unsupported OS rather than launching unpredictably."""
    if sys.platform not in SUPPORTED_PLATFORMS:
        raise RuntimeError(
            "ForgeFast Launcher supports Windows and Linux on x86_64. This system reports "
            f"'{sys.platform}', which is untested and unsupported.")


def app_data_dir() -> Path:
    """Per-platform application data, never the working directory."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def profiles_file() -> Path:
    return app_data_dir() / "profiles.json"


def find_java26() -> Path | None:
    """Locate a Java 26 runtime.

    Deliberately does not consult JAVA_HOME or PATH first: this project's whole premise is that the
    JVM version is controlled, and a system Java 8 on PATH would launch and then fail deep inside
    Forge. Candidate directories are probed and the version is *verified* by running the binary.
    """
    exe = "java.exe" if sys.platform == "win32" else "java"  # Windows or Linux; see SUPPORTED_PLATFORMS
    candidates: list[Path] = []
    managed = app_data_dir() / "runtime" / "java26" / "bin" / exe
    candidates.append(managed)
    if sys.platform == "win32":
        for root in (Path("C:/Program Files/Java"), Path("C:/Program Files/Eclipse Adoptium")):
            if root.is_dir():
                candidates += sorted(root.glob("*26*/bin/java.exe"))
    else:
        for root in (Path("/usr/lib/jvm"), Path.home() / ".jdks", Path("/opt")):
            if root.is_dir():
                candidates += sorted(root.glob("*26*/bin/java"))
    for candidate in candidates:
        if candidate.is_file() and java_major(candidate) == 26:
            return candidate
    return None


def java_major(java: Path) -> int | None:
    """Actual major version of a JVM, by asking it rather than parsing its path."""
    try:
        done = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'version "(\d+)', (done.stderr or "") + (done.stdout or ""))
    return int(match.group(1)) if match else None


class Profile:
    """One modpack installation plus the launcher state that belongs to it."""

    def __init__(self, data: dict):
        self.data = data

    @staticmethod
    def create(name: str, pack_dir: Path) -> "Profile":
        return Profile({
            "id": str(uuid.uuid4()),
            "name": name,
            "path": str(pack_dir),
            "minecraft": None,
            "forge": None,
            "mods": 0,
            "ramMb": 0,
            "extraJvmArgs": [],
            "history": [],
            "lastError": None,
        })

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def path(self) -> Path:
        return Path(self.data["path"])

    def private_dir(self) -> Path:
        """Launcher-owned state, isolated per profile so one pack cannot affect another."""
        return app_data_dir() / "profiles" / self.id


def analyse(profile: Profile) -> list[str]:
    """Detect what the pack is, and report problems instead of guessing.

    Returns a list of human-readable problems; empty means launchable.
    """
    problems: list[str] = []
    pack = profile.path
    if not pack.is_dir():
        return [f"Folder does not exist: {pack}"]

    mods = pack / "mods"
    jars = sorted(p for p in mods.glob("*.jar")) if mods.is_dir() else []
    profile.data["mods"] = len(jars)
    if not jars:
        problems.append("No mods/*.jar found -- is this a Minecraft instance folder?")

    # Minecraft/Forge version comes from the install the pack points at, which is where the harness
    # reads it from too. Detection is by directory, then verified against the known supported build.
    install = harness.resolve_install(pack, None) if harness else None
    if install is None:
        problems.append("Could not find a Minecraft install (a folder with versions/ and libraries/) "
                        "near this pack. Launch the pack once in its original launcher first.")
        return problems

    profile.data["install"] = str(install)
    mc, forge = detect_versions(pack, install)
    profile.data["minecraft"] = mc
    profile.data["forge"] = forge

    if mc is None or forge is None:
        problems.append("Could not determine the Minecraft and Forge version of this pack. "
                        "ForgeFast v1 supports Minecraft 1.12.2 with Forge 14.23.5.x only.")
        return problems
    if mc != "1.12.2":
        problems.append(f"This pack is Minecraft {mc}. ForgeFast v1 supports 1.12.2 only.")
    elif not forge.startswith("14.23.5"):
        problems.append(f"This pack uses Forge {forge}; v1 is validated only against 14.23.5.x.")

    version_dir = install / "versions" / f"forge-{forge}"
    if not version_dir.is_dir():
        problems.append(f"The install has no versions/forge-{forge} folder. "
                        "Launch the pack once in its original launcher so Forge is installed.")
    return problems


def detect_versions(pack: Path, install: Path) -> tuple[str | None, str | None]:
    """The pack's Minecraft and Forge version.

    Read from the pack's own metadata, not by scanning the install. A shared install legitimately
    contains many loaders -- this machine's holds Forge 14.23.5, Forge 47.4.21, several NeoForge
    builds and multiple Minecraft versions -- so scanning it cannot tell which one *this* pack wants,
    and picking the first match would launch the wrong thing.
    """
    meta = pack / "minecraftinstance.json"
    if meta.is_file():
        try:
            data = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
            mc = (data.get("gameVersion")
                  or (data.get("baseModLoader") or {}).get("minecraftVersion"))
            forge = (data.get("baseModLoader") or {}).get("forgeVersion")
            if forge is None:
                # Older CurseForge files record only the composite name, e.g. forge-14.23.5.2860.
                name = (data.get("baseModLoader") or {}).get("name") or ""
                match = re.search(r"forge-([\d.]+)", name)
                forge = match.group(1) if match else None
            if mc and forge:
                return str(mc), str(forge)
        except Exception:
            pass

    # Fallback: a MultiMC/Prism style pack description.
    mmc = pack.parent / "mmc-pack.json"
    if mmc.is_file():
        try:
            data = json.loads(mmc.read_text(encoding="utf-8", errors="replace"))
            mc = forge = None
            for component in data.get("components", []):
                if component.get("uid") == "net.minecraft":
                    mc = component.get("version")
                elif component.get("uid") == "net.minecraftforge":
                    forge = component.get("version")
            if mc and forge:
                return str(mc), str(forge)
        except Exception:
            pass
    return None, None


def sanitize_jvm_args(user_args: list[str]) -> tuple[list[str], list[str]]:
    """Split user JVM arguments into (kept, dropped-with-reason).

    Modpack configs routinely carry Java 8-era flags. Appending correct flags afterwards does not
    cancel them, so they are removed explicitly and the reason is shown rather than silently applied.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for arg in user_args:
        head = arg.split("=", 1)[0]
        if any(arg.startswith(bad) or head == bad for bad in OBSOLETE_JVM_ARGS):
            dropped.append(arg)
            continue
        if arg.startswith("-Xmx") or arg.startswith("-Xms"):
            dropped.append(arg)  # heap is launcher-owned, set from the profile
            continue
        kept.append(arg)
    return kept, dropped


def build_command(profile: Profile, forgefast: dict, safe_mode: bool = False) -> list[str]:
    """The full argv for the child JVM. Never a shell string.

    Process arguments are passed as a list so a pack path containing spaces, parentheses or
    non-ASCII characters cannot be re-parsed by a shell.
    """
    require_supported_platform()
    require_supported_platform()
    java = find_java26()
    if java is None:
        raise RuntimeError("No Java 26 runtime found. ForgeFast requires Java 26 and will not fall "
                           "back to another version.")
    install = Path(profile.data["install"])
    ram = profile.data.get("ramMb") or default_ram_mb()
    extra, _ = sanitize_jvm_args(profile.data.get("extraJvmArgs") or [])

    properties: list[str] = [f"{k}={v}" for k, v in OPTIMIZATION_PROPERTIES.items()]
    if forgefast.get("coremod") is not None:
        # Loading the coremod from the classpath leaves the user's mods/ folder untouched. Copying the
        # jar into the pack would work too, but it mutates the user's installation for something the
        # launcher can declare instead.
        properties.append("fml.coreMods.load=org.forgefast.core.ForgeFastCorePlugin")
    if safe_mode:
        # One switch that turns off every optional fast path while keeping the Java 26 compatibility
        # layer, so a failure can be attributed to ForgeFast or to Java 26 rather than guessed at.
        properties.append("forgefast.safeMode=true")

    command = harness.launch_command(
        java=java,
        instance=profile.path,
        install=install,
        forge_version=FORGE_VERSION,
        max_heap=f"{ram}m",
        initial_heap=None,
        jfr_file=None,
        gc="legacy",          # G1. ZGC measured 23.88 s slower on this workload.
        verify_none=False,
        huge_pages=False,
        artifacts=forgefast.get("artifacts") or {},
        extra_jars=forgefast.get("extra_jars") or [],
        jvm_properties=properties,
        extra_jvm_flags=extra,
    )

    # The harness hardcodes placeholder credentials (accessToken "0"), which is fine for benchmarks
    # and for singleplayer, but Mojang's session server rejects it -- that is the "Invalid session"
    # a server login reports. Substitute the real session when the user has supplied one.
    auth = load_auth(profile.id)
    if auth is not None:
        for flag, value in (("--username", auth["username"]),
                            ("--uuid", auth["uuid"]),
                            ("--accessToken", auth["accessToken"]),
                            ("--userType", auth.get("userType", "msa"))):
            if flag in command:
                command[command.index(flag) + 1] = value
    return command


def default_ram_mb() -> int:
    """A defensible default rather than "all the RAM"."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        total = 8192
    return max(4096, min(12288, int(total * 0.45)))


class Store:
    """Profile persistence. Written atomically so a crash cannot truncate the file."""

    def __init__(self) -> None:
        self.profiles: list[Profile] = []
        self.load()

    def load(self) -> None:
        path = profiles_file()
        if not path.is_file():
            self.profiles = []
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.profiles = [Profile(entry) for entry in raw.get("profiles", [])]
        except Exception:
            # A corrupt file must not make the launcher unusable; keep it for inspection.
            path.replace(path.with_suffix(".json.corrupt"))
            self.profiles = []

    def save(self) -> None:
        path = profiles_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "profiles": [p.data for p in self.profiles]}
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(path)


def launch(profile: Profile, forgefast: dict, on_line: Callable[[str], None],
           on_done: Callable[[float | None, int, str], None], safe_mode: bool = False) -> None:
    """Spawn Minecraft and stream its output. Runs on a worker thread; never blocks the GUI."""
    try:
        for note in apply_optimizations(profile, safe_mode):
            on_line(f"[ForgeFast] {note}")
        command = build_command(profile, forgefast, safe_mode)
    except Exception as failure:
        on_done(None, -1, str(failure))
        return
    if load_auth(profile.id) is None:
        on_line("[ForgeFast] no saved session: offline mode. Singleplayer works; "
                "joining a server will fail with 'Invalid session'. Fix with:  ffl.py auth <profile>")

    logs = profile.private_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"launch-{time.strftime('%Y%m%d-%H%M%S')}.log"

    started = time.perf_counter()
    ready: float | None = None
    try:
        process = subprocess.Popen(command, cwd=str(profile.path), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as failure:
        on_done(None, -1, f"Could not start Java: {failure}")
        return

    with log_path.open("w", encoding="utf-8") as log_file:
        assert process.stdout is not None
        for line in process.stdout:
            log_file.write(line)
            if ready is None and READY_MARKER.search(line):
                ready = time.perf_counter() - started
                on_line(f"[ForgeFast] usable main menu in {ready:.2f} s")
            on_line(line.rstrip())
    code = process.wait()
    on_done(ready, code, classify_exit(code, ready, log_path))


def classify_exit(code: int, ready: float | None, log_path: Path) -> str:
    """Turn an exit code into something a user can act on."""
    if code == 0:
        return "Exited normally."
    if ready is not None:
        return f"Game closed (exit {code}) after reaching the main menu."
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if "java.lang.OutOfMemoryError" in text:
        return "Out of memory. Increase the RAM allocation for this profile."
    if "Unable to make" in text and "accessible" in text:
        return ("A component was blocked by Java 26 module access. This needs a compatibility rule; "
                "try ForgeFast Safe Mode and send the log.")
    if "NoClassDefFoundError" in text or "ClassNotFoundException" in text:
        return "A required library was missing from the classpath. See the log."
    if "Mixin" in text and "FAILED" in text.upper():
        return "A Mixin failed to apply. Try ForgeFast Safe Mode."
    if "forgefast" in text.lower() and "disagreed" in text.lower():
        return "A ForgeFast fast path disagreed with Forge and stopped. This is a bug; send the log."
    return f"Did not reach the main menu (exit {code}). See the log."
