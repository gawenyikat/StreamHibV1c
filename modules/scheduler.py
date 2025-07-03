from datetime import datetime, timedelta
import pytz
from flask import request, jsonify
from .config import *
from .auth import login_required
from .sessions import load_sessions, save_sessions, get_active_sessions_data, get_inactive_sessions_data
from .streaming import create_service_file, stop_streaming_session

def init_scheduler_module(app, socketio, scheduler):
    """Initialize scheduler module"""
    
    # Set global instances
    from . import set_global_socketio, set_global_scheduler
    set_global_socketio(socketio)
    set_global_scheduler(scheduler)
    
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
                        from . import get_scheduler
                        scheduler_inst = get_scheduler()
                        if scheduler_inst:
                            if sched.get('recurrence_type') == 'daily':
                                try:
                                    scheduler_inst.remove_job(f"daily-start-{old_sanitized_service_id}")
                                    logger.info(f"SCHEDULER API: Removed old daily start job: daily-start-{old_sanitized_service_id}")
                                except:
                                    pass
                                try:
                                    scheduler_inst.remove_job(f"daily-stop-{old_sanitized_service_id}")
                                    logger.info(f"SCHEDULER API: Removed old daily stop job: daily-stop-{old_sanitized_service_id}")
                                except:
                                    pass
                            else:
                                try:
                                    scheduler_inst.remove_job(old_schedule_def_id)
                                    logger.info(f"SCHEDULER API: Removed old one-time start job: {old_schedule_def_id}")
                                except:
                                    pass
                                if not sched.get('is_manual_stop', sched.get('duration_minutes', 0) == 0):
                                    try:
                                        scheduler_inst.remove_job(f"onetime-stop-{old_sanitized_service_id}")
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

            from . import get_scheduler
            scheduler_inst = get_scheduler()
            if not scheduler_inst:
                return jsonify({'status': 'error', 'message': 'Scheduler tidak tersedia.'}), 500

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

                scheduler_inst.add_job(start_scheduled_streaming, 'cron', hour=start_hour, minute=start_minute,
                                  args=[platform, stream_key, video_file, session_name_original, 0, 'daily', start_time_of_day, stop_time_of_day],
                                  id=aps_start_job_id, replace_existing=True, misfire_grace_time=3600)
                logger.info(f"SCHEDULER API: Jadwal harian START '{aps_start_job_id}' untuk '{session_name_original}' ditambahkan: {start_time_of_day}")

                scheduler_inst.add_job(stop_scheduled_streaming, 'cron', hour=stop_hour, minute=stop_minute,
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
                scheduler_inst.add_job(start_scheduled_streaming, 'date', run_date=start_dt,
                                  args=[platform, stream_key, video_file, session_name_original, duration_minutes, 'one_time', None, None],
                                  id=aps_start_job_id, replace_existing=True)
                logger.info(f"SCHEDULER API: Jadwal sekali jalan START '{aps_start_job_id}' untuk '{session_name_original}' ditambahkan pada {start_dt}")

                if not is_manual_stop:
                    stop_dt = start_dt + timedelta(minutes=duration_minutes)
                    aps_stop_job_id = f"onetime-stop-{sanitized_service_id_part}"
                    scheduler_inst.add_job(stop_scheduled_streaming, 'date', run_date=stop_dt,
                                      args=[session_name_original], id=aps_stop_job_id, replace_existing=True)
                    logger.info(f"SCHEDULER API: Jadwal sekali jalan STOP '{aps_stop_job_id}' untuk '{session_name_original}' ditambahkan pada {stop_dt}")
                
                msg = f'Sesi "{session_name_original}" dijadwalkan sekali pada {start_dt.strftime("%d-%m-%Y %H:%M:%S")}'
                msg += f' selama {duration_minutes} menit.' if not is_manual_stop else ' hingga dihentikan manual.'
            
            else:
                return jsonify({'status': 'error', 'message': f"Tipe recurrence '{recurrence_type}' tidak dikenal."}), 400

            s_data.setdefault('scheduled_sessions', []).append(sched_entry)
            save_sessions(s_data)
            
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('schedules_update', get_schedules_list_data())
                socketio_inst.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            
            logger.info(f"SCHEDULER API: Berhasil membuat jadwal untuk '{session_name_original}'. Total jobs aktif: {len(scheduler_inst.get_jobs())}")
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
                from . import get_scheduler
                scheduler_inst = get_scheduler()
                if scheduler_inst:
                    if schedule_to_cancel_obj.get('recurrence_type') == 'daily':
                        aps_start_job_id = f"daily-start-{sanitized_service_id_from_def}"
                        aps_stop_job_id = f"daily-stop-{sanitized_service_id_from_def}"
                        try: 
                            scheduler_inst.remove_job(aps_start_job_id)
                            removed_scheduler_jobs_count += 1
                            logger.info(f"Job harian START '{aps_start_job_id}' dihapus.")
                        except Exception as e: 
                            logger.info(f"Gagal hapus job harian START '{aps_start_job_id}': {e}")
                        try: 
                            scheduler_inst.remove_job(aps_stop_job_id)
                            removed_scheduler_jobs_count += 1
                            logger.info(f"Job harian STOP '{aps_stop_job_id}' dihapus.")
                        except Exception as e: 
                            logger.info(f"Gagal hapus job harian STOP '{aps_stop_job_id}': {e}")
                    
                    elif schedule_to_cancel_obj.get('recurrence_type', 'one_time') == 'one_time':
                        aps_start_job_id = schedule_definition_id_to_cancel 
                        try: 
                            scheduler_inst.remove_job(aps_start_job_id)
                            removed_scheduler_jobs_count += 1
                            logger.info(f"Job sekali jalan START '{aps_start_job_id}' dihapus.")
                        except Exception as e: 
                            logger.info(f"Gagal hapus job sekali jalan START '{aps_start_job_id}': {e}")

                        if not schedule_to_cancel_obj.get('is_manual_stop', schedule_to_cancel_obj.get('duration_minutes', 0) == 0):
                            aps_stop_job_id = f"onetime-stop-{sanitized_service_id_from_def}"
                            try: 
                                scheduler_inst.remove_job(aps_stop_job_id)
                                removed_scheduler_jobs_count += 1
                                logger.info(f"Job sekali jalan STOP '{aps_stop_job_id}' dihapus.")
                            except Exception as e: 
                                logger.info(f"Gagal hapus job sekali jalan STOP '{aps_stop_job_id}': {e}")
            
            if idx_to_remove_json != -1:
                del s_data['scheduled_sessions'][idx_to_remove_json]
                save_sessions(s_data)
                logger.info(f"Definisi jadwal '{session_display_name}' (ID: {schedule_definition_id_to_cancel}) dihapus dari sessions.json.")
            
            from . import get_socketio
            socketio_inst = get_socketio()
            if socketio_inst:
                socketio_inst.emit('schedules_update', get_schedules_list_data())
            
            return jsonify({
                'status': 'success',
                'message': f"Definisi jadwal '{session_display_name}' dibatalkan. {removed_scheduler_jobs_count} job dari scheduler berhasil dihapus."
            })
        except Exception as e:
            req_data_cancel = request.get_json(silent=True) or {}
            def_id_err = req_data_cancel.get('id', 'N/A')
            logger.error(f"Error saat membatalkan jadwal, ID definisi dari request: {def_id_err}", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Kesalahan Server Internal: {str(e)}'}), 500

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
        
        # Import socketio from the calling module
        from . import get_socketio
        socketio_inst = get_socketio()
        if socketio_inst:
            socketio_inst.emit('sessions_update', get_active_sessions_data())
            socketio_inst.emit('schedules_update', get_schedules_list_data())
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