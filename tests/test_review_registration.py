"""P1-1: optional v2 failure must not remove legacy registrations."""
import builtins
import importlib.util
import logging
from pathlib import Path
import pytest
from reinfolib import adapter

ROOT = Path(__file__).resolve().parents[1]


def entry():
    spec = importlib.util.spec_from_file_location('p1_review_entry', ROOT / '__init__.py')
    assert spec is not None and spec.loader is not None
    spec.submodule_search_locations = None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Context:
    def __init__(self):
        self.tools, self.commands, self.skills, self.blueprints = [], [], [], []
    def register_tool(self, **kwargs):
        self.tools.append(kwargs['name'])
    def register_command(self, name, **kwargs):
        self.commands.append(name)
    def register_skill(self, name, *args, **kwargs):
        self.skills.append(name)
    def register_automation_blueprint(self, name, **kwargs):
        self.blueprints.append(name)


@pytest.mark.parametrize('failure', ['import', 'register'])
def test_legacy_survives_optional_v2_failure(monkeypatch, caplog, failure):
    module = entry()
    if failure == 'register':
        def broken(*args, **kwargs):
            raise RuntimeError('synthetic-private-exception-detail')
        monkeypatch.setattr(adapter, 'register', broken)
    else:
        original = builtins.__import__
        def broken(name, *args, **kwargs):
            if name == 'reinfolib.adapter':
                raise ImportError('synthetic-private-exception-detail')
            return original(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', broken)
    ctx = Context()
    with caplog.at_level(logging.WARNING):
        module.register(ctx)
    assert ctx.tools == ['reinfolib_quarter']
    assert ctx.commands == ['jp-reinfolib']
    assert ctx.skills == ['weekly-city-check']
    assert ctx.blueprints == ['weekly-city-check']
    assert 'Optional v2 registration failed; legacy registrations remain available.' in caplog.text
    assert 'synthetic-private-exception-detail' not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
