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

## Current implementation status

### 2026-10-05 correction: reference freeze withdrawn

This section supersedes earlier release-gate claims below. The old audits
checked summaries, not the complete Python-to-engine state. Newly confirmed
bugs include private stat-key/HP conversion, opponent boost keys, boost reset
on switch/Haze, form matching, real `|-terastallize|` events, and Encore
last-move identity. Candidate `ps-adapter-state-sync/v0.4.1` fixes these paths
and rejects unsupported active Transform states instead of silently searching
the wrong configuration. Transform event-time copied stats/moves remain a
release blocker. No reference or Dual performance freeze is approved.

- Latest full suite: 625 passed; Ruff passed including the audit tools.
- Engine-field replay: 781 older states passed the selected fields;
  697 new states contained 6 explicitly rejected Transform states, with
  no selected-field differences among the other 691 states.
- Audited fields: private stats/HP/Tera history, active five-stat boosts,
  and active opponent public HP. This is NOT full engine-state parity.
- An evenly spaced 100-state candidate policy replay at 100 ms completed
  99 decisions, rejected one known Transform state, and recorded zero action
  projections and zero input mutations. This is offline, not a live smoke.
- A fainted PS request clears its `terastallized` value; preserving public
  Tera history in the engine is intentional to prevent a second team Tera.
- Pre-fix 8093 execution: 3-17, 48 `raw-best-action-not-legal` projections,
  one actual Encore panic/HTTP disconnect (two runner fallback records).
- New untouched-upstream 8095 execution: 11-9 in 20 completed games,
  rooms 1488-1507. These are independent, unpaired Random Battle batches.
- 8093 and 8094 are still running OLD in-memory code. Process stop/start
  commands were rejected by the execution policy; 8096 was NOT started.
  Candidate fixes must not be described as deployed or live-validated.
- Approximate hand-written transitions are preview-only and blocked by
  default. The old 500-point audit does not establish rules fidelity or
  information safety; its earlier basic-gate claim is withdrawn.
- The existing engine binding now has an offline native-instruction
  transition wrapper with probability/reversibility tests. Engine outcomes
  are not public PS observations. No rollout is enabled on 8094.

See `docs/REFERENCE-PERFORMANCE-CALIBRATION-20261005.md` for evidence and
remaining gates. Earlier figures below are historical, not current approval.

- Task 1: repository/dependency audit and Foul Play decision-entry documentation completed.
- Task 2: PS protocol replay implemented in `pokemon-battle-ai/platform/ps`.
- Task 3/4: minimal `fp.dual_view.DualViewState` and visibility tests completed.
- Task 5: initial Gen 9 Random Battle public-information belief sampler completed.
- Current sampler uses Foul Play's pinned Random Battle dataset and fixed-seed particle generation.
- Task 6: public-history consistency filtering completed.
- The current filter checks explicit public configuration and move observations only; do not treat
  it as a complete Bayesian posterior.
- Task 7: offline Foul Play policy oracle completed.
- Task 8: initial shadow prediction completed.
- Task 9: offline root-action reweighting interface completed.
- Task 10: initial replay decision observer completed.
- Task 11: online shadow reference sidecar and PS room observer implemented in
  `fp/live_reference.py` and `pokemon-battle-ai/platform/ps/live-observer.js`.
- The comparison logger writes a sanitized full state, Foul Play action
  distribution, and action agreement; optional room announcements are
  difference-only and never include hidden opponent configuration.
- Freeze candidate `v0.3-state-sync` was frozen for infrastructure comparison on 2026-10-04. It fixes
  current active Pokemon/form mapping, private-team order matching, and PS
  stat-boost name conversion. The freeze report is in
  `docs/freeze-v0.3-state-sync.md`.
- Validation for the freeze candidate: the initial audit had 559 Python tests
  passed; after the weather-duration regression fix, 573 Python tests passed,
  Ruff passed,
  all PS adapter/replay/observer/stat tests passed, 2621 saved decision states
  passed active/boost/conversion audits, and a 100-battle entry contract smoke
  had zero reference errors, zero DualView errors, zero stale Garganacl
  predictions, and zero invalid selected actions.
