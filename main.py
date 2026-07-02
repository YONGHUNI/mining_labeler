import site
import sys
import os
import re
import math
import io
import base64
import shutil
from urllib.request import Request, urlopen
from datetime import datetime
import pandas as pd
import warnings
from branca.element import MacroElement
import json
import html as html_lib
from jinja2 import Template

# =========================================================================
# GLOBAL CONFIGURATION
# =========================================================================
# Number of patches per row/col in a grouped matrix bin (3x3 = 9 patches)
SUPER_GRID_SIZE = 3  

# =========================================================================
# PATH NORMALIZATION UTILITY (RESOLVES CROSS-SESSION LOADING BUG)
# =========================================================================
def normalize_spatial_path(path: str) -> str:
    """
    Normalizes a filesystem path into a platform-independent unified string 
    with forward slashes to prevent cross-session matching bugs.
    """
    if not path:
        return ""
    return os.path.normpath(path).replace('\\', '/')

def get_script_dir() -> str:
    return normalize_spatial_path(os.path.dirname(os.path.abspath(__file__)))

def get_labeling_output_dir() -> str:
    return normalize_spatial_path(os.path.join(get_script_dir(), "labeling_output"))

def get_gpkg_output_dir() -> str:
    return normalize_spatial_path(os.path.join(get_labeling_output_dir(), "gpkg"))

def get_nature_mine_gpkg_path() -> str:
    candidates = [
        os.path.join(get_script_dir(), "data", "nature_mine_poly_nearest.gpkg"),
        os.path.join(get_script_dir(), "data", "shp", "nature_mine_poly_nearest.gpkg"),
        os.path.join(get_script_dir(), "nature_mine_poly_nearest.gpkg"),
    ]
    for path in candidates:
        norm_path = normalize_spatial_path(path)
        if os.path.exists(norm_path):
            return norm_path
    return ""

def get_map_base_url():
    base_dir = get_script_dir()
    if not base_dir.endswith("/"):
        base_dir += "/"
    return QUrl.fromLocalFile(base_dir)

def get_stable_scene_tif_path(tif_path: str) -> str:
    """
    Stores scene paths relative to main.py when possible so resume records do
    not depend on which working directory the user selected.
    """
    norm_tif = normalize_spatial_path(tif_path)
    script_dir = get_script_dir()
    try:
        return normalize_spatial_path(os.path.relpath(norm_tif, script_dir))
    except ValueError:
        return norm_tif

def extract_scene_uid(scene_name: str) -> str:
    match = re.search(r'([A-Z]{3})[-_]([CIG])[-_](\d+)', scene_name, re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}-{match.group(2).upper()}-{match.group(3)}"
    return normalize_spatial_path(scene_name)

def extract_output_identifier(scene_name: str) -> str:
    match = re.search(r'([A-Z]{3})[-_]([CIG])[-_](\d+)', str(scene_name), re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}_{match.group(2).upper()}_{match.group(3)}"
    return str(scene_name).replace("-", "_")

def make_annotation_key(scene_uid: str, grid_index: str) -> str:
    return f"{scene_uid}::{grid_index}"

def ensure_ogr_fields(layer, field_types: dict):
    layer_defn = layer.GetLayerDefn()
    existing = {layer_defn.GetFieldDefn(i).GetName() for i in range(layer_defn.GetFieldCount())}
    for name, field_type in field_types.items():
        if name not in existing:
            layer.CreateField(ogr.FieldDefn(name, field_type))

def make_srs(epsg: int):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    try:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    return srs

def grid_cell_polygon(cell: dict):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    ring.AddPoint_2D(cell["min_x"], cell["max_y"])
    ring.AddPoint_2D(cell["max_x"], cell["max_y"])
    ring.AddPoint_2D(cell["max_x"], cell["min_y"])
    ring.AddPoint_2D(cell["min_x"], cell["min_y"])
    ring.AddPoint_2D(cell["min_x"], cell["max_y"])
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly

def enrich_grids_with_nature_polygons(grid_cells: list, target_mineral: str, iso3_code: str):
    for cell in grid_cells:
        cell["nature_polygons"] = []
    gpkg_path = get_nature_mine_gpkg_path()
    if not target_mineral or not iso3_code or not gpkg_path:
        return

    driver = ogr.GetDriverByName("GPKG")
    ds = driver.Open(gpkg_path, 0)
    if ds is None:
        return
    layer = ds.GetLayerByName("nature_mine_poly_nearest") or ds.GetLayer(0)
    if layer is None:
        ds = None
        return

    srs4326 = make_srs(4326)
    srs3857 = make_srs(3857)
    to3857 = osr.CoordinateTransformation(srs4326, srs3857)
    to4326 = osr.CoordinateTransformation(srs3857, srs4326)
    safe_mineral = str(target_mineral).replace("'", "''")
    safe_iso3 = str(iso3_code).replace("'", "''")
    layer.SetAttributeFilter(f"Mining_Category = '{safe_mineral}' AND ISO3_CODE = '{safe_iso3}'")

    for cell in grid_cells:
        grid_geom = grid_cell_polygon(cell)
        grid_geom.AssignSpatialReference(srs4326)
        grid_3857 = grid_geom.Clone()
        grid_3857.Transform(to3857)
        layer.SetSpatialFilter(grid_3857)
        features = []
        for feat in layer:
            geom = feat.GetGeometryRef()
            if geom is None or not geom.Intersects(grid_3857):
                continue
            geom_4326 = geom.Clone()
            geom_4326.Transform(to4326)
            features.append({
                "type": "Feature",
                "geometry": json.loads(geom_4326.ExportToJson()),
                "properties": {
                    "Mine_Name": feat.GetField("Mine_Name") or "",
                    "Mining_Category": feat.GetField("Mining_Category") or "",
                    "Primary_Commodity": feat.GetField("Primary_Commodity") or "",
                    "Nearest_Dist_m": feat.GetField("Nearest_Dist_m"),
                },
            })
        cell["nature_polygons"] = features
        layer.ResetReading()
    layer.SetSpatialFilter(None)
    layer.SetAttributeFilter(None)
    ds = None

# =========================================================================
# 1. BULLETPROOF DLL PATCH & WARNING SUPPRESSION
# =========================================================================
warnings.filterwarnings("ignore", category=UserWarning)

if sys.platform == 'win32':
    for sp in site.getsitepackages():
        qt6_bin_path = os.path.join(sp, "PyQt6", "Qt6", "bin")
        if os.path.exists(qt6_bin_path):
            os.add_dll_directory(qt6_bin_path)
            os.environ['PATH'] = qt6_bin_path + os.pathsep + os.environ.get('PATH', '')
            break

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QFileDialog, QListWidget, 
                             QLabel, QMessageBox, QGroupBox, QAbstractItemView, 
                             QMenu, QListWidgetItem, QGridLayout, QRadioButton, 
                             QButtonGroup, QCheckBox, QComboBox, QDialog,
                             QTextBrowser, QFormLayout, QDialogButtonBox,
                             QKeySequenceEdit)
from PyQt6.QtCore import Qt, QUrl, QThread, pyqtSignal, QObject, QEvent
from PyQt6.QtGui import QColor, QKeyEvent, QKeySequence, QShortcut, QPalette
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings

import folium
from osgeo import gdal, ogr, osr
gdal.UseExceptions()


TAXONOMY_DEFINITIONS = {
    "spoil_heap": {
        "label": "Waste Heap",
        "fig_dir": "Spoil",
        "desc_en": "Extensive accumulation of bare waste rock typically associated with open-pit mining. Common traits include terraced design, flat-topped structure, disorganized pile patterns, and small adjacent piles.",
        "desc_ko": "노천 채굴지 주변에 쌓인 폐석 더미. 계단식 지형, 평평한 상단, 불규칙한 더미 패턴, 인접한 작은 더미가 단서가 될 수 있음.",
    },
    "processing_building": {
        "label": "Processing Building",
        "fig_dir": "Processing_Building",
        "desc_en": "Large metal industrial structure(s) near mine, often with elevated conveyors attached and/or circular pools (similar to sewage treatment plants)",
        "desc_ko": "광산 근처의 대형 금속 산업 구조물. 높은 컨베이어가 연결되어 있거나 원형 수조가 함께 보이는 경우가 많음.",
    },
    "related_building": {
        "label": "Related Building",
        "fig_dir": "Related_Building",
        "desc_en": "Administrative or support structure within the mine operational perimeter. Exclude residential buildings and power plants.",
        "desc_ko": "광산 운영 경계 안에 있는 행정 또는 지원 목적의 구조물. 주거 건물과 발전소는 제외.",
    },
    "tailings_pond": {
        "label": "Tailings Pond",
        "fig_dir": "Tailings_Pond",
        "desc_en": "Discoloured impoundment adjacent to the processing area",
        "desc_ko": "처리 시설 인근에 위치한 변색된 저류지 또는 침전지.",
    },
    "rectangular_pond": {
        "label": "Artificial Pond",
        "fig_dir": "Rectangular_Pond",
        "desc_en": "Small engineered industrial water body near a processing building, often with sharp boundaries and murky or contaminated-looking water. It is not always perfectly rectangular; exclude ponds that appear unrelated to mining, such as distant irrigation, livestock, or decorative ponds.",
        "desc_ko": "처리 건물 인근의 작은 인공 산업용 수체. 날카로운 경계와 탁하거나 오염된 듯한 물색이 단서가 되며, 항상 완벽한 직사각형일 필요는 없음. 광산 시설과 멀리 떨어진 관개, 가축, 조경 목적의 연못은 제외.",
    },
    "open_pit": {
        "label": "Open Pit",
        "fig_dir": "Open_Pit",
        "desc_en": "Exposed excavation face, sometimes with distinct step-terrace geometry. Exclude pits that are completely or mostly filled with water.",
        "desc_ko": "노출된 채굴면. 뚜렷한 계단식 테라스 형태가 단서가 될 수 있음. 완전히 또는 대부분 물로 차 있는 pit은 제외.",
    },
}

DESCRIPTOR_GUIDE_ORDER = [
    "spoil_heap",
    "open_pit",
    "processing_building",
    "related_building",
    "tailings_pond",
    "rectangular_pond",
]

COMPONENT_KEYS = ["spoil_heap", "processing_building", "related_building", "tailings_pond", "rectangular_pond", "open_pit"]
QUALITY_FLAG_KEY = "quality_flag"
LEGACY_FLAG_KEYS = ["error_flag", "unidentifiable_flag"]
QUALITY_FLAG_DESC = "Mark when the 3x3 bin has broken imagery, missing tiles, or cannot be confidently interpreted."
DEFAULT_KEYBINDINGS = {
    "toggle_component": "A",
    "next_component": "J",
    "previous_component": "K",
    "complete_next_bin": "Return",
    "previous_bin": "Shift+Return",
    "undo": "Ctrl+Z",
    "redo": "Ctrl+Shift+Z",
}
KEYBINDING_LABELS = {
    "toggle_component": "Toggle current component",
    "next_component": "Next component",
    "previous_component": "Previous component",
    "complete_next_bin": "Complete bin and move next",
    "previous_bin": "Move to previous bin",
    "undo": "Undo",
    "redo": "Redo",
}


