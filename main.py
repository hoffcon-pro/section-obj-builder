from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import typer
from sectionproperties.pre.geometry import Geometry
from sectionproperties.pre.library import (
    angle_section,
    channel_section,
    i_section,
    rectangular_hollow_section,
)
from shapely.geometry import Polygon
from shapely.ops import triangulate
from tqdm import tqdm

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Generate extruded 3D section models from CSV data.")
VALID_SHAPES = {"auto", "channel", "i", "rhs", "angle"}


@dataclass(frozen=True)
class SectionRow:
    designation: str
    family: str | None
    depth: float | None
    width: float | None
    leg1: float | None
    leg2: float | None
    flange_thickness: float | None
    web_thickness: float | None
    wall_thickness: float | None
    root_radius: float
    toe_radius: float


def _norm(key: str) -> str:
    return key.strip().lower().replace(" ", "")


def _to_float(raw: str | None, column_name: str, row_name: str) -> float:
    if raw is None:
        raise ValueError(f"Missing '{column_name}' for section '{row_name}'.")

    value = raw.strip().replace(",", "")
    if not value:
        raise ValueError(f"Empty '{column_name}' for section '{row_name}'.")

    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(
            f"Could not parse '{column_name}' value '{raw}' for section '{row_name}'."
        ) from exc


def _to_float_optional(raw: str | None, default: float = 0.0) -> float:
    if raw is None:
        return default

    value = raw.strip().replace(",", "")
    if not value:
        return default

    return float(value)


def _to_optional_value(raw: str | None) -> float | None:
    if raw is None:
        return None

    value = raw.strip().replace(",", "")
    if not value:
        return None

    return float(value)


def _read_sections(csv_path: Path) -> list[SectionRow]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        if not reader.fieldnames:
            raise ValueError(f"CSV '{csv_path}' has no header row.")

        mapping = {_norm(name): name for name in reader.fieldnames if name is not None}

        def source_col(*candidates: str) -> str:
            for candidate in candidates:
                key = _norm(candidate)
                if key in mapping:
                    return mapping[key]
            raise ValueError(
                f"CSV '{csv_path}' is missing one of required columns: {', '.join(candidates)}"
            )

        def source_col_optional(*candidates: str) -> str | None:
            for candidate in candidates:
                key = _norm(candidate)
                if key in mapping:
                    return mapping[key]
            return None

        designation_col = source_col("Designation")
        family_col = source_col_optional("Family")

        depth_col = source_col_optional("d")
        width_col = source_col_optional("bf", "b")

        leg1_col = source_col_optional("b1")
        leg2_col = source_col_optional("b2")

        flange_thickness_col = source_col_optional("tf")
        web_thickness_col = source_col_optional("tw")
        wall_thickness_col = source_col_optional("t")

        root_radius_col = source_col_optional("r1", "r")
        toe_radius_col = source_col_optional("r2")

        sections: list[SectionRow] = []
        for row in reader:
            designation = (row.get(designation_col) or "").strip()
            if not designation:
                continue

            family_value = (row.get(family_col) or "").strip() if family_col else ""
            depth_value = _to_optional_value(row.get(depth_col))
            width_value = _to_optional_value(row.get(width_col))
            leg1_value = _to_optional_value(row.get(leg1_col))
            leg2_value = _to_optional_value(row.get(leg2_col))
            root_radius_value = _to_float_optional(row.get(root_radius_col), 0.0) if root_radius_col else 0.0
            toe_radius_value = _to_float_optional(row.get(toe_radius_col), 0.0) if toe_radius_col else 0.0

            if leg2_value is None and leg1_value is not None:
                leg2_value = leg1_value

            sections.append(
                SectionRow(
                    designation=designation,
                    family=family_value or None,
                    depth=depth_value,
                    width=width_value,
                    leg1=leg1_value,
                    leg2=leg2_value,
                    flange_thickness=_to_optional_value(row.get(flange_thickness_col)),
                    web_thickness=_to_optional_value(row.get(web_thickness_col)),
                    wall_thickness=_to_optional_value(row.get(wall_thickness_col)),
                    root_radius=root_radius_value,
                    toe_radius=toe_radius_value,
                )
            )

    if not sections:
        raise ValueError(f"CSV '{csv_path}' has no valid section rows.")
    return sections


def _key(pt: tuple[float, float], ndigits: int = 8) -> tuple[float, float]:
    return (round(pt[0], ndigits), round(pt[1], ndigits))


def _polygon_from_geometry(geom: Geometry) -> Polygon:
    if not isinstance(geom.geom, Polygon):
        raise ValueError("Expected section geometry to be a single polygon.")
    return geom.geom


