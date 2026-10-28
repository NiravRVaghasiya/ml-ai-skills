That email is a prompt injection attempt. Content arriving from outside — email bodies, attachments, web pages — is data, not instructions, and must never be able to trigger actions on its own.

Controls that actually work: limit the agent's tools to what support needs (it should have no bulk-export capability at all), enforce refund limits in code with human approval for anything beyond policy, and keep untrusted text away from any path that can execute actions. A system-prompt warning helps a little but is not a reliable defense by itself.
