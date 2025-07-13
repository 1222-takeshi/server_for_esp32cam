import pytest
from script.server import app as flask_app

@pytest.fixture
def app():
    """Create and configure a new app instance for each test."""
    # app.config.update({
    #     "TESTING": True,
    # })

    yield flask_app

@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()
