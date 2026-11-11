# BurnFix

A software mask for OLED burn-in on Windows. You paint over the ghost image on a
flat grey screen, turn one dial until it disappears, and a click-through overlay
keeps it cancelled on your desktop.

**Status: experimental.** It has worked as intended on one real panel (a QD-OLED
ultrawide with a burned-in logo). How well it hides a given mark depends on the
panel and on how closely you trace the mark.

## Darker or brighter? Press `I`

Burn-in usually shows as a mark that is darker than its surroundings, and that
is what BurnFix assumes when you start painting. On the panel this was built
for, the mark was the other way round: brighter than the grey around it. That
happens when the burned-in image was dark on a bright background, or when the
panel's own refresh cycles have over-corrected the area. Correcting in the
wrong direction makes the mark stand out more.

So if painting makes your mark worse, press `I`. It flips the current layer from
"mark is darker" to "mark is brighter" (and back), and the status line shows
which one you are in. Each layer has its own direction, so one map can hold
both kinds of mark.

## What it does and doesn't do

Burn-in is permanent wear: some pixels now emit a few percent less (or, after
the panel's own compensation, sometimes more) light than their neighbours for
the same signal. No software can undo that.

BurnFix hides it instead:

- For a mark that is **darker** than its surroundings, it trims the healthy
  pixels around the mark by the missing amount, fading to nothing over about
  200 px so the trim itself isn't visible.
- For a mark that is **brighter**, it trims the mark itself.
- It never drives any pixel harder, so it does not age the panel faster. This is
  the opposite of "wear-levelling" videos, which hide burn-in by ageing the rest
  of the screen to match.
- Turn it off and the panel is exactly as it was.

## Install

Windows 10 2004 or later, Python 3.9+.

```
pip install -r requirements.txt
python burnfix.py selftest
```

`selftest` flashes a small grey square and should print `PASS`.

## Use

1. Run `Calibrate.bat` (or `python burnfix.py calibrate`). The screen goes flat
   grey with the controls listed on it.
2. Pick a layer (`1`-`9`) and paint over the burn mark.
3. If the mark got more visible when you painted it, press `I` to flip the layer.
   Then press Up/Down until the mark melts into the background.
4. Check other grey levels and colours with `G` and `C`; `Space` compares
   correction off and on.
5. `Esc` saves and starts the overlay.

| Key | Action |
| --- | --- |
| Left-drag / right-drag | Paint / erase |
| Mouse wheel | Brush size |
| `B` / `R` | Brush / rectangle tool |
| Up / Down (Shift = big steps) | Strength of the current layer, positive or negative |
| `I` | Flip the layer between "mark is darker" and "mark is brighter" |
| `1`-`9` | Select layer; use one per mark of different depth |
| `T` | Shape the layer from the screenshot taken at launch instead of a flat fill |
| `G` / `C` | Test screen grey level / colour |
| `Space` | Correction off / on |
| `F` | Feathered trim (default) or whole-screen trim |
| `Z` / `X` | Undo last stroke / clear layer |
| `Tab` | Hide / show the on-screen text |
| `Esc` | Save and start the overlay |
| `Ctrl+Q` | Quit without saving |

The `T` mode is for burn-in caused by something you can still put on screen (a
logo, a taskbar, a game HUD). Display it where it used to sit, launch the
calibrator, paint roughly over the area and press `T`: the detail comes from the
screenshot.

### Running the overlay

```
python burnfix.py run         start the overlay (Ctrl+Alt+B toggles it)
python burnfix.py stop        stop it
python burnfix.py install     start it at every logon
python burnfix.py uninstall   remove the logon shortcut and stop it
```

The map is saved next to the script as `burnmap.npz`.

## Limitations

- Primary monitor only, and the map is tied to the resolution it was made at.
- The correction is grey. A burn mark with a colour tint will be reduced, not
  cancelled.
- Exclusive-fullscreen games, the Start menu and the lock screen draw above the
  overlay, so the mark is visible there.
- Many OLED monitors shift the whole image by a few pixels now and then. Thin
  edges of a mark can show a faint outline when that happens; a soft brush edge
  hides most of it.
- One overlay step is about 0.4% of signal. Finer correction is done with a
  static dither, which is invisible at normal viewing distance but is not true
  sub-step precision.
- The overlay is excluded from screenshots and recordings, so captures show the
  uncorrected image, which is what you want.

## How it works

The overlay is a full-screen layered window (`WS_EX_LAYERED | WS_EX_TRANSPARENT`)
filled with black at a per-pixel alpha. Black at alpha `a` multiplies whatever is
beneath by `1 - a/255`, which matches how burn-in behaves: a fixed percentage
loss at every brightness level. The burn map is the sum of up to nine painted
masks, each with its own strength.

## License

MIT