class TaxonomyDataManager:
    """
    Manages central CSV persistence utilizing Python DataFrames.
    Maps native patch matrix bounds to structural taxonomy evaluations.
    """
    def __init__(self, folder_path: str):
        self.workspace_folder = normalize_spatial_path(folder_path)
        self.folder_path = get_labeling_output_dir()
        os.makedirs(self.folder_path, exist_ok=True)
        self.csv_path = normalize_spatial_path(os.path.join(self.folder_path, "nk_mining_taxonomy.csv"))
        self.headers = [
            "annotation_key", "scene_uid", "scene_name",
            "scene_tif_path", "identifier", "grid_index", "mining_category", "mine_point_count",
            "top_left_jpg", "bottom_right_jpg",
            "spoil_heap", "processing_building", "related_building",
            "tailings_pond", "rectangular_pond", "open_pit",
            "quality_flag", "eval_start", "eval_end"
        ]
        self.df = self._load_or_create_csv()

    def _legacy_csv_candidates(self) -> list:
        candidates = [
            normalize_spatial_path(os.path.join(get_script_dir(), "nk_mining_taxonomy.csv")),
            normalize_spatial_path(os.path.join(self.workspace_folder, "nk_mining_taxonomy.csv")),
        ]
        return [p for i, p in enumerate(candidates) if p and p not in candidates[:i]]

    def _normalize_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.loc[:, ~df.columns.duplicated()].copy()
        if "scene_tif_path" in df.columns:
            df["scene_tif_path"] = df["scene_tif_path"].apply(normalize_spatial_path)
        if "scene_name" in df.columns:
            if "identifier" not in df.columns:
                df["identifier"] = ""
            scene_identifiers = df["scene_name"].fillna("").astype(str).apply(extract_output_identifier)
            current_identifiers = df["identifier"].fillna("").astype(str).str.strip()
            df["identifier"] = current_identifiers
            df.loc[(current_identifiers == "") | current_identifiers.str.contains("-", regex=False), "identifier"] = scene_identifiers
        if "evaluated_at" in df.columns:
            if "eval_end" not in df.columns:
                df["eval_end"] = ""
            legacy_end = df["evaluated_at"].fillna("").astype(str).str.strip()
            df["eval_end"] = df["eval_end"].fillna("").astype(str)
            df.loc[df["eval_end"].str.strip() == "", "eval_end"] = legacy_end
            df = df.drop(columns=["evaluated_at"])
        for col in ["eval_start", "eval_end"]:
            if col in df.columns:
                df[col] = df[col].fillna("").astype(str)
        if QUALITY_FLAG_KEY not in df.columns:
            df[QUALITY_FLAG_KEY] = 0
        quality_text = df[QUALITY_FLAG_KEY].fillna("").astype(str).str.strip().str.lower()
        quality_flag = quality_text.isin(["1", "true", "yes", "y", "error", "unidentifiable", "unidentified", "unknown"]).astype(int)
        numeric_quality = pd.to_numeric(df[QUALITY_FLAG_KEY], errors="coerce").fillna(0).astype(int).clip(0, 1)
        quality_flag = pd.Series(quality_flag, index=df.index).mask(numeric_quality == 1, 1)
        if "unidentifiable_flag" in df.columns:
            legacy_unidentifiable = pd.to_numeric(
                df["unidentifiable_flag"], errors="coerce"
            ).fillna(0).astype(int).clip(0, 1)
            quality_flag = quality_flag.mask(legacy_unidentifiable == 1, 1)
        if "error_flag" in df.columns:
            legacy_error = pd.to_numeric(
                df["error_flag"], errors="coerce"
            ).fillna(0).astype(int).clip(0, 1)
            quality_flag = quality_flag.mask(legacy_error == 1, 1)
        df[QUALITY_FLAG_KEY] = pd.to_numeric(quality_flag, errors="coerce").fillna(0).astype(int).clip(0, 1)
        df = df.drop(columns=[col for col in LEGACY_FLAG_KEYS if col in df.columns])

        bool_cols = COMPONENT_KEYS + [QUALITY_FLAG_KEY]
        for col in self.headers:
            if col not in df.columns:
                df[col] = 0 if col in bool_cols else ""
        for col in bool_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int).clip(0, 1)
        ordered_cols = self.headers + [col for col in df.columns if col not in self.headers]
        df = df.reindex(columns=ordered_cols)
        return df

    def external_csv_candidates(self) -> list:
        paths = []
        for name in sorted(os.listdir(self.folder_path)):
            path = normalize_spatial_path(os.path.join(self.folder_path, name))
            if not name.lower().endswith(".csv"):
                continue
            if path == self.csv_path:
                continue
            lower_name = name.lower()
            if lower_name.startswith("nk_mining_taxonomy.backup_"):
                continue
            paths.append(path)
        return paths

    def backup_csv(self) -> str:
        if not os.path.exists(self.csv_path):
            return ""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = normalize_spatial_path(os.path.join(self.folder_path, f"nk_mining_taxonomy.backup_{stamp}.csv"))
        shutil.copy2(self.csv_path, backup_path)
        return backup_path

    def _merge_key_for_row(self, row: pd.Series) -> str:
        annotation_key = str(row.get("annotation_key", "")).strip()
        if annotation_key:
            return f"annotation::{annotation_key}"
        scene_uid = str(row.get("scene_uid", "")).strip()
        grid_index = str(row.get("grid_index", "")).strip()
        if scene_uid and grid_index:
            return f"scene::{scene_uid}::{grid_index}"
        scene_tif_path = normalize_spatial_path(str(row.get("scene_tif_path", "")).strip())
        if scene_tif_path and grid_index:
            return f"path::{scene_tif_path}::{grid_index}"
        return ""

    def _is_blank_value(self, value) -> bool:
        if pd.isna(value):
            return True
        text = str(value).strip()
        return text == "" or text.lower() == "nan"

    def load_external_csv_dataframe(self, csv_path: str) -> pd.DataFrame:
        return self._normalize_dataframe(pd.read_csv(csv_path))

    def analyze_external_dataframe(self, incoming: pd.DataFrame, csv_path: str) -> dict:
        incoming = self._normalize_dataframe(incoming.copy())
        current = self._normalize_dataframe(self.df.copy())
        key_to_index = {}
        for idx, row in current.iterrows():
            key = self._merge_key_for_row(row)
            if key:
                key_to_index[key] = idx

        new_rows = 0
        matched_rows = 0
        conflict_rows = 0
        fillable_rows = 0
        missing_jpg_rows = 0
        scenes = set()
        for _, row in incoming.iterrows():
            scene_uid = str(row.get("scene_uid", "")).strip()
            if scene_uid:
                scenes.add(scene_uid)
            if self._is_blank_value(row.get("top_left_jpg", "")) or self._is_blank_value(row.get("bottom_right_jpg", "")):
                missing_jpg_rows += 1

            key = self._merge_key_for_row(row)
            if not key or key not in key_to_index:
                new_rows += 1
                continue

            matched_rows += 1
            target_idx = key_to_index[key]
            has_conflict = False
            has_fillable = False
            for col in self.headers:
                incoming_value = row.get(col, "")
                current_value = current.at[target_idx, col] if col in current.columns else ""
                if self._is_blank_value(incoming_value):
                    continue
                if self._is_blank_value(current_value):
                    has_fillable = True
                elif str(current_value) != str(incoming_value):
                    has_conflict = True
            if has_conflict:
                conflict_rows += 1
            if has_fillable:
                fillable_rows += 1

        return {
            "path": csv_path,
            "rows": int(len(incoming)),
            "new_rows": new_rows,
            "matched_rows": matched_rows,
            "conflict_rows": conflict_rows,
            "fillable_rows": fillable_rows,
            "missing_jpg_rows": missing_jpg_rows,
            "scene_count": len(scenes),
            "scenes": scenes,
        }

    def analyze_external_csv(self, csv_path: str) -> dict:
        return self.analyze_external_dataframe(self.load_external_csv_dataframe(csv_path), csv_path)

    def merge_external_dataframe(self, incoming: pd.DataFrame, csv_path: str, conflict_policy: str) -> dict:
        incoming = self._normalize_dataframe(incoming.copy())
        if incoming.empty:
            return {"added": 0, "updated": 0, "conflicts": 0, "backup": ""}

        backup_path = self.backup_csv()
        current = self._normalize_dataframe(self.df.copy())
        if current.empty:
            current = pd.DataFrame(columns=self.headers)

        key_to_index = {}
        for idx, row in current.iterrows():
            key = self._merge_key_for_row(row)
            if key:
                key_to_index[key] = idx

        added = 0
        updated = 0
        conflicts = 0
        for _, row in incoming.iterrows():
            key = self._merge_key_for_row(row)
            if not key or key not in key_to_index:
                current = pd.concat([current, pd.DataFrame([row])], ignore_index=True)
                if key:
                    key_to_index[key] = current.index[-1]
                added += 1
                continue

            conflicts += 1
            target_idx = key_to_index[key]
            changed = False
            for col in self.headers:
                incoming_value = row.get(col, "")
                current_value = current.at[target_idx, col] if col in current.columns else ""
                if conflict_policy == "overwrite":
                    if not self._is_blank_value(incoming_value) and str(current_value) != str(incoming_value):
                        current.at[target_idx, col] = incoming_value
                        changed = True
                else:
                    if self._is_blank_value(current_value) and not self._is_blank_value(incoming_value):
                        current.at[target_idx, col] = incoming_value
                        changed = True
            if changed:
                updated += 1

        self.df = self._normalize_dataframe(current)
        self.df.to_csv(self.csv_path, index=False)
        return {"added": added, "updated": updated, "conflicts": conflicts, "backup": backup_path}

    def merge_external_csv(self, csv_path: str, conflict_policy: str) -> dict:
        return self.merge_external_dataframe(self.load_external_csv_dataframe(csv_path), csv_path, conflict_policy)

    def archive_imported_csv(self, csv_path: str) -> str:
        imported_dir = normalize_spatial_path(os.path.join(self.folder_path, "imported"))
        os.makedirs(imported_dir, exist_ok=True)
        base = os.path.basename(csv_path)
        target = normalize_spatial_path(os.path.join(imported_dir, base))
        if os.path.exists(target):
            stem, ext = os.path.splitext(base)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = normalize_spatial_path(os.path.join(imported_dir, f"{stem}_{stamp}{ext}"))
        shutil.move(csv_path, target)
        return target

    def _load_or_create_csv(self) -> pd.DataFrame:
        if os.path.exists(self.csv_path):
            try:
                df = pd.read_csv(self.csv_path)
                df = self._normalize_dataframe(df)
                df.to_csv(self.csv_path, index=False)
                return df
            except Exception:
                return pd.DataFrame(columns=self.headers)

        for legacy_path in self._legacy_csv_candidates():
            if os.path.exists(legacy_path):
                try:
                    df = self._normalize_dataframe(pd.read_csv(legacy_path))
                    df.to_csv(self.csv_path, index=False)
                    return df
                except Exception:
                    pass

        df = pd.DataFrame(columns=self.headers)
        df.to_csv(self.csv_path, index=False)
        return df

    def _clean_timestamp_value(self, value) -> str:
        if pd.isna(value):
            return ""
        text = str(value).strip()
        return "" if text.lower() == "nan" else text

    def get_grid_record(self, tif_path: str, grid_index: str, annotation_key: str = None) -> dict:
        if annotation_key and "annotation_key" in self.df.columns:
            key_match = self.df[self.df["annotation_key"] == annotation_key]
            if not key_match.empty:
                return key_match.iloc[0].to_dict()

        norm_path = normalize_spatial_path(tif_path)
        match = self.df[(self.df["scene_tif_path"] == norm_path) & (self.df["grid_index"] == grid_index)]
        return match.iloc[0].to_dict() if not match.empty else None

    def is_grid_evaluated(self, tif_path: str, grid_index: str, annotation_key: str = None) -> bool:
        row = self.get_grid_record(tif_path, grid_index, annotation_key)
        if not row:
            return False
        eval_end = str(row.get("eval_end", "")).strip()
        return bool(eval_end)

    def is_scene_fully_evaluated(self, tif_path: str, total_grids: int, scene_uid: str = None) -> bool:
        if scene_uid and "scene_uid" in self.df.columns:
            scene_rows = self.df[self.df["scene_uid"] == scene_uid]
        else:
            norm_path = normalize_spatial_path(tif_path)
            scene_rows = self.df[self.df["scene_tif_path"] == norm_path]
        if len(scene_rows) < total_grids or total_grids == 0:
            return False
        completed = scene_rows["eval_end"].fillna("").astype(str).str.strip()
        if int((completed != "").sum()) < total_grids:
            return False
        for g_idx in scene_rows["grid_index"].unique():
            key = make_annotation_key(scene_uid, g_idx) if scene_uid else None
            if not self.is_grid_evaluated(tif_path, g_idx, key):
                return False
        return True

    def scene_evaluated_count(self, tif_path: str, scene_uid: str = None) -> int:
        if scene_uid and "scene_uid" in self.df.columns:
            scene_rows = self.df[self.df["scene_uid"] == scene_uid]
        else:
            norm_path = normalize_spatial_path(tif_path)
            scene_rows = self.df[self.df["scene_tif_path"] == norm_path]
        if scene_rows.empty or "eval_end" not in scene_rows.columns:
            return 0
        eval_end = scene_rows["eval_end"].fillna("").astype(str).str.strip()
        return int((eval_end != "").sum())

    def scene_record_count(self, tif_path: str, scene_uid: str = None) -> int:
        if scene_uid and "scene_uid" in self.df.columns:
            scene_rows = self.df[self.df["scene_uid"] == scene_uid]
        else:
            norm_path = normalize_spatial_path(tif_path)
            scene_rows = self.df[self.df["scene_tif_path"] == norm_path]
        if scene_rows.empty:
            return 0
        if "grid_index" in scene_rows.columns:
            return int(scene_rows["grid_index"].fillna("").astype(str).str.strip().replace("", pd.NA).dropna().nunique())
        return int(len(scene_rows))

    def save_or_update_grid(self, payload: dict, mark_started: bool = True, mark_completed: bool = False):
        now = datetime.now().isoformat()
        tif_path = normalize_spatial_path(payload["scene_tif_path"])
        payload["scene_tif_path"] = tif_path
        g_idx = payload["grid_index"]
        annotation_key = payload.get("annotation_key")
        
        if annotation_key:
            match_idx = self.df[self.df["annotation_key"] == annotation_key].index
        else:
            match_idx = self.df[(self.df["scene_tif_path"] == tif_path) & (self.df["grid_index"] == g_idx)].index
        if not match_idx.empty:
            row_idx = match_idx[0]
            existing_start = self._clean_timestamp_value(self.df.at[row_idx, "eval_start"]) if "eval_start" in self.df.columns else ""
            existing_end = self._clean_timestamp_value(self.df.at[row_idx, "eval_end"]) if "eval_end" in self.df.columns else ""
            if (mark_started or mark_completed) and not existing_start.strip():
                payload["eval_start"] = now
            else:
                payload["eval_start"] = existing_start
            payload["eval_end"] = now if mark_completed else existing_end
            for col, val in payload.items():
                if col not in self.df.columns:
                    self.df[col] = ""
                self.df.at[row_idx, col] = val
        else:
            payload["eval_start"] = now if (mark_started or mark_completed) else ""
            payload["eval_end"] = now if mark_completed else ""
            new_row = pd.DataFrame([payload])
            self.df = pd.concat([self.df, new_row], ignore_index=True)
        self.df = self._normalize_dataframe(self.df)
        self.df.to_csv(self.csv_path, index=False)

    def update_grid_jpg_metadata(self, records: list) -> int:
        changed = 0
        for record in records:
            annotation_key = record.get("annotation_key")
            tif_path = normalize_spatial_path(record.get("scene_tif_path", ""))
            grid_index = record.get("grid_index", "")
            if annotation_key and "annotation_key" in self.df.columns:
                match_idx = self.df[self.df["annotation_key"] == annotation_key].index
            else:
                match_idx = self.df[(self.df["scene_tif_path"] == tif_path) & (self.df["grid_index"] == grid_index)].index
            if match_idx.empty:
                continue
            row_idx = match_idx[0]
            for col in ["top_left_jpg", "bottom_right_jpg"]:
                value = record.get(col, "")
                if value and self._is_blank_value(self.df.at[row_idx, col]):
                    self.df.at[row_idx, col] = value
                    changed += 1
        if changed:
            self.df = self._normalize_dataframe(self.df)
            self.df.to_csv(self.csv_path, index=False)
        return changed

    def set_grid_eval_end(self, tif_path: str, grid_index: str, annotation_key: str = None, eval_end: str = ""):
        row = self.get_grid_record(tif_path, grid_index, annotation_key)
        if not row:
            return
        if annotation_key:
            match_idx = self.df[self.df["annotation_key"] == annotation_key].index
        else:
            norm_path = normalize_spatial_path(tif_path)
            match_idx = self.df[(self.df["scene_tif_path"] == norm_path) & (self.df["grid_index"] == grid_index)].index
        if not match_idx.empty:
            self.df.at[match_idx[0], "eval_end"] = eval_end
            self.df = self._normalize_dataframe(self.df)
            self.df.to_csv(self.csv_path, index=False)

    def purge_scene_records(self, scene_uid: str = None, scene_tif_path: str = None) -> int:
        if self.df.empty:
            return 0

        mask = pd.Series(False, index=self.df.index)
        if scene_uid and "scene_uid" in self.df.columns:
            mask = mask | (self.df["scene_uid"] == scene_uid)
        if scene_tif_path and "scene_tif_path" in self.df.columns:
            mask = mask | (self.df["scene_tif_path"] == normalize_spatial_path(scene_tif_path))

        removed = int(mask.sum())
        if removed:
            self.df = self.df.loc[~mask].copy()
            self.df.to_csv(self.csv_path, index=False)
        return removed

    def purge_all_records(self) -> int:
        removed = len(self.df)
        self.df = pd.DataFrame(columns=self.headers)
        if os.path.exists(self.csv_path):
            os.remove(self.csv_path)
        return removed


