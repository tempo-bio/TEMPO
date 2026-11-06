# Windows updater candidate

The patched Velopack 1.2.161 updater is **unverified**. The tracked pin and `installer/tempo-updates.json` keep in-app updates disabled. The Windows fixture, a certified native updater, a certified independent repair executable, legacy migration, and the full application installation and preservation matrix must all pass before release adoption. Do not publish artifacts from this candidate workflow.

## Build and review the native candidate

The source is Velopack 1.2.161, commit `92d6a1c91716729d449034df5c50307dcce39493`, under the accompanying MIT license. The patch refuses replacement on unconfirmed process waits, retains the previous payload, and attempts restoration if activation fails. A separate companion provides offline repair and confirmation. Process enumeration alone does not prove all processes have exited; the application lease and native behavior require Windows review.

On an isolated Windows builder, obtain a clean checkout of the pinned source, then run:

```powershell
python scripts/prepare_velopack_updater.py <velopack-checkout>
Push-Location <velopack-checkout>
cargo build --locked --release -p velopack_bins --bin update --features windows --target x86_64-pc-windows-msvc
Pop-Location
```

The preparation script verifies the commit, clean tree, upstream source hashes and patch digest. The manually dispatched `velopack-adoption` workflow builds distinct production-feature and fixture-fault binaries and exercises disposable installations. Fixture results are evidence, not production certification. Review actual Windows results and the broader acceptance matrix in `docs/WINDOWS_UPDATE_ADOPTION_PLAN.md` before changing the pin to `certified` and recording the production updater SHA-256. Keep the exact production binary and its evidence.

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
