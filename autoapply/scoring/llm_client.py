"""
LLM client with automatic failover across free-tier providers and API keys.
Uses LiteLLM for unified OpenAI-compatible interface.

Provider priority:
  1. Groq Llama 3.3 70B  — 1,000 req/day per key, 100K tokens, no card (PRIMARY)
  2. Google Gemini Flash  — 1,500 req/day per key, 1M context, no card (rate-limited more often)
  3. GitHub Models GPT-4o — 150-1000 req/day via GitHub token (last resort)

Each provider can have multiple API keys (env_keys / numbered env vars) pooled
via autoapply.utils.key_pool.KeyPool — a rate-limited key is cooled down and
the next key/provider is tried immediately instead of blocking.
"""

import os
import json
import time
import re
from rich.console import Console

from autoapply.utils.key_pool import KeyPool, load_keys_from_env

console = Console()

DEFAULT_REQUEST_TIMEOUT = 20
DEFAULT_COOLDOWN_SECONDS = 60

_RATE_RE = re.compile(r"\b(rate[ _-]?limit\w*|429|quota|too many requests)\b")
_TIMEOUT_RE = re.compile(r"\b(timeout|timed out|deadline exceeded)\b")
_UNAVAILABLE_RE = re.compile(r"\b(503|502|504|service unavailable|overloaded|unavailable)\b")
_AUTH_RE = re.compile(r"\b(401|403|invalid[ _-]?api[ _-]?key|authentication|unauthorized|permission denied)\b")


