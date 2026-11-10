# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Execution IDs must remain unique after their old log directory is removed."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_state import SessionState


with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, BLOCKING_SHELL_STATE_DIR=directory):
    state = SessionState('one')
    logs = Path(directory)
    with patch('session_state.new_id', return_value='old-ref'):
        state.accept({'cmd': 'old'}, logs, {}).rmdir()

    def allocate(is_used):
        assert is_used('old-ref')
        assert not is_used('new-ref')
        return 'new-ref'

    with patch('session_state.new_id', side_effect=allocate):
        result = state.accept({'cmd': 'new'}, logs, {})
    assert result.name == 'blocking-shell-new-ref'
    assert state.execution(None)['run_ref'] == 'new-ref'
    assert state.execution('old-ref')['arguments']['cmd'] == 'old'
    assert state.command()['cmd'] == 'new'
print('durable ID collision avoidance and atomic acceptance: passed')
