# Workspace administration

Use this guide to grant access, transfer responsibility for an existing workspace, and recover shared settings. Each collaborator signs in with their own Hugging Face account. Maintainers who run training or evaluations also need access to the shared notebook from their own Google account.

## Find the active repositories

Open **Settings → Account and Shared Data → Refresh Shared Settings**. Record the displayed **Hugging Face Organization**, **Private Dataset Repository**, **Model Hub Repository**, and **Colab Notebook** in the team's private handoff record. Use the displayed values when inviting accounts and verifying the notebook. The defaults are listed below.

- **Private dataset** (`tempo-bio/cardiomyocyte-data`) holds shared images, annotations, reservations, submissions, and regular training status.
- **Private model repository** (`tempo-bio-settings/tempo-models`) holds published weights, training results, and executable Model Evaluation code.
- **Private shared settings repository** (`tempo-bio-settings/workspace`) holds the repository IDs and notebook URL in `tempo_shared_settings.json`. Installed clients use this fixed repository to discover the workspace. Keep it accessible at that address during the handoff.

## Choose access by responsibility

- **Annotate and submit.** Grant read/write access to the dataset and read access to the models and shared settings.
- **Review and approve.** Also grant the admin role in the dataset organization. The reviewer needs a credential that can write to the dataset.
- **Train, evaluate, or publish models.** Grant reviewer access plus read/write access to the model repository.
- **Publish shared settings.** Grant the admin role in the settings organization and write access to the settings repository. The publisher also needs dataset read/write and model read access for the configured destinations.

Grant each person the combined permissions for their responsibilities. In the default workspace, a typical annotator has **write** in `tempo-bio` and **read** in `tempo-bio-settings`. Hugging Face's **contributor** role only grants write access to repositories that person created. [Hugging Face organization roles](https://huggingface.co/docs/hub/organizations-security)

Reserve write access to `tempo-bio-settings/workspace` for workspace administrators. Hugging Face organization-wide **write** also allows edits to shared settings outside TEMPO-C. If a training maintainer needs write access only to the model repository, scope it with a Hugging Face resource group if your plan supports it. Check effective access to both repositories before changing a role. [Hugging Face resource groups](https://huggingface.co/docs/hub/enterprise-resource-groups) · [Credential and execution security](CREDENTIAL_SECURITY.md#executable-content-and-downloads)

An administrator using a fine-grained token needs settings-repository read/write access and **Read access to organization settings** for `tempo-bio-settings`. TEMPO checks both the account's admin role and the credential's write access before publishing settings.

## Add a collaborator or maintainer

1. Ask for the person's Hugging Face username and invite that account to the organizations they need. Have them accept the invitations and connect their account in TEMPO-C. [Connect your workspace](GETTING_STARTED.md#connect-your-workspace)
2. Have them choose **Refresh Shared Settings**, then open **Segmentation Annotation → Shared Annotation Pool**. Verify that the pool shows the expected dataset. If they use a fine-grained token, check both the account's permissions and the token's selected resources.
3. For reviewers and trainers, confirm **Maintainer** appears after sign-in. That tab confirms maintainer access to the dataset. Check model write access separately for the account and credential used to train, evaluate, or publish.

## Browser sign-in

On the Hugging Face consent screen, select the organizations that own the active dataset, models, and shared settings. If you missed one, sign in again and include it. Choose **Use an access token** for a credential scoped to specific repositories. [Browser credentials and storage](CREDENTIAL_SECURITY.md#browser-sign-in)

If browser sign-in fails for several accounts, ask the OAuth registration owner to check its status and the client ID in `tempo_oauth.py`. If the registration must be replaced, register a public/native OAuth application with the loopback callback and scopes in the credential guide. Test the client ID in a source checkout with `TEMPO_HF_OAUTH_CLIENT_ID`. Verify sign-in with a collaborator and a settings administrator before packaging the new client ID. [Hugging Face OAuth registration](https://huggingface.co/docs/hub/oauth) · [Browser sign-in implementation](CREDENTIAL_SECURITY.md#browser-sign-in)

## Hand off workspace responsibilities

1. Add the incoming administrators to the existing Hugging Face organizations before changing the outgoing team's access. Assign owners for dataset review, shared settings, model publication, and the shared notebook.
2. Verify access with the incoming team's own accounts. A collaborator should see read-only shared fields and open the shared pool. A reviewer should be able to open **Maintainer → Review Submissions**. A settings administrator should be able to refresh the shared fields and edit them. Confirm the settings credential's write scope, too. The **Update Shared Settings** button becomes enabled when there are changes to publish.
3. If the notebook depends on the departing owner's Drive access, export a matching copy and upload it from an account the incoming team controls. Share it with the trainers and have the incoming settings admin publish its URL. Have a second account refresh settings and open the notebook from TEMPO-C. If the current copy is already under team control and matches the installed build, keep using it. [Replace the notebook](TRAINING_WORKFLOW.md#replace-the-notebook-when-needed)
4. Have the incoming trainer enable their own `HF_TOKEN` in Colab Secrets and run the notebook's **Settings** cell by itself. Confirm the intended GPU and repository IDs appear. A full regular run can replace current models even at one epoch. [Verify the notebook before starting](TRAINING_WORKFLOW.md#verify-the-notebook-before-starting)
5. Before removing departing users, confirm their annotation drafts have synced and their active Colab runs have finished or stopped. Hand over pending work and any local evaluation protocol sources the team needs. Reservations belong to the original accounts. For work being left behind, let a new collaborator adopt it after expiry to preserve the last synced draft. Update membership in both Hugging Face organizations and remove departing users from notebook sharing when their access ends. [Shared drafts and reservations](COLLABORATION_WORKFLOW.md#collaborators)

If the incoming team will maintain releases, also hand over control of GitHub Actions and Releases, the Hugging Face OAuth app registration, access to the approved signing environment, and the retained release sets and catalog checksums. Verify that the new release maintainer can recover a previous set. Keep personal tokens and private signing keys in their respective credential or signing stores. [Development and verification](DEVELOPMENT_AND_RELEASE_GUIDE.md) · [Verify a retained release set](WINDOWS_UPDATE_RELEASE.md#verify-retained-material-from-durable-storage)

## Change or recover shared settings

Before editing, choose **Refresh Shared Settings** to load the current values. Confirm the affected accounts can open the proposed destinations. The publisher needs write access to the destination dataset, which must be private and belong to the listed organization. Use an HTTPS Colab notebook URL with a `/drive/` or `/github/` path. Publish from **Settings → Account and Shared Data → Update Shared Settings**.

Collaborators receive the change at launch or through **Refresh Shared Settings**. Sync drafts before restarting to apply a repository change. Publishing new IDs changes where TEMPO-C connects. Plan any transfer of repository data separately, then export a fresh notebook from the active workspace or generate it with the intended repository IDs before starting a run.

If refresh fails, open **Show Details** and check the signed-in account's access to the private settings repository. TEMPO-C keeps the last saved settings. If another administrator published while you were editing, refresh and reapply your intended changes. If publication succeeded but local saving failed, correct the local folder permissions and refresh again.