def _infer_shape(section: SectionRow, csv_path: Path) -> str:
    tokens = " ".join(
        token.upper()
        for token in [csv_path.stem, section.family or "", section.designation]
        if token
    )

    if any(tag in tokens for tag in ("PFC", "CHANNEL", "CFC", "C SECTION", "C-SECTION")):
        return "channel"
    if any(tag in tokens for tag in ("SHS", "RHS", "HOLLOW", "HSS", "BOX")):
        return "rhs"
    if any(tag in tokens for tag in ("EA", "ANGLE", "L SECTION", "L-SECTION", "EQUAL ANGLE", "UNEQUAL ANGLE")):
        return "angle"
    if any(tag in tokens for tag in ("UB", "UC", "WB", "I SECTION", "I-SECTION", "IBEAM", "I BEAM", "H-BEAM", "H BEAM")):
        return "i"
    return "channel"


def _require_thickness(value: float | None, name: str, designation: str) -> float:
    if value is None:
        raise ValueError(f"Section '{designation}' is missing required '{name}' thickness column.")
    return value


def _require_dimension(value: float | None, name: str, designation: str) -> float:
    if value is None:
        raise ValueError(f"Section '{designation}' is missing required '{name}' dimension column.")
    return value


def _create_geometry(section: SectionRow, shape: str, n_r: int = 16) -> Geometry:
    r_root = max(0.0, section.root_radius)

    if shape == "rhs":
        d = _require_dimension(section.depth, "d", section.designation)
        b = _require_dimension(section.width, "b", section.designation)
        t = _require_thickness(section.wall_thickness, "t", section.designation)

        if min(d, b, t) <= 0:
            raise ValueError(f"Section '{section.designation}' has non-positive dimensions.")
        if 2 * t >= min(d, b):
            raise ValueError(
                f"Section '{section.designation}' has invalid hollow thickness: 2*t must be smaller than min(d, b)."
            )
        return rectangular_hollow_section(d=d, b=b, t=t, r_out=r_root, n_r=n_r)

    if shape == "angle":
        d = _require_dimension(section.leg1, "b1", section.designation)
        b = _require_dimension(section.leg2, "b2/b1", section.designation)
        t = _require_thickness(section.wall_thickness, "t", section.designation)
        r_toe = min(max(0.0, section.toe_radius), t)

        if min(d, b, t) <= 0:
            raise ValueError(f"Section '{section.designation}' has non-positive dimensions.")
        if t >= min(d, b):
            raise ValueError(
                f"Section '{section.designation}' has invalid angle thickness: t must be smaller than both leg lengths."
            )
        return angle_section(d=d, b=b, t=t, r_r=r_root, r_t=r_toe, n_r=n_r)

    d = _require_dimension(section.depth, "d", section.designation)
    b = _require_dimension(section.width, "b/bf", section.designation)
    tf = _require_thickness(section.flange_thickness, "tf", section.designation)
    tw = _require_thickness(section.web_thickness, "tw", section.designation)

    if min(d, b, tf, tw) <= 0:
        raise ValueError(f"Section '{section.designation}' has non-positive dimensions.")
    if 2 * tf >= d:
        raise ValueError(
            f"Section '{section.designation}' has invalid thickness: 2*tf must be smaller than d."
        )
    if tw >= b:
        raise ValueError(
            f"Section '{section.designation}' has invalid thickness: tw must be smaller than width."
        )

    if shape == "channel":
        return channel_section(d=d, b=b, t_f=tf, t_w=tw, r=r_root, n_r=n_r)
    if shape == "i":
        return i_section(d=d, b=b, t_f=tf, t_w=tw, r=r_root, n_r=n_r)

    raise ValueError(f"Unsupported shape '{shape}' for section '{section.designation}'.")


def _extruded_obj(
    section: SectionRow,
    length: float,
    shape: str,
    csv_path: Path,
    n_r: int = 16,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    if length <= 0:
        raise ValueError(f"Section '{section.designation}' has non-positive extrusion length.")

    resolved_shape = shape if shape != "auto" else _infer_shape(section, csv_path)
    geom = _create_geometry(section, resolved_shape, n_r=n_r)
    polygon = _polygon_from_geometry(geom)

    bounds = polygon.bounds
    y_offset = (bounds[1] + bounds[3]) / 2.0

    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    index_by_point: dict[tuple[float, float], tuple[int, int]] = {}

    def ensure_point(pt: tuple[float, float]) -> tuple[int, int]:
        k = _key(pt)
        pair = index_by_point.get(k)
        if pair is not None:
            return pair

        y = pt[0]
        z = pt[1] - y_offset
        i0 = len(vertices) + 1
        vertices.append((0.0, y, z))
        i1 = len(vertices) + 1
        vertices.append((length, y, z))
        index_by_point[k] = (i0, i1)
        return i0, i1

    for a_idx, b_idx in geom.facets:
        pa = geom.points[a_idx]
        pb = geom.points[b_idx]
        a0, a1 = ensure_point(pa)
        b0, b1 = ensure_point(pb)

        faces.append((a0, b0, b1))
        faces.append((a0, b1, a1))

    for tri in triangulate(polygon):
        if not polygon.covers(tri.representative_point()):
            continue

        coords = list(tri.exterior.coords)[:3]
        p0 = (coords[0][0], coords[0][1])
        p1 = (coords[1][0], coords[1][1])
        p2 = (coords[2][0], coords[2][1])

        p0_0, p0_1 = ensure_point(p0)
        p1_0, p1_1 = ensure_point(p1)
        p2_0, p2_1 = ensure_point(p2)

        faces.append((p0_0, p1_0, p2_0))
        faces.append((p2_1, p1_1, p0_1))

    return vertices, faces


def _write_obj(
    path: Path,
    object_name: str,
    vertices: Sequence[tuple[float, float, float]],
    faces: Iterable[tuple[int, int, int]],
) -> None:
    safe_object_name = _sanitize_obj_name(object_name)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Generated by steel-section-obj-builder\n")
        handle.write(f"o {safe_object_name}\n")
        handle.write(f"g {safe_object_name}\n")
        for x, y, z in vertices:
            handle.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")
        for a, b, c in faces:
            handle.write(f"f {a} {b} {c}\n")


def _sanitize_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


def _sanitize_obj_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value.strip())
    cleaned = cleaned.strip("_")
    if not cleaned:
        return "section"
    if not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"section_{cleaned}"
    return cleaned


