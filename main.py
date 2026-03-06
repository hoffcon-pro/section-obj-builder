from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import typer
from sectionproperties.pre.geometry import Geometry
from sectionproperties.pre.library import channel_section, i_section
from shapely.geometry import Polygon
from shapely.ops import triangulate
from tqdm import tqdm

app = typer.Typer(add_completion=False, help="Generate extruded 3D section models from CSV data.")
VALID_SHAPES = {"auto", "channel", "i"}


@dataclass(frozen=True)
class SectionRow:
    designation: str
    family: str | None
    depth: float
    flange_width: float
    flange_thickness: float
    web_thickness: float
    root_radius: float


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
        depth_col = source_col("d")
        flange_width_col = source_col("bf")
        flange_thickness_col = source_col("tf")
        web_thickness_col = source_col("tw")
        root_radius_col = source_col_optional("r1", "r")

        sections: list[SectionRow] = []
        for row in reader:
            designation = (row.get(designation_col) or "").strip()
            if not designation:
                continue

            family_value = (row.get(family_col) or "").strip() if family_col else ""
            root_radius_value = _to_float_optional(row.get(root_radius_col), 0.0) if root_radius_col else 0.0

            sections.append(
                SectionRow(
                    designation=designation,
                    family=family_value or None,
                    depth=_to_float(row.get(depth_col), depth_col, designation),
                    flange_width=_to_float(row.get(flange_width_col), flange_width_col, designation),
                    flange_thickness=_to_float(
                        row.get(flange_thickness_col), flange_thickness_col, designation
                    ),
                    web_thickness=_to_float(row.get(web_thickness_col), web_thickness_col, designation),
                    root_radius=root_radius_value,
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
    if any(tag in tokens for tag in ("UB", "UC", "WB", "I SECTION", "I-SECTION", "IBEAM", "I BEAM", "H-BEAM", "H BEAM")):
        return "i"
    return "channel"


def _create_geometry(section: SectionRow, shape: str, n_r: int = 16) -> Geometry:
    d = section.depth
    bf = section.flange_width
    tf = section.flange_thickness
    tw = section.web_thickness
    r = max(0.0, section.root_radius)

    if min(d, bf, tf, tw) <= 0:
        raise ValueError(f"Section '{section.designation}' has non-positive dimensions.")
    if 2 * tf >= d:
        raise ValueError(
            f"Section '{section.designation}' has invalid thickness: 2*tf must be smaller than d."
        )
    if tw >= bf:
        raise ValueError(
            f"Section '{section.designation}' has invalid thickness: tw must be smaller than bf."
        )

    if shape == "channel":
        return channel_section(d=d, b=bf, t_f=tf, t_w=tw, r=r, n_r=n_r)
    if shape == "i":
        return i_section(d=d, b=bf, t_f=tf, t_w=tw, r=r, n_r=n_r)
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
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Generated by steel-section-obj-builder\n")
        handle.write(f"o {object_name}\n")
        for x, y, z in vertices:
            handle.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")
        for a, b, c in faces:
            handle.write(f"f {a} {b} {c}\n")


def _sanitize_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


def _length_label(length: float) -> str:
    return f"{length:g}mm"


@app.command()
def generate(
    csv_path: Path = typer.Argument(..., exists=True, readable=True, help="Input section CSV file."),
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
        help="Section shape: auto, channel, or i. Auto infers from CSV name/family/designation.",
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
            "Shape must be one of: auto, channel, i.",
            param_hint="--shape",
        )

    sections = _read_sections(csv_path)

    selected = {
        item.strip().lower()
        for item in (designation or [])
        if item and item.strip()
    }
    if selected:
        sections = [s for s in sections if s.designation.lower() in selected]
        if not sections:
            raise typer.BadParameter(
                "No matching designations found in input CSV.",
                param_hint="--designation",
            )

    output_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    for section in tqdm(sections, desc="Generating", unit="section"):
        try:
            vertices, faces = _extruded_obj(section, length, shape_value, csv_path)
            if length_in_name:
                output_name = _sanitize_filename(f"{section.designation}_{_length_label(length)}")
            else:
                output_name = _sanitize_filename(section.designation)
            out_path = output_dir / f"{output_name}.{output_format}"
            _write_obj(out_path, section.designation, vertices, faces)
        except ValueError as exc:
            errors.append(str(exc))

    typer.echo(f"Generated {len(sections) - len(errors)} file(s) in '{output_dir}'.")

    if errors:
        for message in errors:
            typer.echo(f"Skipped: {message}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
