#!/usr/bin/env bash
# install.sh - Script to set up Zsh with optimal configuration

set -euo pipefail  # Exit on error, unset variables, or pipeline failures

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to print colored messages
print_message() {
  local message=$1
  local color=$2
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
  if [[ "$response" =~ ^[Yy]$ ]] || [[ -z "$response" ]]; then
    return 0
  else
    return 1
  fi
}

# Function to install a plugin from git if not already installed
# Usage: install_plugin <git_repo_url> <target_directory> <config_append_lines>
install_plugin() {
  local repo_url=$1
  local target_dir=$2
  local config_lines=$3

  if [ -d "${target_dir}" ]; then
    print_message "Plugin at ${target_dir} is already installed. Skipping." "$YELLOW"
  else
    print_message "Installing plugin from ${repo_url}..." "$BLUE"
    git clone "${repo_url}" "${target_dir}"
    # Append configuration lines if provided
    if [ -n "${config_lines}" ]; then
      echo -e "\n# Plugin configuration" >> "$HOME/.zshrc.local"
      echo -e "${config_lines}" >> "$HOME/.zshrc.local"
    fi
    print_message "Plugin installed successfully in ${target_dir}." "$GREEN"
  fi
}

# Do not run as root
if [ "$(id -u)" -eq 0 ]; then
  print_message "This script should not be run as root. Please run as a regular user." "$RED"
  exit 1
fi

print_message "Starting Zsh configuration installation..." "$BLUE"

# Check and install Zsh if needed
if ! command_exists zsh; then
  print_message "Zsh is not installed. Installing Zsh..." "$YELLOW"
  if command_exists dnf; then
    sudo dnf install -y zsh util-linux-user git
  elif command_exists yum; then
    sudo yum install -y zsh
  elif command_exists apt; then
    sudo apt update && sudo apt install -y zsh
  elif command_exists pacman; then
    sudo pacman -S --noconfirm zsh
  elif command_exists brew; then
    brew install zsh
  else
    print_message "Could not install Zsh automatically. Please install Zsh manually and run this script again." "$RED"
    exit 1
  fi
else
  print_message "Zsh is already installed." "$GREEN"
fi

# Store Zsh path for later use
ZSH_PATH="$(command -v zsh)"

# Create empty local config file if not present
touch "$HOME/.zshrc.local"
print_message "Created empty .zshrc.local for custom machine-specific settings." "$GREEN"

# Create ~/bin directory if missing
if [ ! -d "$HOME/bin" ]; then
  mkdir -p "$HOME/bin"
  print_message "Created ~/bin directory for personal scripts." "$GREEN"
fi

# Set Zsh as default shell if it's not already
if [ "$SHELL" != "$ZSH_PATH" ]; then
  print_message "Setting Zsh as your default shell..." "$YELLOW"
  print_message "Note: You may be prompted for your password." "$YELLOW"
  sudo chsh -s "$ZSH_PATH" "$USER"
  if [ $? -eq 0 ]; then
    print_message "Zsh has been set as your default shell. Log out and back in for changes to take effect." "$GREEN"
  else
    print_message "Failed to set Zsh as default shell. Set it manually with: chsh -s $ZSH_PATH" "$RED"
  fi
else
  print_message "Zsh is already your default shell." "$GREEN"
fi

