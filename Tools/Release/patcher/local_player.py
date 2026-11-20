"""Rebuild the PSP2 code locally from Steam DLLs and a local Unity port project."""

import argparse
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from .core import PatchError, check_cancel, sha256
from .local import check_original


ASSEMBLIES = {
    'Assembly-CSharp-firstpass': 'Assets/Plugins/Assembly-CSharp-firstpass',
    'Assembly-CSharp': 'Assets/Scripts/Assembly-CSharp',
}


def run_tool(command, log, cancel, *, cwd=None, environment=None):
    check_cancel(cancel)
    with log.open('wb') as stream:
        with subprocess.Popen([str(arg) for arg in command], cwd=cwd, env=environment,
                              stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT) as process:
            try:
                while True:
                    check_cancel(cancel)
                    try:
                        code = process.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                process.terminate()
                raise
    if code:
        raise PatchError(f'Command exited with code {code}. Log: {log}')


def source_index(directory):
    indexed = {}
    for file in directory.rglob('*.cs'):
        name = file.relative_to(directory).as_posix()
        if not file.resolve().is_relative_to(directory.resolve()) or name.lower() in indexed:
            raise PatchError('Ambiguous script path: ' + name)
        indexed[name.lower()] = (name, file)
    return indexed


def apply_edits(before, target):
    """Compute and replay edits using only the two local versions of a script."""
    if before == target:
        return before, 0, 0
    old = before.splitlines(keepends=True)
    new = target.splitlines(keepends=True)
    edits = SequenceMatcher(None, old, new).get_opcodes()
    output = []
    inserted = removed = 0
    for op, a, b, x, y in edits:
        if op == 'equal':
            output.extend(old[a:b])
        else:
            output.extend(new[x:y])
            inserted += y - x
            removed += b - a
    result = b''.join(output)
    if result != target:
        raise PatchError('Could not replay the local C# changes.')
    return result, inserted, removed


def stage_scripts(extracted, port_sources, staged, cancel=None):
    original, desired = source_index(extracted), source_index(port_sources)
    if not original or not desired:
        raise PatchError('Decompiled scripts or local port sources were not found.')
    matched = original.keys() & desired.keys()
    if not matched:
        raise PatchError('The Steam scripts do not match the local port project.')
    result = {'extracted': len(original), 'files': len(desired), 'matched': len(matched),
              'edited': 0, 'added': len(desired.keys() - original.keys()),
              'removed': len(original.keys() - desired.keys()), 'inserted_lines': 0, 'removed_lines': 0,
              'source_hashes': {}}
    for key, (relative, file) in sorted(desired.items()):
        check_cancel(cancel)
        target = file.read_bytes()
        if key in original:
            generated, inserted, removed = apply_edits(original[key][1].read_bytes(), target)
            result['edited'] += int(inserted != 0 or removed != 0)
            result['inserted_lines'] += inserted
            result['removed_lines'] += removed
        else:
            generated = target
        output = staged / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(generated)
        result['source_hashes'][relative] = hashlib.sha256(generated).hexdigest()
    return result


def decompiler_command(path):
    if not path.is_file():
        raise PatchError('ILSpyCmd not found: ' + str(path))
    if path.suffix.lower() == '.dll':
        dotnet = shutil.which('dotnet')
        if not dotnet:
            raise PatchError('Install the .NET Runtime to run ilspycmd.dll.')
        return [dotnet, path]
    return [path]


def compile_references(profile, port, editor, managed):
    data = editor.parent / 'Data'
    references, flags = [], []
    for line in profile.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip().replace('\\', '/')
        if line.startswith('-define:'):
            flags.append(line)
        if not line.startswith('-r:'):
            continue
        name = line[3:].strip('"')
        if Path(name).name == 'Assembly-CSharp-firstpass.dll':
            continue
        if '/Editor/Data/' in name:
            file = data / name.split('/Editor/Data/', 1)[1]
        else:
            file = port / name
        if not file.is_file():
            raise PatchError('Missing PSP2 compiler reference: ' + str(file))
        references.append(f'-r:"{file}"')
    # Match the checked-in PSP2 compiler profile, using the runtime Mono
    # assemblies in the local Unity cache for the standard library.
    for library in ('mscorlib.dll', 'System.dll', 'System.Core.dll'):
        if not (managed / library).is_file():
            raise PatchError('Missing PSP2 runtime assembly: ' + library)
        references.append(f'-r:"{managed / library}"')
    if '-define:UNITY_PSP2' not in flags:
        raise PatchError('The compilation profile does not target PSP2.')
    return flags + references


