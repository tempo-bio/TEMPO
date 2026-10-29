"""Offline HTTP fixtures using the transport actually selected by the Hub SDK."""
import importlib
from huggingface_hub.utils._http import default_client_factory


client = default_client_factory()
try:
    httpx = importlib.import_module(type(client).__module__.split('.')[0])
finally:
    client.close()
