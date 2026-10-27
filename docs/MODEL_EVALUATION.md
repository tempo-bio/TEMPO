# Model Evaluation developer reference

Model Evaluation is a maintainer tool for running **developer-supplied evaluation protocols**. A protocol defines the models or conditions to compare, its settings and stages, the data it needs, its measurements and its decision rules. TEMPO provides the shared interface, immutable snapshots, Colab execution, progress, cancellation, recoverable artifacts and report display.

For operating instructions, see [Model Evaluation workflow](MODEL_EVALUATION_WORKFLOW.md), also available through the application's **Instructions** button. This reference covers evaluation design and the Python protocol interface.

Every completed stage retains a common summary of snapshot size, execution time and actual GPU. **Run Details** includes the protocol/version, frozen settings, image-role counts, dataset/model revisions, source checksums and attempt identity. Execution time measures that attempt's preparation and stage work; inference latency or other scientific timing belongs in the protocol's own measurements. Scientific score tables and image views vary with what the selected evaluation measures.

## Designing an evaluation

Define the question and decision before opening reserved evaluation data. Identify the reference and candidate conditions, the intended use and the scope of any conclusion. Change one factor at a time when the goal is to attribute an effect; otherwise define a factorial or other controlled design explicitly in the protocol.

Choose the independent unit to match the scientific question. Related frames, views, wells, preparations or subjects may need to remain together. Fields, objects and repeated training seeds are not automatically independent biological replicates. Preserve prior exposure: an image used to fit or select a model cannot later become untouched evaluation evidence for that comparison.

Separate model fitting, parameter/model selection and final measurement. Fix candidate selection before inspecting reserved data. State whether labels are exhaustive, partial or unknown, and define which predictions can be scored. Missing annotations must not silently become negative truth. Specify matching rules, units, class handling, aggregation and missing-data behavior.

Define the practical success criterion, uncertainty estimate and regression checks for the intended decision. A metric's direction must be clear: higher may be better for one measurement and lower for another. Missing coverage or an unsupported interval is inconclusive. Display the evidence even when a decision fails. Numerical results and targeted visual review should support the conclusion together.

Validate the independent-unit and subgroup coverage needed for those criteria before expensive fitting. If the available data support only descriptive results, make that limitation explicit before the run rather than presenting an unattainable acceptance decision as the objective.

For preprocessing comparisons, include a baseline and use a bounded, predefined set of transformations. Save exact parameters, operation order, spatial units, clipping/encoding and library behavior with the model; training and inference must use the same recipe. Select against development outcomes for the intended task. Different model roles may need different recipes: compiled datasets can carry `scout_preprocessing` separately from `preprocessing`, and a regular-training architecture receipt can specify `scout_profile` separately from `profile`. Older receipts without these fields retain their shared-recipe behavior. When isolating another component's effect, hold the relevant preprocessing fixed within that comparison.

Use a separate software-check dataset when testing new training or scoring code. A short execution establishes feasibility, not improved model performance. Inspecting a reserved cohort during debugging changes what can honestly be claimed about subsequent evaluations on that cohort.

## Evaluation protocols

A passing decision may include `model_release` with format `tempo-evaluated-model-release-v1`, a `models` object keyed by TEMPO model role, and optional `training_receipt` settings. Each model declares its workspace-relative `artifact` and its evaluation `metrics`. For example, `models.cellpose.artifact` can identify a frozen Cellpose bundle; its exact nuclear helper must also be proposed when the bundle depends on one. The saved artifact inventory supplies the authoritative checksums. Keep study-specific paths and selection logic in the protocol, rather than adding branches to the application.

For an unassisted Cellpose condition using normalization, the optional proposal has this structure:

```json
{
  "format": "tempo-evaluated-model-release-v1",
  "models": {
    "cellpose": {
      "artifact": "results/selected/cellpose4_cardio.pt",
      "metrics": {"metric": "cell_class_F1_IoU50", "score": 0.8}
    }
  },
  "training_receipt": {"profile": "P0", "nuclear_assistance_passed": false}
}
```

Use measured metrics and the selected artifact's actual recipe. Supported model-role keys are `cellpose`, `stardist` and `nano_scout`. A protocol may propose any subset consistent with the published dependency graph; it need not fit every architecture.

The recipe may declare per-role `batch_sizes`, for example `{"cellpose": 1, "stardist": 4}`. Regular training applies those sizes to the corresponding workers and keeps the existing defaults for roles not declared. This preserves a tested memory budget without tying every model to one batch size. CPDINO inference requires a native Python 3.11+ environment with the DINOv3 dependency from `requirements-training.txt`; the hardware-acquisition environment remains separate.

