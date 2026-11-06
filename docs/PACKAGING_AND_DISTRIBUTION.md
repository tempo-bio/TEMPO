# Windows packaging

TEMPO is bundled as a 64-bit Windows executable with PyInstaller and distributed through an Inno Setup installer. Python and the locked application runtimes are included. Developer preferences and account credentials are not part of the declared payload.

For installation instructions and unsigned-installer guidance, see [Getting started](GETTING_STARTED.md#install-on-windows).

## Run a test build

Push the intended revision. In GitHub Actions, select **Build & Package Windows Release → Run workflow**, then select the branch to test.

The workflow accepts manual dispatch only, has read-only repository-content permission, and contains no release-publication step. Successful builds produce workflow artifacts, without creating a release or tag.

The runner uses `windows-2025`, Python 3.13.16 x64, hash-pinned dependencies, and a checksum-verified Inno Setup compiler. This is the automated verification platform, not evidence that every collaborator's Windows or camera configuration has been tested.

## Required checks

The build requires repository privacy/file checks, dependency consistency checks, notebook regeneration, the regression/scientific suite, installer failure injections, interface renders at three display/font profiles, and source-runtime verification. Packaging also checks application-owned files for unintended local data and includes dependency license/notice files with an inventory.

The actual packaged executable repeats the runtime checks. These include native model imports, credential-store write/read/delete with an isolated synthetic entry, offscreen startup and shutdown, plot lifecycle, scientific processing and export, and combined native-library initialization.

A separate Windows runner downloads the exact Inno installer without a source checkout or Python setup. It verifies the checksum, installs in a path containing spaces, checks the installed app offline with an isolated user environment, and uninstalls it.

The release workflow also requires the [disabled full-app Velopack candidate checks](../installer/velopack/README.md#full-application-candidate-automation). It packages the complete tested CPU folder, verifies native delta reconstruction against full inventories, and transfers the exact artifacts to another fresh runner. That runner checks installation, same-source replacements, simulated independent recovery, published v1.0.1 CPU migration, component refusal and uninstall. Its report binds the accepted artifact hashes. All existing build gates and both installer paths must succeed for the final build check. This is not Velopack release-adoption certification: signing/trust, distinct releases, component compatibility, the complete update transaction matrix and hardware acceptance still apply, and in-app updates remain disabled.

These checks do not connect to microscope hardware, use real collaborator accounts, or run Colab GPU training.

## Artifacts

| Artifact | Contents | Retention |
| --- | --- | --- |
| `Windows_install_payload` | Installer, SHA-256 checksum, source/dependency provenance, dependency source index, verification contract, and installation-check scripts | 3 days |
| `Windows_NVIDIA_runtime` | Optional CUDA 12.6 runtime parts and the descriptor pinned into the installer | 7 days |
| `TEMPO_Colab_Trainer` | Notebook regenerated from tested training sources | 7 days |
| `Windows_test_evidence` | Regression output and machine-readable results | 7 days |
| `Windows_prebuild_evidence` | Source checks and interface captures | 3 days |
| `Windows_bundle_evidence` | Packaged-runtime checks and bundler diagnostics | 3 days |
| `Windows_build_evidence` | Collected build reports, logs, and captures | 7 days |
| `Windows_install_evidence` | Fresh-runner installation, runtime, and uninstall reports | 7 days |
| `velopack-production-feature-candidate` | Production-feature updater, source/patch identity and native fixture linkage; unverified adoption pin | 7 days |
| `Windows_velopack_candidate_payload` | Three same-source full-app packaging revisions, original setup executables, deltas, test companion, public test key and exact artifact contract | 7 days |
| `Windows_velopack_candidate_evidence` | Installed diagnostics, replacement/recovery/migration/uninstall results and exact artifact acceptance gate | 7 days |

Download the payload before expiry. Keep the installer, checksum, and provenance together. The filename `TEMPO_Setup_v1.0.1.exe` alone does not identify which test build produced it. The workflow does not publish release assets.

Velopack candidate artifacts use disposable test trust and must not be distributed to collaborators or uploaded to the update feed. Expiring workflow artifacts do not satisfy durable retention for a user release or its repair packages.

The NVIDIA runtime is split into sequential `TEMPO_NVIDIA_Runtime_v1.0.1.zip.part001`, `.part002`, etc., each no larger than 1 GiB. Publish **every part named by `nvidia-runtime.json`** to the matching release tag, preserving filenames. Publish that descriptor alongside them for maintainers. Do not upload the outer Actions artifact ZIP or concatenate the parts into a single release asset: the complete runtime exceeds GitHub's individual file-size limit. Before publication, verify the downloaded artifact with:

```powershell
python -m scripts.package_nvidia_runtime --verify-descriptor path/to/nvidia-runtime.json --commit FULL_TESTED_COMMIT
```

Packaging verifies part sizes, ordered names, individual and combined checksums, and the worker executable. The CPU build repeats this check before bundling the descriptor. Guided Setup downloads and verifies all parts automatically, shows aggregate progress, then checks the GPU before activation. Users do not extract or combine parts themselves. They can opt in during first setup or later through **Settings → Application Tools → Guided Setup**.

After uploading the assets to a public prerelease, verify the published part sizes, GitHub checksums, download URLs, and source tag before marking the release stable:

```powershell
python -m scripts.package_nvidia_runtime --verify-published path/to/nvidia-runtime.json --commit FULL_TESTED_COMMIT
```

Users of `v1.0.1-preview.1` need one manual installation of the updated installer to receive multipart download support. An existing runtime remains selected only when its identity matches the installed application; setup obtains the runtime built for the new installer. Changing the GitHub prerelease flag does not update an installed application.

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

For Velopack, follow the [Windows update release procedure](WINDOWS_UPDATE_RELEASE.md) to retain signed release sets, verify a copy recovered from durable storage, coordinate signing-key transitions and check served artifacts before advertising the feed. This gate is separate from the disabled candidate workflow and requires reviewed full-app/hardware evidence and a Windows signing decision. The retention and publication-check tools perform no uploads or feed writes.

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
