# FastForge

A launcher that starts Minecraft **Forge 1.12.2** modpacks on **Java 26**, with startup
optimizations applied automatically.

Measured on MC Eternal (390 mods, 341 archives):

| Configuration | Time to usable main menu |
| --- | ---: |
| Stock Forge, Java 8 | 493.31 s |
| Java 26, no optimizations | 224.15 s |
| **Java 26 + FastForge** | **203.47 s** |

Every figure here was measured on the same machine and pack with an interleaved A/B harness.
Nothing is estimated.

## What it does

Old modpacks were built for Java 8. Running them on a modern JVM is most of the win, but it needs
compatibility work first, and Forge 1.12.2 does a lot of avoidable work during startup. FastForge
handles both.

**Java 26 compatibility**

- Replaces `commons-lang3` 3.5, whose `SystemUtils` cannot parse a Java 26 version string and throws
  `NullPointerException` — this takes coremods such as IvToolkit down during startup
- Supplies `pack200` and `javax.annotation-api`, removed from modern JDKs but still required
- Strips Java 8-era JVM flags that no longer exist (`UseConcMarkSweepGC`, `UseParNewGC`,
  `AggressiveOpts`) rather than appending newer flags after them, which does not undo them

**Startup optimizations**

| Optimization | Effect |
| --- | --- |
| Atlas fast path | Vanilla recursive slot packing measured **19.60 s** against **44 ms** for shelf packing over 48,598 sprites |
| Manifest cache | `findClass` asked each archive for its manifest twice per class load, and the JDK answers with a deep copy: **123,767 → 360** clones |
| Discovery cache | Warm launches replay captured mod discovery: 349/349 archives, zero rescans |
| Resource index | 93,596 entries indexed in 36 ms; negative lookup cache short-circuits ~20% of resource probes |
| Registry fast index | Primitive side indexes replace boxed `BiMap` inverse lookups, differential-verified with zero divergence |

**Rejected, and why** — kept here so they are not retried:

- **ZGC** — removed nearly all stop-the-world pause (5.066 s → 0.002 s) and cost **23.88 s** of wall
  clock. Load-barrier overhead dominates on a single saturated critical-path thread.
- **Compact object headers** — +2.09 s, inside noise, and peak RSS *rose* 11.97 → 13.09 GB, opposite
  to the mechanism. Unexplained, so not shipped.
- **Parallel transformers** — slower at every worker count than serial.
- **Texture preprocessing** — +21.6 s.

## Usage

Requires Python 3.9+ and a Java 26 runtime. PySide6 is needed only for the desktop UI.

```bash
python3 launcher/ffl.py doctor                       # report runtime and component status
python3 launcher/ffl.py add "My Pack" /path/to/pack  # detect Minecraft/Forge version and mods
python3 launcher/ffl.py play "My Pack"               # launch
python3 launcher/app.py                              # desktop UI
```

`optimize` applies everything to the pack in place so that **another launcher** starts the optimized
build. Useful when you want to keep using a launcher you are already signed in to:

```bash
python3 launcher/ffl.py optimize "My Pack"
python3 launcher/ffl.py restore  "My Pack"   # undoes every change
```

Nothing is destructive. The ForgeFast coremod is *added* to `mods/`, fork jars are installed under
new Maven coordinates so stock libraries are never overwritten, and each modified version JSON is
copied to `<name>.json.forgefast-backup` first.

## Sign-in

`launcher/msauth.py` implements the OAuth 2.0 **device authorization grant**: you approve a short
code at `microsoft.com/link` in your own browser, and the launcher never sees a password. The chain
is Microsoft → Xbox Live → XSTS → Minecraft services → profile, with an entitlements check before
launch. Only a refresh token is stored, at `0600`, and tokens are redacted from all output.

Sign-in needs an Azure application ID, set once:

```bash
python3 launcher/ffl.py login "My Pack" --set-client-id <application-client-id>
```

Register at portal.azure.com → App registrations → **Personal accounts only** (this selects the
`consumers` tenant, which Minecraft auth requires; `common` fails), platform **Mobile and desktop
applications**, and enable **Allow public client flows**.

Note that newly created Azure applications must additionally be approved for the Minecraft API
before `api.minecraftservices.com` stops returning HTTP 403. Until then, authentication succeeds
through Microsoft, Xbox Live and XSTS and fails only at the final hop — the launcher reports this
explicitly rather than as a generic failure.

## Scope

Minecraft 1.12.2, Forge 14.23.5.x, Java 26, on Windows and Linux x86_64. Deliberately narrow.
Unsupported versions are rejected with a clear message rather than launched unpredictably.

## Status

Verified on Linux against MC Eternal. Windows code paths are written but have **not** been executed.
Java 26 is located, not downloaded. See the measurement notes above for what is established and what
is not.