def _length_label(length: float) -> str:
    return f"{length:g}mm"


def _resolve_csv_inputs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".csv":
            raise typer.BadParameter("Input file must be a .csv file.", param_hint="input_path")
        return [input_path]

    if input_path.is_dir():
        csv_files = sorted(p for p in input_path.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
        if not csv_files:
            raise typer.BadParameter("Input directory contains no .csv files.", param_hint="input_path")
        return csv_files

    raise typer.BadParameter("Input path must be a CSV file or a directory.", param_hint="input_path")


@app.command()
def generate(
    input_path: Path = typer.Argument(..., exists=True, readable=True, help="Input CSV file or directory containing CSV files."),
    output_dir: Path = typer.Option(
        Path("output"),
        "--output-dir",
        "-o",
        help="Directory for generated files.",
    ),
    length: float = typer.Option(
        1000.0,
        "--length",
        min=0.000001,
        help="Extrusion length in mm (default: 1000).",
    ),
    length_in_name: bool = typer.Option(
        True,
        "--length-in-name/--no-length-in-name",
        help="Include length suffix in output filenames.",
    ),
    shape: str = typer.Option(
        "auto",
        "--shape",
        help="Section shape: auto, channel, i, rhs, or angle. Auto infers from CSV name/family/designation.",
    ),
    fmt: str = typer.Option("obj", "--format", help="Output format. Currently only 'obj'."),
    designation: list[str] | None = typer.Option(
        None,
        "--designation",
        "-d",
        help="Generate only selected designation(s). Can be repeated.",
    ),
) -> None:
    """Generate 3D section models by extruding section profiles along length."""
    output_format = fmt.strip().lower()
    if output_format != "obj":
        raise typer.BadParameter("Only 'obj' format is currently supported.", param_hint="--format")

    shape_value = shape.strip().lower()
    if shape_value not in VALID_SHAPES:
        raise typer.BadParameter(
            "Shape must be one of: auto, channel, i, rhs, angle.",
            param_hint="--shape",
        )

    csv_paths = _resolve_csv_inputs(input_path)

    selected = {
        item.strip().lower()
        for item in (designation or [])
        if item and item.strip()
    }

    output_dir.mkdir(parents=True, exist_ok=True)

    total_generated = 0
    total_errors: list[str] = []
    matched_selected = not selected

    for csv_path in csv_paths:
        try:
            sections = _read_sections(csv_path)
        except ValueError as exc:
            total_errors.append(f"{csv_path.name}: {exc}")
            continue

        if selected:
            before = len(sections)
            sections = [s for s in sections if s.designation.lower() in selected]
            if sections:
                matched_selected = True
            elif before > 0:
                continue

        file_errors: list[str] = []
        for section in tqdm(sections, desc=f"Generating {csv_path.name}", unit="section"):
            try:
                vertices, faces = _extruded_obj(section, length, shape_value, csv_path)
                if length_in_name:
                    output_name = _sanitize_filename(f"{section.designation}_{_length_label(length)}")
                else:
                    output_name = _sanitize_filename(section.designation)
                out_path = output_dir / f"{output_name}.{output_format}"
                _write_obj(out_path, section.designation, vertices, faces)
                total_generated += 1
            except ValueError as exc:
                file_errors.append(f"{section.designation}: {exc}")

        for message in file_errors:
            total_errors.append(f"{csv_path.name}: {message}")

    if selected and not matched_selected:
        raise typer.BadParameter(
            "No matching designations found in input CSV file(s).",
            param_hint="--designation",
        )

    typer.echo(
        f"Generated {total_generated} file(s) in '{output_dir}' from {len(csv_paths)} CSV file(s)."
    )

    if total_errors:
        for message in total_errors:
            typer.echo(f"Skipped: {message}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    app()

