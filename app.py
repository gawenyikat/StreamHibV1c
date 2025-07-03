import os
import json
import subprocess
import hashlib
import uuid
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from filelock import FileLock
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import atexit
import signal
import sys

app = Flask(__name__)
app.secret_key = 'streamhib_v2_secret_key_2025'
socketio = SocketIO(app, cors_allowed_origins="*")
CORS(app)

# Timezone
jakarta_tz = pytz.timezone('Asia/Jakarta')

# File paths
SESSIONS_FILE = 'sessions.json'
USERS_FILE = 'users.json'
DOMAIN_CONFIG_FILE = 'domain_config.json'
VIDEOS_DIR = 'videos'

# File locks
sessions_lock = FileLock(f"{SESSIONS_FILE}.lock")
users_lock = FileLock(f"{USERS_FILE}.lock")
domain_lock = FileLock(f"{DOMAIN_CONFIG_FILE}.lock")

# Admin credentials
ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'streamhib2025'

# Ensure directories exist
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs('static', exist_ok=True)
os.makedirs('templates', exist_ok=True)

def load_json_file(filepath, lock, default_data):
    """Load JSON file with file locking"""
    try:
        with lock:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    return json.load(f)
            else:
                with open(filepath, 'w') as f:
                    json.dump(default_data, f, indent=2)
                return default_data
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return default_data

def save_json_file(filepath, lock, data):
    """Save JSON file with file locking"""
    try:
        with lock:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving {filepath}: {e}")
        return False

def load_sessions():
    """Load sessions data"""
    default_sessions = {
        "active_sessions": {},
        "inactive_sessions": {},
        "scheduled_sessions": {}
    }
    return load_json_file(SESSIONS_FILE, sessions_lock, default_sessions)

def save_sessions(sessions_data):
    """Save sessions data"""
    return save_json_file(SESSIONS_FILE, sessions_lock, sessions_data)

def load_users():
    """Load users data"""
    return load_json_file(USERS_FILE, users_lock, {})

def save_users(users_data):
    """Save users data"""
    return save_json_file(USERS_FILE, users_lock, users_data)

def load_domain_config():
    """Load domain configuration"""
    default_config = {
        "domain_name": "",
        "ssl_enabled": False,
        "port": 5000,
        "configured_at": "",
        "nginx_configured": False
    }
    return load_json_file(DOMAIN_CONFIG_FILE, domain_lock, default_config)

def save_domain_config(config_data):
    """Save domain configuration"""
    return save_json_file(DOMAIN_CONFIG_FILE, domain_lock, config_data)

def create_nginx_config(domain_name, ssl_enabled=False, port=5000):
    """Create nginx configuration for domain"""
    try:
        # Nginx config content
        if ssl_enabled:
            # For Cloudflare SSL (Flexible mode)
            nginx_config = f"""server {{
    listen 80;
    listen 443 ssl http2;
    server_name {domain_name};

    # Cloudflare SSL certificates (self-signed for backend)
    ssl_certificate /etc/ssl/certs/ssl-cert-snakeoil.pem;
    ssl_certificate_key /etc/ssl/private/ssl-cert-snakeoil.key;
    
    # SSL settings for Cloudflare
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;

    # Real IP from Cloudflare
    set_real_ip_from 173.245.48.0/20;
    set_real_ip_from 103.21.244.0/22;
    set_real_ip_from 103.22.200.0/22;
    set_real_ip_from 103.31.4.0/22;
    set_real_ip_from 141.101.64.0/18;
    set_real_ip_from 108.162.192.0/18;
    set_real_ip_from 190.93.240.0/20;
    set_real_ip_from 188.114.96.0/20;
    set_real_ip_from 197.234.240.0/22;
    set_real_ip_from 198.41.128.0/17;
    set_real_ip_from 162.158.0.0/15;
    set_real_ip_from 104.16.0.0/13;
    set_real_ip_from 104.24.0.0/14;
    set_real_ip_from 172.64.0.0/13;
    set_real_ip_from 131.0.72.0/22;
    real_ip_header CF-Connecting-IP;

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        
        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }}
}}"""
        else:
            # HTTP only
            nginx_config = f"""server {{
    listen 80;
    server_name {domain_name};

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}
}}"""

        # Write nginx config
        config_path = f"/etc/nginx/sites-available/{domain_name}"
        with open(config_path, 'w') as f:
            f.write(nginx_config)
        
        # Remove existing symlink if exists
        enabled_path = f"/etc/nginx/sites-enabled/{domain_name}"
        if os.path.exists(enabled_path):
            os.remove(enabled_path)
        
        # Create symlink
        os.symlink(config_path, enabled_path)
        
        # Test nginx config
        result = subprocess.run(['nginx', '-t'], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"DOMAIN ERROR: Nginx config test failed: {result.stderr}")
            return False
        
        # Reload nginx
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
        
        print(f"DOMAIN SUCCESS: Nginx configured for {domain_name} (SSL: {ssl_enabled})")
        return True
        
    except Exception as e:
        print(f"DOMAIN ERROR: Failed to create nginx config: {e}")
        return False

