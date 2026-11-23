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
& .\.venv\Scripts\python.exe -m scripts.render_ui_screens --output .agent-artifacts/screens --font-size 10
```

The renderer uses isolated preferences and simulated hardware and remote services. Review screenshots alongside tests. Generated captures and diagnostics stay outside tracking; local investigation evidence belongs in ignored `.agent-artifacts/`.

## Notebook generation

After changing training or Colab runtime sources, regenerate the maintained notebook:

```powershell
& .\.venv\Scripts\python.exe -m notebooks.build_staged_colab_notebook
```

The notebook embeds the explicit source-file list defined by the cloud bundle. The builder performs no training and needs no account credentials. Keep the default notebook tracked without saved outputs or execution counts. To update the shared Colab copy or export a notebook for a different workspace, follow [Replace the notebook](TRAINING_WORKFLOW.md#replace-the-notebook-when-needed).

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

## Acquisition, focus, and timing

`tempo_gui.CaptureTab` owns the Acquisition view. `tempo_capture_ui` coordinates camera tasks, field collection, and stimulation. `tempo_focus_calibration` and `tempo_quality` fit profiles and store versioned focus evidence. `tempo_recording` owns native frames and timing, and `tempo_analysis_io` carries quality decisions into saved and exported results. Preserve frame and time coverage, the original assessment, and the decision history when changing any of those interfaces. [Recording and focus workflow](CAPTURE_QUALITY.md)

`tempo_stimulation` handles firmware acknowledgements and reported output transitions. A command acknowledgement establishes receipt. Pulse timing comes from the device's reported edges and the available camera timestamps. Keep hardware I/O in workers and preserve original clock and timing-source metadata. Verify pulse-to-exposure alignment on hardware before making timing claims. [Stimulation and capture](STIMULATION_SETUP.md#confirmation-stopping-and-data)

## Windows application updates

`tempo_update_manifest` validates signed release metadata and builds the transfer plan. `tempo_update_service` verifies downloads and cached packages. `tempo_update_coordinator` prepares the matching NVIDIA runtime when needed and requests a normal dashboard shutdown before handing off replacement. The dashboard's close guards cover acquisition, processing, and pending writes. `tempo_update_recovery` owns offline repair and confirms the replacement after desktop startup. `tempo_legacy_migration` handles registered Inno installations through the repair companion. [Upgrade and recovery](GETTING_STARTED.md#upgrade-tempo-c)

When changing the update path, test an intact delta, a failed delta with full fallback, restart while work is active, and startup confirmation after replacement. Keep the original package and recovery record available until confirmation succeeds. To check a release candidate, use the published package for the supported base version and the exact target installer and packages from the assembly run. [Windows update release procedure](WINDOWS_UPDATE_RELEASE.md)

## Windows builds and publication

Run **Build & Package Windows Release** in GitHub Actions on the branch containing the intended revision. Supply the application version as `release_tag`, including the leading `v`. Review the executable, installation, offline-operation, and uninstall checks before using its artifacts. Use the release procedure to assemble the updater-enabled installer and verify it before publication. [Windows packaging](PACKAGING_AND_DISTRIBUTION.md) · [Windows update release procedure](WINDOWS_UPDATE_RELEASE.md)

## Annotation identity repair

Run `python -m scripts.repair_annotation_identities --help` from the source checkout. The default command prepares a read-only reconciliation plan for historical aliases. Applying requires a maintainer, a closed application, explicit names for any reservations being released, and an unchanged remote revision.

## Planned work

The source checkout records proposed v1.1 work in `docs/DEVELOPMENT_ROADMAP.md`.
