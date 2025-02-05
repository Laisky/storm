import datetime

def utcnow() -> str:
    return datetime.datetime.utcnow().format("%Y-%m-%dT%H:%M:%SZ")
