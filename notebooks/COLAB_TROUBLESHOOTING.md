# Troubleshoot Colab training

The [Training workflow](../docs/TRAINING_WORKFLOW.md) is the maintained guide for setup, execution, evaluation, model updates and recovery. It is also available in **Maintainer → Cloud training → Training Workflow**.

Open the shared notebook with your own Google account and enable your own `HF_TOKEN` through Colab Secrets. If the shared URL is unavailable or points to older training code, follow the [notebook replacement procedure](../docs/TRAINING_WORKFLOW.md#replace-the-notebook-when-needed).

## The notebook points to a different workspace

Run only the **Settings** cell and compare its dataset and model repository output with **Settings → Account and Shared Data** in TEMPO-C. After a repository change, refresh and restart TEMPO-C, then export a notebook for the new repositories and ask the workspace administrator to publish its URL. Changing the URL or refreshing desktop settings leaves existing notebook copies pointed at their original repositories. [Verify the notebook](../docs/TRAINING_WORKFLOW.md#verify-the-notebook-before-starting)

## The notebook stops during setup

Read the first failed setup cell before restarting the runtime. For a GPU mismatch, select the requested GPU in Colab or change **Required GPU** and choose **Run all** again. Colab controls which GPUs are available to your account.

If package setup fails, retain its log and check the assigned Python version. The maintained notebook supports Python 3.12–3.13 and installs PyTorch's CUDA 12.6 build in an isolated environment. For a controlled Cellpose run, check the CPDINO initialization log and pinned DINOv3 dependency.

## Run all opens an evaluation

A queued evaluation takes precedence over the notebook's ordinary training settings. Open **Maintainer → Model Evaluation** to confirm the evaluation ID and stage. Use **Run Stage** or **Resume Stage** to queue the work you want, or choose **Return to Regular Training** before launching an ordinary run. [Model Evaluation workflow](../docs/MODEL_EVALUATION_WORKFLOW.md)

## Logs and retained files

Each execution has a separate folder at `/content/TEMPO-cloud/<run-id>/<bundle-prefix>/`. Its `runs/` directory contains setup, diagnostic and training logs, candidate weights and results. Output streams during execution, including elapsed-time reports during quiet periods. Download useful files before resetting or deleting the runtime.

Model Evaluation also maintains `/content/TEMPO-evaluations/<evaluation-id>/` across its stages. Completed stages, verified weights and reports are checkpointed under `training/evaluations/<evaluation-id>/` in the protected private model repository. Resume restores completed stages after a runtime reset. Local large-weight caching is bounded; an interrupted training task restarts, while completed tasks are reused. Recovery after final evaluation opens must preserve the selected checkpoints.

Automatic one-epoch trials retain per-model logs under `runs/training-checks/<check-id>/` and partial results in `summary.json` and `runs/preflight_report.json`. A failed trial stops before launching another model. Training has no fixed time limit; cancellation remains available. Package and GPU diagnostics retain their bounded time limits.

`RUNNING` reports worker-process elapsed time, not completed epochs. For a quiet StarDist worker, inspect its last model output before these messages to distinguish initialization, training, threshold selection and saving.

Status-upload and cancellation-check failures appear separately, with available HTTP status and error details. Failed requests back off independently and honor `Retry-After`. Status-upload failure does not stop GPU work. If cancellation checks fail, use Colab's stop control; clearing TEMPO's stale record does not terminate the worker.

Successful regular runs archive the three model-role artifacts and results in the model repository. Current-model updates use same-image score comparisons. Archive completion and replacement counts are reported separately. Colab archives evaluation candidates in the private model repository. When a passing evaluation proposes models, publication is a separate maintainer action in TEMPO. [Review evaluation results](../docs/MODEL_EVALUATION_WORKFLOW.md#review-results)

## Maintained notebook generation

From the project root:

```sh
python -m notebooks.build_staged_colab_notebook
```

The builder packages only maintained training sources and generates the notebook without credentials, personal preferences, execution counts or saved outputs. Regenerate it after changing included training sources.
