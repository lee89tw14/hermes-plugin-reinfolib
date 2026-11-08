"""P3-2: v2 workflow is discoverable without changing weekly legacy instructions."""
from pathlib import Path
import importlib.util
import os
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'property-transactions' / 'SKILL.md'
QUERIES = ['transaction snapshot records', 'page saved property records',
           'export property records CSV', 'station property transactions',
           'Japan property records export']


def test_v2_skill_contract_and_registration():
    assert SKILL.exists()
    text = SKILL.read_text(encoding='utf-8')
    assert text.startswith('---\nname: property-transactions\n')
    for required in ['reinfolib_transactions', 'reinfolib_records', 'reinfolib_export',
                     'fallback_required', 'API-covered records', '01', '02', 'both',
                     'deleted automatically after 30 days', 'snapshot_id', 'deletion is not exposed']:
        assert required in text
    assert 'no automatic retention' not in text
    spec = importlib.util.spec_from_file_location('skill_review_entry', ROOT / '__init__.py')
    assert spec and spec.loader
    spec.submodule_search_locations = None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = []
    class Context:
        def register_tool(self, **kwargs): pass
        def register_command(self, *args, **kwargs): pass
        def register_skill(self, name, path, **kwargs): names.append((name, Path(path)))
    module.register(Context())
    assert names == [('property-transactions', SKILL), ('weekly-city-check', ROOT / 'skills' / 'weekly-city-check' / 'SKILL.md')]


def test_v2_tool_recall_against_core_tools(tmp_path):
    python, src = os.environ.get('HERMES_AGENT_PYTHON'), os.environ.get('HERMES_AGENT_SRC')
    if not python or not src:
        pytest.skip('Set HERMES_AGENT_PYTHON and HERMES_AGENT_SRC to rank v2 tools.')
    code = r'''
import importlib.util, sys
from pathlib import Path
root=Path(sys.argv[1]); sys.path.insert(0, sys.argv[2])
import model_tools
from toolsets import _HERMES_CORE_TOOLS
from tools.registry import registry
from tools.tool_search_catalog import build_catalog, search_catalog
spec=importlib.util.spec_from_file_location('v2_rank', root/'__init__.py', submodule_search_locations=[str(root)])
m=importlib.util.module_from_spec(spec); sys.modules['v2_rank']=m; spec.loader.exec_module(m)
tools=[]
class C:
    def register_tool(self, **k): tools.append(k)
    def __getattr__(self, n): return lambda *a, **k: None
m.register(C())
core=[{'type':'function','function':{**e.schema,'name':e.name}} for e in registry.get_all_entries() if e.name in set(_HERMES_CORE_TOOLS)]
catalog=build_catalog(core+[{'type':'function','function':t['schema']} for t in tools])
v2={'reinfolib_transactions','reinfolib_records','reinfolib_export'}
for q in sys.argv[3:]:
    ranked=[x.name for x in search_catalog(catalog, q, limit=5)]
    print(('HIT' if v2.intersection(ranked) else 'MISS'), q, ranked)
'''
    env = os.environ.copy()
    env['HERMES_HOME'] = str(tmp_path / 'home')
    proc = subprocess.run([python, '-c', code, str(ROOT), src, *QUERIES], text=True, capture_output=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count('HIT ') == len(QUERIES), proc.stdout
