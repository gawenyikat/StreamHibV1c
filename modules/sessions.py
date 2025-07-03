import os
import json
import subprocess
from datetime import datetime, timedelta
import pytz
from .config import *
from .auth import login_required

def init_sessions(app, socketio):
    """Initialize sessions module"""
    
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
            
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
                
            logger.info(f"Berhasil menghapus semua ({deleted_count}) sesi tidak aktif.")
            return jsonify({'status': 'success', 'message': f'Berhasil menghapus {deleted_count} sesi tidak aktif.', 'deleted_count': deleted_count}), 200
        except Exception as e:
            logger.error("Error di API delete_all_inactive_sessions", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

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