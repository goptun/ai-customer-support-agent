# The LLM provider is configurable, with an OpenAI-compatible endpoint besides Anthropic

The spec puts Claude behind a small provider interface (Haiku 4.5 to classify, Sonnet 5.5 to respond and to judge in evaluation). Running it on the paid Anthropic API isn't an option for this portfolio project, so `LLM_PROVIDER=openai_compatible` points the same interface at any chat completions endpoint — in practice a self-hosted OpenAI-compatible router that puts several models (GPT, Gemini, Ollama) behind one endpoint. Anthropic stays the default and the code path the spec describes.

Such endpoints don't share Anthropic's structured output, so the JSON schema goes in the prompt and the reply is parsed leniently (the first JSON object that fits, past code fences, prose or a reasoning block); anything unparseable takes the same safe fallback as before — an unconfident `other` that asks again, and never a `confirm`. Everything that moves money or states facts stays in code, so a weaker model costs quality, not safety.

Consequences: a combo may answer each call with a different model, so an evaluation report records the combo, not the model behind each answer, and two runs compare less precisely; the hallucination judge is a smaller model than Sonnet, so the hallucination rate is noisier and the human review sample matters more.
