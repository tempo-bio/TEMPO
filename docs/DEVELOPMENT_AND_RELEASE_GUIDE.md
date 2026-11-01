# Development and verification

Application modules are at the repository root. Maintained automation is in `scripts/`, automated tests and synthetic fixtures are in `tests/`, the Colab trainer and its builder are in `notebooks/`, and Windows installer definitions are in `installer/`.

## Environments

Use an isolated virtual environment; do not install project dependencies into the system Python. Windows builds use Python **3.13.16 x64** and hash-pinned wheels. With that Python installed, prepare an equivalent source environment in PowerShell:

```powershell
py -3.13 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --no-deps --require-hashes -r requirements-windows.lock
& .\.venv\Scripts\python.exe -m scripts.windows_native_bundle
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe tempo_gui.py
```

The graphical Windows bootstrap is a separate source-installation path. The packaged installer does not run it or install dependencies with pip on a collaborator's computer.

macOS development uses `.venv` for native operations and training, and `.venv_x86` under Rosetta for Micro-Manager acquisition. The GUI can relaunch under Rosetta on Apple Silicon. Hardware drivers and adapters must match that interpreter. Automated GUI checks use isolated offscreen fixtures; do not launch a second native TEMPO instance for validation.

## Tests and interface fixtures

Run the full suite from the repository root. On Windows:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
& .\.venv\Scripts\python.exe -m scripts.windows_test_suite --output Output/unittest-results.json
```

Discovery includes scientific signal and model-component checks. The runner rejects unexpected Windows skips. Tests use temporary data and mocked services; passing them does not establish camera-driver compatibility, a completed Colab GPU run, or successful collaboration between real accounts.

Render the maintained interface fixtures with:

```powershell
& .\.venv\Scripts\python.exe -m scripts.render_ui_screens --output Output/screens --font-size 10
```

The renderer uses isolated preferences and simulated hardware and remote services. Review screenshots alongside tests. Generated captures and diagnostics stay outside tracking; local investigation evidence belongs in ignored `.agent-artifacts/`.

## Notebook generation

After changing training or Colab runtime sources, regenerate the maintained notebook:

```powershell
& .\.venv\Scripts\python.exe -m notebooks.build_staged_colab_notebook
```

The notebook embeds the explicit source-file list defined by the cloud bundle. The builder performs no training and needs no account credentials. Keep the maintained notebook tracked without saved outputs or execution counts. Regeneration does not update a Colab copy already uploaded by a user.

## Model evaluation

The [Model Evaluation developer guide](MODEL_EVALUATION.md) explains how to design a comparison, define data and metric contracts, use the evaluation infrastructure, and add another protocol. It also documents the current implementation's limits. Study-specific hypotheses, condition lists, training budgets, acceptance thresholds and run-through instructions belong in local ignored protocol documents.

| Module | Responsibility |
| --- | --- |
| `model_evaluation.py` | Maintainer page, stage actions, numerical results and linked prediction views. |
| `maintainer_tab.py` | Verified access, instruction dialogs and pending-write shutdown protection. |
| `tempo_evaluation.py` | Immutable plans, exposure-aware assignments, transactions, queuing, recovery and adoption. |
| `tempo_evaluation_notebook.py` | Queued-intent lookup and frozen-source verification before package setup. |
| `tempo_evaluation_runner.py` | Colab ownership, progress, cancellation, artifact persistence and recovery. |
| `tempo_evaluation_protocol.py` | Versioned protocol packages, parameter validation, execution context and report validation. |
| `tempo_training.py` and model-specific training modules | Shared compilation, training, inference and scoring. |
| `tempo_cloud.py`, `tempo_cloud_runner.py` | Source bundles, training transactions, dispatch and regular-training integration. |
| `notebooks/build_staged_colab_notebook.py` | Maintained Run-all entry point for queued evaluation or ordinary training. |

When extending evaluation, preserve frozen inputs, selection/evaluation separation, attempt ownership, recoverable completed stages and the boundary between evaluation results and model publication. Supply another versioned protocol package to add a study; stages, settings and reports come from the package. Update the shared guide and regression checks alongside changed behavior, then regenerate the maintained notebook after changes to bundled sources.

With the project environment activated, run the focused infrastructure checks from the repository root:

```sh
python -m unittest tests.test_model_evaluation tests.test_model_evaluation_ui tests.test_staged_colab_notebook tests.test_maintainer_workspace
```

Add protocol-specific checks for its label assumptions, matching rules, metrics and acceptance criteria. Cloud fixtures and mocked training do not establish successful GPU execution or model efficacy. Exercise the real training path on a separate software-check dataset before interpreting a study; preserve the study's reserved evaluation data. Do not launch native TEMPO instances automatically for validation.

For visual review, run `tests.test_model_evaluation_ui` with `TEMPO_EVALUATION_SCREENS` pointing to an ignored output directory. Inspect empty, populated, active and completed states at normal, wide, small-window and enlarged-font sizes. Long instructions belong in a labelled button's scrollable dialog; reserve information hovers for brief definitions or caveats. Package the [operator workflow](MODEL_EVALUATION_WORKFLOW.md) in `tempo.spec`; keep the developer reference outside the application's help dialog. A fresh checkout or packaged application must not depend on a local study document.

## Repository maintenance

Track tools required to build, install, operate, or reliably verify TEMPO. Keep one-off rewrites, personal probes, private cloud fixtures, and generated reports locally and untracked. Preview tools need a documented, repeatable purpose. Tests should protect meaningful behavior; removals and consolidation need a reviewable reason.

Run the [repository checks](CREDENTIAL_SECURITY.md#repository-checks) before committing. They inspect file boundaries, notebook outputs, private values, and credentials. Review matches instead of suppressing entire test directories.

## Live focus assistance and v1.1 validation

In Capture, integrate live focus assistance with the cell-counter experience. Alongside the healthy/dead cell estimate, show whether the assessed image appears sharp, out of focus, or cannot be assessed reliably. This is live aiming guidance for both manual microscope operation and TEMPO-controlled acquisition. Assess sarcomeric detail together with cell-edge sharpness: apparently sharp edges alone must not certify that the imaging depth resolves intracellular detail. Cells without usable sarcomeric texture may provide insufficient evidence. Keep the assessment associated with the frame and time it describes; a preview check does not certify an entire recording.

Reuse the existing image-associated Z-index/depth recording rather than adding a parallel depth-entry workflow. Preserve whether a value was manually entered or obtained from hardware, and distinguish a Z index from a calibrated physical depth. A focus assessment supplements this metadata; it does not establish an absolute depth or authorize stage movement. Manual focusing must remain usable without motorized Z control.

When a flagged recording reaches analysis, alert the user and let them explicitly include or exclude it. Do not silently discard it or clear the quality flag when inclusion is chosen. Preserve the original assessment, reason, assessed frame/time coverage, method/version and settings, available Z metadata, recording identity, and subsequent inclusion decisions with actor/time and changes. Carry the flag and decision through saved analysis, affected per-cell/field measurements, and exported data. Summaries and reports must make included flagged recordings identifiable. Legacy recordings without an assessment remain unknown until assessed; missing metadata is not a sharp result. A later assessment or changed inclusion decision retains the earlier record.

The integration is implemented; see [recording storage and quality](CAPTURE_QUALITY.md). No calibrated focus profile ships with it. Focus calibration and validation, physical camera/stage/stimulation acceptance, native-interface/independent-collaborator acceptance, and modularization of `tempo_gui.py` into dedicated controller delegates are deferred to v1.1. These checks are unverified and do not block v1 publication; preserve their limitations in the release notes.

Before treating Sharp/Out of focus as reliable guidance, validate against separate operator-reviewed fields at different concentrations, including sharp-edged cells with poorly resolved sarcomeres or nuclei. Verify manual focusing, automatic collection, focus changes during recording and flag/decision persistence through save/reopen, reanalysis and export. Camera and physical-stimulation acceptance remain separate from offscreen software checks.

## v1.0.2 stimulation and video timing

Add a shared elapsed-time reference for stimulation events and recorded camera frames so each event can be located on the recording timeline. Keep this initial scope focused on timing records and clock alignment; pre-trigger buffering, pacing-response verification, latency analysis and additional external measurement channels remain separate capabilities.

- Record device-reported pulse-edge events with sequence identifiers and device timestamps, together with recording/frame identities and available camera timestamps. Distinguish requested commands, acknowledgements and reported output transitions; command receipt alone does not establish when a pulse occurred.
- Map the stimulator, camera and host clocks into one session timeline while retaining their original timestamps, units and timing-source semantics. Use exposure-related camera timing where available; explicitly identify host receipt timing and unsupported or uncertain mappings. Account for clock drift, device resets, timestamp rollover and missing events without silently inventing pulse/frame associations.
- Save the alignment information and measured timing uncertainty with recording metadata and preserve them through retention, processing, reopening and export. A shared display clock or software timestamp precision alone must not be presented as verified physical synchronization. Validate pulse-to-exposure alignment on the target hardware before making accuracy claims; keep timing collection bounded in background workers and preserve reachable stopping actions.

## v1.0.2 configurable outlier-beat inclusion

Give users an analysis option to include or exclude beats identified as outliers, with configurable definitions of an outlier. Apply the rules to detected beat measurements rather than treating the peak-detection threshold as an outlier policy.

- Let users choose the measured properties and criteria, such as amplitude, beat interval or contraction/relaxation duration, with explicit units, thresholds, rule combinations and reference population. Support absolute bounds and configurable deviation rules where the data supports them. Distinguish missing measurements or insufficient reference data from confirmed outliers.
- Preserve every detected beat's identity, original measurements and trace, the criteria that flagged it, and the chosen inclusion policy. Including flagged beats keeps their outlier status visible; excluding them changes the applicable summaries without deleting source data or inventing new beat intervals.
- Recalculate affected analysis summaries and show included, excluded and total beat counts. If no eligible measurements remain, report that state explicitly. Save the rules and policy with the analysis and carry flags, inclusion decisions and counts through reopening, reanalysis and export so results can be reproduced.

## v1.1 feature buildlist

This is an open working list, alongside the focus, hardware, collaborator-validation and controller-modularization work above and Windows automatic updates below. Feature scope will continue to evolve through the v1.0.x releases.

### Expanded theme colors and customizable fonts

Expand beyond Dark, Light and Oatmeal with a substantially larger selection of color themes. Let users choose the interface font family and size, preview their choices and restore defaults. Save appearance choices as personal preferences and apply them consistently across controls, dialogs, plots and help views, with a fallback when a chosen font is unavailable. Preserve readable contrast, meaningful status/selection colors and scientific color scales. Review rendered theme/font combinations at ordinary, wide and small-window sizes, including larger fonts, to keep text, controls and plot labels readable and reachable.

### Experiment workspace and editable plate layout

Organize recordings, analysis revisions and notes within an experiment. Assign treatments, concentrations and units, controls, preparation identities and replicate groups to wells. Build on existing recording/well/field identities and preserve the experimental design with results and exports rather than relying on filenames or a separate spreadsheet.

### Comparisons across conditions and experiments

Select control wells, compare treatment groups and concentration responses, and compare baseline with post-treatment recordings. Show individual-cell distributions alongside well and biological-replicate summaries, with grouping, pairing, normalization and inclusion decisions explicit. Preserve the comparison settings and source analysis revisions so comparisons can be reopened and reproduced.

Include linked population selection: plot any two available measurements, select cells by numerical ranges or drawn regions, and highlight the same cells in the spatial map and results table. Save named populations, selection rules, source revisions and membership with comparisons and exports. Preserve experimental treatment/replicate identities separately from these measurement-based selections.

### Reusable acquisition protocols

Save named acquisition sequences with well assignments, camera/recording settings, pre-pacing, pacing changes, settling intervals and operator checkpoints. Let users inspect the sequence before starting and repeat it across experiments. Record requested steps, acknowledged hardware settings, actual execution and interruptions with the acquired data; retain reachable stopping actions throughout execution.

### Repeated measurements at saved fields

Save named stage positions with reference images and optical calibration, revisit them after treatment or at scheduled intervals, and assist with image registration. Support paired field comparisons while recording elapsed time, alignment evidence and failed relocations. Use the same-cell tracking workflow below for individual-cell pairing; returning to a stage position alone does not establish cell identity.

### Same-cell tracking across recordings

Maintain persistent cell identities across baseline, treatment and recovery recordings. Show proposed matches over aligned reference images, provide matching confidence and manual correction, and explicitly identify unmatched or ambiguous cells. Preserve source recording/field/cell identities and matching revisions with paired comparisons and exports; missing or uncertain matches must not silently become confirmed pairs.

### Assisted focus recovery at revisited fields

With compatible motorized Z hardware, search a configured, bounded range around a saved position and assess intracellular detail using a validated focus profile. Present the proposed focus position for operator acceptance or apply it within an explicitly configured acquisition protocol. Preserve manual focusing, reachable stopping actions, requested and acknowledged stage positions, calibration/profile identity, and focus failures or overrides. Keep focus assessment distinct from authorization to move the stage and avoid certifying focus from cell-edge sharpness alone.

### Unified experiment quality review

Bring focus evidence, timing gaps, saturation, pacing interruptions, segmentation concerns and analysis exclusions into one experiment-level review. Link flagged results to the relevant image, trace or acquisition event, and distinguish unavailable evidence from a passing check. Reuse existing quality records and decision history, preserve reasons for inclusion/exclusion, and carry the reviewed state into comparisons and exports.

### Frame-linked video, trace and optical-flow review

When original or intermediate cell footage is retained, provide integrated playback and scrubbing linked to the measured trace. Selecting a trace time seeks to its corresponding video frame; playback moves the trace cursor. Show the selected cell's reference outline, available stimulation events and frame-associated optical-flow vectors. Support original-field and retained-cell views where available, and preserve frame/time correspondence across cropped footage, timing gaps and differing frame rates.

Compute or load optical-flow vectors from verified retained footage in background workers and retain their method, settings, frame associations and coordinate/scale information. Present these as an additional motion visualization alongside the existing MUSCLEMOTION signal. Use calibrated units only where supported; missing footage, timing or flow evidence must remain explicit. The existing recorded-signal view remains usable without footage, and optical-flow visualization does not replace fixed-reference intensity measurements.

### Pacing-response verification

Compare recorded stimulation events with observed optical contractions. Report pulses followed by a response, missed responses and extra contractions with supporting traces, assessment coverage and uncertainty. Carry the evidence into experiment quality review and allow operator checkpoints when response criteria are not met. Automatic stimulation adjustment is a separate extension; an optical response assessment does not directly measure electrical activation.

### Synchronized recording and pre-trigger buffering

Build on the shared stimulation/video timeline planned for v1.0.2 and retain a configurable interval of acquired frames before a recording trigger. Preserve device events, frame identities, exposure/timestamp semantics, clock relationships and measured timing uncertainty. Support hardware synchronization where available and assess software synchronization against the actual camera/stimulator configuration; host receipt times and serial round-trip estimates alone do not establish exposure-to-pulse accuracy. Keep buffering and event collection in background acquisition workers.

### Stimulus-to-contraction latency

Measure the delay from a recorded stimulus to observed contraction onset and compare distributions across cells, treatments and pacing frequencies. Preserve the onset method, event association, timing evidence and uncertainty with each measurement. Report unavailable or ambiguous associations explicitly, and distinguish stimulus-to-motion latency from electrical-activation-to-contraction delay. Sub-frame interpolation must not be presented as independently validated submillisecond or microsecond accuracy.

### Mechanical activation and synchrony maps

Map contraction onset across cells or image regions, visualize the spread of mechanical activity and summarize response synchrony. Link spatial results to the underlying traces, calibrated coordinates, event associations and timing uncertainty. Report unresolved delays and missing responses explicitly. Electrical conduction interpretations require separate validation; mechanical activation maps alone do not establish electrical conduction velocity or conduction block.

### Sarcomere length and shortening analysis

Where striations are sufficiently resolved, measure resting sarcomere length, shortening fraction and shortening/relengthening rates alongside existing motion measurements. Show the measured region, sarcomere-length trace and signal-quality evidence. Preserve spatial calibration, orientation/region selection, analysis method/settings and timing provenance with results and exports. Report unresolved striations or unreliable measurements explicitly, and validate against reference measurements before interpreting the new metrics scientifically.

Estimate the spatial spacing of repeated sarcomeric bands in each assessed frame and convert pixel spacing using a measured calibration matched to the optical configuration. This provides image-plane sarcomere spacing rather than a conversion of MUSCLEMOTION intensity amplitude into length. Account for orientation and unresolved/out-of-plane structure; do not infer whole-cell length or force from band spacing alone.

## v1.1 Windows automatic updates

Add an updater for packaged Windows installations so users can receive application updates inside TEMPO instead of manually downloading each installer from GitHub Releases.

- Detect compatible releases from the selected stable or prerelease channel, download the installer, and verify its trusted source, version, and checksum before installation. Let users defer installation and confirm the required restart.
- Wait for acquisition, analysis, model assistance, and pending saves to finish before closing TEMPO for an update. Preserve accounts, preferences, annotation drafts, models, recordings, and analysis outputs; coordinate application updates with the matching optional NVIDIA runtime.
- Handle interrupted downloads and failed installation with a usable recovery path, clear errors, and a manual installer fallback. Verify upgrades from v1.0.x and prerelease installations, subsequent automatic upgrades, preserved user data, and failure/cancellation cases.

Existing v1.0.x installations will need one manual installer upgrade to receive the updater. Changing a GitHub release from prerelease to stable does not add automatic updating to an already installed application.

## Windows builds and publication

The Windows workflow is **manual**. Pushing commits or tags does not publish a release. Push the intended revision, then run **Build & Package Windows Release** in GitHub Actions with that branch selected.

The workflow builds and verifies the executable and installer, then tests installation, offline operation, and uninstall on a separate Windows runner. See [Windows packaging](PACKAGING_AND_DISTRIBUTION.md) for artifacts and required checks.

Release publication is a separate manual operation; the build workflow does not publish releases or create tags.

## Annotation identity repair

The maintained annotation-identity repair tool prepares a read-only reconciliation plan for historical aliases. Applying requires a maintainer, a closed application, explicit reservation names, and an unchanged remote revision. Read its `--help` before preparing a repair.
