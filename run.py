"""Start the Tuff Trucks social media manager:  python run.py"""
import uvicorn

from app import config

if __name__ == "__main__":
    print(f"\n  Social manager running → http://localhost:{config.PORT}  (and http://<this-computer's-IP>:{config.PORT} on your network)\n")
    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT, log_level="warning")
