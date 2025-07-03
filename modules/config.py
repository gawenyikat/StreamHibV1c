import os
import json
import logging
import pytz
from filelock import FileLock

logger = logging.getLogger(__name__)

# Timezone configuration
jakarta_tz = pytz.timezone('Asia/Jakarta')

# Trial mode configuration
TRIAL_MODE_ENABLED = False  # Ganti menjadi True untuk mengaktifkan mode trial
TRIAL_RESET_HOURS = 2       # Interval reset dalam jam

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