An evaluation that only adopts training settings can still use `decision.training_receipt`. Recording that decision binds the protected override to the current model identities. Progress commits do not invalidate it; changed model records do. Regular training applies it to those models; otherwise it uses the recipe published atomically with the weights. This prevents an older dataset-side receipt from overriding a newly published release while its compatibility mirror is pending.

Recording a decision and publishing its model release are separate maintainer actions. Publication checks the approved report, downloads and verifies the proposed artifacts, preserves unrelated models, and commits the coupled weights, recipe and provenance together. Regular cloud training uses the recipe pinned in that model release, with the older dataset receipt retained as a compatibility fallback. Approved evaluations in the protected repository can attach an explicit selection through `tempo_model_adoption.save_proposal` without editing their frozen report. `prepare_adoption` is read-only; `publish_adoption` performs publication and records its resulting revision. Previous model revisions remain available for rollback.

A protocol is a folder containing `protocol.json`, its Python entry point and the source files explicitly listed in the descriptor. The folder can remain local and ignored. TEMPO freezes the selected files into a private source archive when creating an evaluation. No permanent GUI edit, built-in study branch or application rebuild is needed to load another protocol using this interface.

Evaluation format `tempo-model-evaluation-v3` keeps the queue, plan, source/protocol archives, artifacts, reports and approval receipts in the protected model repository. Its repository revisions are distinct from the dataset revision used for annotations and images. Image-role reservations and compatibility training-setting mirrors remain in the dataset. Reserve image exposure before publishing the protected evaluation; if publication fails, reservations remain conservative and the evaluation must be prepared again. No evaluation content is automatically trusted from older dataset-side archives. Recreate an older evaluation from reviewed local sources, and replace uploaded notebooks with the regenerated maintained notebook. See [execution security](CREDENTIAL_SECURITY.md#executable-content-and-downloads) for the required permissions and trust assumptions.

Example descriptor structure:

```json
{
  "format": "tempo-evaluation-protocol-v1",
  "id": "my-model-comparison",
  "version": "1",
  "name": "Model comparison",
  "entrypoint": "protocol.py",
  "files": ["protocol.py"],
  "parameters": [
    {"name": "repeats", "label": "Repeats", "type": "integer", "default": 3, "minimum": 1, "maximum": 20}
  ],
  "stages": [
    {"id": "selection", "label": "Model selection"},
    {"id": "measurement", "label": "Final measurement"}
  ]
}
```

Identifiers use lowercase letters, digits, underscores or hyphens and start with a letter. Stage identifiers and parameter names must be unique. Parameter types are `integer`, `number`, `boolean`, `choice` and `text`; supply defaults and appropriate bounds or `choices`. These definitions create the GUI's controls and validate saved settings.

### Responding to user selections

Use `visible_when` on a parameter and `when` on a stage to adapt one protocol to the selected test. For example, these descriptor fields show different controls and execute different stages for speed and quality evaluations:

```json
{
  "parameters": [
    {"name": "test", "label": "Test", "type": "choice", "choices": ["speed", "quality"], "default": "speed"},
    {"name": "batch", "label": "Batch size", "type": "integer", "default": 4, "visible_when": {"test": "speed"}},
    {"name": "threshold", "label": "Threshold", "type": "number", "default": 0.5, "visible_when": {"test": "quality"}}
  ],
  "stages": [
    {"id": "speed", "label": "Speed measurement", "when": {"test": "speed"}},
    {"id": "quality", "label": "Quality measurement", "when": {"test": "quality"}}
  ]
}
```

Conditions match declared parameter values. Multiple keys are combined with AND; a list of expected values accepts any member of that list. The GUI immediately updates visible fields and the stage preview as a selection changes. A setting cannot hide itself. Hidden values remain recorded for reproducibility; the protocol must use settings appropriate to the selected test. A selection that activates no stages cannot create an evaluation.

The active stage list is frozen as `plan['stages']`. Inactive stages cannot be queued and do not appear as missing results. Choose the appropriate metrics, tables and views in `run` using the frozen selection. Results from an existing snapshot remain tied to its original settings; changing a new form does not reinterpret old results.

List every required source/configuration file in `files`. Use relative imports for sibling Python modules, such as `from . import scoring`. The protocol is loaded in its own module namespace so different packages do not reuse each other's imported modules. Files must remain inside the protocol folder; duplicate paths, traversal, external symlinks and oversized archives are rejected. The source archive is limited to 20 MiB; model weights belong in artifact storage.

Optional `requirements` names a bundled requirements file, also listed in `files`. Colab installs it before importing the protocol, then checks package compatibility. Pin dependencies compatible with the maintained training environment. The existing host runs in Colab with a CUDA runtime and the application's model stack. Protocols may use other Python model libraries through declared dependencies; a different system/runtime integration still requires an implementation appropriate to that platform.

### Preparation contract

The entry point exports `prepare(context)` and `run(context)`. `prepare` runs during snapshot creation, before training. Keep it limited to input validation and preparation; do not fit models, inspect candidate predictions or perform long-running GPU work in it. Put training-only imports inside `run` so preparation works in the GUI's environment, including the separate acquisition interpreter on macOS.

The preparation context provides:

| Attribute | Meaning |
| --- | --- |
| `records` | Approved annotations and approved supplementary labels at the pinned dataset revision. |
| `previous_splits` | Earlier image roles, including known training exposure and saved evaluation reservations, with reconciled aliases. |
| `parameters` | Validated values from the protocol's GUI settings. |
| `stages` | Active stages selected by those values. |
| `dataset_revision` | The dataset revision being frozen. |

Return a JSON object containing `scope` and `images`. `scope` describes the evidence's population or evaluation unit; optional `scope_label` supplies a readable display name when the scope is a machine identifier. `images` maps selected approved filenames to objects with `split` (`train`, `val` or `test`) and `unit` (the independent-unit identifier). Include any additional protocol metadata needed by the runtime. All images sharing a unit must keep the same role. The host rejects reassignment of prior training/validation exposure to a less exposed role, unknown images and duplicate or unverified identities.

Protocols can use `tempo_training_groups.plan_splits` to retain the standard automatic assignments or implement another exposure-preserving allocation. Group related images before assigning roles where the study requires it. Requirements for classes, labels, sample counts, grouping or coverage belong in the protocol's preparation function, which raises an actionable error when they are unmet. An evaluation without image data, such as a model resource benchmark, can return an empty `images` mapping.

Historical split files can include unannotated pool entries that were never selected as model inputs. An assignment alone does not prove exposure. The host reconciles these only when a pinned approved-only training snapshot proves the entries were empty, the current model weights match that request's baseline, and saved evaluation plans preserve any subsequent selections. Unknown or annotated history stays protected. The frozen plan records `assignment_resolution`; the shared ledger's checksum and provenance are saved together so an older model split file cannot reintroduce disproven assignments. Unreadable historical inputs or changed ledger provenance stop preparation rather than clearing exposure.

### Runtime contract

`run(context)` executes the queued stage in Colab and returns its JSON report. The protocol owns training, inference, stage-internal loops and scoring. It may use existing TEMPO model helpers or its own model implementation.

| Context member | Use |
| --- | --- |
| `plan`, `stage`, `parameters` | Frozen plan, current stage descriptor and parameter values. |
| `input_root` | Prepared inputs under `datasets/images/` and `datasets/universal_annotations.json`. |
| `workspace` | Stable evaluation workspace; write outputs under `results/`. |
| `engine_root` | Frozen TEMPO source directory for calling existing workers. |
| `reports` | Reports from previously completed stages. |
| `progress(message, **details)` | Report actual work and check execution ownership/cancellation. |
| `check_cancelled()`, `cancelled()` | Cooperatively check for cancellation. |
| `run_command(command, log=…, timeout=…)` | Run a cancellable subprocess with a retained log. |
| `checkpoint()` | Persist completed outputs and their checksums privately. |
| `materialize(path)` | Restore a saved artifact evicted from the local cache. |
| `model_path(filename)` | Retrieve a model from the model-repository revision pinned with the plan. |
| `freeze_selection(value)` | Persist an immutable selected model/configuration before reserved-data measurement. |

Checkpoint after meaningful completed work. Make stage-internal tasks resumable by recording their input identity and output checksums, then verifying them before reuse. Do not treat a partial checkpoint as a completed task. Long loops and subprocesses must remain cancellable.

Persist the selected configurations and exact candidate artifacts before scoring reserved data. On recovery, verify and reuse that selection; never quietly retrain a missing candidate against an already inspected cohort. The host supplies immutable selection storage, but the protocol must call it at the correct point and keep its training and scoring order scientifically valid.

### Report contract

Return a JSON object with an optional `summary`, `tables`, `previews`, `decision` and protocol-specific `details`. The host adds reserved `execution` metadata for the common run summary and reproducibility details:

```json
{
  "summary": "Summary supported by the measured results.",
  "tables": [{
    "title": "Measurements",
    "columns": ["Condition", "Measurement", "Unit"],
    "rows": [["reference", 12.0, "milliseconds"], ["candidate", 9.0, "milliseconds"]]
  }],
  "previews": [{
    "name": "Example input",
    "views": {"Reference": "results/reference.png", "Candidate": "results/candidate.png"}
  }],
  "decision": {"passed": true, "summary": "Explanation using the predefined rule."}
}
```

The values above illustrate the report format; a real protocol supplies its measurements. Table titles, columns, rows and image-view labels come from the protocol. Rows must match their columns. Numbers must be finite; use `null` for unavailable measurements. Image paths are relative to the workspace and must refer to retained, checksummed artifacts. Image comparisons are optional and synchronize pan and zoom.

`decision.passed` is `true`, `false` or `null` for inconclusive; a report may omit a decision when none applies. Only a passing decision from the final stage can be recorded. Provide the criteria and evidence in the report tables and summary rather than relying on a pass label alone.

An optional `decision.training_receipt` proposes settings for the existing regular-training receipt interface. Use it only when regular training understands those settings and the protocol actually validates them. The maintainer must explicitly record the decision before the host writes that receipt. Other evaluations can record evidence without proposing training settings. Evaluation never publishes model weights automatically.

## Interruptions and recovery

**Cancel Run** requests cancellation of the current attempt. If Colab cannot read cancellation requests, stop its execution directly. An interrupted stage can be queued with **Resume Stage**; completed stages are verified and retained.

After more than two minutes without a heartbeat, **Recover Stopped Run** can release a disconnected attempt. Stop the old runtime first. Recovery fences the old attempt out of later progress and result writes; it does not terminate the old process. Resume uses a new attempt identifier and the same frozen plan.

The protected private model repository stores plans, protocol/engine archives, stage reports, state and checksummed workspace artifacts under `training/evaluations/<id>/`. The notebook uses a stable `/content/TEMPO-evaluations/<id>/` workspace. Large saved models can be restored on demand after local cache eviction. Keep adequate private artifact storage for the protocol's workload.

The maintained notebook archives its execution controller and support files under `runtimes/<runtime_sha256>.zip`. `tempo_training_runtime.py` contains narrowly matched compatibility rules for known source checksums; it does not select fixes by study name or evaluation ID. Repairs load through an explicit worker launcher/import loader, retain original engine paths, and leave the frozen source files intact. Execution reports include the runtime checksum and applied repair identifiers. Use both engine and runtime identities when reproducing an execution. Unknown source revisions receive no automatic rewrite.

TEMPO scout workers save `trained_candidate.json` after fitting/export and before benchmarking. The receipt binds the model checksum to the compiled dataset digest, seed, epochs, batch size and baseline identity. A retry may score that exact export without fitting again; changed inputs or weights are rejected. Supported legacy evaluation workers can adopt their completed final exports after checking embedded training arguments and scout metadata. Keep protocol-level task identities and checkpoint calls: the worker receipt does not replace a protocol's completion record.

Completed evaluation intent remains selected until the user queues another stage or chooses **Return to Regular Training**. Repeated Run all cannot reset a completed stage or silently start ordinary training. A new snapshot does not turn previously inspected images into fresh evidence.

## Developer verification

Test preparation with missing and partial inputs, prior exposure, related-unit leakage and alternative settings. Test the metric/matching rules independently using known outcomes. Exercise one-stage and multi-stage execution, custom tables/views, empty reports, cancellation, cold restoration and final-decision integrity. Use a mocked protocol to verify infrastructure and a separate real software-check run to verify the model runtime.

## Repository writes and stopped-runtime preservation

Progress uploads are limited to once per minute while cancellation remains independently polled every ten seconds. The evaluation controller shares a commit budget between progress, checkpoints and final status, spacing attempts by at least 35 seconds. Progress yields to checkpoint saves, and unchanged checkpoints do not create commits. HTTP 429 responses delay mandatory saves using the server's reset hint; an hourly commit-limit response without a reset hint receives a conservative one-hour cooldown. A failed final-status save must not replace an earlier training or scoring exception.

If a stopped Colab runtime still contains unsaved files, generate a save-only cell with `python -m notebooks.build_evaluation_recovery --output recovery-cell.txt` and run its contents in that same notebook and runtime. It creates a checksummed local backup and requests its download before attempting remote preservation. The cell does not fit models or publish weights. It verifies preserved model bytes before printing `PRESERVATION CONFIRMED`. Keep the runtime connected until that confirmation; the server cooldown can still apply. Recovery can atomically fence the exact stopped job when its final status write failed, provided its heartbeat is stale; a different or fresh job is rejected. Protocol, plan and model identities remain fixed.

Review the actual GUI at normal, wide, small-window and enlarged-font sizes with populated, empty, running and failed states. Instructions open through a labelled button in a scrollable dialog. Information hovers, where needed elsewhere, are for short definitions or caveats. Keep study recipes and generated evidence local and outside Git.

The [development guide](DEVELOPMENT_AND_RELEASE_GUIDE.md#model-evaluation) maps the implementation and verification entry points. Update the shared protocol contract when its schema or behavior changes. Rebuild the maintained notebook after changing bundled host sources; keep it free of saved outputs and private values.
