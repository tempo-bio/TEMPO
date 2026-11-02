Optional Windows x64 data-collection package for the existing stable TEMPO v1.0.1 installation.

Download **TEMPO_Stimulation_Investigation_1_v0.1.0.zip**, extract the complete archive, close TEMPO, and run **Install.cmd**. Reopen TEMPO and choose **Stimulation Investigation 1** in Acquisition. For a custom installation folder, see README.txt in the archive.

The package adds condition tracking, independent oscilloscope records, attachments, and raw Arduino acquisition. Setup stays on the left, records replace the image preview, and the existing Stimulation controls and Expected Signal remain on the right. Live Arduino acquisition requires a separate development-firmware installation from the Arduino tab.

The download reuses the installed v1.0.1 libraries and backs up the application files it replaces. **Remove.cmd** restores the original application files. Research data, saved settings, credentials, and models are retained. Remove this package before upgrading or uninstalling the base application.

This experimental package collects data without calibration calculations or automatic output adjustment. The publishing workflow checks installation and data collection on Windows with simulated instruments. Physical measurement accuracy and direct-input compatibility require bench verification with the actual hardware.

[Required base installation: TEMPO v1.0.1](https://github.com/tempo-bio/TEMPO/releases/tag/v1.0.1). GitHub Packages distributes the add-on ZIP together with its matching source archive and checksums.

## Maintainer packaging

The **Publish Stimulation Investigation 1 package** workflow builds from the
stable v1.0.1 executable, verifies package installation, simulated collection,
removal, and reinstall on Windows, and publishes to GitHub Packages. It runs
when the `codex/stimulation-investigation-1` branch is pushed, and also supports
manual dispatch. It uses the job's `GITHUB_TOKEN` with `packages: write`;
maintainers do not need to supply a personal access token for this route.

The destination is `ghcr.io/tempo-bio/stimulation-investigation-1:0.1.0`.
The workflow retains the installable ZIP, matching source, checksums, and
verification results as Actions artifacts, including when publication fails.
It creates no GitHub Release.

New packages default to private. Set the package's visibility to public in
GitHub's package settings if the final anonymous-download check fails for a
newly created package. A successful upload alone does not establish public
availability. Public downloads are checked against the local file hashes.

### Local packaging

Use the unmodified stable Windows v1.0.1 application as the binary base and
Python 3.13 to build the package. The source folder must contain the v1.0.1
sources with the investigation changes applied. Supply a ZIP of those same
sources, with paths relative to its root. The builder checks that the changed
modules, resources, and installer match the source archive.

```sh
python -m scripts.package_stimulation_investigation --installed /path/to/TEMPO --source /path/to/matching-source --source-archive /path/to/source.zip --output Output/investigation-package
python -m scripts.publish_stimulation_package --package Output/investigation-package/TEMPO_Stimulation_Investigation_1_v0.1.0.zip --source-archive /path/to/source.zip
```

The second command uses a native [ORAS client](https://oras.land/docs/installation/)
to stage an OCI artifact locally. The artifact contains the installable ZIP,
matching source ZIP, checksums, and installation instructions.

Before publishing, verify the installed executable and removal on Windows using
`scripts/test_stimulation_package_install.ps1` against a disposable copy of the
stable installation. This uses simulated instruments and does not establish
hardware accuracy.

For an optional local upload, authenticate ORAS to `ghcr.io` with a classic personal access token
that has `write:packages`, following [GitHub's registry authentication instructions](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-with-a-personal-access-token-classic).
Keep credentials out of source files and command arguments. Then repeat the
staging command with `--publish`.

Users with ORAS can retrieve the ZIP and source without Docker:

```sh
oras pull ghcr.io/tempo-bio/stimulation-investigation-1:0.1.0
```
