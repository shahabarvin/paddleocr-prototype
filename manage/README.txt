Double-click a file to manage the OCR service. No command line needed.

  Status.cmd       Is the service running? (no admin needed)
  Start.cmd        Start the service
  Stop.cmd         Turn it off until you click Start (or the PC reboots)
  Restart.cmd      Restart the service and the tunnel
  Set-Workers.cmd  Change how many documents are processed in parallel
  Set-Cleanup.cmd  Choose after how many days old job files are deleted (0 = never)
  Dashboard.cmd    Open the live dashboard in the browser

Start / Stop / Restart / Set-Workers / Set-Cleanup ask for administrator permission (click Yes).
The service runs on its own at boot; nothing here needs to stay open.
