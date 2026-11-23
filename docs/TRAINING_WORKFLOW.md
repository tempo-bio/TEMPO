# Training workflow

Maintainers run training in Colab using approved images from the shared dataset. Regular training saves its results to the model repository and can automatically replace the models offered to the whole workspace.

## Verify the notebook before starting

1. Sign in to Colab with your own Google account. In TEMPO, choose **Maintainer → Cloud training → Open Notebook** to open the shared notebook in your system's default browser. If access is denied, ask the notebook owner to share it. If you land on the Colab homepage, check the shared notebook URL with the workspace administrator.
2. Open **Secrets** in the Colab sidebar. Add your own Hugging Face token as `HF_TOKEN` and enable **Notebook access** for it. Use an account authorized as a dataset maintainer and a token with read/write access to both the dataset and model repositories. The token in Colab is configured separately from the TEMPO-C desktop sign-in.
3. In Colab, choose **Runtime → Change runtime type** and select a GPU. The notebook defaults to **Required GPU** set to **A100**. **Any CUDA GPU** permits another assigned CUDA GPU. GPU availability depends on your Colab account.
4. For a handoff or a new notebook, run only the code cell under **Settings**. Check **Assigned GPU**, **Dataset repository**, **Model repository**, and **Workflow** in the cell output. Compare the repository IDs with **Settings → Account and Shared Data** in TEMPO-C. The cell checks the notebook configuration and the evaluation assignment without starting training or writing to Hugging Face. Verify repository write access separately.

If the IDs differ, refresh the app's shared settings and check that you opened the intended notebook. A notebook keeps the repository IDs it was created with. Export a matching notebook if the workspace repositories changed.

## Run training

1. In TEMPO-C, review annotations and approve the intended images. Check that the approved inputs have healthy and dead targets for Cellpose and reviewed nuclei for StarDist, with enough independent groups for nuclear-helper cross-fitting.
2. Check **Maintainer → Model Evaluation** for an evaluation assignment. A queued stage takes precedence over regular training and uses its own saved settings. To switch back, coordinate with the evaluation maintainer. Finish or cancel the queued stage, then choose **Return to Regular Training**.
3. In the notebook, set **Maximum epochs** and **Required GPU**, then choose **Runtime → Run all**. Check that the **Settings** output shows **Regular training** for an ordinary run.
4. After execution finishes, inspect the **Results** cell for its run ID, dataset revision, evaluation basis and model outcomes. Each model reports its score and the previous model's score when available. The current weights are evaluated using the same comparison images.

Run all checks the runtime, selects approved images, trains and compares the models, then saves the results. A failed check stops execution.

**Maximum epochs** accepts whole numbers from 1 to 10,000 and defaults to 150. An epoch is a training cycle. Image and patch sampling differ between model libraries, and early stopping can end a model before its maximum.

For requests longer than one epoch, a one-epoch trial runs automatically before full training. Its temporary weights are separate from the full run and cannot replace current models. Setting **Maximum epochs** to **1** starts a regular run that can save weights and replace current models. For an end-to-end training rehearsal, use a separate private software-check dataset, model repository, and notebook configured for those repositories.

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

Assisted training uses at least four units assigned to training, with reviewed nuclei in at least three of them. Cross-fitting trains the helper without using the images it will assist. The validated training configuration determines whether those units are fields or biological preparations. Scores on training images are labelled as diagnostics and cannot establish architecture adoption.

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

Cellpose trains one specialist for each represented target class: healthy cells, dead cells, healthy groups and dead groups. Each initializes independently and sees the same approved training images. Only its target outlines receive foreground instance labels; other objects remain visible as background. Images with no target outlines are retained as negative examples. Training must include both healthy and dead targets, which may be individual cells or groups. Classes without training examples receive no specialist. Only the healthy individual-cell specialist receives the predicted nuclear helper. Dead and group specialists keep microscopy-image input. Helper training uses grouped cross-fitting, training-only helper dropout and explicit channel normalization. The configured epoch count applies to each specialist; a dataset with all four classes requires four sequential training passes.

The Cellpose results identify each specialist's actual backbone, initial weights and training outline count. Regular training can fall back to Cellpose-SAM if CPDINO cannot initialize. Controlled CPDINO evaluations stop on initialization failure to keep the backbone consistent between experiment arms. StarDist helper generation runs before Cellpose in an isolated process; helper caches remain outside the frozen compiled dataset. Cross-class predictions covering at least half the smaller mask are combined into an amber unclassified outline for manual review; confidence values from independent models are not used to choose a winner.

