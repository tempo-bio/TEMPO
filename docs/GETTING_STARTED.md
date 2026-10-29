# Getting started with TEMPO

The v1.0.0 Windows release opens the dashboard first; choose **Guided Setup** from Settings → Application Tools. Builds made from the updated source open Setup on first launch and resume saved progress if you close it. Setup checks this computer, connects your personal Hugging Face account, and installs the published models. On Windows, the updated setup offers an optional NVIDIA acceleration download for Cellpose outlines when the matching runtime is published. Leave it unchecked to use CPU processing. You can connect camera and stimulation hardware later in **Acquisition**.

For the Windows download and an explanation of unsigned-installer warnings, see [Install on Windows](../README.md#install-on-windows).

## Connect your workspace

1. Create your own [Hugging Face account](https://huggingface.co/join), or use your existing personal account. Reservations and submissions belong to that account; do not use a shared login or token.
2. Send your Hugging Face username to your workspace administrator. Accept the organization invitations sent to your account's email address. Your administrator can confirm which organizations host your dataset, models, and workspace settings.
3. Enter the name or laboratory you want collaborators to see alongside your annotations.
4. Sign in through your browser, approve access to the workspace organizations, and return to TEMPO. Choose **Save and continue** to save the account connection.

Browser sign-in includes read/write access to your personal repositories. For access limited to the workspace repositories, choose **Use an access token**. A fine-grained token needs dataset read/write, model read, and workspace-settings read access. A token cannot grant permissions your account does not already have.

See [Account and credential security](CREDENTIAL_SECURITY.md) for token permissions, secure storage, changing accounts, and sign-in troubleshooting.

## Models and hardware

Choose **Install models** to verify and install the published release: Nano Scout for scouting, StarDist Nuclei for nuclear segmentation, and Cellpose Cells for cell segmentation. TEMPO checks the release before replacing installed weights. Keep TEMPO open until installation finishes. If a download fails, retry; your saved account remains connected. Downloads already present in the local download cache can be reused.

You can read these guides while installation runs:

- [Training and published models](TRAINING_WORKFLOW.md)
- [Stimulation hardware setup](STIMULATION_SETUP.md)

Connect the camera in Acquisition when you are ready to image. Previously recorded data can be analyzed without a connected camera.

## Your first shared annotation

Open **Segmentation Annotation → Shared Annotation Pool**. Reserve images before editing, annotate the cells, and submit fully annotated images for maintainer review. Check sync status before closing: local saving and uploading to the shared workspace are separate operations.

See the [Collaboration workflow](COLLABORATION_WORKFLOW.md) for reservations, review, and recovery, or the [TEMPO overview](../README.md) for the rest of the application.
