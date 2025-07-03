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

# Import modules
from modules.config import *
from modules.auth import *
from modules.sessions import *
from modules.videos import *
from modules.streaming import *
from modules.scheduler import *
from modules.domain import *
from modules.recovery import *
from modules.admin import *

app = Flask(__name__)
app.secret_key = 'streamhib_v2_secret_key_2025'
socketio = SocketIO(app, cors_allowed_origins="*")
CORS(app)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize scheduler
scheduler = BackgroundScheduler(timezone=jakarta_tz)

# Initialize modules with app context
init_auth(app)
init_sessions(app, socketio)
init_videos(app, socketio)
init_streaming(app, socketio, scheduler)
init_scheduler_module(app, socketio, scheduler)
init_domain(app, socketio)
init_recovery(app, socketio, scheduler)
init_admin(app, socketio)

# Set global instances after module initialization
from modules import set_global_socketio, set_global_scheduler
set_global_socketio(socketio)
set_global_scheduler(scheduler)

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
    
    # Import scheduler functions
    from modules.scheduler import get_schedules_list_data
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

# Initialize scheduler and recovery
def init_scheduler():
    """Initialize the background scheduler"""
    try:
        # Import recovery functions
        from modules.recovery import recover_orphaned_sessions, check_systemd_sessions, trial_reset
        from modules.scheduler import get_schedules_list_data
        
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

def recover_schedules():
    """Recover scheduled sessions on startup"""
    try:
        from modules.scheduler import start_scheduled_streaming, stop_scheduled_streaming
        
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