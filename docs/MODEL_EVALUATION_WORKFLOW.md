# Model Evaluation

Use Model Evaluation to compare models or settings with a prepared evaluation protocol. The selected protocol determines the available settings, stages, measurements and image comparisons.

## Start an evaluation

1. Open **Maintainer → Model Evaluation** and select **New evaluation**.
2. Click **Load Protocol** and select the protocol's `protocol.json`.
3. Set its options and required GPU. Review the displayed stages, then click **Create Evaluation**.
4. Review **Images and Settings**. Settings and image assignments are fixed after creation; create another evaluation to change them.

## Run stages in Colab

Use the maintained `TEMPO_Colab_Trainer.ipynb` for regular training and evaluation protocols.

1. For first-time setup or an outdated notebook, choose **Save Colab Notebook**, upload it to Colab, and save its URL through **Maintainer → Cloud training → Edit Notebook URL** using **Update Shared Settings**. Do this before queuing or resuming a stage. See [Training Workflow](TRAINING_WORKFLOW.md) for account setup.
2. Click **Run Stage** (or **Resume Stage**). Wait for **Awaiting Colab** for the intended stage, then click **Open Notebook**. Run Stage queues the next unfinished stage; the selector in Results reviews completed reports.
3. In Colab, enable access to the `HF_TOKEN` secret, connect a GPU runtime and choose **Run all**. Queuing a stage in TEMPO does not start Colab.
4. Wait for successful completion through the notebook's final **Results** cell. Check its evaluation identifier and stage against TEMPO's saved report before proceeding. Then review the completed stage and run the next stage in the same way, choosing **Run all** again.

## Review results

**Results** shows each completed stage's measurements and summary. Image count, execution time and GPU appear alongside the protocol's results. Execution time includes stage preparation and work; use the protocol's timing measurements when comparing inference speed. **Run Details** provides the saved settings, data versions and execution identifiers.

Use **Images** for any supplied visual comparisons. Choose the image and views to compare; pan and zoom are linked, and **Fit** restores the full view. Some protocols produce numerical results without images.

When a final decision passes, **Record Decision** records its evidence and any proposed regular-training settings. It does not publish or install model weights. Choose **Return to Regular Training** when finished; until then, the notebook remains assigned to the evaluation.

If the evaluation proposes model artifacts, **Publish Models** becomes available after recording the decision. Review the selected models, preprocessing and scope before publishing. TEMPO checks the saved weights and dependencies, then publishes the models and their regular-training recipe together. Use **Settings → Update Models** to install that release. Publication reuses the evaluated weights; it does not train again. A report without a model proposal remains a recorded evaluation only.

## Interruptions and recovery

**Cancel Run** requests a stop. If Colab is disconnected and cannot receive the request, stop execution in Colab directly. Use **Resume Stage** to retry an interrupted stage; completed stages are retained.

If scout training finished but scoring was interrupted, resuming can verify the saved model and rerun scoring without training it again. Changed inputs or weights produce an error; the saved files are retained.

After more than two minutes without an update, **Recover Stopped Run** becomes available. Stop the previous Colab execution before recovering: recovery does not stop that process. Then use **Resume Stage** and run the notebook again.