def prepare_managed(port, output, unity, cancel):
    data = unity.parent / 'Data'
    backend = data / 'PlaybackEngines/PSP2Player'
    mono = data / 'MonoBleedingEdge/bin/mono.exe'
    compiler = data / 'MonoBleedingEdge/lib/mono/4.5/mcs.exe'
    runtime = port / 'Library/PlayerDataCache/Data/Managed'
    profile = port / 'Tools/HardwareRepairPlayerValidation.rsp'
    for prerequisite in (unity, mono, compiler, profile, backend / 'UnityEditor.PSP2.Extensions.dll',
                         runtime / 'UnityEngine.CoreModule.dll'):
        if not prerequisite.is_file():
            raise PatchError('Missing local build component: ' + str(prerequisite))
    managed = output / 'Managed'
    managed.mkdir()
    for file in runtime.glob('*.dll'):
        if file.stem not in ASSEMBLIES:
            shutil.copyfile(file, managed / file.name)
    common = ['-noconfig', '-nostdlib', '-target:library', '-unsafe', '-langversion:6']
    common.extend(compile_references(profile, port, unity, managed))
    firstpass = managed / 'Assembly-CSharp-firstpass.dll'
    for assembly in ASSEMBLIES:
        target = managed / (assembly + '.dll')
        options = [*common, f'-out:"{target}"']
        if target != firstpass:
            options.append(f'-r:"{firstpass}"')
        options.extend(f'"{path}"' for _, path in source_index(output / 'Sources' / assembly).values())
        response = output / (assembly + '.rsp')
        response.write_text('\n'.join(options) + '\n', encoding='utf-8')
        run_tool([mono, compiler, '@' + str(response)], output / (assembly + '.compile.log'), cancel)
        if not target.is_file():
            raise PatchError('The C# compiler did not produce ' + target.name)
    return managed


def build_native(port, output, unity, sdk, managed, cancel):
    if sdk is None or not (sdk / 'host_tools/build/bin/psp2snc.exe').is_file():
        raise PatchError('Specify the installed PSP2 SDK using --sdk.')
    data = unity.parent / 'Data'
    tools = data / 'PlaybackEngines/PSP2Player/Tools'
    mono = data / 'MonoBleedingEdge/bin/mono.exe'
    compiler = data / 'MonoBleedingEdge/lib/mono/4.5/mcs.exe'
    helper = Path(__file__).with_name('LocalVitaUserTypes.cs')
    types_tool = output / 'LocalVitaUserTypes.exe'
    cecil = tools / 'Mono.Cecil.dll'
    run_tool([mono, compiler, '-out:' + str(types_tool), '-r:' + str(cecil), helper],
             output / 'user-types.compile.log', cancel)
    shutil.copyfile(cecil, output / cecil.name)

    project = output / 'UnityProject'
    scripts = project / 'Assets/Editor'
    scripts.mkdir(parents=True)
    for directory in ('Assets/Scripts', 'Assets/Plugins', 'ProjectSettings'):
        (project / directory).mkdir(parents=True)
    for name in ('ProjectVersion.txt', 'ProjectSettings.asset'):
        shutil.copyfile(port / 'ProjectSettings' / name, project / 'ProjectSettings' / name)
    builder = port / 'Assets/Editor/VitaNativeCodeOnlyBuild.cs'
    if 'public static void BuildPrepared()' not in builder.read_text(encoding='utf-8-sig'):
        raise PatchError('The local project needs the updated VitaNativeCodeOnlyBuild.cs.')
    shutil.copyfile(builder, scripts / builder.name)
    cache = project / 'Library/VitaCodeBuild'
    cache.mkdir(parents=True)
    shutil.copytree(managed, cache / 'Managed')
    run_tool([mono, types_tool, cache / 'UserTypes.tsv', *[managed / (a + '.dll') for a in ASSEMBLIES]],
             output / 'user-types.log', cancel)
    env = dict(os.environ, SCE_ROOT_DIR=str(sdk.parent), SCE_PSP2_SDK_DIR=str(sdk))
    run_tool([unity, '-batchmode', '-nographics', '-quit', '-projectPath', project,
              '-buildTarget', 'PSP2', '-executeMethod', 'VitaNativeCodeOnlyBuild.BuildPrepared',
              '-logFile', output / 'unity-psp2.log'], output / 'unity-launch.log', cancel, cwd=project,
             environment=env)
    stamp = cache / 'Result.txt'
    if not stamp.is_file() or not stamp.read_text(encoding='utf-8-sig').startswith('SUCCESS '):
        raise PatchError('IL2CPP did not confirm the build. Log: ' + str(output / 'unity-psp2.log'))
    artifacts = {}
    for relative in ('Modules/il2CppAssemblies.suprx', 'Metadata/global-metadata.dat'):
        source = cache / 'Output/Media' / relative
        target = output / 'Native/Media' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        artifacts[relative] = sha256(target)
    return artifacts


