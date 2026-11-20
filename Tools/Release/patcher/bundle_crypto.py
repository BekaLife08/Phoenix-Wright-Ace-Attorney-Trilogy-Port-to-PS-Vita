"""Read bundle settings from the user's environment or the private workspace."""

from functools import lru_cache
import hashlib
import os
from pathlib import Path
import runpy
import sys

from .core import PatchError


@lru_cache(maxsize=1)
def game_cipher_parameters():
    password = os.environ.get('PWAAT_BUNDLE_PASSWORD')
    salt = os.environ.get('PWAAT_BUNDLE_SALT')
    if password and salt:
        material = hashlib.pbkdf2_hmac('sha1', password.encode(), salt.encode(), 1000, 32)
        return material[:16], material[16:]
    if password or salt:
        raise PatchError('Set both PWAAT_BUNDLE_PASSWORD and PWAAT_BUNDLE_SALT.')
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[3]))
    private_tool = root / 'Tools/Compress-VitaBundles.py'
    if private_tool.is_file():
        return runpy.run_path(str(private_tool))['derive_key']()
    raise PatchError('Encrypted input requires locally supplied PWAAT_BUNDLE_PASSWORD and PWAAT_BUNDLE_SALT.')


def decrypt(data):
    from Cryptodome.Cipher import AES

    key, iv = game_cipher_parameters()
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        return plain[:-pad]
    return plain


def encrypt(data):
    from Cryptodome.Cipher import AES

    key, iv = game_cipher_parameters()
    pad = 16 - len(data) % 16
    return AES.new(key, AES.MODE_CBC, iv).encrypt(data + bytes([pad]) * pad)
