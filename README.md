# el sounder

A tiny  audio visualizer for Windows. Reacts to whatever's playing, and shows the track's title,
artist and cover art — either as a spinning vinyl or a flat cover behind
the bars.

## What it does

- Reacts to **Spotify only**, or to **everything coming out of your
  speakers** — switch anytime, or let it switch itself based on whether
  Spotify is running.
- Shows now playing info (title/artist/cover) via Windows' own media
  session — works with Spotify, browsers, and most other players.
- Optional **Discord Rich Presence**: puts the current track on your
  profile, cover art included.
- Bar or wave style, vinyl or flat cover, custom colors/gradients,
  adjustable sensitivity and bar count — all from the right-click menu.
- Lives in the system tray, remembers its position and settings, ships
  in 11 languages.

## Requirements

- Windows 10/11 (this is a Windows-only app — WASAPI loopback + SMTC +
  taskbar integration are all Windows APIs)

## Project layout

```
app/          entry point + startup wiring
core/         audio capture, FFT analysis, now-playing (SMTC), the engine
              tying it all together
backstage/    Windows-only glue: taskbar icon, tray, Discord RPC
ui/           the overlay window, animation state, and the renderers
              that draw bars/vinyl/cover/text
config/       settings (load/save/presets) + translations
resources/    icon + fonts
```

Settings are stored at `%APPDATA%\ElSounder\settings.json`.

## Notes

- Discord Rich Presence needs an internet connection; cover art is
  uploaded anonymously to catbox.moe to get a URL Discord can display
  (Discord's API requires a public image URL — there's no way around
  that).
- "Only Spotify" mode uses Windows' per-process loopback API, which
  needs Windows 10 build 20348+. On older builds it falls back to
  showing nothing rather than silently mixing in other apps' audio.

## how to install
- go udner releases
- search for  "installer"
- downlaod the .exe and start it
- done !

by Guter Mensch – no certificate yet
