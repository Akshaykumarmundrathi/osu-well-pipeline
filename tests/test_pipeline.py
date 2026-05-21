"""
Comprehensive pipeline test suite.

Covers every stage with synthetic inputs designed to exercise:
  - Happy paths
  - Missing / partial data
  - OCR noise and edge cases
  - All PDF tiers (early / transition / mid / late / modern)
  - Security: env-var credential loading
  - Connection lifecycle: open / close verified

Run:
    cd D:/project_modular
    pytest tests/test_pipeline.py -v
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup so project modules are importable
# ---------------------------------------------------------------------------
_PROJECT = Path(__file__).resolve().parents[1] / "project"
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

_DETECTOR = Path(__file__).resolve().parents[1] / "well_dot_detector"
if str(_DETECTOR) not in sys.path:
    sys.path.insert(0, str(_DETECTOR))


# ===========================================================================
# 1. Config: env-var driven paths
# ===========================================================================

class TestConfig:
    def test_output_root_uses_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path))
        import importlib, config
        importlib.reload(config)
        assert config.OUTPUT_ROOT == tmp_path

    def test_output_root_has_sensible_default(self, monkeypatch):
        monkeypatch.delenv("OUTPUT_ROOT", raising=False)
        import importlib, config
        importlib.reload(config)
        assert config.OUTPUT_ROOT != Path("")

    def test_tier_for_boundaries(self):
        from config import tier_for, TIER_EARLY, TIER_MODERN, TIER_LATE
        assert tier_for(1)  == TIER_EARLY
        assert tier_for(6)  == TIER_EARLY
        assert tier_for(13) == TIER_MODERN
        assert tier_for(11) == TIER_LATE
        assert tier_for(None) == TIER_EARLY
        assert tier_for(0) == TIER_EARLY

    def test_decade_for(self):
        from config import decade_for
        assert decade_for("1923") == "1920s"
        assert decade_for("2005") == "2000s"
        assert decade_for(None)  == ""
        assert decade_for("bad") == ""


# ===========================================================================
# 2. PLSS resolver: credentials loaded from env, not hardcoded
# ===========================================================================

class TestPLSSResolverSecurity:
    def test_no_hardcoded_credentials_in_source(self):
        """Ensure the source file does not contain the old plaintext password."""
        src = (_PROJECT / "coord" / "plss_resolver.py").read_text(encoding="utf-8")
        assert "Geology#OSU" not in src
        assert "LookUpMaster" not in src

    def test_connect_raises_without_env(self, monkeypatch):
        for var in ("RDS_HOST", "RDS_DBNAME", "RDS_USER", "RDS_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        import importlib
        import coord.plss_resolver as pr
        importlib.reload(pr)
        resolver = pr.PLSSResolver()
        with pytest.raises(EnvironmentError, match="RDS_HOST"):
            resolver._connect()

    def test_connect_reads_env(self, monkeypatch):
        monkeypatch.setenv("RDS_HOST",     "test-host")
        monkeypatch.setenv("RDS_DBNAME",   "testdb")
        monkeypatch.setenv("RDS_USER",     "testuser")
        monkeypatch.setenv("RDS_PASSWORD", "testpass")
        monkeypatch.setenv("RDS_PORT",     "5432")

        with patch("psycopg2.connect") as mock_conn:
            mock_conn.return_value = MagicMock()
            mock_conn.return_value.closed = False
            mock_conn.return_value.set_session = MagicMock()
            import coord.plss_resolver as pr
            resolver = pr.PLSSResolver()
            resolver._connect()
            call_kwargs = mock_conn.call_args[1]
            assert call_kwargs["host"]     == "test-host"
            assert call_kwargs["dbname"]   == "testdb"
            assert call_kwargs["user"]     == "testuser"
            assert call_kwargs["password"] == "testpass"

    def test_close_is_idempotent(self):
        from coord.plss_resolver import PLSSResolver
        r = PLSSResolver()
        r.close()   # called before connect — must not raise
        r.close()   # second call — must not raise


# ===========================================================================
# 3. PLSS math: label ↔ (row, col) round-trip
# ===========================================================================

class TestQuadrantExtractor:
    @pytest.mark.parametrize("db_label,expected_row,expected_col", [
        ("NW-NW-NW", 1, 1),
        ("NW-NW-NE", 1, 2),
        ("SE-SE-SE", 8, 8),
        ("NE-SW-NW", 3, 5),
        ("SW-NE-SE", 6, 4),
    ])
    def test_label_to_row_col(self, db_label, expected_row, expected_col):
        from ocr.quadrant_extractor import label_to_row_col
        row, col = label_to_row_col(db_label)
        assert row == expected_row, f"{db_label}: got row={row}, want {expected_row}"
        assert col == expected_col, f"{db_label}: got col={col}, want {expected_col}"

    def test_round_trip(self):
        from ocr.quadrant_extractor import label_to_row_col, row_col_to_label
        for r in range(1, 9):
            for c in range(1, 9):
                label = row_col_to_label(r, c)
                r2, c2 = label_to_row_col(label)
                assert (r2, c2) == (r, c), f"round-trip fail at ({r},{c}): label={label}"

    def test_invalid_label(self):
        from ocr.quadrant_extractor import label_to_row_col
        assert label_to_row_col("XX-YY-ZZ") == (None, None)
        assert label_to_row_col("NW-NW") == (None, None)    # only 2 parts
        assert label_to_row_col("") == (None, None)

    @pytest.mark.parametrize("text,expected_db", [
        # Standard 3-level (PDF fine→coarse → DB coarse→fine reversed)
        ("NW SW NE",     "NE-SW-NW"),
        ("NW-SW-NE",     "NE-SW-NW"),
        ("NE/SW/NW",     "NW-SW-NE"),
        ("SW NE NW",     "NW-NE-SW"),
        # OCR noise variants
        ("NW  SW  NE",   "NE-SW-NW"),
    ])
    def test_extract_quadrant_from_text(self, text, expected_db):
        from ocr.quadrant_extractor import extract_quadrant
        result = extract_quadrant(text)
        assert result is not None, f"extract_quadrant returned None for: {text!r}"
        assert result["db_label"] == expected_db

    def test_extract_quadrant_returns_none_when_absent(self):
        from ocr.quadrant_extractor import extract_quadrant
        assert extract_quadrant("some random text without any direction codes") is None
        assert extract_quadrant("") is None
        assert extract_quadrant(None) is None


# ===========================================================================
# 4. Location header (Form 1002A) parser
# ===========================================================================

class TestLocationHeader:
    _FORM_1002A = """\
