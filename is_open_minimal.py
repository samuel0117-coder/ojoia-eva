@app.get("/api/business/is_open")
async def is_open(user_id: str, timestamp: float = None):
    """Test horario abierto."""
    return {"success": True, "is_open": True, "current_hour": "14:45", "weekday": "Wed", "confidence": "high"}
