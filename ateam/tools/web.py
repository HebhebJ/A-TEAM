"""Web tools: search and fetch documentation."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

from .base import Tool


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web for documentation, error solutions, package info, or anything else. "
        "Use this when you're stuck, need to find the right command/flag, or want to check docs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (e.g. 'npm create vite non-interactive flags', 'tailwindcss v3 install')",
            },
            "num_results": {
                "type": "integer",
                "description": "Number of results to return (default 5, max 10)",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    async def execute(self, arguments: dict[str, Any], project_path: Path) -> str:
        query = arguments["query"]
        num_results = min(int(arguments.get("num_results", 5)), 10)

        # DuckDuckGo HTML lite — no API key needed
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": "Mozilla/5.0 (compatible; A-TEAM-agent/1.0)"},
                follow_redirects=True,
            ) as client:
                response = await client.get(url)

            if response.status_code != 200:
                return f"Search failed: HTTP {response.status_code}"

            results = _parse_ddg_results(response.text, num_results)
            if not results:
                return f"No results found for: {query}"

            lines = [f"Search results for: {query}\n"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['title']}")
                lines.append(f"   {r['url']}")
                lines.append(f"   {r['snippet']}")
                lines.append("")
            return "\n".join(lines)

        except httpx.TimeoutException:
            return "Search timed out. Try again or rephrase your query."
        except Exception as e:
            return f"Search error: {e}"


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = (
        "Fetch a web page and return its text content. "
        "Use to read documentation, GitHub READMEs, npm package pages, Stack Overflow answers, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch",
            },
            "max_length": {
                "type": "integer",
                "description": "Max characters to return (default 8000)",
                "default": 8000,
            },
        },
        "required": ["url"],
    }

    async def execute(self, arguments: dict[str, Any], project_path: Path) -> str:
        url = arguments["url"]
        max_length = min(int(arguments.get("max_length", 8000)), 20000)

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": "Mozilla/5.0 (compatible; A-TEAM-agent/1.0)"},
                follow_redirects=True,
            ) as client:
                response = await client.get(url)

            if response.status_code != 200:
                return f"Fetch failed: HTTP {response.status_code}"

            content_type = response.headers.get("content-type", "")
            if "html" in content_type:
                text = _html_to_text(response.text)
            else:
                text = response.text

            if len(text) > max_length:
                text = text[:max_length] + f"\n\n... [truncated at {max_length} chars]"

            return f"Content from {url}:\n\n{text}"

        except httpx.TimeoutException:
            return f"Fetch timed out: {url}"
        except Exception as e:
            return f"Fetch error: {e}"


def _parse_ddg_results(html_text: str, num: int) -> list[dict[str, str]]:
    """Parse DuckDuckGo HTML search results."""
    results = []

    # Extract result blocks
    blocks = re.findall(
        r'<div class="result[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html_text,
        re.DOTALL,
    )

    for block in blocks[:num * 2]:  # grab extra in case some parse badly
        title_m = re.search(r'class="result__title"[^>]*>.*?<a[^>]*>(.*?)</a>', block, re.DOTALL)
        url_m = re.search(r'class="result__url"[^>]*>\s*(.*?)\s*</[^>]+>', block, re.DOTALL)
        snippet_m = re.search(r'class="result__snippet"[^>]*>(.*?)</[^>]+>', block, re.DOTALL)

        if not title_m:
            continue

        title = _strip_tags(title_m.group(1)).strip()
        url = _strip_tags(url_m.group(1)).strip() if url_m else ""
        snippet = _strip_tags(snippet_m.group(1)).strip() if snippet_m else ""

        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= num:
                break

    return results


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _html_to_text(html_text: str) -> str:
    """Convert HTML to readable plain text."""
    # Remove script/style blocks
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    # Replace block elements with newlines
    text = re.sub(r"<(br|p|div|h[1-6]|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
