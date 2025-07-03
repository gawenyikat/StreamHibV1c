import os
import json
import subprocess
import hashlib
import uuid
from datetime import datetime, timedelta
import pytz
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from filelock import FileLock
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import logging
import time

app = Flask(__name__)
app.secret_key = 'streamhib_v2_secret_key_2025'
socketio = SocketIO(app, cors_allowed_origins="*")
CORS(app)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
jakarta_tz = pytz.timezone('Asia/Jakarta')
TRIAL_MODE_ENABLED = False
TRIAL_RESET_HOURS = 2

# File paths
SESSIONS_FILE = 'sessions.json'
USERS_FILE = 'users.json'
DOMAIN_CONFIG_FILE = 'domain_config.json'
VIDEOS_DIR = 'videos'
SERVICE_DIR = "/etc/systemd/system"

# File locks
sessions_lock = FileLock(f"{SESSIONS_FILE}.lock")
users_lock = FileLock(f"{USERS_FILE}.lock")
domain_lock = FileLock(f"{DOMAIN_CONFIG_FILE}.lock")

# Admin credentials
ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'streamhib2025'

# Global instances
scheduler = None
socketio_instance = None

def load_json_file(file_path, default_data=None):
    """Load JSON file with error handling"""
    if default_data is None:
        default_data = {}
    
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
        return default_data
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading {file_path}: {e}")
        return default_data

def save_json_file(file_path, data, lock):
    """Save JSON file with file locking"""
    try:
        with lock:
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving {file_path}: {e}")
        return False

def sanitize_for_service_name(session_name_original):
    """Sanitize name for systemd service"""
    import re
    sanitized = re.sub(r'[^\w-]', '-', str(session_name_original))
    sanitized = re.sub(r'-+', '-', sanitized)
    sanitized = sanitized.strip('-')
    return sanitized[:50]

def add_or_update_session_in_list(session_list, new_session_item):
    """Add or update session in list"""
    session_id = new_session_item.get('id')
    if not session_id:
        logger.warning("Session tidak memiliki ID, tidak dapat ditambahkan/diperbarui dalam daftar.")
        return session_list 

    # Hapus item lama jika ada ID yang sama
    updated_list = [s for s in session_list if s.get('id') != session_id]
    updated_list.append(new_session_item)
    return updated_list

# Auth functions
def is_admin_logged_in():
    """Check if admin is logged in"""
    return session.get('admin_logged_in', False)

def is_customer_logged_in():
    """Check if customer is logged in"""
    return session.get('customer_logged_in', False) and session.get('username')

def load_users():
    """Load users data"""
    return load_json_file(USERS_FILE, {})

def save_users(users_data):
    """Save users data"""
    return save_json_file(USERS_FILE, users_data, users_lock)

