# Getting started with TEMPO

This guide covers installation, workspace access, and your first shared annotation in the current Windows release. Guided Setup connects your Hugging Face account and installs the models. Camera and stimulation hardware can be configured later in **Acquisition**.

## Install on Windows

1. Download the `TEMPO_Setup_v*.exe` installer from the release you are installing. The installer includes Python and the application libraries. [Latest stable release](https://github.com/tempo-bio/TEMPO/releases/latest)
2. Run the installer. TEMPO v1.0.2 uses `%LOCALAPPDATA%\TEMPO.Bio.Desktop`. The v1.0.1 default was `%LOCALAPPDATA%\Programs\TEMPO`.
3. Open TEMPO from the Start menu or desktop shortcut. Guided Setup opens automatically and resumes if you close it before finishing.

For an existing installation, follow the [upgrade instructions](#upgrade-tempo) before running the installer.

### Unsigned Windows installer

The Windows installer is unsigned. Windows may show **Unknown publisher** or **Windows protected your PC**; a managed computer's policy may block installation.

Download from this project's release page and compare the installer's SHA-256 with its entry in `SHA256SUMS.txt`. In PowerShell, from the download folder, run the following with the filename of the installer you downloaded.

```powershell
Get-FileHash .\TEMPO_Setup_v1.0.2.exe -Algorithm SHA256
```

If the values differ, do not run the file. A matching checksum confirms file integrity; it does not verify the publisher. On a managed computer, ask IT if installation is blocked. Keep Windows security protections enabled. See Microsoft's [SmartScreen guidance](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation).

## Upgrade TEMPO

### Move from v1.0.1 to v1.0.2

Back up your recordings, results, and local annotation drafts. Close TEMPO and keep v1.0.1 installed while preparing the move. Install v1.0.2 in its default separate location and follow the migration procedure with the original v1.0.1 installer, the v1.0.2 repair companion, the signed release manifest, and the full package. Remove the old installation through Windows Settings only after the repair companion reports `prepared`. Run the finish command before opening TEMPO. [Migration procedure](../installer/velopack/README.md#legacy-migration-candidate)

If you use NVIDIA acceleration, follow the NVIDIA preparation and finish steps. The migration can download both the old and new runtimes when the old archive is missing. If you have the Stimulation Investigation 1 add-on, follow the assessment and removal steps in the migration procedure before proceeding. If you use v1.0.0, install v1.0.1 first and confirm your work is accessible there. [v1.0.1 downloads](https://github.com/tempo-bio/TEMPO/releases/tag/v1.0.1)

### Update from within TEMPO v1.0.2 or later

Open **Settings → Application Tools → Check Software Updates**. If an update is available, choose **Download Update**. Save your work, then choose **Install and Restart**. If TEMPO offers **Download Full Update**, you can download it or wait and try again later.

### Recover a Windows installation

If setup failed or shortcuts disappeared, close TEMPO and rerun the verified installer for that version. If legacy migration stops, keep its recovery files and follow the migration procedure from the step that stopped.

If an in-app update stopped after it downloaded its recovery files, close TEMPO and its workers. Run `TEMPORepair.exe --mode forward` from PowerShell using the repair companion for the installed release. The companion checks the retained signed files and tries to complete the update. If repair stops, keep those files and report the exact message, both version numbers, and whether you use NVIDIA acceleration. [TEMPO issues](https://github.com/tempo-bio/TEMPO/issues)

## Connect your workspace

1. Create your own [Hugging Face account](https://huggingface.co/join), or use your existing personal account. Reservations and submissions belong to that account; do not use a shared login or token.
2. Send your Hugging Face username to your workspace administrator. Accept the organization invitations sent to your account's email address. Your administrator can confirm which organizations host your dataset, models, and workspace settings.
3. Enter the name or laboratory you want collaborators to see alongside your annotations.
4. Sign in through your browser, approve access to the workspace organizations, and return to TEMPO. Choose **Save and continue** to save the account connection.

Browser sign-in includes read/write access to your personal repositories. For access limited to the workspace repositories, choose **Use an access token**. A fine-grained token needs dataset read/write, model read, and workspace-settings read access. A token cannot grant permissions your account does not already have.

See [Account and credential security](CREDENTIAL_SECURITY.md) for token permissions, secure storage, changing accounts, and sign-in troubleshooting.

## Models and hardware

Choose **Install models** to verify and install the published release: Nano Scout for scouting, StarDist Nuclei for nuclear segmentation, and Cellpose Cells for cell segmentation. TEMPO checks the release before replacing installed weights. Keep TEMPO open until installation finishes. If a download fails, retry; your saved account remains connected. Downloads already present in the local download cache can be reused.

### Optional NVIDIA acceleration

For Cellpose outlining on a compatible NVIDIA GPU, select **Download NVIDIA acceleration (optional)** before choosing **Install models**. An installed, compatible NVIDIA graphics driver is required. The additional runtime download is about 6 GB; allow about 13 GB of free space on the Windows drive during installation.

Leave the option clear to use CPU processing. You can add acceleration later through **Settings → Application Tools → Guided Setup**. If NVIDIA installation fails, the downloaded models are retained; retry the optional installation or clear the checkbox to continue with CPU processing.

You can read these guides while installation runs:

- [Training and published models](TRAINING_WORKFLOW.md)
- [Stimulation hardware setup](STIMULATION_SETUP.md)

Connect the camera in Acquisition when you are ready to image. Previously recorded data can be analyzed without a connected camera.

## Your first shared annotation

Open **Segmentation Annotation → Shared Annotation Pool**. Reserve images before editing, annotate the cells, and submit fully annotated images for maintainer review. Check sync status before closing: local saving and uploading to the shared workspace are separate operations.

**Count Cells**, **Suggest Nuclei**, and **Outline Cells** show observed preparation, model, and result stages with elapsed time. A waiting message means TEMPO is responsive while it waits for the model; it does not estimate how much work remains. While a model runs, its action button becomes a Stop action. Choose it to stop that operation and keep your existing annotations. If a model does not return within 15 minutes, the attempt stops and your draft stays intact.

See the [Collaboration workflow](COLLABORATION_WORKFLOW.md) for reservations, review, and recovery, or the [TEMPO overview](../README.md) for the rest of the application.

## Uninstall TEMPO

To uninstall TEMPO on Windows:

1. **From within TEMPO**:
   Open **Settings → Application Tools → Uninstall TEMPO...**. Review your user data options (with checkboxes to optionally remove saved preferences or stored Hugging Face authentication tokens), then choose **Uninstall and Exit**. TEMPO closes automatically and runs the Windows uninstaller.
2. **From Windows Settings**:
   Open **Windows Settings → Apps → Installed apps** (or *Add or Remove Programs*), find **TEMPO**, and select **Uninstall**.
3. **From the Start Menu**:
   Open the Start Menu, locate the **TEMPO** folder, and select **Uninstall TEMPO**.

Personal recordings and analysis outputs in your data folder (default: `Documents\TEMPO`) are kept safe and will not be removed. Confirm that all local annotation drafts are synchronized before uninstalling.

## Earlier releases

In v1.0.0, open **Settings → Application Tools → Guided Setup** to start setup manually. The optional NVIDIA download is unavailable in v1.0.1-preview.1; install the current stable release to use NVIDIA setup.
