#!/usr/bin/env bash
#
# Creates a tmux session with:
#   window "monitor": 3 panes, each running a watch command
#   window "shell": plain command prompt

SESSION="work"

# Start new session, first window named "monitor"
tmux new-session -d -s "$SESSION" -n monitor

# Pane 0: already exists, run first watch command
tmux send-keys -t "$SESSION:monitor" 'watch -n 1 --no-wrap "docker container ls -a"' C-m

# Split vertically to create pane 1, run second watch command
tmux split-window -h -t "$SESSION:monitor"
tmux send-keys -t "$SESSION:monitor" 'watch -n 1 --no-wrap "df -h"' C-m

# Split pane 1 horizontally to create pane 2, run third watch command
tmux split-window -v -t "$SESSION:monitor"
tmux send-keys -t "$SESSION:monitor" 'watch -n 1 --no-wrap "free -h"' C-m

# Arrange panes evenly
tmux select-layout -t "$SESSION:monitor" tiled

# Create second window "shell" with a plain prompt
tmux new-window -t "$SESSION" -n shell

# Attach to the session, starting on the monitor window
tmux select-window -t "$SESSION:monitor"
tmux attach-session -t "$SESSION"