def main(argv, cancel):
    parser = argparse.ArgumentParser(prog='PWAATVitaConverter local-player',
                                     description='Build PSP2 code locally from Steam and your Unity port project.')
    for name in ('steam', 'port-project', 'decompiler', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--unity', type=Path, help='Unity 2017.4.2f2 executable with PSP2 support')
    parser.add_argument('--sdk', type=Path, help='Installed PSP2 SDK')
    parser.add_argument('--stage', choices=('sources', 'managed', 'native'), default='native')
    args = parser.parse_args(argv)
    game = check_original(args.steam)
    port, output = args.port_project.resolve(strict=True), args.output.resolve()
    if game == output or game in output.parents or output in game.parents or output == port or output in port.parents:
        raise PatchError('Choose a separate output folder for the build.')
    for folder in ('Assets', 'Library', 'ProjectSettings'):
        if output.is_relative_to(port / folder):
            raise PatchError('Choose an output folder outside the files imported by Unity.')
    if args.stage != 'sources' and args.unity is None:
        parser.error('--unity is required for the managed and native stages')
    if args.stage == 'native' and args.sdk is None:
        parser.error('--sdk is required for the native stage')
    for directory in ASSEMBLIES.values():
        if not (port / directory).is_dir():
            raise PatchError('Scripts missing from the local port project: ' + directory)
    command = decompiler_command(args.decompiler.resolve())
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise PatchError('Choose an empty folder for the local build.')
    report = {'complete_media': False, 'ripped_from_steam': {}, 'native_artifacts': {}}
    for assembly, directory in ASSEMBLIES.items():
        original = game / 'Managed' / (assembly + '.dll')
        if not original.is_file():
            raise PatchError('Missing Steam assembly: ' + str(original))
        print('Decompiling and applying local changes:', assembly, flush=True)
        extracted = output / 'Extracted' / assembly
        extracted.mkdir(parents=True)
        run_tool([*command, '--disable-updatecheck', '-p', '--nested-directories', '-lv', 'CSharp6', '-r', original.parent,
                  '-o', extracted, original], output / (assembly + '.ilspy.log'), cancel)
        info = stage_scripts(extracted, (port / directory).resolve(), output / 'Sources' / assembly, cancel)
        info['input_dll_sha256'] = sha256(original)
        report['ripped_from_steam'][assembly] = info
        print(f"  {info['matched']} matched names, {info['edited']} edited, {info['added']} added,",
              f"{info['removed']} removed", flush=True)
    marker = output / 'local-player-status.json'
    marker.write_text(json.dumps(report, indent=2), encoding='utf-8')
    if args.stage != 'sources':
        unity = args.unity.resolve()
        print('Compiling C# for PSP2', flush=True)
        managed = prepare_managed(port, output, unity, cancel)
        report['managed_ready'] = True
        marker.write_text(json.dumps(report, indent=2), encoding='utf-8')
    if args.stage == 'native':
        print('Building IL2CPP for PSP2 locally', flush=True)
        report['native_artifacts'] = build_native(port, output, unity, args.sdk.resolve(), managed, cancel)
        marker.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Report:', marker, flush=True)
    print('Player code prepared. Complete Media still requires scenes and assets.', flush=True)
    return 0
