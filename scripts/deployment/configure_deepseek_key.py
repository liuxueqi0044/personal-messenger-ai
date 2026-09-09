"""Store a DeepSeek key with the existing user-scoped DPAPI SecretStore."""
from __future__ import annotations
import argparse
import getpass
from pathlib import Path
from messenger_ai.observability.secrets import WindowsDPAPISecretStore

def configure(store, alias: str, prompt=getpass.getpass) -> None:
    value = prompt("DeepSeek API key (input hidden): ")
    if not value:
        raise ValueError("key must not be empty")
    store.set_secret(alias, value.encode("utf-8"))

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True)
    parser.add_argument("--alias", default="deepseek.api_key")
    args = parser.parse_args()
    configure(WindowsDPAPISecretStore(Path(args.vault)), args.alias)
    print(f"Stored DPAPI secret under alias: {args.alias}")
if __name__ == "__main__": main()
