"""Encrypted wallet keystore.

Your private key controls real money. It is stored encrypted at rest with a
scrypt-derived key, decrypted only into process memory on the Brain, and it
never crosses the network — the Brain builds and signs transactions locally
and sends only the signed bytes to an RPC node.

Storing a Solana private key in a plaintext JSON file, which most snipe-bot
tutorials tell you to do, means anyone who gets read access to that Pi's SD
card gets your whole wallet. That is a real and common way people lose funds.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCRYPT_N = 2**15  # ~32 MB of memory; a Pi 5 handles this comfortably
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32


def _require_crypto() -> Any:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'cryptography' package is required for keystore operations.\n"
            "Install the trading extras:  pip install -e '.[trade]'"
        ) from exc
    return Fernet


def _derive(passphrase: str, salt: bytes) -> bytes:
    import hashlib

    raw = hashlib.scrypt(
        passphrase.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_LEN
    )
    return base64.urlsafe_b64encode(raw)


def create_keystore(
    private_key_b58: str, passphrase: str, path: str | Path
) -> dict[str, Any]:
    """Encrypt a base58 private key to disk. Returns the public key.

    Accepts the base58 secret key that Phantom, Solflare and friends export.
    """
    Fernet = _require_crypto()

    pubkey = derive_pubkey(private_key_b58)

    salt = os.urandom(16)
    token = Fernet(_derive(passphrase, salt)).encrypt(private_key_b58.encode())
    blob = {
        "version": 1,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode(),
        "ciphertext": token.decode(),
        "pubkey": pubkey,
    }

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob, indent=2))
    # Owner read/write only.
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    log.info("keystore written to %s for wallet %s", path, pubkey)
    return {"path": str(path), "pubkey": pubkey}


def load_keystore(path: str | Path, passphrase: str | None = None) -> str:
    """Decrypt and return the base58 private key.

    If no passphrase is supplied, prompts on the terminal. That is the safest
    mode: nothing on disk can unlock the wallet on its own.
    """
    Fernet = _require_crypto()

    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"No keystore at {path}. Create one with:  mctb keystore create"
        )

    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning(
            "keystore %s is readable by other users — fix with: chmod 600 %s", path, path
        )

    blob = json.loads(path.read_text())
    if passphrase is None:
        passphrase = getpass.getpass(f"Passphrase for {path.name}: ")

    salt = base64.b64decode(blob["salt"])
    try:
        plaintext = Fernet(_derive(passphrase, salt)).decrypt(blob["ciphertext"].encode())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            "Could not decrypt keystore — wrong passphrase, or the file is corrupt."
        ) from exc
    return plaintext.decode()


def derive_pubkey(private_key_b58: str) -> str:
    """Public key for a base58 secret key, so we can show which wallet is armed."""
    try:
        from solders.keypair import Keypair
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The 'solders' package is required.\nInstall with:  pip install -e '.[trade]'"
        ) from exc

    try:
        return str(Keypair.from_base58_string(private_key_b58).pubkey())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            "That does not look like a valid Solana private key. Export the "
            "'Private Key' from Phantom/Solflare — it is a long base58 string, "
            "not the seed phrase and not the public address."
        ) from exc


def keypair_from_keystore(path: str | Path, passphrase: str | None = None):
    """Load a solders Keypair ready for signing."""
    from solders.keypair import Keypair

    return Keypair.from_base58_string(load_keystore(path, passphrase))
