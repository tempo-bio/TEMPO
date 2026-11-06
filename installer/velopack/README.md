# Windows updater candidate

The patched Velopack 1.2.161 updater is **unverified**. The tracked pin and `installer/tempo-updates.json` keep in-app updates disabled. The Windows fixture, a certified native updater, a certified independent repair executable, legacy migration, and the full application installation and preservation matrix must all pass before release adoption. Do not publish artifacts from this candidate workflow.

## Build and review the native candidate

The source is Velopack 1.2.161, commit `92d6a1c91716729d449034df5c50307dcce39493`, under the accompanying MIT license. The patch refuses replacement on unconfirmed process waits, retains the previous payload, and attempts restoration if activation fails. It checks for running package processes before lifecycle hooks and before replacement; hook cleanup reports remaining processes without terminating them. A separate companion provides offline repair and confirmation. Process enumeration alone does not prove all processes have exited; the application lease and native behavior require Windows review.

On an isolated Windows builder, obtain a clean checkout of the pinned source, then run:

```powershell
python scripts/prepare_velopack_updater.py <velopack-checkout>
Push-Location <velopack-checkout>
cargo build --locked --release -p velopack_bins --bin update --features windows --target x86_64-pc-windows-msvc
Pop-Location
```

The preparation script verifies the commit, clean tree, upstream source hashes and patch digest. The `velopack-adoption` workflow builds distinct production-feature and fixture-fault binaries and exercises disposable installations. Fixture results are evidence, not production certification. Review actual Windows results and the broader acceptance matrix in `docs/WINDOWS_UPDATE_ADOPTION_PLAN.md` before changing the pin to `certified` and recording the production updater SHA-256. Keep the exact production binary and its evidence.

The signed package inventory covers every ZIP member. Verification of the installed `current` directory uses the pinned extractor's layout: `Squirrel.exe` and `*_ExecutionStub.exe` remain installation-owner files and are excluded from that directory. Offline repair uses the same layout. The root `Update.exe` is verified separately against the signed `lib/app/Squirrel.exe` checksum before apply; missing or changed application files still fail verification.

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

Use the hash-verified pinned VPK 1.2.161 packager with its Windows x64 vendor updater replaced by the **same certified production updater**, then pack the staged folder. For example, with the VPK nupkg extracted at `<vpk>` and its `vendor/update_x64.exe` already replaced and verified:

```powershell
dotnet <vpk>/tools/net8.0/any/vpk.dll '[win]' pack --packId TEMPO.Bio.Desktop --packTitle TEMPO --packVersion <version> --packDir <dist>/TEMPO --mainExe TEMPO.exe --runtime win-x64 --outputDir <candidate-releases> --noPortable --skip-updates --yes
```

The separate `scripts/package_windows_updates.py` release step binds the resulting full package SHA-256 and verifies the embedded updater against its release evidence, then creates signed metadata. Its package-level evidence and publication checks follow packaging; native evidence alone does not certify a full package. The command does not publish. Preserve and certify the exact full package and installer, and complete the separate Windows installation, recovery, component and state-preservation gates before considering publication.

## Legacy migration candidate

The prepare/finish commands currently support the reviewed Windows v1.0.1 CPU installation. `tempo_legacy_migration.SUPPORTED_LEGACY` binds its executable, embedded provenance, source commit and original installer to exact checksums. The original installer is the [published v1.0.1 artifact](https://github.com/tempo-bio/TEMPO/releases/tag/v1.0.1), not a rebuilt copy. Other versions, orphaned registrations, optional-package receipts and existing NVIDIA selections stop for review. NVIDIA migration and the full real-installer acceptance matrix remain release gates.

Install the candidate separately at `%LOCALAPPDATA%\TEMPO.Bio.Desktop`, keeping the old installation closed. Before removing it, use the companion with the target's signed release envelope and full package:

```powershell
& '<candidate>/TEMPORepair.exe' --mode prepare-migration --legacy-root '<registered old install>' --legacy-installer '<original v1.0.1 installer>' --target-manifest '<signed target release.json>' --target-package '<target full.nupkg>'
```

Preparation verifies the installed target against the signed inventory, excludes legacy writers, and copies both historical flat and `_internal` dataset/model layouts into personal storage. It retains raw snapshots, the verified old installer, and the full target package under `%LOCALAPPDATA%\TEMPO\updates\legacy-migration`. Differing stores, conflicting destination work or source changes require review; no active collaboration pointer changes during preparation. Exact references to verified dataset files are remapped in JSON values and generated YOLO `data.yaml` files. External research paths stay unchanged, and original metadata remains in the raw snapshots. Unsupported absolute identifiers and YAML references stop before uninstall.

Only after preparation returns `prepared`, remove the **old** installation through Windows Settings. Keep TEMPO closed throughout. Repair the candidate installation with its original verified setup executable if old shortcuts were removed, then finish:

```powershell
& '<candidate>/TEMPORepair.exe' --mode finish-migration
```

Finish requires the old executable and registrations to be gone. It verifies retained snapshots and surviving source files again, preserves repository-specific model selection, and publishes the stable collaboration root last. A pending transaction blocks normal startup until completion, including after an interrupted copy or publication. The commands never run a legacy executable or uninstaller, remove research files, export credentials, or delete the offline copies. Account settings, onboarding state, existing model directories, recordings and external analysis locations retain their existing paths.

The Windows fixture checks that migration refuses an already-running legacy process and prevents another instance from opening the old executable while preparation owns it. This fixture and the isolated data-transaction tests are necessary evidence; they do not replace verification of the actual old Inno installer, target TEMPO payload, shortcuts, NVIDIA components and both uninstallers.
