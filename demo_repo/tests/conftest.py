import pytest

from app.db import connect, seed


@pytest.fixture
def conn():
    c = connect()
    seed(c)
    yield c
    c.close()
