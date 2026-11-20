"""Apply the port's console wording to the user's stock Steam menus."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile

from .core import PatchError, check_cancel, safe_relative, sha256


def load_tools():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[3])) / 'Tools/Localization'
    translations = json.loads((root / 'ConsoleMessages.json').read_text(encoding='utf-8'))
    sys.path.insert(0, str(root))
    try:
        import stage_text
    finally:
        sys.path.remove(str(root))
    return stage_text, translations


def convert(name, raw, tools):
    codec, translations = tools
    family, _, suffix = Path(name).stem.partition('_text')
    messages = translations[suffix.removeprefix('_')]
    if family == 'option':
        replacements = dict(zip((136, 140, 66, 62, 159, 160, 161), messages[:7]))
    elif family == 'save':
        replacements = dict.fromkeys((11, 12, 13, 14), ','.join(messages[7:9]))
    else:
        raise ValueError('Unsupported stock menu table')
    rows = codec.parse(codec.reader.c.decrypt(raw))
    ids = [key for key, _ in rows]
    if any(ids.count(key) != 1 for key in replacements):
        raise ValueError('Missing or duplicate menu record')
    return codec.reader.c.encrypt(codec.encode([(key, replacements.get(key, text)) for key, text in rows]))


def prepare(streaming, output, recipe, progress, cancel):
    profile = json.loads(recipe.read_text(encoding='utf-8'))
    if profile['format'] != 'pwaat-stock-menu/1':
        raise PatchError('Unsupported menu text recipe.')
    tools = load_tools()
    entries = profile['files']
    for index, item in enumerate(entries, 1):
        check_cancel(cancel)
        relative = safe_relative(item['path'])
        source, target = streaming / relative, output / 'Media/StreamingAssets' / relative
        if not source.resolve().is_relative_to(streaming) or not target.resolve().is_relative_to(output):
            raise PatchError('Menu table path points outside the selected folder.')
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise PatchError('Menu table does not match the supported Steam version: ' + relative)
        if target.exists():
            if sha256(target, cancel) != item['output_sha256']:
                raise PatchError('Unexpected output menu table: ' + relative)
        else:
            encoded = convert(relative, raw, tools)
            if hashlib.sha256(encoded).hexdigest() != item['output_sha256']:
                raise PatchError('Output does not match the verified menu table: ' + relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                path = Path(stream.name)
                try:
                    stream.write(encoded)
                    stream.close()
                    path.rename(target)
                finally:
                    stream.close()
                    path.unlink(missing_ok=True)
        if progress:
            progress('PSP2 menu text', index, len(entries), relative)
    return {row['path'].lower() for row in entries}
