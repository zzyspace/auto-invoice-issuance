from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


class PortalQrMatchError(RuntimeError):
    pass


def ensure_qr_match_helper(cache_dir: Path | None = None) -> Path:
    source = Path(__file__).with_suffix(".m")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    cache_dir = cache_dir or source.resolve().parents[1] / "data" / "tax-portal-tools"
    helper = cache_dir / f"qr-match-{digest}"
    if helper.is_file():
        return helper
    cache_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".qr-match-", dir=cache_dir) as temporary:
        compiled = Path(temporary) / "qr-match"
        try:
            subprocess.run(
                ["/usr/bin/clang", "-fobjc-arc", "-framework", "Foundation", "-framework", "Vision",
                 "-framework", "CoreGraphics", "-framework", "ImageIO", str(source), "-o", str(compiled)],
                check=True, capture_output=True, timeout=60,
            )
            compiled.replace(helper)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PortalQrMatchError("Unable to build the local QR matching helper.") from exc
    return helper


def match_qr_image(helper: Path, source: Path, screenshot: Path) -> list[dict[str, float]]:
    try:
        completed = subprocess.run(
            [str(helper), str(source), str(screenshot)],
            check=True, capture_output=True, text=True, timeout=5,
        )
        matches = json.loads(completed.stdout)["matches"]
        if not isinstance(matches, list):
            raise ValueError("Invalid match list")
        for match in matches:
            values = [match[key] for key in ("x", "y", "width", "height")]
            if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
                raise ValueError("Invalid match geometry")
            x, y, width, height = values
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < width <= 1 and 0 < height <= 1
                    and x + width <= 1.001 and y + height <= 1.001):
                raise ValueError("Match geometry is outside the screenshot")
        return matches
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise PortalQrMatchError("Unable to match the recorded QR against the picker screenshot.") from exc
