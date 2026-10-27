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

## Windows builds and publication

The Windows workflow is **manual**. Pushing commits or tags does not publish a release. Push the intended revision, then run **Build & Package Windows Release** in GitHub Actions with that branch selected.

The workflow builds and verifies the executable and installer, then tests installation, offline operation, and uninstall on a separate Windows runner. See [Windows packaging](PACKAGING_AND_DISTRIBUTION.md) for artifacts and required checks.

Release publication is a separate manual operation; the build workflow does not publish releases or create tags.

## Annotation identity repair

The maintained annotation-identity repair tool prepares a read-only reconciliation plan for historical aliases. Applying requires a maintainer, a closed application, explicit reservation names, and an unchanged remote revision. Read its `--help` before preparing a repair.
