"""Serve the dashboard from the LOCAL database (for testing): python -m scripts.dashboard -> http://localhost:8765
On the server the worker serves it; open that one with deploy/dashboard.ps1."""
from src.config import DASHBOARD_PORT, DB_PATH
from src.dashboard import serve

if __name__ == "__main__":
    print(f"dashboard for {DB_PATH} at http://localhost:{DASHBOARD_PORT}  (Ctrl+C to stop)")
    try:
        serve("127.0.0.1", DASHBOARD_PORT)
    except KeyboardInterrupt:
        pass
