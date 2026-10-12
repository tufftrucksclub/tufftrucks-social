"""Start the Tuff Trucks social media manager:  python run.py"""
import uvicorn

from app import config

if __name__ == "__main__":
    network = "" if config.HOST in ("127.0.0.1", "localhost") else f"  (and http://<this-computer's-IP>:{config.PORT} on your network)"
    print(f"\n  Social manager running → http://localhost:{config.PORT}{network}\n")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="warning")
