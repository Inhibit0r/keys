#!/bin/sh
# keys: install into ~/.local and load the active keys in every new shell. Safe to rerun.
set -eu
RAW="https://raw.githubusercontent.com/Inhibit0r/keys/main/keys.py"
DIR="$HOME/.local/share/keys"
BIN="$HOME/.local/bin"
command -v python3 >/dev/null || { echo "keys: python3 3.10+ is required" >&2; exit 1; }
mkdir -p "$DIR" "$BIN"
curl -fsSL "$RAW" -o "$DIR/keys.py"
printf '#!/bin/sh\nexec python3 "%s/keys.py" "$@"\n' "$DIR" > "$BIN/keys"
chmod +x "$BIN/keys"

case "${SHELL##*/}" in
  zsh) RC="$HOME/.zshenv" ;;
  bash) RC="$HOME/.bashrc" ;;
  *) RC="$HOME/.profile" ;;
esac
MARK="# keys: active Tavily / Firecrawl keys"
if ! grep -qF "$MARK" "$RC" 2>/dev/null; then
  cat >> "$RC" <<'EOF'

# keys: active Tavily / Firecrawl keys
for f in "$HOME/.config/tavily/env" "$HOME/.config/firecrawl/env"; do
  if [ -r "$f" ]; then . "$f"; fi
done
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
EOF
fi
echo "keys installed: $BIN/keys (shell setup in $RC)"
echo "open a new terminal and run: keys"
