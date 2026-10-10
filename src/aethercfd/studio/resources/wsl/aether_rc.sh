# AetherCFD terminal rc (bash --noprofile --rcfile <this>): a clean shell with EXACTLY ONE OpenFOAM
# fork sourced (never two), and a prompt that says which one. ~/.bashrc is not read on purpose; set
# AETHER_SOURCE_USER_BASHRC=1 in the environment to opt in.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[ -f /etc/bash.bashrc ] && . /etc/bash.bashrc
if [ "${AETHER_SOURCE_USER_BASHRC:-0}" = 1 ] && [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi
unset WM_PROJECT_DIR WM_PROJECT_VERSION FOAM_INST_DIR
if [ -n "${AETHER_FOAM_BASHRC:-}" ]; then
  set +u
  . "$AETHER_FOAM_BASHRC" > /dev/null 2>&1
  if [ -z "${WM_PROJECT_DIR:-}" ]; then
    echo "AetherCFD: could not load OpenFOAM from $AETHER_FOAM_BASHRC" >&2
  fi
fi
if [ "$(id -u)" = 0 ]; then
  # the distro runs as root: let tutorial scripts' mpirun (runParallel) start
  export OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
fi
PS1='\[\e[36m\][${AETHER_FORK:-no OpenFOAM}]\[\e[0m\] \[\e[2m\]\w\[\e[0m\]\$ '
alias ll='ls -la --color=auto'
alias ls='ls --color=auto'
echo "AetherCFD terminal: ${AETHER_FORK_LABEL:-OpenFOAM} in $(pwd)"
