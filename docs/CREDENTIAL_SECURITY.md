# TEMPO credential storage

## Account access and token identity

Each collaborator uses their own Hugging Face account, authorizing TEMPO through browser sign-in or supplying their own fine-grained token. The administrator invites that account and grants repository access; the credential authenticates the account and can restrict that access further. Effective access requires both account permission and credential permission. Annotation collaborators need dataset read/write, model read, and read access to the separate `tempo-bio-settings/workspace` repository. See [Hugging Face tokens](https://huggingface.co/docs/hub/security-tokens) and [workspace administration](WORKSPACE_ADMINISTRATION.md).

TEMPO binds reservations and submissions to the account ID verified by Hugging Face, independently of the profile/laboratory display name. Creating one administrator-owned token per collaborator would still authenticate every collaborator as that administrator. A token is a bearer credential: anyone who obtains it can use its allowed access until it is invalidated or that account's access is removed. Removing workspace access cannot recall data already downloaded.

Dataset write permission also permits edits through Hugging Face's API outside TEMPO. Reservation ownership and maintainer approval checks in the desktop application are workflow protections for trusted collaborators, not server-enforced restrictions on individual dataset files. The separate settings repository must remain read-only for collaborators; Hugging Face then enforces that restriction independently of the application. Enforcing annotation-only writes against untrusted clients would require a server that validates each operation while retaining the repository write credential itself.

On Hugging Face's free plan, organization write membership permits writes to every repository in that organization. When data and published models share that owner, dataset writers can also change model files outside TEMPO. Fine-grained personal tokens can narrow an individual credential, but do not remove the account's ability to create a broader credential. Protecting models from those accounts requires separate organization permissions or the resource groups available on Team and Enterprise plans.

Ordinary organization membership management does not issue another user's personal access tokens. Hugging Face offers administrator-controlled issuance on behalf of organization members through [Enterprise OAuth Token Exchange](https://huggingface.co/docs/hub/oauth#token-exchange-for-organizations-rfc-8693). TEMPO has no Enterprise token-exchange integration or backend for it. Issuing a token from the administrator's own account is not equivalent to issuing one for the member.

## Browser sign-in

TEMPO's interactive OAuth flow uses a public client, a random state value, and PKCE with `S256`. It opens the system browser to Hugging Face and accepts the authorization response only on a temporary `127.0.0.1` listener with the expected callback path, host, and matching state. Each attempt expires after three minutes. Cancelling, navigating away, closing the dialog, or changing the active account/workspace prevents a late result from being applied. The listener closes when the attempt finishes; callback URLs are not logged.

Authorization codes and PKCE verifiers remain in memory. TEMPO exchanges the code only at Hugging Face's HTTPS token endpoint, rejects redirects, and verifies the returned account, dataset write permission, model read permission, and private settings read access before offering to save. Browser authorization alone does not overwrite the saved account: choose **Save Account** in Account Setup or **Save and continue** in the setup guide. Closing the dialog discards the pending credential. Tokens are saved through the same native credential store as personal tokens and remain absent from the token-entry field.

TLS keeps hostname and certificate verification enabled and adds the packaged `certifi` public CA bundle to the runtime's system/custom trust. This supports macOS framework Python installations whose OpenSSL default certificate store is empty.

No client secret is distributed with the desktop app. OAuth requests `openid`, `profile`, `read-memberships`, and `write-repos` (which includes repository reads); organization access is selected on Hugging Face's consent screen. The consent grants repository-content write access to the user's personal repositories as well as the selected organizations, constrained by that account's permissions. TEMPO uses its configured workspace repositories, but the credential itself is broader than a repository-specific fine-grained token. The flow does not request email or billing access and does not store refresh tokens. If the access token expires or is revoked, the user signs in again. Application registration and the live release verification procedure are in [workspace administration](WORKSPACE_ADMINISTRATION.md#browser-sign-in-application).

## Credential storage

Desktop TEMPO stores its Hugging Face access token in the operating system's credential store, using a pinned `keyring` dependency:

| Platform | Store |
| --- | --- |
| Windows | Windows Credential Manager, persisted for the current user on this machine |
| macOS | macOS Keychain |
| Linux desktop | Secret Service, when available and unlocked |

The service is `TEMPO/HuggingFace`; the entry is `active-token`. TEMPO selects the native backend explicitly. Environment-selected keyring plugins, plaintext backends, Hugging Face environment variables and the SDK's implicit credential-cache lookup cannot replace this desktop storage path. Missing, locked or inaccessible secure storage produces an error; TEMPO does not fall back to a plaintext credential file.

Windows dependencies and the hash-pinned lockfile include the credential-store package and its dependencies. The frozen application includes the native platform backend. Windows release verification requires a synthetic credential write/read/delete operation; a failed credential-store probe prevents certification.

## Saving credentials

Account Setup checks the proposed token's account and repository access before saving it. A secure save must read the same token back successfully. Failed writes attempt to restore the previous credential and report restoration failures. The form retains unsaved input on failure. The saved token never populates the form; replacement input is password-masked and cleared when the dialog closes. Active and replacement tokens are redacted from account diagnostics and rejected in profile-name fields.

Hugging Face API requests receive explicit credentials; a missing credential cannot silently authenticate using a different environment/cache account. Desktop TEMPO does not write tokens to `HF_TOKEN` or use the SDK's `login` function. The Hugging Face CLI has separate sign-in behavior. Colab uses its own Secrets facility and an in-memory token in its isolated runtime.

Native credential storage protects saved secrets at rest; it does not prevent access by every program running under an authorized, unlocked OS user account. Research images and annotations are stored separately.

For macOS source installations, perform live credential checks in the GUI's signed Python runtime, using `arch -x86_64 .venv_x86/bin/python` when the GUI runs under Rosetta. A different bootstrap interpreter can trigger separate Keychain approvals or signature-related failures. Reuse the GUI runtime rather than repeatedly retrying access or weakening the credential store's access rules.

## Executable content and downloads

Model Evaluation stores its queue, frozen Python source, protocols, dependencies, checkpoints, reports and approval records in the private model repository. Only trusted maintainers may write there; annotation collaborators need read access. The dataset remains writable for annotations and image-role reservations. Colab never uses the dataset as a fallback source for executable evaluation content or publication approvals. Hashes detect content changes within this boundary; the repository's server-enforced permissions establish who may authorize that content.

Dataset-only evaluations from older releases are not automatically trusted or migrated. Recreate them from reviewed local protocol sources with the updated application and regenerated notebook. Existing cloud files are retained. Replace previously uploaded notebook copies: older notebooks retain their old download behavior.

Protocols and model publishers remain trusted code suppliers. Python protocols and some model formats can execute code with the running process's permissions; repository isolation does not sandbox them. Restrict model-repository write access accordingly.

Model installation accepts only the explicitly registered current and historical model filenames. New downloads go into a repository-specific `models` directory beside the personal `config` directory, outside application code. Existing local or bundled weights remain usable until replaced by an installation. A remote manifest cannot add filenames to this allowlist.

Authenticated bootstrap and update downloads preserve authorization only on redirects within the same HTTPS origin. Redirects to another origin strip credentials, and redirects to HTTP or URLs containing embedded credentials are rejected. OAuth token exchange continues to reject all redirects.

## Personal preferences

Source and packaged installations both keep personal preferences outside the application and repository:

| Platform | Preferences file |
| --- | --- |
| Windows | `%LOCALAPPDATA%\TEMPO\config\tempo_preferences.json` |
| macOS | `~/Library/Application Support/TEMPO/config/tempo_preferences.json` |
| Linux | `$XDG_CONFIG_HOME/TEMPO/config/tempo_preferences.json`, or `~/.config/TEMPO/config/tempo_preferences.json` |

An unwritable preferences folder produces a settings error. TEMPO does not save preferences into source or installation directories. macOS/Linux files use owner-only permissions.

The account form shows verification status separately from the saved-token mask. The mask reveals neither the token nor its length. Delete, Backspace, or Clear enables masked replacement entry; leaving the mask unchanged reuses the stored credential. Clearing it without entering a replacement blocks saving. Cancellation retains the stored token, and failed verification or secure saving retains replacement input for retry. Closed forms and changed accounts reject stale verification results.

## Repository checks

From the repository root, use `python -m scripts.check_repository` to inspect maintained files and the notebook's embedded sources. Add `--staged` to inspect the Git index instead of working files. For the full credential check, install the checksum-pinned development scanner into an ignored directory, then supply its path:

```sh
python -m scripts.check_repository --install-scanner .agent-artifacts/security-tools
python -m scripts.check_repository --scanner .agent-artifacts/security-tools/gitleaks
```

On Windows the binary is `gitleaks.exe`. The installer supports Windows x64, Linux x64, and macOS arm64. Use the project's Python environment; this does not install system packages.

Checks reject private local configuration, generated data, saved notebook outputs, credential-shaped values, personal paths, and private notebook links. Exceptions identify a reviewed synthetic value by its exact file, rule, and SHA-256 digest, with a reason. They do not exclude whole test directories. Reports contain locations and rule names rather than secret values. Passing a scan does not replace manual privacy review or inspect private GitHub comments and artifacts.

The repository-check workflow runs on pushes and pull requests with read-only permissions. Windows builds also check the intended commit and application-owned packaged files. Neither workflow publishes releases.
