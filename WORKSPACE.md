# Foul Play lab

This checkout is the experimental branch for information-boundary and policy improvements around Foul Play.

## Repository roles

- `D:\Other\ai\foul-play`: upstream baseline checkout. Do not modify source files here.
- `D:\Other\ai\foul-play-lab`: this checkout, branch `codex/dual-view-foul-play`.
- `D:\Other\ai\pokemon-battle-ai`: PS adapter, replay, benchmark, and integration harness.
- `D:\Other\ai\poke-engine`: pinned engine source checkout.
- `D:\Other\ai\ps-sim`: pinned local Pokemon Showdown checkout.

## Rules

1. Keep baseline and lab changes separate.
2. Record source commits and runtime versions in `EXPERIMENT-MANIFEST.json` for reproducible experiments.
3. Do not commit virtual environments, Rust targets, credentials, live logs, or benchmark results into this repository.
4. Use Gen 9 Random Battle as the first target. Add Gen 8 Factory only after the Gen 9 information-boundary replay is correct.
5. Build the following gates in order: replay, dual-view visibility, belief consistency, oracle/shadow prediction, then decision changes.

## Current baseline

- Foul Play commit: `5bd041d62a6f4c8587699a598a9a92407383e4f3`
- Poke-engine commit: `7126ba7ba9029f24cbddefdd6e9dc6193ee72d33`
- Showdown commit: `a5df8274e85b0889bf2a9b3422a08b39732374fc`
