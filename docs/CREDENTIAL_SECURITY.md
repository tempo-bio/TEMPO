# Accounts and credential storage

## Sign in to the workspace

Use your own Hugging Face account. TEMPO-C ties reservations and submissions to the verified account ID. Your profile or lab name is the label other collaborators see. A token carries the identity and permissions of the account that issued it. Keep personal tokens out of notebooks, screenshots, and logs. [Workspace access](WORKSPACE_ADMINISTRATION.md)

Both the account and its credential need permission. An annotator needs write access to the dataset and read access to the model and settings repositories. Dataset write access also permits direct edits through Hugging Face. The app's reservation and approval checks coordinate work within TEMPO-C. Hugging Face controls access to the separate model and settings repositories. [Repository access](WORKSPACE_ADMINISTRATION.md#find-the-active-repositories)

When someone leaves, remove that account's access from both workspace organizations. Copies already downloaded stay on their computer.

## Browser sign-in

Select your workspace organizations on the Hugging Face consent screen. Browser sign-in grants repository-content access to your personal repositories and the organizations you select, within your account's permissions. Choose **Use an access token** if you need to scope a credential to particular repositories. Sign in again if the consent expires, was revoked, or omitted a required organization. [Hugging Face tokens](https://huggingface.co/docs/hub/security-tokens)

The app opens a temporary callback on `127.0.0.1`. The public client uses authorization code with PKCE (`S256`) and a random state value. It accepts a callback with the expected host, path, and state for up to three minutes. Closing or cancelling the dialog ends the attempt. Codes and verifiers stay in memory. The HTTPS token request rejects redirects and verifies the server certificate and hostname.

For a developer maintaining the registered client, the callback is `http://127.0.0.1/oauth/callback` with a port selected at runtime. The scopes are `openid`, `profile`, `read-memberships`, and `write-repos`. The public client config in `tempo_oauth.py` holds the client ID. The app asks the user to sign in again when a token expires. [Workspace sign-in troubleshooting](WORKSPACE_ADMINISTRATION.md#browser-sign-in)

## Save or replace a credential

Choose **Save Account** in Account Setup or **Save and continue** in Guided Setup. TEMPO-C checks the account and repository access before saving, then verifies the saved token by reading it back. On a save error, check the credential-store status and retry. The app keeps replacement input while you resolve the failure and attempts to restore the previous credential.

To replace a saved token, clear the token field and enter the replacement. The mask represents a saved credential. Leaving it as-is reuses that credential. Closing the dialog discards pending changes.

## Find the saved credential

| Platform | Store |
| --- | --- |
| Windows | Windows Credential Manager for the current user |
| macOS | macOS Keychain |
| Linux desktop | Secret Service when installed and unlocked |

The desktop entry is `active-token` in the `TEMPO/HuggingFace` service. TEMPO-C reads it through the native backend. Unlock the store or restore access if saving fails. Desktop sign-in, Hugging Face CLI credentials, and Colab Secrets have separate lifecycles.

If the macOS Keychain asks repeatedly for permission in a source checkout, check from the same signed Python runtime the GUI uses. For a GUI running under Rosetta, use `arch -x86_64 .venv_x86/bin/python`. A different interpreter may need its own Keychain approval.

## Executable content and downloads

Colab loads Model Evaluation code, protocols, and checkpoints from the private model repository. Evaluation code runs with the Colab process's credentials, and some model formats can execute code when loaded. Give repository write access to the maintainers responsible for reviewing and publishing these files. Checksums detect changes to saved artifacts.

For an older evaluation stored only in the dataset repository, start a new evaluation from the reviewed local protocol with the current notebook. This preserves the old files and gives the new evaluation a plan in the model repository. [Model Evaluation workflow](MODEL_EVALUATION_WORKFLOW.md) · [Replace the notebook](TRAINING_WORKFLOW.md#replace-the-notebook-when-needed)

Model downloads use an allowlist of known filenames. Downloaded weights live under the personal configuration root in a repository-specific `models` directory. Same-origin HTTPS redirects can retain authorization. Redirects to another origin drop it. Redirects to HTTP or URLs with embedded credentials are refused.

## Personal preferences

TEMPO-C saves `tempo_preferences.json` in your platform's configuration folder.

- **Windows** uses `%LOCALAPPDATA%\TEMPO\config`.
- **macOS** uses `~/Library/Application Support/TEMPO/config`.
- **Linux** uses `$XDG_CONFIG_HOME/TEMPO/config` or, if that variable is unset, `~/.config/TEMPO/config`.

If preferences cannot be saved, check write access to that folder. On macOS and Linux the file is restricted to its owner.

## Repository checks

From the source checkout, run `python -m scripts.check_repository` to scan the working files and the notebook's embedded sources. Add `--staged` to scan the files being committed. For the full credential scan, use the project's Python environment to install and run the pinned scanner:

```sh
python -m scripts.check_repository --install-scanner .agent-artifacts/security-tools
python -m scripts.check_repository --scanner .agent-artifacts/security-tools/gitleaks
```

Use `gitleaks.exe` on Windows. The installer supports Windows x64, Linux x64, and macOS arm64. Review any reported file and rule before submitting a change. Exceptions are for individual verified synthetic values. The checker scans the repository and notebook contents. Review release artifacts and support attachments before sharing them separately.
