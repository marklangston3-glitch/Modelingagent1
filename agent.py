"""
agent.py – EDGAR Financial Modeling Agent powered by Claude.

Usage:
    python agent.py                          # interactive REPL
    python agent.py "What is Apple's CIK?"  # single prompt
    python agent.py --help

Environment variables:
    ANTHROPIC_API_KEY   Required. Your Anthropic API key.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import anthropic

import tools as tool_module

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL      = "claude-opus-4-7"   # swap to claude-haiku-4-5-20251001 for speed
MAX_TOKENS = 4096
SYSTEM_PROMPT = """\
You are a financial analyst assistant with access to the SEC EDGAR database.
You can look up company filings, retrieve financial data, and export results
to Excel.  When a user asks a financial question that requires data, always
use the available tools to fetch accurate information rather than guessing.
Present numbers clearly and explain what they mean in plain English."""


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent(user_message: str, client: anthropic.Anthropic) -> str:
    """
    Run the agentic tool-use loop for a single user message.

    Continues calling Claude (and executing tools) until the model produces
    a final text response with no pending tool calls.
    """
    messages: list[dict] = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tool_module.TOOLS,
            messages=messages,
        )

        # Append assistant turn to history
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract the final text block
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "(no text response)"

        if response.stop_reason != "tool_use":
            return f"(unexpected stop_reason: {response.stop_reason})"

        # Process every tool_use block in this turn
        tool_results: list[dict] = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            print(f"  [tool] {block.name}({json.dumps(block.input, default=str)[:120]})")
            result_str = tool_module.dispatch(block.name, block.input)

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     result_str,
            })

        # Feed results back to the model
        messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

def repl(client: anthropic.Anthropic) -> None:
    print("EDGAR Financial Modeling Agent  (type 'exit' or Ctrl-D to quit)\n")
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Goodbye.")
            break

        print("Agent: ", end="", flush=True)
        answer = run_agent(user_input, client)
        print(answer)
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="EDGAR Financial Modeling Agent")
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Optional one-shot prompt. If omitted, starts an interactive REPL.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Error: ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic(api_key=api_key)

    if args.prompt:
        print(run_agent(args.prompt, client))
    else:
        repl(client)


if __name__ == "__main__":
    main()