def remove_nginx_config(domain_name):
    """Remove nginx configuration for domain"""
    try:
        # Remove symlink
        enabled_path = f"/etc/nginx/sites-enabled/{domain_name}"
        if os.path.exists(enabled_path):
            os.remove(enabled_path)
        
        # Remove config file
        config_path = f"/etc/nginx/sites-available/{domain_name}"
        if os.path.exists(config_path):
            os.remove(config_path)
        
        # Reload nginx
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
        
        print(f"DOMAIN SUCCESS: Nginx config removed for {domain_name}")
        return True
        
    except Exception as e:
        print(f"DOMAIN ERROR: Failed to remove nginx config: {e}")
        return False

def get_video_files():
    """Get list of video files"""
    try:
        video_extensions = ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm']
        video_files = []
        
        if os.path.exists(VIDEOS_DIR):
            for file in os.listdir(VIDEOS_DIR):
                if any(file.lower().endswith(ext) for ext in video_extensions):
                    video_files.append(file)
        
        return sorted(video_files)
    except Exception as e:
        print(f"Error getting video files: {e}")
        return []

def hash_password(password):
    """Hash password using SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def is_admin_logged_in():
    """Check if admin is logged in"""
    return session.get('admin_logged_in', False)

def is_customer_logged_in():
    """Check if customer is logged in"""
    return session.get('customer_logged_in', False) and session.get('username')

def get_stats():
    """Get system statistics"""
    try:
        sessions_data = load_sessions()
        users_data = load_users()
        video_files = get_video_files()
        
        stats = {
            'total_users': len(users_data),
            'active_sessions': len(sessions_data.get('active_sessions', {})),
            'inactive_sessions': len(sessions_data.get('inactive_sessions', {})),
            'scheduled_sessions': len(sessions_data.get('scheduled_sessions', {})),
            'total_videos': len(video_files)
        }
        
        return stats
    except Exception as e:
        print(f"Error getting stats: {e}")
        return {
            'total_users': 0,
            'active_sessions': 0,
            'inactive_sessions': 0,
            'scheduled_sessions': 0,
            'total_videos': 0
        }

def recovery_orphaned_sessions():
    """Recovery function for orphaned sessions"""
    try:
        print("RECOVERY: Starting orphaned session recovery...")
        
        sessions_data = load_sessions()
        active_sessions = sessions_data.get('active_sessions', {})
        
        recovered_count = 0
        moved_to_inactive = 0
        
        for session_id, session_info in list(active_sessions.items()):
            try:
                service_name = f"stream-{session_id[:8]}"
                
                # Check if systemd service exists and is active
                result = subprocess.run(
                    ['systemctl', 'is-active', service_name],
                    capture_output=True,
                    text=True
                )
                
                if result.returncode != 0:  # Service not active
                    print(f"RECOVERY: Found orphaned session {session_id[:8]}...")
                    
                    # Check if video file exists
                    video_path = os.path.join(VIDEOS_DIR, session_info.get('video_file', ''))
                    
                    if os.path.exists(video_path):
                        # Try to recover the session
                        username = session_info.get('username', 'unknown')
                        video_file = session_info.get('video_file', '')
                        
                        # Create systemd service
                        service_content = f"""[Unit]
Description=StreamHib Session {session_id[:8]}
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/ffmpeg -re -i "{video_path}" -c:v libx264 -preset veryfast -maxrate 3000k -bufsize 6000k -pix_fmt yuv420p -g 50 -c:a aac -b:a 160k -ac 2 -ar 44100 -f flv rtmp://a.rtmp.youtube.com/live2/{session_info.get('stream_key', '')}
Restart=always
User=root

