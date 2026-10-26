"""Start the localhost-only development server and open the browser when ready."""
import threading
import time
import urllib.request
import webbrowser
import uvicorn

ADDRESS = 'http://127.0.0.1:8000'

def open_when_ready():
    for _ in range(90):
        try:
            with urllib.request.urlopen(ADDRESS, timeout=1) as r:
                if r.status == 200:
                    webbrowser.open(ADDRESS)
                    return
        except Exception:
            time.sleep(0.5)

if __name__ == '__main__':
    threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run('main:app', host='127.0.0.1', port=8000)
