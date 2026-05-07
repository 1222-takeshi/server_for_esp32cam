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
        # Check if a file was uploaded
        if 'image' not in request.files:
            app.logger.error('No image file part in the request')
            return jsonify({'message': 'No image file part'}), 400

        file = request.files['image']

        # If the user does not select a file, the browser submits an
        # empty file without a filename.
        if file.filename == '':
            app.logger.error('No selected file')
            return jsonify({'message': 'No selected file'}), 400

        if file:
            allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
            if '.' not in file.filename or \
               file.filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
                app.logger.error(f'Invalid file type uploaded: {file.filename}')
                return jsonify({'message': 'Invalid file type'}), 400

            # Save with a timestamped filename
            timestamp_filename = f'image_{int(time.time())}.jpg'
            timestamp_filepath = os.path.join(UPLOAD_FOLDER, timestamp_filename)
            file.save(timestamp_filepath)

            # Also save as 'latest.jpg' (overwriting previous latest)
            latest_filepath = os.path.join(UPLOAD_FOLDER, 'latest.jpg')
            file.seek(0) # Reset file pointer to the beginning before saving again
            file.save(latest_filepath)

            app.logger.info(f'File uploaded successfully: {timestamp_filepath} and {latest_filepath}')
            return jsonify({'message': 'File uploaded successfully'}), 200
    except Exception as e:
        app.logger.error(f'Exception occurred during file upload: {str(e)}')
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
