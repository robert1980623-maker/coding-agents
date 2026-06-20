#!/usr/bin/env python3
"""Mock Claude CLI that outputs stream-json format.

Simulates claude CLI behavior for testing without API key.
"""

import json
import sys
import time


def main():
    # Parse arguments (minimal)
    args = sys.argv[1:]

    # Check for --version
    if "--version" in args:
        print("claude-cli 1.0.0-mock")
        sys.exit(0)

    # Simulate stream-json output
    # Event 1: system message
    print(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "claude-3-haiku-20240307",
            }
        )
    )

    # Event 2: assistant message
    print(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "This is a mock response from the Claude CLI.",
                        }
                    ],
                },
            }
        )
    )

    # Event 3: result with cost/usage
    print(
        json.dumps(
            {
                "type": "result",
                "total_cost_usd": 0.00123,
                "model": "claude-3-haiku-20240307",
                "usage": {
                    "input_tokens": 150,
                    "output_tokens": 25,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            }
        )
    )

    # Exit successfully
    sys.exit(0)


if __name__ == "__main__":
    main()
