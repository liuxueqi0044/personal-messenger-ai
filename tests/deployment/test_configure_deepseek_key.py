import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('configure',Path('scripts/deployment/configure_deepseek_key.py'))
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
class FakeStore:
    def __init__(self): self.values={}
    def set_secret(self,name,value): self.values[name]=value
def test_configures_without_exposing_value():
    store=FakeStore(); module.configure(store,'deepseek.api_key',lambda _:'secret-value')
    assert store.values=={'deepseek.api_key':b'secret-value'}
