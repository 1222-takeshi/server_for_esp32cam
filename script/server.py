import os
import socket
import time
from flask import Flask, request, jsonify, send_from_directory, render_template

# Set socket default timeout to 60 seconds
socket.setdefaulttimeout(60)

# Set the base directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if not request.data:
            app.logger.error('No data received')
            return jsonify({'message': 'No data received'}), 400
        filename = f'image_{int(time.time())}.jpg'  # タイムスタンプをファイル名に使用
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        with open(filepath, 'wb') as f:
            f.write(request.data)
        app.logger.info(f'File uploaded successfully: {filepath}')
        return jsonify({'message': 'File uploaded successfully'}), 200
    except Exception as e:
        app.logger.error(f'Exception occurred: {str(e)}')
        return jsonify({'message': 'File upload failed', 'error': str(e)}), 500

@app.route('/files')
def list_files():
    try:
        files = os.listdir(app.config['UPLOAD_FOLDER'])
        return render_template('files.html', files=files)
    except Exception as e:
        app.logger.error(f'Exception occurred: {str(e)}')
        return jsonify({'message': 'Error listing files', 'error': str(e)}), 500

@app.route('/files/<filename>')
def get_file(filename):
    try:
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
    except Exception as e:
        app.logger.error(f'Exception occurred: {str(e)}')
        return jsonify({'message': 'Error retrieving file', 'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