class ICMMDataLoader(QThread):
    """
    Asynchronously parses the ICMM dataset from local source files, with the
    ICMM-hosted workbook as a network fallback.
    """
    loading_finished = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.remote_xlsx_url = "https://www.icmm.com/website/data/2025/global-mining-dataset.xlsx"

    def run(self):
        try:
            base_script_dir = normalize_spatial_path(os.path.dirname(os.path.abspath(__file__)))
            local_xlsx_path = normalize_spatial_path(os.path.join(base_script_dir, "data", "global-mining-dataset.xlsx"))
            local_csv_fallback = normalize_spatial_path(os.path.join(base_script_dir, "data", "global-mining-dataset.csv"))

            if os.path.exists(local_xlsx_path):
                df = pd.read_excel(local_xlsx_path, sheet_name=1)
            elif os.path.exists(local_csv_fallback):
                df = pd.read_csv(local_csv_fallback)
            else:
                request = Request(self.remote_xlsx_url, headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(request) as response:
                    df = pd.read_excel(io.BytesIO(response.read()), sheet_name=1)

            df.columns = [re.sub(r'_+', '_', re.sub(r'[^0-9A-Za-z]+', '_', str(c)).strip('_')) for c in df.columns]
            required_cols = [
                "Latitude", "Longitude", "Confidence_Factor", "Country_or_Region",
                "Asset_Type", "Primary_Commodity"
            ]
            missing_cols = [col for col in required_cols if col not in df.columns]
            if missing_cols:
                raise KeyError(f"ICMM dataset missing required columns after normalization: {missing_cols}")
            
            for col in df.select_dtypes(include=['object', 'string']).columns:
                df[col] = df[col].astype(str).str.strip()

            df['Latitude'] = pd.to_numeric(df['Latitude'], errors='coerce')
            df['Longitude'] = pd.to_numeric(df['Longitude'], errors='coerce')
            df = df.dropna(subset=['Longitude', 'Latitude'])
            
            target_nations = ["United States", "Australia", "China"]
            df = df[
                df['Confidence_Factor'].isin(["High", "Moderate"]) &
                df['Country_or_Region'].isin(target_nations) &
                df['Asset_Type'].str.contains("Mine", na=False, case=False)
            ]
            
            def assign_category(commodity):
                c_str = str(commodity).lower().strip()
                if "coal" in c_str: return "Coal"
                if "gold" in c_str: return "Gold"
                if "iron" in c_str: return "Iron"
                return None

            df['Mining_Category'] = df['Primary_Commodity'].apply(assign_category)
            df = df.dropna(subset=['Mining_Category'])
            
            self.loading_finished.emit(df)
        except Exception as e:
            print(f"Local ICMM Ingestion Fault Stack Exception: {e}")
            empty_df = pd.DataFrame(columns=["Longitude", "Latitude", "Mining_Category", "Country_or_Region"])
            self.loading_finished.emit(empty_df)


class DirectoryScannerThread(QThread):
    """Scans root directories to isolate and cluster structured scene nodes."""
    scan_finished = pyqtSignal(dict)

    def __init__(self, target_dir: str):
        super().__init__()
        self.target_dir = normalize_spatial_path(target_dir)

    def _is_scene_folder_name(self, folder_name: str) -> bool:
        return bool(re.search(r'([A-Z]{3})[-_]([CIG])[-_](\d+)', folder_name, re.IGNORECASE))

    def _first_tif_in_folder(self, root: str, files: list) -> str:
        for file_name in sorted(files):
            if file_name.lower().endswith(('.tif', '.tiff')):
                return normalize_spatial_path(os.path.join(root, file_name))
        return ""

    def _has_georef_jpg_tiles(self, root: str) -> bool:
        for tile_root, _, tile_files in os.walk(root):
            lower_files = {name.lower() for name in tile_files}
            for file_name in tile_files:
                if not file_name.lower().endswith(".jpg"):
                    continue
                jgw_name = os.path.splitext(file_name)[0].lower() + ".jgw"
                if jgw_name in lower_files:
                    return True
        return False

    def _register_scene(self, scenes: dict, root: str, files: list):
        norm_root = normalize_spatial_path(root)
        scene_name = os.path.basename(norm_root)
        tif_path = self._first_tif_in_folder(norm_root, files) or norm_root
        scenes[norm_root] = {
            'scene_name': scene_name,
            'scene_abs_path': norm_root,
            'tif_path': tif_path,
            'grid_cells': None
        }

    def run(self):
        scenes = {}
        for root, dirs, files in os.walk(self.target_dir):
            norm_root = normalize_spatial_path(root)
            scene_named_folder = self._is_scene_folder_name(os.path.basename(norm_root))
            has_tif = bool(self._first_tif_in_folder(norm_root, files))
            if scene_named_folder and (has_tif or self._has_georef_jpg_tiles(norm_root)):
                self._register_scene(scenes, norm_root, files)
                dirs[:] = []
                continue
            if has_tif:
                self._register_scene(scenes, norm_root, files)
                dirs[:] = []
        self.scan_finished.emit(scenes)


# =========================================================================
# CORE LOGIC: NATIVE MATRIX BINNING WITH SCENE-WIDE ATTRIBUTE CALCULATIONS
# =========================================================================
def _read_jgw_transform(jgw_path: str) -> dict:
    with open(jgw_path, 'r') as jf:
        lines = [float(l.strip()) for l in jf.readlines()]
    x_scale, _, _, y_scale, top_left_x, top_left_y = lines[:6]
    return {
        "x_scale": x_scale,
        "y_scale": y_scale,
        "top_left_x": top_left_x,
        "top_left_y": top_left_y,
        "width_deg": 256 * abs(x_scale),
        "height_deg": 256 * abs(y_scale),
    }


def _scene_jpg_tiles(scene_key: str) -> list:
    tiles = []
    for root, _, files in os.walk(scene_key):
        norm_root = normalize_spatial_path(root)
        lower_files = {name.lower(): name for name in files}
        for f in files:
            if not f.lower().endswith('.jpg'):
                continue
            stem = os.path.splitext(f)[0]
            jgw_name = stem + '.jgw'
            actual_jgw_name = lower_files.get(jgw_name.lower())
            if not actual_jgw_name:
                continue
            match = re.search(r'_(\d+)_(\d+)_(\d+)$', stem)
            tile = {
                "jpg_path": normalize_spatial_path(os.path.join(norm_root, f)),
                "jgw_path": normalize_spatial_path(os.path.join(norm_root, actual_jgw_name)),
                "x": None,
                "y": None,
                "z": None,
            }
            if match:
                tile["x"] = int(match.group(1))
                tile["y"] = int(match.group(2))
                tile["z"] = int(match.group(3))
            tiles.append(tile)
    return tiles


def _patch_from_transform(jpg_path: str, transform: dict, top_left_x: float, top_left_y: float) -> dict:
    min_x = top_left_x
    max_y = top_left_y
    max_x = min_x + transform["width_deg"]
    min_y = max_y - transform["height_deg"]
    return {
        "jpg_path": jpg_path,
        "min_x": min_x, "max_x": max_x,
        "min_y": min_y, "max_y": max_y,
        "bounds": [[min_y, min_x], [max_y, max_x]],
        "width_deg": transform["width_deg"],
        "height_deg": transform["height_deg"]
    }


def _compile_patches_from_jgw_tiles(tiles: list) -> list:
    patches_info = []
    for tile in tiles:
        transform = _read_jgw_transform(tile["jgw_path"])
        patches_info.append(_patch_from_transform(
            tile["jpg_path"],
            transform,
            transform["top_left_x"],
            transform["top_left_y"],
        ))
    return patches_info


def _build_tile_index_models(tiles: list) -> dict:
    if not tiles or any(tile["x"] is None or tile["y"] is None or tile["z"] is None for tile in tiles):
        return {}
    by_zoom = {}
    for tile in tiles:
        by_zoom.setdefault(tile["z"], []).append(tile)

    models = {}
    for zoom, zoom_tiles in by_zoom.items():
        top_left_tile = min(zoom_tiles, key=lambda tile: (tile["x"], -tile["y"]))
        bottom_right_tile = max(zoom_tiles, key=lambda tile: (tile["x"], -tile["y"]))
        top_left_transform = _read_jgw_transform(top_left_tile["jgw_path"])
        bottom_right_transform = _read_jgw_transform(bottom_right_tile["jgw_path"])

        step_x = 256 * top_left_transform["x_scale"]
        if bottom_right_tile["x"] != top_left_tile["x"]:
            step_x = (
                bottom_right_transform["top_left_x"] - top_left_transform["top_left_x"]
            ) / (bottom_right_tile["x"] - top_left_tile["x"])
        else:
            for tile in zoom_tiles:
                if tile["x"] == top_left_tile["x"]:
                    continue
                ref_transform = _read_jgw_transform(tile["jgw_path"])
                step_x = (
                    ref_transform["top_left_x"] - top_left_transform["top_left_x"]
                ) / (tile["x"] - top_left_tile["x"])
                break

        step_y = top_left_transform["height_deg"]
        if bottom_right_tile["y"] != top_left_tile["y"]:
            step_y = (
                bottom_right_transform["top_left_y"] - top_left_transform["top_left_y"]
            ) / (bottom_right_tile["y"] - top_left_tile["y"])
        else:
            for tile in zoom_tiles:
                if tile["y"] == top_left_tile["y"]:
                    continue
                ref_transform = _read_jgw_transform(tile["jgw_path"])
                step_y = (
                    ref_transform["top_left_y"] - top_left_transform["top_left_y"]
                ) / (tile["y"] - top_left_tile["y"])
                break

        if abs(bottom_right_transform["width_deg"] - top_left_transform["width_deg"]) > 1e-12:
            return {}
        if abs(bottom_right_transform["height_deg"] - top_left_transform["height_deg"]) > 1e-12:
            return {}

        models[zoom] = {
            "anchor_x": top_left_tile["x"],
            "anchor_y": top_left_tile["y"],
            "anchor_top_left_x": top_left_transform["top_left_x"],
            "anchor_top_left_y": top_left_transform["top_left_y"],
            "step_x": step_x,
            "step_y": step_y,
            "width_deg": top_left_transform["width_deg"],
            "height_deg": top_left_transform["height_deg"],
        }
    return models


def _predict_indexed_tile_patch(tile: dict, model: dict) -> dict:
    transform = {
        "width_deg": model["width_deg"],
        "height_deg": model["height_deg"],
    }
    top_left_x = model["anchor_top_left_x"] + (tile["x"] - model["anchor_x"]) * model["step_x"]
    top_left_y = model["anchor_top_left_y"] + (tile["y"] - model["anchor_y"]) * model["step_y"]
    return _patch_from_transform(tile["jpg_path"], transform, top_left_x, top_left_y)


def _compile_patches_from_indexed_tiles(tiles: list) -> list:
    try:
        models = _build_tile_index_models(tiles)
        if not models:
            return []
        return [_predict_indexed_tile_patch(tile, models[tile["z"]]) for tile in tiles]
    except Exception:
        return []


def _compile_scene_grids(scene_key: str, scene_data: dict, icmm_df: pd.DataFrame, current_folder: str, data_manager: TaxonomyDataManager) -> list:
    """
    Compiles 256x256 image patches into native NxN matrices based on relative spatial indexing.
    Maintains existing GeoPackage layer vectors on disk if present, aligning attribute records.
    """
    scene_key = normalize_spatial_path(scene_key)
    current_folder = normalize_spatial_path(current_folder)
    
    scene_uid = extract_scene_uid(scene_data['scene_name'])
    match = re.search(r'([A-Z]{3})[-_]([CIG])[-_](\d+)', scene_data['scene_name'], re.IGNORECASE)
    identifier = extract_output_identifier(scene_data['scene_name']) if match else "UNKNOWN"
    iso3_code = match.group(1).upper() if match else ""
    target_mineral = {"C": "Coal", "G": "Gold", "I": "Iron"}.get(match.group(2).upper(), None) if match else None

    tiles = _scene_jpg_tiles(scene_key)
    patches_info = _compile_patches_from_indexed_tiles(tiles)
    if not patches_info:
        patches_info = _compile_patches_from_jgw_tiles(tiles)

    if not patches_info:
        return []

    scene_min_x = min(p["min_x"] for p in patches_info)
    scene_max_x = max(p["max_x"] for p in patches_info)
    scene_min_y = min(p["min_y"] for p in patches_info)
    scene_max_y = max(p["max_y"] for p in patches_info)

    scene_mine_point_count = 0
    if not icmm_df.empty and target_mineral:
        filtered = icmm_df[icmm_df['Mining_Category'] == target_mineral]
        matches = filtered[
            (filtered['Longitude'] >= scene_min_x) & (filtered['Longitude'] <= scene_max_x) &
            (filtered['Latitude'] >= scene_min_y) & (filtered['Latitude'] <= scene_max_y)
        ]
        scene_mine_point_count = len(matches)

    grouped_grids = {}

    for p in patches_info:
        col_idx = int(round((p["min_x"] - scene_min_x) / p["width_deg"]))
        row_idx = int(round((scene_max_y - p["max_y"]) / p["height_deg"]))
        p["col_idx"] = col_idx
        p["row_idx"] = row_idx
        
        group_col = col_idx // SUPER_GRID_SIZE
        group_row = row_idx // SUPER_GRID_SIZE
        
        grid_id = f"Matrix_{group_row:03d}_{group_col:03d}"

        if grid_id not in grouped_grids:
            grouped_grids[grid_id] = {
                "grid_index": grid_id,
                "identifier": identifier,
                "patches": [],
                "min_x": float('inf'), "max_x": float('-inf'),
                "min_y": float('inf'), "max_y": float('-inf'),
            }
        
        grouped_grids[grid_id]["patches"].append({
            "jpg_path": p["jpg_path"],
            "bounds": p["bounds"],
            "row_idx": row_idx,
            "col_idx": col_idx,
        })
        
        grouped_grids[grid_id]["min_x"] = min(grouped_grids[grid_id]["min_x"], p["min_x"])
        grouped_grids[grid_id]["max_x"] = max(grouped_grids[grid_id]["max_x"], p["max_x"])
        grouped_grids[grid_id]["min_y"] = min(grouped_grids[grid_id]["min_y"], p["min_y"])
        grouped_grids[grid_id]["max_y"] = max(grouped_grids[grid_id]["max_y"], p["max_y"])

    grid_cells_list = []
    for g_id, cell in grouped_grids.items():
        min_x, max_x = cell["min_x"], cell["max_x"]
        min_y, max_y = cell["min_y"], cell["max_y"]
        top_left_patch = min(cell["patches"], key=lambda patch: (patch["row_idx"], patch["col_idx"]))
        bottom_right_patch = max(cell["patches"], key=lambda patch: (patch["row_idx"], patch["col_idx"]))
        
        cell["mine_point_count"] = scene_mine_point_count
        cell["mining_category"] = target_mineral or "Unknown"
        cell["scene_uid"] = scene_uid
        cell["scene_name"] = scene_data['scene_name']
        cell["annotation_key"] = make_annotation_key(scene_uid, cell["grid_index"])
        cell["top_left_jpg"] = os.path.basename(top_left_patch["jpg_path"])
        cell["bottom_right_jpg"] = os.path.basename(bottom_right_patch["jpg_path"])
        cell["center"] = [(min_y + max_y) / 2.0, (min_x + max_x) / 2.0]
        cell["bounds"] = [[min_y, min_x], [max_y, max_x]]
        grid_cells_list.append(cell)

    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
    
    grid_cells_list.sort(key=lambda x: natural_sort_key(x['grid_index']))
    enrich_grids_with_nature_polygons(grid_cells_list, target_mineral, iso3_code)

    return grid_cells_list


class SceneProcessorWorker(QThread):
    finished_signal = pyqtSignal(str, list)

    def __init__(self, scene_key, scene_data, icmm_df, current_folder, data_manager):
        super().__init__()
        self.scene_key = normalize_spatial_path(scene_key)
        self.scene_data = scene_data
        self.icmm_df = icmm_df
        self.current_folder = normalize_spatial_path(current_folder)
        self.data_manager = data_manager

    def run(self):
        grids = _compile_scene_grids(self.scene_key, self.scene_data, self.icmm_df, self.current_folder, self.data_manager)
        self.finished_signal.emit(self.scene_key, grids)


class PrefetchWorker(QThread):
    prefetch_finished = pyqtSignal(str, list)
    
    def __init__(self, icmm_df, current_folder, data_manager):
        super().__init__()
        self.queue = []
        self.running = True
        self.icmm_df = icmm_df
        self.current_folder = normalize_spatial_path(current_folder)
        self.data_manager = data_manager
        self.scene_database_ref = None

    def set_database_ref(self, db_ref):
        self.scene_database_ref = db_ref

    def update_paths(self, scene_keys):
        if not self.scene_database_ref: return
        new_targets = [normalize_spatial_path(k) for k in scene_keys if self.scene_database_ref.get(normalize_spatial_path(k), {}).get('grid_cells') is None and normalize_spatial_path(k) not in self.queue]
        self.queue.extend(new_targets)

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            if self.queue and self.scene_database_ref:
                scene_key = self.queue.pop(0)
                scene_data = self.scene_database_ref.get(scene_key)
                if scene_data and scene_data['grid_cells'] is None:
                    try:
                        grids = _compile_scene_grids(scene_key, scene_data, self.icmm_df, self.current_folder, self.data_manager)
                        self.prefetch_finished.emit(scene_key, grids)
                    except Exception as e:
                        pass
            else:
                self.msleep(200)


class FastBackgroundGrid(MacroElement):
    """
    Optimizes background grid rendering by injecting raw data matrices into Leaflet JS script tags.
    """
    def __init__(self, grids: list, active_index: str):
        super().__init__()
        self.active_index = active_index
        simplified_grids = [
            {"grid_index": g["grid_index"], "bounds": g["bounds"], "center": g["center"]}
            for g in grids
        ]
        self.grids_json = json.dumps(simplified_grids)
        
        self._template = Template("""
            {% macro script(this, kwargs) %}
            var map_instance = {{ this._parent.get_name() }};
            var gridData = {{ this.grids_json }};
            var activeIdx = "{{ this.active_index }}";
            
            gridData.forEach(function(grid) {
                if (grid.grid_index === activeIdx) return;
                var b = grid.bounds;
                
                L.rectangle([[b[0][0], b[0][1]], [b[1][0], b[1][1]]], {
                    color: '#3388ff',
                    weight: 2,
                    fill: false,
                    opacity: 0.6
                }).addTo(map_instance);
                
                var labelTxt = grid.grid_index.substring(grid.grid_index.indexOf('_') + 1);
                var htmlStr = '<div style="font-size: 9pt; color: #1a5f7a; font-weight: bold; ' +
                              'background-color: rgba(255,255,255,0.75); padding: 2px 5px; ' +
                              'border: 1px solid #3388ff; border-radius: 4px; white-space: nowrap; ' +
                              'transform: translate(-50%, -50%); text-align: center;">' + labelTxt + '</div>';
                
                L.marker([grid.center[0], grid.center[1]], {
                    icon: L.divIcon({
                        html: htmlStr,
                        iconSize: [0, 0],
                        iconAnchor: [0, 0]
                    }),
                    interactive: false
                }).addTo(map_instance);
            });
            {% endmacro %}
        """)


class LeafletPaneSetup(MacroElement):
    def __init__(self, panes: list):
        super().__init__()
        self.panes_json = json.dumps(panes)
        self._template = Template("""
            {% macro script(this, kwargs) %}
            var map_instance = {{ this._parent.get_name() }};
            var panes = {{ this.panes_json }};
            panes.forEach(function(paneDef) {
                var pane = map_instance.getPane(paneDef.name) || map_instance.createPane(paneDef.name);
                pane.style.zIndex = paneDef.zIndex;
                if (paneDef.pointerEvents) {
                    pane.style.pointerEvents = paneDef.pointerEvents;
                }
            });
            {% endmacro %}
        """)


class DescriptorOverlay(MacroElement):
    def __init__(self, html_content: str):
        super().__init__()
        self.html_content = html_content
        self._template = Template("""
            {% macro html(this, kwargs) %}
            <div style="
                position: fixed;
                right: 18px;
                top: 72px;
                z-index: 9999;
                width: 360px;
                max-height: 72vh;
                overflow-y: auto;
                padding: 12px 14px;
                background: rgba(20, 20, 20, 0.72);
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.38);
                border-radius: 8px;
                box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
                font-family: Arial, sans-serif;
                font-size: 12px;
                line-height: 1.35;
                pointer-events: none;
            ">
                {{ this.html_content }}
            </div>
            {% endmacro %}
        """)


class MapHudOverlay(MacroElement):
    def __init__(self, html_content: str):
        super().__init__()
        self.html_content = html_content
        self._template = Template("""
            {% macro html(this, kwargs) %}
            <div id="minegrid-map-hud" style="
                position: fixed;
                left: 18px;
                top: 18px;
                z-index: 9999;
                min-width: 260px;
                max-width: 420px;
                padding: 12px 14px;
                background: rgba(12, 20, 33, 0.82);
                color: #F8FBFF;
                border: 1px solid rgba(255, 255, 255, 0.30);
                border-radius: 8px;
                box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
                font-family: Arial, sans-serif;
                font-size: 12px;
                line-height: 1.35;
                pointer-events: none;
            ">
                {{ this.html_content }}
            </div>
            {% endmacro %}
        """)


class FitBoundsOnce(MacroElement):
    def __init__(self, bounds: list, padding_px: int = 0):
        super().__init__()
        self.bounds_json = json.dumps(bounds)
        self.padding_px = int(padding_px)
        self._template = Template("""
            {% macro script(this, kwargs) %}
            var map_instance = {{ this._parent.get_name() }};
            var target_bounds = {{ this.bounds_json }};
            var padding_px = {{ this.padding_px }};
            map_instance.whenReady(function() {
                map_instance.invalidateSize();
                map_instance.fitBounds(target_bounds, {padding: [padding_px, padding_px]});
            });
            {% endmacro %}
        """)


class TabFocusFilter(QObject):
    """
    Custom event filter to trap Tab and Shift+Tab focus transitions
    strictly within the taxonomy checkbox collection.
    """
    def __init__(self, widgets, parent=None):
        super().__init__(parent)
        self.widgets = widgets

    def eventFilter(self, obj, event):
        # RESOLVED CRASH: Removed erroneous QKeyEvent constructor recreation wrapper
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Tab:
                parent = self.parent()
                if parent and hasattr(parent, "_consume_first_taxonomy_tab"):
                    if parent._consume_first_taxonomy_tab():
                        return True
                current_sb = None
                for sb in self.widgets:
                    if obj == sb:
                        current_sb = sb
                        break
                if current_sb:
                    idx = self.widgets.index(current_sb)
                    next_idx = (idx + 1) % len(self.widgets)
                    self.widgets[next_idx].setFocus()
                    return True
            elif event.key() == Qt.Key.Key_Backtab:
                current_sb = None
                for sb in self.widgets:
                    if obj == sb:
                        current_sb = sb
                        break
                if current_sb:
                    idx = self.widgets.index(current_sb)
                    next_idx = (idx - 1) % len(self.widgets)
                    self.widgets[next_idx].setFocus()
                    return True
        return super().eventFilter(obj, event)


class PagedHelpDialog(QDialog):
    def __init__(self, pages: list, parent=None, title: str = "Shortcuts / Features", size: tuple = (520, 520)):
        super().__init__(parent)
        self.pages = pages
        self.page_idx = 0
        self.setWindowTitle(title)
        self.resize(*size)

        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setStyleSheet(
            "QTextBrowser { background: #111827; color: #F8FAFC; border: 1px solid #374151; border-radius: 8px; }"
        )
        layout.addWidget(self.browser, stretch=1)

        nav_layout = QHBoxLayout()
        self.btn_prev_page = QPushButton("< Previous")
        self.lbl_page = QLabel()
        self.lbl_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn_next_page = QPushButton("Next >")
        self.btn_close = QPushButton("Close")
        for btn in [self.btn_prev_page, self.btn_next_page, self.btn_close]:
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setStyleSheet("font-weight: bold; padding: 6px 10px;")
        self.btn_prev_page.clicked.connect(self.prev_page)
        self.btn_next_page.clicked.connect(self.next_page)
        self.btn_close.clicked.connect(self.accept)
        nav_layout.addWidget(self.btn_prev_page)
        nav_layout.addWidget(self.lbl_page, stretch=1)
        nav_layout.addWidget(self.btn_next_page)
        nav_layout.addWidget(self.btn_close)
        layout.addLayout(nav_layout)
        self.render_page()

    def render_page(self):
        self.browser.setHtml(self.pages[self.page_idx])
        self.lbl_page.setText(f"{self.page_idx + 1} / {len(self.pages)}")
        self.btn_prev_page.setEnabled(self.page_idx > 0)
        self.btn_next_page.setEnabled(self.page_idx < len(self.pages) - 1)

    def prev_page(self):
        if self.page_idx > 0:
            self.page_idx -= 1
            self.render_page()

    def next_page(self):
        if self.page_idx < len(self.pages) - 1:
            self.page_idx += 1
            self.render_page()


class KeybindingDialog(QDialog):
    def __init__(self, bindings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keybindings")
        self.resize(520, 360)
        self.edits = {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        for action, label in KEYBINDING_LABELS.items():
            edit = QKeySequenceEdit(QKeySequence(bindings.get(action, DEFAULT_KEYBINDINGS[action])))
            self.edits[action] = edit
            form.addRow(label, edit)
        layout.addLayout(form)

        self.lbl_warning = QLabel("")
        self.lbl_warning.setStyleSheet("color: #B45309; font-weight: 700;")
        layout.addWidget(self.lbl_warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore_defaults)
        layout.addWidget(buttons)

    def restore_defaults(self):
        for action, sequence in DEFAULT_KEYBINDINGS.items():
            self.edits[action].setKeySequence(QKeySequence(sequence))

    def bindings(self) -> dict:
        return {
            action: edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            for action, edit in self.edits.items()
        }

    def validate_and_accept(self):
        bindings = self.bindings()
        values = [value for value in bindings.values() if value]
        if len(values) != len(bindings):
            self.lbl_warning.setText("Every action needs a key sequence.")
            return
        if len(values) != len(set(values)):
            self.lbl_warning.setText("Duplicate key sequences are not allowed.")
            return
        self.accept()


class MiningTaxonomyUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"MineGrid Labeler - {SUPER_GRID_SIZE}x{SUPER_GRID_SIZE} Scene Matrix")
        self.resize(1700, 950)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.current_folder = ""
        self.data_manager = None
        
        self.icmm_df = pd.DataFrame()
        self.scene_database = {}
        self.active_scene_key = None
        self.active_grid_idx = -1
        self._is_populating_ui = False
        self._taxonomy_cursor_reset_pending = False
        self._pending_grid_selection = None
        self._workspace_load_id = 0
        self.descriptor_tooltip_language = "en"
        self.descriptor_overlay_enabled = False
        self.zoom_offset_steps = 0
        self._last_applied_zoom_offset_steps = 0
        self._pending_zoom_offset_apply = False
        self.context_bins_enabled = True
        self.mine_polygons_available = bool(get_nature_mine_gpkg_path())
        self.mine_polygons_enabled = self.mine_polygons_available
        self.map_hud_text = "Target: None"
        self.count_undo_stack = []
        self.count_redo_stack = []
        self.active_component_idx = 0
        self._last_quality_flag = 0
        self._dirty_gpkg_scene_keys = set()
        self.shortcut_bindings = self._load_keybindings()
        self.shortcut_objects = {}
        
        self.scanner_thread = None
        self.active_worker = None
        
        self.prefetch_thread = PrefetchWorker(pd.DataFrame(), "", None)
        self.prefetch_thread.prefetch_finished.connect(self._on_prefetch_finished)
        self.prefetch_thread.start()

        self._init_ui()
        self.open_working_directory(get_script_dir())

    def _init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)

        left_panel = QVBoxLayout()
        self.btn_select_dir = QPushButton("Open Working Directory")
        self.btn_select_dir.clicked.connect(self.select_directory)
        self.btn_select_dir.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        left_panel.addWidget(self.btn_select_dir)

        self.lbl_status = QLabel("System Status: Awaiting Directory Injection.")
        left_panel.addWidget(self.lbl_status)

        self.lbl_files = QLabel("Scene Roots (Consolidated TIF Clusters):")
        left_panel.addWidget(self.lbl_files)
        filter_layout = QHBoxLayout()
        self.country_filter = QComboBox()
        self.mineral_filter = QComboBox()
        self.country_filter.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mineral_filter.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.country_filter.addItem("All Nations", "")
        self.mineral_filter.addItem("All Minerals", "")
        self.country_filter.currentIndexChanged.connect(self._refresh_scene_list)
        self.mineral_filter.currentIndexChanged.connect(self._refresh_scene_list)
        filter_layout.addWidget(self.country_filter)
        filter_layout.addWidget(self.mineral_filter)
        left_panel.addLayout(filter_layout)

        self.file_list_widget = QListWidget()
        self.file_list_widget.itemClicked.connect(self.on_scene_changed)
        self.file_list_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        left_panel.addWidget(self.file_list_widget)
        
        self.lbl_grids = QLabel()
        self._set_matrix_bin_count(0)
        left_panel.addWidget(self.lbl_grids)
        self.grid_list_widget = QListWidget()
        self.grid_list_widget.itemClicked.connect(self.on_grid_changed)
        self.grid_list_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        left_panel.addWidget(self.grid_list_widget)
        
        tax_group = QGroupBox("Mine Component Categorization (Live Auto-Save Matrix)")
        tax_group.setStyleSheet("font-weight: bold;")
        tax_group_layout = QVBoxLayout()
        self.lbl_active_component = QLabel("Selected Component: None")
        self.lbl_active_component.setStyleSheet(
            "font-weight: 700; padding: 6px 8px; border: 1px solid #4C6FFF; background: #EAF1FF; color: #102A43;"
        )
        tax_group_layout.addWidget(self.lbl_active_component)
        tax_grid = QGridLayout()
        tax_group_layout.addLayout(tax_grid)
        tax_group.setLayout(tax_group_layout)

        self.tax_spinboxes = {}
        self.flag_label_widgets = {}
        self.component_rows = {}
        self.flag_rows = {}
        self.component_label_widgets = {}
        self.component_help_buttons = {}
        self.component_labels = {}
        self.core_keys = DESCRIPTOR_GUIDE_ORDER.copy()

        row = 0
        for key in self.core_keys:
            info = TAXONOMY_DEFINITIONS[key]
            self.component_labels[key] = info["label"]
            row_widget = QWidget()
            row_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(4, 4, 4, 4)
            row_layout.setSpacing(10)
            lbl = QLabel(info["label"])
            lbl.setStyleSheet("font-weight: normal;")
            lbl.setToolTip(info["desc_en"])
            self.component_label_widgets[key] = lbl
            row_layout.addWidget(lbl)
            help_btn = QPushButton("?")
            help_btn.setFixedSize(24, 24)
            help_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            help_btn.setToolTip(f"Show descriptor and examples for {info['label']}")
            help_btn.setStyleSheet("font-weight: 800; padding: 0; border-radius: 12px;")
            help_btn.clicked.connect(lambda _checked=False, component_key=key: self.show_component_reference(component_key))
            self.component_help_buttons[key] = help_btn
            row_layout.addWidget(help_btn)
            row_layout.addStretch(1)
            
            cb = QCheckBox()
            cb.setChecked(False)
            cb.setText("OFF")
            cb.setMinimumWidth(76)
            cb.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            cb.stateChanged.connect(lambda state, component_key=key: self._on_component_checkbox_changed(component_key, state))
            self.tax_spinboxes[key] = cb
            row_layout.addWidget(cb)
            self.component_rows[key] = row_widget
            tax_grid.addWidget(row_widget, row, 0, 1, 2)
            row += 1

        flag_title = QLabel("Quality Flags")
        flag_title.setStyleSheet("font-weight: 700; margin-top: 8px;")
        tax_grid.addWidget(flag_title, row, 0, 1, 2)
        row += 1
        flag_row_widget = QWidget()
        flag_row_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        flag_row_layout = QHBoxLayout(flag_row_widget)
        flag_row_layout.setContentsMargins(4, 4, 4, 4)
        flag_row_layout.setSpacing(12)
        lbl = QLabel("Error / Unidentifiable")
        lbl.setToolTip("Optional bin-level quality flag.")
        self.flag_label_widgets[QUALITY_FLAG_KEY] = lbl
        flag_row_layout.addWidget(lbl)
        self.quality_flag_checkbox = QCheckBox()
        self.quality_flag_checkbox.setChecked(False)
        self.quality_flag_checkbox.setText("OFF")
        self.quality_flag_checkbox.setMinimumWidth(76)
        self.quality_flag_checkbox.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.quality_flag_checkbox.setToolTip(QUALITY_FLAG_DESC)
        self.quality_flag_checkbox.stateChanged.connect(self._on_quality_flag_changed)
        flag_row_layout.addWidget(self.quality_flag_checkbox)
        self.flag_rows[QUALITY_FLAG_KEY] = flag_row_widget
        flag_row_layout.addStretch(1)
        tax_grid.addWidget(flag_row_widget, row, 0, 1, 2)
        row += 1

        left_panel.addWidget(tax_group)

        help_layout = QHBoxLayout()
        self.btn_shortcut_help = QPushButton("Shortcuts / Features")
        self.btn_keybinding_help = QPushButton("Keybindings")
        self.btn_descriptor_help = QPushButton("Show Descriptor Overlay")
        self.descriptor_language_combo = QComboBox()
        self.descriptor_language_combo.addItem("EN", "en")
        self.descriptor_language_combo.addItem("KR", "ko")
        self.descriptor_language_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.descriptor_language_combo.currentIndexChanged.connect(self._on_descriptor_language_changed)
        for btn in [self.btn_shortcut_help, self.btn_keybinding_help, self.btn_descriptor_help]:
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_shortcut_help.clicked.connect(self.show_shortcut_help)
        self.btn_keybinding_help.clicked.connect(self.show_keybinding_dialog)
        self.btn_descriptor_help.clicked.connect(self.toggle_descriptor_overlay)
        help_layout.addWidget(self.btn_shortcut_help)
        help_layout.addWidget(self.btn_keybinding_help)
        help_layout.addWidget(self.btn_descriptor_help)
        help_layout.addWidget(self.descriptor_language_combo)
        left_panel.addLayout(help_layout)

        purge_layout = QHBoxLayout()
        self.btn_purge_scene = QPushButton("Purge Scene Labels")
        self.btn_purge_all = QPushButton("Full Purge")
        for btn in [self.btn_purge_scene, self.btn_purge_all]:
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setStyleSheet("font-weight: bold; padding: 6px; color: #B71C1C;")
            purge_layout.addWidget(btn)
        self.btn_purge_scene.clicked.connect(self.purge_active_scene_database)
        self.btn_purge_all.clicked.connect(self.purge_all_databases)
        left_panel.addLayout(purge_layout)

        main_layout.addLayout(left_panel, stretch=1)

        self.tab_filter = TabFocusFilter(list(self.tax_spinboxes.values()), self)
        for sb in self.tax_spinboxes.values():
            sb.installEventFilter(self.tab_filter)
        QApplication.instance().installEventFilter(self)
        self._setup_shortcuts()

        right_panel = QVBoxLayout()
        map_nav_layout = QHBoxLayout()
        
        self.btn_prev_grid = QPushButton("◀ Prev Grid")
        self.btn_next_grid = QPushButton("Next Grid ▶")
        self.zoom_offset_combo = QComboBox()
        self.zoom_offset_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for offset in [-1, 0, 1, 2]:
            self.zoom_offset_combo.addItem(f"Zoom {offset:+d}", offset)
        self.zoom_offset_combo.setCurrentIndex(self.zoom_offset_combo.findData(self.zoom_offset_steps))
        self.zoom_offset_combo.currentIndexChanged.connect(self._on_zoom_offset_changed)
        self.context_bins_checkbox = QCheckBox("Surrounding Context")
        self.context_bins_checkbox.setChecked(self.context_bins_enabled)
        self.context_bins_checkbox.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.context_bins_checkbox.stateChanged.connect(self._on_context_bins_changed)
        mine_polygon_label = "Mine Polygon Overlay" if self.mine_polygons_available else "Mine Polygon Overlay (GPKG missing)"
        self.mine_polygons_checkbox = QCheckBox(mine_polygon_label)
        self.mine_polygons_checkbox.setChecked(self.mine_polygons_enabled)
        self.mine_polygons_checkbox.setEnabled(self.mine_polygons_available)
        self.mine_polygons_checkbox.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mine_polygons_checkbox.setToolTip(
            "Uses data/nature_mine_poly_nearest.gpkg or data/shp/nature_mine_poly_nearest.gpkg."
            if self.mine_polygons_available else
            "Missing nature_mine_poly_nearest.gpkg. Place it in data/ or data/shp/ to enable this overlay."
        )
        self.mine_polygons_checkbox.stateChanged.connect(self._on_mine_polygons_changed)
        self.btn_prev_grid.setStyleSheet("font-weight: bold; padding: 6px; font-size: 10pt;")
        self.btn_next_grid.setStyleSheet("font-weight: bold; padding: 6px; font-size: 10pt;")
        self.btn_prev_grid.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_next_grid.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        self.btn_prev_grid.clicked.connect(self._navigate_prev_grid)
        self.btn_next_grid.clicked.connect(self._navigate_next_grid)
        
        map_nav_layout.addWidget(self.btn_prev_grid)
        map_nav_layout.addWidget(self.btn_next_grid)
        map_nav_layout.addStretch(1)
        map_nav_layout.addWidget(self.context_bins_checkbox)
        map_nav_layout.addWidget(self.mine_polygons_checkbox)
        map_nav_layout.addWidget(QLabel("Zoom Offset"))
        map_nav_layout.addWidget(self.zoom_offset_combo)
        right_panel.addLayout(map_nav_layout)

        self.web_view = QWebEngineView()
        self.web_view.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        self.web_view.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        self.web_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.web_view.loadFinished.connect(self._on_map_load_finished)
        right_panel.addWidget(self.web_view, stretch=1)
        main_layout.addLayout(right_panel, stretch=3)
        
        self._load_empty_map()
        self._refresh_component_focus_styles()

    def _taxonomy_widget_has_focus(self) -> bool:
        focused = QApplication.focusWidget()
        return any(focused == widget for widget in self.tax_spinboxes.values())

    def _checkbox_state(self, key: str) -> int:
        widget = self.tax_spinboxes.get(key)
        return 1 if widget and widget.isChecked() else 0

    def _quality_flag_value(self) -> int:
        checkbox = getattr(self, "quality_flag_checkbox", None)
        return 1 if checkbox is not None and checkbox.isChecked() else 0

    def _current_history_context(self) -> dict:
        if not self.active_scene_key or self.active_grid_idx == -1:
            return {}
        scene_data = self.scene_database[self.active_scene_key]
        grid_data = scene_data["grid_cells"][self.active_grid_idx]
        return {
            "scene_key": self.active_scene_key,
            "grid_idx": self.active_grid_idx,
            "component_idx": self.active_component_idx,
            "scene_tif_path": get_stable_scene_tif_path(scene_data["tif_path"]),
            "grid_index": grid_data["grid_index"],
            "annotation_key": grid_data.get("annotation_key"),
        }

    def _set_component_state(self, key: str, value: int, record_history: bool = True, source: str = "keyboard"):
        widget = self.tax_spinboxes.get(key)
        if widget is None:
            return
        new_value = 1 if int(value) else 0
        old_value = 1 if widget.isChecked() else 0
        if old_value == new_value:
            return

        widget.blockSignals(True)
        widget.setChecked(bool(new_value))
        widget.blockSignals(False)

        if record_history:
            self.count_undo_stack.append({
                "type": "component",
                "key": key,
                "old": old_value,
                "new": new_value,
                **self._current_history_context(),
            })
            self.count_redo_stack.clear()
        label = self._component_label(key)
        self.lbl_status.setText(f"{source.title()}: {label} = {new_value}")
        self._refresh_component_focus_styles()
        self._auto_save_current_grid()

    def _on_component_checkbox_changed(self, key: str, state: int):
        if self._is_populating_ui or self.active_grid_idx == -1:
            return
        new_value = 1 if state else 0
        old_value = 0 if new_value == 1 else 1
        self.count_undo_stack.append({
            "type": "component",
            "key": key,
            "old": old_value,
            "new": new_value,
            **self._current_history_context(),
        })
        self.count_redo_stack.clear()
        if key in self.core_keys:
            self._set_active_component_index(self.core_keys.index(key), focus=False)
        self.lbl_status.setText(f"Checkbox: {self._component_label(key)} = {new_value}")
        self._refresh_component_focus_styles()
        self._auto_save_current_grid()

    def _on_quality_flag_changed(self, state: int):
        if self._is_populating_ui or self.active_grid_idx == -1:
            return
        new_value = 1 if state else 0
        old_value = self._last_quality_flag
        if new_value == old_value:
            return
        self.count_undo_stack.append({
            "type": "quality_flag",
            "old": old_value,
            "new": new_value,
            **self._current_history_context(),
        })
        self.count_redo_stack.clear()
        self._last_quality_flag = new_value
        self.lbl_status.setText(f"Quality Flag: {new_value}")
        self._refresh_component_focus_styles()
        self._auto_save_current_grid()

    def _auto_save_current_grid(self, mark_started: bool = True, mark_completed: bool = False):
        if self._is_populating_ui or self.active_grid_idx == -1 or not self.active_scene_key:
            return
        scene_data = self.scene_database[self.active_scene_key]
        grid_data = scene_data["grid_cells"][self.active_grid_idx]
        tif_rel_path = get_stable_scene_tif_path(scene_data["tif_path"])
        payload = {
            "annotation_key": grid_data.get("annotation_key"),
            "scene_uid": grid_data.get("scene_uid"),
            "scene_name": scene_data.get("scene_name"),
            "scene_tif_path": tif_rel_path,
            "identifier": extract_output_identifier(scene_data.get("scene_name", grid_data.get("identifier", ""))),
            "grid_index": grid_data["grid_index"],
            "mining_category": grid_data.get("mining_category", "Unknown"),
            "mine_point_count": grid_data["mine_point_count"],
            "top_left_jpg": grid_data.get("top_left_jpg", ""),
            "bottom_right_jpg": grid_data.get("bottom_right_jpg", "")
        }
        for k in self.core_keys:
            payload[k] = self._checkbox_state(k)
        payload[QUALITY_FLAG_KEY] = self._quality_flag_value()
        self.data_manager.save_or_update_grid(payload, mark_started=mark_started, mark_completed=mark_completed)
        self._mark_scene_gpkg_dirty(self.active_scene_key)
        self._update_grid_completion_state()

    def _label_for_binary_key(self, key: str) -> str:
        if key in self.component_labels:
            return self._component_label(key)
        return key

    def _binary_widget_for_key(self, key: str):
        return self.tax_spinboxes.get(key)

    def _apply_binary_history_value(self, action: dict, value_name: str):
        self._select_history_grid(action)
        key = action["key"]
        widget = self._binary_widget_for_key(key)
        if widget is None:
            return
        widget.blockSignals(True)
        widget.setChecked(bool(action[value_name]))
        widget.blockSignals(False)
        if key in self.core_keys:
            self._set_active_component_index(self.core_keys.index(key), focus=True)
        self._refresh_component_focus_styles()
        self._auto_save_current_grid()

    def _apply_quality_flag_history_value(self, action: dict, value_name: str):
        self._select_history_grid(action)
        value = 1 if int(action.get(value_name, 0)) else 0
        checkbox = getattr(self, "quality_flag_checkbox", None)
        if checkbox is None:
            return
        checkbox.blockSignals(True)
        checkbox.setChecked(bool(value))
        checkbox.blockSignals(False)
        self._last_quality_flag = value
        self._refresh_component_focus_styles()
        self._auto_save_current_grid()

    def _select_history_grid(self, action: dict):
        scene_key = normalize_spatial_path(action.get("scene_key", ""))
        grid_idx = int(action.get("grid_idx", -1))
        if not scene_key or grid_idx < 0:
            return
        if self.active_scene_key != scene_key:
            for row in range(self.file_list_widget.count()):
                item = self.file_list_widget.item(row)
                if normalize_spatial_path(item.data(Qt.ItemDataRole.UserRole)) == scene_key:
                    self.file_list_widget.setCurrentRow(row)
                    self._pending_grid_selection = (scene_key, grid_idx)
                    self.on_scene_changed(item)
                    break
        if self.active_scene_key == scene_key and self.grid_list_widget.count() > grid_idx:
            self.grid_list_widget.setCurrentRow(grid_idx)
            self.on_grid_changed(self.grid_list_widget.currentItem(), initial_component_idx=int(action.get("component_idx", 0)))

    def _set_history_grid_eval_end(self, action: dict, eval_end: str):
        self.data_manager.set_grid_eval_end(
            action["scene_tif_path"],
            action["grid_index"],
            action.get("annotation_key"),
            eval_end,
        )
        self._select_history_grid(action)
        self._auto_save_current_grid(mark_started=False, mark_completed=False)

    def undo_last_map_count(self):
        if not self.count_undo_stack:
            return
        action = self.count_undo_stack.pop()
        action_type = action.get("type", "component")
        if action_type == "component":
            self._apply_binary_history_value(action, "old")
            self.count_redo_stack.append(action)
            self.lbl_status.setText(f"Undo: {self._label_for_binary_key(action['key'])} = {action['old']}")
            return
        if action_type == "quality_flag":
            self._apply_quality_flag_history_value(action, "old")
            self.count_redo_stack.append(action)
            self.lbl_status.setText(f"Undo: quality flag = {int(action.get('old', 0))}")
            return
        if action_type == "bin_complete":
            self._set_history_grid_eval_end(action, action.get("old_eval_end", ""))
            self.count_redo_stack.append(action)
            self.lbl_status.setText(f"Undo: completion cleared for {action.get('grid_index', '')}")

    def redo_last_map_count(self):
        if not self.count_redo_stack:
            return
        action = self.count_redo_stack.pop()
        action_type = action.get("type", "component")
        if action_type == "component":
            self._apply_binary_history_value(action, "new")
            self.count_undo_stack.append(action)
            self.lbl_status.setText(f"Redo: {self._label_for_binary_key(action['key'])} = {action['new']}")
            return
        if action_type == "quality_flag":
            self._apply_quality_flag_history_value(action, "new")
            self.count_undo_stack.append(action)
            self.lbl_status.setText(f"Redo: quality flag = {int(action.get('new', 0))}")
            return
        if action_type == "bin_complete":
            self._set_history_grid_eval_end(action, action.get("new_eval_end", ""))
            self.count_undo_stack.append(action)
            self.lbl_status.setText(f"Redo: completion restored for {action.get('grid_index', '')}")

    def _component_label(self, key: str) -> str:
        return self.component_labels.get(key, key)

    def _current_component_key(self) -> str:
        if not self.core_keys:
            return ""
        idx = max(0, min(self.active_component_idx, len(self.core_keys) - 1))
        return self.core_keys[idx]

    def _set_active_component_index(self, idx: int, focus: bool = True):
        if not self.core_keys:
            return
        idx = max(0, min(idx, len(self.core_keys) - 1))
        self.active_component_idx = idx
        self._refresh_component_focus_styles()
        if focus:
            widget = self.tax_spinboxes.get(self._current_component_key())
            if widget is not None:
                widget.setFocus()

    def toggle_current_component(self):
        if not self.active_scene_key or self.active_grid_idx == -1:
            return
        key = self._current_component_key()
        if not key:
            return
        self._set_component_state(key, 0 if self._checkbox_state(key) else 1, record_history=True, source="toggle")

    def mark_current_component_no(self):
        if not self.active_scene_key or self.active_grid_idx == -1:
            return
        if self.active_component_idx >= len(self.core_keys) - 1:
            self._complete_current_grid()
        self._move_component_forward()

    def _navigate_next_bin(self):
        if not self.active_scene_key or self.active_grid_idx == -1:
            return
        self._complete_current_grid()
        grid_len = len(self.scene_database[self.active_scene_key]["grid_cells"])
        self._navigate_next_matrix_position(grid_len)

    def _navigate_prev_bin(self):
        if not self.active_scene_key or self.active_grid_idx == -1:
            return
        self._navigate_prev_matrix_position(initial_component_idx=self.active_component_idx)

    def _move_component_forward(self):
        if self.active_component_idx < len(self.core_keys) - 1:
            self._set_active_component_index(self.active_component_idx + 1)
        else:
            self._navigate_next_matrix_position(len(self.scene_database[self.active_scene_key]["grid_cells"]))

    def _complete_current_grid(self):
        if not self.active_scene_key or self.active_grid_idx == -1:
            return
        scene_data = self.scene_database[self.active_scene_key]
        grid_data = scene_data["grid_cells"][self.active_grid_idx]
        tif_rel_path = get_stable_scene_tif_path(scene_data["tif_path"])
        current_rec = self.data_manager.get_grid_record(tif_rel_path, grid_data["grid_index"], grid_data.get("annotation_key"))
        old_eval_end = str(current_rec.get("eval_end", "")) if current_rec else ""
        action = {
            "type": "bin_complete",
            "scene_key": self.active_scene_key,
            "grid_idx": self.active_grid_idx,
            "component_idx": self.active_component_idx,
            "scene_tif_path": tif_rel_path,
            "grid_index": grid_data["grid_index"],
            "annotation_key": grid_data.get("annotation_key"),
            "old_eval_end": old_eval_end,
            "new_eval_end": "",
        }
        self._auto_save_current_grid(mark_completed=True)
        new_rec = self.data_manager.get_grid_record(tif_rel_path, grid_data["grid_index"], grid_data.get("annotation_key"))
        action["new_eval_end"] = str(new_rec.get("eval_end", "")) if new_rec else ""
        self.count_undo_stack.append(action)
        self.count_redo_stack.clear()

    def previous_component_or_bin(self):
        if not self.active_scene_key or self.active_grid_idx == -1:
            return
        if self.active_component_idx > 0:
            self._set_active_component_index(self.active_component_idx - 1)
        else:
            self._navigate_prev_matrix_position()

    def next_component_or_bin(self):
        if not self.active_scene_key or self.active_grid_idx == -1:
            return
        self._move_component_forward()

    def _update_grid_completion_state(self):
        if self.active_scene_key and self.active_grid_idx >= 0:
            item = self.grid_list_widget.item(self.active_grid_idx)
            scene_data = self.scene_database[self.active_scene_key]
            grid_data = scene_data["grid_cells"][self.active_grid_idx]
            evaluated = self.data_manager.is_grid_evaluated(
                get_stable_scene_tif_path(scene_data["tif_path"]),
                grid_data["grid_index"],
                grid_data.get("annotation_key"),
            )
            if item:
                if evaluated:
                    item.setBackground(QColor("#E8F5E9"))
                    item.setForeground(QColor("#2E7D32"))
                else:
                    item.setBackground(QColor(Qt.GlobalColor.transparent))
                    item.setForeground(self.palette().color(QPalette.ColorRole.Text))
            scene_item = self.file_list_widget.currentItem()
            if scene_item:
                self._apply_scene_item_completion_style(scene_item, scene_data)

    def _refresh_component_focus_styles(self):
        has_active_grid = bool(self.active_scene_key and self.active_grid_idx >= 0)
        current_key = self._current_component_key() if has_active_grid else ""
        current_label = self._component_label(current_key) if current_key else "None"
        normal_text = self.palette().color(QPalette.ColorRole.WindowText).name()
        muted_border = self.palette().color(QPalette.ColorRole.Mid).name()
        if hasattr(self, "lbl_active_component"):
            self.lbl_active_component.setText(f"Selected Component: {current_label}")
        self._refresh_map_hud()
        for key, row_widget in self.component_rows.items():
            is_active = has_active_grid and key == current_key
            row_widget.setStyleSheet(
                "QWidget { background: #EAF1FF; border: 1px solid #4C6FFF; border-radius: 6px; }"
                if is_active else
                "QWidget { background: transparent; border: 1px solid transparent; }"
            )
            label_widget = self.component_label_widgets.get(key)
            if label_widget is not None:
                label_widget.setStyleSheet(
                    "font-weight: 700; color: #102A43;" if is_active else f"font-weight: normal; color: {normal_text};"
                )
            checkbox = self.tax_spinboxes.get(key)
            if checkbox is not None:
                checkbox.setText("ON" if checkbox.isChecked() else "OFF")
                checkbox.setStyleSheet(
                    "QCheckBox { color: #102A43; font-weight: 800; spacing: 7px; }"
                    "QCheckBox::indicator { width: 42px; height: 22px; border-radius: 11px; border: 1px solid #6B7A90; background: #2E3440; }"
                    "QCheckBox::indicator:checked { background: #2E7D32; border: 1px solid #86EFAC; }"
                    if is_active else
                    f"QCheckBox {{ color: {normal_text}; font-weight: 700; spacing: 7px; }}"
                    f"QCheckBox::indicator {{ width: 42px; height: 22px; border-radius: 11px; border: 1px solid {muted_border}; background: #242A33; }}"
                    "QCheckBox::indicator:checked { background: #2E7D32; border: 1px solid #86EFAC; }"
                )
        for key, row_widget in self.flag_rows.items():
            row_widget.setStyleSheet("QWidget { background: transparent; border: 1px solid transparent; }")
            label_widget = self.flag_label_widgets.get(key)
            if label_widget is not None:
                label_widget.setStyleSheet(f"font-weight: normal; color: {normal_text};")
        checkbox = getattr(self, "quality_flag_checkbox", None)
        if checkbox is not None:
            checkbox.setText("ON" if checkbox.isChecked() else "OFF")
            checkbox.setStyleSheet(
                f"QCheckBox {{ color: {normal_text}; font-weight: 700; spacing: 7px; }}"
                f"QCheckBox::indicator {{ width: 42px; height: 22px; border-radius: 11px; border: 1px solid {muted_border}; background: #242A33; }}"
                "QCheckBox::indicator:checked { background: #B45309; border: 1px solid #FCD34D; }"
            )

    def _refresh_map_hud(self):
        if not hasattr(self, "web_view"):
            return
        self._update_map_hud_dom()

    def _map_hud_html(self) -> str:
        if not self.active_scene_key or self.active_grid_idx == -1:
            return (
                "<div style='font-size: 10px; letter-spacing: 0; color: #A9B8CC; font-weight: 700;'>TARGET</div>"
                "<div style='font-size: 18px; font-weight: 800; margin-top: 2px;'>None</div>"
            )
        key = self._current_component_key()
        label = self._component_label(key) if key else "None"
        value = self._checkbox_state(key) if key else 0
        badge_bg = "#16A34A" if value else "#475569"
        badge_text = "ON" if value else "OFF"
        return (
            "<div style='font-size: 10px; letter-spacing: 0; color: #A9B8CC; font-weight: 700;'>TARGET COMPONENT</div>"
            f"<div style='font-size: 20px; font-weight: 800; margin-top: 2px; color: #FFFFFF;'>{label}</div>"
            "<div style='display: flex; align-items: center; gap: 8px; margin-top: 9px;'>"
            f"<span style='background: {badge_bg}; color: #FFFFFF; border-radius: 999px; padding: 3px 9px; font-weight: 800; font-size: 11px;'>{badge_text}</span>"
            "<span style='color: #D9E2EC; font-size: 11px;'>a=toggle &middot; j=next &middot; k=prev &middot; Enter=complete+next</span>"
            "</div>"
        )

    def _update_map_hud_dom(self):
        html = json.dumps(self._map_hud_html())
        script = (
            "(function(){"
            "var hud=document.getElementById('minegrid-map-hud');"
            f"if(hud){{hud.innerHTML={html};}}"
            "})();"
        )
        try:
            self.web_view.page().runJavaScript(script)
        except Exception:
            pass

    def _keybinding_path(self) -> str:
        return normalize_spatial_path(os.path.join(get_labeling_output_dir(), "keybindings.json"))

    def _load_keybindings(self) -> dict:
        bindings = DEFAULT_KEYBINDINGS.copy()
        path = self._keybinding_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for action in DEFAULT_KEYBINDINGS:
                    if loaded.get(action):
                        bindings[action] = str(loaded[action])
            except Exception:
                pass
        return bindings

    def _save_keybindings(self):
        os.makedirs(get_labeling_output_dir(), exist_ok=True)
        with open(self._keybinding_path(), "w", encoding="utf-8") as f:
            json.dump(self.shortcut_bindings, f, indent=2)

    def _setup_shortcuts(self):
        for shortcut in self.shortcut_objects.values():
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self.shortcut_objects = {}
        handlers = {
            "toggle_component": self.toggle_current_component,
            "next_component": self.mark_current_component_no,
            "previous_component": self.previous_component_or_bin,
            "complete_next_bin": self._navigate_next_bin,
            "previous_bin": self._navigate_prev_bin,
            "undo": self.undo_last_map_count,
            "redo": self.redo_last_map_count,
        }
        for action, handler in handlers.items():
            shortcut = QShortcut(QKeySequence(self.shortcut_bindings[action]), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(handler)
            self.shortcut_objects[action] = shortcut

    def _set_shortcuts_enabled(self, enabled: bool):
        for shortcut in self.shortcut_objects.values():
            shortcut.setEnabled(enabled)

    def show_keybinding_dialog(self):
        self._set_shortcuts_enabled(False)
        dialog = KeybindingDialog(self.shortcut_bindings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.shortcut_bindings = dialog.bindings()
            self._save_keybindings()
            self._setup_shortcuts()
            self.lbl_status.setText("Keybindings updated.")
        else:
            self._set_shortcuts_enabled(True)

    def _component_descriptor_text(self, key: str, language: str = None) -> str:
        info = TAXONOMY_DEFINITIONS.get(key, {})
        lang = language or self.descriptor_tooltip_language
        return info.get("desc_ko" if lang == "ko" else "desc_en", "")

    def _component_reference_style(self) -> str:
        return """
            <style>
                body { font-family: Arial, sans-serif; background: #111827; color: #F8FAFC; margin: 0; padding: 18px; }
                h1 { font-size: 22px; margin: 0 0 10px 0; color: #FFFFFF; }
                h2 { font-size: 13px; margin: 18px 0 10px 0; color: #93C5FD; text-transform: uppercase; letter-spacing: 0; }
                .descriptor { color: #DDE7F3; font-size: 14px; line-height: 1.45; padding: 12px 14px; background: #1F2937; border: 1px solid #374151; border-radius: 8px; }
                .example { background: #0F172A; border: 1px solid #334155; border-radius: 8px; overflow: hidden; margin: 0 0 14px 0; }
                .example img { display: block; width: 100%; height: auto; }
                .caption { padding: 8px 10px; color: #CBD5E1; font-size: 12px; font-weight: 700; }
                .missing { color: #FDE68A; padding: 12px 14px; background: #292524; border: 1px solid #57534E; border-radius: 8px; }
            </style>
        """

    def _component_example_images_html(self, key: str) -> str:
        info = TAXONOMY_DEFINITIONS.get(key, {})
        fig_dir = normalize_spatial_path(os.path.join(get_script_dir(), "data", "figs", info.get("fig_dir", "")))
        if not os.path.isdir(fig_dir):
            return "<div class='missing'>No example image folder found.</div>"

        image_paths = [
            normalize_spatial_path(os.path.join(fig_dir, name))
            for name in sorted(os.listdir(fig_dir))
            if name.lower().endswith((".png", ".jpg", ".jpeg"))
        ]
        if not image_paths:
            return "<div class='missing'>No example images found.</div>"

        chunks = []
        for path in image_paths:
            try:
                with open(path, "rb") as image_file:
                    encoded = base64.b64encode(image_file.read()).decode("ascii")
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                mime = "jpeg" if ext in ("jpg", "jpeg") else "png"
                caption = html_lib.escape(os.path.splitext(os.path.basename(path))[0].replace("_", " "))
                chunks.append(
                    "<div class='example'>"
                    f"<img src='data:image/{mime};base64,{encoded}' />"
                    f"<div class='caption'>{caption}</div>"
                    "</div>"
                )
            except Exception as exc:
                caption = html_lib.escape(os.path.basename(path))
                chunks.append(f"<div class='missing'>{caption}: {html_lib.escape(str(exc))}</div>")
        return "".join(chunks)

    def _component_reference_html(self, key: str) -> str:
        info = TAXONOMY_DEFINITIONS.get(key, {})
        label = html_lib.escape(info.get("label", key))
        desc = html_lib.escape(self._component_descriptor_text(key))
        return (
            self._component_reference_style() +
            f"<h1>{label}</h1>"
            f"<div class='descriptor'>{desc}</div>"
            "<h2>Reference examples</h2>"
            f"{self._component_example_images_html(key)}"
        )

    def show_component_reference(self, key: str):
        label = self._component_label(key)
        dialog = PagedHelpDialog(
            [self._component_reference_html(key)],
            self,
            title=f"{label} Reference",
            size=(900, 720),
        )
        dialog.exec()

    def _map_zoom_delta_script(self, delta: int, delay_ms: int = 0) -> str:
        return (
            "(function(){"
            f"var apply=function(){{"
            "for(var key in window){"
            "if(key.indexOf('map_')===0 && window[key] && typeof window[key].getZoom==='function'){"
            f"window[key].setZoom(window[key].getZoom()+({int(delta)}));"
            "break;"
            "}"
            "}"
            "};"
            f"setTimeout(apply,{int(delay_ms)});"
            "})();"
        )

    def _on_map_load_finished(self, ok: bool):
        if not ok or not self._pending_zoom_offset_apply:
            return
        self._pending_zoom_offset_apply = False
        self._last_applied_zoom_offset_steps = self.zoom_offset_steps
        if self.zoom_offset_steps:
            self.web_view.page().runJavaScript(self._map_zoom_delta_script(self.zoom_offset_steps, 180))

    def _on_zoom_offset_changed(self, *_args):
        new_offset = int(self.zoom_offset_combo.currentData() or 0)
        delta = new_offset - self._last_applied_zoom_offset_steps
        self.zoom_offset_steps = new_offset
        self._last_applied_zoom_offset_steps = new_offset
        if self.active_scene_key and self.active_grid_idx >= 0 and delta:
            try:
                self.web_view.page().runJavaScript(self._map_zoom_delta_script(delta))
            except Exception:
                self._rerender_current_map()

    def _on_context_bins_changed(self, state: int):
        self.context_bins_enabled = bool(state)
        if self.active_scene_key and self.active_grid_idx >= 0:
            self._rerender_current_map()

    def _on_mine_polygons_changed(self, state: int):
        self.mine_polygons_enabled = bool(state) and self.mine_polygons_available
        if self.active_scene_key and self.active_grid_idx >= 0:
            self._rerender_current_map()

    def _matrix_coords(self, grid_index: str):
        match = re.search(r"Matrix_(\d+)_(\d+)", str(grid_index))
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    def _context_neighbor_grids(self, grids: list, active_grid: dict) -> list:
        active_coords = self._matrix_coords(active_grid.get("grid_index", ""))
        if not active_coords:
            return []
        neighbors = []
        for grid in grids:
            if grid is active_grid:
                continue
            coords = self._matrix_coords(grid.get("grid_index", ""))
            if not coords:
                continue
            if max(abs(coords[0] - active_coords[0]), abs(coords[1] - active_coords[1])) == 1:
                neighbors.append(grid)
        return neighbors

    def _on_descriptor_language_changed(self, *_args):
        self.descriptor_tooltip_language = self.descriptor_language_combo.currentData() or "en"
        if self.descriptor_overlay_enabled:
            self._rerender_current_map()

    def show_shortcut_help(self):
        dialog = PagedHelpDialog(self._shortcut_help_pages(), self)
        dialog.exec()

    def _shortcut_help_pages(self) -> list:
        base_style = """
            <style>
                body { font-family: Arial, sans-serif; background: #111827; color: #F8FAFC; margin: 0; padding: 18px; }
                h1 { font-size: 20px; margin: 0 0 14px 0; color: #FFFFFF; }
                h2 { font-size: 13px; margin: 16px 0 8px 0; color: #93C5FD; text-transform: uppercase; letter-spacing: 0; }
                .key { display: inline-block; min-width: 72px; padding: 5px 8px; margin-right: 10px; border-radius: 6px; background: #1F2937; border: 1px solid #4B5563; color: #FFFFFF; font-weight: 800; text-align: center; }
                .row { padding: 8px 0; border-bottom: 1px solid #263244; }
                .desc { color: #CBD5E1; }
                .pill { display: inline-block; padding: 4px 9px; border-radius: 999px; background: #2563EB; color: #FFFFFF; font-size: 11px; font-weight: 800; }
                .warn { color: #FDE68A; }
            </style>
        """
        keys = self.shortcut_bindings
        return [
            base_style + f"""
            <h1>Keyboard Flow</h1>
            <div class='row'><span class='key'>{html_lib.escape(keys['toggle_component'])}</span><span class='desc'>Toggle the current component between OFF and ON.</span></div>
            <div class='row'><span class='key'>{html_lib.escape(keys['next_component'])}</span><span class='desc'>Move to the next component without changing its value.</span></div>
            <div class='row'><span class='key'>{html_lib.escape(keys['previous_component'])}</span><span class='desc'>Move to the previous component.</span></div>
            <div class='row'><span class='key'>{html_lib.escape(keys['complete_next_bin'])}</span><span class='desc'>Complete the current bin and move directly to the next matrix bin. At the last bin, move to the next scene.</span></div>
            <div class='row'><span class='key'>{html_lib.escape(keys['previous_bin'])}</span><span class='desc'>Move directly to the previous matrix bin.</span></div>
            <div class='row'><span class='key'>{html_lib.escape(keys['undo'])}</span><span class='desc'>Undo the last component, flag, or bin completion action.</span></div>
            <div class='row'><span class='key'>{html_lib.escape(keys['redo'])}</span><span class='desc'>Redo the last undone component, flag, or bin completion action.</span></div>
            """,
            base_style + """
            <h1>Labeling Model</h1>
            <h2>Boolean component labels</h2>
            <div class='row'><span class='pill'>OFF</span> <span class='desc'>Default value. Use j to leave it unchanged and continue scanning.</span></div>
            <div class='row'><span class='pill'>ON</span> <span class='desc'>Use a to toggle the active component on. Press a again to turn it back off.</span></div>
            <div class='row'><span class='desc'>Record existence only: if one or more examples of the target component appear inside the 3x3 bin, mark ON. Do not count multiple examples.</span></div>
            <div class='row'><span class='desc'>When uncertain, keep OFF and continue. Use the descriptor overlay or each component's ? button to compare morphology before turning a component ON.</span></div>
            """,
            base_style + """
            <h1>Progress Meaning</h1>
            <h2>Auto-save</h2>
            <div class='row'><span class='desc'>Changing a value starts the bin and writes eval_start, but does not mark the bin complete.</span></div>
            <div class='row'><span class='desc'>The final component + j, or Enter, completes the bin and writes eval_end.</span></div>
            <div class='row'><span class='desc'>Green completion means eval_end exists: the bin has been explicitly reviewed, not merely opened.</span></div>
            <div class='row'><span class='desc'>Opening a new bin shows OFF defaults in the UI without creating a completed record.</span></div>
            """,
            base_style + """
            <h1>Workspace Tools</h1>
            <div class='row'><span class='key'>Open Dir</span><span class='desc'>Choose another working directory and rebuild the scene list from that root.</span></div>
            <div class='row'><span class='key'>Filters</span><span class='desc'>Limit scene roots by nation and mineral code.</span></div>
            <div class='row'><span class='key'>Overlay</span><span class='desc'>Show or hide the descriptor guide on top of the map.</span></div>
            <div class='row'><span class='key'>Purge Scene</span><span class='desc'>Delete the active scene's CSV label rows after confirmation.</span></div>
            <div class='row'><span class='key'>Full Purge</span><span class='desc warn'>Delete labeling CSV files under the active workspace after confirmation.</span></div>
            """
        ]

    def _descriptor_overlay_html(self) -> str:
        if self.descriptor_tooltip_language == "ko":
            title = "Descriptor Guide (KR)"
        else:
            title = "Descriptor Guide (EN)"

        chunks = [f"<div style='font-weight: 700; font-size: 13px; margin-bottom: 8px;'>{title}</div>"]
        for key in DESCRIPTOR_GUIDE_ORDER:
            info = TAXONOMY_DEFINITIONS[key]
            label = html_lib.escape(info["label"])
            desc = html_lib.escape(self._component_descriptor_text(key))
            chunks.append(
                "<div style='margin-bottom: 7px;'>"
                f"<div style='font-weight: 700; color: #FFE082;'>{label}</div>"
                f"<div>{desc}</div>"
                "</div>"
            )
        return "".join(chunks)

    def _add_descriptor_overlay(self, folium_map):
        if self.descriptor_overlay_enabled:
            folium_map.add_child(DescriptorOverlay(self._descriptor_overlay_html()))

    def toggle_descriptor_overlay(self):
        self.descriptor_overlay_enabled = not self.descriptor_overlay_enabled
        self.btn_descriptor_help.setText("Hide Descriptor Overlay" if self.descriptor_overlay_enabled else "Show Descriptor Overlay")
        self._rerender_current_map()

    def _rerender_current_map(self):
        if self.active_scene_key and self.active_grid_idx >= 0:
            item = self.grid_list_widget.item(self.active_grid_idx)
            if item:
                self.on_grid_changed(item)
                return
        self._load_empty_map()

    def _set_matrix_bin_count(self, count: int = 0):
        suffix = f" - {count} shown" if count else " - 0 shown"
        self.lbl_grids.setText(f"Native Spatial Matrix Bins ({SUPER_GRID_SIZE}x{SUPER_GRID_SIZE} Sub-Matrix){suffix}:")

    def _focus_first_taxonomy_widget(self) -> bool:
        if not self.active_scene_key or self.active_grid_idx == -1:
            return False
        self._set_active_component_index(0)
        self._taxonomy_cursor_reset_pending = False
        return True

    def _reset_taxonomy_cursor(self):
        self._taxonomy_cursor_reset_pending = True
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _consume_first_taxonomy_tab(self) -> bool:
        if not self._taxonomy_cursor_reset_pending:
            return False
        return self._focus_first_taxonomy_widget()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            if not self._taxonomy_widget_has_focus() and event.key() == Qt.Key.Key_Tab:
                return self._consume_first_taxonomy_tab() or self._focus_first_taxonomy_widget()
        return super().eventFilter(obj, event)

    def _load_empty_map(self):
        self._pending_zoom_offset_apply = False
        m = folium.Map(location=[0, 0], zoom_start=2)
        self._add_descriptor_overlay(m)
        m.add_child(MapHudOverlay(self._map_hud_html()))
        data = io.BytesIO()
        m.save(data, close_file=False)
        self.web_view.setHtml(data.getvalue().decode('utf-8'), get_map_base_url())
        data.close()

    def _reset_scene_filters(self):
        if not hasattr(self, "country_filter"):
            return
        self.country_filter.blockSignals(True)
        self.mineral_filter.blockSignals(True)
        self.country_filter.clear()
        self.mineral_filter.clear()
        self.country_filter.addItem("All Nations", "")
        self.mineral_filter.addItem("All Minerals", "")
        self.country_filter.blockSignals(False)
        self.mineral_filter.blockSignals(False)

    def _delete_label_csv_files(self, root_dir: str) -> tuple[int, list]:
        deleted = 0
        failed = []
        csv_paths = set()
        if self.data_manager:
            csv_paths.add(normalize_spatial_path(self.data_manager.csv_path))
        for root, _, files in os.walk(normalize_spatial_path(root_dir)):
            for file_name in files:
                if file_name in {"nk_mining_taxonomy.csv", "nk_mining_taxonomy_merged.csv"}:
                    csv_paths.add(normalize_spatial_path(os.path.join(root, file_name)))

        for csv_path in sorted(csv_paths):
            if not os.path.exists(csv_path):
                continue
            try:
                os.remove(csv_path)
                deleted += 1
            except Exception as exc:
                failed.append(f"{csv_path}: {exc}")
        return deleted, failed

    def _delete_output_gpkg_files(self) -> tuple[int, list]:
        deleted = 0
        failed = []
        gpkg_dir = get_gpkg_output_dir()
        if not os.path.exists(gpkg_dir):
            return deleted, failed
        for root, _, files in os.walk(gpkg_dir):
            for file_name in files:
                if not file_name.lower().endswith(".gpkg"):
                    continue
                path = normalize_spatial_path(os.path.join(root, file_name))
                try:
                    os.remove(path)
                    deleted += 1
                except Exception as exc:
                    failed.append(f"{path}: {exc}")
        return deleted, failed

    def _delete_scene_output_gpkg(self, scene_data: dict) -> bool:
        path = self._scene_gpkg_path(scene_data)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def _reset_active_view(self):
        self._flush_dirty_gpkg_exports()
        self.scene_database = {}
        self.active_scene_key = None
        self.active_grid_idx = -1
        self._pending_grid_selection = None
        self._taxonomy_cursor_reset_pending = False
        self.active_component_idx = 0
        self.count_undo_stack.clear()
        self.count_redo_stack.clear()
        self._dirty_gpkg_scene_keys.clear()
        self.prefetch_thread.queue.clear()
        self.file_list_widget.clear()
        self.grid_list_widget.clear()
        self._set_matrix_bin_count(0)
        self._load_empty_map()
        for cb in self.tax_spinboxes.values():
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        if hasattr(self, "quality_flag_checkbox"):
            self.quality_flag_checkbox.blockSignals(True)
            self.quality_flag_checkbox.setChecked(False)
            self.quality_flag_checkbox.blockSignals(False)
            self._last_quality_flag = 0
        self._refresh_component_focus_styles()

    def purge_active_scene_database(self):
        if not self.active_scene_key or self.active_scene_key not in self.scene_database:
            QMessageBox.information(self, "No Active Scene", "Select a scene root before purging scene data.")
            return

        scene_data = self.scene_database[self.active_scene_key]
        scene_uid = extract_scene_uid(scene_data["scene_name"])
        tif_path = get_stable_scene_tif_path(scene_data["tif_path"])
        warning = (
            f"This will remove CSV label records for scene:\n{scene_data['scene_name']}\n\n"
            "This cannot be undone. Continue?"
        )
        reply = QMessageBox.warning(
            self,
            "Purge Scene Root Database",
            warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        removed_rows = self.data_manager.purge_scene_records(scene_uid, tif_path) if self.data_manager else 0
        self._dirty_gpkg_scene_keys.discard(self.active_scene_key)
        deleted_gpkg = 0
        try:
            deleted_gpkg = 1 if self._delete_scene_output_gpkg(scene_data) else 0
        except Exception:
            deleted_gpkg = 0
        self.scene_database[self.active_scene_key]['grid_cells'] = None
        self.grid_list_widget.clear()
        self.active_grid_idx = -1
        self._load_empty_map()
        self._reset_taxonomy_cursor()
        self.on_scene_changed(self.file_list_widget.currentItem())

        msg = f"Scene purge complete. Removed {removed_rows} CSV row(s), deleted {deleted_gpkg} output GPKG file(s)."
        QMessageBox.information(self, "Scene Purge Complete", msg)

    def purge_all_databases(self):
        if not self.current_folder:
            QMessageBox.information(self, "No Working Directory", "Open a working directory before full purge.")
            return

        warning = (
            f"This will delete labeling CSV files under:\n{self.current_folder}\n\n"
            f"Primary labeling CSV:\n{self.data_manager.csv_path if self.data_manager else ''}\n\n"
            "This is intended only for catastrophic reset situations and cannot be undone. Continue?"
        )
        reply = QMessageBox.warning(
            self,
            "Full Database Purge",
            warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        removed_rows = len(self.data_manager.df) if self.data_manager else 0
        deleted_csv, csv_failed = self._delete_label_csv_files(self.current_folder)
        deleted_gpkg, gpkg_failed = self._delete_output_gpkg_files()
        failed = csv_failed + gpkg_failed
        self.data_manager = TaxonomyDataManager(self.current_folder)
        self._dirty_gpkg_scene_keys.clear()
        self._reset_active_view()
        self.open_working_directory(self.current_folder)

        msg = (
            f"Full purge complete. Deleted {deleted_csv} CSV file(s), "
            f"deleted {deleted_gpkg} output GPKG file(s), removed {removed_rows} CSV row(s)."
        )
        if failed:
            msg += f"\n\nFailed to delete {len(failed)} file(s). Check permissions or open file handles."
        QMessageBox.information(self, "Full Purge Complete", msg)

    def select_directory(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Root Directory", self.current_folder or get_script_dir())
        if not folder: return
        self.open_working_directory(folder)

    def _choose_csv_conflict_policy(self) -> str:
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle("CSV Merge Conflict Policy")
        msg.setText("Imported rows conflict with existing output rows.")
        msg.setInformativeText("Choose how to handle conflicting non-empty values.")
        keep_btn = msg.addButton("Keep Current", QMessageBox.ButtonRole.AcceptRole)
        overwrite_btn = msg.addButton("Overwrite With Imported", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = msg.addButton("Cancel Merge", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == keep_btn:
            return "keep"
        if clicked == overwrite_btn:
            return "overwrite"
        if clicked == cancel_btn:
            return ""
        return ""

    def _ask_calculate_missing_jpg_before_merge(self, missing_rows: int) -> str:
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle("Calculate JPG References")
        msg.setText(f"{missing_rows} imported row(s) are missing top-left or bottom-right JPG references.")
        msg.setInformativeText(
            "Calculate them from the current scene bin geometry before merging?\n\n"
            "This may compile the related scenes, but only after this confirmation."
        )
        calculate_btn = msg.addButton("Calculate", QMessageBox.ButtonRole.AcceptRole)
        skip_btn = msg.addButton("Merge Without JPG Fill", QMessageBox.ButtonRole.ActionRole)
        cancel_btn = msg.addButton("Cancel Merge", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == calculate_btn:
            return "calculate"
        if clicked == skip_btn:
            return "skip"
        if clicked == cancel_btn:
            return ""
        return ""

    def _ask_update_gpkg_after_merge(self, scene_count: int) -> bool:
        scene_text = f"{scene_count} related scene(s)" if scene_count else "the related scenes"
        reply = QMessageBox.question(
            self,
            "Update GPKG Outputs",
            f"CSV merge is complete. Generate or update GPKG files for {scene_text}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _normalize_import_scene_uid(self, value: str) -> str:
        text = str(value or "").strip()
        if "::" in text:
            text = text.split("::", 1)[0].strip()
        match = re.search(r'([A-Z]{3})[-_]([CIG])[-_](\d+)', text, re.IGNORECASE)
        if match:
            return f"{match.group(1).upper()}-{match.group(2).upper()}-{match.group(3)}"
        return text

    def _scene_uid_candidates_from_row(self, row: pd.Series) -> list:
        candidates = []
        for col in ["scene_uid", "annotation_key", "scene_name", "identifier"]:
            value = self._normalize_import_scene_uid(row.get(col, ""))
            if value and value not in candidates:
                candidates.append(value)
        return candidates

    def _scene_key_for_import_row(self, row: pd.Series) -> str:
        uid_candidates = self._scene_uid_candidates_from_row(row)
        for scene_key, scene_data in self.scene_database.items():
            scene_uid = extract_scene_uid(scene_data.get("scene_name", ""))
            if scene_uid in uid_candidates:
                return normalize_spatial_path(scene_key)

        row_tif = normalize_spatial_path(str(row.get("scene_tif_path", "")).strip())
        if row_tif:
            for scene_key, scene_data in self.scene_database.items():
                stable_tif = get_stable_scene_tif_path(scene_data.get("tif_path", ""))
                if row_tif == stable_tif or row_tif == normalize_spatial_path(scene_data.get("tif_path", "")):
                    return normalize_spatial_path(scene_key)
        return ""

    def _scene_uids_from_import_dataframe(self, df: pd.DataFrame) -> set:
        scene_uids = set()
        for _, row in df.iterrows():
            for candidate in self._scene_uid_candidates_from_row(row):
                if candidate:
                    scene_uids.add(candidate)
        return scene_uids

    def _scene_keys_for_import_uids(self, scene_uids: set) -> list:
        keys = []
        for scene_key, scene_data in self.scene_database.items():
            if extract_scene_uid(scene_data.get("scene_name", "")) in scene_uids:
                keys.append(normalize_spatial_path(scene_key))
        return keys

    def _ensure_scene_grids_for_output(self, scene_key: str) -> bool:
        norm_scene_key = normalize_spatial_path(scene_key)
        scene_data = self.scene_database.get(norm_scene_key)
        if not scene_data:
            return False
        if scene_data.get("grid_cells") is None:
            self.lbl_status.setText(f"Compiling scene bins for {scene_data.get('scene_name', '')}...")
            QApplication.processEvents()
            grids = _compile_scene_grids(norm_scene_key, scene_data, self.icmm_df, self.current_folder, self.data_manager)
            scene_data["grid_cells"] = grids
        return bool(scene_data.get("grid_cells"))

    def _enrich_import_dataframe_jpg_metadata(self, df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
        enriched = self.data_manager._normalize_dataframe(df.copy())
        stats = {
            "missing_rows": 0,
            "recovered_rows": 0,
            "unresolved_rows": 0,
            "compiled_scenes": set(),
            "failed": [],
        }

        for idx, row in enriched.iterrows():
            top_missing = self.data_manager._is_blank_value(row.get("top_left_jpg", ""))
            bottom_missing = self.data_manager._is_blank_value(row.get("bottom_right_jpg", ""))
            if not top_missing and not bottom_missing:
                continue
            stats["missing_rows"] += 1

            scene_key = self._scene_key_for_import_row(row)
            grid_index = str(row.get("grid_index", "")).strip()
            if not scene_key or not grid_index:
                stats["unresolved_rows"] += 1
                continue

            scene_data = self.scene_database.get(scene_key)
            was_uncompiled = bool(scene_data and scene_data.get("grid_cells") is None)
            try:
                if not self._ensure_scene_grids_for_output(scene_key):
                    stats["unresolved_rows"] += 1
                    continue
                if was_uncompiled:
                    stats["compiled_scenes"].add(scene_key)
            except Exception as exc:
                stats["failed"].append(f"{os.path.basename(scene_key)}: {exc}")
                stats["unresolved_rows"] += 1
                continue

            grid_match = None
            for grid in self.scene_database[scene_key].get("grid_cells") or []:
                if str(grid.get("grid_index", "")).strip() == grid_index:
                    grid_match = grid
                    break
            if not grid_match:
                stats["unresolved_rows"] += 1
                continue

            if top_missing:
                enriched.at[idx, "top_left_jpg"] = grid_match.get("top_left_jpg", "")
            if bottom_missing:
                enriched.at[idx, "bottom_right_jpg"] = grid_match.get("bottom_right_jpg", "")

            if (
                not self.data_manager._is_blank_value(enriched.at[idx, "top_left_jpg"])
                and not self.data_manager._is_blank_value(enriched.at[idx, "bottom_right_jpg"])
            ):
                stats["recovered_rows"] += 1
            else:
                stats["unresolved_rows"] += 1

        return self.data_manager._normalize_dataframe(enriched), stats

    def _refresh_scene_outputs_for_import_uids(self, scene_uids: set) -> dict:
        stats = {"generated": 0, "skipped": 0, "failed": []}
        for scene_key in self._scene_keys_for_import_uids(scene_uids):
            scene_data = self.scene_database.get(scene_key, {})
            try:
                if not self._ensure_scene_grids_for_output(scene_key):
                    stats["skipped"] += 1
                    continue
                self._fill_scene_jpg_metadata(scene_key)
                self._export_scene_gpkg(scene_key)
                gpkg_path = self._scene_gpkg_path(scene_data)
                if os.path.exists(gpkg_path):
                    stats["generated"] += 1
                else:
                    stats["failed"].append(f"{scene_data.get('scene_name', os.path.basename(scene_key))}: output file was not created")
            except Exception as exc:
                stats["failed"].append(f"{scene_data.get('scene_name', os.path.basename(scene_key))}: {exc}")
        return stats

    def _prompt_for_external_csv_imports(self):
        if not self.data_manager:
            return
        candidates = self.data_manager.external_csv_candidates()
        if not candidates:
            return

        import_items = []
        failures = []
        for path in candidates:
            try:
                incoming = self.data_manager.load_external_csv_dataframe(path)
                import_items.append({
                    "path": path,
                    "df": incoming,
                    "analysis": self.data_manager.analyze_external_dataframe(incoming, path),
                })
            except Exception as exc:
                failures.append(f"{os.path.basename(path)}: {exc}")
        if not import_items:
            if failures:
                QMessageBox.warning(self, "CSV Import Analysis Failed", "\n".join(failures[:20]))
            return

        analyses = [item["analysis"] for item in import_items]
        total_rows = sum(item["rows"] for item in analyses)
        total_new = sum(item["new_rows"] for item in analyses)
        total_matched = sum(item["matched_rows"] for item in analyses)
        total_conflicts = sum(item["conflict_rows"] for item in analyses)
        total_fillable = sum(item["fillable_rows"] for item in analyses)
        total_missing_jpg = sum(item["missing_jpg_rows"] for item in analyses)
        scene_ids = set()
        for item in import_items:
            scene_ids.update(item["analysis"].get("scenes", set()))
            scene_ids.update(self._scene_uids_from_import_dataframe(item["df"]))
        total_scenes = len(scene_ids)
        lines = []
        for item in analyses[:8]:
            lines.append(
                f"- {os.path.basename(item['path'])}: {item['rows']} rows, "
                f"{item['new_rows']} new, {item['conflict_rows']} conflicts, "
                f"{item['missing_jpg_rows']} missing JPG refs"
            )
        if len(analyses) > 8:
            lines.append(f"- ... and {len(analyses) - 8} more")
        if failures:
            lines.append("")
            lines.append("Skipped during analysis:")
            lines.extend(f"- {failure}" for failure in failures[:5])

        summary = (
            "\n".join(lines)
            + "\n\nTotals:"
            + f"\nRows: {total_rows}"
            + f"\nNew rows: {total_new}"
            + f"\nMatching rows: {total_matched}"
            + f"\nRows with conflicting values: {total_conflicts}"
            + f"\nRows that can fill blank fields: {total_fillable}"
            + f"\nRows missing JPG references: {total_missing_jpg}"
            + f"\nRelated scenes: {total_scenes}"
        )

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle("External CSV Import Preview")
        msg.setText(f"Found {len(analyses)} importable CSV file(s) in labeling_output.")
        msg.setInformativeText(summary + "\n\nMerge them into nk_mining_taxonomy.csv?")
        merge_btn = msg.addButton("Merge", QMessageBox.ButtonRole.AcceptRole)
        ignore_btn = msg.addButton("Ignore", QMessageBox.ButtonRole.RejectRole)
        cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked != merge_btn:
            return

        jpg_fill_summaries = []
        if total_missing_jpg > 0:
            jpg_choice = self._ask_calculate_missing_jpg_before_merge(total_missing_jpg)
            if not jpg_choice:
                return
            if jpg_choice == "calculate":
                enriched_items = []
                total_recovered = 0
                total_unresolved = 0
                compiled_scene_count = 0
                for item in import_items:
                    enriched_df, stats = self._enrich_import_dataframe_jpg_metadata(item["df"])
                    item["df"] = enriched_df
                    item["analysis"] = self.data_manager.analyze_external_dataframe(enriched_df, item["path"])
                    enriched_items.append(item)
                    total_recovered += stats["recovered_rows"]
                    total_unresolved += stats["unresolved_rows"]
                    compiled_scene_count += len(stats["compiled_scenes"])
                    for failure in stats["failed"][:5]:
                        failures.append(f"{os.path.basename(item['path'])}: {failure}")
                import_items = enriched_items
                analyses = [item["analysis"] for item in import_items]
                total_conflicts = sum(item["conflict_rows"] for item in analyses)
                scene_ids = set()
                for item in import_items:
                    scene_ids.update(item["analysis"].get("scenes", set()))
                    scene_ids.update(self._scene_uids_from_import_dataframe(item["df"]))
                total_scenes = len(scene_ids)
                jpg_fill_summaries.append(
                    f"JPG refs filled: {total_recovered} row(s), unresolved: {total_unresolved}, "
                    f"compiled scenes: {compiled_scene_count}"
                )

        policy = "keep"
        if total_conflicts > 0:
            policy = self._choose_csv_conflict_policy()
            if not policy:
                return

        merge_summaries = []
        failures = failures[:]
        for item in import_items:
            path = item["path"]
            try:
                result = self.data_manager.merge_external_dataframe(item["df"], path, policy)
                archived_path = self.data_manager.archive_imported_csv(path)
                merge_summaries.append(
                    f"{os.path.basename(path)}: +{result['added']} rows, {result['updated']} updated, "
                    f"{result['conflicts']} conflicts"
                )
                if result.get("backup"):
                    merge_summaries.append(f"Backup: {os.path.basename(result['backup'])}")
                merge_summaries.append(f"Archived: imported/{os.path.basename(archived_path)}")
            except Exception as exc:
                failures.append(f"{os.path.basename(path)}: {exc}")

        summaries = jpg_fill_summaries + merge_summaries
        if summaries:
            QMessageBox.information(self, "CSV Merge Complete", "\n".join(summaries[:20]))
        if failures:
            QMessageBox.warning(self, "CSV Merge Issues", "\n".join(failures[:20]))
        if merge_summaries and self._ask_update_gpkg_after_merge(total_scenes):
            gpkg_stats = self._refresh_scene_outputs_for_import_uids(scene_ids)
            gpkg_lines = [
                f"Generated/updated: {gpkg_stats['generated']} scene GPKG file(s)",
                f"Skipped: {gpkg_stats['skipped']} scene(s)",
            ]
            if gpkg_stats["failed"]:
                gpkg_lines.append("")
                gpkg_lines.append("Issues:")
                gpkg_lines.extend(gpkg_stats["failed"][:10])
            QMessageBox.information(self, "GPKG Output Update", "\n".join(gpkg_lines))

    def _scene_grid_metadata_records(self, scene_key: str) -> list:
        scene_data = self.scene_database.get(normalize_spatial_path(scene_key), {})
        grids = scene_data.get("grid_cells") or []
        tif_rel_path = get_stable_scene_tif_path(scene_data.get("tif_path", ""))
        records = []
        for grid in grids:
            records.append({
                "annotation_key": grid.get("annotation_key"),
                "scene_tif_path": tif_rel_path,
                "grid_index": grid.get("grid_index", ""),
                "top_left_jpg": grid.get("top_left_jpg", ""),
                "bottom_right_jpg": grid.get("bottom_right_jpg", ""),
            })
        return records

    def _fill_scene_jpg_metadata(self, scene_key: str):
        if self.data_manager:
            self.data_manager.update_grid_jpg_metadata(self._scene_grid_metadata_records(scene_key))

    def _scene_gpkg_path(self, scene_data: dict) -> str:
        os.makedirs(get_gpkg_output_dir(), exist_ok=True)
        scene_id = extract_output_identifier(scene_data.get("scene_name", "scene"))
        return normalize_spatial_path(os.path.join(get_gpkg_output_dir(), f"{scene_id}_matrix.gpkg"))

    def _mark_scene_gpkg_dirty(self, scene_key: str):
        norm_scene_key = normalize_spatial_path(scene_key)
        if norm_scene_key and norm_scene_key in self.scene_database:
            self._dirty_gpkg_scene_keys.add(norm_scene_key)

    def _flush_dirty_gpkg_exports(self, scene_keys=None):
        if not self.data_manager or not getattr(self, "_dirty_gpkg_scene_keys", None):
            return
        targets = [normalize_spatial_path(k) for k in (scene_keys or list(self._dirty_gpkg_scene_keys))]
        for scene_key in targets:
            if scene_key not in self._dirty_gpkg_scene_keys:
                continue
            scene_data = self.scene_database.get(scene_key)
            if not scene_data or not scene_data.get("grid_cells"):
                self._dirty_gpkg_scene_keys.discard(scene_key)
                continue
            try:
                self._export_scene_gpkg(scene_key)
                self._dirty_gpkg_scene_keys.discard(scene_key)
            except Exception as exc:
                print(f"Could not refresh deferred GPKG for {scene_key}: {exc}")

    def _export_scene_gpkg(self, scene_key: str):
        if not self.data_manager:
            return
        norm_scene_key = normalize_spatial_path(scene_key)
        scene_data = self.scene_database.get(norm_scene_key)
        if not scene_data or not scene_data.get("grid_cells"):
            return
        grids = scene_data["grid_cells"]
        self._fill_scene_jpg_metadata(norm_scene_key)

        gpkg_path = self._scene_gpkg_path(scene_data)
        driver = ogr.GetDriverByName("GPKG")
        if os.path.exists(gpkg_path):
            try:
                driver.DeleteDataSource(gpkg_path)
            except Exception:
                try:
                    os.remove(gpkg_path)
                except Exception as exc:
                    print(f"Could not replace output GPKG {gpkg_path}: {exc}")
                    return

        ds = driver.CreateDataSource(gpkg_path)
        if ds is None:
            return
        srs = make_srs(4326)
        layer = ds.CreateLayer("matrix_bins", srs, ogr.wkbPolygon)
        string_fields = [
            "annotation_key", "scene_uid", "scene_name", "scene_tif_path", "identifier",
            "grid_index", "mining_category", "top_left_jpg", "bottom_right_jpg",
            "eval_start", "eval_end",
        ]
        int_fields = ["mine_point_count"] + COMPONENT_KEYS + [QUALITY_FLAG_KEY]
        for field_name in string_fields:
            layer.CreateField(ogr.FieldDefn(field_name, ogr.OFTString))
        for field_name in int_fields:
            layer.CreateField(ogr.FieldDefn(field_name, ogr.OFTInteger))

        def safe_int(value) -> int:
            try:
                if pd.isna(value):
                    return 0
                return int(float(value))
            except Exception:
                return 0

        tif_rel_path = get_stable_scene_tif_path(scene_data["tif_path"])
        for grid in grids:
            rec = self.data_manager.get_grid_record(tif_rel_path, grid["grid_index"], grid.get("annotation_key")) or {}
            feat = ogr.Feature(layer.GetLayerDefn())
            values = {
                "annotation_key": grid.get("annotation_key", ""),
                "scene_uid": grid.get("scene_uid", ""),
                "scene_name": scene_data.get("scene_name", ""),
                "scene_tif_path": tif_rel_path,
                "identifier": grid.get("identifier", ""),
                "grid_index": grid.get("grid_index", ""),
                "mining_category": grid.get("mining_category", "Unknown"),
                "top_left_jpg": grid.get("top_left_jpg", ""),
                "bottom_right_jpg": grid.get("bottom_right_jpg", ""),
                "eval_start": rec.get("eval_start", ""),
                "eval_end": rec.get("eval_end", ""),
            }
            for field_name in string_fields:
                value = values.get(field_name, "")
                feat.SetField(field_name, "" if pd.isna(value) else str(value))
            feat.SetField("mine_point_count", safe_int(grid.get("mine_point_count", 0)))
            for key in COMPONENT_KEYS:
                feat.SetField(key, safe_int(rec.get(key, 0)))
            feat.SetField(QUALITY_FLAG_KEY, safe_int(rec.get(QUALITY_FLAG_KEY, 0)))
            feat.SetGeometry(grid_cell_polygon(grid))
            layer.CreateFeature(feat)
            feat = None
        ds = None

    def _refresh_compiled_scene_outputs(self):
        for scene_key, scene_data in self.scene_database.items():
            if scene_data.get("grid_cells"):
                self._fill_scene_jpg_metadata(scene_key)
                self._export_scene_gpkg(scene_key)

    def open_working_directory(self, folder: str):
        self._flush_dirty_gpkg_exports()
        self._workspace_load_id += 1
        load_id = self._workspace_load_id
        self.current_folder = normalize_spatial_path(folder)
        self.data_manager = TaxonomyDataManager(self.current_folder)

        self.icmm_df = pd.DataFrame()
        self.scene_database = {}
        self.active_scene_key = None
        self.active_grid_idx = -1
        self.active_worker = None
        self._pending_grid_selection = None
        self._taxonomy_cursor_reset_pending = False
        self.active_component_idx = 0
        self.count_undo_stack.clear()
        self.count_redo_stack.clear()
        self.prefetch_thread.queue.clear()
        self.prefetch_thread.set_database_ref(self.scene_database)
        self.prefetch_thread.icmm_df = pd.DataFrame()
        self.prefetch_thread.current_folder = self.current_folder
        self.prefetch_thread.data_manager = self.data_manager

        self.file_list_widget.clear()
        self.grid_list_widget.clear()
        self._reset_scene_filters()
        self._load_empty_map()
        self._refresh_component_focus_styles()

        self.btn_select_dir.setEnabled(False)
        self.lbl_status.setText(f"System: Booting ICMM Spatial Ingestion Engine for {self.current_folder}...")
        self.icmm_worker = ICMMDataLoader()
        self.icmm_worker.loading_finished.connect(lambda df, lid=load_id: self._on_icmm_ready(df, lid))
        self.icmm_worker.start()

    def _on_icmm_ready(self, df, load_id: int):
        if load_id != self._workspace_load_id:
            return
        self.icmm_df = df
        
        self.prefetch_thread.icmm_df = df
        self.prefetch_thread.current_folder = self.current_folder
        self.prefetch_thread.data_manager = self.data_manager
        
        self.btn_select_dir.setEnabled(False)
        self.lbl_status.setText(f"System: ICMM Dataset Ready ({len(df)} records loaded). Scanning workspace...")
        
        self.scanner_thread = DirectoryScannerThread(self.current_folder)
        self.scanner_thread.scan_finished.connect(lambda scenes, lid=load_id: self._on_scan_completed(scenes, lid))
        self.scanner_thread.start()

    def _scene_filter_tokens(self, scene_name: str) -> tuple:
        match = re.search(r'([A-Z]{3})[-_]([CIG])[-_](\d+)', scene_name, re.IGNORECASE)
        if not match:
            return "", ""
        return match.group(1).upper(), match.group(2).upper()

    def _natural_sort_key(self, text: str) -> list:
        return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', text)]

    def _populate_scene_filters(self):
        countries = set()
        minerals = set()
        for scene_data in self.scene_database.values():
            country, mineral = self._scene_filter_tokens(scene_data['scene_name'])
            if country:
                countries.add(country)
            if mineral:
                minerals.add(mineral)

        current_country = self.country_filter.currentData() if hasattr(self, "country_filter") else ""
        current_mineral = self.mineral_filter.currentData() if hasattr(self, "mineral_filter") else ""

        self.country_filter.blockSignals(True)
        self.mineral_filter.blockSignals(True)
        self.country_filter.clear()
        self.mineral_filter.clear()
        self.country_filter.addItem("All Nations", "")
        self.mineral_filter.addItem("All Minerals", "")
        for country in sorted(countries):
            self.country_filter.addItem(country, country)
        mineral_labels = {"C": "C - Coal", "G": "G - Gold", "I": "I - Iron"}
        for mineral in sorted(minerals):
            self.mineral_filter.addItem(mineral_labels.get(mineral, mineral), mineral)

        country_idx = self.country_filter.findData(current_country)
        mineral_idx = self.mineral_filter.findData(current_mineral)
        self.country_filter.setCurrentIndex(country_idx if country_idx >= 0 else 0)
        self.mineral_filter.setCurrentIndex(mineral_idx if mineral_idx >= 0 else 0)
        self.country_filter.blockSignals(False)
        self.mineral_filter.blockSignals(False)

    def _scene_matches_filters(self, scene_data: dict) -> bool:
        country_filter = self.country_filter.currentData()
        mineral_filter = self.mineral_filter.currentData()
        country, mineral = self._scene_filter_tokens(scene_data['scene_name'])
        if country_filter and country != country_filter:
            return False
        if mineral_filter and mineral != mineral_filter:
            return False
        return True

    def _scene_completion_summary(self, scene_data: dict) -> tuple[int, int]:
        tif_path = get_stable_scene_tif_path(scene_data["tif_path"])
        scene_uid = extract_scene_uid(scene_data["scene_name"])
        total = len(scene_data.get("grid_cells") or [])
        completed = self.data_manager.scene_evaluated_count(tif_path, scene_uid) if self.data_manager else 0
        if total == 0 and self.data_manager:
            total = self.data_manager.scene_record_count(tif_path, scene_uid)
        return completed, total

    def _apply_scene_item_completion_style(self, item: QListWidgetItem, scene_data: dict):
        completed, total = self._scene_completion_summary(scene_data)
        if completed <= 0:
            item.setBackground(QColor(Qt.GlobalColor.transparent))
            item.setForeground(self.palette().color(QPalette.ColorRole.Text))
        elif total > 0 and completed >= total:
            item.setBackground(QColor("#E8F5E9"))
            item.setForeground(QColor("#2E7D32"))
        else:
            item.setBackground(QColor("#FFF4E5"))
            item.setForeground(QColor("#B45309"))

    def _refresh_scene_list(self):
        if not hasattr(self, "file_list_widget"):
            return
        self.file_list_widget.clear()
        self.grid_list_widget.clear()
        self.active_scene_key = None
        self.active_grid_idx = -1
        self._pending_grid_selection = None
        self._set_matrix_bin_count(0)
        self._load_empty_map()

        sorted_scene_keys = sorted(
            self.scene_database.keys(),
            key=lambda k: self._natural_sort_key(self.scene_database[k]['scene_name'])
        )
        visible_count = 0
        for scene_key in sorted_scene_keys:
            scene_data = self.scene_database[scene_key]
            if not self._scene_matches_filters(scene_data):
                continue
            item = QListWidgetItem(scene_data['scene_name'])
            item.setData(Qt.ItemDataRole.UserRole, scene_key)
            self._apply_scene_item_completion_style(item, scene_data)
            self.file_list_widget.addItem(item)
            visible_count += 1

        total_count = len(self.scene_database)
        self.lbl_status.setText(f"Active Workspace Bound. Showing {visible_count} of {total_count} Scene Clusters.")

    def _on_scan_completed(self, scenes: dict, load_id: int):
        if load_id != self._workspace_load_id:
            return
        self.btn_select_dir.setEnabled(True)
        self.scene_database = scenes
        self.prefetch_thread.set_database_ref(self.scene_database)
        self._populate_scene_filters()
        self._refresh_scene_list()
        self._prompt_for_external_csv_imports()

    def on_scene_changed(self, current_item: QListWidgetItem):
        if not current_item: return
        scene_key = normalize_spatial_path(current_item.data(Qt.ItemDataRole.UserRole))
        if self.active_scene_key and normalize_spatial_path(self.active_scene_key) != scene_key:
            self._flush_dirty_gpkg_exports([self.active_scene_key])
        scene_data = self.scene_database[scene_key]
        self.active_scene_key = scene_key
        
        self.grid_list_widget.clear()
        self._set_matrix_bin_count(0)

        if scene_data['grid_cells'] is None:
            self.lbl_status.setText(f"JIT Compilation: Matrix clustering active for {scene_data['scene_name']}...")
            self.file_list_widget.setEnabled(False)
            
            self.active_worker = SceneProcessorWorker(scene_key, scene_data, self.icmm_df, self.current_folder, self.data_manager)
            self.active_worker.finished_signal.connect(self._on_active_scene_compiled)
            self.active_worker.start()
        else:
            self._render_scene_grids(scene_key)

    def _on_active_scene_compiled(self, scene_key: str, grids: list):
        self.scene_database[normalize_spatial_path(scene_key)]['grid_cells'] = grids
        self.file_list_widget.setEnabled(True)
        self._fill_scene_jpg_metadata(scene_key)
        self._mark_scene_gpkg_dirty(scene_key)
        self._render_scene_grids(scene_key)
        self._apply_pending_grid_selection(scene_key)

    def _on_prefetch_finished(self, scene_key: str, grids: list):
        self.scene_database[normalize_spatial_path(scene_key)]['grid_cells'] = grids
        self._fill_scene_jpg_metadata(scene_key)

    def _render_scene_grids(self, scene_key: str):
        scene_data = self.scene_database[normalize_spatial_path(scene_key)]
        grids = scene_data['grid_cells']
        self._set_matrix_bin_count(len(grids))
        
        tif_rel_path = get_stable_scene_tif_path(scene_data['tif_path'])

        for grid in grids:
            ui_item = QListWidgetItem(f"Matrix: {grid['grid_index']} ({len(grid['patches'])} patches)")
            if self.data_manager.is_grid_evaluated(tif_rel_path, grid['grid_index'], grid.get('annotation_key')):
                ui_item.setBackground(QColor("#E8F5E9"))
                ui_item.setForeground(QColor("#2E7D32"))
            self.grid_list_widget.addItem(ui_item)

        current_idx = self.file_list_widget.currentRow()
        prefetch_targets = []
        if current_idx > 0:
            prefetch_targets.append(self.file_list_widget.item(current_idx - 1).data(Qt.ItemDataRole.UserRole))
        if current_idx < self.file_list_widget.count() - 1:
            prefetch_targets.append(self.file_list_widget.item(current_idx + 1).data(Qt.ItemDataRole.UserRole))
            
        self.prefetch_thread.update_paths(prefetch_targets)

        if grids:
            target_idx = self._find_first_unfinished_grid_index(grids, tif_rel_path)
            self.grid_list_widget.setCurrentRow(target_idx)
            self.on_grid_changed(self.grid_list_widget.currentItem())

    def _apply_pending_grid_selection(self, scene_key: str):
        if not self._pending_grid_selection:
            return
        pending = self._pending_grid_selection
        pending_scene_key, target = pending[:2]
        initial_component_idx = int(pending[2]) if len(pending) > 2 else 0
        norm_scene_key = normalize_spatial_path(scene_key)
        if normalize_spatial_path(pending_scene_key) != norm_scene_key:
            return
        grids = self.scene_database[norm_scene_key].get('grid_cells') or []
        if not grids:
            return
        target_idx = len(grids) - 1 if target == "last" else int(target)
        target_idx = max(0, min(target_idx, len(grids) - 1))
        self._pending_grid_selection = None
        self.grid_list_widget.setCurrentRow(target_idx)
        self.on_grid_changed(self.grid_list_widget.currentItem(), initial_component_idx=initial_component_idx)

    def _find_first_unfinished_grid_index(self, grids: list, tif_rel_path: str) -> int:
        for i, grid in enumerate(grids):
            if not self.data_manager.is_grid_evaluated(tif_rel_path, grid['grid_index'], grid.get('annotation_key')):
                return i
        return 0
            
    def on_grid_changed(self, item: QListWidgetItem, initial_component_idx: int = 0):
        """Processes and structuralizes layout maps instantaneously with optimized web frames."""
        idx = self.grid_list_widget.row(item)
        if idx < 0: return
        self.active_grid_idx = idx
        
        scene_data = self.scene_database[self.active_scene_key]
        grid_data = scene_data['grid_cells'][idx]
        
        tif_rel_path = get_stable_scene_tif_path(scene_data['tif_path'])
        
        self._is_populating_ui = True
        self._pending_zoom_offset_apply = True
        
        m = folium.Map(
            location=grid_data["center"], 
            zoom_start=15, 
            max_zoom=24,
            control_scale=True,
            doubleClickZoom=False
        )
        folium.TileLayer('OpenStreetMap', max_zoom=24, max_native_zoom=19).add_to(m)
        m.add_child(LeafletPaneSetup([
            {"name": "contextPatches", "zIndex": 200},
            {"name": "minePolygons", "zIndex": 250},
            {"name": "activePatches", "zIndex": 300},
            {"name": "labelOutlines", "zIndex": 450},
        ]))

        def add_patch_overlay(patch: dict, opacity: float, pane: str, interactive: bool = True):
            img_url = QUrl.fromLocalFile(patch["jpg_path"]).toString()
            folium.raster_layers.ImageOverlay(
                image=img_url,
                bounds=patch["bounds"],
                opacity=opacity,
                interactive=interactive,
                cross_origin=False,
                pane=pane
            ).add_to(m)

        context_grids = self._context_neighbor_grids(scene_data["grid_cells"], grid_data) if self.context_bins_enabled else []
        for context_grid in context_grids:
            for patch in context_grid["patches"]:
                add_patch_overlay(patch, opacity=0.34, pane="contextPatches", interactive=False)
        for patch in grid_data["patches"]:
            add_patch_overlay(patch, opacity=1.0, pane="activePatches")
        
        b = grid_data["bounds"]
        folium.Rectangle(
            bounds=[[b[0][0], b[0][1]], [b[1][0], b[1][1]]],
            color='#D32F2F', weight=5, fill=True, fill_color='#D32F2F',
            fill_opacity=0.04, pane="labelOutlines"
        ).add_to(m)

        if self.mine_polygons_enabled and grid_data.get("nature_polygons"):
            folium.GeoJson(
                {"type": "FeatureCollection", "features": grid_data["nature_polygons"]},
                name="Nature mine polygons",
                style_function=lambda _feature: {
                    "color": "#C026D3",
                    "weight": 3,
                    "fillColor": "#D946EF",
                    "fillOpacity": 0.22,
                },
                pane="minePolygons",
                tooltip=folium.GeoJsonTooltip(
                    fields=["Mine_Name", "Mining_Category", "Primary_Commodity"],
                    aliases=["Mine", "Category", "Commodity"],
                    sticky=False,
                ),
            ).add_to(m)
        
        fast_grid_layer = FastBackgroundGrid(scene_data['grid_cells'], grid_data["grid_index"])
        m.add_child(fast_grid_layer)
        for context_grid in context_grids:
            cb = context_grid["bounds"]
            folium.Rectangle(
                bounds=[[cb[0][0], cb[0][1]], [cb[1][0], cb[1][1]]],
                color='#F59E0B', weight=4, fill=True, fill_color='#F59E0B',
                fill_opacity=0.08, opacity=1.0, pane="labelOutlines"
            ).add_to(m)
        self._add_descriptor_overlay(m)
        m.add_child(MapHudOverlay(self._map_hud_html()))
        
        target_bounds = [[b[0][0], b[0][1]], [b[1][0], b[1][1]]]
        m.add_child(FitBoundsOnce(target_bounds, padding_px=0))

        data = io.BytesIO()
        m.save(data, close_file=False)
        self.web_view.setHtml(data.getvalue().decode('utf-8'), get_map_base_url())
        data.close()

        current_rec = self.data_manager.get_grid_record(tif_rel_path, grid_data["grid_index"], grid_data.get("annotation_key"))
        
        for k, cb in self.tax_spinboxes.items():
            cb.blockSignals(True)
            if current_rec:
                cb.setChecked(bool(int(current_rec.get(k, 0))))
            else:
                cb.setChecked(False)
            cb.blockSignals(False)
        quality_flag = int(current_rec.get(QUALITY_FLAG_KEY, 0)) if current_rec else 0
        quality_flag = 1 if quality_flag else 0
        self.quality_flag_checkbox.blockSignals(True)
        self.quality_flag_checkbox.setChecked(bool(quality_flag))
        self.quality_flag_checkbox.blockSignals(False)
        self._last_quality_flag = quality_flag
                
        self._is_populating_ui = False
        self._reset_taxonomy_cursor()
        self._set_active_component_index(initial_component_idx, focus=True)
        icmm_category = grid_data.get("mining_category", "Unknown")
        self.lbl_status.setText(
            f"Active Grid: {grid_data['grid_index']} | ICMM: {icmm_category} ({grid_data['mine_point_count']} hits)"
        )
        self._update_map_hud_dom()

    def _auto_save_event_trigger(self, value: int):
        self._auto_save_current_grid()

    def _navigate_prev_grid(self):
        """Navigates to the mathematically preceding grid cell item."""
        if not self.active_scene_key or self.active_grid_idx <= 0: return
        self.grid_list_widget.setCurrentRow(self.active_grid_idx - 1)
        prev_item = self.grid_list_widget.currentItem()
        if prev_item:
            last_component_idx = len(self.core_keys) - 1
            self.on_grid_changed(prev_item, initial_component_idx=last_component_idx)

    def _navigate_next_grid(self):
        """Navigates to the mathematically succeeding grid cell item."""
        if not self.active_scene_key or self.active_grid_idx == -1: return
        grid_len = len(self.scene_database[self.active_scene_key]['grid_cells'])
        if self.active_grid_idx < grid_len - 1:
            self.grid_list_widget.setCurrentRow(self.active_grid_idx + 1)
            next_item = self.grid_list_widget.currentItem()
            if next_item:
                self.on_grid_changed(next_item, initial_component_idx=0)

    def _navigate_prev_matrix_position(self, initial_component_idx: int = None):
        if initial_component_idx is None:
            initial_component_idx = len(self.core_keys) - 1
        if self.active_grid_idx > 0:
            self.grid_list_widget.setCurrentRow(self.active_grid_idx - 1)
            prev_item = self.grid_list_widget.currentItem()
            if prev_item:
                self.on_grid_changed(prev_item, initial_component_idx=initial_component_idx)
            return

        current_file_row = self.file_list_widget.currentRow()
        if current_file_row <= 0:
            return

        self.file_list_widget.setCurrentRow(current_file_row - 1)
        prev_item = self.file_list_widget.currentItem()
        prev_scene_key = normalize_spatial_path(prev_item.data(Qt.ItemDataRole.UserRole))
        self._pending_grid_selection = (prev_scene_key, "last", initial_component_idx)
        self.on_scene_changed(self.file_list_widget.currentItem())
        prev_scene = self.scene_database.get(prev_scene_key)
        if prev_scene and prev_scene.get('grid_cells'):
            last_idx = len(prev_scene['grid_cells']) - 1
            self._pending_grid_selection = None
            self.grid_list_widget.setCurrentRow(last_idx)
            prev_item = self.grid_list_widget.currentItem()
            if prev_item:
                self.on_grid_changed(prev_item, initial_component_idx=initial_component_idx)

    def _navigate_next_matrix_position(self, grid_len: int):
        if self.active_grid_idx < grid_len - 1:
            self.grid_list_widget.setCurrentRow(self.active_grid_idx + 1)
            self.on_grid_changed(self.grid_list_widget.currentItem())
            return

        current_file_row = self.file_list_widget.currentRow()
        if current_file_row < self.file_list_widget.count() - 1:
            self.file_list_widget.setCurrentRow(current_file_row + 1)
            self.on_scene_changed(self.file_list_widget.currentItem())
        else:
            QMessageBox.information(self, "Project Pipeline Complete", "All Native Matrices successfully categorized.")

    def keyPressEvent(self, event: QKeyEvent):
        if not self.active_scene_key or self.active_grid_idx == -1:
            super().keyPressEvent(event)
            return

        if event.key() == Qt.Key.Key_Left:
            self.previous_component_or_bin()
            return
        elif event.key() == Qt.Key.Key_Right:
            self.next_component_or_bin()

        super().keyPressEvent(event)

    def closeEvent(self, event):
        self._flush_dirty_gpkg_exports()
        super().closeEvent(event)


if __name__ == "__main__":
    sys.argv.append("--enable-gpu-rasterization")
    sys.argv.append("--ignore-gpu-blocklist")
    sys.argv.append("--enable-zero-copy")
    
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    ui = MiningTaxonomyUI()
    ui.show()
    sys.exit(app.exec())
