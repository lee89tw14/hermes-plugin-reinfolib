"""M3-6: separate history/diff weekly blueprint; legacy weekly-city-check untouched."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def entry():
    spec = importlib.util.spec_from_file_location('m3_blueprint_entry', ROOT / '__init__.py')
    spec.submodule_search_locations = None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Ctx:
    def __init__(self):
        self.tools, self.blueprints = [], []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs['name'])

    def register_command(self, *args, **kwargs):
        pass

    def register_skill(self, *args, **kwargs):
        pass

    def register_automation_blueprint(self, key, **kwargs):
        self.blueprints.append((key, kwargs))
        return object()


def test_new_blueprint_registered_after_legacy_one():
    ctx = Ctx()
    entry().register(ctx)
    assert [key for key, _ in ctx.blueprints] == ['weekly-city-check', 'weekly-history-check']


def test_legacy_blueprint_prompt_is_byte_identical_to_dev():
    import blueprint
    # sha256 of blueprint.PROMPT_TEMPLATE at dev 2d137c5 (legacy contract).
    digest = hashlib.sha256(blueprint.PROMPT_TEMPLATE.encode()).hexdigest()
    assert digest == LEGACY_PROMPT_SHA256
    ctx = Ctx()
    entry().register(ctx)
    legacy = dict(ctx.blueprints)['weekly-city-check']
    assert legacy['prompt_template'] == blueprint.PROMPT_TEMPLATE
    assert 'reinfolib_history' not in legacy['prompt_template']


LEGACY_PROMPT_SHA256 = '0646f8d76c85a2c8e455b295caf2fc582db68b5d70ff957a83f489db585d88b6'


def test_new_prompt_uses_only_new_tools_and_safe_wording():
    from reinfolib import blueprints
    prompt = blueprints.PROMPT_TEMPLATE
    assert 'reinfolib_history' in prompt and 'reinfolib_diff' in prompt
    assert 'reinfolib_quarter' not in prompt and 'reinfolib_transactions' not in prompt
    assert 'not new transactions' in prompt
    assert 'not zero' in prompt
    assert '[SILENT]' in prompt and 'Never answer [SILENT]' not in prompt
    for outcome in ('found', 'none_found_in_range', 'failed', 'budget_exhausted'):
        assert outcome in prompt
    assert prompt.format(city='13104').count('13104') >= 1
    assert blueprints.KEY != 'weekly-city-check'
    assert [s['name'] for s in blueprints.SLOTS] == ['city', 'day', 'time']


def test_not_registered_without_v2_or_without_api():
    from reinfolib import blueprints
    class Old:
        pass
    assert blueprints.register_blueprint(Old()) is False
    class Raises(Ctx):
        def register_automation_blueprint(self, key, **kwargs):
            raise ValueError('rejected')
    assert blueprints.register_blueprint(Raises()) is False


def test_no_cron_creation_in_new_modules():
    for path in (ROOT / 'reinfolib').glob('*.py'):
        text = path.read_text(encoding='utf-8')
        assert 'create_job(' not in text and 'cron.jobs' not in text, path.name


def test_hermes_accepts_the_blueprint_and_builds_the_job_prompt():
    plugins = pytest.importorskip('cron.blueprint_plugins')
    prompt_mod = pytest.importorskip('cron.scheduler_prompt')
    from cron.blueprint_catalog import fill_blueprint
    from reinfolib import blueprints
    built = plugins.build_plugin_blueprint(
        'jp-reinfolib', blueprints.KEY, title=blueprints.TITLE, description=blueprints.DESCRIPTION,
        schedule_template=blueprints.SCHEDULE_TEMPLATE, prompt_template=blueprints.PROMPT_TEMPLATE,
        category='weekly', slots=list(blueprints.SLOTS), deliver_default='origin', skills=[], tags=['japan'])
    filled = fill_blueprint(built, {'city': '13104'})
    assert filled['schedule'] == '0 9 * * 1'
    job = {'id': 'x', 'name': blueprints.TITLE, 'prompt': filled['prompt']}
    assembled = prompt_mod._build_job_prompt(job)
    assert filled['prompt'] in assembled and '13104' in assembled
