# MineGrid Labeler

MineGrid Labeler is a PyQt-based desktop labeling tool for reviewing 3x3 spatial matrix bins and recording mine component presence as boolean annotations.

## Project Layout

```text
sampled_other_nations/
  main.py
  environment.yml
  data/
    global-mining-dataset.xlsx
    # or global-mining-dataset.csv
  labeling_output/
```

`labeling_output/` is created next to `main.py` and stores annotation outputs. The ICMM reference dataset is loaded from `data/` first; if no local file exists, the app tries the ICMM-hosted workbook as a fallback.

## Install

Create the conda environment from `environment.yml`.

```powershell
cd F:\workspace\labeling_day\trial2
micromamba env create -f environment.yml
micromamba activate minelabeler
```

If the `minelabeler` environment already exists, update it instead.

```powershell
cd F:\workspace\labeling_day\trial2
micromamba env update -n minelabeler -f environment.yml --prune
micromamba activate minelabeler
```

If you use `mamba` instead of `micromamba`, the same commands work with `mamba`.

```powershell
mamba env create -f environment.yml
mamba activate minelabeler
```

## Data Setup

For the most reliable startup, place the ICMM dataset in the project `data/` directory.

Supported local files:

```text
data/global-mining-dataset.xlsx
data/global-mining-dataset.csv
```

The original workbook source is:

```text
https://www.icmm.com/website/data/2025/global-mining-dataset.xlsx
```

The local file is recommended because the website may reject automated downloads.

## Run

```powershell
cd F:\workspace\labeling_day\trial2
micromamba activate minelabeler
python .\main.py
```

The app opens with the working directory set relative to `main.py`. Use **Open Working Directory** if you want to browse another scene root location during a session.

## Labeling Workflow

Each matrix bin is reviewed as a 3x3 group of image patches. Mine component categories are stored as boolean values:

- `0`: component is not identified in the current bin
- `1`: component is identified at least once in the current bin

Default values are `0`, so if a component is absent, you can move through the list without changing it. Count is not recorded; only presence or absence is recorded.

Recommended workflow:

1. Select a scene root.
2. Select a matrix bin.
3. Review the highlighted target component.
4. Toggle the component if present.
5. Move through the component list.
6. Complete the bin and continue to the next one.

The green completion state means the bin has been explicitly reviewed and has `eval_end` recorded. Opening a bin alone should not mark it complete.

## Keyboard Shortcuts

```text
A                 Toggle current component ON/OFF
J                 Move to next component
K                 Move to previous component
Enter             Complete current bin and move to next bin
Shift+Enter       Move to previous bin
Ctrl+Z            Undo component change
Ctrl+Shift+Z      Redo component change
Left Arrow        Previous grid
Right Arrow       Next grid
Tab               Focus first component control
Shift+Tab         Move focus backward
```

When the current component is the final category, pressing `J` completes the component scan for that bin.

## Output Files

Annotation outputs are written under:

```text
labeling_output/
```

The main CSV stores one row per evaluated matrix bin. GeoPackage outputs are also written for scene/grid spatial data.

Important timing fields:

- `eval_start`: first meaningful interaction with a bin
- `eval_end`: explicit completion time for the bin

The app uses `eval_end` to determine whether a matrix bin should be shown as completed.

## Purging Outputs

The app includes purge controls for removing generated CSV and GeoPackage outputs. Use these carefully:

- **Purge Scene Root** removes outputs for the current scene root.
- **Full Purge** removes all generated labeling outputs.

A confirmation dialog is shown before deletion.

## Notes

- Use the local ICMM dataset in `data/` when working offline or when the ICMM website blocks automated access.
- `labeling_output/` can be deleted and regenerated, but doing so removes annotation progress.
- The environment is intentionally minimal and focused on `main.py`.
