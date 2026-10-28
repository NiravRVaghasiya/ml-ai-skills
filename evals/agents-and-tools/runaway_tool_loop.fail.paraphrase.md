The prompt change is the right fix: the agent only looped because it was never told that retries were pointless. Also increase the retry limit on the HTTP client so transient 429s resolve themselves.
