# Windows updater candidate

The patched Velopack 1.2.161 updater is **unverified**. The tracked pin and `installer/tempo-updates.json` keep in-app updates disabled. The Windows fixture, a certified native updater, a certified independent repair executable, legacy migration, and the full application installation and preservation matrix must all pass before release adoption. Do not publish artifacts from this candidate workflow.

## Build and review the native candidate

The source is Velopack 1.2.161, commit `92d6a1c91716729d449034df5c50307dcce39493`, under the accompanying MIT license. The patch refuses replacement on unconfirmed process waits, retains the previous payload, and attempts restoration if activation fails. It checks for running package processes before lifecycle hooks and before replacement; hook cleanup reports remaining processes without terminating them. Uninstall also refuses active work and unsettled hooks. It retires the application directory to a unique sibling before scheduling removal, so delayed cleanup cannot target a replacement at the original path. A separate companion provides offline repair and confirmation. Process enumeration alone does not prove all processes have exited; the application lease and native behavior require Windows review.

On an isolated Windows builder, obtain a clean checkout of the pinned source, then run:

```powershell
python scripts/prepare_velopack_updater.py <velopack-checkout>
Push-Location <velopack-checkout>
cargo build --locked --release -p velopack_bins --bin update --bin setup --features windows --target x86_64-pc-windows-msvc
Pop-Location
```

The preparation script verifies the commit, clean tree, upstream source hashes and patch digest. The `velopack-adoption` workflow builds both setup and updater with production features, then separately builds the fault-injection pair. It exercises the actual production pair as well as the fault fixtures on disposable installations. Review the Windows results and the adoption plan's acceptance matrix before marking the release pin `certified` and recording the production updater SHA-256. Keep the exact production binary and its evidence. The [release assembly procedure](../../docs/WINDOWS_UPDATE_RELEASE.md#assemble-and-smoke-test-the-stable-channel-installer) accepts the reviewed pin separately from the tested source.

The signed package inventory covers every ZIP member. Verification of the installed `current` directory uses the pinned extractor's layout: `Squirrel.exe` and `*_ExecutionStub.exe` remain installation-owner files and are excluded from that directory. Offline repair uses the same layout. The root `Update.exe` is verified separately against the signed `lib/app/Squirrel.exe` checksum before apply; missing or changed application files still fail verification.

## Full application candidate automation

`release-windows.yml` calls the native workflow at the same TEMPO revision, then packages the complete CPU PyInstaller folder after the existing source, privacy, licensing, NVIDIA-descriptor and frozen-runtime gates. `scripts/build_velopack_candidate.py` verifies the pinned VPK archive, the native candidate identity and every file from the tested folder. It explicitly runs the real frozen executable's four lifecycle hooks with bounded offscreen environments before packaging, and leaves VPK's compatibility checking enabled. VPK's managed-code inspection alone cannot establish Python lifecycle support. The production-feature updater reconstructs each generated delta; its complete target inventory must match the independently packaged full target.

The workflow retains three immutable `1.0.2-velopack.1` through `.3` packaging revisions and each original setup executable. All three contain the **same source and executable version**; they exercise replacement mechanics, not distinct TEMPO release acceptance. These candidate package identities do not change the maintained application release version. The completion marker, `candidate-contract.json`, binds the source commit, updater, companion, installers, full packages, installed inventories and reconstructed deltas. A failed build does not write that marker.

The independent test companion embeds a fresh public test key. Its private key exists only in builder memory, is never exported, and cannot sign a user release. The full application retains the tracked disabled configuration and empty trust store. These artifacts must not be published or used to replace a collaborator's installation.

The initial full-app matrix and its sequential replay use separate fresh Windows runners with the same immutable installers/packages. Neither runner checks out source, sets up Python/.NET, or installs application dependencies. Both run:

