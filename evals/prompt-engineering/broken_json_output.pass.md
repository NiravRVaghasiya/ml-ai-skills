Wording won't get you to zero. Capital letters and temperature 0 make malformed JSON rarer, but a prompt-only instruction still leaves a residual failure rate, because nothing forces the decoder to emit valid JSON.

1. Use the provider's native schema-constrained mode where it exists: Anthropic structured outputs or a tool with strict: true, OpenAI Structured Outputs with strict: true. These constrain decoding to your JSON Schema instead of asking nicely.
2. Keep a validation step anyway: parse, then validate against the schema with a library such as jsonschema or Pydantic, and on failure retry with the error message or fall back to a safe default, with a bounded number of attempts.
3. Handle the cases constrained modes don't cover. A refusal, or a response cut off at max_tokens, can still be non-conforming, so check the stop reason before parsing and leave enough token headroom.
4. Remember that the schema constrains shape, not correctness: a valid object can still hold wrong values, so add semantic checks where it matters.

"Zero" is realistic as zero unhandled failures, not as a guarantee that every raw response parses.
