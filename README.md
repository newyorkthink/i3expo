# i3expo

[![codecov](https://codecov.io/gh/laur89/i3expo/branch/main/graph/badge.svg?token=i3expo_token_here)](https://codecov.io/gh/laur89/i3expo)
[![CI](https://github.com/laur89/i3expo/actions/workflows/main.yml/badge.svg)](https://github.com/laur89/i3expo/actions/workflows/main.yml)

## Overwiew

Expo is an simple and straightforward way to get a visual impression of all your
i3 workspaces that many compositing window managers use. It's not a very
powerful approach, but a very intuitive one and especially fits workflows that use
lots of temporary windows or those in which the workspaces are mentally arranged in
a grid.

i3expo emulates that function within the limitations of a non-compositing window
manager. By listening to the IPC, it takes a screenshot whenever a window event
occurs. Thanks to a fast C library, this produces negligible overhead in normal
operation and allows the script to remember what state you left a workspace in.

The script is run as a background process and reacts to signals in order to open its
UI in which you get an overview of the known state of your workspaces and can select
another with the mouse or keyboard.

Example output:

![Sample](img/ui.png)

## Installation

```bash
pipx install i3expo
```

## Usage

Launch i3expo from i3 config; alternatively you may prefer to run `i3expo` in a
terminal in order to catch any errors in this pre-alpha stage.

On first run, i3expo creates `$XDG_CONFIG_HOME/i3expo/config` (normally
`~/.config/i3expo/config`). It contains the complete editable theme, startup scan,
and global shortcut settings. Existing configuration files are never overwritten.
Color values can use PyGame names, `#fff`, or `#ffffff` hex.

Send `SIGUSR1` to `i3expo` to toggle the Expo UI, for example by adding a `bindsym`
for `killall -s SIGUSR1 i3expo` to your i3 config. Send `SIGHUP` to have the
application reload its configuration.

The default global shortcut is `Mod4+e`; edit `toggle_shortcut` and restart i3expo
to change it, or leave it empty and use an i3 `bindsym`. Navigate the UI with the
mouse, arrow keys, Return, and Escape. Typing a one-character workspace name jumps
to it directly (`1` selects workspace `1`, `a` selects workspace `a`). The `hjkl`
keys navigate only when a workspace with that exact one-character name is absent.

Recommended i3 config:

```
  exec_always --no-startup-id i3expo
  for_window [class="^i3expo$"] fullscreen enable
  # Optional when toggle_shortcut is empty:
  bindsym $mod1+e exec --no-startup-id killall -s SIGUSR1 i3expo
```

Note the script depends on pre-compiled `i3expo/prtscn.so` for screen-grabbing. If
it doesn't work you may need to compile `prtscn.c` yourself following the instruction
in the file header. In that case add `screenshot_lib_path` config item, pointing
to the compiled prtscn executable.

## Limitations

Since it works by taking screenshots, X11 cannot provide an image for a workspace
which has never been mapped. The default startup scan briefly maps each non-empty
workspace, refreshes its preview, and restores the original window focus. This scan
is automatic but cannot be completely invisible on a non-compositing window manager.

## Caution

This is pre-alpha software and some bugs are still around. It works for my own,
single monitor and a common workflow. There is not much input validation and no
protection against you screwing up the layout or worse.

## Bugs

Stalled windows whose content i3 doesn't know cause interface bugs and could
probably be handled better, but this needs more testing.

Daemon's cpu usage raises considerably upon first time UI gets rendered. Believe this
has something to do with pygame not de-initing itself properly. *Confirm this is still
the case*

## Todo

- It's theoretically feasible to take the window information from i3's tree and allow
for dragging of windows from one workspace to another or even from container to
container. However, this would be massively complex (especially on the UI side) and
it's not clear if it would be worth the effort.
- ~~multimonitor support~~
- pause screenshotting while screen is locked, eg via i3lock

## Credit

- original code from https://gitlab.com/d.reis/i3expo
    - for a time this fork was developed under https://gitlab.com/layr89/i3expo
- Stackoverflow [answer](https://stackoverflow.com/a/16141058/1803648) for the
  screenshot library