- Some saved records represent forced-switch transition requests. Their
  `selectionFallback` is expected request-timing behavior and must be reported
  separately from ordinary decision fallback.
- The 20-game live audit completed with 4 wins and 16 losses, zero reference
  errors, zero invalid actions, and 60.29% ordinary-decision agreement with
  the 8093 reference. This is an infrastructure freeze result, not a policy
  performance claim.
- The 2026-10-05 upstream-compatible calibration completed 20 games and
  781 decision points with a 10-10 record and zero reference errors.
  Final action agreement was 388/781 (49.68%); this is an upstream selector
  calibration result, not a translation pass rate. Reanalysis found three
  Aurora Veil timing differences and no unexplained decision-relevant
  translation differences in the summary-only audit. This did not verify
  engine stat keys, boosts or Tera and is not a release gate. Eight
  baseline-summary HP differences remain separately classified for audit.
- The 8093 response contract now emits
  `schemaVersion=foul-play-reference/v1`,
  `referenceContractVersion=reference-contract/v0.1`, and
  `translationVersion=ps-adapter-state-sync/v0.4`.
- The `strength=0` upstream-compatible 8094 control is implemented and
  covered by offline tests and an HTTP smoke comparison against 8093 using
  the same saved battle-state fixture. This is not yet a live PS match
  release gate.
- A separate pre-fix 8093 actual-execution calibration completed on 2026-10-05
  with 3 wins and 17 losses, zero invalid actions, 48 action projections,
  and one Encore panic/HTTP disconnect counted twice by the runner.
  The existing 8095 untouched-upstream calibration was 10 wins and 10 losses
  in 20 games. The 8093 result is a negative signal, not a causal conclusion,
  because the two batches used independent Random Battle trajectories.
- `fp/belief_transition.py` and `fp/public_observation.py` now provide the
  first auditable one-step transition boundary. A 500-decision replay audit
  recorded zero exceptions, zero hidden-information leaks, zero fallback in
  214 basic attack/switch pairs, and p50/p95 transition latency of
  1.285/6.348 ms. Utility/status moves remain explicit fallbacks; the basic
  gate claim is withdrawn because the preview is not rules-faithful and
  the visibility probe did not cover exact HP. The rollout-ready gate fails.
- `fp/one_step_rollout.py` now evaluates Top-K action pairs through the
  transition boundary and a next-node Foul Play oracle. It is offline-only,
  does not yet update belief inside the rollout, and is not connected to
  8094 actual decisions.
- Benchmark technical statistics now include `teraUses`, `firstTeraTurn`, and
  `teraTurns`; the summarizer reports average uses, usage rate, and timing
  quantiles while excluding games with no tera from timing quantiles.
- The first root-reweighting ablation exposed and fixed two live conversion
  cases: `null` `weatherState.turnsRemaining`, and a sampled/public move list
  longer than the four slots supported by poke-engine. The valid post-fix
  `strength=0` comparison batch is stored separately from the preserved pre-fix
  failed batches; neither result is treated as a policy-performance freeze.
- A 20-game local Foul Play control against the remote `foulplaybot` completed
  on 2026-10-04 with 0 wins, 16.35 average non-initial switches, and 4.30
  average attack moves. This is not within a plausible 50% win-rate sampling
  range and shows that the current local `LiveReferencePolicy` is not yet
  performance-equivalent to the remote bot. Calibrate this control before
  using remote-bot win rate as a direct Dual policy metric.
- A private-server battle setup was confirmed on 2026-10-03, but the local LLM
  decision service exceeded the three-minute battle timeout. No completed live
  comparison sample was collected.
- The live observer has not yet been validated through a complete PS match.
- Online win-rate validation has not started.

## Current baseline

- Foul Play commit: `5bd041d62a6f4c8587699a598a9a92407383e4f3`
- Poke-engine commit: `7126ba7ba9029f24cbddefdd6e9dc6193ee72d33`
- Showdown commit: `a5df8274e85b0889bf2a9b3422a08b39732374fc`
