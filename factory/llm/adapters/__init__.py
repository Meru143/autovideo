"""LLM provider adapters."""
from .cli import call_cli
from .gemini import call_gemini
from .openai_compatible import call_openai_compatible

__all__ = ["call_cli", "call_gemini", "call_openai_compatible"]
