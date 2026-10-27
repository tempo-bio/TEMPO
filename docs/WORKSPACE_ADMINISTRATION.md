# Workspace administration

TEMPO uses private Hugging Face repositories for research data, models, and shared application settings. The settings repository is separate because annotators need to write annotations without gaining permission to change the workspace configuration.

## Settings access

The trusted settings location is `tempo-bio-settings/workspace`, a private model repository containing `tempo_shared_settings.json`. Its address is fixed in `tempo_shared_settings.py`; an editable dataset or model destination cannot redirect this lookup.

Only administrators belong to this settings organization with the **admin** role. Collaborators have the **read** role. The organization must have no non-administrator accounts with **write** or **contributor** roles. Hugging Face enforces repository access even when someone bypasses TEMPO and calls its API directly. See [Hugging Face organization access control](https://huggingface.co/docs/hub/organizations-security).

TEMPO checks the signed-in account's organization role before offering shared-setting edits. Publishing also requires a token with write access to the settings repository. Readers require an authenticated token with read access, and TEMPO rejects a public settings repository. Publisher identities are recorded in the private settings document.

This protection depends on the organization's access rules. The `updated_by` field is an audit record, not proof of authorization. Granting a collaborator write access to the settings organization would allow them to replace its files outside TEMPO.

## Initial setup

1. Create the settings organization and private **model** repository at the fixed address above. Do not put research images, weights, or tokens in it.
2. Assign organization admins and invite collaborators with **read** access. Manage data and model repository permissions separately.
3. Give the administrator's token read/write access to the settings repository and **Read access to organization settings** for **tempo-bio-settings**. Both are needed: repository access permits publishing, while organization access lets TEMPO verify the admin role. Give collaborators' tokens read access to the settings repository. Include this repository in fine-grained token scopes.
4. Using a TEMPO build configured for this settings repository, sign in with an administrator account. Check the four shared fields in **Settings → Account and Shared Data**, then choose **Update Shared Settings** to publish them.
5. In a collaborator account, choose **Refresh Shared Settings**. Confirm that the values arrive and that **Update Shared Settings** is unavailable. A collaborator's direct write to the settings repository must also be denied by Hugging Face.

## Browser sign-in application

Register one public OAuth application for TEMPO in [Hugging Face developer settings](https://huggingface.co/settings/applications/new). Each collaborator still needs their own Hugging Face account and the repository access described above. The application is shared by TEMPO installations; collaborators do not register their own apps. Public-client sign-in does not require Enterprise.

Use these registration settings:

| Setting | Value |
| --- | --- |
| Application name | TEMPO |
| Application type | Public/native application, without a client secret |
| Application URL | The project's public repository or homepage |
| Redirect URI | `http://127.0.0.1/oauth/callback` |
| OAuth scopes | `openid profile read-memberships write-repos` (`write-repos` includes reads) |
| Authorization flow | Authorization code with PKCE (`S256`) |

Put the application's public client ID in `PUBLIC_CLIENT_ID` in `tempo_oauth.py` before packaging. Source developers can use the `TEMPO_HF_OAUTH_CLIENT_ID` environment variable. The client ID is public and can be distributed in the application; never embed a client secret or an administrator token. The registered loopback URI omits the port because TEMPO selects an available local port for each attempt, as supported by [Hugging Face OAuth](https://huggingface.co/docs/hub/oauth#redirect-uris).

The model repository may have a different owner from the dataset. The existing `tempo-bio-settings` organization can host a separate private `tempo-models` repository alongside `workspace`: admins publish models and settings, while collaborators read both. Keep model weights out of the `workspace` repository itself. Set the shared model-repository value to the actual location; the data organization continues to own the annotation dataset.

The private model repository also holds executable Model Evaluation bundles, protocols, saved checkpoints, reports and approval records under `training/`. Reserve all writes to that repository for trusted maintainers, including Colab credentials used to run evaluations. Dataset write membership must not grant model-repository write access. Fine-grained annotator tokens need dataset writes plus model and workspace reads; evaluation-running credentials additionally need model writes. Dataset-only evaluations from older versions are not automatically imported into this trusted repository; see [credential and execution security](CREDENTIAL_SECURITY.md#executable-content-and-downloads).

Hugging Face's consent for `write-repos` includes personal repositories in addition to the selected organizations. TEMPO accesses its configured workspace, but this browser credential is broader than a repository-specific token. Users who need a narrower credential can use the fine-grained token fallback.

On the Hugging Face consent screen, collaborators select the workspace organizations. The scopes permit repository access within the user's granted accounts and organizations; they do not provide the repository-by-repository restriction of a manually created fine-grained token. On the free plan, organization write membership covers every repository in that organization, including published models if they share the dataset's owner. A manually restricted token cannot prevent that account from creating a broader token. Protect published models through a separate organization with read-only collaborator membership, or through the resource-group permissions available on Team and Enterprise plans. Keep the settings organization read-only for collaborators. `read-memberships` permits role verification; no email, billing, inference, or repository-management scope is requested.

For live account acceptance, test with an invited collaborator and an administrator. Verify dataset writes, model and private-settings reads, maintainer visibility, rejected access when an organization is not selected, cancellation, and a fresh sign-in after expiry. Independent-collaborator acceptance is deferred to v1.1 and is not a v1 publication blocker. Local tests simulate the authorization response and cannot establish that the registered application's consent and repository permissions work. Until the public client ID is configured, collaborators can use the personal-token method.

## Changes and succession

Administrators publish changes with **Update Shared Settings**. Collaborators receive them at launch or through **Refresh Shared Settings**. Repository changes received during a session are saved for the next launch so that current edits stay with their original dataset.

A successor needs admin membership in the settings organization, appropriate access to the data and model repositories, and a suitably scoped token. Training access is separate; see the [training workflow](TRAINING_WORKFLOW.md). Removing admin membership removes shared-setting edit authority without changing the application source.

Unavailable or unreadable settings leave saved values in use. TEMPO does not fall back to a writable data/model repository as a configuration source.
