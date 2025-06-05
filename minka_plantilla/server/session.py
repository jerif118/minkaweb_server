# shared.py
import time

SESSION_TIMEOUT       = 7_200      # 2 h
RECONNECT_GRACE_PERIOD = 60        # 1 min

sessions = {}  # room_id → {password, clients, last_activity}
clients  = {}  # client_id → {ws, status, room_id, ...}

def now():
    return time.time()