# Model Evaluation

Use Model Evaluation to compare models or settings with a prepared evaluation protocol. The selected protocol determines the available settings, stages, measurements and image comparisons.

## Start an evaluation

1. Open **Maintainer → Model Evaluation**. If the evaluation is already prepared, review **Images and Settings** and continue its stages. To start a new comparison, select **New evaluation**.
2. Click **Load Protocol** and select the protocol's `protocol.json`.
3. Set its options and required GPU. Review the displayed stages, then click **Create Evaluation**.
4. Review **Images and Settings**. Settings and image assignments are fixed after creation; create another evaluation to change them.

## Run stages in Colab

Use the workspace's shared Colab notebook with your own Google account. Confirm that the notebook targets the active repositories and that your `HF_TOKEN` can write to both before queuing work. The [training workflow](TRAINING_WORKFLOW.md) covers credential access and replacing an outdated or inaccessible notebook.

1. Click **Run Stage** (or **Resume Stage**). Wait for **Awaiting Colab** for the intended stage, then click **Open Notebook**. Run Stage queues the next unfinished stage. Use the Results selector to review completed reports.
2. In Colab, connect the GPU required by the queued stage and choose **Run all** to start execution.
3. Wait for the notebook's final **Results** cell. Check its evaluation identifier and stage against TEMPO's saved report. Review the results before queuing the next stage.

## Review results

**Results** shows each completed stage's measurements and summary. Image count, execution time and GPU appear alongside the protocol's results. Execution time includes stage preparation and work; use the protocol's timing measurements when comparing inference speed. **Run Details** provides the saved settings, data versions and execution identifiers.

Use **Images** for any supplied visual comparisons. Choose the image and views to compare; pan and zoom are linked, and **Fit** restores the full view. Some protocols produce numerical results without images.

When a final decision passes, choose **Record Decision** to save its evidence and any proposed regular-training settings. Before starting an ordinary training run, choose **Return to Regular Training** to clear the notebook's evaluation assignment.

If the evaluation proposes model artifacts, **Publish Models** becomes available after recording the decision. Review the selected models, preprocessing, and scope before publishing. TEMPO publishes the verified weights with their regular-training recipe. Use **Settings → Application Tools → Update Models** to install that release. A report without a model proposal remains a recorded evaluation only.

## Interruptions and recovery

**Cancel Run** requests a stop. If Colab is disconnected and cannot receive the request, stop execution in Colab directly. Use **Resume Stage** to retry an interrupted stage; completed stages are retained.

If scout training finished but scoring was interrupted, resuming can verify the saved model and rerun scoring without training it again. Changed inputs or weights produce an error; the saved files are retained.

After more than two minutes without an update, **Recover Stopped Run** becomes available. Stop the previous Colab execution, choose **Recover Stopped Run**, then use **Resume Stage** and run the notebook again.
