"""LLM post-processing: protocols and summaries through a local text model.

The audio side of Стенограф never leaves the machine; this package talks to a
local OpenAI-compatible server (LM Studio, Ollama, vLLM) for the *text* stage
only — nothing here touches audio or the ASR engines.
"""
