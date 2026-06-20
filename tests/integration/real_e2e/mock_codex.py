#!/usr/bin/env python3
"""Mock Codex CLI that outputs --json format.

Simulates codex CLI behavior for testing without API key.
"""

import json
import sys


def main():
    # Parse arguments (minimal)
    args = sys.argv[1:]

    # Simulate --json output
    # Event 1: item.completed (agent message)
    print(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "This is a mock response from the Codex CLI.",
                },
            }
        )
    )

    # Event 2: turn.completed with usage
    print(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 30,
                },
            }
        )
    )

    # Exit successfully
    sys.exit(0)


if __name__ == "__main__":
    main()
