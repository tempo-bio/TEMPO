# Training workflow

This guide is for maintainers with access to the private dataset, model repository, and Colab notebook. Annotation access alone does not grant training access.

Training runs in Colab. TEMPO opens the notebook, displays reported progress and provides cancellation. Approved images remain available for later runs.

## Where actions happen

| Component | Responsibilities |
| --- | --- |
| TEMPO | Annotation approval, Open Notebook, reported progress, Cancel Run, model installation and this guide. |
| Colab | GPU runtime, training settings, package checks, training, evaluation and saving results. |
| Hugging Face | Approved images and annotations, run status, saved training results and current model files. |

**Open Notebook** opens the saved URL in the system's default browser. With no saved link, it opens Colab to choose a saved notebook or upload one. TEMPO does not embed the browser, read its page, allocate a Colab GPU or remotely start notebook execution.

## Setup

1. Upload `notebooks/TEMPO_Colab_Trainer.ipynb` to Colab and save the notebook for reuse.
   Windows builds also provide a `TEMPO_Colab_Trainer` download in their GitHub Actions artifacts. Upload the matching notebook when updating TEMPO; an older saved Colab copy keeps its original training code.
2. In **Maintainer → Cloud training**, choose **Edit Notebook URL** to open **Settings → Account and Shared Data → Colab Notebook**. As the administrator, enter its URL and choose **Update Shared Settings**. **Open Notebook** opens the saved link. Collaborators receive the updated link at launch.
3. Add `HF_TOKEN` in Colab Secrets and enable notebook access. The token must permit reading the private dataset, writing training records and updating the model repository. Training requires maintainer access.
4. Select a GPU runtime in Colab. The notebook defaults to **Required GPU: A100**. **Any CUDA GPU** permits another assigned CUDA GPU; availability is controlled by Colab.

## Run training

1. Finish annotating and approve the intended images in TEMPO.
2. Open the notebook and set **Maximum epochs** and **Required GPU** before execution.
3. Choose **Run all** in Colab.
4. Inspect the results after execution finishes. Each model reports its new score, previous model score and replacement outcome. The previous model score comes from evaluating the weights that were current before replacement on the same comparison images.

Run all performs setup, GPU checks, approved-image selection, training, comparison and saving in order. A failed check stops execution. No manual choice of notebook sections is required.

The default maximum is 150 epochs. An epoch is a training cycle; image and patch sampling differ between model libraries. Early stopping can end a model before its maximum.

For requests longer than one epoch, a one-epoch trial runs automatically before full training. Its temporary weights are separate from the full run and cannot replace current models. Setting **Maximum epochs** to **1** requests an optional one-epoch training run that includes saved-weight and evaluation checks. No separate preliminary run is required in that case.

Each trial includes model initialization, an epoch, checkpoint saving and evaluation. Elapsed time covers all these steps; it is not an epoch counter. Training workers have no fixed time limit and remain cancellable. A failed trial stops execution before the next model or full training. Logs, produced weights and partial check results remain in the run folder.

## Approved input and repeated runs

Only explicitly approved images enter training. Drafts, pending submissions and records without approval are excluded. Every cell in an approved image must be annotated. An inseparable clump receives one outer outline; the maintainer marks it as a group during review while retaining its healthy/dead label. Submission has no minimum image, cell or class count. Training input must contain cell annotations; cell-free negative-image training is not supported.

The run records the approved dataset version when it starts. Images approved afterward enter a later run. The notebook reports the selected image counts; approval does not automatically start training.

Approved images are retained and reused. Training does not mark them used, change their annotations, release reservations or remove approval. Each new run recompiles labels from its recorded dataset version.

## Training, validation and comparison images

- **Training images** provide the annotations used to update the models.
- **Validation images** monitor training and support saved-weight or threshold selection.
- **Comparison images** evaluate the saved models and their current counterparts after training. They are also called test images or held-out images.

For a study across biological preparations, verified preparation or animal IDs keep related images together in one role. Related wells, fields and repeated frames from one animal must remain together when measuring performance across animals. Comparisons between fields from one mouse do not establish performance on new mice.

