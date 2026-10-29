# Running an open-source model locally

Hugging Face's routed Inference Providers need credits (free accounts get none; see
`docs/agent.md`). The same kind of open models can be run on your own computer for free.
Hugging Face hosts the model files; [Ollama](https://ollama.com) runs them and offers an
OpenAI-compatible server on `http://localhost:11434/v1`, which our client already speaks.

Model used in the examples: **Qwen2.5-1.5B-Instruct** (licence Apache-2.0), as a GGUF file from
the Hub repo `bartowski/Qwen2.5-1.5B-Instruct-GGUF`, quantisation `Q4_K_M` (about 1 GB). The
Hub page shows this command: `ollama run hf.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M`.

## Windows (PowerShell)
1. Install Ollama from ollama.com and check it: `ollama --version`.
2. Download the model once (about 1 GB):
   ```
   ollama pull hf.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M
   ```
3. Point WattMagent at it (for the current PowerShell window only):
   ```
   $env:WATTMAGENT_HF_BASE_URL = "http://localhost:11434/v1"
   $env:WATTMAGENT_LLM_MODELS = "hf.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M"
   $env:WATTMAGENT_REQUEST_TIMEOUT_S = "180"
   ```
   No token is needed for a local address. The long timeout covers the first answer, when the
   model is loaded into memory.
4. Check that it answers, then try the agent with the model, then grade it:
   ```
   python -m wattmagent.graph --llm-check
   python -m wattmagent.graph "90% by 6:30, not before 22:00" --csv data\prices_hourly_utc.csv --llm
   python -m wattmagent.graph --llm-eval
   ```

## What to expect
- A small model on a laptop CPU is slow (seconds per answer) and sometimes wrong. That is
  acceptable here: its parse is checked against the text and the validator, and a failed or
  rejected reply falls back to the rules; its wording is checked for invented numbers.
- `--llm-eval` runs the 30 labelled requests through "model first, rules as fallback" and
  prints how many were fully right and how many were answered by the model. Use it to compare
  models (a larger one, for example a 3B variant, if your computer copes) with numbers.
- The result depends on your computer and the model; record the figures you measure in
  `docs/results.md` with the model name and quantisation.
