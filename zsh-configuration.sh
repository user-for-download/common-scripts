#!/usr/bin/env bash
# install.sh - Script to set up Zsh with a complete Antigen/Prezto/P10k configuration

set -euo pipefail # Exit on error, unset variables, or pipeline failures

# --- Helper Functions ---
# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to print colored messages
print_message() {
  local message=$1
  local color=${2:-$NC}
  echo -e "${color}${message}${NC}"
}

# Function to check if a command exists
command_exists() {
  command -v "$1" >/dev/null 2>&1
}

# Function to prompt yes/no with default Yes
prompt_yes_no() {
  local prompt_msg=$1
  local response
  read -rp "$prompt_msg [Y/n]: " response
  [[ "$response" =~ ^[Nn]$ ]] && return 1 || return 0
}

# Function to detect package manager and install packages
install_packages() {
  local packages=("$@")
  print_message "Attempting to install: ${packages[*]}" "$BLUE"
  if command_exists apt-get; then
    sudo apt-get update && sudo apt-get install -y "${packages[@]}"
  elif command_exists dnf; then
    sudo dnf install -y "${packages[@]}"
  elif command_exists yum; then
    sudo yum install -y "${packages[@]}"
  elif command_exists pacman; then
    sudo pacman -S --noconfirm "${packages[@]}"
  elif command_exists brew; then
    brew install "${packages[@]}"
  else
    print_message "Could not detect package manager. Please install packages manually: ${packages[*]}" "$RED"
    return 1
  fi
  return 0
}

# --- Main Script ---

# Do not run as root
if [ "$(id -u)" -eq 0 ]; then
  print_message "This script should not be run as root. Please run as a regular user." "$RED"
  exit 1
fi

print_message "Starting Zsh configuration installation..." "$BLUE"

# 1. Install prerequisite system packages
if ! command_exists zsh || ! command_exists git || ! command_exists curl; then
  print_message "Installing prerequisites (zsh, git, curl, wget)..." "$YELLOW"
  if ! install_packages zsh git curl wget; then
    print_message "Failed to install required packages. Please install them manually and run this script again." "$RED"
    exit 1
  fi
else
  print_message "Prerequisites (zsh, git, curl) are already installed." "$GREEN"
fi

# Store Zsh path for later use
ZSH_PATH="$(command -v zsh)"

# 2. Create directory structure required by .zshrc
mkdir -p "$HOME/.local/bin" "$HOME/.local/etc" "$HOME/bin"
print_message "Created directory structure for local configuration files." "$GREEN"

# 3. Create placeholder files for custom local settings
touch "$HOME/.zshrc.local" "$HOME/.local/etc/init.sh" "$HOME/.local/etc/local.zsh" "$HOME/.local/etc/config.zsh"
print_message "Created empty placeholder files for your custom settings." "$GREEN"

# 4. Set Zsh as default shell
if [ "${SHELL##*/}" != "zsh" ]; then
  print_message "Setting Zsh as your default shell..." "$YELLOW"
  print_message "Note: You may be prompted for your password." "$YELLOW"
  if chsh -s "$ZSH_PATH" "$USER" 2>/dev/null || sudo chsh -s "$ZSH_PATH" "$USER"; then
    print_message "Zsh has been set as your default shell. Log out and log back in for changes to take effect." "$GREEN"
  else
    print_message "Failed to set Zsh as default shell. Please set it manually: chsh -s $ZSH_PATH" "$RED"
  fi
else
  print_message "Zsh is already your default shell." "$GREEN"
fi

# 5. Install optional external tools
if prompt_yes_no "Install optional tools like fzf (fuzzy finder)?"; then
  # Install fzf for fuzzy finding
  if ! command_exists fzf; then
    if prompt_yes_no "  -> Install fzf (fuzzy finder)?"; then
        if git clone --depth 1 https://github.com/junegunn/fzf.git "$HOME/.fzf"; then
            "$HOME/.fzf/install" --all --no-update-rc # --no-update-rc is important! Our .zshrc handles sourcing.
            print_message "fzf installed successfully." "$GREEN"
        else
            print_message "Failed to clone fzf repository." "$RED"
        fi
    fi
  else
    print_message "fzf is already installed." "$GREEN"
  fi
