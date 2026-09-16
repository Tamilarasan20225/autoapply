"""
LLM client with automatic failover across free-tier providers.
Uses LiteLLM for unified OpenAI-compatible interface.

Provider priority:
  1. Google Gemini Flash  — 1,500 req/day, 1M context, no card
  2. Groq Llama 3.3 70B  — 1,000 req/day, 100K tokens, no card
  3. GitHub Models GPT-4o — 150-1000 req/day via GitHub token
"""

import os
import json
import time
import re
from typing import Any
from rich.console import Console

console = Console()


class LLMClient:
    """
    Manages LLM calls with automatic failover and rate limit safety.
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        llm_cfg = self.config.get("llm", {})
        self.call_delay = llm_cfg.get("call_delay_seconds", 2)

        # Build provider list from config or use defaults
        raw_providers = llm_cfg.get("providers", [
            {"name": "gemini", "model": "gemini/gemini-2.0-flash", "env_key": "GEMINI_API_KEY"},
            {"name": "groq", "model": "groq/llama-3.3-70b-versatile", "env_key": "GROQ_API_KEY"},
            {"name": "github", "model": "github/gpt-4o", "env_key": "GITHUB_TOKEN"},
        ])

        # Only include providers whose API key is set
        self.providers = []
        for p in raw_providers:
            key = os.environ.get(p.get("env_key", ""))
            if key:
                provider_entry = {
                    "name": p["name"],
                    "model": p["model"],
                    "api_key": key,
                }
                # Support optional api_base (e.g. for Groq with non-standard models)
                if p.get("api_base"):
                    provider_entry["api_base"] = p["api_base"]
                self.providers.append(provider_entry)

        if not self.providers:
            console.print("[bold red]ERROR:[/bold red] No LLM API keys found in environment!")
            console.print("Please set at least GEMINI_API_KEY or GROQ_API_KEY in your .env file")

        self._call_count = 0

    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> str | None:
        """
        Send a chat message with automatic failover across providers.

        Args:
            messages: List of {"role": "user/assistant", "content": "..."}
            system_prompt: Optional system instruction
            max_tokens: Max tokens to generate
            temperature: Sampling temperature (lower = more deterministic)

        Returns:
            Response text, or None if all providers failed
        """
        try:
            import litellm
            import logging
            litellm.set_verbose = False
            # Suppress noisy LiteLLM deprecation and info logs
            logging.getLogger("LiteLLM").setLevel(logging.ERROR)
            logging.getLogger("litellm").setLevel(logging.ERROR)
        except ImportError:
            console.print("[red]litellm not installed. Run: pip install litellm[/red]")
            return None

        if system_prompt:
            full_messages = [{"role": "system", "content": system_prompt}] + messages
        else:
            full_messages = messages

        for provider in self.providers:
            try:
                # Add safety delay between calls
                if self._call_count > 0:
                    time.sleep(self.call_delay)

                completion_kwargs = dict(
                    model=provider["model"],
                    messages=full_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    api_key=provider["api_key"],
                )
                # Pass api_base for providers that need it (e.g. Groq with non-standard models)
                if provider.get("api_base"):
                    completion_kwargs["api_base"] = provider["api_base"]

                response = litellm.completion(**completion_kwargs)

                self._call_count += 1
                content = response.choices[0].message.content or ""
                # If empty response, retry once before failing to next provider
                if not content.strip():
                    # One retry for transient empty responses (Gemini warmup issue)
                    import time as _time
                    _time.sleep(2)
                    try:
                        response2 = litellm.completion(**completion_kwargs)
                        content = response2.choices[0].message.content or ""
                    except Exception:
                        pass
                if not content.strip():
                    console.print(f"[yellow]LLM {provider['name']}:[/yellow] Empty response — trying next provider")
                    continue
                console.print(f"[dim]LLM ({provider['name']}):[/dim] ✓ {len(content)} chars")
                return content

            except Exception as e:
                err_str = str(e).lower()
                if "rate" in err_str or "429" in err_str or "quota" in err_str:
                    console.print(
                        f"[yellow]LLM {provider['name']}:[/yellow] Rate limited — trying next provider"
                    )
                    # Longer wait before trying next provider (allows quota to recover)
                    time.sleep(10)
                elif "service" in err_str or "503" in err_str or "unavailable" in err_str:
                    console.print(
                        f"[yellow]LLM {provider['name']}:[/yellow] Service unavailable — trying next provider"
                    )
                    time.sleep(2)
                elif "401" in err_str or "403" in err_str or "auth" in err_str:
                    console.print(
                        f"[yellow]LLM {provider['name']}:[/yellow] Auth error — check API key"
                    )
                else:
                    console.print(
                        f"[yellow]LLM {provider['name']}:[/yellow] {type(e).__name__}: {str(e)[:100]}"
                    )
                continue

        console.print("[red]All LLM providers failed for this call[/red]")
        return None

    def chat_json(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> dict | None:
        """
        Send a chat message expecting JSON response.
        Handles common LLM quirks like markdown code blocks.

        Returns:
            Parsed dict, or None if parsing failed
        """
        raw = self.chat(messages, system_prompt, max_tokens, temperature)
        if not raw:
            return None

        # Strip markdown code blocks if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            raw = raw.strip()

        # Strip <think>...</think> sections from reasoning models (Qwen, DeepSeek)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        # If we stripped think blocks and nothing is left, return None
        if not raw:
            return None

        # If response starts with <think> but wasn't closed (truncated at max_tokens),
        # strip everything up to and including the last </think> or from <think> onward
        if "<think>" in raw and "</think>" not in raw:
            # Truncated think block — nothing usable
            return None
        if raw.startswith("<think>"):
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract JSON from the response
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except Exception:
                    pass

            # Try to fix truncated JSON (LLM hit max_tokens mid-response)
            # Attempt to close open structure and parse
            truncated = raw
            # Count open braces/brackets
            open_braces = truncated.count('{') - truncated.count('}')
            open_brackets = truncated.count('[') - truncated.count(']')
            # Close any open strings
            if truncated.count('"') % 2 == 1:
                truncated += '"'
            # Close arrays and objects
            truncated += ']' * open_brackets + '}' * open_braces
            try:
                return json.loads(truncated)
            except Exception:
                pass

            console.print(f"[yellow]JSON parse failed:[/yellow] {raw[:200]}")
            return None

    @property
    def available(self) -> bool:
        """Check if at least one provider is configured."""
        return len(self.providers) > 0
