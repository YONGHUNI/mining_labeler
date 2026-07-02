# MineGrid Labeler

MineGrid Labeler is a PyQt-based desktop labeling tool for reviewing 3x3 spatial matrix bins and recording mine component presence as boolean annotations.

## Project Layout

```text
<project-root>/
  main.py
  environment.yml
  data/
    global-mining-dataset.csv
    nature_mine_poly_nearest.gpkg
    figs/
  labeling_output/
```

`labeling_output/` is created next to `main.py` and stores annotation outputs. *The International Council on Mining and Metals* (ICMM) reference dataset is loaded from `data/` first; if no local file exists, the app tries the ICMM-hosted workbook as a fallback.

## Install

Use `conda` or `mamba` from a conda-forge based distribution such as Miniforge. The same `environment.yml` is intended for Windows, macOS, and Linux. It pins package versions where needed, but does not pin conda build strings, so the solver can choose OS-appropriate builds.

```powershell
cd <project-root>
conda env create -f environment.yml
conda activate minelabeler
```

If the `minelabeler` environment already exists, update it instead.

```powershell
cd <project-root>
conda env update -n minelabeler -f environment.yml --prune
conda activate minelabeler
```

If you use `mamba` instead of `conda`, the same commands work with `conda`.

```powershell
mamba env create -f environment.yml
mamba activate minelabeler
```

### System Packages

On macOS, no separate system packages are usually required when running from a normal desktop session.

On Linux desktop installs, the required Qt libraries are often already present. Minimal installs, Docker, WSL, and remote/headless systems may need additional GUI/WebEngine libraries. On Ubuntu/Debian, install these if Qt reports missing `xcb`, OpenGL, NSS, GBM, or DBus libraries:

```bash
sudo apt update
sudo apt install -y \
  libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
  libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xfixes0 \
  libdbus-1-3 libnss3 libxcomposite1 libxdamage1 libxrandr2 \
  libgbm1 fonts-dejavu-core libxcb-xkb1 libnspr4 libnss3 

export QT_QPA_PLATFORM=xcb
```

If audio-related WebEngine errors appear on Ubuntu/Debian, also install the distribution's ALSA runtime package, usually `libasound2` or `libasound2t64`.

### Verify Imports

After activation, verify the main GUI and GIS dependencies before running the app.

```bash
python -c "from PyQt6.QtWidgets import QApplication; from PyQt6.QtWebEngineWidgets import QWebEngineView; from osgeo import gdal; print('ok')"
```

## Data Setup

For the most reliable startup, place the ICMM dataset in the project `data/` directory.

Supported local files:

```text
data/global-mining-dataset.csv
```

The original workbook source is:

```text
https://www.icmm.com/website/data/2025/global-mining-dataset.xlsx
```

The local file is recommended because the website may reject automated downloads.

Optional local files:

```text
data/nature_mine_poly_nearest.gpkg
data/shp/nature_mine_poly_nearest.gpkg
data/figs/<component-name>/*.png
data/figs/<component-name>/*.jpg
```

`nature_mine_poly_nearest.gpkg` enables the Nature mine polygon overlay. `data/figs/` supplies reference images for the component guide.

Scene folders are discovered when they contain a `.tif` or `.tiff`, or when the folder name matches IDs such as `USA_C_4`, `CHN_I_9`, or `AUS_G_25` and contains georeferenced `.jpg` tiles. JPG tiles need matching `.jgw` world files for spatial binning.

For faster scene loading, JPG tile filenames should keep the tile index pattern used by the sampler, for example `ges_<x>_<y>_<zoom>.jpg`. When that pattern is available, the app reads the top-left and bottom-right `.jgw` files to reconstruct the remaining patch bounds instead of reading every `.jgw` file. If a patch set does not follow this pattern, the app falls back to reading all `.jgw` files so non-standard scenes remain compatible.

## Run

```powershell
cd <project-root>
conda activate minelabeler
python .\main.py
```

The app opens with the working directory set relative to `main.py`. Use **Open Working Directory** if you want to browse another scene root location during a session.

## Labeling Workflow

Each matrix bin is reviewed as a 3x3 group of image patches. Mine component categories are stored as boolean values:

- `0`: component is not identified in the current bin
- `1`: component is identified at least once in the current bin

Default values are `0`, so **if a component is absent, you can move through the list without changing it**. Only presence or absence is recorded.

Recommended workflow:

1. Select a scene root.
2. Select a matrix bin.
3. Review the highlighted target component.
4. Toggle the component if present.
5. Move through the component list.
6. Complete the bin and continue to the next one.

The green completion state means the bin has been explicitly reviewed and has `eval_end` recorded. Opening a bin alone should not mark it complete.

### Component Classes

The app shows the current field guide labels in the UI, while the CSV keeps stable historical column names for compatibility.

```text
UI label              Stored CSV field
Waste Heap            spoil_heap
Open Pit              open_pit
Processing Building   processing_building
Related Building      related_building
Tailings Pond         tailings_pond
Artificial Pond       rectangular_pond
```

Component guide notes:

