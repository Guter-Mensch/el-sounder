# el sounder

A tiny always-on-top audio visualizer for Windows. Sits in a corner of
your screen, reacts to whatever's playing, and shows the track's title,
artist and cover art — either as a spinning vinyl or a flat cover behind
the bars.

## What it does

- Reacts to **Spotify only**, or to **everything coming out of your
  speakers** — switch anytime, or let it switch itself based on whether
  Spotify is running.
- Shows now-playing info (title/artist/cover) via Windows' own media
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
- Python 3.12+
- Dependencies in `requirements.txt`:

```
pip install -r requirements.txt
```

## Running it

```
python -m app.main
```

Optional flags:

| Flag              | What it does                                   |
|-------------------|-------------------------------------------------|
| `--list-devices`  | Print all audio devices and exit                |
| `--device INDEX`  | Force a specific device instead of auto-detect   |

Right-click the overlay (or the tray icon) for every other setting —
audio source, colors, bar/cover style, sensitivity, Discord, language.

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

## Compile to .exe

- at first you neded as mentionen to download the python libaries to even start !
- go in the folder next to the ```__main__.py```
- then open with rightclick on empty space the cmd (ps)
  and run one of the command's :
#
- powershell
```
pyinstaller --noconfirm --onefile --windowed --noconsole --icon="resources/icons/app.ico" --add-data="resources;resources" --add-data="translations.json;." --collect-all=app --collect-all=core --collect-all=config --collect-all=ui --collect-all=platform --name="el Sounder" "app/main.py"; move-item "el Sounder.spec" "dist\"; move-item "build" "dist\"; explorer "dist"
```
#
- cmd
```
pyinstaller --noconfirm --onefile --windowed --noconsole --icon="resources/icons/app.ico" --add-data="resources;resources" --add-data="translations.json;." --collect-all=app --collect-all=core --collect-all=config --collect-all=ui --collect-all=platform --name="el Sounder" "app/main.py" && move "el Sounder.spec" "dist\" && robocopy "build" "dist\build" /E /MOVE && explorer "dist"
```
