from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from app.portal_qr_match import PortalQrMatchError, ensure_qr_match_helper, match_qr_image


class PortalQrMatchTests(unittest.TestCase):
    def test_invalid_geometry_or_helper_output_is_rejected(self):
        cases = ["not json", '{"matches":null}', '{"matches":[{}]}',
                 '{"matches":[{"x":NaN,"y":0,"width":0.2,"height":0.2}]}',
                 '{"matches":[{"x":0.9,"y":0,"width":0.2,"height":0.2}]}']
        for value in cases:
            with self.subTest(value=value), patch("app.portal_qr_match.subprocess.run", return_value=Mock(stdout=value)):
                with self.assertRaises(PortalQrMatchError):
                    match_qr_image(Path("helper"), Path("source"), Path("screenshot"))

    def test_helper_error_does_not_expose_stderr(self):
        error = subprocess.CalledProcessError(1, ["helper"], stderr="private login challenge")
        with patch("app.portal_qr_match.subprocess.run", side_effect=error):
            with self.assertRaises(PortalQrMatchError) as caught:
                match_qr_image(Path("helper"), Path("source"), Path("screenshot"))
        self.assertNotIn("private login", str(caught.exception))


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("clang"), "requires macOS Vision and compiler")
class PortalQrMatchNativeTests(unittest.TestCase):
    """Only synthetic local images. No apps, screen capture, network or user Photos."""
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.helper = ensure_qr_match_helper(Path(cls.directory.name))
        cls.source = Path(__file__).with_name("fixtures") / "portal-control-qr.png"
        cls.blank = Path(cls.directory.name) / "blank.png"
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        cls.blank.write_bytes(b"\x89PNG\r\n\x1a\n" +
                             chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 0, 0, 0, 0)) +
                             chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * 64) * 64)) +
                             chunk(b"IEND", b""))

    def test_matches_test_qr_and_returns_geometry_without_payload(self):
        completed = subprocess.run([str(self.helper), str(self.source), str(self.source)],
                                   check=True, capture_output=True, text=True, timeout=5)
        result = json.loads(completed.stdout)
        self.assertEqual(1, len(result["matches"]))
        self.assertNotIn("TPUI-", completed.stdout + completed.stderr)
        self.assertEqual({"x", "y", "width", "height"}, set(result["matches"][0]))
        matches = match_qr_image(self.helper, self.source, self.source)
        self.assertEqual(1, len(matches))

    def test_no_matching_qr_returns_empty_list(self):
        self.assertEqual([], match_qr_image(self.helper, self.source, self.blank))

    def test_no_quiet_zone_thumbnail_keeps_correct_geometry(self):
        # The synthetic fixture has a four-module (48 px) white border on each side.
        width, height = struct.unpack(">II", self.source.read_bytes()[16:24])
        cropped = Path(self.directory.name) / "no-quiet-zone.png"
        subprocess.run(["/usr/bin/sips", "-c", str(height - 96), str(width - 96),
                        str(self.source), "--out", str(cropped)], check=True, capture_output=True, timeout=5)
        for size in (120, 156):
            scaled = Path(self.directory.name) / f"small-{size}.png"
            subprocess.run(["/usr/bin/sips", "-Z", str(size), str(cropped), "--out", str(scaled)],
                           check=True, capture_output=True, timeout=5)
            matches = match_qr_image(self.helper, self.source, scaled)
            self.assertEqual(1, len(matches))
            box = matches[0]
            self.assertAlmostEqual(0.5, box["x"] + box["width"] / 2, delta=0.05)
            self.assertAlmostEqual(0.5, box["y"] + box["height"] / 2, delta=0.05)

    def test_unreadable_reference_qr_fails(self):
        with self.assertRaises(PortalQrMatchError):
            match_qr_image(self.helper, self.blank, self.source)
