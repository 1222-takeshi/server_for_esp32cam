import os
import pytest
from io import BytesIO

# Assuming UPLOAD_FOLDER is defined in script/server.py
# For testing, we might want to use a temporary directory
# However, for simplicity in this test, we'll rely on the app's UPLOAD_FOLDER

@pytest.fixture(autouse=True)
def clean_uploads_folder(app):
    """Fixture to clean the uploads folder before each test."""
    upload_folder = app.config['UPLOAD_FOLDER']
    for filename in os.listdir(upload_folder):
        file_path = os.path.join(upload_folder, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
        except Exception as e:
            print(f'Failed to delete {file_path}. Reason: {e}')
    yield

def test_index_page(client):
    """Test that the index page loads correctly."""
    response = client.get('/')
    assert response.status_code == 200
    assert b"ESP32 Camera Server" in response.data # Check for a known string in index.html

def test_upload_file_success(client):
    """Test successful file upload."""
    # Create a dummy image file
    image_data = b'my fake image content'
    image_file = BytesIO(image_data)
    image_file.filename = 'test_image.jpg' # Add filename attribute

    data = {'image': (image_file, 'test_image.jpg')}
    response = client.post('/upload', data=data, content_type='multipart/form-data')
    assert response.status_code == 200
    assert b"File uploaded successfully" in response.data
    # Verify file exists in uploads folder
    upload_folder = client.application.config['UPLOAD_FOLDER']
    assert os.path.exists(os.path.join(upload_folder, 'latest.jpg'))
    # Check for timestamped file (we can't know the exact name, just that one exists)
    timestamp_files = [f for f in os.listdir(upload_folder) if f.startswith('image_') and f.endswith('.jpg')]
    assert len(timestamp_files) >= 1

def test_upload_file_no_image_part(client):
    """Test file upload without 'image' part in request."""
    data = {'not_image': (b'some content', 'test.txt')}
    response = client.post('/upload', data=data, content_type='multipart/form-data')
    assert response.status_code == 400
    assert b"No image file part" in response.data

def test_upload_file_empty_filename(client):
    """Test file upload with empty filename."""
    # Create a dummy image file with empty filename
    image_data = b'my fake image content'
    image_file = BytesIO(image_data)
    image_file.filename = '' # Empty filename

    data = {'image': (image_file, '')}
    response = client.post('/upload', data=data, content_type='multipart/form-data')
    assert response.status_code == 400
    assert b"No selected file" in response.data

def test_list_files_page(client):
    """Test that the files listing page loads correctly."""
    response = client.get('/files')
    assert response.status_code == 200
    assert b"Uploaded Files" in response.data # Check for a known string in files.html

def test_get_file_success(client):
    """Test successful file retrieval."""
    # First, upload a file to ensure it exists
    image_data = b'my fake image content for download'
    image_file = BytesIO(image_data)
    image_file.filename = 'download_test.jpg'
    data = {'image': (image_file, 'download_test.jpg')}
    client.post('/upload', data=data, content_type='multipart/form-data')

    # Then, try to retrieve it (using latest.jpg for simplicity)
    response = client.get('/files/latest.jpg')
    assert response.status_code == 200
    assert b"my fake image content for download" in response.data

def test_get_file_not_found(client):
    """Test retrieval of a non-existent file."""
    response = client.get('/files/non_existent_file.jpg')
    assert response.status_code == 404 # Flask returns 404 for send_from_directory not found
    # The error message might vary based on Flask's default 404 handler
