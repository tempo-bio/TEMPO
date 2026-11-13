# TEMPO

TEMPO is a desktop application for microscopy acquisition and cell-motion analysis. Use it to annotate cells, process recordings, inspect contraction traces and measurements, and train detection and segmentation models from reviewed annotations.

Camera control supports Micro-Manager and USB cameras; motorized stages use Micro-Manager.

Shared images, reservations, annotations, and training records are stored in a private Hugging Face workspace. Access to TEMPO's source code does not grant access to that workspace.

## Install on Windows

Download the 64-bit Windows installer from the [latest release](https://github.com/tempo-bio/TEMPO/releases/latest). Python and the application libraries are included.

### First launch

1. Run the installer. The default location is `%LOCALAPPDATA%\Programs\TEMPO`.
2. Open TEMPO from the Start menu or desktop shortcut.
3. Follow Guided Setup to connect your Hugging Face account and install the models.

Optional NVIDIA acceleration for Cellpose outlining is available during setup. You can add it later through **Settings → Application Tools → Guided Setup**.

See [Getting started](docs/GETTING_STARTED.md) for workspace access, model installation, and your first shared annotation. If Windows shows an unsigned-publisher warning, see [Unsigned Windows installer](docs/GETTING_STARTED.md#unsigned-windows-installer).

### Updating TEMPO

Moving from v1.0.1 to v1.0.2 requires a migration before removing the old installation. Follow the version-specific procedure, including its NVIDIA or add-on steps if applicable. Once you have a release with in-app updates, use **Settings → Application Tools → Check Software Updates**. [Upgrade and recovery instructions](docs/GETTING_STARTED.md#upgrade-tempo)

### Uninstall TEMPO

Use **Settings → Application Tools → Uninstall TEMPO…**, Windows **Settings → Apps → Installed apps**, or the **Uninstall TEMPO** Start menu entry. The in-app dialog lets you choose whether to remove saved preferences and Hugging Face sign-in details.

Recordings and analysis outputs in your data folder are retained. Check that annotation drafts have synced before uninstalling. See [Uninstall TEMPO](docs/GETTING_STARTED.md#uninstall-tempo) for the available methods and data-removal choices.

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

Acquisition includes stimulation, pre-pacing across multiple fields, and
calibrated automatic field collection. Firmware can be installed from TEMPO;
see [stimulation setup and capture](docs/STIMULATION_SETUP.md).

Choose where new recordings and results are saved in **Settings → Data Folder**
(default **Documents/TEMPO**). Capture saves videos only when their retention
options are selected; successful processing keeps measurements and quality
evidence. See [recording storage, focus and quality review](docs/CAPTURE_QUALITY.md).

## Maintainer training workflow

Maintainers train detection and segmentation models in Colab using approved annotations. Training requires maintainer access to the workspace and its configured notebook; progress and results are available in TEMPO.

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