Fresh training assignments target approximately 70% training, 15% validation and 15% comparison while keeping verified groups indivisible. Exact image proportions vary with preserved roles and coverage requirements. In Model Evaluation, the selected protocol determines grouping and assignments while preserving prior training and validation exposure.

Assisted training needs enough groups and reviewed nuclear examples to train the helper without using the images it will assist. The validated training configuration determines whether these groups represent fields or biological preparations. Scores on training images are labelled as diagnostics and cannot establish architecture adoption.

Assignments persist between runs, including runs that update no models. Previously used training images remain training images. New images fill missing roles first, then follow the same proportions. A dataset with preserved assignments may lack a comparison role until additional images are approved; training and saving remain available.

## Models and scores

| Model | Output |
| --- | --- |
| Nano Scout | Fixed-size individual-cell center boxes and full-footprint healthy/dead group boxes. |
| StarDist Nuclei | Nuclear instances and a continuous helper map; nuclear multiplicity does not determine cell identity. |
| Cellpose | Healthy/dead cell and group outlines; conflicting predictions require review. |

**IoU**, intersection over union, measures overlap between a prediction and its annotation: shared area divided by combined area. An IoU of 0.5 means half of the combined area overlaps.

Nano reports class-aware center F1 at a 12-pixel radius at the 640-pixel reference scale, with count error on fields without unresolved groups. Group detections do not provide constituent-cell counts.

StarDist reports recall against confidently outlined nuclei and outside-cell false positives. Unlabelled interiors of annotated cells and groups are ignored. Missing nuclear outlines never mean nuclear absence, and unmatched predictions inside cells are not automatically false positives.

Cellpose reports **detection and class F1 at IoU 0.5**, with separate scores for healthy/dead cells and any group classes present. A correct prediction must overlap the annotation and have the correct healthy/dead and cell/group class. Unclassified predictions do not count as correct classifications. An additional outline-only score separates segmentation quality from class errors.

Each new model and its current counterpart are scored on identical comparison images and annotations. Higher scores are better within that model and metric. Scores from different metrics cannot be compared directly. A higher score on a small image set does not establish broad scientific usefulness.

Cellpose trains one specialist for each represented target class: healthy cells, dead cells, healthy groups and dead groups. Each starts independently from CPDINO pretrained weights and sees the same approved training images. Only its target outlines receive foreground instance labels; other objects remain visible as background. Images with no target outlines are retained as negative examples. Training must include both healthy and dead targets, which may be individual cells or groups. Classes without training examples receive no specialist. Only the healthy individual-cell specialist receives the predicted nuclear helper. Dead and group specialists keep microscopy-image input. Helper training uses preparation-grouped cross-fitting, training-only helper dropout and explicit channel normalization. The configured epoch count applies to each specialist; a dataset with all four classes requires four sequential training passes.

The Cellpose results identify each specialist's actual backbone, initial weights and training outline count. Controlled training stops if CPDINO cannot initialize, keeping the backbone consistent between experiment arms. StarDist helper generation runs before Cellpose in an isolated process; helper caches remain outside the frozen compiled dataset. Cross-class predictions covering at least half the smaller mask are combined into an amber unclassified outline for manual review; confidence values from independent models are not used to choose a winner.

## Saved weights, current models and installed models

A **saved training run** contains all three successfully trained model artifacts and their results. Its files remain available even when no current model improves.

**Current models** are the model-repository files offered by Update Models. The scout is compared independently. StarDist and the Cellpose bundle consuming its exact helper are compared and replaced together based on downstream Cellpose performance:

- **Updated:** the trained model scored strictly higher than its current counterpart on held-out comparison images.
- **Kept current model:** the new score was equal or lower, or no held-out comparison images were available. The trained weights remain saved with the run.
- **First model saved:** no current model existed and the architecture passed its required experiment gate, so verified weights became current.
- **Awaiting experiment validation:** candidate weights are archived while the first centroid scout or nuclear-assisted release waits for a passing architecture receipt.