# Install optional extra tools if desired
if prompt_yes_no "Would you like to install additional tools?"; then

  # Install Oh-My-Zsh
  if prompt_yes_no "Would you like to install Oh-My-Zsh?"; then
    print_message "Installing Oh-My-Zsh..." "$BLUE"
    if [ -d "$HOME/.oh-my-zsh" ]; then
      print_message "Oh-My-Zsh is already installed. Skipping installation." "$YELLOW"
    else
      sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended
      # Attempt to restore our .zshrc if overwritten
      if cp "$HOME/.zshrc.backup."* "$HOME/.zshrc" 2>/dev/null; then
        print_message "Restored previous .zshrc." "$GREEN"
      else
        print_message "Note: Failed to restore .zshrc after Oh-My-Zsh installation. You may need to merge changes manually." "$YELLOW"
      fi
      # Uncomment necessary Oh-My-Zsh lines
      sed -i 's/# export ZSH/export ZSH/g' "$HOME/.zshrc"
      sed -i 's/# ZSH_THEME/ZSH_THEME/g' "$HOME/.zshrc"
      sed -i 's/# plugins=/plugins=/g' "$HOME/.zshrc"
      sed -i 's/# source \$ZSH/source \$ZSH/g' "$HOME/.zshrc"
      print_message "Oh-My-Zsh installed successfully." "$GREEN"
    fi
  fi

  # Install syntax highlighting plugin
  if prompt_yes_no "Would you like to install the syntax highlighting plugin?"; then
    install_plugin "https://github.com/zsh-users/zsh-syntax-highlighting.git" "$HOME/.zsh-syntax-highlighting" "source \$HOME/.zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
  fi

  # Install autosuggestions plugin
  if prompt_yes_no "Would you like to install the autosuggestions plugin?"; then
    install_plugin "https://github.com/zsh-users/zsh-autosuggestions.git" "$HOME/.zsh-autosuggestions" "source \$HOME/.zsh-autosuggestions/zsh-autosuggestions.zsh"
  fi
fi

# Backup existing .zshrc if it exists
if [ -f "$HOME/.zshrc" ]; then
  BACKUP_FILE="$HOME/.zshrc.backup.$(date +%Y%m%d%H%M%S)"
  print_message "Creating backup of existing .zshrc to ${BACKUP_FILE}" "$YELLOW"
  cp "$HOME/.zshrc" "$BACKUP_FILE"
fi

# Create new .zshrc file with the Zsh configuration
print_message "Creating new .zshrc file..." "$BLUE"
cat > "$HOME/.zshrc" << 'EOF'
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
    curl -L "$URL" -o "$TMPFILE"
  elif command -v wget >/dev/null 2>&1; then
    wget "$URL" -O "$TMPFILE"
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

# Load local bash/zsh compatible settings
INIT_SH_NOFUN=1
INIT_SH_NOLOG=1
DISABLE_Z_PLUGIN=1
[ -f "$HOME/.local/etc/init.sh" ] && source "$HOME/.local/etc/init.sh"

# Exit for non-interactive shell
[[ $- != *i* ]] && return

# WSL compatibility fix for BG_NICE
[ -d "/mnt/c" ] && [[ "$(uname -a)" == *Microsoft* ]] && unsetopt BG_NICE

# Initialize command prompt
export PS1="%n@%m:%~%# "

# Initialize antigen
source "$ANTIGEN"

# Setup directory stack
DIRSTACKSIZE=10
setopt autopushd pushdminus pushdsilent pushdtohome pushdignoredups cdablevars
alias d='dirs -v | head -10'

# Disable correction features
unsetopt correct_all correct
DISABLE_CORRECTION="true"

# Enable 256 colors for auto-suggestions
export TERM="xterm-256color"
ZSH_AUTOSUGGEST_USE_ASYNC=1

# Prezto module configurations
zstyle ':prezto:*:*' color 'yes'
zstyle ':prezto:module:editor' key-bindings 'emacs'
zstyle ':prezto:module:git:alias' skip 'yes'
zstyle ':prezto:module:prompt' theme 'redhat'
zstyle ':prezto:module:prompt' pwd-length 'short'
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
  'autosuggestions' \
  'prompt'

# Initialize prezto via antigen
antigen use prezto

# Default bundles
antigen bundle rupa/z z.sh
antigen bundle Vifon/deer
antigen bundle zdharma-continuum/fast-syntax-highlighting
antigen bundle willghatch/zsh-cdr

# Login shell scripts
if [[ -o login ]]; then
  [ -f "$HOME/.local/etc/login.sh" ] && source "$HOME/.local/etc/login.sh"
  [ -f "$HOME/.local/etc/login.zsh" ] && source "$HOME/.local/etc/login.zsh"
fi

