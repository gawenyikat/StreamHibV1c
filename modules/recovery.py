import os
import subprocess
import time
from datetime import datetime, timedelta
import pytz
from flask import request, jsonify
from .config import *
from .auth import admin_required
from .sessions import load_sessions, save_sessions, get_active_sessions_data
from .streaming import is_service_running, create_systemd_service, sanitize_for_service_name

def init_recovery(app, socketio, scheduler):
    """Initialize recovery module"""
    
    # Set global instances
    from . import set_global_socketio, set_global_scheduler
    set_global_socketio(socketio)
    set_global_scheduler(scheduler)
    
    @app.route('/api/recovery/manual', methods=['POST'])
    @admin_required
    def manual_recovery_api():
        try:
            logger.info("RECOVERY: Manual recovery triggered")
            
            # Run recovery
            recovery_result = recover_orphaned_sessions()
            
            # Run cleanup
            cleanup_count = cleanup_unused_services()
            
            # Emit updates to frontend
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('sessions_update', get_active_sessions_data())
            
            return jsonify({
                'success': True,
                'message': 'Manual recovery completed',
                'recovery_result': recovery_result,
                'cleanup_count': cleanup_count
            })
            
        except Exception as e:
            logger.error(f"Manual recovery error: {e}")
            return jsonify({'success': False, 'message': f'Recovery failed: {str(e)}'})

    @app.route('/api/recovery/status', methods=['GET'])
    @admin_required
    def recovery_status_api():
        """API untuk mendapatkan status recovery sistem"""
        try:
            s_data = load_sessions()
            active_sessions = s_data.get('active_sessions', [])
            scheduled_sessions = s_data.get('scheduled_sessions', [])
            
            try:
                output = subprocess.check_output(["systemctl", "list-units", "--type=service", "--state=running"], text=True)
                running_services = len([line for line in output.strip().split('\n') if "stream-" in line])
            except:
                running_services = 0
            
            from . import get_scheduler
            scheduler_inst = get_scheduler()
            scheduler_jobs = len(scheduler_inst.get_jobs()) if scheduler_inst else 0
            
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
                if is_service_running(sanitized_service_id):
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
                if create_systemd_service(sanitized_service_id, video_file, rtmp_url, stream_key):
                    logger.info(f"RECOVERY: Successfully recovered session {session_id}")
                    
                    # Update session info
                    session_info['recovered_at'] = datetime.now(jakarta_tz).isoformat()
                    session_info['recovery_count'] = session_info.get('recovery_count', 0) + 1
                    
                    recovered_count += 1
                else:
                    logger.error(f"RECOVERY: Failed to recover session {session_id}")
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

def cleanup_unused_services():
    """Clean up systemd services that are not in active sessions"""
    try:
        logger.info("CLEANUP: Starting cleanup of unused services...")
        
        sessions_data = load_sessions()
        active_sessions = sessions_data.get('active_sessions', [])
        active_session_ids = set(s.get('sanitized_service_id') for s in active_sessions if s.get('sanitized_service_id'))
        
        # Get all stream services
        result = subprocess.run(
            ['systemctl', 'list-units', '--type=service', '--all', '--no-pager'],
            capture_output=True,
            text=True
        )
        
        cleanup_count = 0
        
        for line in result.stdout.split('\n'):
            if 'stream-' in line and '.service' in line:
                # Extract service name
                parts = line.split()
                if parts:
                    service_name = parts[0]
                    if service_name.endswith('.service'):
                        service_name = service_name[:-8]  # Remove .service
                    
                    # Extract session ID
                    if service_name.startswith('stream-'):
                        session_id = service_name[7:]  # Remove 'stream-'
                        
                        if session_id not in active_session_ids:
                            logger.info(f"CLEANUP: Removing unused service {service_name}")
                            # Stop and remove service
                            subprocess.run(['systemctl', 'stop', f"{service_name}.service"], check=False)
                            subprocess.run(['systemctl', 'disable', f"{service_name}.service"], check=False)
                            
                            service_file = f"/etc/systemd/system/{service_name}.service"
                            if os.path.exists(service_file):
                                os.remove(service_file)
                            
                            cleanup_count += 1
        
        if cleanup_count > 0:
            subprocess.run(['systemctl', 'daemon-reload'], check=True)
        
        logger.info(f"CLEANUP: Completed - Cleaned {cleanup_count} services")
        return cleanup_count
        
    except Exception as e:
        logger.error(f"CLEANUP: Error in cleanup process: {e}")
        return 0

