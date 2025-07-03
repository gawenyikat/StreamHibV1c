import os
import subprocess
import re
import shutil
from flask import request, jsonify, send_from_directory
from .config import *
from .auth import login_required

def init_videos(app, socketio):
    """Initialize videos module"""
    
    @app.route('/api/videos', methods=['GET'])
    @login_required
    def list_videos_api():
        try: 
            return jsonify(get_videos_list())
        except Exception as e: 
            logger.error(f"Error API /api/videos: {str(e)}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Gagal ambil daftar video.'}), 500

    @app.route('/api/download', methods=['POST'])
    @login_required
    def download_video_api():
        try:
            data = request.json
            input_val = data.get('file_id')
            if not input_val: 
                return jsonify({'status': 'error', 'message': 'ID/URL Video diperlukan'}), 400
            vid_id = extract_drive_id(input_val)
            if not vid_id: 
                return jsonify({'status': 'error', 'message': 'Format ID/URL GDrive tidak valid atau tidak ditemukan.'}), 400
            
            output_dir_param = VIDEOS_DIR + os.sep 
            cmd = ["/usr/local/bin/gdown", f"https://drive.google.com/uc?id={vid_id.strip()}&export=download", "-O", output_dir_param, "--no-cookies", "--quiet", "--continue"]
            
            logger.debug(f"Download cmd: {' '.join(cmd)}")
            files_before = set(os.listdir(VIDEOS_DIR))
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800) 
            files_after = set(os.listdir(VIDEOS_DIR))
            new_files = files_after - files_before

            if res.returncode == 0:
                downloaded_filename_to_check = None
                if new_files:
                    downloaded_filename_to_check = new_files.pop() 
                    name_part, ext_part = os.path.splitext(downloaded_filename_to_check)
                    if not ext_part and name_part == vid_id: 
                        new_filename_with_ext = f"{downloaded_filename_to_check}.mp4" 
                        try:
                            os.rename(os.path.join(VIDEOS_DIR, downloaded_filename_to_check), os.path.join(VIDEOS_DIR, new_filename_with_ext))
                            logger.info(f"File download {downloaded_filename_to_check} di-rename menjadi {new_filename_with_ext}")
                        except Exception as e_rename_gdown:
                            logger.error(f"Gagal me-rename file download {downloaded_filename_to_check} setelah gdown: {e_rename_gdown}")
                elif "already exists" in res.stderr.lower() or "already exists" in res.stdout.lower():
                     logger.info(f"File untuk ID {vid_id} kemungkinan sudah ada. Tidak ada file baru terdeteksi.")
                else:
                    logger.warning(f"gdown berhasil (code 0) tapi tidak ada file baru terdeteksi di {VIDEOS_DIR}. Output: {res.stdout} Err: {res.stderr}")

                from . import get_socketio
                socketio_inst = get_socketio()
                if socketio_inst:
                    socketio_inst.emit('videos_update', get_videos_list())
                return jsonify({'status': 'success', 'message': 'Download video berhasil. Cek daftar video.'})
            else:
                logger.error(f"Gdown error (code {res.returncode}): {res.stderr} | stdout: {res.stdout}")
                err_msg = f'Download Gagal: {res.stderr[:250]}' 
                if "Permission denied" in res.stderr or "Zugriff verweigert" in res.stderr: 
                    err_msg = "Download Gagal: Pastikan file publik atau Anda punya izin."
                elif "File not found" in res.stderr or "No such file" in res.stderr or "Cannot retrieve BFC cookies" in res.stderr: 
                    err_msg = "Download Gagal: File tidak ditemukan atau tidak dapat diakses."
                elif "ERROR:" in res.stderr: 
                    err_msg = f"Download Gagal: {res.stderr.split('ERROR:')[1].strip()[:200]}"
                return jsonify({'status': 'error', 'message': err_msg}), 500
        except subprocess.TimeoutExpired: 
            logger.error("Proses download video timeout.")
            return jsonify({'status': 'error', 'message': 'Download timeout (30 menit).'}), 500
        except Exception as e: 
            logger.error("Error tidak terduga saat download video", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

    @app.route('/api/videos/delete', methods=['POST'])
    @login_required
    def delete_video_api(): 
        try:
            fname = request.json.get('file_name')
            if not fname: 
                return jsonify({'status': 'error', 'message': 'Nama file diperlukan'}), 400
            fpath = os.path.join(VIDEOS_DIR, fname)
            if not os.path.isfile(fpath): 
                return jsonify({'status': 'error', 'message': f'File "{fname}" tidak ada'}), 404
            os.remove(fpath)
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('videos_update', get_videos_list())
            return jsonify({'status': 'success', 'message': f'Video "{fname}" dihapus'})
        except Exception as e: 
            logger.error(f"Error delete video {request.json.get('file_name', 'N/A')}", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

    @app.route('/api/videos/delete-all', methods=['POST'])
    @login_required
    def delete_all_videos_api(): 
        try:
            count = 0
            for vid in get_videos_list(): 
                try: 
                    os.remove(os.path.join(VIDEOS_DIR, vid))
                    count += 1
                except Exception as e: 
                    logger.error(f"Error hapus video {vid}: {str(e)}")
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('videos_update', get_videos_list())
            return jsonify({'status': 'success', 'message': f'Berhasil menghapus {count} video.', 'deleted_count': count})
        except Exception as e: 
            logger.error("Error di API delete_all_videos", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

    @app.route('/api/videos/rename', methods=['POST'])
    @login_required
    def rename_video_api(): 
        try:
            data = request.get_json()
            old, new_base = data.get('old_name'), data.get('new_name')
            if not all([old, new_base]): 
                return jsonify({'status': 'error', 'message': 'Nama lama & baru diperlukan'}), 400
            if not re.match(r'^[\w\-. ]+$', new_base): 
                return jsonify({'status': 'error', 'message': 'Nama baru tidak valid (hanya huruf, angka, spasi, titik, strip, underscore).'}), 400
            old_p = os.path.join(VIDEOS_DIR, old)
            if not os.path.isfile(old_p): 
                return jsonify({'status': 'error', 'message': f'File "{old}" tidak ada'}), 404
            new_p = os.path.join(VIDEOS_DIR, new_base.strip() + os.path.splitext(old)[1])
            if old_p == new_p: 
                return jsonify({'status': 'success', 'message': 'Nama video tidak berubah.'})
            if os.path.isfile(new_p): 
                return jsonify({'status': 'error', 'message': f'Nama "{os.path.basename(new_p)}" sudah ada.'}), 400
            os.rename(old_p, new_p)
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('videos_update', get_videos_list())
            return jsonify({'status': 'success', 'message': f'Video diubah ke "{os.path.basename(new_p)}"'})
        except Exception as e: 
            logger.error("Error rename video", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

    @app.route('/api/disk-usage', methods=['GET'])
    @login_required
    def disk_usage_api(): 
        try:
            t, u, f = shutil.disk_usage(VIDEOS_DIR)
            tg, ug, fg = t/(2**30), u/(2**30), f/(2**30)
            pu = (u/t)*100 if t > 0 else 0
            stat = 'full' if pu > 95 else 'almost_full' if pu > 80 else 'normal'
            return jsonify({'status': stat, 'total': round(tg, 2), 'used': round(ug, 2), 'free': round(fg, 2), 'percent_used': round(pu, 2)})
        except Exception as e: 
            logger.error(f"Error disk usage: {str(e)}", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

    @app.route('/videos/<filename>')
    @login_required
    def serve_video(filename):
        return send_from_directory(VIDEOS_DIR, filename)

def get_videos_list():
    """Get list of video files"""
    if not os.path.exists(VIDEOS_DIR):
        os.makedirs(VIDEOS_DIR)
        return []
    
    video_extensions = ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm']
    video_files = []
    
    for file in os.listdir(VIDEOS_DIR):
        if any(file.lower().endswith(ext) for ext in video_extensions):
            video_files.append(file)
    
    return sorted(video_files)

def extract_drive_id(url_or_id):
    """Extract Google Drive file ID from URL or return ID if already valid"""
    if not url_or_id:
        return None
    
    # If it's a Google Drive URL, extract ID
    if "drive.google.com" in url_or_id:
        patterns = [
            r'/file/d/([a-zA-Z0-9_-]+)',
            r'id=([a-zA-Z0-9_-]+)',
            r'/d/([a-zA-Z0-9_-]+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url_or_id)
            if match:
                return match.group(1)
    
    # If it looks like a valid ID, return it
    if re.match(r'^[a-zA-Z0-9_-]{20,}$', url_or_id):
        return url_or_id
    
    return None