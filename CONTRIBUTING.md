# Contributing

Issues and PRs welcome — this started as a one-machine project (a Pixel 7 fed
by an Arch box) and the fastest way it gets good on *your* hardware is a report
or a patch from you.

## Ground rules

1. **Borrowed code is credited code.** Adapting logic from another project is
   encouraged (that's the whole philosophy), but it must carry a header comment
   naming the source file, project, and license, plus a row in
   [BORROWED.md](BORROWED.md). License compatibility matters: this repo is
   GPL-3.0, so GPL/LGPL/Apache-2.0/MIT/BSD sources are all fine.
2. **No new runtime dependencies without a fight.** The server is deliberately
   gi + aiohttp + evdev; the web client is deliberately framework-free. If a
   feature seems to need a dependency, open an issue first.
3. **Keep the native-feel checklist honest** (ARCHITECTURE.md). A change that
   adds latency to the touch or video path needs a measured before/after, not
   vibes.

## Quick checks before a PR

```bash
bash -n start.sh
python -m py_compile server/nx-streamerd.py
node --check web/app.js
```

CI runs the same three. If you have NX Hub's CLI, `nx manifest check --file
nx-app.json` validates the hub manifest too.

## Good first contributions

- `setup` support for apt/dnf distros (see README "Non-Arch distros")
- latency instrumentation (client-measured RTT surfaced server-side)
- audio track (design notes in ARCHITECTURE.md)
- testing on Intel VAAPI and reporting encoder quirks