Oklahoma Corporation Commission
Location:
ALFALFA 36 28N 11W
C SW SW
660 FSL 660 FWL of 1/4 SEC
Latitude: 36.8569153 Longitude: -98.339600497
"""
    _LOCATE_WELL = """\
LOCATE WELL
SEC 14 T5N R9W
NW NE NW
330 FNL 330 FEL of 1/4 SEC
"""

    def test_form_1002a_basic(self):
        from latlong.location_header import parse_location_block
        r = parse_location_block(self._FORM_1002A)
        assert r["found"]
        assert r["form_type"] == "form_1002a"
        assert r["county"].lower() == "alfalfa"
        assert r["section"] == "36"
        assert r["township"] == "28N"
        assert r["range"] == "11W"
        assert r["feet_fsl"] == 660
        assert r["feet_fwl"] == 660
        assert r["feet_ref"] == "1/4 SEC"
        assert r["rel_x_from_feet"] is not None

    def test_locate_well_variant(self):
        from latlong.location_header import parse_location_block
        r = parse_location_block(self._LOCATE_WELL)
        assert r["found"]
        assert r["form_type"] == "locate_well"
        assert r["section"] == "14"
        assert r["township"] == "5N"
        assert r["range"] == "9W"

    def test_empty_text(self):
        from latlong.location_header import parse_location_block
        r = parse_location_block("")
        assert not r["found"]

    def test_no_trigger(self):
        from latlong.location_header import parse_location_block
        r = parse_location_block("Well name: SMITH 1\nAPI: 35-001-12345")
        assert not r["found"]

    @pytest.mark.parametrize("fsl,fnl,fwl,fel,ref,exp_x,exp_y", [
        (660,  None, 660,  None, "1/4 SEC", 660/2640,    1 - 660/2640),
        (None, 330,  None, 330,  "1/4 SEC", 1 - 330/2640, 330/2640),
        (2640, None, None, None, "SEC",     None,         1 - 2640/5280),
    ])
    def test_feet_to_rel(self, fsl, fnl, fwl, fel, ref, exp_x, exp_y):
        from latlong.location_header import feet_to_rel
        rx, ry = feet_to_rel(fsl, fnl, fwl, fel, ref)
        if exp_x is None:
            assert rx is None
        else:
            assert abs(rx - exp_x) < 1e-9
        if exp_y is None:
            assert ry is None
        else:
            assert abs(ry - exp_y) < 1e-9

    def test_quadrant_center_two_level(self):
        from latlong.location_header import parse_quadrant_notation
        r = parse_quadrant_notation("C SW SW")
        assert r["is_center"]
        assert r["quadrant_type"] in ("center_two_level", "two_level")

    def test_quadrant_three_level(self):
        from latlong.location_header import parse_quadrant_notation
        r = parse_quadrant_notation("NW NE NW")
        assert r["quadrant_type"] == "three_level"
        assert r["quadrant_row"] is not None


# ===========================================================================
# 5. Lat/Lon extractor — all extraction methods
# ===========================================================================

class TestLatLonExtractor:
    def _make_manager(self, text: str):
        """Fake PDFDocumentManager that yields a single page with given text."""
        m = MagicMock()
        m._ocr_cache = {}
        annotation = MagicMock()
        annotation.description = text
        page_img = MagicMock()
        m.iter_pil_pages.return_value = iter([(1, page_img)])

        with patch("latlong.latlong_extractor.detect_text_with_vision",
                   return_value=[annotation]):
            from latlong.latlong_extractor import process_single_latlong
            return process_single_latlong(m, "TEST_12345_WELL_1_001")

    def test_labeled_decimal_detected(self):
        r = self._make_manager(
            "Latitude: 36.856915  Longitude: -98.339600\n"
            "Location:\nALFALFA 36 28N 11W\n"
        )
        assert r["detected"]
        assert abs(float(r["lat"]) - 36.856915) < 1e-5
        assert r["latlong_method"] == "labeled_decimal"
        assert r["confidence"] >= 95

    def test_dms_detected(self):
        r = self._make_manager(
            "36° 51' 24.89\" N   98° 20' 22.56\" W\n"
        )
        assert r["detected"]
        assert r["latlong_method"] == "dms"

    def test_unlabeled_decimal_detected(self):
        r = self._make_manager(
            "Well location: 36.123456 and -97.654321\n"
        )
        assert r["detected"]
        assert r["latlong_method"] == "unlabeled_decimal"

    def test_no_coordinates(self):
        r = self._make_manager(
            "API Number: 35-001-12345\nWell Name: SMITH 1\n"
        )
        assert not r["detected"]
        assert r["lat"] == ""
        assert r["lon"] == ""

    def test_invalid_lat_rejected(self):
        r = self._make_manager(
            "Latitude: 99.123456  Longitude: -98.339600\n"
        )
        # 99° lat is invalid — should fall through to DMS/unlabeled or not detected
        # Either way the labeled path must not return 99 as a lat
        if r["detected"]:
            assert abs(float(r["lat"])) <= 90.0

    def test_form_1002a_confidence_boost(self):
        r = self._make_manager(
            "Location:\nALFALFA 36 28N 11W\nC SW SW\n"
            "Latitude: 36.856915  Longitude: -98.339600\n"
        )
        assert r["detected"]
        assert r["confidence"] >= 97

    def test_well_type_swd(self):
        from latlong.latlong_extractor import _well_type_from_name
        assert _well_type_from_name("35001_SMITH 1 SWD_001") == "water/disposal"

    def test_well_type_from_ocr(self):
        from latlong.latlong_extractor import _detect_well_type
        assert _detect_well_type("This is a gas well", "") == "gas"
        assert _detect_well_type("Saltwater disposal well", "") == "water/disposal"


# ===========================================================================
# 6. Processing status CSV: schema, mark_done, mark_coord_audit
# ===========================================================================

class TestProcessingStatus:
    def test_init_record_and_mark_done(self, tmp_path):
        from utils.processing_status import ProcessingStatus, DONE, PENDING
        ps = ProcessingStatus(tmp_path / "status.csv")
        ps.init_record("stem1", "path/to.pdf", collection="col1", year="2005", month="01")
        assert ps.get_status("stem1", "latlong") == PENDING

        ps.mark_done("stem1", "latlong", {
            "lat": "36.5", "lon": "-97.5", "confidence": 95,
            "latlong_method": "labeled_decimal",
        })
        assert ps.get_status("stem1", "latlong") == DONE

    def test_mark_failed_classifies_error(self, tmp_path):
        from utils.processing_status import ProcessingStatus, FAILED
        ps = ProcessingStatus(tmp_path / "status.csv")
        ps.init_record("stem2", "path.pdf")
        ps.mark_failed("stem2", "grid", "503 Service Unavailable")
        assert ps.get_status("stem2", "grid") == FAILED
        assert ps.get_error_type("stem2", "grid") == "api_error"

    def test_mark_coord_audit(self, tmp_path):
        from utils.processing_status import ProcessingStatus
        ps = ProcessingStatus(tmp_path / "status.csv")
        ps.init_record("stem3", "path.pdf")
        ps.mark_coord_audit("stem3",
            derivation="dot_interpolation",
            latlong_source="",
            section_source="header_block",
            township_source="header_block",
            range_source="ocr_extracted",
            county_used="Alfalfa",
            dot_source="unet")
        row = ps._rows["stem3"]
        assert row["coord_derivation"] == "dot_interpolation"
        assert row["coord_county_used"] == "Alfalfa"

    def test_force_save_and_reload(self, tmp_path):
        from utils.processing_status import ProcessingStatus, DONE
        ps = ProcessingStatus(tmp_path / "status.csv")
        ps.init_record("stem4", "path.pdf")
        ps.mark_done("stem4", "county", {"name": "Blaine", "confidence": 90, "fuzzy_score": 92})
        ps.force_save()

        ps2 = ProcessingStatus(tmp_path / "status.csv")
        assert ps2.get_status("stem4", "county") == DONE

    def test_latlong_detected(self, tmp_path):
        from utils.processing_status import ProcessingStatus
        ps = ProcessingStatus(tmp_path / "status.csv")
        ps.init_record("stem5", "path.pdf")
        ps.mark_done("stem5", "latlong", {"lat": "36.5", "lon": "-97.5", "confidence": 95})
        assert ps.latlong_detected("stem5")

    def test_latlong_not_detected_when_empty(self, tmp_path):
        from utils.processing_status import ProcessingStatus
        ps = ProcessingStatus(tmp_path / "status.csv")
        ps.init_record("stem6", "path.pdf")
        assert not ps.latlong_detected("stem6")


# ===========================================================================
# 7. PLSS math: geometry / bilinear interpolation
# ===========================================================================

class TestPLSSGeometry:
    def test_compute_cell_corners_first_cell(self):
        from coord.plss_resolver import compute_cell_corners
        # Section from (0,36) to (1,37) — first cell (row=1,col=1) should be NW corner
        corners = compute_cell_corners(0, 36, 1, 37, row=1, col=1)
        assert corners["tl"] == (0.0, 37.0)   # NW
        assert corners["br"] == pytest.approx((0.125, 36.875), abs=1e-9)

    def test_cell_corners_from_bbox(self):
        from coord.plss_resolver import cell_corners_from_bbox
        c = cell_corners_from_bbox(0.1, 36.1, 0.2, 36.2)
        assert c["tl"] == (0.1, 36.2)   # NW
        assert c["br"] == (0.2, 36.1)   # SE

    def test_bilinear_interpolate_center(self):
        from coord.plss_resolver import bilinear_interpolate, cell_corners_from_bbox
        corners = cell_corners_from_bbox(-98.0, 36.0, -97.9, 36.1)
        lat, lon = bilinear_interpolate(corners, 0.5, 0.5)
        assert abs(lat - 36.05) < 1e-9
        assert abs(lon - (-97.95)) < 1e-9

    def test_dot_relative_clamp(self):
        from coord.plss_resolver import dot_relative_in_cell
        # x_norm=0.63, row=3, col=5 → rel_x = 0.63*8-4 = 1.04 → clamped to 0.5
        rx, ry = dot_relative_in_cell(0.63, 0.5, row=3, col=5)
        assert rx == 0.5   # clamped (out of range)

        rx2, ry2 = dot_relative_in_cell(0.5, 0.5, row=3, col=3)
        assert 0.0 <= rx2 <= 1.0

    def test_validate_oklahoma_plss_valid(self):
        from coord.plss_resolver import validate_oklahoma_plss
        ok, reason = validate_oklahoma_plss(14, 5, "N", 9, "W")
        assert ok

    def test_validate_oklahoma_plss_bad_section(self):
        from coord.plss_resolver import validate_oklahoma_plss
        ok, reason = validate_oklahoma_plss(37, 5, "N", 9, "W")
        assert not ok
        assert "section" in reason

    def test_validate_oklahoma_plss_s99_garbage(self):
        from coord.plss_resolver import validate_oklahoma_plss
        ok, reason = validate_oklahoma_plss(99, 99, "N", 99, "W")
        assert not ok


# ===========================================================================
# 8. County constraints: build / load / infer
# ===========================================================================

class TestCountyConstraints:
    _SAMPLE = {
        "Alfalfa": {
            "min_twp": 25, "max_twp": 29, "ns_values": ["N"],
            "min_rng": 8,  "max_rng": 12, "ew_values": ["W"],
            "n_sections": 50,
        },
        "Texas": {
            "min_twp": 1, "max_twp": 5, "ns_values": ["N"],
            "min_rng": 14, "max_rng": 24, "ew_values": ["E", "W"],
            "n_sections": 120,
        },
    }

    def test_save_and_load(self, tmp_path):
        from coord.county_constraints import _save, load
        _save(tmp_path, self._SAMPLE)
        loaded = load(tmp_path)
        assert loaded == self._SAMPLE

    def test_infer_ns_from_county(self):
        from coord.county_constraints import infer_directions
        ns, ew = infer_directions(self._SAMPLE, "Alfalfa", 27, 10, None, None)
        assert ns == ["N"]
        assert ew == ["W"]

    def test_infer_fallback_for_unknown_county(self):
        from coord.county_constraints import infer_directions
        ns, ew = infer_directions(self._SAMPLE, "Unknown County", None, None, None, None)
        assert "N" in ns
        assert "W" in ew

    def test_find_county_case_insensitive(self):
        from coord.county_constraints import _find_county
        assert _find_county(self._SAMPLE, "alfalfa") is not None
        assert _find_county(self._SAMPLE, "ALFALFA COUNTY") is not None
        assert _find_county(self._SAMPLE, "Alfalfa County") is not None

    def test_load_missing_file(self, tmp_path):
        from coord.county_constraints import load
        result = load(tmp_path / "nonexistent")
        assert result == {}


# ===========================================================================
# 9. Dot extractor: tier thresholds
# ===========================================================================

class TestDotExtractorTiers:
    def test_threshold_decreases_for_modern(self):
        from dot.dot_extractor import _threshold_for_tier
        early   = _threshold_for_tier("early")
        modern  = _threshold_for_tier("modern")
        assert early > modern

    def test_threshold_unknown_tier_returns_default(self):
        from dot.dot_extractor import _threshold_for_tier, _DEFAULT_THRESHOLD
        assert _threshold_for_tier("nonexistent_tier") == _DEFAULT_THRESHOLD

    def test_grid_image_not_found(self, tmp_path):
        from dot.dot_extractor import process_single_dot
        r = process_single_dot(tmp_path, tmp_path, "no_such_stem", MagicMock())
        assert not r["detected"]
        assert r["error"] == "grid_image_not_found"


# ===========================================================================
# 10. Security: no hardcoded credentials anywhere in the codebase
# ===========================================================================

class TestNoHardcodedSecrets:
    _BANNED = [
        "Geology#OSU",
        "LookUpMaster",
        "oklahomagridlatlongdb",
        "smiling-breaker",
        "BEGIN PRIVATE KEY",
    ]
    _SOURCE_ROOTS = [
        Path(__file__).resolve().parents[1] / "project",
        Path(__file__).resolve().parents[1] / "aws",
    ]

    @pytest.mark.parametrize("banned", _BANNED)
    def test_banned_string_not_in_source(self, banned):
        for root in self._SOURCE_ROOTS:
            for py_file in root.rglob("*.py"):
                content = py_file.read_text(encoding="utf-8", errors="replace")
                assert banned not in content, (
                    f"SECURITY: '{banned}' found in {py_file.relative_to(root.parent)}"
                )

    def test_no_json_credential_files_in_project(self):
        """GCP service account JSON must not be in the project directory."""
        project_root = Path(__file__).resolve().parents[1]
        # Check only files directly at project root (not aws/config/*.json which are templates)
        for f in project_root.glob("*.json"):
            content = f.read_text(encoding="utf-8", errors="replace")
            assert "private_key" not in content, (
                f"SECURITY: {f.name} appears to contain a private key"
            )


# ===========================================================================
# 11. S3 reader: parse_s3_uri
# ===========================================================================

class TestS3Reader:
    def test_parse_s3_uri_valid(self):
        from utils.s3_reader import parse_s3_uri
        bucket, key = parse_s3_uri("s3://my-bucket/path/to/file.zip")
        assert bucket == "my-bucket"
        assert key == "path/to/file.zip"

    def test_parse_s3_uri_invalid(self):
        from utils.s3_reader import parse_s3_uri
        with pytest.raises(ValueError):
            parse_s3_uri("https://not-s3/path")

    def test_parse_s3_uri_root_key(self):
        from utils.s3_reader import parse_s3_uri
        bucket, key = parse_s3_uri("s3://bucket/file.pdf")
        assert bucket == "bucket"
        assert key == "file.pdf"


# ===========================================================================
# 12. Evolutionary advisor
# ===========================================================================

class TestEvolutionary:
    def test_no_suggestions_with_insufficient_runs(self, tmp_path):
        from utils.evolutionary import learn_from_run
        suggestions = learn_from_run(tmp_path)
        assert suggestions == []

    def test_suggestions_from_run_history(self, tmp_path):
        import json
        history_file = tmp_path / "run_history.jsonl"
        run = {
            "counts": {
                "county": {"done": 2, "failed": 10, "pending": 0, "skipped": 0}
            },
            "failure_breakdown": [
                {"stage": "county", "error_type": "no_match", "tier": "early", "count": 8},
            ],
        }
        with history_file.open("w") as f:
            for _ in range(5):   # 5 identical runs → triggers threshold check
                f.write(json.dumps(run) + "\n")

        from utils.evolutionary import learn_from_run
        suggestions = learn_from_run(tmp_path)
        assert isinstance(suggestions, list)
        # Should suggest tuning FUZZY_MATCH_THRESHOLD
        params = [s["parameter"] for s in suggestions]
        assert any("FUZZY" in p for p in params)
