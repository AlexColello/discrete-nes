#!/bin/bash
# PostToolUse hook: auto-run verify_schematics.py after generate_*.py
#
# Reads the JSON event from stdin, checks if the Bash command ran a
# generate_*.py script, and if so, finds the matching board directory
# and runs its verify_schematics.py.

# Read stdin JSON
INPUT=$(cat)

# Extract the command that was run
COMMAND=$(echo "$INPUT" | python -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('tool_input', {}).get('command', ''))
except:
    print('')
" 2>/dev/null)

# Check if the command ran a generate_*.py script
if echo "$COMMAND" | grep -qE 'generate_[a-zA-Z_]+\.py'; then
    # Extract the board directory from the script path
    # Pattern: .../boards/<board-name>/scripts/generate_*.py
    SCRIPT_PATH=$(echo "$COMMAND" | grep -oE '[^ ]*generate_[a-zA-Z_]+\.py' | head -1)

    if [ -n "$SCRIPT_PATH" ]; then
        # Find the board directory (parent of scripts/)
        SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
        BOARD_DIR=$(dirname "$SCRIPT_DIR")

        # Check if verify_schematics.py exists in the same scripts directory
        VERIFY_SCRIPT="$SCRIPT_DIR/verify_schematics.py"
        if [ -f "$VERIFY_SCRIPT" ]; then
            echo ""
            echo "=== Auto-verify hook: running $VERIFY_SCRIPT ==="

            # Use the same Python that ran the generate script
            # Try to find the venv python
            PROJECT_DIR="$CLAUDE_PROJECT_DIR"
            if [ -z "$PROJECT_DIR" ]; then
                PROJECT_DIR=$(cd "$BOARD_DIR/../.." 2>/dev/null && pwd)
            fi

            VENV_PYTHON="$PROJECT_DIR/venv/Scripts/python"
            if [ ! -f "$VENV_PYTHON" ]; then
                VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
            fi
            if [ ! -f "$VENV_PYTHON" ]; then
                VENV_PYTHON="python"
            fi

            cd "$BOARD_DIR" && "$VENV_PYTHON" "$VERIFY_SCRIPT" --no-erc 2>&1
        fi
    fi
fi
