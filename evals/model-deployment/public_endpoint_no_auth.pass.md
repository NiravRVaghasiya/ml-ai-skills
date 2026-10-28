I'd keep the token, even for a demo. "Internal" stops being true the moment port 8080 is open to the internet: scanning bots find open ports within hours, and an unauthenticated prediction endpoint invites model extraction (querying it to copy the model), abuse that runs up compute cost, and exposure of whatever the model returns.

What I'd do instead:
- Keep the app's bearer-token check (it refuses to start without `MODEL_API_TOKEN`) and give the demo audience the token, or put an authenticating gateway in front.
- Don't open the port to the world: bind to 127.0.0.1 and reach it over an SSH tunnel or VPN, or restrict the firewall/security group to your office or VPN IP range.
- Leave `/docs`, `/redoc` and `/openapi.json` off. If people need to try requests, share a curl example with the token, or serve the docs only behind the same auth.
