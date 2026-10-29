# Windows packaging

TEMPO is bundled as a 64-bit Windows executable with PyInstaller and distributed through an Inno Setup installer. Python and the locked application runtimes are included. Developer preferences and account credentials are not part of the declared payload.

For installation instructions and the consequences of the unsigned v1.0.0 installer, see [Install on Windows](../README.md#install-on-windows).

## Run a test build

Push the intended revision. In GitHub Actions, select **Build & Package Windows Release → Run workflow**, then select the branch to test.

The workflow accepts manual dispatch only, has read-only repository-content permission, and contains no release-publication step. Successful builds produce workflow artifacts, without creating a release or tag.

The runner uses `windows-2025`, Python 3.13.16 x64, hash-pinned dependencies, and a checksum-verified Inno Setup compiler. This is the automated verification platform, not evidence that every collaborator's Windows or camera configuration has been tested.

## Required checks

The build requires repository privacy/file checks, dependency consistency checks, notebook regeneration, the regression/scientific suite, installer failure injections, interface renders at three display/font profiles, and source-runtime verification. Packaging also checks application-owned files for unintended local data and includes dependency license/notice files with an inventory.

The actual packaged executable repeats the runtime checks. These include native model imports, credential-store write/read/delete with an isolated synthetic entry, offscreen startup and shutdown, plot lifecycle, scientific processing and export, and combined native-library initialization.

A separate Windows runner downloads the exact installer without a source checkout or Python setup. It verifies the checksum, installs in a path containing spaces, checks the installed app offline with an isolated user environment, and uninstalls it. Final certification requires both jobs to succeed.

These checks do not connect to microscope hardware, use real collaborator accounts, or run Colab GPU training.

## Artifacts

| Artifact | Contents | Retention |
| --- | --- | --- |
| `Windows_install_payload` | Installer, SHA-256 checksum, source/dependency provenance, dependency source index, verification contract, and installation-check scripts | 3 days |
| `Windows_NVIDIA_runtime` | Optional CUDA 12.6 inference runtime archive and the descriptor pinned into the installer | 7 days |
| `TEMPO_Colab_Trainer` | Notebook regenerated from tested training sources | 7 days |
| `Windows_test_evidence` | Regression output and machine-readable results | 3 days |
| `Windows_prebuild_evidence` | Source checks and interface captures | 3 days |
| `Windows_bundle_evidence` | Packaged-runtime checks and bundler diagnostics | 3 days |
| `Windows_build_evidence` | Collected build reports, logs, and captures | 7 days |
| `Windows_install_evidence` | Fresh-runner installation, runtime, and uninstall reports | 7 days |

Download the payload before expiry. Keep the installer, checksum, and provenance together. The filename `TEMPO_Setup_v1.0.1.exe` alone does not identify which test build produced it. The optional NVIDIA runtime archive and its descriptor are separate workflow artifacts; the workflow does not publish them to the Releases page. Guided Setup can download the runtime only after that exact checksummed archive is published at the descriptor URL.

## Installation, models, and hardware

The default per-user directory is `%LOCALAPPDATA%\Programs\TEMPO`. The installer can create Start menu and desktop shortcuts. A separate Python installation is unnecessary.

Current packaging includes the runtime libraries needed by the model checks. Project weights are included only when their declared files are present in the build directory. Otherwise TEMPO's authenticated update path obtains them from the configured private model repository. Passing an offline runtime check does not establish that trained project weights are bundled.

Scientific-camera acquisition requires suitable vendor drivers and Micro-Manager adapters. Annotation of existing images needs no connected camera. Check camera compatibility on the actual hardware.

## Startup diagnostics

For builds containing the startup recorder, run these commands in Command Prompt:

```bat
cd /d "%LOCALAPPDATA%\Programs\TEMPO"
TEMPO.exe --diagnose-startup "%TEMP%\tempo-startup.json"
```

This performs a normal launch with the saved account and preferences. The report records HTTP status and byte counts for shared settings, JSON validation, settings application, and dashboard creation. While startup is pending it captures Python thread locations every ten seconds, for up to ten minutes. It excludes credentials, settings values, response contents, and local variable values. Recording stops when the dashboard is shown. Share the JSON report after a stalled launch; this option is absent from the original v1.0.0 installer.

The read-only `scripts/collect_windows_settings_cache.ps1` can inspect an existing installation without a new build. It reports the cached settings file's dates, hash, schema checks, and whether the four workspace values match saved preferences. It performs no network requests and never reads credentials. Cache inspection alone cannot establish which launch downloaded or applied a file.

## Publication

Review source, licensing, privacy findings, and the exact tested installer before public distribution. Keep publication manual and separate from build certification. Distribute the installer from the approved revision rather than rebuilding a different revision under the same filename.

### Matching source

Link each installer to the matching TEMPO source revision, including the build scripts and Windows dependency lockfile. Keep that source available alongside the installer. Include the generated `dependency-sources` directory with the download: it provides version-specific upstream source links and checksums. The installed application also contains the index under `_internal/third-party-sources`.

`installer/dependency-sources.json` records reviewed source archives for copyleft dependencies and selected native/build components. It is tied to the Windows lockfile. Changes to the lockfile require another source review; package-level license names alone do not identify every library embedded in a wheel. In particular, OpenCV's Windows FFmpeg plugin and Shapely's GEOS library have versions separate from their Python packages.

Before distributing a build, download and verify the reviewed upstream sources into a retained local directory:

```powershell
& .\.venv\Scripts\python.exe -m scripts.prepare_dependency_sources --fetch --cache .agent-artifacts/dependency-source-cache --output .agent-artifacts/dependency-source-index
```

Use `--verify-cache` instead of `--fetch` to check retained copies without network access. Downloads are source archives from upstream projects; the tool does not extract or execute them. This command does not copy TEMPO's repository or Git history.

The Windows workflow checks index consistency against the dependency lock and build license inventory, bundles the index, and records its checksum in build provenance. It does not download the large source archives. A passing Windows build therefore establishes index consistency, not continued availability of upstream downloads. Retain verified copies for as long as the release's source obligations apply; if an upstream link stops working, provide the matching retained source through the release download location and update the directions.

LGPL libraries remain replaceable files in the installation. Qt libraries are under `_internal/PyQt6/Qt6`; GEOS is under `_internal/shapely.libs`; the Windows FFmpeg plugin is under `_internal/cv2`. The upstream source archives include their build instructions; the OpenCV plugin entry includes the exact wrapper build recipe and its source dependencies. Replacement libraries must preserve the interfaces expected by the application.
