"""R-1: v2 tool registration is all-or-nothing; legacy stays complete."""
import importlib.util
import json
import logging
import shutil
from pathlib import Path

import pytest
from reinfolib import adapter

ROOT = Path(__file__).resolve().parents[1]
V2 = {'reinfolib_transactions', 'reinfolib_records', 'reinfolib_export', 'reinfolib_catalog',
      'reinfolib_municipalities', 'reinfolib_stations', 'reinfolib_snapshots', 'reinfolib_history', 'reinfolib_diff', 'reinfolib_layer', 'reinfolib_land_values', 'reinfolib_geocode', 'reinfolib_point_layers'}


def entry():
    spec = importlib.util.spec_from_file_location('r1_review_entry', ROOT / '__init__.py')
    assert spec is not None and spec.loader is not None
    spec.submodule_search_locations = None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Handle:
    def __init__(self, tools, name):
        self.tools, self.name = tools, name

    def dispose(self):
        self.tools.remove(self.name)


class Context:
    """Mirrors Hermes: register_tool returns a disposable PluginRegistration."""

    def __init__(self, fail_on):
        self.fail_on, self.tools, self.commands, self.skills = fail_on, [], [], []

    def register_tool(self, **kwargs):
        if kwargs['name'] == self.fail_on:
            raise RuntimeError('synthetic-private-detail')
        self.tools.append(kwargs['name'])
        return Handle(self.tools, kwargs['name'])

    def register_command(self, name, **kwargs):
        self.commands.append(name)

    def register_skill(self, name, *args, **kwargs):
        self.skills.append(name)

    def register_automation_blueprint(self, *args, **kwargs):
        pass


@pytest.mark.parametrize('fail_on', ['reinfolib_records', 'reinfolib_export', 'reinfolib_catalog'])
def test_partial_v2_failure_leaves_no_v2_tools(fail_on, caplog):
    context = Context(fail_on)
    with caplog.at_level(logging.WARNING):
        entry().register(context)
    assert not V2 & set(context.tools)
    assert context.tools == ['reinfolib_quarter']
    assert context.commands == ['jp-reinfolib'] and context.skills == ['weekly-city-check']
    assert 'synthetic-private-detail' not in caplog.text


def test_rejected_name_among_accepted_handles_rolls_back():
    class Rejecting(Context):
        def register_tool(self, **kwargs):
            if kwargs['name'] == 'reinfolib_export':
                return None  # Hermes returns None when a name is refused
            return super().register_tool(**kwargs)

    context = Rejecting(fail_on=None)
    entry().register(context)
    assert context.tools == ['reinfolib_quarter']
    assert 'property-transactions' not in context.skills


def test_definitions_are_built_before_any_registration(monkeypatch):
    """A failure while preparing schemas must happen before ctx.register_tool is called."""
    context = Context(fail_on=None)
    broken = dict(adapter.DEFINITIONS)
    broken['reinfolib_catalog'] = None  # not unpackable
    monkeypatch.setattr(adapter, 'DEFINITIONS', broken)
    with pytest.raises(Exception):
        adapter.register(context)
    assert context.tools == []


def test_real_hermes_partial_failure_rolls_back(monkeypatch, tmp_path):
    plugins = pytest.importorskip('hermes_cli.plugins')
    registry_mod = pytest.importorskip('tools.registry')
    constants = pytest.importorskip('hermes_constants')
    home = tmp_path / 'hermes'
    dest = home / 'plugins' / 'jp-reinfolib'
    dest.mkdir(parents=True)
    for path in ROOT.iterdir():
        if path.name.startswith('.') or path.name in {'tests', '__pycache__', 'docs'}:
            continue
        if path.is_file():
            shutil.copy2(path, dest / path.name)
        elif path.name in {'skills', 'reinfolib'}:
            shutil.copytree(path, dest / path.name)
    (home / 'config.yaml').write_text('plugins:\n  enabled:\n    - jp-reinfolib\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(home))
    override = constants.set_hermes_home_override(str(home))
    original = plugins.PluginContext.register_tool

    def failing(self, name, *args, **kwargs):
        if name == 'reinfolib_export':
            raise RuntimeError('synthetic-private-detail')
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(plugins.PluginContext, 'register_tool', failing)
    plugins._reset_plugin_managers_for_tests()
    try:
        plugins.discover_plugins(force=True)
        names = set(registry_mod.registry.get_all_tool_names())
        assert 'reinfolib_quarter' in names
        assert not V2 & names, sorted(V2 & names)
    finally:
        constants.reset_hermes_home_override(override)
        plugins._reset_plugin_managers_for_tests()
