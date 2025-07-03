import os
import subprocess
import re
from datetime import datetime, timedelta
import pytz
from flask import request, jsonify, session
from .config import *
from .auth import login_required
from .sessions import load_sessions, save_sessions, get_active_sessions_data, get_inactive_sessions_data

def init_streaming(app, socketio, scheduler):
    """Initialize streaming module"""
    
    @app.route('/api/start', methods=['POST'])
    @login_required
    def start_streaming_api(): 
        try:
            data = request.json
            platform = data.get('platform')
            stream_key = data.get('stream_key')
            video_file = data.get('video_file')
            session_name_original = data.get('session_name')
            
            if not all([platform, stream_key, video_file, session_name_original, session_name_original.strip()]):
                return jsonify({'status': 'error', 'message': 'Semua field wajib diisi dan nama sesi tidak boleh kosong.'}), 400
            
            video_path = os.path.abspath(os.path.join(VIDEOS_DIR, video_file))
            if not os.path.isfile(video_path):
                return jsonify({'status': 'error', 'message': f'File video {video_file} tidak ditemukan'}), 404
            if platform not in ["YouTube", "Facebook"]:
                return jsonify({'status': 'error', 'message': 'Platform tidak valid. Pilih YouTube atau Facebook.'}), 400
            
            platform_url = "rtmp://a.rtmp.youtube.com/live2" if platform == "YouTube" else "rtmps://live-api-s.facebook.com:443/rtmp"
            
            service_name_systemd, sanitized_service_id_part = create_service_file(session_name_original, video_path, platform_url, stream_key)
            subprocess.run(["systemctl", "start", service_name_systemd], check=True)
            
            start_time_iso = datetime.now(jakarta_tz).isoformat()
            new_session_entry = {
                "id": session_name_original,
                "sanitized_service_id": sanitized_service_id_part,
                "video_name": video_file,
                "stream_key": stream_key, 
                "platform": platform, 
                "status": "active",
                "start_time": start_time_iso, 
                "scheduleType": "manual", 
                "stopTime": None, 
                "duration_minutes": 0 
            }
            
            s_data = load_sessions()
            s_data['active_sessions'] = add_or_update_session_in_list(
                s_data.get('active_sessions', []), new_session_entry
            )
            s_data['inactive_sessions'] = [s for s in s_data.get('inactive_sessions', []) if s.get('id') != session_name_original]
            save_sessions(s_data)
            
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('sessions_update', get_active_sessions_data())
                socketio_inst.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            return jsonify({'status': 'success', 'message': f'Berhasil memulai Live Stream untuk sesi "{session_name_original}"'}), 200
            
        except subprocess.CalledProcessError as e: 
            session_name_req = data.get('session_name', 'N/A') if isinstance(data, dict) else 'N/A'
            logger.error(f"Gagal start service untuk sesi '{session_name_req}': {e.stderr if e.stderr else e.stdout}")
            return jsonify({'status': 'error', 'message': f"Gagal memulai layanan systemd: {e.stderr if e.stderr else e.stdout}"}), 500
        except Exception as e: 
            session_name_req = data.get('session_name', 'N/A') if isinstance(data, dict) else 'N/A'
            logger.error(f"Error tidak terduga saat start streaming untuk sesi '{session_name_req}'", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

    @app.route('/api/stop', methods=['POST'])
    @login_required
    def stop_streaming_api(): 
        try:
            data = request.get_json()
            if not data: 
                return jsonify({'status': 'error', 'message': 'Request JSON tidak valid.'}), 400
            session_id_to_stop = data.get('session_id')
            if not session_id_to_stop: 
                return jsonify({'status': 'error', 'message': 'ID sesi (nama sesi asli) diperlukan'}), 400
            
            if stop_streaming_session(session_id_to_stop):
                from . import get_socketio
                socketio_inst = get_socketio()
                if socketio_inst:
                    socketio_inst.emit('sessions_update', get_active_sessions_data())
                    socketio_inst.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
                return jsonify({'status': 'success', 'message': f'Sesi "{session_id_to_stop}" berhasil dihentikan atau sudah tidak aktif.'})
            else:
                return jsonify({'status': 'error', 'message': 'Failed to stop streaming'}), 500
                
        except Exception as e:
            logger.error(f"Error stopping streaming: {e}")
            return jsonify({'status': 'error', 'message': 'Failed to stop streaming'}), 500

    @app.route('/api/reactivate', methods=['POST'])
    @login_required
    def reactivate_session_api(): 
        try:
            data = request.json
            session_id_to_reactivate = data.get('session_id')
            if not session_id_to_reactivate: 
                return jsonify({"status": "error", "message": "ID sesi (nama sesi asli) diperlukan"}), 400
            
            s_data = load_sessions()
            session_obj_to_reactivate = next((s for s in s_data.get('inactive_sessions', []) if s['id'] == session_id_to_reactivate), None)
            if not session_obj_to_reactivate: 
                return jsonify({"status": "error", "message": f"Sesi '{session_id_to_reactivate}' tidak ada di daftar tidak aktif."}), 404
            
            video_file = session_obj_to_reactivate.get("video_name")
            stream_key = session_obj_to_reactivate.get("stream_key")
            platform = data.get('platform', session_obj_to_reactivate.get('platform', 'YouTube')) 
            
            if not video_file or not stream_key:
                return jsonify({"status": "error", "message": "Detail video atau stream key tidak lengkap untuk reaktivasi."}), 400
            
            video_path = os.path.abspath(os.path.join(VIDEOS_DIR, video_file))
            if not os.path.isfile(video_path):
                return jsonify({"status": "error", "message": f"File video '{video_file}' tidak ditemukan untuk reaktivasi."}), 404
            if platform not in ["YouTube", "Facebook"]: 
                platform = "YouTube" 
            
            platform_url = "rtmp://a.rtmp.youtube.com/live2" if platform == "YouTube" else "rtmps://live-api-s.facebook.com:443/rtmp"
            
            service_name_systemd, new_sanitized_service_id_part = create_service_file(session_id_to_reactivate, video_path, platform_url, stream_key) 
            subprocess.run(["systemctl", "start", service_name_systemd], check=True) 
            
            session_obj_to_reactivate['status'] = 'active'
            session_obj_to_reactivate['start_time'] = datetime.now(jakarta_tz).isoformat()
            session_obj_to_reactivate['platform'] = platform 
            session_obj_to_reactivate['sanitized_service_id'] = new_sanitized_service_id_part
            if 'stop_time' in session_obj_to_reactivate: 
                del session_obj_to_reactivate['stop_time'] 
            session_obj_to_reactivate['scheduleType'] = 'manual_reactivated'
            session_obj_to_reactivate['stopTime'] = None 
            session_obj_to_reactivate['duration_minutes'] = 0

            s_data['inactive_sessions'] = [s for s in s_data['inactive_sessions'] if s['id'] != session_id_to_reactivate] 
            s_data['active_sessions'] = add_or_update_session_in_list(
                s_data.get('active_sessions', []), session_obj_to_reactivate
            )
            save_sessions(s_data)
            
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('sessions_update', get_active_sessions_data())
                socketio_inst.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            return jsonify({"status": "success", "message": f"Sesi '{session_id_to_reactivate}' berhasil diaktifkan kembali (Live Sekarang).", "platform": platform})

        except subprocess.CalledProcessError as e: 
            req_data_reactivate = request.get_json(silent=True) or {}
            session_id_err_reactivate = req_data_reactivate.get('session_id', 'N/A')
            logger.error(f"Gagal start service untuk reaktivasi sesi '{session_id_err_reactivate}': {e.stderr if e.stderr else e.stdout}")
            return jsonify({"status": "error", "message": f"Gagal memulai layanan systemd: {e.stderr if e.stderr else e.stdout}"}), 500
        except Exception as e: 
            req_data_reactivate_exc = request.get_json(silent=True) or {}
            session_id_err_reactivate_exc = req_data_reactivate_exc.get('session_id', 'N/A')
            logger.error(f"Error saat reaktivasi sesi '{session_id_err_reactivate_exc}'", exc_info=True)
            return jsonify({"status": "error", "message": f'Kesalahan Server Internal: {str(e)}'}), 500

    @app.route('/api/delete-session', methods=['POST'])
    @login_required
    def delete_session_api(): 
        try:
            session_id_to_delete = request.json.get('session_id')
            if not session_id_to_delete: 
                return jsonify({'status': 'error', 'message': 'ID sesi (nama sesi asli) diperlukan'}), 400
            s_data = load_sessions()
            if not any(s['id'] == session_id_to_delete for s in s_data.get('inactive_sessions', [])): 
                return jsonify({'status': 'error', 'message': f"Sesi '{session_id_to_delete}' tidak ditemukan di daftar tidak aktif."}), 404
            s_data['inactive_sessions'] = [s for s in s_data['inactive_sessions'] if s['id'] != session_id_to_delete]
            save_sessions(s_data)
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            return jsonify({'status': 'success', 'message': f"Sesi '{session_id_to_delete}' berhasil dihapus dari daftar tidak aktif."})
        except Exception as e: 
            req_data_del_sess = request.get_json(silent=True) or {}
            session_id_err_del_sess = req_data_del_sess.get('session_id', 'N/A')
            logger.error(f"Error delete sesi '{session_id_err_del_sess}'", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

    @app.route('/api/edit-session', methods=['POST'])
    @login_required
    def edit_inactive_session_api(): 
        try:
            data = request.json
            session_id_to_edit = data.get('session_name_original', data.get('id'))
            new_stream_key = data.get('stream_key')
            new_video_name = data.get('video_file')
            new_platform = data.get('platform', 'YouTube')
            
            if not session_id_to_edit: 
                return jsonify({"status": "error", "message": "ID sesi (nama sesi asli) diperlukan untuk edit."}), 400
            s_data = load_sessions()
            session_found = next((s for s in s_data.get('inactive_sessions', []) if s['id'] == session_id_to_edit), None)
            if not session_found: 
                return jsonify({"status": "error", "message": f"Sesi '{session_id_to_edit}' tidak ditemukan di daftar tidak aktif."}), 404
            
            if not new_stream_key or not new_video_name:
                return jsonify({"status": "error", "message": "Stream key dan nama video baru diperlukan untuk update."}), 400
            
            video_path_check = os.path.join(VIDEOS_DIR, new_video_name)
            if not os.path.isfile(video_path_check):
                return jsonify({"status": "error", "message": f"File video baru '{new_video_name}' tidak ditemukan."}), 404
            if new_platform not in ["YouTube", "Facebook"]: 
                new_platform = "YouTube" 
            
            session_found['stream_key'] = new_stream_key.strip()
            session_found['video_name'] = new_video_name
            session_found['platform'] = new_platform
            
            save_sessions(s_data)
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            return jsonify({"status": "success", "message": f"Detail sesi tidak aktif '{session_id_to_edit}' berhasil diperbarui."})
        except Exception as e: 
            req_data_edit_sess = request.get_json(silent=True) or {}
            session_id_err_edit_sess = req_data_edit_sess.get('session_name_original', req_data_edit_sess.get('id', 'N/A'))
            logger.error(f"Error edit sesi tidak aktif '{session_id_err_edit_sess}'", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server Internal: {str(e)}'}), 500

def create_service_file(session_name_original, video_path, platform_url, stream_key):
    """Create systemd service file"""
    sanitized_service_part = sanitize_for_service_name(session_name_original)
    service_name = f"stream-{sanitized_service_part}.service"

    service_path = os.path.join(SERVICE_DIR, service_name)
    
    # Enhanced FFmpeg command with better error handling and logging
    service_content = f"""[Unit]
Description=Streaming service for {session_name_original}
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/ffmpeg -re -stream_loop -1 -i "{video_path}" -c:v libx264 -preset veryfast -maxrate 3000k -bufsize 6000k -pix_fmt yuv420p -g 50 -c:a aac -b:a 160k -ac 2 -ar 44100 -f flv "{platform_url}/{stream_key}"
Restart=always
RestartSec=5
User=root
StandardOutput=journal
StandardError=journal
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(service_path, 'w') as f: 
            f.write(service_content)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        logger.info(f"STREAMING: Service file created: {service_name} (from original: '{session_name_original}')")
        return service_name, sanitized_service_part
    except Exception as e:
        logger.error(f"STREAMING: Error creating service file {service_name} (from original: '{session_name_original}'): {e}")
        raise

def stop_streaming_session(session_id_to_stop):
    """Stop a streaming session"""
    try:
        s_data = load_sessions()
        active_session_data = next((s for s in s_data.get('active_sessions', []) if s['id'] == session_id_to_stop), None)
        
        sanitized_service_id_for_stop = None
        if active_session_data and 'sanitized_service_id' in active_session_data:
            sanitized_service_id_for_stop = active_session_data['sanitized_service_id']
        else:
            sanitized_service_id_for_stop = sanitize_for_service_name(session_id_to_stop)
            logger.warning(f"STREAMING: Menggunakan fallback sanitized_service_id '{sanitized_service_id_for_stop}' untuk menghentikan sesi '{session_id_to_stop}'.")

        service_name_systemd = f"stream-{sanitized_service_id_for_stop}.service"
        
        try:
            subprocess.run(["systemctl", "stop", service_name_systemd], check=False, timeout=15)
            service_path = os.path.join(SERVICE_DIR, service_name_systemd)
            if os.path.exists(service_path): 
                os.remove(service_path)
                subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=10)
        except Exception as e_service_stop:
             logger.warning(f"STREAMING: Peringatan saat menghentikan/menghapus service {service_name_systemd}: {e_service_stop}")
            
        stop_time_iso = datetime.now(jakarta_tz).isoformat()
        session_updated_or_added_to_inactive = False

        if active_session_data: 
            active_session_data['status'] = 'inactive'
            active_session_data['stop_time'] = stop_time_iso
            s_data['inactive_sessions'] = add_or_update_session_in_list(
                s_data.get('inactive_sessions', []), active_session_data
            )
            s_data['active_sessions'] = [s for s in s_data['active_sessions'] if s['id'] != session_id_to_stop]
            session_updated_or_added_to_inactive = True
        elif not any(s['id'] == session_id_to_stop for s in s_data.get('inactive_sessions', [])): 
            s_data.setdefault('inactive_sessions', []).append({
                "id": session_id_to_stop,
                "sanitized_service_id": sanitized_service_id_for_stop,
                "video_name": "unknown (force stop)", 
                "stream_key": "unknown", 
                "platform": "unknown",
                "status": "inactive", 
                "stop_time": stop_time_iso, 
                "duration_minutes": 0,
                "scheduleType": "manual_force_stop"
            })
            session_updated_or_added_to_inactive = True
            
        if session_updated_or_added_to_inactive:
            save_sessions(s_data)
        
        logger.info(f"STREAMING: Stopped streaming session: {session_id_to_stop}")
        return True
        
    except Exception as e:
        logger.error(f"STREAMING: Error stopping streaming session: {e}")
        return False

def is_service_running(session_id):
    """Check if systemd service is running"""
    try:
        service_name = f"stream-{session_id}"
        result = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True,
            text=True
        )
        return result.stdout.strip() == 'active'
    except Exception as e:
        logger.error(f"Error checking service {session_id}: {e}")
        return False

def create_systemd_service(session_id, video_file, rtmp_url, stream_key):
    """Create systemd service for streaming"""
    try:
        service_name = f"stream-{session_id}"
        service_file = f"/etc/systemd/system/{service_name}.service"
        
        video_path = os.path.join(os.getcwd(), VIDEOS_DIR, video_file)
        full_rtmp_url = f"{rtmp_url}/{stream_key}"
        
        service_content = f"""[Unit]
Description=StreamHib Session {session_id}
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/ffmpeg -re -stream_loop -1 -i "{video_path}" -c:v libx264 -preset veryfast -maxrate 3000k -bufsize 6000k -pix_fmt yuv420p -g 50 -c:a aac -b:a 160k -ac 2 -ar 44100 -f flv "{full_rtmp_url}"
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"""
        
        with open(service_file, 'w') as f:
            f.write(service_content)
        
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', service_name], check=True)
        subprocess.run(['systemctl', 'start', service_name], check=True)
        
        logger.info(f"Created and started service {service_name}")
        return True
        
    except Exception as e:
        logger.error(f"Error creating service for {session_id}: {e}")
        return False

def stop_systemd_service(session_id):
    """Stop and remove systemd service"""
    try:
        service_name = f"stream-{session_id}"
        service_file = f"/etc/systemd/system/{service_name}.service"
        
        subprocess.run(['systemctl', 'stop', service_name], check=False)
        subprocess.run(['systemctl', 'disable', service_name], check=False)
        
        if os.path.exists(service_file):
            os.remove(service_file)
        
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        
        logger.info(f"Stopped and removed service {service_name}")
        return True
        
    except Exception as e:
        logger.error(f"Error stopping service for {session_id}: {e}")
        return False

def create_streaming_session(platform, stream_key, video_file, session_name):
    """Create a new streaming session"""
    try:
        session_id = sanitize_for_service_name(session_name)
        
        if platform == 'YouTube':
            rtmp_url = 'rtmp://a.rtmp.youtube.com/live2'
        elif platform == 'Facebook':
            rtmp_url = 'rtmps://live-api-s.facebook.com:443/rtmp'
        else:
            logger.error(f"Unsupported platform: {platform}")
            return None
        
        if not create_systemd_service(session_id, video_file, rtmp_url, stream_key):
            return None
        
        sessions_data = load_sessions()
        sessions_data['active_sessions'][session_id] = {
            'username': session.get('username', 'Unknown'),
            'video_file': video_file,
            'platform': platform,
            'stream_key': stream_key,
            'started_at': datetime.now(jakarta_tz).isoformat(),
            'status': 'active'
        }
        
        save_sessions(sessions_data)
        
        logger.info(f"Created streaming session: {session_id}")
        return session_id
        
    except Exception as e:
        logger.error(f"Error creating streaming session: {e}")
        return None