def login_required(f):
    """Decorator for routes that require customer login"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_customer_logged_in():
            return redirect(url_for('customer_login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator for routes that require admin login"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_admin_logged_in():
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# Sessions functions
def load_sessions():
    """Load sessions data"""
    return load_json_file(SESSIONS_FILE, {
        'active_sessions': [],
        'inactive_sessions': [],
        'scheduled_sessions': []
    })

def save_sessions(sessions_data):
    """Save sessions data"""
    return save_json_file(SESSIONS_FILE, sessions_data, sessions_lock)

# Videos functions
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

# Domain functions
def load_domain_config():
    """Load domain configuration"""
    return load_json_file(DOMAIN_CONFIG_FILE, {})

def save_domain_config(domain_data):
    """Save domain configuration"""
    return save_json_file(DOMAIN_CONFIG_FILE, domain_data, domain_lock)

def get_current_url(domain_config=None):
    """Get current URL based on domain configuration"""
    if domain_config is None:
        domain_config = load_domain_config()
    
    if domain_config.get('domain_name'):
        protocol = 'https' if domain_config.get('ssl_enabled') else 'http'
        domain = domain_config.get('domain_name')
        port = domain_config.get('port', 5000)
        
        # Don't show port for standard ports
        if (protocol == 'http' and port == 80) or (protocol == 'https' and port == 443):
            return f"{protocol}://{domain}"
        else:
            return f"{protocol}://{domain}:{port}"
    else:
        # Fallback to IP
        try:
            server_ip = subprocess.check_output(["curl", "-s", "ifconfig.me"], text=True, timeout=5).strip()
        except:
            try:
                server_ip = subprocess.check_output(["curl", "-s", "ipinfo.io/ip"], text=True, timeout=5).strip()
            except:
                server_ip = "localhost"
        
        return f"http://{server_ip}:5000"

# Streaming functions
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

def get_active_sessions_data():
    """Get active sessions data"""
    try:
        output = subprocess.check_output(["systemctl", "list-units", "--type=service", "--state=running"], text=True)
        all_sessions_data = load_sessions() 
        active_sessions_list = []
        active_services_systemd = {line.split()[0] for line in output.strip().split('\n') if "stream-" in line}
        json_active_sessions = all_sessions_data.get('active_sessions', [])
        needs_json_update = False

        for service_name_systemd in active_services_systemd:
            sanitized_id_from_systemd_service = service_name_systemd.replace("stream-", "").replace(".service", "")
            
            session_json = next((s for s in json_active_sessions if s.get('sanitized_service_id') == sanitized_id_from_systemd_service), None)

            if session_json:
                actual_schedule_type = session_json.get('scheduleType', 'manual')
                actual_stop_time_iso = session_json.get('stopTime') 
                formatted_display_stop_time = None
                if actual_stop_time_iso: 
                    try:
                        stop_time_dt = datetime.fromisoformat(actual_stop_time_iso)
                        formatted_display_stop_time = stop_time_dt.astimezone(jakarta_tz).strftime('%d-%m-%Y Pukul %H:%M:%S')
                    except ValueError: 
                        pass
                
                active_sessions_list.append({
                    'id': session_json.get('id'), 
                    'name': session_json.get('id'), 
                    'startTime': session_json.get('start_time', 'unknown'),
                    'platform': session_json.get('platform', 'unknown'),
                    'video_name': session_json.get('video_name', 'unknown'),
                    'stream_key': session_json.get('stream_key', 'unknown'),
                    'stopTime': formatted_display_stop_time, 
                    'scheduleType': actual_schedule_type,
                    'sanitized_service_id': session_json.get('sanitized_service_id')
                })
            
            else:
                logger.warning(f"Service {service_name_systemd} aktif tapi tidak di JSON active_sessions. Mencoba memulihkan...")
                
                scheduled_definition = next((
                    sched for sched in all_sessions_data.get('scheduled_sessions', []) 
                    if sched.get('sanitized_service_id') == sanitized_id_from_systemd_service
                ), None)

                session_id_original = f"recovered-{sanitized_id_from_systemd_service}"
                video_name_to_use = "unknown (recovered)"
                stream_key_to_use = "unknown"
                platform_to_use = "unknown"
                schedule_type_to_use = "manual_recovered" 
                recovered_stop_time_iso = None
                recovered_duration_minutes = 0
                
                current_recovery_time_iso = datetime.now(jakarta_tz).isoformat()
                current_recovery_dt = datetime.fromisoformat(current_recovery_time_iso)
                formatted_display_stop_time_frontend = None

                if scheduled_definition:
                    logger.info(f"Definisi jadwal ditemukan untuk service {service_name_systemd}: {scheduled_definition.get('session_name_original')}")
                    session_id_original = scheduled_definition.get('session_name_original', session_id_original)
                    video_name_to_use = scheduled_definition.get('video_file', video_name_to_use)
                    stream_key_to_use = scheduled_definition.get('stream_key', stream_key_to_use)
                    platform_to_use = scheduled_definition.get('platform', platform_to_use)
                    
                    recurrence = scheduled_definition.get('recurrence_type')
                    if recurrence == 'daily':
                        schedule_type_to_use = "daily_recurring_instance_recovered"
                        daily_start_time_str = scheduled_definition.get('start_time_of_day')
                        daily_stop_time_str = scheduled_definition.get('stop_time_of_day')
                        if daily_start_time_str and daily_stop_time_str:
                            start_h, start_m = map(int, daily_start_time_str.split(':'))
                            stop_h, stop_m = map(int, daily_stop_time_str.split(':'))
                            
                            duration_daily_minutes = (stop_h * 60 + stop_m) - (start_h * 60 + start_m)
                            if duration_daily_minutes <= 0: 
                                duration_daily_minutes += 24 * 60 
                            recovered_duration_minutes = duration_daily_minutes
                            recovered_stop_time_iso = (current_recovery_dt + timedelta(minutes=recovered_duration_minutes)).isoformat()
                            
                            intended_stop_today_dt = current_recovery_dt.replace(hour=stop_h, minute=stop_m, second=0, microsecond=0)
                            actual_scheduled_stop_dt = intended_stop_today_dt if current_recovery_dt <= intended_stop_today_dt else (intended_stop_today_dt + timedelta(days=1))
                            formatted_display_stop_time_frontend = actual_scheduled_stop_dt.astimezone(jakarta_tz).strftime('%d-%m-%Y Pukul %H:%M:%S')
                        else:
                            schedule_type_to_use = "manual_recovered_daily_data_missing"
                            
                    elif recurrence == 'one_time':
                        schedule_type_to_use = "scheduled_recovered"
                        original_start_iso = scheduled_definition.get('start_time_iso')
                        duration_mins_sched = scheduled_definition.get('duration_minutes', 0)
                        is_manual_stop_sched = scheduled_definition.get('is_manual_stop', duration_mins_sched == 0)

                        if not is_manual_stop_sched and duration_mins_sched > 0 and original_start_iso:
                            original_start_dt = datetime.fromisoformat(original_start_iso)
                            intended_stop_dt = original_start_dt + timedelta(minutes=duration_mins_sched)
                            recovered_stop_time_iso = intended_stop_dt.isoformat()
                            recovered_duration_minutes = duration_mins_sched
                            if current_recovery_dt >= intended_stop_dt:
                                schedule_type_to_use = "scheduled_recovered_overdue"
                            formatted_display_stop_time_frontend = intended_stop_dt.astimezone(jakarta_tz).strftime('%d-%m-%Y Pukul %H:%M:%S')
                        elif is_manual_stop_sched:
                             recovered_stop_time_iso = None
                             recovered_duration_minutes = 0
                        else:
                             schedule_type_to_use = "manual_recovered_onetime_data_missing"

                recovered_session_entry_for_json = {
                    "id": session_id_original,
                    "sanitized_service_id": sanitized_id_from_systemd_service, 
                    "video_name": video_name_to_use, 
                    "stream_key": stream_key_to_use, 
                    "platform": platform_to_use,
                    "status": "active", 
                    "start_time": current_recovery_time_iso,
                    "scheduleType": schedule_type_to_use,
                    "stopTime": recovered_stop_time_iso,
                    "duration_minutes": recovered_duration_minutes
                }
                
                all_sessions_data['active_sessions'] = add_or_update_session_in_list(
                    all_sessions_data.get('active_sessions', []), 
                    recovered_session_entry_for_json
                )
                needs_json_update = True
                
                active_sessions_list.append({
                    'id': recovered_session_entry_for_json['id'], 
                    'name': recovered_session_entry_for_json['id'], 
                    'startTime': recovered_session_entry_for_json['start_time'],
                    'platform': recovered_session_entry_for_json['platform'],
                    'video_name': recovered_session_entry_for_json['video_name'],
                    'stream_key': recovered_session_entry_for_json['stream_key'],
                    'stopTime': formatted_display_stop_time_frontend,
                    'scheduleType': recovered_session_entry_for_json['scheduleType'],
                    'sanitized_service_id': recovered_session_entry_for_json['sanitized_service_id']
                })
        
        if needs_json_update: 
            save_sessions(all_sessions_data)
        return sorted(active_sessions_list, key=lambda x: x.get('startTime', ''))
    except Exception as e: 
        logger.error(f"Error get_active_sessions_data: {e}", exc_info=True)
        return []

def get_inactive_sessions_data():
    """Get inactive sessions data"""
    try:
        data_sessions = load_sessions()
        inactive_list = []
        for item in data_sessions.get('inactive_sessions', []):
            item_details = {
                'id': item.get('id'),
                'sanitized_service_id': item.get('sanitized_service_id'),
                'video_name': item.get('video_name'),
                'stream_key': item.get('stream_key'),
                'platform': item.get('platform'),
                'status': item.get('status'),
                'start_time_original': item.get('start_time'),
                'stop_time': item.get('stop_time'),
                'duration_minutes_original': item.get('duration_minutes')
            }
            inactive_list.append(item_details)
        return sorted(inactive_list, key=lambda x: x.get('stop_time', ''), reverse=True)
    except Exception: 
        return []

# Scheduler functions
def start_scheduled_streaming(platform, stream_key, video_file, session_name_original, 
                              one_time_duration_minutes=0, recurrence_type='one_time', 
                              daily_start_time_str=None, daily_stop_time_str=None):
    """Start a scheduled streaming session"""
    logger.info(f"SCHEDULER EXEC: Mulai stream terjadwal: '{session_name_original}', Tipe: {recurrence_type}, Durasi One-Time: {one_time_duration_minutes} menit, Jadwal Harian: {daily_start_time_str}-{daily_stop_time_str}")
    
    video_path = os.path.abspath(os.path.join(VIDEOS_DIR, video_file))
    if not os.path.isfile(video_path):
        logger.error(f"SCHEDULER EXEC: Video {video_file} tidak ada untuk jadwal '{session_name_original}'. Jadwal mungkin perlu dibatalkan.")
        return

    platform_url = "rtmp://a.rtmp.youtube.com/live2" if platform == "YouTube" else "rtmps://live-api-s.facebook.com:443/rtmp"
    
    try:
        service_name_systemd, sanitized_service_id_part = create_service_file(session_name_original, video_path, platform_url, stream_key)
        
        # Start the systemd service
        result = subprocess.run(["systemctl", "start", service_name_systemd], check=True, capture_output=True, text=True)
        logger.info(f"SCHEDULER EXEC: Service {service_name_systemd} untuk jadwal '{session_name_original}' dimulai. Output: {result.stdout}")
        
        # Wait a moment and verify service is running
        import time
        time.sleep(2)
        
        verify_result = subprocess.run(["systemctl", "is-active", service_name_systemd], capture_output=True, text=True)
        if verify_result.stdout.strip() != 'active':
            logger.error(f"SCHEDULER EXEC: Service {service_name_systemd} tidak aktif setelah start. Status: {verify_result.stdout.strip()}")
            # Try to get service status for debugging
            status_result = subprocess.run(["systemctl", "status", service_name_systemd], capture_output=True, text=True)
            logger.error(f"SCHEDULER EXEC: Service status: {status_result.stdout}")
            return
        
        logger.info(f"SCHEDULER EXEC: Service {service_name_systemd} berhasil aktif dan berjalan.")
        
        current_start_time_iso = datetime.now(jakarta_tz).isoformat()
        s_data = load_sessions()

        active_session_stop_time_iso = None
        active_session_duration_minutes = 0
        active_schedule_type = "unknown"
        current_start_dt = datetime.fromisoformat(current_start_time_iso)

        if recurrence_type == 'daily' and daily_start_time_str and daily_stop_time_str:
            active_schedule_type = "daily_recurring_instance"
            start_h, start_m = map(int, daily_start_time_str.split(':'))
            stop_h, stop_m = map(int, daily_stop_time_str.split(':'))
            duration_for_this_instance = (stop_h * 60 + stop_m) - (start_h * 60 + start_m)
            if duration_for_this_instance <= 0: 
                duration_for_this_instance += 24 * 60
            active_session_duration_minutes = duration_for_this_instance
            active_session_stop_time_iso = (current_start_dt + timedelta(minutes=duration_for_this_instance)).isoformat()
        elif recurrence_type == 'one_time':
            active_schedule_type = "scheduled"
            active_session_duration_minutes = one_time_duration_minutes
            if one_time_duration_minutes > 0:
                active_session_stop_time_iso = (current_start_dt + timedelta(minutes=one_time_duration_minutes)).isoformat()
        else:
             active_schedule_type = "manual_from_schedule_error"

        new_active_session_entry = {
            "id": session_name_original,
            "sanitized_service_id": sanitized_service_id_part,
            "video_name": video_file, 
            "stream_key": stream_key, 
            "platform": platform,
            "status": "active", 
            "start_time": current_start_time_iso,
            "scheduleType": active_schedule_type,
            "stopTime": active_session_stop_time_iso,
            "duration_minutes": active_session_duration_minutes
        }
        s_data['active_sessions'] = add_or_update_session_in_list(
            s_data.get('active_sessions', []), new_active_session_entry
        )

        if recurrence_type == 'one_time':
            s_data['scheduled_sessions'] = [s for s in s_data.get('scheduled_sessions', []) if not (s.get('session_name_original') == session_name_original and s.get('recurrence_type', 'one_time') == 'one_time')]
        
        save_sessions(s_data)
        
        # Emit updates to frontend
        socketio.emit('sessions_update', get_active_sessions_data())
        socketio.emit('schedules_update', get_schedules_list_data())
        logger.info(f"SCHEDULER EXEC: Sesi terjadwal '{session_name_original}' (Tipe: {recurrence_type}) dimulai, update dikirim.")

    except subprocess.CalledProcessError as e:
        logger.error(f"SCHEDULER EXEC: Error start_scheduled_streaming untuk '{session_name_original}': {e}. Stderr: {e.stderr}. Stdout: {e.stdout}", exc_info=True)
    except Exception as e:
        logger.error(f"SCHEDULER EXEC: Error start_scheduled_streaming untuk '{session_name_original}': {e}", exc_info=True)

def stop_scheduled_streaming(session_name_original_or_active_id):
    """Stop a scheduled streaming session"""
    logger.info(f"SCHEDULER EXEC: Menghentikan stream (terjadwal/aktif): '{session_name_original_or_active_id}'")
    stop_streaming_session(session_name_original_or_active_id)

def get_schedules_list_data():
    """Get list of scheduled sessions"""
    sessions_data = load_sessions()
    schedule_list = []

    for sched_json in sessions_data.get('scheduled_sessions', []):
        try:
            session_name_original = sched_json.get('session_name_original', 'N/A')
            item_id = sched_json.get('id')
            platform = sched_json.get('platform', 'N/A')
            video_file = sched_json.get('video_file', 'N/A')
            recurrence = sched_json.get('recurrence_type', 'one_time')

            display_entry = {
                'id': item_id,
                'session_name_original': session_name_original,
                'video_file': video_file,
                'platform': platform,
                'stream_key': sched_json.get('stream_key', 'N/A'),
                'recurrence_type': recurrence,
                'sanitized_service_id': sched_json.get('sanitized_service_id')
            }

            if recurrence == 'daily':
                start_time_of_day = sched_json.get('start_time_of_day')
                stop_time_of_day = sched_json.get('stop_time_of_day')
                if not start_time_of_day or not stop_time_of_day:
                    logger.warning(f"Data jadwal harian tidak lengkap untuk {session_name_original}")
                    continue
                
                display_entry['start_time_display'] = f"Setiap hari pukul {start_time_of_day}"
                display_entry['stop_time_display'] = f"Berakhir pukul {stop_time_of_day}"
                display_entry['is_manual_stop'] = False
                display_entry['start_time_of_day'] = start_time_of_day
                display_entry['stop_time_of_day'] = stop_time_of_day
            
            elif recurrence == 'one_time':
                if not all(k in sched_json for k in ['start_time_iso', 'duration_minutes']):
                    logger.warning(f"Data jadwal one-time tidak lengkap untuk {session_name_original}")
                    continue
                
                start_dt_iso_val = sched_json['start_time_iso']
                start_dt = datetime.fromisoformat(start_dt_iso_val).astimezone(jakarta_tz)
                duration_mins = sched_json['duration_minutes']
                is_manual_stop_val = sched_json.get('is_manual_stop', duration_mins == 0)
                
                display_entry['start_time_iso'] = start_dt.isoformat()
                display_entry['start_time_display'] = start_dt.strftime('%d-%m-%Y %H:%M:%S')
                display_entry['stop_time_display'] = (start_dt + timedelta(minutes=duration_mins)).strftime('%d-%m-%Y %H:%M:%S') if not is_manual_stop_val else "Stop Manual"
                display_entry['is_manual_stop'] = is_manual_stop_val
                display_entry['duration_minutes'] = duration_mins
            else:
                logger.warning(f"Tipe recurrence tidak dikenal: {recurrence} untuk sesi {session_name_original}")
                continue
            
            schedule_list.append(display_entry)

        except Exception as e:
            logger.error(f"Error memproses item jadwal {sched_json.get('session_name_original')}: {e}", exc_info=True)
            
    try:
        return sorted(schedule_list, key=lambda x: (x['recurrence_type'] == 'daily', x.get('start_time_iso', x['session_name_original'])))
    except TypeError:
        return sorted(schedule_list, key=lambda x: x['session_name_original'])

def recover_schedules():
    """Recover scheduled sessions on startup"""
    try:
        s_data = load_sessions()
        now_jkt = datetime.now(jakarta_tz)
        valid_schedules_in_json = []

        logger.info("SCHEDULER RECOVERY: Memulai pemulihan jadwal...")
        for sched_def in s_data.get('scheduled_sessions', []):
            try:
                session_name_original = sched_def.get('session_name_original')
                schedule_definition_id = sched_def.get('id') 
                sanitized_service_id = sched_def.get('sanitized_service_id') 

                platform = sched_def.get('platform')
                stream_key = sched_def.get('stream_key')
                video_file = sched_def.get('video_file')
                recurrence = sched_def.get('recurrence_type', 'one_time')

                if not all([session_name_original, sanitized_service_id, platform, stream_key, video_file, schedule_definition_id]):
                    logger.warning(f"SCHEDULER RECOVERY: Skip jadwal '{session_name_original}' karena field dasar kurang.")
                    continue

                # Verify video file exists
                video_path = os.path.join(VIDEOS_DIR, video_file)
                if not os.path.exists(video_path):
                    logger.warning(f"SCHEDULER RECOVERY: Video file '{video_file}' tidak ditemukan untuk jadwal '{session_name_original}'. Skip.")
                    continue

                if recurrence == 'daily':
                    start_time_str = sched_def.get('start_time_of_day')
                    stop_time_str = sched_def.get('stop_time_of_day')
                    if not start_time_str or not stop_time_str:
                        logger.warning(f"SCHEDULER RECOVERY: Skip jadwal harian '{session_name_original}' karena field waktu harian kurang.")
                        continue
                    
                    start_h, start_m = map(int, start_time_str.split(':'))
                    stop_h, stop_m = map(int, stop_time_str.split(':'))

                    aps_start_job_id = f"daily-start-{sanitized_service_id}" 
                    aps_stop_job_id = f"daily-stop-{sanitized_service_id}"   

                    scheduler.add_job(start_scheduled_streaming, 'cron', hour=start_h, minute=start_m,
                                      args=[platform, stream_key, video_file, session_name_original, 0, 'daily', start_time_str, stop_time_str],
                                      id=aps_start_job_id, replace_existing=True, misfire_grace_time=3600)
                    logger.info(f"SCHEDULER RECOVERY: Recovered daily start job '{aps_start_job_id}' for '{session_name_original}' at {start_time_str}")

                    scheduler.add_job(stop_scheduled_streaming, 'cron', hour=stop_h, minute=stop_m,
                                      args=[session_name_original],
                                      id=aps_stop_job_id, replace_existing=True, misfire_grace_time=3600)
                    logger.info(f"SCHEDULER RECOVERY: Recovered daily stop job '{aps_stop_job_id}' for '{session_name_original}' at {stop_time_str}")
                    valid_schedules_in_json.append(sched_def)

                elif recurrence == 'one_time':
                    start_time_iso = sched_def.get('start_time_iso')
                    duration_minutes = sched_def.get('duration_minutes')
                    is_manual = sched_def.get('is_manual_stop', duration_minutes == 0)
                    aps_start_job_id = schedule_definition_id 

                    if not start_time_iso or duration_minutes is None:
                        logger.warning(f"SCHEDULER RECOVERY: Skip jadwal one-time '{session_name_original}' karena field waktu/durasi kurang.")
                        continue

                    start_dt = datetime.fromisoformat(start_time_iso).astimezone(now_jkt.tzinfo)

                    if start_dt > now_jkt:
                        scheduler.add_job(start_scheduled_streaming, 'date', run_date=start_dt,
                                          args=[platform, stream_key, video_file, session_name_original, duration_minutes, 'one_time', None, None],
                                          id=aps_start_job_id, replace_existing=True)
                        logger.info(f"SCHEDULER RECOVERY: Recovered one-time start job '{aps_start_job_id}' for '{session_name_original}' at {start_dt}")

                        if not is_manual:
                            stop_dt = start_dt + timedelta(minutes=duration_minutes)
                            if stop_dt > now_jkt:
                                aps_stop_job_id = f"onetime-stop-{sanitized_service_id}" 
                                scheduler.add_job(stop_scheduled_streaming, 'date', run_date=stop_dt,
                                                  args=[session_name_original],
                                                  id=aps_stop_job_id, replace_existing=True)
                                logger.info(f"SCHEDULER RECOVERY: Recovered one-time stop job '{aps_stop_job_id}' for '{session_name_original}' at {stop_dt}")
                        valid_schedules_in_json.append(sched_def)
                    else:
                        logger.info(f"SCHEDULER RECOVERY: Skip jadwal one-time '{session_name_original}' karena waktu sudah lewat.")
                else:
                     logger.warning(f"SCHEDULER RECOVERY: Tipe recurrence '{recurrence}' tidak dikenal untuk '{session_name_original}'.")

            except Exception as e:
                logger.error(f"SCHEDULER RECOVERY: Gagal memulihkan jadwal '{sched_def.get('session_name_original', 'UNKNOWN')}': {e}", exc_info=True)
        
        if len(s_data.get('scheduled_sessions', [])) != len(valid_schedules_in_json):
            s_data['scheduled_sessions'] = valid_schedules_in_json
            save_sessions(s_data)
            logger.info("SCHEDULER RECOVERY: File sessions.json diupdate dengan jadwal yang valid setelah pemulihan.")
        
        logger.info(f"SCHEDULER RECOVERY: Pemulihan jadwal selesai. Total jadwal valid: {len(valid_schedules_in_json)}")
        
    except Exception as e:
        logger.error(f"SCHEDULER RECOVERY: Error recovering schedules: {e}", exc_info=True)

# Recovery functions
def recover_orphaned_sessions():
    """Recovery function for orphaned sessions"""
    try:
        logger.info("RECOVERY: Starting orphaned session recovery...")
        
        sessions_data = load_sessions()
        active_sessions = sessions_data.get('active_sessions', [])
        
        if not active_sessions:
            logger.info("RECOVERY: No active sessions to check")
            return {'recovered': 0, 'moved_to_inactive': 0, 'total_active': 0}
        
        recovered_count = 0
        moved_to_inactive_count = 0
        
        for session_info in list(active_sessions):
            try:
                session_id = session_info.get('id')
                sanitized_service_id = session_info.get('sanitized_service_id')
                
                if not sanitized_service_id:
                    sanitized_service_id = sanitize_for_service_name(session_id)
                    session_info['sanitized_service_id'] = sanitized_service_id
                
                # Check if service is running
                service_name = f"stream-{sanitized_service_id}"
                result = subprocess.run(
                    ['systemctl', 'is-active', service_name],
                    capture_output=True,
                    text=True
                )
                is_running = result.stdout.strip() == 'active'
                
                if is_running:
                    logger.info(f"RECOVERY: Session {session_id} service is running - OK")
                    continue
                
                logger.warning(f"RECOVERY: Found orphaned session {session_id}")
                
                # Check if video file exists
                video_file = session_info.get('video_name')
                if not video_file:
                    logger.error(f"RECOVERY: Session {session_id} has no video file")
                    continue
                
                video_path = os.path.join(VIDEOS_DIR, video_file)
                if not os.path.exists(video_path):
                    logger.error(f"RECOVERY: Video file not found for session {session_id}: {video_file}")
                    # Move to inactive
                    session_info['stopped_at'] = datetime.now(jakarta_tz).isoformat()
                    session_info['stop_reason'] = 'Video file not found during recovery'
                    session_info['status'] = 'inactive'
                    sessions_data['inactive_sessions'].append(session_info)
                    sessions_data['active_sessions'] = [s for s in sessions_data['active_sessions'] if s.get('id') != session_id]
                    moved_to_inactive_count += 1
                    continue
                
                # Try to recover the session
                platform = session_info.get('platform')
                stream_key = session_info.get('stream_key')
                
                if not platform or not stream_key:
                    logger.error(f"RECOVERY: Session {session_id} missing platform or stream key")
                    continue
                
                # Get platform URL
                if platform == 'YouTube':
                    rtmp_url = 'rtmp://a.rtmp.youtube.com/live2'
                elif platform == 'Facebook':
                    rtmp_url = 'rtmps://live-api-s.facebook.com:443/rtmp'
                else:
                    logger.error(f"RECOVERY: Unsupported platform {platform} for session {session_id}")
                    continue
                
                # Recreate systemd service
                try:
                    service_name_systemd, _ = create_service_file(session_id, video_path, rtmp_url, stream_key)
                    subprocess.run(["systemctl", "start", service_name_systemd], check=True)
                    
                    logger.info(f"RECOVERY: Successfully recovered session {session_id}")
                    
                    # Update session info
                    session_info['recovered_at'] = datetime.now(jakarta_tz).isoformat()
                    session_info['recovery_count'] = session_info.get('recovery_count', 0) + 1
                    
                    recovered_count += 1
                except Exception as e:
                    logger.error(f"RECOVERY: Failed to recover session {session_id}: {e}")
                    # Move to inactive
                    session_info['stopped_at'] = datetime.now(jakarta_tz).isoformat()
                    session_info['stop_reason'] = 'Recovery failed'
                    session_info['status'] = 'inactive'
                    sessions_data['inactive_sessions'].append(session_info)
                    sessions_data['active_sessions'] = [s for s in sessions_data['active_sessions'] if s.get('id') != session_id]
                    moved_to_inactive_count += 1
                    
            except Exception as e:
                logger.error(f"RECOVERY: Error processing session {session_info.get('id', 'unknown')}: {e}")
        
        # Save updated sessions
        save_sessions(sessions_data)
        
        total_active = len(sessions_data.get('active_sessions', []))
        
        logger.info(f"RECOVERY: Completed - Recovered: {recovered_count}, Moved to inactive: {moved_to_inactive_count}, Total active: {total_active}")
        
        return {
            'recovered': recovered_count,
            'moved_to_inactive': moved_to_inactive_count,
            'total_active': total_active
        }
        
    except Exception as e:
        logger.error(f"RECOVERY: Error in recovery process: {e}")
        return {'recovered': 0, 'moved_to_inactive': 0, 'total_active': 0}

def check_systemd_sessions():
    """Check and sync systemd sessions with JSON data"""
    try:
        active_sysd_services = {ln.split()[0] for ln in subprocess.check_output(["systemctl", "list-units", "--type=service", "--state=running"], text=True).strip().split('\n') if "stream-" in ln}
        s_data = load_sessions()
        now_jakarta_dt = datetime.now(jakarta_tz)
        json_changed = False

        # Check scheduled sessions for overdue stops
        for sched_item in list(s_data.get('scheduled_sessions', [])): 
            if sched_item.get('recurrence_type', 'one_time') == 'daily': 
                continue
            if sched_item.get('is_manual_stop', False): 
                continue
            
            try:
                start_dt = datetime.fromisoformat(sched_item['start_time_iso'])
                dur_mins = sched_item.get('duration_minutes', 0)
                if dur_mins <= 0: 
                    continue 
                stop_dt = start_dt + timedelta(minutes=dur_mins)
                sanitized_service_id_from_schedule = sched_item.get('sanitized_service_id')
                if not sanitized_service_id_from_schedule:
                    logger.warning(f"CHECK_SYSTEMD: sanitized_service_id tidak ada di jadwal one-time {sched_item.get('session_name_original')}. Skip.")
                    continue
                serv_name = f"stream-{sanitized_service_id_from_schedule}.service"

                if now_jakarta_dt > stop_dt and serv_name in active_sysd_services:
                    logger.info(f"CHECK_SYSTEMD: Menghentikan sesi terjadwal (one-time) yang terlewat waktu: {sched_item['session_name_original']}")
                    stop_streaming_session(sched_item['session_name_original']) 
                    json_changed = True 
            except Exception as e_sched_check:
                 logger.error(f"CHECK_SYSTEMD: Error memeriksa jadwal one-time {sched_item.get('session_name_original')}: {e_sched_check}")
        
        # Check active sessions for overdue stops
        for active_session_check in list(s_data.get('active_sessions', [])):
            stop_time_iso = active_session_check.get('stopTime')
            session_id_to_check = active_session_check.get('id')
            sanitized_id_service_check = active_session_check.get('sanitized_service_id')

            if not session_id_to_check or not sanitized_id_service_check:
               logger.warning(f"CHECK_SYSTEMD: Melewati sesi aktif {session_id_to_check or 'UNKNOWN'} karena ID atau sanitized_service_id kurang.")
               continue

            service_name_check = f"stream-{sanitized_id_service_check}.service"

            if stop_time_iso and service_name_check in active_sysd_services:
                try:
                    stop_time_dt = datetime.fromisoformat(stop_time_iso)
                    if stop_time_dt.tzinfo is None:
                        stop_time_dt = jakarta_tz.localize(stop_time_dt)
                    else:
                        stop_time_dt = stop_time_dt.astimezone(jakarta_tz)

                    if now_jakarta_dt > stop_time_dt:
                        logger.info(f"CHECK_SYSTEMD: Sesi aktif '{session_id_to_check}' telah melewati waktu berhenti. Menghentikan...")
                        stop_streaming_session(session_id_to_check)
                        json_changed = True
                except ValueError:
                    logger.warning(f"CHECK_SYSTEMD: Format stopTime tidak valid untuk sesi '{session_id_to_check}'.")
                except Exception as e_fallback_stop:
                    logger.error(f"CHECK_SYSTEMD: Error menghentikan sesi '{session_id_to_check}': {e_fallback_stop}", exc_info=True)

        # Check for orphaned sessions in JSON
        for active_json_session in list(s_data.get('active_sessions', [])): 
            san_id_active_service = active_json_session.get('sanitized_service_id')
            if not san_id_active_service : 
                logger.warning(f"CHECK_SYSTEMD: Sesi aktif {active_json_session.get('id')} tidak memiliki sanitized_service_id. Skip.")
                continue 
            serv_name_active = f"stream-{san_id_active_service}.service"

            if serv_name_active not in active_sysd_services:
                is_recently_stopped_by_scheduler = any(
                    s['id'] == active_json_session.get('id') and 
                    s.get('status') == 'inactive' and
                    (datetime.now(jakarta_tz) - datetime.fromisoformat(s.get('stop_time')).astimezone(jakarta_tz) < timedelta(minutes=2))
                    for s in s_data.get('inactive_sessions', [])
                )
                if is_recently_stopped_by_scheduler:
                    logger.info(f"CHECK_SYSTEMD: Sesi {active_json_session.get('id')} sepertinya baru dihentikan oleh scheduler. Skip pemindahan otomatis.")
                    continue

                logger.info(f"CHECK_SYSTEMD: Sesi {active_json_session.get('id','N/A')} tidak aktif di systemd. Memindahkan ke inactive.")
                active_json_session['status'] = 'inactive'
                active_json_session['stop_time'] = now_jakarta_dt.isoformat()
                s_data.setdefault('inactive_sessions', []).append(active_json_session)
                s_data['active_sessions'] = [s for s in s_data['active_sessions'] if s.get('id') != active_json_session.get('id')]
                json_changed = True
        
        if json_changed: 
            save_sessions(s_data) 
            # Emit updates if socketio is available
            socketio.emit('sessions_update', get_active_sessions_data())
            socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
    except Exception as e: 
        logger.error(f"CHECK_SYSTEMD: Error: {e}", exc_info=True)

def trial_reset():
    """Reset application for trial mode"""
    if not TRIAL_MODE_ENABLED:
        logger.info("Mode trial tidak aktif, proses reset dilewati.")
        return

    logger.info("MODE TRIAL: Memulai proses reset aplikasi...")
    try:
        s_data = load_sessions()
        active_sessions_copy = list(s_data.get('active_sessions', []))
        
        logger.info(f"MODE TRIAL: Menghentikan dan menghapus {len(active_sessions_copy)} sesi aktif...")
        for item in active_sessions_copy:
            sanitized_id_service = item.get('sanitized_service_id')
            if not sanitized_id_service:
                sanitized_id_service = sanitize_for_service_name(item.get('id', f'unknown_id_{datetime.now().timestamp()}'))
            
            service_name_to_stop = f"stream-{sanitized_id_service}.service"
            try:
                subprocess.run(["systemctl", "stop", service_name_to_stop], check=False, timeout=15)
                service_path_to_stop = os.path.join(SERVICE_DIR, service_name_to_stop)
                if os.path.exists(service_path_to_stop):
                    os.remove(service_path_to_stop)
                logger.info(f"MODE TRIAL: Service {service_name_to_stop} dihentikan dan dihapus.")
                
                item['status'] = 'inactive'
                item['stop_time'] = datetime.now(jakarta_tz).isoformat() 
                item['duration_minutes'] = item.get('duration_minutes', 0)
                s_data['inactive_sessions'] = add_or_update_session_in_list(
                    s_data.get('inactive_sessions', []), item
                )
            except Exception as e_stop:
                logger.error(f"MODE TRIAL: Gagal menghentikan/menghapus service {service_name_to_stop}: {e_stop}")
        s_data['active_sessions'] = []

        try:
            subprocess.run(["systemctl", "daemon-reload"], check=False, timeout=10)
        except Exception as e_reload:
            logger.error(f"MODE TRIAL: Gagal daemon-reload: {e_reload}")

        logger.info(f"MODE TRIAL: Menghapus semua ({len(s_data.get('scheduled_sessions', []))}) jadwal...")
        scheduled_sessions_copy = list(s_data.get('scheduled_sessions', []))
        for sched_item in scheduled_sessions_copy:
            sanitized_id = sched_item.get('sanitized_service_id')
            schedule_def_id = sched_item.get('id')
            recurrence = sched_item.get('recurrence_type')

            if not sanitized_id or not schedule_def_id:
                logger.warning(f"MODE TRIAL: Melewati item jadwal karena sanitized_id atau schedule_def_id kurang: {sched_item}")
                continue

            # Remove scheduler jobs
            try:
                if recurrence == 'daily':
                    try: scheduler.remove_job(f"daily-start-{sanitized_id}")
                    except: pass
                    try: scheduler.remove_job(f"daily-stop-{sanitized_id}")
                    except: pass
                elif recurrence == 'one_time':
                    try: scheduler.remove_job(schedule_def_id)
                    except: pass
                    if not sched_item.get('is_manual_stop', sched_item.get('duration_minutes', 0) == 0):
                        try: scheduler.remove_job(f"onetime-stop-{sanitized_id}")
                        except: pass
            except:
                pass
        s_data['scheduled_sessions'] = []

        logger.info(f"MODE TRIAL: Menghapus semua file video...")
        videos_to_delete = get_videos_list()
        for video_file in videos_to_delete:
            try:
                os.remove(os.path.join(VIDEOS_DIR, video_file))
                logger.info(f"MODE TRIAL: File video {video_file} dihapus.")
            except Exception as e_vid_del:
                logger.error(f"MODE TRIAL: Gagal menghapus file video {video_file}: {e_vid_del}")
        
        save_sessions(s_data)
        
        # Emit updates if socketio is available
        socketio.emit('sessions_update', get_active_sessions_data())
        socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
        socketio.emit('schedules_update', get_schedules_list_data())
        socketio.emit('videos_update', get_videos_list())
        socketio.emit('trial_reset_notification', {
            'message': 'Aplikasi telah direset karena mode trial. Semua sesi dan video telah dihapus.'
        })
        socketio.emit('trial_status_update', {
            'is_trial': TRIAL_MODE_ENABLED,
            'message': 'Mode Trial Aktif - Reset setiap {} jam.'.format(TRIAL_RESET_HOURS) if TRIAL_MODE_ENABLED else ''
        })

        logger.info("MODE TRIAL: Proses reset aplikasi selesai.")

    except Exception as e:
        logger.error(f"MODE TRIAL: Error besar selama proses reset: {e}", exc_info=True)

def perform_startup_recovery():
    """Perform complete recovery on startup"""
    logger.info("=== STARTING STARTUP RECOVERY ===")
    
    try:
        # Recovery orphaned sessions
        recover_orphaned_sessions()
        
        logger.info("=== STARTUP RECOVERY COMPLETED ===")
        
    except Exception as e:
        logger.error(f"STARTUP RECOVERY: Error during recovery: {e}")

# Routes
@app.route('/')
def index():
    if not is_customer_logged_in():
        # Check if any customer users exist
        users = load_users()
        if not users:
            # No users exist, redirect to register
            return redirect(url_for('customer_register'))
        else:
            # Users exist, redirect to login
            return redirect(url_for('customer_login'))
    return render_template('index.html')

@app.route('/login')
def customer_login():
    if is_customer_logged_in():
        return redirect(url_for('index'))
    
    # Check if any users exist
    users = load_users()
    if not users:
        # No users exist, redirect to register
        return redirect(url_for('customer_register'))
    
    return render_template('customer_login.html')

@app.route('/register')
def customer_register():
    if is_customer_logged_in():
        return redirect(url_for('index'))
    
    # Check if any users exist (only allow one user unless trial mode)
    users = load_users()
    if not TRIAL_MODE_ENABLED and users:
        return render_template('registration_closed.html')
    
    return render_template('customer_register.html')

@app.route('/logout')
def customer_logout():
    session.clear()
    return redirect(url_for('customer_login'))

# Admin routes
@app.route('/admin/login')
def admin_login():
    if is_admin_logged_in():
        return redirect(url_for('admin_index'))
    return render_template('admin_login.html')

@app.route('/admin')
@admin_required
def admin_index():
    try:
        # Get stats
        sessions_data = load_sessions()
        users_data = load_users()
        domain_config = load_domain_config()
        video_files = get_videos_list()
        
        stats = {
            'total_users': len(users_data),
            'active_sessions': len(sessions_data.get('active_sessions', [])),
            'inactive_sessions': len(sessions_data.get('inactive_sessions', [])),
            'scheduled_sessions': len(sessions_data.get('scheduled_sessions', [])),
            'total_videos': len(video_files)
        }
        
        return render_template('admin_index.html', 
                             stats=stats, 
                             sessions=sessions_data,
                             domain_config=domain_config)
    except Exception as e:
        logger.error(f"Error rendering admin index: {e}", exc_info=True)
        return f"Internal Server Error: {str(e)}", 500

@app.route('/admin/users')
@admin_required
def admin_users():
    try:
        users_data = load_users()
        return render_template('admin_users.html', users=users_data)
    except Exception as e:
        logger.error(f"Error rendering admin users: {e}", exc_info=True)
        return f"Internal Server Error: {str(e)}", 500

@app.route('/admin/domain')
@admin_required
def admin_domain():
    try:
        domain_config = load_domain_config()
        return render_template('admin_domain.html', domain_config=domain_config)
    except Exception as e:
        logger.error(f"Error rendering admin domain: {e}", exc_info=True)
        return f"Internal Server Error: {str(e)}", 500

@app.route('/admin/recovery')
@admin_required
def admin_recovery():
    try:
        return render_template('admin_recovery.html')
    except Exception as e:
        logger.error(f"Error rendering admin recovery: {e}", exc_info=True)
        return f"Internal Server Error: {str(e)}", 500

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

# SocketIO Events
@socketio.on('connect')
def handle_connect():
    logger.info("Client connected")
    if not is_customer_logged_in() and not is_admin_logged_in(): 
        logger.warning("Unauthorized client connection rejected.")
        return False 
    
    # Send initial data to client
    socketio.emit('videos_update', get_videos_list())
    socketio.emit('sessions_update', get_active_sessions_data())
    socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
    socketio.emit('schedules_update', get_schedules_list_data())
    
    # Send domain configuration
    domain_config = load_domain_config()
    socketio.emit('domain_config_update', {
        'current_url': get_current_url(),
        'config': domain_config
    })
    
    # Send trial status
    if TRIAL_MODE_ENABLED:
        socketio.emit('trial_status_update', {
            'is_trial': True,
            'message': f"Mode Trial Aktif, Live, Schedule Live dan Video akan terhapus tiap {TRIAL_RESET_HOURS} jam"
        })
    else:
        socketio.emit('trial_status_update', {'is_trial': False, 'message': ''})

@socketio.on('disconnect')
def handle_disconnect():
    logger.info("Client disconnected")

# API Routes
@app.route('/api/customer/login', methods=['POST'])
def customer_login_api():
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')
        
        users = load_users()
        if username in users and users[username] == password:
            session['customer_logged_in'] = True
            session['username'] = username
            session.permanent = True
            return jsonify({'success': True, 'message': 'Login successful'})
        else:
            return jsonify({'success': False, 'message': 'Invalid credentials'}), 401
            
    except Exception as e:
        logger.error(f"Customer login error: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500

@app.route('/api/customer/register', methods=['POST'])
def customer_register_api():
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return jsonify({'success': False, 'message': 'Username and password required'}), 400
        
        users = load_users()
        
        # Check trial mode and user limit
        if not TRIAL_MODE_ENABLED and len(users) >= 1:
            return jsonify({'success': False, 'message': 'Registration closed (user limit reached)'}), 403
        
        if username in users:
            return jsonify({'success': False, 'message': 'Username already exists'}), 400
        
        users[username] = password
        save_users(users)
        
        session['customer_logged_in'] = True
        session['username'] = username
        session.permanent = True
        
        return jsonify({'success': True, 'message': 'Registration successful'})
        
    except Exception as e:
        logger.error(f"Customer register error: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500

@app.route('/api/admin/login', methods=['POST'])
def admin_login_api():
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return jsonify({'success': True, 'message': 'Admin login successful'})
        else:
            return jsonify({'success': False, 'message': 'Invalid admin credentials'})
        
    except Exception as e:
        logger.error(f"Admin login error: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'Login failed'})

@app.route('/api/admin/users/<username>', methods=['DELETE'])
@admin_required
def api_delete_user(username):
    try:
        users = load_users()
        
        if username not in users:
            return jsonify({'success': False, 'message': 'User not found'})
        
        del users[username]
        
        if save_users(users):
            return jsonify({'success': True, 'message': 'User deleted successfully'})
        else:
            return jsonify({'success': False, 'message': 'Failed to delete user'})
        
    except Exception as e:
        logger.error(f"Delete user error: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Failed to delete user: {str(e)}'})

@app.route('/api/check-session', methods=['GET'])
@login_required
def check_session_api(): 
    return jsonify({'logged_in': True, 'user': session.get('username')})

# Video API
@app.route('/api/videos', methods=['GET'])
@login_required
def list_videos_api():
    try: 
        return jsonify(get_videos_list())
    except Exception as e: 
        logger.error(f"Error API /api/videos: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Gagal ambil daftar video.'}), 500

def extract_drive_id(url_or_id):
    """Extract Google Drive file ID from URL or return ID if already valid"""
    if not url_or_id:
        return None
    
    # If it's a Google Drive URL, extract ID
    if "drive.google.com" in url_or_id:
        import re
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
    import re
    if re.match(r'^[a-zA-Z0-9_-]{20,}$', url_or_id):
        return url_or_id
    
    return None

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

            socketio.emit('videos_update', get_videos_list())
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
        socketio.emit('videos_update', get_videos_list())
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
        socketio.emit('videos_update', get_videos_list())
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
        import re
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
        socketio.emit('videos_update', get_videos_list())
        return jsonify({'status': 'success', 'message': f'Video diubah ke "{os.path.basename(new_p)}"'})
    except Exception as e: 
        logger.error("Error rename video", exc_info=True)
        return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

@app.route('/api/disk-usage', methods=['GET'])
@login_required
def disk_usage_api(): 
    try:
        import shutil
        t, u, f = shutil.disk_usage(VIDEOS_DIR)
        tg, ug, fg = t/(2**30), u/(2**30), f/(2**30)
        pu = (u/t)*100 if t > 0 else 0
        stat = 'full' if pu > 95 else 'almost_full' if pu > 80 else 'normal'
        return jsonify({'status': stat, 'total': round(tg, 2), 'used': round(ug, 2), 'free': round(fg, 2), 'percent_used': round(pu, 2)})
    except Exception as e: 
        logger.error(f"Error disk usage: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

from flask import send_from_directory

@app.route('/videos/<filename>')
@login_required
def serve_video(filename):
    return send_from_directory(VIDEOS_DIR, filename)

# Streaming API
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
        
        socketio.emit('sessions_update', get_active_sessions_data())
        socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
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
            socketio.emit('sessions_update', get_active_sessions_data())
            socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
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
        
        socketio.emit('sessions_update', get_active_sessions_data())
        socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
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
        socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
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
        socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
        return jsonify({"status": "success", "message": f"Detail sesi tidak aktif '{session_id_to_edit}' berhasil diperbarui."})
    except Exception as e: 
        req_data_edit_sess = request.get_json(silent=True) or {}
        session_id_err_edit_sess = req_data_edit_sess.get('session_name_original', req_data_edit_sess.get('id', 'N/A'))
        logger.error(f"Error edit sesi tidak aktif '{session_id_err_edit_sess}'", exc_info=True)
        return jsonify({'status': 'error', 'message': f'Kesalahan Server Internal: {str(e)}'}), 500

# Sessions API
@app.route('/api/sessions', methods=['GET'])
@login_required
def list_sessions_api():
    try:
        return jsonify(get_active_sessions_data())
    except Exception as e:
        logger.error(f"Error API /api/sessions: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Gagal ambil sesi aktif.'}), 500

@app.route('/api/inactive-sessions', methods=['GET'])
@login_required
def list_inactive_sessions_api():
    try: 
        return jsonify({"inactive_sessions": get_inactive_sessions_data()})
    except Exception as e: 
        logger.error(f"Error API /api/inactive-sessions: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Gagal ambil sesi tidak aktif.'}), 500

@app.route('/api/inactive-sessions/delete-all', methods=['POST'])
@login_required
def delete_all_inactive_sessions_api():
    try:
        s_data = load_sessions()
        deleted_count = len(s_data.get('inactive_sessions', []))
        
        if deleted_count == 0:
            return jsonify({'status': 'success', 'message': 'Tidak ada sesi nonaktif untuk dihapus.', 'deleted_count': 0}), 200

        s_data['inactive_sessions'] = []
        save_sessions(s_data)
        
        socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            
        logger.info(f"Berhasil menghapus semua ({deleted_count}) sesi tidak aktif.")
        return jsonify({'status': 'success', 'message': f'Berhasil menghapus {deleted_count} sesi tidak aktif.', 'deleted_count': deleted_count}), 200
    except Exception as e:
        logger.error("Error di API delete_all_inactive_sessions", exc_info=True)
        return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

# Scheduler API
@app.route('/api/schedule', methods=['POST'])
@login_required
def schedule_streaming_api():
    try:
        data = request.json
        logger.info(f"SCHEDULER API: Menerima data penjadwalan: {data}")

        recurrence_type = data.get('recurrence_type', 'one_time')
        session_name_original = data.get('session_name_original', '').strip()
        platform = data.get('platform', 'YouTube')
        stream_key = data.get('stream_key', '').strip()
        video_file = data.get('video_file')

        if not all([session_name_original, platform, stream_key, video_file]):
            return jsonify({'status': 'error', 'message': 'Nama sesi, platform, stream key, dan video file wajib diisi.'}), 400
        if platform not in ["YouTube", "Facebook"]:
             return jsonify({'status': 'error', 'message': 'Platform tidak valid.'}), 400
        if not os.path.isfile(os.path.join(VIDEOS_DIR, video_file)):
            return jsonify({'status': 'error', 'message': f"File video '{video_file}' tidak ditemukan."}), 404

        sanitized_service_id_part = sanitize_for_service_name(session_name_original)
        if not sanitized_service_id_part:
            return jsonify({'status': 'error', 'message': 'Nama sesi tidak valid setelah sanitasi untuk ID layanan.'}), 400

        s_data = load_sessions()
        idx_to_remove = -1
        for i, sched in enumerate(s_data.get('scheduled_sessions', [])):
            if sched.get('session_name_original') == session_name_original:
                logger.info(f"SCHEDULER API: Menemukan jadwal yang sudah ada dengan nama sesi asli '{session_name_original}', akan menggantinya.")
                old_sanitized_service_id = sched.get('sanitized_service_id')
                old_schedule_def_id = sched.get('id')
                try:
                    if sched.get('recurrence_type') == 'daily':
                        try:
                            scheduler.remove_job(f"daily-start-{old_sanitized_service_id}")
                            logger.info(f"SCHEDULER API: Removed old daily start job: daily-start-{old_sanitized_service_id}")
                        except:
                            pass
                        try:
                            scheduler.remove_job(f"daily-stop-{old_sanitized_service_id}")
                            logger.info(f"SCHEDULER API: Removed old daily stop job: daily-stop-{old_sanitized_service_id}")
                        except:
                            pass
                    else:
                        try:
                            scheduler.remove_job(old_schedule_def_id)
                            logger.info(f"SCHEDULER API: Removed old one-time start job: {old_schedule_def_id}")
                        except:
                            pass
                        if not sched.get('is_manual_stop', sched.get('duration_minutes', 0) == 0):
                            try:
                                scheduler.remove_job(f"onetime-stop-{old_sanitized_service_id}")
                                logger.info(f"SCHEDULER API: Removed old one-time stop job: onetime-stop-{old_sanitized_service_id}")
                            except:
                                pass
                    logger.info(f"SCHEDULER API: Job scheduler lama untuk '{session_name_original}' berhasil dihapus.")
                except Exception as e_remove_old_job:
                    logger.info(f"SCHEDULER API: Tidak ada job scheduler lama untuk '{session_name_original}' atau error saat menghapus: {e_remove_old_job}")
                idx_to_remove = i
                break
        if idx_to_remove != -1:
            del s_data['scheduled_sessions'][idx_to_remove]
        
        s_data['inactive_sessions'] = [s for s in s_data.get('inactive_sessions', []) if s.get('id') != session_name_original]

        msg = ""
        schedule_definition_id = ""
        sched_entry = {
            'session_name_original': session_name_original,
            'sanitized_service_id': sanitized_service_id_part,
            'platform': platform, 
            'stream_key': stream_key, 
            'video_file': video_file,
            'recurrence_type': recurrence_type
        }

        if recurrence_type == 'daily':
            start_time_of_day = data.get('start_time_of_day') 
            stop_time_of_day = data.get('stop_time_of_day')   

            if not start_time_of_day or not stop_time_of_day:
                return jsonify({'status': 'error', 'message': "Untuk jadwal harian, 'start_time_of_day' dan 'stop_time_of_day' (format HH:MM) wajib diisi."}), 400
            try:
                start_hour, start_minute = map(int, start_time_of_day.split(':'))
                stop_hour, stop_minute = map(int, stop_time_of_day.split(':'))
                if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59 and 0 <= stop_hour <= 23 and 0 <= stop_minute <= 59):
                    raise ValueError("Jam atau menit di luar rentang valid.")
            except ValueError as ve:
                return jsonify({'status': 'error', 'message': f"Format waktu harian tidak valid: {ve}. Gunakan HH:MM."}), 400

            schedule_definition_id = f"daily-{sanitized_service_id_part}"
            sched_entry.update({
                'id': schedule_definition_id,
                'start_time_of_day': start_time_of_day,
                'stop_time_of_day': stop_time_of_day
            })
            
            aps_start_job_id = f"daily-start-{sanitized_service_id_part}"
            aps_stop_job_id = f"daily-stop-{sanitized_service_id_part}"

            scheduler.add_job(start_scheduled_streaming, 'cron', hour=start_hour, minute=start_minute,
                              args=[platform, stream_key, video_file, session_name_original, 0, 'daily', start_time_of_day, stop_time_of_day],
                              id=aps_start_job_id, replace_existing=True, misfire_grace_time=3600)
            logger.info(f"SCHEDULER API: Jadwal harian START '{aps_start_job_id}' untuk '{session_name_original}' ditambahkan: {start_time_of_day}")

            scheduler.add_job(stop_scheduled_streaming, 'cron', hour=stop_hour, minute=stop_minute,
                              args=[session_name_original],
                              id=aps_stop_job_id, replace_existing=True, misfire_grace_time=3600)
            logger.info(f"SCHEDULER API: Jadwal harian STOP '{aps_stop_job_id}' untuk '{session_name_original}' ditambahkan: {stop_time_of_day}")
            
            msg = f"Sesi harian '{session_name_original}' dijadwalkan setiap hari dari {start_time_of_day} sampai {stop_time_of_day}."

        elif recurrence_type == 'one_time':
            start_time_str = data.get('start_time') 
            duration_input = data.get('duration', 0) 

            if not start_time_str:
                return jsonify({'status': 'error', 'message': "Untuk jadwal sekali jalan, 'start_time' (YYYY-MM-DDTHH:MM) wajib diisi."}), 400
            try:
                naive_start_dt = datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M')
                start_dt = jakarta_tz.localize(naive_start_dt)
                if start_dt <= datetime.now(jakarta_tz):
                    return jsonify({'status': 'error', 'message': "Waktu mulai jadwal sekali jalan harus di masa depan."}), 400
            except ValueError:
                 return jsonify({'status': 'error', 'message': "Format 'start_time' untuk jadwal sekali jalan tidak valid. Gunakan YYYY-MM-DDTHH:MM."}), 400

            duration_minutes = int(float(duration_input) * 60) if float(duration_input) >= 0 else 0
            is_manual_stop = (duration_minutes == 0)
            schedule_definition_id = f"onetime-{sanitized_service_id_part}" 

            sched_entry.update({
                'id': schedule_definition_id,
                'start_time_iso': start_dt.isoformat(), 
                'duration_minutes': duration_minutes,
                'is_manual_stop': is_manual_stop
            })
            
            aps_start_job_id = schedule_definition_id
            scheduler.add_job(start_scheduled_streaming, 'date', run_date=start_dt,
                              args=[platform, stream_key, video_file, session_name_original, duration_minutes, 'one_time', None, None],
                              id=aps_start_job_id, replace_existing=True)
            logger.info(f"SCHEDULER API: Jadwal sekali jalan START '{aps_start_job_id}' untuk '{session_name_original}' ditambahkan pada {start_dt}")

            if not is_manual_stop:
                stop_dt = start_dt + timedelta(minutes=duration_minutes)
                aps_stop_job_id = f"onetime-stop-{sanitized_service_id_part}"
                scheduler.add_job(stop_scheduled_streaming, 'date', run_date=stop_dt,
                                  args=[session_name_original], id=aps_stop_job_id, replace_existing=True)
                logger.info(f"SCHEDULER API: Jadwal sekali jalan STOP '{aps_stop_job_id}' untuk '{session_name_original}' ditambahkan pada {stop_dt}")
            
            msg = f'Sesi "{session_name_original}" dijadwalkan sekali pada {start_dt.strftime("%d-%m-%Y %H:%M:%S")}'
            msg += f' selama {duration_minutes} menit.' if not is_manual_stop else ' hingga dihentikan manual.'
        
        else:
            return jsonify({'status': 'error', 'message': f"Tipe recurrence '{recurrence_type}' tidak dikenal."}), 400

        s_data.setdefault('scheduled_sessions', []).append(sched_entry)
        save_sessions(s_data)
        
        socketio.emit('schedules_update', get_schedules_list_data())
        socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
        
        logger.info(f"SCHEDULER API: Berhasil membuat jadwal untuk '{session_name_original}'. Total jobs aktif: {len(scheduler.get_jobs())}")
        return jsonify({'status': 'success', 'message': msg})

    except (KeyError, ValueError) as e:
        logger.error(f"SCHEDULER API: Input tidak valid untuk penjadwalan: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': f"Input tidak valid: {str(e)}"}), 400
    except Exception as e:
        req_data_sched = request.get_json(silent=True) or {}
        session_name_err_sched = req_data_sched.get('session_name_original', 'N/A')
        logger.error(f"SCHEDULER API: Error server saat menjadwalkan sesi '{session_name_err_sched}'", exc_info=True)
        return jsonify({'status': 'error', 'message': f'Kesalahan Server Internal: {str(e)}'}), 500

@app.route('/api/schedule-list', methods=['GET'])
@login_required
def get_schedules_api():
    try: 
        return jsonify(get_schedules_list_data())
    except Exception as e: 
        logger.error(f"Error API /api/schedule-list: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Gagal ambil daftar jadwal.'}), 500

@app.route('/api/cancel-schedule', methods=['POST'])
@login_required
def cancel_schedule_api():
    try:
        data = request.json
        schedule_definition_id_to_cancel = data.get('id')
        if not schedule_definition_id_to_cancel:
            return jsonify({'status': 'error', 'message': 'ID definisi jadwal diperlukan.'}), 400

        s_data = load_sessions()
        schedule_to_cancel_obj = None
        idx_to_remove_json = -1

        for i, sched in enumerate(s_data.get('scheduled_sessions', [])):
            if sched.get('id') == schedule_definition_id_to_cancel:
                schedule_to_cancel_obj = sched
                idx_to_remove_json = i
                break
        
        if not schedule_to_cancel_obj:
            return jsonify({'status': 'error', 'message': f"Definisi jadwal dengan ID '{schedule_definition_id_to_cancel}' tidak ditemukan."}), 404

        removed_scheduler_jobs_count = 0
        sanitized_service_id_from_def = schedule_to_cancel_obj.get('sanitized_service_id')
        session_display_name = schedule_to_cancel_obj.get('session_name_original', schedule_definition_id_to_cancel)

        if not sanitized_service_id_from_def:
            logger.error(f"Tidak dapat membatalkan job scheduler untuk def ID '{schedule_definition_id_to_cancel}' karena sanitized_service_id tidak ada.")
        else:
            if schedule_to_cancel_obj.get('recurrence_type') == 'daily':
                aps_start_job_id = f"daily-start-{sanitized_service_id_from_def}"
                aps_stop_job_id = f"daily-stop-{sanitized_service_id_from_def}"
                try: 
                    scheduler.remove_job(aps_start_job_id)
                    removed_scheduler_jobs_count += 1
                    logger.info(f"Job harian START '{aps_start_job_id}' dihapus.")
                except Exception as e: 
                    logger.info(f"Gagal hapus job harian START '{aps_start_job_id}': {e}")
                try: 
                    scheduler.remove_job(aps_stop_job_id)
                    removed_scheduler_jobs_count += 1
                    logger.info(f"Job harian STOP '{aps_stop_job_id}' dihapus.")
                except Exception as e: 
                    logger.info(f"Gagal hapus job harian STOP '{aps_stop_job_id}': {e}")
            
            elif schedule_to_cancel_obj.get('recurrence_type', 'one_time') == 'one_time':
                aps_start_job_id = schedule_definition_id_to_cancel 
                try: 
                    scheduler.remove_job(aps_start_job_id)
                    removed_scheduler_jobs_count += 1
                    logger.info(f"Job sekali jalan START '{aps_start_job_id}' dihapus.")
                except Exception as e: 
                    logger.info(f"Gagal hapus job sekali jalan START '{aps_start_job_id}': {e}")

                if not schedule_to_cancel_obj.get('is_manual_stop', schedule_to_cancel_obj.get('duration_minutes', 0) == 0):
                    aps_stop_job_id = f"onetime-stop-{sanitized_service_id_from_def}"
                    try: 
                        scheduler.remove_job(aps_stop_job_id)
                        removed_scheduler_jobs_count += 1
                        logger.info(f"Job sekali jalan STOP '{aps_stop_job_id}' dihapus.")
                    except Exception as e: 
                        logger.info(f"Gagal hapus job sekali jalan STOP '{aps_stop_job_id}': {e}")
        
        if idx_to_remove_json != -1:
            del s_data['scheduled_sessions'][idx_to_remove_json]
            save_sessions(s_data)
            logger.info(f"Definisi jadwal '{session_display_name}' (ID: {schedule_definition_id_to_cancel}) dihapus dari sessions.json.")
        
        socketio.emit('schedules_update', get_schedules_list_data())
        
        return jsonify({
            'status': 'success',
            'message': f"Definisi jadwal '{session_display_name}' dibatalkan. {removed_scheduler_jobs_count} job dari scheduler berhasil dihapus."
        })
    except Exception as e:
        req_data_cancel = request.get_json(silent=True) or {}
        def_id_err = req_data_cancel.get('id', 'N/A')
        logger.error(f"Error saat membatalkan jadwal, ID definisi dari request: {def_id_err}", exc_info=True)
        return jsonify({'status': 'error', 'message': f'Kesalahan Server Internal: {str(e)}'}), 500

# Recovery API
@app.route('/api/recovery/manual', methods=['POST'])
@login_required
def manual_recovery_api():
    try:
        logger.info("RECOVERY: Manual recovery triggered")
        
        # Run recovery
        recovery_result = recover_orphaned_sessions()
        
        # Emit updates to frontend
        socketio.emit('sessions_update', get_active_sessions_data())
        
        return jsonify({
            'success': True,
            'message': 'Manual recovery completed',
            'recovery_result': recovery_result
        })
        
    except Exception as e:
        logger.error(f"Manual recovery error: {e}")
        return jsonify({'success': False, 'message': f'Recovery failed: {str(e)}'})

@app.route('/api/recovery/status', methods=['GET'])
@login_required
def recovery_status_api():
    try:
        s_data = load_sessions()
        active_sessions = s_data.get('active_sessions', [])
        scheduled_sessions = s_data.get('scheduled_sessions', [])
        
        try:
            output = subprocess.check_output(["systemctl", "list-units", "--type=service", "--state=running"], text=True)
            running_services = len([line for line in output.strip().split('\n') if "stream-" in line])
        except:
            running_services = 0
        
        scheduler_jobs = len(scheduler.get_jobs()) if scheduler else 0
        
        return jsonify({
            'status': 'success',
            'data': {
                'active_sessions_count': len(active_sessions),
                'scheduled_sessions_count': len(scheduled_sessions),
                'running_services_count': running_services,
                'scheduler_jobs_count': scheduler_jobs,
                'last_check': datetime.now(jakarta_tz).isoformat(),
                'recovery_enabled': True
            }
        })
        
    except Exception as e:
        logger.error(f"RECOVERY STATUS: Error: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': f'Gagal mendapatkan status recovery: {str(e)}'
        }), 500

# Domain API
@app.route('/api/domain/setup', methods=['POST'])
@admin_required
def setup_domain_api():
    try:
        data = request.get_json()
        domain_name = data.get('domain_name', '').strip()
        ssl_enabled = data.get('ssl_enabled', False)
        port = data.get('port', 5000)
        
        if not domain_name:
            return jsonify({'success': False, 'message': 'Domain name is required'})
        
        # Validate domain format
        import re
        if not re.match(r'^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', domain_name):
            return jsonify({'success': False, 'message': 'Invalid domain format'})
        
        # Save domain configuration
        domain_config = {
            'domain_name': domain_name,
            'ssl_enabled': ssl_enabled,
            'port': port,
            'configured_at': datetime.now().isoformat()
        }
        
        save_domain_config(domain_config)
        
        # Emit update to frontend
        socketio.emit('domain_config_update', {
            'config': domain_config,
            'current_url': get_current_url(domain_config)
        })
        
        return jsonify({'success': True, 'message': f'Domain {domain_name} configured successfully'})
        
    except Exception as e:
        logger.error(f"Domain setup error: {e}")
        return jsonify({'success': False, 'message': f'Domain setup failed: {str(e)}'})

# Initialize scheduler and recovery
def init_scheduler():
    """Initialize the background scheduler"""
    global scheduler
    try:
        scheduler = BackgroundScheduler(timezone=jakarta_tz)
        
        # Startup recovery sequence
        logger.info("=== STARTING STARTUP RECOVERY SEQUENCE ===")
        
        # 1. Recover schedules first
        recover_schedules()
        
        # 2. Perform startup recovery
        perform_startup_recovery()
        
        # 3. Setup monitoring jobs
        scheduler.add_job(
            func=check_systemd_sessions,
            trigger='interval',
            minutes=1,
            id='check_systemd_job',
            replace_existing=True
        )
        
        # 4. Setup recovery job - runs every 5 minutes
        scheduler.add_job(
            func=recover_orphaned_sessions,
            trigger='interval',
            minutes=5,
            id='recovery_job',
            replace_existing=True
        )
        
        # 5. Setup trial reset job if enabled
        if TRIAL_MODE_ENABLED:
            scheduler.add_job(
                func=trial_reset,
                trigger='interval',
                hours=TRIAL_RESET_HOURS,
                id='trial_reset_job',
                replace_existing=True
            )
            logger.info(f"Mode Trial Aktif. Reset dijadwalkan setiap {TRIAL_RESET_HOURS} jam.")
        
        scheduler.start()
        logger.info("SCHEDULER: Background scheduler started")
        logger.info(f"SCHEDULER: Active jobs: {[job.id for job in scheduler.get_jobs()]}")
        
    except Exception as e:
        logger.error(f"SCHEDULER: Error starting scheduler: {e}", exc_info=True)

if __name__ == '__main__':
    # Create necessary directories
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    os.makedirs('static', exist_ok=True)
    os.makedirs('templates', exist_ok=True)
    
    # Initialize scheduler only if not in debug mode or main process
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        init_scheduler()
    
    # Run the app
    logger.info("StreamHib V2 starting...")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)