from pathlib import Path

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_all, collect_data_files


release = Path(SPECPATH)
tools = release.parent
data = [
    (str(release / "recipes"), "recipes"),
    (str(release / "vendor" / "vgmstream"), "vendor/vgmstream"),
    (str(release / "vendor" / "HevagEncoder.exe"), "vendor"),
    (str(release / "vendor" / "audio-tools.json"), "vendor"),
    (str(release / "vendor" / "xdelta3.exe"), "vendor"),
    (str(release / "vendor" / "xdelta3.sha256"), "vendor"),
    (str(release / "vendor" / "xdelta3-LICENSE"), "vendor"),
    (str(release / "vendor" / "xdelta3-SOURCE.txt"), "vendor"),
    (str(release / "patcher" / "audio.py"), "patcher"),
    (str(release / "patcher" / "fsb.py"), "patcher"),
    (str(release / "patcher" / "LocalVitaUserTypes.cs"), "patcher"),
    (str(tools / "Compress-VitaBundles.py"), "Tools"),
]
for filename in ("bundle_reader.py", "convert_failing_forward.py", "stage_text.py", "ConsoleMessages.json"):
    data.append((str(tools / "Localization" / filename), "Tools/Localization"))
data.extend(collect_data_files("archspec"))

binaries, imports = [], []
for package in ("UnityPy", "Cryptodome"):
    package_data, package_bins, package_imports = collect_all(package)
    data.extend(package_data)
    binaries.extend(package_bins)
    imports.extend(package_imports)

analysis = Analysis(
    [str(release / "build24_converter.py")],
    pathex=[str(release)],
    binaries=binaries,
    datas=data,
    hiddenimports=imports,
    excludes=["fmod_toolkit"],
)
# Keep FMOD optional in the frozen EXE (audio uses vgmstream + HevagEncoder).
audio_export = "UnityPy.export.AudioClipConverter"
source = Path(next(path for name, path, _ in analysis.pure if name == audio_export))
code = source.read_text(encoding="utf-8")
eager_import = "\nimport fmod_toolkit\n"
decode_call = "    return fmod_toolkit.raw_to_wav("
if code.count(eager_import) != 1 or code.count(decode_call) != 1:
    raise RuntimeError("Review the optional FMOD import for this version of UnityPy")
patched = Path(CONF["workpath"]) / "AudioClipConverter_optional.py"
patched.write_text(code.replace(eager_import, "\n").replace(
    decode_call, "    import fmod_toolkit\n\n" + decode_call), encoding="utf-8")
pure = [(name, str(patched) if name == audio_export else path, kind)
        for name, path, kind in analysis.pure]
archive = PYZ(pure)
exe = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="PWAATVitaConverter",
    console=True,
    strip=False,
    upx=False,
)
