# Colab training reference

The [Training workflow](../docs/TRAINING_WORKFLOW.md) is the maintained guide for setup, execution, evaluation, model updates and recovery. It is also available in **Maintainer → Cloud training → Training Workflow**.

## Environment

Packages install into an isolated environment using the notebook's included requirements and PyTorch's CUDA 12.6 build. Supported Python versions are 3.12–3.13. Setup uses the notebook kernel's pip with `--python`; training subprocesses use the Agg plotting backend.

GPU diagnostics and model workers run in separate processes. TensorBoard remains available to StarDist; the optional YOLO integration is disabled in its worker. The three roles are StarDist Nuclei, Cellpose Cells and Nano Scout. Controlled Cellpose training requires CPDINO and the pinned DINOv3 dependency; initialization failure stops the run. StarDist generates preparation-grouped training helpers before Cellpose starts.

For queued evaluations, the selected protocol checks its data requirements before creation. Follow the [Model Evaluation workflow](../docs/MODEL_EVALUATION_WORKFLOW.md) for running stages and recovering interrupted runs. Developers implementing a protocol can use the [protocol reference](../docs/MODEL_EVALUATION.md).

A queued evaluation overrides the notebook's ordinary training defaults and loads its frozen protocol and source bundle. Choose **Run all** for each queued stage and review its report in TEMPO before continuing. If the notebook says an evaluation is completed, failed or running, review it in TEMPO and queue its next/resumed stage. Choose **Return to Regular Training** to clear evaluation intent; another Run all then starts ordinary training.

A GPU mismatch stops execution. Select the requested GPU in Colab or change Required GPU before choosing Run all. Colab controls GPU availability.

## Logs and retained files

Each execution has a separate folder under `/content/TEMPO-cloud/`. Its `runs/` directory contains setup, diagnostic and training logs, candidate weights and results. Output streams during execution, including elapsed-time reports during quiet periods. Download useful files before resetting or deleting the runtime.

Model Evaluation also maintains `/content/TEMPO-evaluations/<evaluation-id>/` across its stages. Completed stages, verified weights and reports are checkpointed under `training/evaluations/<evaluation-id>/` in the protected private model repository. Resume restores completed stages after a runtime reset. Local large-weight caching is bounded; an interrupted training task restarts, while completed tasks are reused. Recovery after final evaluation opens must preserve the selected checkpoints.

Automatic one-epoch trials retain per-model logs under `runs/training-checks/<check-id>/` and partial results in `summary.json` and `runs/preflight_report.json`. A failed trial stops before launching another model. Training has no fixed time limit; cancellation remains available. Package and GPU diagnostics retain their bounded time limits.

`RUNNING` reports worker-process elapsed time, not completed epochs. For a quiet StarDist worker, inspect its last model output before these messages to distinguish initialization, training, threshold selection and saving.

Status-upload and cancellation-check failures appear separately, with available HTTP status and error details. Failed requests back off independently and honor `Retry-After`. Status-upload failure does not stop GPU work. If cancellation checks fail, use Colab's stop control; clearing TEMPO's stale record does not terminate the worker.

Successful regular runs archive the three model-role artifacts and results in the model repository. Current-model updates use same-image score comparisons; archive completion and replacement counts are reported separately. Model Evaluation archives its candidates separately in the protected private model repository and never publishes current models. A failed or cancelled run does not partially replace current models.

## Maintained notebook generation

From the project root:

```sh
python -m notebooks.build_staged_colab_notebook
```

The builder packages only maintained training sources and generates the notebook without credentials, personal preferences, execution counts or saved outputs. Regenerate it after changing included training sources.
