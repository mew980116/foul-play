# Offline replay observer

`replay_observer.py` reads a saved Gen 9 Random Battle benchmark replay and
prints one decision point with:

- public protocol state;
- recorded action from the benchmark;
- sampled belief particle count;
- Foul Play self policy;
- opponent shadow prediction.

The benchmark protocol does not contain the original private request JSON.
The observer therefore reconstructs the Foul Play `Battle` from public replay
data and sampled Gen 9 Random Battle configurations. It must not be treated as
the original private state.

Example:

```powershell
$python = 'D:\Other\ai\foul-play-lab\.venv\Scripts\python.exe'
& $python tools\replay_observer.py `
  --match-id gen9randombattle-random-1-1790871378424 `
  --decision-index 6 `
  --particles 3 `
  --search-time-ms 5 `
  --json-out D:\Other\ai\pokemon-battle-experiments\reports\replay-observer-sample.json
```

The observer is offline-only. It does not connect to PS and does not change
the online decision bridge.

## Engine input and candidate policy audits

These tools consume captured `upstream-comparison.jsonl` states, which
preserve the acting player's private request. They do not connect to PS.
Output files are created exclusively; an existing report is never replaced.

```powershell
Set-Location D:\Other\ai\foul-play-lab
$python = 'D:\Other\ai\foul-play-lab\.venv\Scripts\python.exe'
$inputFile = 'D:\Other\ai\pokemon-battle-experiments\dual-view-v0\reference-performance-control-20261005\upstream-20261005-142639\upstream-comparison.jsonl'
$root = 'D:\Other\ai\pokemon-battle-experiments\dual-view-v0'
$stamp = Get-Date -Format yyyyMMdd-HHmmss
& $python tools\engine_input_audit.py --input $inputFile `
  --output "$root\engine-input-$stamp.json"
& $python tools\reference_replay_audit.py --input $inputFile `
  --output "$root\reference-policy-$stamp.json" --limit 100 --search-time-ms 100
```

The engine audit checks selected private/public fields, not full engine
parity. The policy audit samples evenly spaced input records and reports
errors, legal-action projections, and input mutation separately. Unsupported
active Transform states remain a release blocker. Neither report establishes
live performance or upstream policy equivalence.
