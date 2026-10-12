"""Start the Tuff Trucks social media manager:  python run.py"""
import uvicorn

from app import config

if __name__ == "__main__":
    network = "" if config.HOST in ("127.0.0.1", "localhost") else f"  (and http://<this-computer's-IP>:{config.PORT} on your network)"
    print(f"\n  Social manager running -> http://localhost:{config.PORT}{network}\n")
    # By default the app restarts itself whenever a .py file in app/ changes (handy while it's being worked on).
    # To pick up a .env edit, save any file in app/ or restart it. Put TTS_RELOAD=0 in .env to turn this off.
    reload = config.env("TTS_RELOAD", "1") != "0"
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="warning",
                reload=reload, reload_dirs=[str(config.ROOT / "app")] if reload else None)
