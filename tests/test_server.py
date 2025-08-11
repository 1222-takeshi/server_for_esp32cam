import io
import os


def test_index_ok(client):
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'ESP32 Camera Server' in resp.data


def test_files_empty_message(client):
    resp = client.get('/files')
    assert resp.status_code == 200
    assert b'No files uploaded yet.' in resp.data


def test_upload_no_file(client):
    resp = client.post('/upload', data={}, content_type='multipart/form-data')
    assert resp.status_code == 400


def test_upload_invalid_extension(client):
    data = {
        'image': (io.BytesIO(b'not an image'), 'test.txt')
    }
    resp = client.post('/upload', data=data, content_type='multipart/form-data')
    assert resp.status_code == 400


def test_upload_success_and_latest_saved(client, app):
    img_bytes = io.BytesIO(b'\xff\xd8\xff')
    data = {
        'image': (img_bytes, 'photo.jpg')
    }
    resp = client.post('/upload', data=data, content_type='multipart/form-data')
    assert resp.status_code == 200

    upload_dir = app.config['UPLOAD_FOLDER']
    files = os.listdir(upload_dir)
    assert any(f.startswith('image_') and f.endswith('.jpg') for f in files)
    assert 'latest.jpg' in files


def test_files_list_and_get_file(client, app):
    # Upload first
    img_bytes = io.BytesIO(b'\xff\xd8\xff')
    data = {
        'image': (img_bytes, 'abc.jpeg')
    }
    resp = client.post('/upload', data=data, content_type='multipart/form-data')
    assert resp.status_code == 200

    # List files
    resp = client.get('/files')
    assert resp.status_code == 200
    assert b'latest.jpeg' in resp.data or b'latest.jpg' in resp.data

    # Pick a stored timestamped file and request it
    files = sorted(os.listdir(app.config['UPLOAD_FOLDER']))
    ts_files = [f for f in files if f.startswith('image_')]
    assert ts_files, 'No timestamped file found after upload'
    target = ts_files[0]
    resp = client.get(f'/files/{target}')
    assert resp.status_code == 200


def test_upload_size_limit(client, app):
    # Set a very small limit to trigger 413
    app.config['MAX_CONTENT_LENGTH'] = 10
    big_bytes = io.BytesIO(b'X' * 100)
    data = {
        'image': (big_bytes, 'big.png')
    }
    resp = client.post('/upload', data=data, content_type='multipart/form-data')
    # Flask returns 413 Request Entity Too Large
    assert resp.status_code == 413
