from typing import Optional
from datetime import datetime

def to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None