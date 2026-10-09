"""Restore tool-configured environment values after automatic compaction."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_environment import SessionEnvironment


if __name__ == '__main__':
    event = json.load(sys.stdin)
    if event.get('hook_event_name') == 'SessionStart' and event.get('source') == 'compact':
        values = SessionEnvironment(event.get('session_id')).list()
        print('Environment overrides set through blocking-shell for this conversation:')
        print(json.dumps(values, ensure_ascii=False, separators=(',', ':')))