# Syntax highlighting configuration
ZSH_HIGHLIGHT_HIGHLIGHTERS=(main brackets pattern)
typeset -A ZSH_HIGHLIGHT_STYLES
ZSH_HIGHLIGHT_STYLES[default]=none
ZSH_HIGHLIGHT_STYLES[unknown-token]=fg=009
ZSH_HIGHLIGHT_STYLES[reserved-word]=fg=009,standout
ZSH_HIGHLIGHT_STYLES[alias]=fg=blue,bold
ZSH_HIGHLIGHT_STYLES[builtin]=fg=blue,bold
ZSH_HIGHLIGHT_STYLES[function]=fg=blue,bold
ZSH_HIGHLIGHT_STYLES[command]=fg=white,bold
ZSH_HIGHLIGHT_STYLES[precommand]=fg=white,underline
ZSH_HIGHLIGHT_STYLES[commandseparator]=none
ZSH_HIGHLIGHT_STYLES[hashed-command]=fg=009
ZSH_HIGHLIGHT_STYLES[path]=fg=214,underline
ZSH_HIGHLIGHT_STYLES[globbing]=fg=063
ZSH_HIGHLIGHT_STYLES[history-expansion]=fg=white,underline
ZSH_HIGHLIGHT_STYLES[single-hyphen-option]=none
ZSH_HIGHLIGHT_STYLES[double-hyphen-option]=none
ZSH_HIGHLIGHT_STYLES[back-quoted-argument]=none
ZSH_HIGHLIGHT_STYLES[single-quoted-argument]=fg=063
ZSH_HIGHLIGHT_STYLES[double-quoted-argument]=fg=063
ZSH_HIGHLIGHT_STYLES[dollar-double-quoted-argument]=fg=009
ZSH_HIGHLIGHT_STYLES[back-double-quoted-argument]=fg=009
ZSH_HIGHLIGHT_STYLES[assign]=none

# Load local configurations if available
[ -f "$HOME/.local/etc/config.zsh" ] && source "$HOME/.local/etc/config.zsh"
[ -f "$HOME/.local/etc/local.zsh" ] && source "$HOME/.local/etc/local.zsh"

antigen apply

# Workaround for fast syntax highlighting crash
FAST_HIGHLIGHT[chroma-git]="chroma/-ogit.ch"

# Additional shell options
unsetopt correct_all share_history prompt_cr prompt_sp
setopt prompt_subst
setopt BANG_HIST
setopt INC_APPEND_HISTORY
setopt HIST_EXPIRE_DUPS_FIRST
setopt HIST_IGNORE_DUPS
setopt HIST_IGNORE_ALL_DUPS
setopt HIST_FIND_NO_DUPS
setopt HIST_IGNORE_SPACE
setopt HIST_SAVE_NO_DUPS
setopt HIST_REDUCE_BLANKS
setopt HIST_VERIFY

# Setup for deer (directory navigation)
autoload -U deer
zle -N deer

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
bindkey -s '\e;' 'll\n'
bindkey '\e[1;3D' backward-word
bindkey '\e[1;3C' forward-word
bindkey '\e[1;3A' beginning-of-line
bindkey '\e[1;3B' end-of-line
bindkey '\ev' deer
bindkey -s '\eu' 'ranger_cd\n'
bindkey -s '\eOS' 'vim '

# Source additional functions if available
[ -f "$HOME/.local/etc/function.sh" ] && source "$HOME/.local/etc/function.sh"

# Disable correction again (if needed)
unsetopt correct_all correct
DISABLE_CORRECTION="true"

# Completion settings
zstyle ':completion:*:complete:-command-:*:*' ignored-patterns '*.pdf|*.exe|*.dll'
zstyle ':completion:*:*sh:*:' tag-order files

###############################################################################
# End of ZSH Configuration
###############################################################################
EOF

print_message ".zshrc file has been created." "$GREEN"
print_message "Zsh configuration has been successfully installed!" "$GREEN"
print_message "Please log out and log back in, or run 'zsh' to start using your new configuration." "$BLUE"
