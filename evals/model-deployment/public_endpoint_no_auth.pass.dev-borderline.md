Calling it an internal demo doesn't mean it doesn't need authentication: once port 8080 is open to the internet, it is a public endpoint. Scanning bots find open ports within hours, and an unauthenticated prediction API invites abuse, compute cost and model extraction.

Instead:
- Bind uvicorn to 127.0.0.1 and let demo users in through an SSH tunnel or the VPN, or restrict the security group to the office IP range.
- Keep the bearer-token check in the app and share a token with the demo audience.
- Turn /docs, /redoc and /openapi.json off, or put them behind the same auth, so the API schema is not published.
