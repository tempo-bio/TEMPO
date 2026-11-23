# Collaborate in TEMPO-C

Use your own Hugging Face account in the existing workspace. If you have just joined, [connect your account](GETTING_STARTED.md#connect-your-workspace) before reserving images. For access changes or a maintainer handoff, see [workspace administration](WORKSPACE_ADMINISTRATION.md).

## Collaborators

Open **Segmentation Annotation → Shared Annotation Pool**. Click an image to preview it on the annotation canvas. Checkboxes select a batch for reservation or submission.

1. Choose **Reserve Image** for the previewed image or **Reserve Selected** for the checked batch. **Select First N Available** fills the batch from the first eligible images in the displayed order. Choose **Reserve Selected** to reserve them.
2. Open a downloaded reservation to edit it. Annotate every cell before submission. Trace touching cells separately when their shared boundary is visible. If the individual boundaries cannot be distinguished, trace the clump’s outer edge once and label it healthy or dead.
3. Save incomplete work as a draft. To submit, check the complete reserved images and choose **Submit Selected**. Remove any unreserved or incomplete images from the batch if submission is blocked. Submission has no minimum image, cell, or class count.
4. Check the sync indicator before closing. Retry a failed download with the same selection. If a local save fails, choose **Retry Saving Annotations**. If cloud sync fails, your local changes stay available for retry.

Command-click (Mac) or Ctrl-click (Windows/Linux) inside a visible mask to select it and reveal its list entry. Add Shift to add or remove a mask from the selection. Repeat at the same spot to cycle through overlapping masks, smallest first. Ordinary clicks still draw; selecting a mask preserves any unfinished drawing.

Reservations last seven days. While yours is active, you can still edit a pending submission. TEMPO-C syncs those changes to the shared draft and submission together. If someone adopts the reservation after expiry, they receive the last successfully synced draft and take over editing. Any pending submission stays available for review.

The account indicator at the top right shows the verified account, checking, signed-out or verification-failure state. Activate it to open Account Setup. The profile/laboratory label is separate from the verified account identity that owns reservations and submissions.

Reservation and review lists display the profile/laboratory name. Hover over a collaborator's name to inspect the account ID. Changing a display name does not transfer reservation or submission ownership. Saving Account Setup publishes the display name; subsequent collaboration writes also keep it current. An account without a shared profile name appears as “Unnamed collaborator”.

## Shared workspace settings

Check the notebook link and repository names in **Settings → Account and Shared Data**. The workspace administrator manages those fields. Use **Save Personal Settings** for your own profile name and preferences.

TEMPO-C checks for shared settings at launch. Use **Refresh Shared Settings** if a change has not arrived, or **Show Details** if the check fails. Sync drafts in your current workspace before restarting to apply a repository change.

## Maintainer

The Maintainer tab appears after authenticated maintainer access is verified. It contains:

- Images: inspect drafts and approved images; approve a complete image after reviewing its preview.
- **Review Submissions** lets you inspect and correct masks, then return or approve the batch. Reload a submission if its author changes it during review. Approval locks the annotations and admits them to training.
- Reservations: current owners, expiry, and adoption status, with image/collaborator search.
- Cloud training: open the Colab notebook, follow reported progress,
  and request cancellation.
- Model Evaluation: prepare a protocol, queue stages for Colab, and review the
  saved results. [Model Evaluation workflow](MODEL_EVALUATION_WORKFLOW.md)

## Cells and groups

During submission review, the maintainer selects an inseparable clump’s outline and checks **Group**. The healthy/dead label and traced boundary are retained. Collaborators use the healthy/dead labels; they do not assign group status.

Approved group outlines enter model training as healthy-group or dead-group examples.

In **Physiological Analysis**, choose **Cells** or **Groups** under **Measurements** to switch the lists, maps, and well averages. Each group has one mask and one motion trace. Group averages summarize groups, without estimating how many cells they contain.

Automatic plate scouting records a field when the configured healthy individual-cell threshold is met. Groups remain visible but do not independently qualify a field or add to the individual-cell count.

## Cloud training

Open **Maintainer → Cloud training → Open Notebook** to run training with your own Google account and Hugging Face credential. For setup and recovery, choose **Training Workflow** in that tab. [Training workflow](TRAINING_WORKFLOW.md)

## Local files and connectivity

Settings, credentials, and annotation drafts are separate stores. Windows preferences live in `%LOCALAPPDATA%\TEMPO\config\tempo_preferences.json`; tokens use Windows Credential Manager.

Shared-image copies, drafts, reservation caches, and session metadata live together in the dataset's collaboration directory. Current Windows Velopack installations use `%LOCALAPPDATA%\TEMPO\collaboration` after storage setup or migration. A source checkout uses its own `datasets` directory, and older Inno installations can have collaboration files under the installation directory. Within each storage root, the default workspace uses `universal_images` and other workspaces use `workspaces/<repository-id>/universal_images`.

Confirm that drafts have synced and back up the older collaboration directory before removing a legacy installation. [Windows migration procedure](WINDOWS_MIGRATION.md)

New acquisition data and analysis results use the personal **Data Folder**, defaulting to **Documents/TEMPO**. Queue/history metadata live under the personal configuration directory's `processing/` folder. Legacy queue paths and existing data remain in place. See [recording storage and quality](CAPTURE_QUALITY.md) before changing data locations or recovering interrupted captures.

Reservations, downloads, adoption, submission, approval, and synchronization need Hugging Face access. Local saves can remain pending while the service is unavailable. An adopter receives the last synced draft, not another computer's unsynced edits.