def validate_session_data(session_data):
    """Validate session data for recovery"""
    required_fields = ['id', 'video_name', 'stream_key', 'platform']
    
    for field in required_fields:
        if not session_data.get(field):
            logger.error(f"VALIDATION: Missing field '{field}' in session data")
            return False
    
    # Validate platform
    if session_data.get('platform') not in ['YouTube', 'Facebook']:
        logger.error(f"VALIDATION: Invalid platform '{session_data.get('platform')}'")
        return False
    
    # Validate video file exists
    video_path = os.path.join(VIDEOS_DIR, session_data.get('video_name'))
    if not os.path.isfile(video_path):
        logger.error(f"VALIDATION: Video file '{session_data.get('video_name')}' not found")
        return False
    
    return True

def perform_startup_recovery():
    """Perform complete recovery on startup"""
    logger.info("=== STARTING STARTUP RECOVERY ===")
    
    try:
        # Recovery orphaned sessions
        recover_orphaned_sessions()
        
        # Cleanup unused services
        cleanup_unused_services()
        
        logger.info("=== STARTUP RECOVERY COMPLETED ===")
        
    except Exception as e:
        logger.error(f"STARTUP RECOVERY: Error during recovery: {e}")

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
                    from .streaming import stop_streaming_session
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
                        from .streaming import stop_streaming_session
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
            try:
                from . import get_socketio
                socketio_inst = get_socketio()
                if socketio_inst:
                    socketio_inst.emit('sessions_update', get_active_sessions_data())
                    from .sessions import get_inactive_sessions_data
                    socketio_inst.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            except:
                pass
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
                from . import get_scheduler
                scheduler_inst = get_scheduler()
                if scheduler_inst:
                    if recurrence == 'daily':
                        try: scheduler_inst.remove_job(f"daily-start-{sanitized_id}")
                        except: pass
                        try: scheduler_inst.remove_job(f"daily-stop-{sanitized_id}")
                        except: pass
                    elif recurrence == 'one_time':
                        try: scheduler_inst.remove_job(schedule_def_id)
                        except: pass
                        if not sched_item.get('is_manual_stop', sched_item.get('duration_minutes', 0) == 0):
                            try: scheduler_inst.remove_job(f"onetime-stop-{sanitized_id}")
                            except: pass
            except:
                pass
        s_data['scheduled_sessions'] = []

        logger.info(f"MODE TRIAL: Menghapus semua file video...")
        from .videos import get_videos_list
        videos_to_delete = get_videos_list()
        for video_file in videos_to_delete:
            try:
                os.remove(os.path.join(VIDEOS_DIR, video_file))
                logger.info(f"MODE TRIAL: File video {video_file} dihapus.")
            except Exception as e_vid_del:
                logger.error(f"MODE TRIAL: Gagal menghapus file video {video_file}: {e_vid_del}")
        
        save_sessions(s_data)
        
        # Emit updates if socketio is available
        try:
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('sessions_update', get_active_sessions_data())
                from .sessions import get_inactive_sessions_data
                socketio_inst.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
                from .scheduler import get_schedules_list_data
                socketio_inst.emit('schedules_update', get_schedules_list_data())
                from .videos import get_videos_list
                socketio_inst.emit('videos_update', get_videos_list())
                socketio_inst.emit('trial_reset_notification', {
                    'message': 'Aplikasi telah direset karena mode trial. Semua sesi dan video telah dihapus.'
                })
                socketio_inst.emit('trial_status_update', {
                    'is_trial': TRIAL_MODE_ENABLED,
                    'message': 'Mode Trial Aktif - Reset setiap {} jam.'.format(TRIAL_RESET_HOURS) if TRIAL_MODE_ENABLED else ''
                })
        except:
            pass

        logger.info("MODE TRIAL: Proses reset aplikasi selesai.")

    except Exception as e:
        logger.error(f"MODE TRIAL: Error besar selama proses reset: {e}", exc_info=True)