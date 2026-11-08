"""R-2.2: early deletion is a terminal-only `hermes jp-reinfolib delete` command."""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from reinfolib import adapter, cli_commands

ROOT = Path(__file__).resolve().parents[1]
ID = 'a' * 32
V2_AND_LEGACY = ['reinfolib_transactions', 'reinfolib_records', 'reinfolib_export',
                 'reinfolib_catalog', 'reinfolib_municipalities', 'reinfolib_stations', 'reinfolib_snapshots', 'reinfolib_history', 'reinfolib_diff', 'reinfolib_layer', 'reinfolib_land_values', 'reinfolib_geocode', 'reinfolib_point_layers', 'reinfolib_quarter']


def seed(root, kind='snapshot', identifier=ID):
    folder = root / ('snapshots' if kind == 'snapshot' else 'exports')
    folder.mkdir(parents=True, exist_ok=True)
    names = ['.json'] if kind == 'snapshot' else ['.csv', '.metadata.json']
    paths = [folder / (identifier + s) for s in names]
    for path in paths:
        path.write_text('{}')
    return paths


def run(argv, *, root, confirm=None):
    parser = argparse.ArgumentParser()
    cli_commands.setup(parser)
    args = parser.parse_args(argv)
    return cli_commands.handle(args, data_root=lambda: root, ask=confirm, interactive=confirm is not None)


def test_delete_with_yes_removes_snapshot(tmp_path, capsys):
    paths = seed(tmp_path)
    assert run(['delete', 'snapshot', ID, '--yes'], root=tmp_path) == 0
    assert not any(p.exists() for p in paths)
    assert json.loads(capsys.readouterr().out) == {'ok': True, 'kind': 'snapshot', 'artifact_id': ID,
                                                   'deleted_files': 1, 'cascade': False}


def test_delete_export_removes_pair(tmp_path):
    paths = seed(tmp_path, 'export')
    assert run(['delete', 'export', ID, '--yes'], root=tmp_path) == 0
    assert not any(p.exists() for p in paths)


@pytest.mark.parametrize('argv,code,message', [
    (['delete', 'snapshot', ID, '--yes'], 1, 'error: not_found/artifact_not_found'),
    (['delete', 'snapshot', 'A' * 32, '--yes'], 2, 'error: invalid_argument/bad_artifact_id'),
    (['delete', 'snapshot', '../' + ID, '--yes'], 2, 'error: invalid_argument/bad_artifact_id'),
    (['delete', 'tool', ID, '--yes'], 2, 'error: invalid_argument/bad_artifact_kind'),
    (['delete', 'snapshot', ID], 2, 'error: invalid_argument/confirmation_required'),
])
def test_errors_are_fixed_and_nonzero(tmp_path, capsys, argv, code, message):
    assert run(argv, root=tmp_path) == code
    captured = capsys.readouterr()
    assert captured.err.strip() == message and captured.out == ''


def test_symlink_store_is_refused(tmp_path, capsys):
    real = tmp_path / 'real'
    victim = seed(real)[0]
    (tmp_path / 'store').mkdir()
    (tmp_path / 'store' / 'snapshots').symlink_to(real / 'snapshots', target_is_directory=True)
    assert run(['delete', 'snapshot', ID, '--yes'], root=tmp_path / 'store') == 1
    assert victim.exists()
    assert capsys.readouterr().err.strip() == 'error: storage/unsafe_delete_target'


@pytest.mark.parametrize('answer,code', [('y', 0), ('yes', 0), ('n', 2), ('', 2)])
def test_interactive_confirmation(tmp_path, answer, code):
    path = seed(tmp_path)[0]
    assert run(['delete', 'snapshot', ID], root=tmp_path, confirm=lambda prompt: answer) == code
    assert path.exists() is (code != 0)


