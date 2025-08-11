import shutil
import tempfile
import pytest
from script import server as server_module

@pytest.fixture
def app():
    """Create and configure a new app instance for each test with temp upload dir."""
    app = server_module.app
    app.config.update({
        "TESTING": True,
    })

    # Create a temporary upload directory
    tmp_dir = tempfile.mkdtemp(prefix="uploads_")
    original_upload_folder = app.config.get('UPLOAD_FOLDER')
    app.config['UPLOAD_FOLDER'] = tmp_dir
    # Also update module-level variable to minimize surprises
    setattr(server_module, 'UPLOAD_FOLDER', tmp_dir)

    try:
        yield app
    finally:
        # Restore original config and cleanup
        if original_upload_folder is not None:
            app.config['UPLOAD_FOLDER'] = original_upload_folder
            setattr(server_module, 'UPLOAD_FOLDER', original_upload_folder)
        shutil.rmtree(tmp_dir, ignore_errors=True)

@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()
