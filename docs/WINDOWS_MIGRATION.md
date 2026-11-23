# Upgrade an older Windows installation

Use this procedure for TEMPO v1.0.0 or v1.0.1 installed with Inno Setup. If your TEMPO already has in-app updates, follow the [ordinary update instructions](GETTING_STARTED.md#update-from-within-tempo-c).

## Choose the migration route

- **v1.0.1 with its original installer.** Use the original-installer route for a supported build. Keep the installer for offline recovery.
- **v1.0.0, or v1.0.1 without its original installer.** Use data-only migration for a registered build the v1.0.5 repair tool recognizes. Keep the resulting data snapshots and target package. Reinstalling the old app requires its original installer. The v1.0.5 tool is available with the Windows update trial. [v1.0.5 trial downloads](https://github.com/tempo-bio/TEMPO/releases/tag/v1.0.5)

## Prepare and verify before uninstalling

1. Back up recordings, analysis outputs, and local annotation drafts. Close TEMPO and its workers. Leave the older app installed and registered until preparation reports `prepared`.
2. Download the setup executable, `TEMPORepair.exe`, `tempo-update-vX.Y.Z.json`, and `TEMPO.Bio.Desktop-X.Y.Z-full.nupkg` from the same target release. Compare their checksums with that release's `SHA256SUMS.txt`. Install the target at its default location, `%LOCALAPPDATA%\TEMPO.Bio.Desktop`. Keep TEMPO closed after setup.
3. If the old app has the Stimulation Investigation 1 overlay, follow [legacy add-on removal](#legacy-add-on-removal) before preparing. If the old app uses NVIDIA acceleration, follow [NVIDIA migration](#nvidia-migration).
4. Open PowerShell in the folder containing the target release files. Run the command for your route. Replace the example paths and version with your own. The old Inno default was `%LOCALAPPDATA%\Programs\TEMPO`.

   **Original v1.0.1 installer**

   ```powershell
   & '.\TEMPORepair.exe' --mode prepare-migration --legacy-root '<old installation folder>' --legacy-installer '<original v1.0.1 installer>' --target-manifest '.\tempo-update-vX.Y.Z.json' --target-package '.\TEMPO.Bio.Desktop-X.Y.Z-full.nupkg'
   ```

   **Data-only migration**

   ```powershell
   & '.\TEMPORepair.exe' --mode prepare-migration --legacy-root '<old installation folder>' --legacy-data-only --target-manifest '.\tempo-update-v1.0.5.json' --target-package '.\TEMPO.Bio.Desktop-1.0.5-full.nupkg'
   ```

If preparation stops, keep the old installation and the files under `%LOCALAPPDATA%\TEMPO\updates\legacy-migration`. Resolve the reported issue before uninstalling.

## Finish the migration

1. After the repair tool reports `prepared`, remove the **old** installation through Windows **Settings → Apps → Installed apps**. Keep the new installation. If uninstall removes the new shortcuts, rerun the verified target installer before continuing.
2. In the target release folder, run:

   ```powershell
   & '.\TEMPORepair.exe' --mode finish-migration
   ```

3. Open TEMPO after the tool reports completion. Confirm that your account, drafts, and recordings are accessible before resuming work. Keep your backups and the migration files until you have checked the result.

### NVIDIA migration

For a reviewed v1.0.1 NVIDIA selection, add `--prepare-nvidia` to the prepare command. This checks the old runtime, stages the matching target runtime, and tests the GPU before preparation can succeed. It can require both runtime downloads when the archives are missing. If the old parts are unavailable and you have no verified archive, keep the old app installed.

### Legacy add-on removal

If you installed the v0.1.0 overlay for TEMPO v1.0.1, first retain the recovery files with the target repair companion:

```powershell
& '.\TEMPORepair.exe' --mode snapshot-packages --legacy-root '<old installation folder>'
```

If the result is `removal_required`, verify the retained original add-on ZIP against the reported `published_archive_sha256`, then use its `Remove.cmd`. Run the preparation step after the old app's base is restored. For `requires_review` or an unavailable verified ZIP, keep the old app and the package snapshot for review. Preserve the investigation JSON and adjacent attachment folder with your research files.