## Saved weights, current models and installed models

A **saved training run** contains all three successfully trained model artifacts and their results. Its files remain available even when no current model improves.

**Current models** are the model-repository files offered by Update Models. The scout is compared independently. StarDist and the Cellpose bundle consuming its exact helper are compared and replaced together based on downstream Cellpose performance:

- **Updated:** the trained model scored strictly higher than its current counterpart on held-out comparison images.
- **Kept current model:** the new score was equal or lower, or no held-out comparison images were available. The trained weights remain saved with the run.
- **First model saved:** no current model existed and the architecture passed its required experiment gate, so verified weights became current.
- **Awaiting experiment validation:** candidate weights are archived while the first centroid scout or nuclear-assisted release waits for a passing architecture receipt.
- **Updated together** or **Kept coupled models:** the StarDist helper and Cellpose bundle share one replacement decision. The run records both outcomes against the same matched bundle.

Training-image diagnostic scores cannot justify replacing an existing model. Missing comparison images do not prevent training or saving. Authentication, download and evaluation errors stop the update; they are not interpreted as an absent model.

All three model roles must finish successfully before current models change. Qualifying replacements occur together in one atomic update, with unchanged models retained. Failed or cancelled training cannot install a partial set. A completed run with zero improvements is successful and requires no model download.

**Installed models** are the local files used by TEMPO. Training does not automatically replace those files. Open **Settings → Application Tools → Update Models**, then choose **Install Updates**. The complete current set is downloaded and its checksums verified before local replacement. Installation failure triggers rollback. Install updates between active acquisition or analysis jobs.

## Progress and cancellation

Initial package setup and GPU checks appear in Colab. TEMPO begins showing a run after Colab registers it, before dataset compilation and model training.

During steady training, the runner attempts a status upload about once a minute. Phase changes and checkpoint saves can send additional updates. The runner checks for cancellation about every 10 seconds, and TEMPO checks saved progress about every 15 seconds. Network delays and server cooldowns can increase those intervals.

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

An existing active run blocks another registration. Inspect its status and Colab execution before retrying. To start another run after completion, use **Run all** with the intended settings.

Change settings before starting a run. Avoid editing its extracted code, labels, settings or weight files during execution. Verification checks reject changed files. Preserve useful logs and weights before deleting or resetting the Colab runtime; local Colab files can be lost when the runtime ends.

Keep tokens in Secrets. Do not place them in notebook code, saved outputs, screenshots or shared logs. A failed final status update can leave saved models with an uncertain completion state; inspect the run and model records before repeating writes.

## Replace the notebook when needed

Keep using the shared notebook while its training code and repository IDs match the active TEMPO-C workspace and the maintainers can access it. Replace it when those change or when the incoming team needs a copy it controls.

1. In the matching TEMPO-C build, refresh the workspace settings and restart if the repository IDs changed. Choose **Maintainer → Model Evaluation → Save Colab Notebook**. The export embeds the active dataset and model repository IDs.
2. From the Google account that will own the replacement, use Colab's **File → Upload notebook** to open the export. Save it to Drive if needed and share it with the maintainers who will run it. Before sharing, clear prior cell output if you reused an existing copy.
3. Have the workspace administrator enter the new **Colab Notebook** URL in **Settings → Account and Shared Data**, then choose **Update Shared Settings**. **Maintainer → Cloud training → Edit Notebook URL** also opens that field.
4. Have a maintainer refresh shared settings, choose **Open Notebook**, enable their own `HF_TOKEN` for the new copy, and run only the **Settings** cell to confirm the new notebook points to the correct repositories.

The source checkout's `notebooks/TEMPO_Colab_Trainer.ipynb` targets the default workspace. To generate a notebook for other repositories, run this command from the matching checkout with the intended IDs. Choose an output path outside version control for a workspace-specific copy.

```sh
python -m notebooks.build_staged_colab_notebook --dataset-repo ORG/DATASET --models-repo ORG/MODELS --output /path/to/TEMPO_Colab_Trainer.ipynb
```

## Controlled model evaluation

See [Model Evaluation](MODEL_EVALUATION_WORKFLOW.md) for loading a protocol, running stages, reviewing results and recovering interrupted runs.
