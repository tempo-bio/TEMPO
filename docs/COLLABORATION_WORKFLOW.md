# TEMPO collaboration and cloud training

## Collaborators

Open Segmentation Annotation → Shared Annotation Pool. One image browser shows shared images and any local imports, with reservation state, annotation counts and saved overlays. Click an image to preview it on the regular annotation canvas; checkbox clicks select a batch without changing that preview. Zoom, pan, Fit Image and Show Annotations are available during preview.

Command-click (Mac) or Ctrl-click (Windows/Linux) inside a visible mask to select it and reveal its list entry. Add Shift to add or remove a mask from the selection. Repeat at the same spot to cycle through overlapping masks, smallest first. Ordinary clicks still draw; selecting a mask preserves any unfinished drawing.

Reserve Image reserves the focused image; Reserve Selected reserves the checked batch. Leases last seven days. Select First N Available replaces the checked batch with the first N eligible images in displayed order and does not reserve them. Successfully downloaded images immediately use the regular editor and saving/sync paths. Selecting an image already reserved and downloaded opens its editable draft in the same browser. Cell-detection buttons require an editable image; the panel states why they are unavailable. A failed download retains the selection for retry; a failed local save retains the image and edits and offers Retry Saving Annotations.

Save partially annotated images as drafts. Check the fully annotated reserved images in the browser, then choose Submit Selected. Unreserved or incomplete images in the checked batch prevent submission; selecting an image for preview does not add it to that batch. Submission has no minimum image, cell, or class count; completeness is required. The guidelines require every cell in each submitted image to be annotated. Trace touching cells separately when their shared boundary is visible. If the individual boundaries cannot be distinguished, trace the clump’s outer edge once and label it healthy or dead.

Pending submissions remain editable by the active lease owner. Local saves are synced automatically
to the adoptable draft and the pending submission together. The sync indicator distinguishes local
save from successful cloud sync. Failed sync preserves local edits for retry; closing with pending
edits requires acknowledging that they have not reached the cloud.

The account indicator at the top right shows the verified account, checking, signed-out or verification-failure state. Activate it to open Account Setup. The profile/laboratory label is separate from the verified account identity that owns reservations and submissions.

Reservation and review lists display the profile/laboratory name. Hover over a collaborator's name to inspect the account ID. Changing a display name does not transfer reservation or submission ownership. Saving Account Setup publishes the display name; subsequent collaboration writes also keep it current. An account without a shared profile name appears as “Unnamed collaborator”.

## Shared workspace settings

The notebook URL, Hugging Face organization, private dataset repository and model repository are administrator-managed. Everyone can edit their own profile/laboratory name and personal analysis settings. Other accounts can view the shared workspace values but cannot publish changes to them.

The administrator edits **Settings → Account and Shared Data → Update Shared Settings**. This button appears only for the verified administrator and updates the notebook link, organization, dataset repository and model repository for collaborators. **Save Personal Settings** saves personal preferences; it never sends edits to those four shared fields. Unsaved personal preferences remain unchanged when updating shared settings.

TEMPO checks for shared settings at launch, before checking model updates; an offline launch retains saved values. **Refresh Shared Settings** retries a failed check, and **Show Details** displays the reason for a failure. A repository change received during an open session is saved for the next launch, preserving the dataset used by current edits.

Discovery uses `tempo_shared_settings.json` in the separate private `tempo-bio-settings/workspace` repository, independently of the destinations in that file. The settings organization grants collaborators read-only access; admins can publish. TEMPO verifies the administrator's organization role and repository write permission. See [workspace administration](WORKSPACE_ADMINISTRATION.md) for setup and access rules. Keep collaborators' read access to this settings repository when moving the working repositories. Each dataset has its own local images, reservation cache and drafts; changing repositories preserves the previous dataset's local work.

After expiry another collaborator can adopt immediately, including the last successfully synced
annotations. Unsynced changes on another computer cannot be transferred. Adoption preserves the
pending submission and moves editing ownership to the adopter. Only the maintainer can approve or
return submissions for corrections. Approval locks annotations permanently and admits them to
the training dataset. Approval of a stale review is refused if the submission changed meanwhile.

## Maintainer

The Maintainer tab appears after authenticated maintainer access is verified. It contains:

- Images: inspect drafts and approved images; approve a complete image after reviewing its preview.
- Review submissions: inspect and correct masks, return unapproved batches, or approve them.
- Reservations: current owners, expiry, and adoption status, with image/collaborator search.
- Cloud training: open the Colab notebook, follow reported progress,
  and request cancellation. No training process is launched on the desktop.

## Cells and groups

During submission review, the maintainer selects an inseparable clump’s outline and checks **Group**. The healthy/dead label and traced boundary are retained. Collaborators use the healthy/dead labels; they do not assign group status.

Approved group outlines enter model training as healthy-group or dead-group examples.

In analysis, **Measurements → Cells / Groups** separates the lists, maps and well averages. Each group has one mask and one motion trace. Group averages summarize groups, without estimating how many cells they contain.

Automatic plate scouting records a field when the configured healthy individual-cell threshold is met. Groups remain visible but do not independently qualify a field or add to the individual-cell count.

## Cloud training

Open **Maintainer → Cloud training → Training Workflow** for the complete guide inside TEMPO.
The same guide is maintained in [Training workflow](TRAINING_WORKFLOW.md). It covers notebook
setup, Run all, approved-image selection, held-out evaluation, automatic model comparisons,
progress, cancellation and retries.

## Packaging

See [Packaging and distribution](PACKAGING_AND_DISTRIBUTION.md) for build and installation instructions.

## Local files and connectivity

Settings, credentials, and annotation drafts are separate stores. Windows preferences live in `%LOCALAPPDATA%\TEMPO\config\tempo_preferences.json`; tokens use Windows Credential Manager.

Shared-image copies, drafts, reservation caches, and session metadata live together in the dataset's collaboration directory. The default workspace uses `datasets/universal_images` under the application module directory; other workspaces use repository-derived directories under `datasets/workspaces`. Source installations use the source directory; packaged installations use the bundled module directory. Preserve these local annotation stores and confirm synchronization before replacing or removing an application directory.

New acquisition data and analysis results use the personal **Data Folder**, defaulting to **Documents/TEMPO**. Queue/history metadata live under the personal configuration directory's `processing/` folder. Legacy queue paths and existing data remain in place. See [recording storage and quality](CAPTURE_QUALITY.md) before changing data locations or recovering interrupted captures.

Reservations, downloads, adoption, submission, approval, and synchronization need Hugging Face access. Local saves can remain pending while the service is unavailable. An adopter receives the last synced draft, not another computer's unsynced edits.
