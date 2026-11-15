import pytest
from store_contract import StoreContract

from shared_context.store.memory import InMemoryStore


class TestInMemoryStore(StoreContract):
    @pytest.fixture
    def make_store(self):
        return InMemoryStore
