"""Mock subprocess for benchmark tests.

Simulates a long-running task that outputs events continuously.
Accepts duration as first argument (default 300 seconds).
"""

import sys
import time


def main():
    """Output events continuously for specified duration.

    Each event is a simple JSON line. Output rate: ~20 events/sec.
    """
    # Accept duration as first argument (default 300 seconds)
    duration_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    interval_seconds = 0.02  # 50 events/sec (realistic for benchmark)

    start_time = time.time()
    event_count = 0

    while time.time() - start_time < duration_seconds:
        # Output a simple event
        print(
            f'{{"type": "stdout", "seq": {event_count}, "data": "benchmark event {event_count}"}}',
            flush=True,
        )
        event_count += 1

        # Sleep to control output rate
        time.sleep(interval_seconds)

    # Output final summary
    print(f'{{"type": "result", "total_events": {event_count}}}', flush=True)


if __name__ == "__main__":
    main()