def entry():
    spec = importlib.util.spec_from_file_location('cli_review_entry', ROOT / '__init__.py')
    assert spec and spec.loader
    spec.submodule_search_locations = None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Context:
    def __init__(self):
        self.tools, self.commands, self.cli = [], [], []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs['name'])

    def register_command(self, name, **kwargs):
        self.commands.append(name)

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=''):
        self.cli.append((name, setup_fn, handler_fn))

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_registration_is_cli_only():
    context = Context()
    entry().register(context)
    assert context.tools == V2_AND_LEGACY
    assert not [t for t in context.tools if 'delete' in t]
    assert context.commands == ['jp-reinfolib']
    assert [name for name, *_ in context.cli] == ['jp-reinfolib']
    assert context.cli[0][2] is not None


def test_context_without_cli_api_still_registers_everything_else():
    class Old(Context):
        register_cli_command = None
    context = Old()
    entry().register(context)
    assert context.tools == V2_AND_LEGACY


def test_skill_and_tool_descriptions_do_not_point_to_the_command():
    texts = [d[0] for d in adapter.DEFINITIONS.values()]
    texts += [p.read_text(encoding='utf-8') for p in (ROOT / 'skills').rglob('SKILL.md')]
    assert not [t for t in texts if 'hermes jp-reinfolib' in t or 'delete_artifact' in t]


def _copy(dest):
    dest.mkdir(parents=True)
    for path in ROOT.iterdir():
        if path.name.startswith('.') or path.name in {'tests', '__pycache__', 'docs'}:
            continue
        if path.is_file():
            shutil.copy2(path, dest / path.name)
        elif path.name in {'skills', 'reinfolib'}:
            shutil.copytree(path, dest / path.name)


@pytest.mark.skipif(not (shutil.which('hermes') and os.environ.get('HERMES_AGENT_SRC')),
                    reason='Set HERMES_AGENT_SRC with hermes on PATH to run the real CLI subprocess.')
def test_real_hermes_subprocess(tmp_path):
    main = pytest.importorskip('hermes_cli.main')
    assert 'jp-reinfolib' not in main._BUILTIN_SUBCOMMANDS
    home = tmp_path / 'hermes'
    _copy(home / 'plugins' / 'jp-reinfolib')
    (home / 'config.yaml').write_text('plugins:\n  enabled:\n    - jp-reinfolib\n', encoding='utf-8')
    data = home / 'plugin-data' / 'jp-reinfolib' / 'v2'
    env = {k: v for k, v in os.environ.items() if not k.startswith('PYTEST')}
    env.update(HERMES_HOME=str(home), NO_COLOR='1')
    env.pop('PYTHONPATH', None)

    def hermes(*argv, stdin=''):
        return subprocess.run(['hermes', 'jp-reinfolib', *argv], env=env, input=stdin,
                              capture_output=True, text=True, timeout=120)

    snapshot = seed(data)[0]
    export = seed(data, 'export', 'b' * 32)
    refused = hermes('delete', 'snapshot', ID)
    assert refused.returncode == 2 and 'error: invalid_argument/confirmation_required' in refused.stderr
    assert snapshot.exists()
    done = hermes('delete', 'snapshot', ID, '--yes')
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1])['deleted_files'] == 1 and not snapshot.exists()
    pair = hermes('delete', 'export', 'b' * 32, '--yes')
    assert pair.returncode == 0 and not any(p.exists() for p in export)
    missing = hermes('delete', 'snapshot', ID, '--yes')
    assert missing.returncode == 1 and 'error: not_found/artifact_not_found' in missing.stderr
    bad = hermes('delete', 'snapshot', 'Z' * 32, '--yes')
    assert bad.returncode == 2 and 'error: invalid_argument/bad_artifact_id' in bad.stderr
    real = tmp_path / 'outside'
    victim = seed(real)[0]
    shutil.rmtree(data / 'snapshots')
    (data / 'snapshots').symlink_to(real / 'snapshots', target_is_directory=True)
    unsafe = hermes('delete', 'snapshot', ID, '--yes')
    assert unsafe.returncode == 1 and 'error: storage/unsafe_delete_target' in unsafe.stderr
    assert victim.exists()