```powershell
./payload/verify_velopack_install.ps1 -Mode matrix -Payload payload -Evidence evidence
./payload/verify_velopack_install.ps1 -Mode migration -Payload payload -Evidence evidence
./payload/verify_velopack_install.ps1 -Mode uninstall -Payload payload -Evidence evidence
./payload/verify_velopack_install.ps1 -Mode gate -Payload payload -Evidence evidence
```

The matrix installs into an isolated path containing spaces and non-ASCII text, verifies every installed file, checks registration and shortcuts, and repeats the real bundled diagnostics after fresh installation, two replacements, a skipped packaging revision, independent offline forward repair and a further native update after repair. It also reinstalls immediately after the native uninstaller returns. Fixture preferences, onboarding state, drafts, models, recording and analysis bytes are checked after each transaction and uninstall. Diagnostic entrypoints do not establish that the interactive desktop loaded those saved values.

The repair scenario removes `current` and `Update.exe`, damages the native pending marker, and requests forward repair. It verifies the restored second revision, corrupts its updater, and repeats repair without copying an updater back manually. The companion restores the owner from the selected release's signed `lib/app/Squirrel.exe` inventory, retains damaged owner bytes, and quarantines obsolete native markers/backups without deleting them. Completion and confirmation require the exact authenticated owner. After diagnostics, the fixture uses that owner to install the third revision. These are simulated damage checks, not power-loss or complete installation-directory/registry reconstruction. Their Windows evidence remains required before certification.

The builder compiles disposable v1.0.0 and v1.0.1 Inno installers with the legacy registration and shortcut layout. The migration checks use those installers and their real Inno uninstallers with the candidate repair companion. Each case checks original draft and migrated image bytes, relocated image references, retained model bytes, shortcut removal, and refusal to finish while the old app is registered. It also requires refusal of optional-package receipts and an unreviewed NVIDIA selection. The gate verifies both migration reports and binds the installer, full package, and fixture hashes to the candidate contract.

Native adoption, distinct-release and dependency-changing transactions, signed feed downloads/fallback, NVIDIA/add-on migration, user-state interpretation, real interruption boundaries and physical hardware acceptance remain separate gates. Neither a successful candidate job nor its `release_certified: false` report changes the tracked native pin or enables updates. Actions artifacts last seven days; they are temporary test transport, not durable release/recovery storage.

To repeat the full-app matrix after a verification-script correction, dispatch `velopack-adoption.yml` on the reviewed development revision with `full_app_source_run_id` set to the completed Windows build run. Its `velopack-full-app.yml` job downloads that run's original immutable candidate payload and uses only the checked-out verification scripts, without installing application dependencies or rebuilding packages. Evidence records both source and fixture commits plus the verifier hash; a replay does not certify the newer application's source. The fixture creates and resolves Windows shell folders in a fresh child after isolating its profile, checks actual shortcut targets, and retains native setup/update logs. Omit the replay input for normal native fixture validation.

The patched uninstaller schedules detached cleanup only for its uniquely retired directory. It uses the Windows system shell with AutoRun and delayed expansion disabled. Directory names pass through environment values. The fault-injection fixture holds cleanup until Setup finishes reusing the original path, then releases cleanup and verifies that the replacement's owner and complete app inventory remain intact. The production binaries exercise uninstall and reinstall with normal cleanup timing. Existing verification helpers also refuse reuse while an older updater's literal-path cleanup command is pending. Concurrent installers and physical hardware shutdown remain separate acceptance gates.

Setup/repair, apply and uninstall acquire the per-user desktop/worker lease before a stable sibling installation lock. Destructive companion operations and migration use the same order; preparation and confirmation take the installation/package locks while TEMPO may remain open. Startup package maintenance runs under the application, installation and package locks. The installation lock stays outside the replaceable tree and is never unlinked. Native start uses that lock to protect process creation and refuses automatic Squirrel migration; TEMPO's reviewed Inno migration remains a separate companion operation. Setup refuses active work and pending native recovery rather than force-stopping applications or discarding their retained backup.

