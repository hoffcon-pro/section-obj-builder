# Steel Section OBJ Builder

CLI tool to generate extruded 3D `.obj` models of steel sections from CSV property tables.

The tool reads one CSV file, or all CSV files in a directory, builds section geometry using
[`sectionproperties`](https://github.com/robbievanleeuwen/section-properties), and extrudes each section along a specified length.

## Features

- Generates OBJ meshes by default.
- Extrudes each section along `--length` (default: `1000` mm).
- Accepts either:
  - a single CSV file, or
  - a directory of CSV files (`*.csv`, non-recursive).
- Optional section filtering via repeated `--designation`.
- Optional filename length suffix (`--length-in-name/--no-length-in-name`).
- Progress bars via `tqdm`.
- Shape auto-detection with manual override (`--shape`).

## Supported Shapes

- `channel` (e.g. PFC) via `channel_section`
- `i` (e.g. UB/UC) via `i_section`
- `rhs` / SHS via `rectangular_hollow_section`
- `angle` / EA via `angle_section`

### Shape Selection

Use `--shape auto` (default) to infer shape from CSV filename/family/designation tokens, or override explicitly:

- `--shape channel`
- `--shape i`
- `--shape rhs`
- `--shape angle`

## Installation

### Using uv (recommended)

```bash
uv sync
```

Run with:

```bash
uv run python main.py --help
```

### Using pip

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

Then run:

```bash
python main.py --help
```

## Usage

```bash
python main.py [OPTIONS] INPUT_PATH
```

- `INPUT_PATH`: CSV file, or directory containing CSV files.

If run with no args, the app displays help.

## Common Commands

Generate from one CSV:

```bash
uv run python main.py "section data/PFC.csv"
```

Generate from all CSVs in a directory:

```bash
uv run python main.py "section data"
```

Set output directory and length:

```bash
uv run python main.py "section data/UB.csv" --output-dir output --length 6000
```

Generate only selected designations:

```bash
uv run python main.py "section data/UC.csv" --designation 310UC158 --designation 200UC46.2
```

Force shape override:

```bash
uv run python main.py "section data/EA.csv" --shape angle
uv run python main.py "section data/SHS.csv" --shape rhs
```

Disable length suffix in filenames:

```bash
uv run python main.py "section data/PFC.csv" --no-length-in-name
```

## Output Naming

By default (`--length-in-name`), output filenames include length:

- `380PFC_1000mm.obj`
- `610UB125_1000mm.obj`
- `250x9_SHS_1000mm.obj`

Disable with `--no-length-in-name` to get names like `380PFC.obj`.

## CSV Column Expectations

The parser accepts flexible headers by shape family.

### Common

- Required: `Designation`
- Optional: `Family`

### Channel / I

- Dimensions: `d`, `bf` (or `b`)
- Thickness: `tf`, `tw`
- Radius: `r1` (or `r`)

### RHS / SHS

- Dimensions: `d`, `b` (or `bf`)
- Wall thickness: `t`
- Outside corner radius: `r` (or `r1`)

### Angle / EA

- Legs: `b1`, optional `b2` (if missing, `b2=b1`)
- Thickness: `t`
- Inside/root radius: `r1`
- Toe radius: `r2`

Note: for angle rows where `r2 > t`, toe radius is clamped to `t` to satisfy geometry constraints.

## Notes

- OBJ files are ignored by git (`*.obj` in `.gitignore`).
- Generated geometry uses triangulated end caps and extruded side faces.
- Units are assumed to be millimeters, matching the input CSV values.

## Troubleshooting

- **"Missing required column"**: confirm the CSV has the expected headers for that shape.
- **No files generated for `--designation`**: verify exact designation text/case in CSV.
- **Unexpected shape**: force explicit `--shape` override.
