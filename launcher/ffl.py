#!/usr/bin/env python3
"""ForgeFast Launcher command line.

Exists because the Tkinter GUI needs the platform Tk shared library, which is not present on every
machine (this one lacks `libtk8.6.so`) and is a system package rather than something the launcher may
install. The CLI has no such dependency, so "add a profile and play" works today; `gui.py` is the same
core with a window on top and becomes available the moment Tk is installed.

    ffl.py add "MC Eternal" /path/to/pack
    ffl.py list
    ffl.py play "MC Eternal"
    ffl.py play "MC Eternal" --safe
    ffl.py doctor
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forgefast_launcher as core
from forgefast_launcher import forgefast_components


def find(store: core.Store, needle: str) -> core.Profile | None:
    for profile in store.profiles:
        if profile.name == needle or profile.id == needle:
            return profile
    return None


def cmd_add(args) -> int:
    store = core.Store()
    if find(store, args.name):
        print(f"A profile named {args.name!r} already exists.", file=sys.stderr)
        return 1
    profile = core.Profile.create(args.name, Path(args.path).expanduser().resolve())
    problems = core.analyse(profile)
    if problems:
        print("Cannot use this folder:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    store.profiles.append(profile)
    store.save()
    print(f"Added {profile.name!r}: Minecraft {profile.data['minecraft']}, "
          f"Forge {profile.data['forge']}, {profile.data['mods']} mods")
    print(f"  launcher data: {profile.private_dir()}")
    return 0


def cmd_list(args) -> int:
    store = core.Store()
    if not store.profiles:
        print("No profiles yet. Add one with:  ffl.py add \"Name\" /path/to/pack")
        return 0
    print(f"{'Profile':28} {'Minecraft':10} {'Forge':14} {'Mods':>5}  Last startup")
    for profile in store.profiles:
        history = profile.data.get("history") or []
        last = f"{history[-1]:.1f} s" if history else "-"
        print(f"{profile.name[:28]:28} {profile.data.get('minecraft') or '?':10} "
              f"{profile.data.get('forge') or '?':14} {profile.data.get('mods') or 0:>5}  {last}")
    return 0


def cmd_remove(args) -> int:
    store = core.Store()
    profile = find(store, args.name)
    if profile is None:
        print(f"No such profile: {args.name}", file=sys.stderr)
        return 1
    store.profiles.remove(profile)
    store.save()
    # The modpack itself is deliberately untouched.
    print(f"Removed {profile.name!r} from the launcher. Your modpack folder was not modified.")
    return 0


def cmd_auth(args) -> int:
    """Store a session so multiplayer works.

    The launcher does not implement Microsoft sign-in yet, so the token has to come from a launcher
    you are already signed into. It is written to a separate 0600 file and is redacted everywhere it
    could otherwise be printed. Tokens expire, typically within a day, so this will need redoing.
    """
    store = core.Store()
    profile = find(store, args.name)
    if profile is None:
        print(f"No such profile: {args.name}", file=sys.stderr)
        return 1
    uuid_value = args.uuid.replace("-", "")
    if len(uuid_value) == 32:
        uuid_value = "-".join((uuid_value[:8], uuid_value[8:12], uuid_value[12:16],
                               uuid_value[16:20], uuid_value[20:]))
    core.save_auth(profile.id, args.username, uuid_value, args.access_token)
    print(f"Saved session for {profile.name!r} as {args.username}.")
    print(f"  stored in {core.auth_file()} with 0600 permissions, redacted from all logs")
    print("  tokens expire; if 'Invalid session' returns, run this again with a fresh token")
    return 0


def cmd_optimize(args) -> int:
    import optimize
    store = core.Store()
    profile = find(store, args.name)
    if profile is None:
        print(f"No such profile: {args.name}", file=sys.stderr); return 1
    problems = core.analyse(profile)
    if problems:
        for p in problems: print(f"  - {p}", file=sys.stderr)
        return 1
    for note in optimize.apply(profile):
        print(note)
    print("\nNow press Play in CurseForge as usual. Your login is untouched, so multiplayer works.")
    print("Undo at any time with:  ffl.py restore " + f"{args.name!r}")
    return 0


def cmd_restore(args) -> int:
    import optimize
    store = core.Store()
    profile = find(store, args.name)
    if profile is None:
        print(f"No such profile: {args.name}", file=sys.stderr); return 1
    for note in optimize.restore(profile):
        print(note)
    return 0


def cmd_login(args) -> int:
    """Microsoft device-code sign-in. No password is ever entered here."""
    import msauth
    if args.set_client_id:
        target = core.set_client_id(args.set_client_id)
        print(f"Saved client ID to {target}")
        if not args.name:
            return 0
    cid = core.client_id()
    if not cid:
        print("No Azure client ID configured. Microsoft requires one to identify the application.\n"
              "Create a free one:\n"
              "  1. portal.azure.com -> App registrations -> New registration\n"
              "  2. Supported accounts: personal Microsoft accounts\n"
              "  3. Platform: Mobile and desktop applications\n"
              "  4. Authentication -> enable 'Allow public client flows'\n"
              "  5. ffl.py login --set-client-id <Application (client) ID>", file=sys.stderr)
        return 1
    if not args.name:
        print("Give a profile name to sign in for.", file=sys.stderr)
        return 1
    store = core.Store()
    profile = find(store, args.name)
    if profile is None:
        print(f"No such profile: {args.name}", file=sys.stderr)
        return 1

    try:
        device = msauth.begin_device_login(cid)
        print("\n  Open:  " + device["verification_uri"])
        print("  Code:  " + device["user_code"] + "\n")
        print("Waiting for you to approve in the browser...")
        try:
            import webbrowser
            webbrowser.open(device["verification_uri"])
        except Exception:
            pass
        token = msauth.poll_for_token(cid, device)
        session = msauth.minecraft_session(token["access_token"])
    except msauth.AuthError as failure:
        print(f"\nSign-in failed at the {failure.stage} step:\n  {failure.detail}", file=sys.stderr)
        return 1

    core.save_auth(profile.id, session["username"], session["uuid"], session["accessToken"])
    if token.get("refresh_token"):
        core.save_refresh(profile.id, token["refresh_token"])
    print(f"\nSigned in as {session['username']}. Multiplayer will work for {profile.name!r}.")
    print("  Only a refresh token is stored; it renews the session without signing in again.")
    return 0


def cmd_doctor(args) -> int:
    """Report the environment honestly, including what is missing."""
    java = core.find_java26()
    print(f"Application data : {core.app_data_dir()}")
    print(f"Java 26          : {java or 'NOT FOUND'}")
    if java:
        print(f"  verified major : {core.java_major(java)}")
    components = forgefast_components()
    if components["artifacts"]:
        for prefix, path in components["artifacts"].items():
            print(f"ForgeFast jar    : {prefix}* -> {path.name}")
    else:
        print("ForgeFast jar    : NOT BUILT (would launch stock Forge on Java 26)")
    print(f"Support libs     : {[p.name for p in components['extra_jars']] or 'NONE'}")
    store = core.Store()
    for profile in store.profiles:
        state = "saved session" if core.load_auth(profile.id) else "OFFLINE (singleplayer only)"
        print(f"Auth [{profile.name[:20]:20}]: {state}")
    try:
        import tkinter  # noqa: F401
        print("GUI (Tkinter)    : available")
    except Exception as failure:
        print(f"GUI (Tkinter)    : unavailable ({failure.__class__.__name__}); use this CLI")
    return 0 if java else 1


def cmd_play(args) -> int:
    store = core.Store()
    profile = find(store, args.name)
    if profile is None:
        print(f"No such profile: {args.name}", file=sys.stderr)
        return 1
    problems = core.analyse(profile)
    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    components = forgefast_components()
    if not components["artifacts"]:
        print("[ForgeFast] jars not built; launching stock Forge on Java 26.")
    else:
        print(f"[ForgeFast] active{' (SAFE MODE)' if args.safe else ''}")

    if args.print_command:
        for part in core.build_command(profile, components, args.safe):
            print(part)
        return 0

    result: dict = {}

    def done(ready, code, reason):
        result.update(ready=ready, code=code, reason=reason)

    core.launch(profile, components,
                on_line=lambda line: print(line, flush=True) if args.verbose else None,
                on_done=done, safe_mode=args.safe)

    ready = result.get("ready")
    if ready is not None:
        profile.data.setdefault("history", []).append(round(ready, 2))
        profile.data["history"] = profile.data["history"][-20:]
        store.save()
        print(f"\nUsable main menu in {ready:.2f} s")
    print(result.get("reason", "unknown outcome"))
    return 0 if result.get("code") == 0 or ready is not None else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="ffl.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add a profile for an existing 1.12.2 modpack folder")
    add.add_argument("name")
    add.add_argument("path")
    add.set_defaults(func=cmd_add)

    sub.add_parser("list", help="list profiles").set_defaults(func=cmd_list)
    sub.add_parser("doctor", help="report runtime and component status").set_defaults(func=cmd_doctor)

    opt = sub.add_parser("optimize", help="apply ForgeFast to the pack so CurseForge launches it optimized")
    opt.add_argument("name")
    opt.set_defaults(func=cmd_optimize)

    rest = sub.add_parser("restore", help="undo optimize and put the pack back exactly as it was")
    rest.add_argument("name")
    rest.set_defaults(func=cmd_restore)

    login = sub.add_parser("login", help="sign in with Microsoft (opens a code in your browser)")
    login.add_argument("name", nargs="?", help="profile to sign in for")
    login.add_argument("--set-client-id", help="store your Azure application (client) ID")
    login.set_defaults(func=cmd_login)

    auth = sub.add_parser("auth", help="save a Minecraft session so servers accept your login")
    auth.add_argument("name")
    auth.add_argument("--username", required=True, help="your in-game name")
    auth.add_argument("--uuid", required=True, help="your account UUID (no dashes is fine)")
    auth.add_argument("--access-token", required=True,
                      help="session access token from a launcher you are already signed into")
    auth.set_defaults(func=cmd_auth)

    remove = sub.add_parser("remove", help="remove a profile (never deletes your modpack)")
    remove.add_argument("name")
    remove.set_defaults(func=cmd_remove)

    play = sub.add_parser("play", help="launch a profile")
    play.add_argument("name")
    play.add_argument("--safe", action="store_true",
                     help="disable optional ForgeFast fast paths, keep Java 26 compatibility")
    play.add_argument("--verbose", action="store_true", help="stream the game log")
    play.add_argument("--print-command", action="store_true",
                     help="print the launch argv and exit, without starting the game")
    play.set_defaults(func=cmd_play)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
