# Windows packaging

TEMPO is bundled as a 64-bit Windows executable with PyInstaller. The general build produces an Inno Setup test installer. Published Windows releases use a separately assembled Velopack installer for signed in-app updates. Both include Python and the locked application runtimes.

For installation instructions and unsigned-installer guidance, see [Getting started](GETTING_STARTED.md#install-on-windows).

## Run a test build

1. In GitHub Actions, open **Build & Package Windows Release → Run workflow** and select the branch with the intended revision.
2. Enter the application version from `tempo_release.VERSION` as `release_tag`, including the leading `v`. Leave the assembly and signed-trial inputs empty for a test build.
3. Review the checks and download `Windows_install_payload` before it expires. Keep its checksum and provenance alongside the installer. Use the completed build run ID and full commit for any later stable-channel assembly. [Windows update release procedure](WINDOWS_UPDATE_RELEASE.md)

The runner uses `windows-2025`, Python 3.13.16 x64, hash-pinned dependencies, and a checksum-verified Inno Setup compiler. This is the automated verification platform, not evidence that every collaborator's Windows or camera configuration has been tested.

## Required checks

The build requires repository privacy/file checks, dependency consistency checks, notebook regeneration, the regression/scientific suite, installer failure injections, interface renders at three display/font profiles, and source-runtime verification. Packaging also checks application-owned files for unintended local data and includes dependency license/notice files with an inventory.

The actual packaged executable repeats the runtime checks. These include native model imports, credential-store write/read/delete with an isolated synthetic entry, offscreen startup and shutdown, plot lifecycle, scientific processing and export, and combined native-library initialization.

The Windows workflow produces a separate **Stimulation Investigation** ZIP. The
base executable excludes the toolkit. The release check installs the approved ZIP
into an isolated profile, starts the Investigation mode in the built executable,
stages the firmware, checks simulated records and export, and removes the package.
The required workflow in `.github/workflows/stimulation-firmware.yml` compiles
standard pacing and investigation firmware for the Uno R4 Minima using TEMPO's
pinned toolchain.

### Optional package compatibility

Packages use a versioned host interface and live in the user profile across
application updates. Preserve support for existing package API versions and their
approved archive hashes when shipping a new TEMPO version. Changes to Acquisition,
serial transport, and the pacing sketch must pass the same package verification
before release. Keep existing investigation JSON and attachment folders readable.

Build the reproducible ZIP with
`python -m scripts.build_optional_package --output Output/optional-packages`.
If the package source changes, review the diff, increment its release version,
and use `--print-hash` to obtain the hash for the new version's catalog entry in
`tempo_optional_packages.py`. Retain prior entries and test an installed earlier
package before changing the host interface. Distribute the verified ZIP as a
separate download. The ZIP includes the installation and collection guide.

A separate Windows runner downloads the exact Inno test installer without a source checkout or Python setup. It verifies the checksum, installs in a path containing spaces, checks the installed app offline with an isolated user environment, and uninstalls it.

The workflow also runs the full-app Velopack candidate checks described in `installer/velopack/README.md` in the matching source checkout. It packages the tested CPU folder, verifies native delta reconstruction against full inventories, and transfers the exact artifacts to a fresh runner. That runner checks installation, same-source replacements, simulated independent recovery, and uninstall. The reports bind the tested artifact hashes. Candidate builds use disposable trust. The [release procedure](WINDOWS_UPDATE_RELEASE.md) covers production signing, update acceptance, and verification of the published installer.

These checks do not connect to microscope hardware, use real collaborator accounts, or run Colab GPU training.

## Artifacts

| Artifact | Contents | Retention |
| --- | --- | --- |
| `Windows_install_payload` | Installer, SHA-256 checksum, source/dependency provenance, dependency source index, verification contract, and installation-check scripts | 3 days |
| `Windows_NVIDIA_runtime` | Optional CUDA 12.6 runtime parts and the descriptor pinned into the installer | 7 days |
| `Stimulation_Investigation_Optional_Package` | Installable Stimulation Investigation ZIP and its packaged-executable verification report | 7 days |
| `TEMPO_Colab_Trainer` | Notebook regenerated from tested training sources | 7 days |
| `Windows_test_evidence` | Regression output and machine-readable results | 7 days |
| `Windows_prebuild_evidence` | Source checks and interface captures | 3 days |
| `Windows_bundle_evidence` | Packaged-runtime checks and bundler diagnostics | 3 days |
| `Windows_release_bundle` | Exact verified CPU folder and versioned verification reports for stable-channel assembly | 7 days |
| `Windows_build_evidence` | Collected build reports, logs, and captures | 7 days |
| `Windows_install_evidence` | Fresh-runner installation, runtime, and uninstall reports | 7 days |
| `velopack-production-feature-candidate` and `velopack-native-candidate-evidence` | Native updater binaries, source and patch identities, and fixture evidence for release assembly | 7 days |
| `Windows_velopack_candidate_payload` | Three same-source full-app packaging revisions, original setup executables, deltas, test companion, public test key and exact artifact contract | 7 days |
| `Windows_velopack_candidate_evidence` | Installed diagnostics, replacement, recovery, uninstall, and exact artifact acceptance results | 7 days |
| `Windows_velopack_candidate_replay_evidence` | Independent replay of the same candidate packages on a fresh runner | 7 days |

Download the payload before expiry. Keep the installer, checksum, and provenance together. Identify a test build by its source commit and installer checksum. Publication is a separate step.

Velopack candidate artifacts use disposable test trust and must not be distributed to collaborators or uploaded to the update feed. Expiring workflow artifacts do not satisfy durable retention for a user release or its repair packages.

The NVIDIA runtime is split into sequential parts, each no larger than 1 GiB. Publish **every part named by `nvidia-runtime.json`** to the matching release tag, preserving filenames. Publish that descriptor alongside them for maintainers. Do not upload the outer Actions artifact ZIP or concatenate the parts into a single release asset: the complete runtime exceeds GitHub's individual file-size limit. Before publication, verify the downloaded artifact with:

```powershell
python -m scripts.package_nvidia_runtime --verify-descriptor path/to/nvidia-runtime.json --commit FULL_TESTED_COMMIT
```

Packaging verifies part sizes, ordered names, individual and combined checksums, and the worker executable. The CPU build repeats this check before bundling the descriptor. Guided Setup downloads and verifies all parts automatically, shows aggregate progress, then checks the GPU before activation. Users do not extract or combine parts themselves. They can opt in during first setup or later through **Settings → Application Tools → Guided Setup**.

After uploading the assets to a public prerelease, verify the published part sizes, GitHub checksums, download URLs, and source tag before marking the release stable:

```powershell
python -m scripts.package_nvidia_runtime --verify-published path/to/nvidia-runtime.json --commit FULL_TESTED_COMMIT
```

Guided Setup installs the runtime built for the installed application version. When testing an upgrade, check that the new runtime is selected and verify inference on the target GPU.

## Installation, models, and hardware

Published Velopack installers use `%LOCALAPPDATA%\TEMPO.Bio.Desktop`, with the executable under `current`. The Inno test installer defaults to `%LOCALAPPDATA%\Programs\TEMPO`. Both include Python and can create Start menu and desktop shortcuts.

Current packaging includes the runtime libraries needed by the model checks. Project weights are included only when their declared files are present in the build directory. Otherwise TEMPO's authenticated update path obtains them from the configured private model repository. Passing an offline runtime check does not establish that trained project weights are bundled.

Scientific-camera acquisition requires a compatible vendor driver and a Micro-Manager adapter or DirectShow connection. Windows also supports compatible USB and HDMI capture devices through DirectShow. Check the intended camera and driver on the actual hardware. [Camera setup](GETTING_STARTED.md#connect-a-camera)

## Startup diagnostics

If the installed application starts slowly or stalls, reproduce the problem with the startup recorder. Run these commands in Command Prompt:

```bat
cd /d "%LOCALAPPDATA%\TEMPO.Bio.Desktop\current"
TEMPO.exe --diagnose-startup "%TEMP%\tempo-startup.json"
```

For an Inno test build with startup diagnostics, run the same command from its installation directory. The command performs a normal launch with the saved account and preferences. The report records HTTP status and byte counts for shared settings, JSON validation, settings application, and dashboard creation. While startup is pending it captures Python thread locations every ten seconds for up to ten minutes. It omits credentials, settings values, response contents, and local variable values. Recording stops when the dashboard is shown. Share the JSON report after a stalled launch.

For workspace access and shared-settings failures, use the refresh and account checks in [Workspace administration](WORKSPACE_ADMINISTRATION.md#change-or-recover-shared-settings).

## Publication

Review source, licensing, privacy findings, and the exact tested installer before public distribution. Keep publication manual and separate from build certification. Distribute the installer from the approved revision rather than rebuilding a different revision under the same filename.

Follow the [Windows update release procedure](WINDOWS_UPDATE_RELEASE.md) to retain signed release sets, verify a copy recovered from durable storage, coordinate signing-key transitions and check served artifacts before advertising the feed. Release acceptance requires reviewed full-app and hardware evidence and a Windows signing decision. The automated candidate build has its update feed disabled. The retention and publication-check tools perform no uploads or feed writes.

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