fi

# 6. Backup existing .zshrc
if [ -f "$HOME/.zshrc" ] && [ ! -L "$HOME/.zshrc" ]; then
  BACKUP_FILE="$HOME/.zshrc.backup.$(date +%Y%m%d%H%M%S)"
  print_message "Creating backup of existing .zshrc to ${BACKUP_FILE}" "$YELLOW"
  cp "$HOME/.zshrc" "$BACKUP_FILE"
fi

# 7. Create the final .zshrc file
print_message "Creating new .zshrc file..." "$BLUE"
# This heredoc contains the complete, corrected .zshrc content.
cat > "$HOME/.zshrc" << 'EOF'
# Enable Powerlevel10k instant prompt. Should stay close to the top of ~/.zshrc.
# Initialization code that may require console input (password prompts, [y/n]
# confirmations, etc.) must go above this block; everything else may go below.
if [[ -r "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh" ]]; then
  source "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh"
fi

###############################################################################
# ZSH Configuration with Antigen & Prezto modules
###############################################################################

# Antigen: https://github.com/zsh-users/antigen
ANTIGEN="$HOME/.local/bin/antigen.zsh"

# Install antigen.zsh if not exist
if [ ! -f "$ANTIGEN" ]; then
  echo "Installing antigen ..."
  mkdir -p "$HOME/.local/bin"
  URL="http://git.io/antigen"
  TMPFILE="/tmp/antigen.zsh"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TMPFILE"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$URL" -O "$TMPFILE"
  else
    echo "ERROR: please install curl or wget before installation!"
    exit 1
  fi
  if [ $? -ne 0 ]; then
    echo "ERROR: downloading antigen.zsh from $URL failed!"
    exit 1
  fi
  echo "Moving $TMPFILE to $ANTIGEN"
  mv "$TMPFILE" "$ANTIGEN"
fi

# Custom binds and aliases
alias g='openssl rand -base64 24'
alias gg="tr -cd '[:alnum:]' < /dev/urandom | fold -w 32 | head -n 1"
alias ls='ls --color=auto'
alias ll='ls -la'
alias la='ls -A'
alias l='ls -CF'
alias grep='grep --color=auto'
alias fgrep='fgrep --color=auto'
alias egrep='egrep --color=auto'
alias ..='cd ..'
alias ...='cd ../..'
alias ....='cd ../../..'

# Load local bash/zsh compatible settings
[ -f "$HOME/.local/etc/init.sh" ] && source "$HOME/.local/etc/init.sh"

# Exit for non-interactive shell
[[ $- != *i* ]] && return

# WSL compatibility fix for BG_NICE
[ -d "/mnt/c" ] && [[ "$(uname -a)" == *Microsoft* ]] && unsetopt BG_NICE

# History configuration
HISTFILE="$HOME/.zsh_history"
HISTSIZE=50000
SAVEHIST=10000

# Initialize antigen
source "$ANTIGEN"

# Setup directory stack
DIRSTACKSIZE=20
setopt autopushd pushdminus pushdsilent pushdtohome pushdignoredups cdablevars
alias d='dirs -v | head -10'

# Disable correction features
unsetopt correct_all correct
DISABLE_CORRECTION="true"

# Enable 256 colors for auto-suggestions
export TERM="xterm-256color"
ZSH_AUTOSUGGEST_USE_ASYNC=1
ZSH_AUTOSUGGEST_STRATEGY=(history completion)

# Prezto module configurations
zstyle ':prezto:*:*' color 'yes'
zstyle ':prezto:module:editor' key-bindings 'emacs'
zstyle ':prezto:module:git:alias' skip 'yes'
zstyle ':prezto:module:terminal' auto-title 'yes'
zstyle ':prezto:module:autosuggestions' color 'yes'
zstyle ':prezto:module:python' autovenv 'yes'
zstyle ':prezto:load' pmodule \
  'environment' \
  'editor' \
  'history' \
  'git' \
  'utility' \
  'completion' \
  'history-substring-search' \
  'autosuggestions'

