# TEMPO

TEMPO is a desktop application for microscopy acquisition and cell-motion analysis. Use it to annotate cells, process recordings, inspect contraction traces and measurements, and train detection and segmentation models from reviewed annotations.

Camera control supports Micro-Manager and USB cameras; motorized stages use Micro-Manager. Configure and validate acquisition and stimulation with your microscope hardware before collecting experimental data.

Shared images, reservations, annotations, and training records are stored in a private Hugging Face workspace. Access to TEMPO's source code does not grant access to that workspace.

## Install on Windows

Download `TEMPO_Setup_v1.0.0.exe` from the project's [Releases page](https://github.com/tempo-bio/TEMPO/releases). The installer is for 64-bit Windows and includes Python and the application libraries.

### Unsigned installer

The v1.0.0 installer has no code-signing certificate. Windows cannot verify its publisher from a digital signature and may show **Unknown publisher** or **Windows protected your PC**. Smart App Control or your organization's security policy may block it entirely.

Download only from the TEMPO release page. To check that your download matches the release, compare its SHA-256 value with the installer entry in `SHA256SUMS.txt`. In PowerShell, from the download folder:

```powershell
Get-FileHash .\TEMPO_Setup_v1.0.0.exe -Algorithm SHA256
```

If the values differ, do not run the file. The checksum can detect a changed or incomplete download. It cannot verify who published the file or whether it is safe. Proceed only if you trust the source and your device policy permits it. On a managed computer, ask IT if installation is blocked. Do not disable Windows security protections to install TEMPO.

See Microsoft's guidance on [SmartScreen warnings](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation).

### First launch

1. Run the installer. The default location is `%LOCALAPPDATA%\Programs\TEMPO`.
2. Open TEMPO from the Start menu or the optional desktop shortcut.
3. Open **Account Setup** from the account indicator in the top right.
4. Enter a **Profile / Lab Name** and choose **Sign in with Hugging Face**. Approve access to the workspace organizations in your browser, return to TEMPO, and choose **Save Account**. If browser sign-in is unavailable, choose the personal-token method. Use your own account; do not share tokens between collaborators.

Choose **Account setup guide** for help creating an account and accepting workspace invitations. If you already have workspace access, choose **Skip to sign-in**. Maintainers configuring browser sign-in should follow [workspace administration](docs/WORKSPACE_ADMINISTRATION.md#browser-sign-in-application).

Camera acquisition requires compatible vendor drivers and Micro-Manager adapters. Image annotation does not require a connected camera.

See [Getting started](docs/GETTING_STARTED.md) for account setup and your first shared annotation.

## Start annotating

Ask the workspace administrator to grant your Hugging Face account access to the shared dataset and model repositories. Your token must permit the operations you will perform; it cannot grant more access than your account has.

1. Open **Segmentation Annotation → Shared Annotation Pool**.
2. Select an image to preview it. Checkboxes select a batch without changing that preview.
3. Choose **Reserve Image** or **Reserve Selected** before editing shared images.
4. Annotate the cells and save. Partially annotated images can remain drafts.
5. Check the fully annotated images and choose **Submit Selected** for maintainer review.

Check the sync indicator before closing TEMPO. A local save and a successful cloud sync are different states; unsynced changes remain on that computer. Reservations last seven days, after which another collaborator can adopt the last successfully synced draft.

See the [collaboration guide](docs/COLLABORATION_WORKFLOW.md) for reservations, review, adoption, and shared settings.

## Acquisition and analysis

The acquisition view provides camera controls and an XY-stage plate scout. Acquisition needs compatible hardware, drivers, and Micro-Manager adapters. Existing recordings can be processed without connecting a camera.

Processing outputs retain recording and field identity; registered fields can be explored together. See [spatial metadata](docs/SPATIAL_METADATA.md) for registration and coordinate conventions.

Acquisition includes AU26 V2 stimulation, pre-pacing across multiple fields, and
calibrated automatic field collection. Firmware can be installed from TEMPO;
see [stimulation setup and capture](docs/STIMULATION_SETUP.md).

Choose where new recordings and results are saved in **Settings → Data Folder**
(default **Documents/TEMPO**). Capture saves videos only when their retention
options are selected; successful processing keeps measurements and quality
evidence. See [recording storage, focus and quality review](docs/CAPTURE_QUALITY.md).

## Maintainer training workflow

Model training runs in Colab. It requires maintainer access to the private workspace and notebook; most TEMPO users will not have this access. A maintainer approves annotated images, opens the configured notebook, and follows its reported progress from TEMPO. Colab uses its own `HF_TOKEN` secret.

The [training guide](docs/TRAINING_WORKFLOW.md) documents setup, dataset selection, model comparisons, cancellation, and recovery for current and future maintainers.

## Settings and local work

**Save Personal Settings** saves your profile and personal preferences. The notebook link, HF organization, dataset repository, and model repository are shared settings managed by the administrator. TEMPO checks shared settings at launch; **Refresh Shared Settings** retries the check. Repository changes received during a session take effect after restarting.

Windows preferences are stored in `%LOCALAPPDATA%\TEMPO\config\tempo_preferences.json`; tokens use Windows Credential Manager. Local images, drafts, queues, and processing outputs are separate stores. Read [data locations](docs/COLLABORATION_WORKFLOW.md#local-files-and-connectivity) before moving an installation or removing local work.

Cloud collaboration, model downloads, and status refreshes need a network connection.

## Problems and development

Report problems through [GitHub Issues](https://github.com/tempo-bio/TEMPO/issues). Include the TEMPO version, Windows version, the action that failed, and error details. Remove tokens, private images, personal paths, and research identifiers before sharing logs or screenshots. The project is maintained by the [TEMPO organization](https://github.com/tempo-bio).

- [Development and verification](docs/DEVELOPMENT_AND_RELEASE_GUIDE.md)
- [Credential storage](docs/CREDENTIAL_SECURITY.md)
- [Windows packaging](docs/PACKAGING_AND_DISTRIBUTION.md)
- [Workspace administration](docs/WORKSPACE_ADMINISTRATION.md)

## Attribution and licensing

TEMPO's motion-analysis work draws on the [MUSCLEMOTION ImageJ macro](https://github.com/l-sala/MUSCLEMOTION), developed by Luca Sala, Berend J. van Meer, Francis L. Burton, and colleagues. See [scientific credit and third-party software](docs/THIRD_PARTY_SOFTWARE.md#musclemotion) for the original publication and software attribution.

TEMPO is licensed under the GNU Affero General Public License version 3 (AGPL-3.0-only). See [LICENSE](LICENSE). Third-party software and bundled reference images retain their own licenses; see [third-party software](docs/THIRD_PARTY_SOFTWARE.md).