def _extract_balanced_json(text: str) -> str | None:
    """Return the first balanced {...} block, honouring strings and escapes."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None  # unterminated — the response was truncated, do NOT guess


class LLMClient:
    """
    Manages LLM calls with automatic failover across providers and pooled keys.
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        llm_cfg = self.config.get("llm", {})
        self.call_delay = llm_cfg.get("call_delay_seconds", 2)
        self.request_timeout = llm_cfg.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT)

        # Build provider list from config or use defaults (Groq primary, Gemini secondary)
        raw_providers = llm_cfg.get("providers", [
            {"name": "groq", "model": "groq/llama-3.3-70b-versatile", "env_keys": ["GROQ_API_KEY"]},
            {"name": "gemini", "model": "gemini/gemini-2.0-flash", "env_keys": ["GEMINI_API_KEY"]},
            {"name": "github", "model": "github/gpt-4o", "env_keys": ["GITHUB_TOKEN"]},
        ])

        # Each provider gets a KeyPool built from one or more env var base names
        # (back-compat: singular "env_key" is still accepted alongside "env_keys").
        # Providers sharing the same physical keys SHARE one pool, otherwise a key
        # cooled down on one tier still looks fresh on another and re-triggers 429s.
        self.providers = []
        self.missing_providers = []
        shared_pools: dict[tuple, KeyPool] = {}
        for p in raw_providers:
            env_key_bases = p.get("env_keys")
            if not env_key_bases:
                single = p.get("env_key")
                env_key_bases = [single] if single else []

            entries = []
            for base_name in env_key_bases:
                entries.extend(load_keys_from_env(base_name))

            if not entries:
                self.missing_providers.append((p.get("name", "?"), list(env_key_bases)))
                continue

            pool_key = tuple(sorted(env_key_bases))
            if pool_key not in shared_pools:
                # min_interval enforces per-key call spacing, enabling true
                # parallelism across keys without a global sleep.
                shared_pools[pool_key] = KeyPool(entries, min_interval=self.call_delay)

            provider_entry = {
                "name": p["name"],
                "model": p["model"],
                "pool": shared_pools[pool_key],
            }
            # Support optional api_base (e.g. for Groq with non-standard models)
            if p.get("api_base"):
                provider_entry["api_base"] = p["api_base"]
            self.providers.append(provider_entry)

        for name, bases in self.missing_providers:
            console.print(
                f"[yellow]LLM provider '{name}' disabled — no keys found for {bases}[/yellow]"
            )

        if not self.providers:
            console.print("[bold red]ERROR:[/bold red] No LLM API keys found in environment!")
            console.print("Please set at least GROQ_API_KEY or GEMINI_API_KEY in your .env file")

        self._call_count = 0
        self._last_model: str | None = None

    @property
    def available(self) -> bool:
        """True if at least one LLM provider with a valid key is configured."""
        return bool(self.providers)

    @property
    def last_model(self) -> str | None:
        """Model that produced the most recent successful response."""
        return self._last_model

    def primary_model(self) -> str | None:
        return self.providers[0]["model"] if self.providers else None

    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        pin_model: str | None = None,
    ) -> str | None:
        """
        Send a chat message with automatic failover across providers.

        Args:
            messages: List of {"role": "user/assistant", "content": "..."}
            system_prompt: Optional system instruction
            max_tokens: Max tokens to generate
            temperature: Sampling temperature (lower = more deterministic)
            pin_model: Restrict to this model and fail rather than downgrade.

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

        providers = self.providers
        if pin_model:
            providers = [p for p in providers if p["model"] == pin_model]
            if not providers:
                console.print(f"[red]Pinned model '{pin_model}' has no configured keys[/red]")
                self._last_model = None
                return None

        for provider in providers:
            pool: KeyPool = provider["pool"]
            # Try every available key in this provider's pool before moving to the next provider
            for _attempt in range(len(pool)):
                # get_blocking (not get): with more concurrent workers than keys, wait
                # briefly for a busy-but-healthy key to free up instead of failing outright
                key_entry = pool.get_blocking(timeout=self.request_timeout + 5)
                if key_entry is None:
                    break  # every key in this provider is cooling down or bad

                try:
                    # NOTE: per-key minimum interval is enforced in KeyPool.get()
                    # via min_interval=call_delay — no global sleep needed here.
                    # This enables true parallel LLM calls across different keys.
                    completion_kwargs = dict(
                        model=provider["model"],
                        messages=full_messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        api_key=key_entry["key"],
                        timeout=self.request_timeout,
                    )
                    # Pass api_base for providers that need it (e.g. Groq with non-standard models)
                    if provider.get("api_base"):
                        completion_kwargs["api_base"] = provider["api_base"]

                    response = litellm.completion(**completion_kwargs)

                    self._call_count += 1
                    content = response.choices[0].message.content or ""
                    # One retry for transient empty responses (Gemini warmup issue)
                    if not content.strip():
                        time.sleep(2)
                        try:
                            response2 = litellm.completion(**completion_kwargs)
                            content = response2.choices[0].message.content or ""
                        except Exception:
                            pass
                    if not content.strip():
                        console.print(f"[yellow]LLM {provider['name']}:[/yellow] Empty response — trying next key")
                        # Cool it down, otherwise get_blocking hands back the same bad key.
                        pool.mark_cooldown(key_entry, 15)
                        continue
                    console.print(f"[dim]LLM ({provider['name']}):[/dim] ✓ {len(content)} chars")
                    self._last_model = provider["model"]
                    return content

                except Exception as e:
                    # Prefer the transport status code; message substrings misclassify
                    # (e.g. "author" once matched the auth branch and killed a good key).
                    status = getattr(e, "status_code", None) or getattr(
                        getattr(e, "response", None), "status_code", None
                    )
                    err_str = str(e).lower()
                    if status == 429 or (status is None and _RATE_RE.search(err_str)):
                        console.print(
                            f"[yellow]LLM {provider['name']}:[/yellow] Rate limited — cooling down this key, trying next"
                        )
                        pool.mark_cooldown(key_entry, DEFAULT_COOLDOWN_SECONDS)
                    elif status in (408, 504) or (status is None and _TIMEOUT_RE.search(err_str)):
                        console.print(
                            f"[yellow]LLM {provider['name']}:[/yellow] Request timed out — cooling down this key, trying next"
                        )
                        pool.mark_cooldown(key_entry, DEFAULT_COOLDOWN_SECONDS)
                    elif status in (500, 502, 503) or (status is None and _UNAVAILABLE_RE.search(err_str)):
                        console.print(
                            f"[yellow]LLM {provider['name']}:[/yellow] Service unavailable — cooling down this key, trying next"
                        )
                        pool.mark_cooldown(key_entry, 10)
                    elif status in (401, 403) or (status is None and _AUTH_RE.search(err_str)):
                        console.print(
                            f"[yellow]LLM {provider['name']}:[/yellow] Auth error — disabling this key"
                        )
                        pool.mark_bad(key_entry)
                    else:
                        console.print(
                            f"[yellow]LLM {provider['name']}:[/yellow] {type(e).__name__}: {str(e)[:100]}"
                        )
                        pool.mark_cooldown(key_entry, 10)
                    continue
                finally:
                    pool.release(key_entry)

            if pin_model:
                # Pinned scoring must not silently fall through to a weaker model —
                # a mixed-model score column is uncomparable against fixed thresholds.
                console.print(f"[red]Pinned provider '{pin_model}' exhausted — failing (retryable)[/red]")
                self._last_model = None
                return None

        console.print("[red]All LLM providers/keys failed for this call[/red]")
        self._last_model = None
        return None

    def chat_json(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        pin_model: str | None = None,
        required_keys: tuple[str, ...] | None = None,
    ) -> dict | None:
        """
        Send a chat message expecting a JSON response.

        A truncated response is an ERROR, never something to repair. Brace-balancing
        a cut-off `{"score": 7` silently yields 7 instead of 72, which is worse than
        no score at all.

        Returns:
            Parsed dict, or None if parsing/validation failed.
        """
        raw = self.chat(messages, system_prompt, max_tokens, temperature, pin_model=pin_model)
        if not raw:
            return None

        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            raw = raw.strip()

        # Reasoning models (Qwen, DeepSeek) wrap output in <think>...</think>
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        if not raw or ("<think>" in raw and "</think>" not in raw):
            return None

        parsed = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            block = _extract_balanced_json(raw)
            if block:
                try:
                    parsed = json.loads(block)
                except json.JSONDecodeError:
                    parsed = None

        if not isinstance(parsed, dict):
            console.print(f"[yellow]JSON parse failed (truncated or malformed):[/yellow] {raw[:200]}")
            return None

        if required_keys:
            missing = [k for k in required_keys if k not in parsed]
            if missing:
                console.print(f"[yellow]JSON missing required keys {missing}[/yellow]")
                return None

        return parsed

