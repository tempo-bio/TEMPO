# Getting started with TEMPO-C

Install TEMPO-C on Windows, then use Guided Setup to connect your Hugging Face account and install the models. If TEMPO is already installed, start with the upgrade instructions. [Upgrade TEMPO-C](#upgrade-tempo-c)

- **Taking over a workspace?** Verify the incoming team's permissions and notebook access before the outgoing team leaves. [Workspace administration](WORKSPACE_ADMINISTRATION.md#hand-off-workspace-responsibilities)
- **Running training?** Verify the Colab runtime and repository IDs before starting. [Training workflow](TRAINING_WORKFLOW.md)

Use the latest stable installer for ordinary onboarding. The v1.0.5 prerelease is for Windows update and migration testing. Its installed name is **TEMPO-C**. In v1.0.4 and earlier, look for **TEMPO**. [v1.0.5 trial](https://github.com/tempo-bio/TEMPO/releases/tag/v1.0.5)

## Install on Windows

1. Download the `TEMPO_Setup_v*.exe` installer from the release you are installing. The installer includes Python and the application libraries. [Latest stable release](https://github.com/tempo-bio/TEMPO/releases/latest)
2. Run the installer. Velopack releases install to `%LOCALAPPDATA%\TEMPO.Bio.Desktop`.
3. Open the app from the Start menu or desktop shortcut. Guided Setup opens automatically and resumes if you close it before finishing.

For an existing installation, follow the [upgrade instructions](#upgrade-tempo-c) before running the installer.

### Unsigned Windows installer

The Windows installer is unsigned. Windows may show **Unknown publisher** or **Windows protected your PC**; a managed computer's policy may block installation.

Download from this project's release page and compare the installer's SHA-256 with its entry in `SHA256SUMS.txt`. In PowerShell, from the download folder, replace `vX.Y.Z` with the version in your installer filename and run the command.

```powershell
Get-FileHash -LiteralPath '.\TEMPO_Setup_vX.Y.Z.exe' -Algorithm SHA256
```

If the values differ, do not run the file. A matching checksum confirms file integrity; it does not verify the publisher. On a managed computer, ask IT if installation is blocked. Keep Windows security protections enabled. See Microsoft's [SmartScreen guidance](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation).

## Upgrade TEMPO-C

### Update from within TEMPO-C

Open **Settings → Application Tools → Check Software Updates**. If an update is available, choose **Download Update**. Save your work, then choose **Install and Restart**. If TEMPO-C offers **Download Full Update**, you can download it or wait and try again later. The stable feed delivers stable releases. Follow the release page if you intend to install a prerelease. [Windows releases](https://github.com/tempo-bio/TEMPO/releases)

### Move from an older Inno installation

Keep the old installation until the migration tool has prepared and verified your data. For v1.0.0, or v1.0.1 without the original installer, use the data-only route in the v1.0.5 repair tool. [Windows migration procedure](WINDOWS_MIGRATION.md)

### Recover a Windows installation

If setup failed or shortcuts disappeared, close TEMPO-C and rerun the verified installer for that version. If legacy migration stops, keep its recovery files and follow the migration procedure from the step that stopped.

If an in-app update stopped after it downloaded its recovery files, close TEMPO-C and its workers. Open PowerShell in the folder containing the repair companion for the installed release and run `& '.\TEMPORepair.exe' --mode forward`. The companion checks the retained signed files and tries to complete the update. If repair stops, keep those files and report the exact message, both version numbers, and whether you use NVIDIA acceleration. [TEMPO-C issues](https://github.com/tempo-bio/TEMPO/issues)

## Connect your workspace

1. Send your personal Hugging Face username to your workspace administrator. If you need an account, [sign up for Hugging Face](https://huggingface.co/join). TEMPO assigns reservations and submissions to the account you use.
2. Accept the invitations to the organizations that host the existing dataset, models, and shared settings. Ask your administrator which organizations to select during sign-in.
3. Enter the name or laboratory you want collaborators to see alongside your annotations.
4. Sign in through your browser, approve access to the workspace organizations, and return to TEMPO-C. Choose **Save and continue** in Guided Setup or **Save Account** in **Settings → Application Tools → Account Setup**.

Browser sign-in includes read/write access to your personal repositories. For access limited to the workspace repositories, choose **Use an access token**. A fine-grained token needs dataset read/write, model read, and workspace-settings read access. A token cannot grant permissions your account does not already have.

See [Account and credential security](CREDENTIAL_SECURITY.md) for token permissions, secure storage, changing accounts, and sign-in troubleshooting.

## Models and hardware

Choose **Install models** to verify and install the published release: Nano Scout for scouting, StarDist Nuclei for nuclear segmentation, and Cellpose Cells for cell segmentation. TEMPO-C checks the release before replacing installed weights. Keep TEMPO-C open until installation finishes. If a download fails, retry; your saved account remains connected. Downloads already present in the local download cache can be reused.

### Optional NVIDIA acceleration

For Cellpose outlining on a compatible NVIDIA GPU, select **Download NVIDIA acceleration (optional)** before choosing **Install models**. An installed, compatible NVIDIA graphics driver is required. The additional runtime download is about 6 GB; allow about 13 GB of free space on the Windows drive during installation.

Leave the option clear to use CPU processing. You can add acceleration later through **Settings → Application Tools → Guided Setup**. If NVIDIA installation fails, the downloaded models are retained; retry the optional installation or clear the checkbox to continue with CPU processing.

### Connect a camera

In **Acquisition → Camera Settings…**, use **Browse** to open your Micro-Manager `.cfg` file and choose **Start Hardware Feed**. Use an installed Micro-Manager adapter for each device in that configuration, including any motorized XY stage.

On Windows, open **Acquisition → Camera Settings… → DirectShow**, then choose
**Autodetect** to list video inputs. Enter the number beside your microscope
camera or HDMI capture adapter under **Camera number**, leave the resolution on
**Default (Auto)**, and choose **Connect Camera**. If that mode fails, try a
resolution supported by your camera or adapter. TEMPO-C shows the frame size it
receives. Use the camera's acquisition settings to verify its frame rate if
Windows cannot report it.

DirectShow can use standard USB Video Class (UVC) cameras, HDMI capture
adapters that appear as Windows video inputs, and cameras with a compatible
64-bit DirectShow driver. Some microscope cameras, including models from
[ToupTek](https://www.touptekphotonics.com/download/?category=DirectShow),
have a separate DirectShow driver from the manufacturer.

For a camera that only appears in the manufacturer's software, use a
supported Micro-Manager camera adapter if available, or feed the camera's HDMI
output through a Windows-compatible USB capture adapter. With a
[Leica MC120 HD](https://www.leica-microsystems.com/products/microscope-cameras/p/leica-mc120-hd/),
use the camera's **HD** mode for HDMI capture.

For recording, stimulation, and automated field collection, see [Stimulation and capture](STIMULATION_SETUP.md) and [Recording storage and quality review](CAPTURE_QUALITY.md).

## Your first shared annotation

Open **Segmentation Annotation → Shared Annotation Pool**. Reserve images before editing, annotate the cells, and submit fully annotated images for maintainer review. Check sync status before closing: local saving and uploading to the shared workspace are separate operations.

**Count Cells**, **Suggest Nuclei**, and **Outline Cells** show observed preparation, model, and result stages with elapsed time. A waiting message means TEMPO-C is responsive while it waits for the model; it does not estimate how much work remains. While a model runs, its action button becomes a Stop action. Choose it to stop that operation and keep your existing annotations. If a model does not return within 15 minutes, the attempt stops and your draft stays intact.

See the [Collaboration workflow](COLLABORATION_WORKFLOW.md) for reservations, review, and recovery, or the [TEMPO-C overview](../README.md) for the rest of the application.

## Analyze a recording

1. In **Kinematics Extraction**, choose **Add Recordings** and select a supported movie or TEMPO-C recording.
2. Choose **Process Queue**. Review the proposed cell outlines and confirm the cells and groups to include when prompted.
3. Select a completed run and choose **Open in Analysis**. To reopen results later, choose **Physiological Analysis → Open Results CSV** and select that run's `Master_Cell_Data.csv`. Inspect traces and measurements and open **Recording Quality…** to review flagged fields. [Quality review](CAPTURE_QUALITY.md#review-quality-and-include-or-exclude-data) · [Full-well maps](SPATIAL_METADATA.md)

## Uninstall TEMPO-C

Confirm that your annotation drafts are synced before uninstalling. Recordings and analysis outputs stay in your data folder, which defaults to `Documents\TEMPO`.

- Open **Settings → Application Tools → Uninstall TEMPO-C…**. The action is **Uninstall TEMPO...** in v1.0.4. Choose whether to remove preferences and Hugging Face sign-in details, then choose **Uninstall and Exit**.
- In **Windows Settings → Apps → Installed apps**, find **TEMPO-C** and select **Uninstall**. Windows lists versions through v1.0.4 as **TEMPO**.
