"""
Eva v10 Session Manager Module
Handles user sessions for the Eva chat system
"""

import time
import uuid
from typing import Dict, Any, Optional
from .state_machine import EvaPhase


# In-memory session storage (in production, use Redis or database)
_sessions: Dict[str, Dict[str, Any]] = {}


def create_session(user_id: str, business_name: str = "", business_type: str = "", 
                   schedule: Dict[str, str] = None, owner_name: str = "") -> str:
    """
    Create a new Eva session for a user
    Returns session_id
    """
    session_id = str(uuid.uuid4())
    
    if schedule is None:
        schedule = {"open": "08:00", "close": "22:00"}
    
    session = {
        "session_id": session_id,
        "user_id": user_id,
        "phase": EvaPhase.GREET.value,
        "owner_name": owner_name or "amigo",
        "business_name": business_name,
        "business_type": business_type or "tienda",
        "zone": "",
        "concern": "",
        "confirmed_rules": [],
        "rules_ids": [],  # For tracking which rule templates were used
        "camera_id": "",
        "camera_connected": False,
        "position_confirmed": False,
        "frame_available": False,
        "schedule": schedule,
        "created_at": time.time(),
        "last_activity": time.time(),
        "image_b64": "",
        "image_description": ""
    }
    
    _sessions[session_id] = session
    return session_id


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve a session by ID
    Returns None if not found or expired
    """
    session = _sessions.get(session_id)
    if not session:
        return None
    
    # Check for session timeout (30 minutes)
    if time.time() - session.get("last_activity", 0) > 1800:  # 30 minutes
        destroy_session(session_id)
        return None
    
    return session


def update_session(session_id: str, updates: Dict[str, Any]) -> bool:
    """
    Update a session with new values
    Returns True if successful
    """
    session = get_session(session_id)
    if not session:
        return False
    
    session.update(updates)
    session["last_activity"] = time.time()
    _sessions[session_id] = session
    return True


def destroy_session(session_id: str) -> bool:
    """
    Remove a session from memory
    Returns True if session existed
    """
    if session_id in _sessions:
        del _sessions[session_id]
        return True
    return False


def get_user_sessions(user_id: str) -> Dict[str, Dict[str, Any]]:
    """
    Get all sessions for a specific user
    """
    user_sessions = {}
    for session_id, session in _sessions.items():
        if session.get("user_id") == user_id:
            user_sessions[session_id] = session
    return user_sessions


def cleanup_expired_sessions() -> int:
    """
    Remove all expired sessions
    Returns number of sessions removed
    """
    expired = []
    current_time = time.time()
    
    for session_id, session in _sessions.items():
        if current_time - session.get("last_activity", 0) > 1800:  # 30 minutes
            expired.append(session_id)
    
    for session_id in expired:
        del _sessions[session_id]
    
    return len(expired)


def get_session_count() -> int:
    """
    Get total number of active sessions
    """
    return len(_sessions)


def reset_session_to_greet(session_id: str) -> bool:
    """
    Reset a session back to the greeting phase
    Useful for starting over
    """
    session = get_session(session_id)
    if not session:
        return False
    
    # Keep basic user info but reset session state
    user_id = session.get("user_id")
    owner_name = session.get("owner_name")
    business_name = session.get("business_name")
    business_type = session.get("business_type")
    schedule = session.get("schedule")
    
    # Create fresh session
    new_session_id = create_session(user_id, business_name, business_type, schedule, owner_name)
    
    # Destroy old session
    destroy_session(session_id)
    
    return new_session_id is not None