Training-image diagnostic scores cannot justify replacing an existing model. Missing comparison images do not prevent training or saving. Authentication, download and evaluation errors stop the update; they are not interpreted as an absent model.

All three model roles must finish successfully before current models change. Qualifying replacements occur together in one atomic update, with unchanged models retained. Failed or cancelled training cannot install a partial set. A completed run with zero improvements is successful and requires no model download.

**Installed models** are the local files used by TEMPO. Training does not automatically replace those files. Open **Settings → Application Tools → Update Models**, then choose **Install Updates**. The complete current set is downloaded and its checksums verified before local replacement. Installation failure triggers rollback. Install updates between active acquisition or analysis jobs.

## Progress and cancellation

Initial package setup and GPU checks appear in Colab. TEMPO begins showing a run after Colab registers it, before dataset compilation and model training.

The runner writes progress to Hugging Face at most once per minute. Cancellation checks continue approximately every 10 seconds; TEMPO reads progress approximately every 15 seconds. Checkpoint saves can also update progress. Network delays and server cooldowns can increase the visible delay. Progress does not depend on reading the browser.

Failed status uploads retry at increasing intervals, respecting the server's retry interval. Training can continue while TEMPO progress is stale. Cancellation checks retry separately; if those checks also fail, stop execution in Colab. Colab output identifies the failed operation and includes available HTTP error details with tokens removed.

TEMPO shows the assigned GPU, current phase, elapsed time and measured epoch counts. Some libraries report epochs at intervals; counts may advance in batches. When a count is unavailable, progress remains indeterminate rather than showing an estimated percentage. Trial completion, full training completion, saving and model replacement are separate states. Detailed output and logs remain in Colab.

Colab's `RUNNING` messages mean that a worker process remains alive; they do not confirm advancing epochs. StarDist reports nuclear training and cross-fit helper stages. Thresholds are not optimized against incomplete nuclear labels. Evaluation reports prediction and cell matching separately for each image.

Training can continue with TEMPO closed. Reopening the application retrieves the latest recorded status. A fresh status means the runner reported recently; it does not establish successful completion.

Before run registration, execution can be stopped only through Colab. **Cancel Run** writes a cancellation request for a registered run. The runner checks approximately every 10 seconds and stops its training processes before model replacement. Cancellation is a request, not an immediate GPU disconnect. Once the atomic model update begins, cancellation is disabled because that commit cannot be reliably interrupted. Colab's stop control can also interrupt execution.

## Disconnection and recovery

A heartbeat is the timestamp of the latest runner status. After 60 seconds without an update, TEMPO marks it stale and directs attention to Colab. A stale heartbeat can reflect network trouble; it does not prove the GPU process stopped.

**Refresh Status** checks the latest saved run status. If the check times out, retry.

After two minutes without a heartbeat, **Resolve Disconnected Run** can clear the recorded active run. Stop the old cell or terminate its Colab runtime first. Clearing the record does not stop that process. TEMPO rechecks the heartbeat before clearing; a resumed active heartbeat prevents the operation.

Choose **Clear Run Record** to confirm that execution has stopped. A confirmed clear immediately updates TEMPO and permits another notebook run. If the run has already finished or the active run has changed, TEMPO displays the latest record without clearing it. Recovery and cancellation require a successful server write; authentication, access and rate-limit errors remain visible. Failed or uncertain writes do not establish that a record was cleared.

An existing active run blocks another registration. Inspect its status and Colab execution before retrying. Rerunning a training cell belonging to a completed run must not repeat training or saving; use Run all to create a new run when appropriate.

Change settings before starting a run. Avoid editing its extracted code, labels, settings or weight files during execution. Verification checks reject changed files. Preserve useful logs and weights before deleting or resetting the Colab runtime; local Colab files can be lost when the runtime ends.

Keep tokens in Secrets. Do not place them in notebook code, saved outputs, screenshots or shared logs. A failed final status update can leave saved models with an uncertain completion state; inspect the run and model records before repeating writes.

## Controlled model evaluation

See [Model Evaluation](MODEL_EVALUATION_WORKFLOW.md) for loading a protocol, running stages, reviewing results and recovering interrupted runs.
