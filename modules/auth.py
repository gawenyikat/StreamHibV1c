import hashlib
from flask import session, request, jsonify, redirect, url_for
from functools import wraps
from .config import *

def init_auth(app):
    """Initialize authentication module"""
    
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

    @app.route('/api/check-session', methods=['GET'])
    @login_required
    def check_session_api(): 
        return jsonify({'logged_in': True, 'user': session.get('username')})

def hash_password(password):
    """Hash password using SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hashed):
    """Verify password against hash"""
    return hash_password(password) == hashed

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
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_customer_logged_in():
            return redirect(url_for('customer_login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator for routes that require admin login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_admin_logged_in():
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function