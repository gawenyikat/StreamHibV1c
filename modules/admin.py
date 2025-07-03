from flask import render_template, request, jsonify, session, redirect, url_for
from .config import *
from .auth import admin_required, is_admin_logged_in, load_users, save_users
from .sessions import load_sessions, get_active_sessions_data, get_inactive_sessions_data
from .videos import get_videos_list
from .domain import load_domain_config

def init_admin(app, socketio):
    """Initialize admin module"""
    
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
    
    # API Routes
    @app.route('/api/admin/login', methods=['POST'])
    def api_admin_login():
        try:
            data = request.get_json()
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