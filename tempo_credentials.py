"""Keep access tokens out of profile labels and displayed diagnostics."""
import re

_HUB_TOKEN = re.compile(r'hf_(?:oauth_)?[A-Za-z0-9]{10,}')


def contains_access_token(value, *secrets):
    if not isinstance(value, str):
        return False
    return bool(_HUB_TOKEN.search(value)) or any(
        isinstance(secret, str) and secret and
        (value == secret or len(secret) >= 8 and secret in value)
        for secret in secrets)


def safe_profile_name(value, *secrets):
    if not isinstance(value, str) or value.strip().startswith('hf:') or contains_access_token(value, *secrets):
        return ''
    return value


def redact_credentials(value, *secrets):
    text = str(value)
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, '[redacted]')
    return _HUB_TOKEN.sub('[redacted]', text)
