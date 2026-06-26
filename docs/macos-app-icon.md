# macOS app icon (Liquid Glass / Icon Composer)

This documents how the macOS **application icon** (Finder / Dock / Launchpad —
*not* the menu-bar tray glyph) is authored and built so it renders correctly in
macOS 26 (Tahoe)'s **Default / Dark / Clear / Tinted** appearance modes.

## Background

macOS 26 introduced a layered "Liquid Glass" icon format authored with Apple's
**Icon Composer** (bundled in Xcode 26: `Xcode.app/Contents/Applications/Icon
Composer.app`). The source is an **`AppIcon.icon`** bundle (a directory), which
`actool` compiles into an **`Assets.car`** asset catalog. macOS reads the
catalog via the `CFBundleIconName` Info.plist key and composes the four
appearances from the layers.

Older macOS versions ignore `Assets.car` and fall back to the classic
`icon.icns` referenced by `CFBundleIconFile`. We ship **both** so the icon looks
right everywhere.

Sources:
- Apple HIG, *App icons*: https://developer.apple.com/design/human-interface-guidelines/app-icons
- Apple Developer, *Icon Composer*: https://developer.apple.com/icon-composer/
- Apple Developer, *Creating your app icon using Icon Composer*: https://developer.apple.com/documentation/Xcode/creating-your-app-icon-using-icon-composer
- WWDC25 *Say hello to the new look of app icons* (220) and *Create icons with Icon Composer* (361)
- Community write-ups on the non-Xcode `actool` path:
  - https://successfulsoftware.net/2025/09/26/updating-application-icons-for-macos-26-tahoe-and-liquid-glass/
  - https://www.hendrik-erz.de/post/supporting-liquid-glass-icons-in-apps-without-xcode
  - https://www.d12frosted.io/posts/2026-01-08-emacs-plus-liquid-glass-icons
  - https://mjtsai.com/blog/2025/08/08/separate-icons-for-macos-tahoe-vs-earlier/
- `icon.json` schema reference (community, matches Icon Composer 1.5):
  https://github.com/giginet/apple-icon-composer-skill

## Files in this repo

```
app/resources/AppIcon.icon/            # hand-authored Icon Composer bundle
  icon.json                            # manifest (validated against the schema)
  Assets/Foreground.svg                # the k→ brand glyph, vector, white
app/resources/Assets.car               # committed compiled catalog (fallback if no actool)
app/resources/icon.icns                # pre-Tahoe fallback (full 16→1024 iconset)
app/resources/icon-source.png          # high-contrast source (black sq + white k→)
app/packaging/macos_icon.py            # compile + install helper used by the build
```

### `icon.json` design

- **Background**: document-level `fill` is a dark `automatic-gradient`
  (display-p3 near-black) with a `dark` specialization and a `tinted` →
  `automatic` specialization so the system recolors it for Clear/Tinted.
- **Foreground**: one group with a single `Glyph` layer pointing at
  `Foreground.svg`, `glass: true`, solid white `fill`, and a `tinted` →
  `automatic` fill specialization so the system tints the glyph.
- One group only (Apple allows max four). `squares: shared`, `circles: watchOS`.

The glyph is vector SVG (recommended) so it scales and the system can apply
material/blur per appearance.

## How the build consumes it

PyInstaller's `BUNDLE` cannot place arbitrary files into `Contents/Resources`
(user `datas` land in `Contents/Frameworks` on PyInstaller 6). So the spec wires
the icon in two steps, in `app/packaging/kiro_gateway_tray.spec` (darwin branch
only — Windows/Linux are untouched):

1. `BUNDLE(icon=resources/icon.icns, info_plist={... "CFBundleIconName": "AppIcon"})`
   - PyInstaller copies `icon.icns` into `Contents/Resources/icon.icns` and sets
     `CFBundleIconFile` automatically (pre-Tahoe fallback).
   - `CFBundleIconName: AppIcon` tells macOS 26 to use the catalog.
2. After `BUNDLE`, the spec calls `macos_icon.install_into_app(<App>.app)`, which
   compiles `AppIcon.icon` → `Assets.car` with `actool` (or uses the committed
   `resources/Assets.car` if `actool` is unavailable) and copies it into
   `Contents/Resources/Assets.car`.

`app/packaging/make_dist.py` re-runs `install_into_app` on the staged `.app`
before building the DMG (belt-and-suspenders).

### The actool command

```bash
xcrun actool app/resources/AppIcon.icon \
  --compile <out_dir> \
  --app-icon AppIcon \
  --include-all-app-icons \
  --enable-on-demand-resources NO \
  --development-region en \
  --target-device mac \
  --minimum-deployment-target 26.0 \
  --platform macosx \
  --output-partial-info-plist <out_dir>/partial.plist
```

This emits `Assets.car` **and** a flattened `AppIcon.icns`. The partial plist it
writes contains `CFBundleIconName`/`CFBundleIconFile` for reference.

Validate the catalog:

```bash
xcrun --sdk macosx assetutil --info <out_dir>/Assets.car
# expect: Name "AppIcon", Appearances Aqua / DarkAqua / ISAppearanceTintable,
#         a Vector "Foreground" layer, and an IconGroup.
```

## Re-authoring with the Icon Composer GUI (optional)

The `.icon` bundle here was authored by hand and verified to compile and to
validate against the schema. If you prefer the GUI:

1. Open `Xcode.app/Contents/Applications/Icon Composer.app`.
2. Drag `app/resources/AppIcon.icon/Assets/Foreground.svg` in as a layer; set
   the canvas/background fill to the dark brand color, toggle Glass on the
   glyph layer, and adjust Dark/Tinted in the appearance controls at the bottom.
3. Save back over `app/resources/AppIcon.icon`.
4. Re-commit the regenerated `resources/Assets.car`:
   `xcrun actool ...` (command above) then copy `Assets.car` into
   `app/resources/`.

Keep the bundle stem **`AppIcon`** so it matches `CFBundleIconName` and the
`--app-icon AppIcon` argument.

## Regenerating the `.icns` fallback

The fallback `icon.icns` is built from a 1024×1024 master (dark rounded square +
white `k→`). To rebuild:

```bash
rsvg-convert -w 1024 -h 1024 app/resources/AppIcon.icon/Assets/Foreground.svg -o /tmp/fg.png
# composite over a dark squircle (see git history / macos_icon design notes),
# then:
mkdir icon.iconset
for s in 16 32 128 256 512; do
  sips -z $s   $s   master1024.png --out icon.iconset/icon_${s}x${s}.png
  sips -z $((s*2)) $((s*2)) master1024.png --out icon.iconset/icon_${s}x${s}@2x.png
done
iconutil -c icns icon.iconset -o app/resources/icon.icns
```

## What can only be verified on macOS 26

The catalog compiles and validates on this dev machine (Xcode 26.5 / actool
26.5). The actual **Liquid Glass rendering** of Default/Dark/Clear/Tinted can
only be eyeballed on macOS 26 hardware (or Device Hub) after building and
signing the app — the appearance compositing is done by the OS at display time,
not by `actool`. Test by switching System Settings appearance and the icon tint
options, per Apple's guidance.
