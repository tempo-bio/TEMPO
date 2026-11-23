# TEMPO-C

TEMPO-C is a desktop application for microscopy acquisition and cell-motion analysis. Use it to annotate cells, process recordings, inspect contraction traces and measurements, and train detection and segmentation models from reviewed annotations.

Use Micro-Manager to control compatible cameras and motorized stages. Windows also supports compatible USB cameras and video capture adapters through DirectShow.

## Install on Windows

Download the 64-bit Windows installer from the [latest release](https://github.com/tempo-bio/TEMPO/releases/latest). Python and the application libraries are included.

Open the app and follow Guided Setup to connect your account and install the models. If TEMPO is already installed, follow the upgrade instructions. [Getting started](docs/GETTING_STARTED.md) · [Upgrade and recovery](docs/GETTING_STARTED.md#upgrade-tempo-c) · [Uninstall TEMPO-C](docs/GETTING_STARTED.md#uninstall-tempo-c)

If you are taking over an existing workspace, start with the administration handoff, then use the training guide to verify the shared Colab notebook. [Workspace handoff](docs/WORKSPACE_ADMINISTRATION.md#hand-off-workspace-responsibilities) · [Training workflow](docs/TRAINING_WORKFLOW.md)

## Start annotating

Ask the workspace administrator to invite your own Hugging Face account to the private workspace. In **Segmentation Annotation → Shared Annotation Pool**, reserve images, save your outlines, and submit complete annotations for review. [Connect your account](docs/GETTING_STARTED.md#connect-your-workspace) · [Shared annotation and review](docs/COLLABORATION_WORKFLOW.md)

## Acquisition and analysis

The acquisition view provides camera controls and an XY-stage plate scout. Use a compatible Micro-Manager adapter or, on Windows, a camera or capture device that appears in DirectShow. Motorized XY control requires a Micro-Manager stage adapter. Existing recordings can be processed without connecting a camera. [Camera setup](docs/GETTING_STARTED.md#connect-a-camera) · [Analyze a recording](docs/GETTING_STARTED.md#analyze-a-recording)

Processing outputs retain recording and field identity; registered fields can be explored together. See [spatial metadata](docs/SPATIAL_METADATA.md) for registration and coordinate conventions.

Acquisition includes stimulation, pre-pacing across multiple fields, and
calibrated automatic field collection. Firmware can be installed from TEMPO-C;
see [stimulation setup and capture](docs/STIMULATION_SETUP.md).

Choose where new recordings and results are saved in **Settings → Optical Acquisition → Data Folder**
(default **Documents/TEMPO**). The retention options control which videos are
kept after successful processing. Failed or interrupted captures can leave
recoverable footage. Successful processing keeps measurements and quality
evidence. [Recording storage, focus and quality review](docs/CAPTURE_QUALITY.md)

## Maintainer training workflow

Maintainers use their own Google and Hugging Face accounts to train in the shared Colab notebook from approved annotations. Review progress and results in TEMPO-C. [Training workflow](docs/TRAINING_WORKFLOW.md) · [Model Evaluation](docs/MODEL_EVALUATION_WORKFLOW.md)

## Problems and development

Include your TEMPO-C version, operating system and version, the action that failed, and error details. Remove tokens, private images, personal paths, and research identifiers before sharing logs or screenshots. [TEMPO-C issue tracker](https://github.com/tempo-bio/TEMPO/issues)

- [Development and verification](docs/DEVELOPMENT_AND_RELEASE_GUIDE.md)
- [Credential storage](docs/CREDENTIAL_SECURITY.md)
- [Windows packaging](docs/PACKAGING_AND_DISTRIBUTION.md)
- [Workspace administration](docs/WORKSPACE_ADMINISTRATION.md)

## Attribution and licensing

TEMPO-C's motion-analysis work draws on the [MUSCLEMOTION ImageJ macro](https://github.com/l-sala/MUSCLEMOTION), developed by Luca Sala, Berend J. van Meer, Francis L. Burton, and colleagues. See [scientific credit and third-party software](docs/THIRD_PARTY_SOFTWARE.md#musclemotion) for the original publication and software attribution.

TEMPO-C is licensed under the GNU Affero General Public License version 3 (AGPL-3.0-only). See [LICENSE](LICENSE). Third-party software and bundled reference images retain their own licenses; see [third-party software](docs/THIRD_PARTY_SOFTWARE.md).
