"""Restore tool-configured environment values after automatic compaction."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_state import SessionState


if __name__ == '__main__':
    event = json.load(sys.stdin)
    if event.get('hook_event_name') == 'SessionStart' and event.get('source') == 'compact':
        state = SessionState(event.get('session_id'))
        values = state.list()
        print('Environment overrides set through blocking-shell for this conversation:')
        print(json.dumps(values, ensure_ascii=False, separators=(',', ':')))
        bashrc = state.bashrc()
        if bashrc is not None:
            print('Additional blocking-shell bashrc for this conversation: ' + json.dumps(bashrc))
