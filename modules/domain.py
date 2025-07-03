import os
import subprocess
import re
from flask import request, jsonify
from .config import *
from .auth import admin_required

def init_domain(app, socketio):
    """Initialize domain module"""
    
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
            if not re.match(r'^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', domain_name):
                return jsonify({'success': False, 'message': 'Invalid domain format'})
            
            # Setup Nginx configuration
            success, message = setup_nginx_domain(domain_name, ssl_enabled, port)
            
            if success:
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
                
                return jsonify({'success': True, 'message': message})
            else:
                return jsonify({'success': False, 'message': message})
                
        except Exception as e:
            logger.error(f"Domain setup error: {e}")
            return jsonify({'success': False, 'message': f'Domain setup failed: {str(e)}'})

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

def setup_nginx_domain(domain_name, ssl_enabled=False, port=5000):
    """Setup Nginx configuration for domain"""
    try:
        logger.info(f"NGINX SETUP: Starting configuration for {domain_name}")
        
        # Install nginx if not present
        subprocess.run(["apt", "update"], check=False, capture_output=True)
        result = subprocess.run(["apt", "install", "-y", "nginx"], check=False, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"NGINX SETUP: Failed to install nginx: {result.stderr}")
            return False, "Failed to install Nginx"
        
        # Create directories
        os.makedirs("/etc/nginx/sites-available", exist_ok=True)
        os.makedirs("/etc/nginx/sites-enabled", exist_ok=True)
        
        # Remove old configuration if exists
        old_config_available = f"/etc/nginx/sites-available/{domain_name}"
        old_config_enabled = f"/etc/nginx/sites-enabled/{domain_name}"
        
        if os.path.exists(old_config_enabled):
            os.remove(old_config_enabled)
        if os.path.exists(old_config_available):
            os.remove(old_config_available)
        
        # Create Nginx config
        nginx_config = f"""server {{
    listen 80;
    server_name {domain_name};
    
    # Security headers
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    
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
        
        # Timeout settings
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }}
}}"""
        
        # Write configuration
        config_file = f"/etc/nginx/sites-available/{domain_name}"
        with open(config_file, 'w') as f:
            f.write(nginx_config)
        
        # Enable site
        enabled_file = f"/etc/nginx/sites-enabled/{domain_name}"
        os.symlink(config_file, enabled_file)
        
        # Disable default site
        default_enabled = "/etc/nginx/sites-enabled/default"
        if os.path.exists(default_enabled):
            os.remove(default_enabled)
        
        # Test and reload Nginx
        test_result = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
        if test_result.returncode != 0:
            logger.error(f"NGINX SETUP: Configuration test failed: {test_result.stderr}")
            return False, "Nginx configuration test failed"
        
        restart_result = subprocess.run(["systemctl", "restart", "nginx"], capture_output=True, text=True)
        if restart_result.returncode != 0:
            logger.error(f"NGINX SETUP: Failed to restart nginx: {restart_result.stderr}")
            return False, "Failed to restart Nginx"
        
        subprocess.run(["systemctl", "enable", "nginx"], check=False)
        
        # Setup SSL if requested
        if ssl_enabled:
            ssl_success = setup_ssl_with_certbot(domain_name)
            if ssl_success:
                return True, f"Domain {domain_name} configured successfully with SSL"
            else:
                return True, f"Domain {domain_name} configured successfully, but SSL setup failed"
        
        logger.info(f"NGINX SETUP: Successfully configured for domain: {domain_name}")
        return True, f"Domain {domain_name} configured successfully"
        
    except Exception as e:
        logger.error(f"NGINX SETUP: Error setting up domain {domain_name}: {e}")
        return False, f"Domain setup failed: {str(e)}"

def setup_ssl_with_certbot(domain_name):
    """Setup SSL using Let's Encrypt Certbot"""
    try:
        logger.info(f"SSL SETUP: Starting SSL configuration for {domain_name}")
        
        # Install certbot
        install_result = subprocess.run([
            "apt", "install", "-y", "certbot", "python3-certbot-nginx"
        ], capture_output=True, text=True)
        
        if install_result.returncode != 0:
            logger.error(f"SSL SETUP: Failed to install certbot: {install_result.stderr}")
            return False
        
        # Get SSL certificate
        result = subprocess.run([
            "certbot", "--nginx", 
            "-d", domain_name, 
            "--non-interactive", 
            "--agree-tos", 
            "--email", f"admin@{domain_name}",
            "--redirect"
        ], capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0:
            logger.info(f"SSL SETUP: SSL certificate obtained successfully for {domain_name}")
            return True
        else:
            logger.error(f"SSL SETUP: Failed to obtain SSL certificate: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error("SSL SETUP: Certbot command timed out")
        return False
    except Exception as e:
        logger.error(f"SSL SETUP: Error setting up SSL: {e}")
        return False

# Import required functions
from datetime import datetime