- `Waste Heap`: bare waste rock accumulation near open-pit mining; look for terraced design, flat-topped structures, disorganized pile patterns, and small adjacent piles.
- `Open Pit`: exposed excavation face with step-terrace geometry; do not count pits that are completely or mostly filled with water.
- `Processing Building`: large industrial mining structure, often with elevated conveyors or circular treatment pools; confirm it is mine-related rather than unrelated industrial infrastructure.
- `Related Building`: administrative or support structure inside the mine operational perimeter; exclude residential buildings and power plants.
- `Tailings Pond`: discoloured impoundment near the processing area; it may range from mostly dry to very liquid.
- `Artificial Pond`: engineered industrial water body near processing buildings, usually small, sharp-edged, and murky; do not count distant irrigation, livestock, or decorative ponds unrelated to mining.

## Keyboard Shortcuts

```text
A                 Toggle current component ON/OFF
J                 Move to next component
K                 Move to previous component
Enter             Complete current bin and move to next bin
Shift+Enter       Move to previous bin
Ctrl+Z            Undo component change
Ctrl+Shift+Z      Redo component change
Left Arrow        Previous component, or previous bin from the first component
Right Arrow       Next component, or next bin from the final component
Tab               Focus first component control
Shift+Tab         Move focus backward
```

When the current component is the final category, pressing `J` completes the component scan for that bin.

The optional quality flag marks bins with broken imagery, missing tiles, or imagery that cannot be confidently interpreted. It is stored separately from the component booleans as `quality_flag`.

## Output Files

Annotation outputs are written under:

```text
labeling_output/
```

Important generated files:

```text
labeling_output/nk_mining_taxonomy.csv
labeling_output/keybindings.json
labeling_output/gpkg/<SCENE_ID>_matrix.gpkg
labeling_output/imported/
labeling_output/nk_mining_taxonomy.backup_<timestamp>.csv
```

The main CSV stores one row per evaluated matrix bin. GeoPackage outputs are written per scene under `labeling_output/gpkg/`. External CSVs merged through the app are archived under `labeling_output/imported/`, and merge backups use the `nk_mining_taxonomy.backup_<timestamp>.csv` pattern.

### Main CSV Schema

`labeling_output/nk_mining_taxonomy.csv` is the main annotation table. It stores one row per evaluated matrix bin.

Important identity and scene fields:

- `annotation_key`: stable unique key for a scene/bin pair
- `scene_uid`: normalized scene identifier such as `USA-C-4`
- `scene_name`: source scene folder name
- `scene_tif_path`: scene path stored relative to `main.py` when possible
- `identifier`: output-safe scene identifier such as `USA_C_4`
- `grid_index`: matrix bin index

Spatial and source-image fields:

- `mining_category`: inferred target category such as `Coal`, `Gold`, or `Iron`
- `mine_point_count`: number of matching mine points associated with the bin
- `top_left_jpg`: top-left JPG tile used by the bin
- `bottom_right_jpg`: bottom-right JPG tile used by the bin

Annotation fields:

- `spoil_heap`: shown in the UI as `Waste Heap`
- `processing_building`: shown in the UI as `Processing Building`
- `related_building`: shown in the UI as `Related Building`
- `tailings_pond`: shown in the UI as `Tailings Pond`
- `rectangular_pond`: shown in the UI as `Artificial Pond`
- `open_pit`: shown in the UI as `Open Pit`
- `quality_flag`

Component and quality fields are stored as `0` or `1`. `quality_flag` is separate from the component labels and marks bins with broken imagery, missing tiles, or imagery that cannot be confidently interpreted.

Timing fields:

- `eval_start`: first meaningful interaction timestamp
- `eval_end`: explicit completion timestamp

### GeoPackage Schema

GeoPackage outputs are written under:

```text
labeling_output/gpkg/<SCENE_ID>_matrix.gpkg
```

Each GeoPackage contains a `matrix_bins` polygon layer. It mirrors the key CSV fields and stores each matrix bin as spatial geometry, so labels can be inspected in GIS software.

### Other Output Files

- `labeling_output/keybindings.json`: custom keyboard shortcuts saved from the Keybindings dialog
- `labeling_output/imported/`: external CSV files moved here after a successful merge
- `labeling_output/nk_mining_taxonomy.backup_<timestamp>.csv`: automatic backup created before imported CSVs are merged

The app uses `eval_end` to determine whether a matrix bin should be shown as completed.

## Purging Outputs

The app includes purge controls for removing generated CSV and GeoPackage outputs. Use these carefully:

- **Purge Scene Labels** removes outputs for the current scene.
- **Full Purge** removes all generated labeling outputs.

A confirmation dialog is shown before deletion.

## Troubleshooting

### Windows PyQt6 DLL Error

If Windows raises this error when importing `PyQt6.QtWidgets`:

```text
ImportError: DLL load failed while importing QtWidgets: The specified procedure could not be found.
```

make sure `environment.yml` does not include platform-specific conda build strings such as `hbc0d294_0` or `h8206538_0`, then recreate or update the environment from `environment.yml`. Those build strings are OS-specific and can cause solve failures on macOS/Linux; on Windows, they can also make Qt/PyQt DLL behavior harder to reproduce across machines.

### Linux Qt Platform Plugin Error

If Linux reports that the Qt platform plugin `xcb` could not be loaded, install the Linux system packages listed in the install section, reactivate the environment, and run the import verification command again.

### Headless or Remote Linux

This is a desktop GUI app. On headless Linux, run it inside a real desktop session, an X11/Wayland-forwarded session, or a virtual display such as `xvfb`.

## Notes

- Use the local ICMM dataset in `data/` when working offline or when the ICMM website blocks automated access.
- `labeling_output/` can be deleted and regenerated, but doing so removes annotation progress.
- The environment is intentionally minimal and focused on `main.py`.