The companion keeps its application and installation handles through verification, native execution and readback. Windows passes only those handles through an explicit inheritance list. Native code checks their volume/file identities and writer exclusion, consumes the environment values and clears inheritance before invoking hooks. Only the package lock is released for the native owner to reacquire. Uninstall closes its internal package handle before the single directory rename, while retaining both outside leases. Cleanup names only the uniquely retired directory.

The small `windows_fs_probe.rs` fixture exercises Rust's actual directory rename with internal sharing modes, current-directory handles, a running owner, outside-lock exclusion and immediate reuse. PowerShell `Move-Item` is unsuitable as proof of a single rename because its provider can fall back to per-file moves. Dispatch with `operations_only=true` to run these experiments without installing application dependencies. They establish filesystem behavior on that runner, not full-app or power-loss acceptance. Full dependency installation and runtime builds wait for native recovery acceptance.

For a live directory-retirement failure, dispatch `velopack-adoption.yml` with `uninstall_diagnostics=true`. Only the fixture-feature binary can invoke the checksum-pinned [Microsoft Handle utility](https://learn.microsoft.com/en-us/sysinternals/downloads/handle); the workflow also requires its valid Microsoft signature. It captures current-directory and open-handle snapshots before metadata changes, after shortcut handling, and at a failed rename while the uninstaller is still alive. It requests listings only and does not use Handle's handle-closing options. These runs change timing, are marked diagnostic-only, and produce no candidate for full-app packaging; the acceptance gate rejects their reports. Normal validation leaves this instrumentation inactive.

## Prepare a candidate bundle

Use an isolated release environment at the exact source revision being certified. Generate a JSON object mapping key IDs to base64-encoded raw Ed25519 **public** keys; never put a private signing key in this file. Build the companion with those same keys:

```powershell
python scripts/build_update_repair.py --public-keys <public-keys.json> --native-updater <production-update.exe> --dist <repair-dist> --work <build-work>
```

The companion embeds the SHA-256 of the supplied updater for uninstall verification. Supply the exact production updater and certify the companion with that same binary. The reviewed native evidence JSON supplied below has `schema_version: 1`, the full `source_commit`, pinned `upstream_commit`, `patch_sha256`, `updater_sha256`, `repair_sha256`, `public_keys_sha256` (the exact public-key JSON file bytes), and a `checks` object with every check in the pin set to true. The evidence must describe the actual native tests and artifact identities; writing true values alone does not establish certification. It deliberately does not require a full-package hash, because the package does not yet exist.

Build the regular PyInstaller folder with the tracked disabled configuration, then prepare an isolated candidate from it:

```powershell
python scripts/configure_windows_updates.py --public-keys <public-keys.json> --native-updater <production-update.exe> --repair-executable <repair-dist>/TEMPORepair.exe --certification <native-evidence.json> --source-commit <full-source-commit> --output <candidate>/tempo-updates.json --bundle <dist>/TEMPO
```

The command checks the certified pin, patch and tool bytes, key file, source revision and required evidence before writing the enabled configuration into `_internal/assets` and copying `TEMPORepair.exe` to the application folder. Keep the resulting folder and exact evidence together. If using `TEMPO_UPDATE_CONFIG` in `tempo.spec`, set it to that `tempo-updates.json` and set `TEMPO_UPDATE_PUBLIC_KEYS`, `TEMPO_UPDATE_NATIVE_UPDATER`, `TEMPO_UPDATE_REPAIR`, `TEMPO_UPDATE_CERTIFICATION`, and `TEMPO_UPDATE_SOURCE_COMMIT` to the same inputs. The spec validates them before building; run the command with `--bundle` afterward to stage and verify the companion. The default build needs none of these variables and remains disabled.

Use the hash-verified pinned VPK 1.2.161 packager with its Windows x64 vendor updater and setup replaced by the **same reviewed production pair**, then pack the staged folder. For example, with the VPK nupkg extracted at `<vpk>` and its `vendor/update_x64.exe` and `vendor/setup_x64.exe` already replaced and verified:

```powershell
dotnet <vpk>/tools/net8.0/any/vpk.dll '[win]' pack --packId TEMPO.Bio.Desktop --packTitle TEMPO --packVersion <version> --packDir <dist>/TEMPO --mainExe TEMPO.exe --runtime win-x64 --outputDir <candidate-releases> --noPortable --skip-updates --yes
```

The separate `scripts/package_windows_updates.py` release step binds the resulting full package SHA-256 and verifies the embedded updater against its release evidence, then creates signed metadata. Its package-level evidence and publication checks follow packaging; native evidence alone does not certify a full package. The command does not publish. Preserve and certify the exact full package and installer, and complete the separate Windows installation, recovery, component and state-preservation gates before considering publication.

The [release-retention and signing procedure](../../docs/WINDOWS_UPDATE_RELEASE.md) defines co-signing during trust transitions, the reviewed full-app acceptance record, immutable retained release sets, recovered-copy verification and read-only checks of served assets before feed activation. `scripts.windows_update_release` refuses disabled candidates, unverified pins and incomplete evidence; it does not publish or configure durable storage.

## Legacy migration candidate

Starting with the v1.0.5 repair companion, you can migrate data directly from a registered Windows v1.0.0 or v1.0.1 installation. The companion matches the old installation folder and version to its Windows registration and checks the installed build's provenance against the corresponding release revision. Keep the installation in place so those files are available during preparation.

Download the new version's installer, `TEMPORepair.exe`, `tempo-update-v<version>.json`, and `TEMPO.Bio.Desktop-<version>-full.nupkg` from the same release. Compare the downloads with `SHA256SUMS.txt`. Close the old app and its workers. Install the new version at `%LOCALAPPDATA%\TEMPO.Bio.Desktop` and keep it closed. In PowerShell, run the command below, substituting your actual paths. The old installation defaults to `%LOCALAPPDATA%\Programs\TEMPO`.

```powershell
& '<download folder>/TEMPORepair.exe' --mode prepare-migration --legacy-data-only --legacy-root '<registered old install>' --target-manifest '<target signed release.json>' --target-package '<target full.nupkg>'
```

Preparation verifies the installed target against the signed inventory, excludes legacy writers, and copies both historical flat and `_internal` dataset/model layouts into personal storage. It retains raw snapshots and the full target package under `%LOCALAPPDATA%\TEMPO\updates\legacy-migration`. An offline reinstall of the old app still requires its original installer. If you have the original v1.0.1 installer and want to retain it for offline recovery, replace `--legacy-data-only` with `--legacy-installer '<path to original v1.0.1 installer>'`. The companion verifies the original installer, executable, and provenance against pinned checksums before retaining the installer.

Conflicting stores, source changes and unsupported absolute references stop preparation. The raw snapshots retain the original metadata. The migrated copy remaps exact references to verified dataset files in JSON and generated YOLO `data.yaml` files. External research paths retain their existing locations.

Only after preparation returns `prepared`, remove the **old** installation through Windows Settings. Keep TEMPO closed throughout. If shortcuts disappeared, rerun the new version's verified installer. Then finish:

```powershell
& '<download folder>/TEMPORepair.exe' --mode finish-migration
```

Finish requires the old executable and registrations to be gone. It verifies retained snapshots and surviving source files again, preserves repository-specific model selection, and publishes the stable collaboration root last. A pending transaction blocks normal startup until completion, including after an interrupted copy or publication. The commands never run a legacy executable or uninstaller, remove research files, export credentials, or delete the offline copies. Account settings, onboarding state, existing model directories, recordings and external analysis locations retain their existing paths.

The Windows build exercises both data migration paths against the exact packaged target and disposable installers built with the legacy Inno registration and uninstaller. The fixtures use a non-executable application marker. Process-exclusion checks run separately. Physical GPU acceptance and verification against the original installers require those components and installer files.

### NVIDIA preparation and finish

For a reviewed v1.0.1 NVIDIA selection, add `--prepare-nvidia` to the same prepare command. This explicitly authorizes the component downloads and target GPU probe. The source marker must match the exact reviewed descriptor; a marker alone cannot authorize a worker or archive. The signed target descriptor must also match the descriptor inside the signed full CPU package.

The v1.0.1 setup discarded its runtime ZIP. Preparation needs the exact original multipart archive to verify the installed worker and retain it for recovery. If an already verified archive is present, it is reused. If the archive is missing and preparation cannot download it, keep v1.0.1 installed and report the error to TEMPO support before proceeding. Preparation guards the worker's executable handles while comparing the complete installed inventory against the archive.

The new worker is downloaded, verified and GPU-probed before taking the exclusive application lease used for the data copy. The probe needs its own shared application lease. Preparation retains the old selection and both archives; it records the staged target and keeps the exact old descriptor outside the old uninstaller's scope. A retry revalidates and reuses the prepared target. Missing/corrupt libraries, an unknown descriptor, a failed probe, changed selection or conflicting data prevent retirement preparation.

Finish requires the old registration and executable to be gone. It verifies both complete runtime inventories again and selects only the prepared target, then publishes collaboration storage last. These are separate filesystem writes. An interruption between activation and publication leaves a pending migration that blocks desktop startup; running finish again accepts only the original or exact prepared target selection and completes without deleting either runtime. No automatic downgrade or CPU opt-out is inferred from missing or damaged NVIDIA support.

Ordinary application updates use the presence of an NVIDIA selection rather than successful worker lookup to determine component requirements. They recheck the signed base/target pair before handoff and after application exit. Independent confirmation requires the target worker to be selected and its complete retained inventory intact before retiring the application backup. Unit tests simulate downloads/probes; the Windows fixture checks executable-sharing behavior. These checks do not establish physical GPU compatibility or successful inference after a full-app migration.

### Optional-package assessment and snapshots

The reviewed Stimulation Investigation 1 v0.1.0 overlay requires its v1.0.1 base and removal before upgrade. It remains optional and has no v1.0.2 compatibility approval. Preparation still blocks every installed/partial package tree.

Before following its removal procedure, retain its recovery material outside the old installation:

```powershell
& '<candidate>/TEMPORepair.exe' --mode snapshot-packages --legacy-root '<registered old install>'
```

The command copies flat and historical `_internal` receipt/backup trees and the reviewed overlay's current application files into a new `%LOCALAPPDATA%\TEMPO\updates\package-snapshots` directory. It verifies the copies and writes `package-snapshot.json` only on completion. Reruns create separate snapshots. Unknown receipts cannot supply arbitrary paths to copy. Research directories, preferences and firmware are not changed, and no package command is executed.

The result distinguishes `removal_required`, `requires_review` and `base_restored`. The reviewed removal route requires matching receipt identity, original base provenance, expected current hashes and intact original backups. Interrupted install/removal states are accepted only where the published remover supports those exact before/after file hashes. Unknown versions, missing backups, altered files, malformed receipts and historical layouts require review. An empty parent left by the original remover ceases to block migration only when the original executable, provenance and firmware bytes match the reviewed base and the overlay's added files are absent. Other empty directories remain review states. A snapshot preserves evidence; it does not authorize removal.

For `removal_required`, obtain the original published package and verify its archive against the reported `published_archive_sha256` before using its `Remove.cmd`, or defer migration to finish collecting experiment data. Then rerun preparation against the restored reviewed base. Never use receipt-supplied executable paths or copy the old overlay onto v1.0.2. A separate compatible add-on build and real installation/removal acceptance remain required before allowing an overlay through migration.
