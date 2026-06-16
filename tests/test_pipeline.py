from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fingerprint_pipeline import PipelineConfig, run_pipeline


def _result():
    return run_pipeline(ROOT / "images/input/1.jpg", PipelineConfig())


def test_shapes_and_binary_skeleton():
    result = _result()
    assert result.original.shape == result.enhanced.shape
    assert result.skeleton.shape == result.original.shape
    assert set(np.unique(result.skeleton)).issubset({0, 255})
    assert np.count_nonzero(result.skeleton) > 1000


def test_single_image_regression_counts_are_nonempty_and_bounded():
    result = _result()
    # These are guardrails, not biometric accuracy assertions.
    assert 10 <= len(result.minutiae) <= 100
    assert sum(m.kind == "ending" for m in result.minutiae) >= 1
    assert sum(m.kind == "bifurcation" for m in result.minutiae) >= 1
    assert 1 <= len(result.singular_points) <= 6
    assert any(p.kind == "core" for p in result.singular_points)
    assert any(p.kind == "delta" for p in result.singular_points)


def test_branch_cardinality_and_bounds():
    result = _result()
    h, w = result.original.shape
    for m in result.minutiae:
        assert 0 <= m.x < w and 0 <= m.y < h
        assert len(m.branch_angles_rad) == (1 if m.kind == "ending" else 3)
        assert len(m.branch_angles_rad) == len(m.branch_lengths_px)
        assert 0.0 <= m.quality_proxy <= 1.0