# Initialize prezto via antigen
antigen use prezto

# Load Powerlevel10k theme (This will be downloaded by Antigen automatically)
antigen theme romkatv/powerlevel10k

# Load other plugins (bundles)
antigen bundle rupa/z
antigen bundle Vifon/deer
antigen bundle zdharma-continuum/fast-syntax-highlighting
antigen bundle willghatch/zsh-cdr
antigen bundle zsh-users/zsh-completions

# Check for fzf and load if available
if command -v fzf >/dev/null 2>&1; then
  antigen bundle junegunn/fzf
  [ -f ~/.fzf.zsh ] && source ~/.fzf.zsh
fi

# Syntax highlighting configuration for fast-syntax-highlighting
ZSH_HIGHLIGHT_HIGHLIGHTERS=(main brackets pattern)
typeset -A ZSH_HIGHLIGHT_STYLES
ZSH_HIGHLIGHT_STYLES[default]=none
ZSH_HIGHLIGHT_STYLES[unknown-token]=fg=009
ZSH_HIGHLIGHT_STYLES[reserved-word]=fg=009,standout
ZSH_HIGHLIGHT_STYLES[alias]=fg=blue,bold
ZSH_HIGHLIGHT_STYLES[builtin]=fg=blue,bold
ZSH_HIGHLIGHT_STYLES[function]=fg=blue,bold
ZSH_HIGHLIGHT_STYLES[command]=fg=white,bold
ZSH_HIGHLIGHT_STYLES[path]=fg=214,underline

# Load local configurations if available
[ -f "$HOME/.local/etc/config.zsh" ] && source "$HOME/.local/etc/config.zsh"
[ -f "$HOME/.local/etc/local.zsh" ] && source "$HOME/.local/etc/local.zsh"
[ -f "$HOME/.zshrc.local" ] && source "$HOME/.zshrc.local"

# Apply all antigen configurations (this is what loads everything)
antigen apply

# Additional shell options for history management
setopt prompt_subst BANG_HIST INC_APPEND_HISTORY
setopt HIST_EXPIRE_DUPS_FIRST HIST_IGNORE_DUPS HIST_IGNORE_ALL_DUPS
setopt HIST_FIND_NO_DUPS HIST_IGNORE_SPACE HIST_SAVE_NO_DUPS
setopt HIST_REDUCE_BLANKS HIST_VERIFY

# Keybindings
bindkey -s '\ee' 'vim\n'
bindkey '\eh' backward-char
bindkey '\el' forward-char
bindkey '\ej' down-line-or-history
bindkey '\ek' up-line-or-history
bindkey '\eH' backward-word
bindkey '\eL' forward-word
bindkey '\eJ' beginning-of-line
bindkey '\eK' end-of-line
bindkey -s '\eo' 'cd ..\n'
bindkey '\ev' deer # For the 'deer' plugin

# To customize prompt, run `p10k configure` or edit ~/.p10k.zsh.
[[ ! -f ~/.p10k.zsh ]] || source ~/.p10k.zsh

# Add user's local bin to PATH
export PATH="$HOME/.local/bin:$PATH"
EOF

print_message ".zshrc file has been created." "$GREEN"
print_message "Zsh configuration has been successfully installed!" "$GREEN"
print_message "----------------------------------------------------------------" "$BLUE"
print_message "NEXT STEPS:"
print_message "1. Log out and log back in, or run 'exec zsh' to start."
print_message "2. On first launch, Antigen will download all necessary plugins."
print_message "3. After the new shell starts, run 'p10k configure' to set up your prompt."
print_message "----------------------------------------------------------------" "$BLUE"

if prompt_yes_no "Would you like to start Zsh now?"; then
  exec "$ZSH_PATH" -l
fi