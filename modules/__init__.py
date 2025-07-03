# StreamHib V2 Modules
# Global instances for cross-module communication
socketio_instance = None
scheduler_instance = None

def set_global_socketio(socketio):
    """Set global socketio instance"""
    global socketio_instance
    socketio_instance = socketio

def set_global_scheduler(scheduler):
    """Set global scheduler instance"""
    global scheduler_instance
    scheduler_instance = scheduler

def get_socketio():
    """Get global socketio instance"""
    return socketio_instance

def get_scheduler():
    """Get global scheduler instance"""
    return scheduler_instance