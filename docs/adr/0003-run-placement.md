# ADR 0003 — Per-case run placement: in-place on /mnt, or staged ext4 copy

Status: accepted (2026-10-07; default confirmed by the user)

## Context
OpenFOAM breaks on paths with spaces (e.g. `D:\cases\My Engine Study`), and
`/mnt/d` (9P) is slow for heavy I/O. Existing practice is mixed: AutoFOAM cases run in place; larger
cases such as SandiaFlameD are copied to ext4 and rsynced back. The ext4 vhdx never shrinks on its own.

## Decision
`Placement = auto | in-place | staged`, stored per case in `project.toml`. `auto` picks in-place when
the WSL path is OpenFOAM-safe, staged otherwise (or when the user flags heavy I/O). The stage dir is
persistent per case: `~/aethercfd/stage/<project>/<case>/`; stage-in via rsync (skip unchanged mesh,
refuse to clobber un-synced outputs); periodic and final sync-back that skips the newest time dir while
live. During a staged run the viewer reads `\\wsl.localhost\<distro>\…`. GUI edits go to the Windows
copy; pushing an edit to a running stage is an explicit command. Stage usage is tracked and
`stage.clean` is offered.
