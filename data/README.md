# Generated Sprite Assets

Naming order:

    object[_form][_variant][_state][_f-flavor].png

Rules:
- The object type is always the stable filename prefix.
- Forms represent persistent physical transformations.
- Variants represent gameplay-significant subtypes.
- States represent temporary operating modes.
- Cosmetic flavors use `_f-<flavor>` and are discovered from asset files.
- Default components are omitted from filenames.
- PNG dimensions are authoritative and should be read from the asset itself.

Organization:
- `assets/sprites/objects/<object_type>/`
- `assets/sprites/player/`
- `reference/sprite_catalog.png`

Canonical canvases used here:
- Most assets: 32x32
- Doors: 32x64
- Player directions: 32x48

These sprites were extracted and downscaled from the generated catalog. Inspect them in-game;
individual crops may benefit from later hand cleanup because the catalog was generated as one image.