[Install]
WantedBy=multi-user.target
"""
                        
                        service_file_path = f"/etc/systemd/system/{service_name}.service"
                        with open(service_file_path, 'w') as f:
                            f.write(service_content)
                        
                        # Reload systemd and start service
                        subprocess.run(['systemctl', 'daemon-reload'], check=True)
                        subprocess.run(['systemctl', 'start', service_name], check=True)
                        
                        # Update session info
                        sessions_data['active_sessions'][session_id]['recovered_at'] = datetime.now(jakarta_tz).isoformat()
                        
                        recovered_count += 1
                        print(f"RECOVERY: Successfully recovered session {session_id[:8]} for user {username}")
                        
                    else:
                        # Video file doesn't exist, move to inactive
                        sessions_data['inactive_sessions'][session_id] = session_info
                        sessions_data['inactive_sessions'][session_id]['ended_at'] = datetime.now(jakarta_tz).isoformat()
                        sessions_data['inactive_sessions'][session_id]['end_reason'] = 'video_file_missing'
                        
                        del sessions_data['active_sessions'][session_id]
                        moved_to_inactive += 1
                        print(f"RECOVERY: Moved session {session_id[:8]} to inactive (video file missing)")
                        
            except Exception as e:
                print(f"RECOVERY ERROR: Failed to process session {session_id[:8]}: {e}")
                continue
        
        # Save updated sessions
        save_sessions(sessions_data)
        
        recovery_result = {
            'recovered': recovered_count,
            'moved_to_inactive': moved_to_inactive,
            'total_active': len(sessions_data.get('active_sessions', {}))
        }
        
        print(f"RECOVERY: Completed - Recovered: {recovered_count}, Moved to inactive: {moved_to_inactive}")
        return recovery_result
        
    except Exception as e:
        print(f"RECOVERY ERROR: {e}")
        return {'recovered': 0, 'moved_to_inactive': 0, 'total_active': 0}

def cleanup_unused_services():
    """Cleanup unused systemd services"""
    try:
        print("RECOVERY: Starting service cleanup...")
        
        sessions_data = load_sessions()
        active_sessions = sessions_data.get('active_sessions', {})
        
        # Get all stream services
        result = subprocess.run(
            ['systemctl', 'list-units', '--type=service', '--all', '--no-pager'],
            capture_output=True,
            text=True
        )
        
        cleanup_count = 0
        
        if result.returncode == 0:
            lines = result.stdout.split('\n')
            for line in lines:
                if 'stream-' in line and '.service' in line:
                    # Extract service name
                    parts = line.split()
                    if parts:
                        service_name = parts[0]
                        if service_name.startswith('stream-') and service_name.endswith('.service'):
                            # Extract session ID from service name
                            session_prefix = service_name.replace('stream-', '').replace('.service', '')
                            
                            # Check if this session exists in active sessions
                            session_exists = any(
                                session_id.startswith(session_prefix) 
                                for session_id in active_sessions.keys()
                            )
                            
                            if not session_exists:
                                try:
                                    # Stop and disable service
                                    subprocess.run(['systemctl', 'stop', service_name], check=True)
                                    subprocess.run(['systemctl', 'disable', service_name], check=True)
                                    
                                    # Remove service file
                                    service_file = f"/etc/systemd/system/{service_name}"
                                    if os.path.exists(service_file):
                                        os.remove(service_file)
                                    
                                    cleanup_count += 1
                                    print(f"RECOVERY: Cleaned up unused service {service_name}")
                                    
                                except Exception as e:
                                    print(f"RECOVERY ERROR: Failed to cleanup {service_name}: {e}")
        
        # Reload systemd
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        
        print(f"RECOVERY: Service cleanup completed - Removed: {cleanup_count}")
        return cleanup_count
        
    except Exception as e:
        print(f"RECOVERY ERROR: Service cleanup failed: {e}")
        return 0

# Routes
@app.route('/')
def index():
    """Main dashboard"""
    if not is_customer_logged_in():
        return redirect(url_for('customer_login'))
    
    username = session.get('username')
    sessions_data = load_sessions()
    video_files = get_video_files()
    
    # Get user's sessions
    user_sessions = {}
    for session_id, session_info in sessions_data.get('active_sessions', {}).items():
        if session_info.get('username') == username:
            user_sessions[session_id] = session_info
    
    return render_template('index.html', 
                         username=username,
                         sessions=user_sessions,
                         video_files=video_files)

@app.route('/login')
def customer_login():
    """Customer login page"""
    if is_customer_logged_in():
        return redirect(url_for('index'))
    return render_template('customer_login.html')

@app.route('/register')
def customer_register():
    """Customer register page"""
    if is_customer_logged_in():
        return redirect(url_for('index'))
    
    # Check if any users exist (only allow one user)
    users_data = load_users()
    if len(users_data) > 0:
        return render_template('registration_closed.html')
    
    return render_template('customer_register.html')

@app.route('/admin/login')
def admin_login():
    """Admin login page"""
    if is_admin_logged_in():
        return redirect(url_for('admin_index'))
    return render_template('admin_login.html')

@app.route('/admin')
def admin_index():
    """Admin dashboard"""
    if not is_admin_logged_in():
        return redirect(url_for('admin_login'))
    
    stats = get_stats()
    sessions_data = load_sessions()
    domain_config = load_domain_config()
    
    return render_template('admin_index.html', 
                         stats=stats,
                         sessions=sessions_data,
                         domain_config=domain_config)

@app.route('/admin/domain')
def admin_domain():
    """Admin domain management"""
    if not is_admin_logged_in():
        return redirect(url_for('admin_login'))
    
    domain_config = load_domain_config()
    return render_template('admin_domain.html', domain_config=domain_config)

@app.route('/admin/users')
def admin_users():
    """Admin user management"""
    if not is_admin_logged_in():
        return redirect(url_for('admin_login'))
    
    users_data = load_users()
    return render_template('admin_users.html', users=users_data)

@app.route('/admin/recovery')
def admin_recovery():
    """Admin recovery management"""
    if not is_admin_logged_in():
        return redirect(url_for('admin_login'))
    
    return render_template('admin_recovery.html')

@app.route('/admin/logout')
def admin_logout():
    """Admin logout"""
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/logout')
def customer_logout():
    """Customer logout"""
    session.pop('customer_logged_in', None)
    session.pop('username', None)
    return redirect(url_for('customer_login'))

# API Routes
@app.route('/api/customer/login', methods=['POST'])
def api_customer_login():
    """Customer login API"""
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '')
        
        if not username or not password:
            return jsonify({'success': False, 'message': 'Username and password required'})
        
        users_data = load_users()
        
        if username not in users_data:
            return jsonify({'success': False, 'message': 'Invalid username or password'})
        
        stored_password = users_data[username].get('password', '')
        if stored_password != hash_password(password):
            return jsonify({'success': False, 'message': 'Invalid username or password'})
        
        session['customer_logged_in'] = True
        session['username'] = username
        
        return jsonify({'success': True, 'message': 'Login successful'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Login error: {str(e)}'})

@app.route('/api/customer/register', methods=['POST'])
def api_customer_register():
    """Customer register API"""
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '')
        
        if not username or not password:
            return jsonify({'success': False, 'message': 'Username and password required'})
        
        users_data = load_users()
        
        # Only allow one user
        if len(users_data) > 0:
            return jsonify({'success': False, 'message': 'Registration is closed'})
        
        if username in users_data:
            return jsonify({'success': False, 'message': 'Username already exists'})
        
        users_data[username] = {
            'password': hash_password(password),
            'created_at': datetime.now(jakarta_tz).isoformat(),
            'role': 'customer'
        }
        
        if save_users(users_data):
            return jsonify({'success': True, 'message': 'Registration successful'})
        else:
            return jsonify({'success': False, 'message': 'Failed to save user data'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Registration error: {str(e)}'})

@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    """Admin login API"""
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '')
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return jsonify({'success': True, 'message': 'Admin login successful'})
        else:
            return jsonify({'success': False, 'message': 'Invalid admin credentials'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Admin login error: {str(e)}'})

@app.route('/api/domain/setup', methods=['POST'])
def api_domain_setup():
    """Setup domain configuration"""
    if not is_admin_logged_in():
        return jsonify({'success': False, 'message': 'Admin access required'})
    
    try:
        data = request.get_json()
        domain_name = data.get('domain_name', '').strip()
        ssl_enabled = data.get('ssl_enabled', False)
        port = data.get('port', 5000)
        
        if not domain_name:
            return jsonify({'success': False, 'message': 'Domain name required'})
        
        # Load current config
        domain_config = load_domain_config()
        
        # Remove old nginx config if exists
        if domain_config.get('domain_name') and domain_config.get('nginx_configured'):
            remove_nginx_config(domain_config['domain_name'])
        
        # Create new nginx config
        nginx_success = create_nginx_config(domain_name, ssl_enabled, port)
        
        if not nginx_success:
            return jsonify({'success': False, 'message': 'Failed to configure nginx'})
        
        # Update domain config
        new_config = {
            'domain_name': domain_name,
            'ssl_enabled': ssl_enabled,
            'port': port,
            'configured_at': datetime.now(jakarta_tz).isoformat(),
            'nginx_configured': True
        }
        
        if save_domain_config(new_config):
            print(f"DOMAIN SUCCESS: Domain {domain_name} configured successfully")
            return jsonify({
                'success': True, 
                'message': f'Domain {domain_name} configured successfully',
                'config': new_config
            })
        else:
            return jsonify({'success': False, 'message': 'Failed to save domain configuration'})
        
    except Exception as e:
        print(f"DOMAIN ERROR: {e}")
        return jsonify({'success': False, 'message': f'Domain setup error: {str(e)}'})

@app.route('/api/domain/remove', methods=['POST'])
def api_domain_remove():
    """Remove domain configuration"""
    if not is_admin_logged_in():
        return jsonify({'success': False, 'message': 'Admin access required'})
    
    try:
        domain_config = load_domain_config()
        
        if domain_config.get('domain_name') and domain_config.get('nginx_configured'):
            # Remove nginx config
            remove_nginx_config(domain_config['domain_name'])
            
            # Reset domain config
            default_config = {
                "domain_name": "",
                "ssl_enabled": False,
                "port": 5000,
                "configured_at": "",
                "nginx_configured": False
            }
            
            if save_domain_config(default_config):
                return jsonify({'success': True, 'message': 'Domain configuration removed'})
            else:
                return jsonify({'success': False, 'message': 'Failed to save configuration'})
        else:
            return jsonify({'success': False, 'message': 'No domain configured'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error removing domain: {str(e)}'})

@app.route('/api/recovery/manual', methods=['POST'])
def api_manual_recovery():
    """Manual recovery trigger"""
    if not is_admin_logged_in():
        return jsonify({'success': False, 'message': 'Admin access required'})
    
    try:
        # Run recovery
        recovery_result = recovery_orphaned_sessions()
        
        # Run cleanup
        cleanup_count = cleanup_unused_services()
        
        return jsonify({
            'success': True,
            'message': 'Manual recovery completed',
            'recovery_result': recovery_result,
            'cleanup_count': cleanup_count
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Recovery error: {str(e)}'})

@app.route('/api/admin/users/<username>', methods=['DELETE'])
def api_delete_user(username):
    """Delete user"""
    if not is_admin_logged_in():
        return jsonify({'success': False, 'message': 'Admin access required'})
    
    try:
        users_data = load_users()
        
        if username not in users_data:
            return jsonify({'success': False, 'message': 'User not found'})
        
        del users_data[username]
        
        if save_users(users_data):
            return jsonify({'success': True, 'message': f'User {username} deleted'})
        else:
            return jsonify({'success': False, 'message': 'Failed to save user data'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error deleting user: {str(e)}'})

@app.route('/api/videos')
def api_get_videos():
    """Get video files"""
    if not is_customer_logged_in():
        return jsonify({'success': False, 'message': 'Login required'})
    
    try:
        video_files = get_video_files()
        return jsonify({'success': True, 'videos': video_files})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error getting videos: {str(e)}'})

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(
    func=recovery_orphaned_sessions,
    trigger="interval",
    minutes=5,
    id='recovery_job'
)

def cleanup_on_exit():
    """Cleanup function on exit"""
    try:
        if scheduler.running:
            scheduler.shutdown()
        print("StreamHib V2 shutdown completed")
    except Exception as e:
        print(f"Error during cleanup: {e}")

# Register cleanup function
atexit.register(cleanup_on_exit)

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    print(f"Received signal {sig}, shutting down...")
    cleanup_on_exit()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == '__main__':
    try:
        print("Starting StreamHib V2...")
        print("Scheduler dimulai untuk recovery otomatis setiap 5 menit")
        
        # Start scheduler
        scheduler.start()
        
        # Run Flask app
        socketio.run(app, host='0.0.0.0', port=5000, debug=False)
        
    except Exception as e:
        print(f"Error starting StreamHib V2: {e}")
        cleanup_on_exit()