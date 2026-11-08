"""Do not spend real rate-spacing time on injected offline M4 transports."""
import pytest


@pytest.fixture(autouse=True)
def m4_offline_clock(request, monkeypatch, tmp_path):
    if request.node.path.name.startswith('test_m4_') and 'live' not in request.node.path.name:
        import client
        class Immediate:
            def run(self, call):
                return call()
        monkeypatch.setattr(client, 'DEFAULT_GAP', Immediate())
        from reinfolib import adapter
        from reinfolib.scheduling import RateGate
        monkeypatch.setattr(adapter, 'production_gate', lambda: RateGate(tmp_path / 'm4-rate', spacing=0))
