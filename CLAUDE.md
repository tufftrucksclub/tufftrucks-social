# Tuff Trucks Social

- The live Tuff Trucks Social (port 8420) runs from a scheduled task. Restart it only with `cscript //nologo stop-social.vbs` and then
  `cscript //nologo start-social-hidden.vbs`. The start script hands off to the scheduled task, so it doesn't run inside the Claude
  app and close when the app closes or updates.
- Never start the live app from a Claude shell with `run.py`, `social-service.cmd`, `start.bat` or `start-social-hidden.vbs /direct`.
