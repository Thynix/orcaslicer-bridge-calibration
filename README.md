# bridge_plate

Setting up bridge experiments manually can be tedious to set flow and bridge ratio modifiers, and remembering which one is which can be a challenge. With this script to generate variants of a manually-set reference, and text of flow and bridge density, these problems are solved:

<img width="3600" height="2160" alt="comparison" src="https://github.com/user-attachments/assets/92f16c96-7b61-4bb3-8490-75d82e9c3e20" />

```
usage: bridge_plate.py [-h] [--keep-text-mesh] reference output flow count min_density density_step

Make a bridge flow/density test plate from an OrcaSlicer 3MF reference

positional arguments:
  reference         reference project
  output            output project, overwritten if it exists
  flow              bridge flow ratio, 1.0 to 1.9 in steps of 0.1
  count             number of copies
  min_density       first bridge density, integer percent, 10 to 125
  density_step      bridge density step between copies, percent (may be negative); unused when count is 1

options:
  -h, --help        show this help message and exit
  --keep-text-mesh  keep each copy's text part on the reference's original, unmodified mesh (so stock OrcaSlicer doesn't drop it), instead of an empty mesh a patched OrcaSlicer rebuilds per-copy; the text will read the reference's original label until
                    edited by hand
```

## To use it

1. Open [`reference/reference.published.3mf`](https://github.com/Thynix/orcaslicer-bridge-calibration/blob/main/reference/reference.published.3mf) in OrcaSlicer
2. Save a copy with the printer, speed, and other settings you wish to use in the generated plate.
3. Run something like `python bridge_plate.py reference/mini-reference-0.8.3mf mini-bridge-flow-1.1.3mf 1.1 6 105 1`
4. Print the generated project using OrcaSlicer - if you're using my PR that support regenerating text, step 3 will work as-is. If not, add `--keep-text-mesh` so OrcaSlicer doesn't drop the empty elements, open each text, and change something so that it regenerates the mesh.

I've specialized the reference project for text placement, size, and behavior. It uses the font [Overpass Monospace](https://overpassfont.org/) thanks to Teque5's [Font Swatches Tested & Ranked](https://www.printables.com/model/71231-font-swatches-tested-ranked).
