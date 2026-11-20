from pathlib import Path
import hashlib
import sys
from .core import PatchError, sha256

TOOL_ZIP_URL = 'https://github.com/jmacd/xdelta/releases/download/v3.2.2/xdelta3-3.2.2-windows-x86_64.zip'
TOOL_ZIP_SHA256 = 'd48f7651d86b78fdca397057b46601f89131f37219675df736db70af44ec3379'


def tool_path():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    binary = root / 'vendor/xdelta3.exe'
    record = root / 'vendor/xdelta3.sha256'
    if not binary.is_file() or not record.is_file():
        raise PatchError('Не найден компонент xdelta3. Используйте полный комплект патчера.')
    expected = record.read_text(encoding='ascii').strip().split()[0]
    if sha256(binary) != expected:
        raise PatchError('Компонент xdelta3 повреждён.')
    return binary
