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
        from modules.scheduler import recover_schedules
        
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