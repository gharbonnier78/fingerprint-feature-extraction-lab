from pathlib import Path
import json
import cv2
import matplotlib.pyplot as plt
import numpy as np

from fingerprint_pipeline import (
    PipelineConfig,
    draw_minutiae_branches,
    draw_orientation_field,
    draw_singular_points,
    run_pipeline,
    save_result_tables,
)

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
RESULTS.mkdir(exist_ok=True)

cfg = PipelineConfig()
res = run_pipeline(ROOT / "images/input/1.jpg", cfg)
save_result_tables(res, RESULTS)

# Save machine-readable intermediate rasters.
cv2.imwrite(str(RESULTS / "01_original.png"), res.original)
cv2.imwrite(str(RESULTS / "02_clahe.png"), res.enhanced)
cv2.imwrite(str(RESULTS / "03_roi_mask.png"), res.roi_mask)
cv2.imwrite(str(RESULTS / "04_inner_roi_mask.png"), res.inner_roi_mask)
cv2.imwrite(str(RESULTS / "05_binary_ridges.png"), res.binary_ridges)
cv2.imwrite(str(RESULTS / "06_skeleton.png"), 255 - res.skeleton)

orient = draw_orientation_field(res.enhanced, res.orientation_blocks, res.coherence_blocks, cfg)
min_overlay = draw_minutiae_branches(res.original, res.minutiae)
min_skel_overlay = draw_minutiae_branches(255 - res.skeleton, res.minutiae)
sing_overlay = draw_singular_points(res.original, res.singular_points)
combined = draw_singular_points(min_overlay, res.singular_points)
for name, image in [
    ("07_orientation_field.png", orient),
    ("08_minutiae_branches.png", min_overlay),
    ("09_minutiae_on_skeleton.png", min_skel_overlay),
    ("10_core_delta_branches.png", sing_overlay),
    ("11_combined_features.png", combined),
]:
    cv2.imwrite(str(RESULTS / name), image)

# Figure 1: ordered pipeline.
fig, axes = plt.subplots(2, 3, figsize=(11, 8))
items = [
    (res.original, "(a) Input 1.jpg", "gray"),
    (res.enhanced, "(b) CLAHE enhancement", "gray"),
    (res.roi_mask, "(c) Foreground ROI", "gray"),
    (res.binary_ridges, "(d) Binary ridge map", "gray"),
    (255 - res.skeleton, "(e) One-pixel skeleton", "gray"),
    (cv2.cvtColor(combined, cv2.COLOR_BGR2RGB), "(f) Extracted features", None),
]
for ax, (image, title, cmap) in zip(axes.ravel(), items):
    ax.imshow(image, cmap=cmap)
    ax.set_title(title)
    ax.axis("off")
fig.tight_layout()
fig.savefig(RESULTS / "figure_pipeline_overview.png", dpi=220, bbox_inches="tight")
plt.close(fig)

# Figure 2: minutiae diagnostics.
fig, axes = plt.subplots(1, 3, figsize=(12, 5))
axes[0].imshow(255 - res.skeleton, cmap="gray")
axes[0].set_title("Skeleton after conservative pruning")
axes[1].imshow(cv2.cvtColor(min_skel_overlay, cv2.COLOR_BGR2RGB))
axes[1].set_title("Accepted endings/bifurcations + branches")
axes[2].imshow(cv2.cvtColor(min_overlay, cv2.COLOR_BGR2RGB))
axes[2].set_title("Accepted minutiae on input")
for ax in axes:
    ax.axis("off")
fig.suptitle(
    f"Minutiae: {len(res.minutiae)} accepted "
    f"({sum(m.kind=='ending' for m in res.minutiae)} endings, "
    f"{sum(m.kind=='bifurcation' for m in res.minutiae)} bifurcations)"
)
fig.tight_layout()
fig.savefig(RESULTS / "figure_minutiae_diagnostics.png", dpi=220, bbox_inches="tight")
plt.close(fig)

# Figure 3: orientation/singularity diagnostics.
fig, axes = plt.subplots(1, 3, figsize=(12, 5))
axes[0].imshow(cv2.cvtColor(orient, cv2.COLOR_BGR2RGB))
axes[0].set_title("Block orientation field")
axes[1].imshow(res.coherence_dense, cmap="viridis")
axes[1].set_title("Dense coherence proxy")
axes[2].imshow(cv2.cvtColor(sing_overlay, cv2.COLOR_BGR2RGB))
axes[2].set_title("Core/delta candidates + experimental arms")
for ax in axes:
    ax.axis("off")
fig.tight_layout()
fig.savefig(RESULTS / "figure_singularity_diagnostics.png", dpi=220, bbox_inches="tight")
plt.close(fig)

# Figure 4: legacy vs reconstructed outputs.
legacy = cv2.imread(str(ROOT / "images/legacy/legacy_final_overlay.png"))
fig, axes = plt.subplots(1, 2, figsize=(8, 6))
axes[0].imshow(cv2.cvtColor(legacy, cv2.COLOR_BGR2RGB))
axes[0].set_title("Legacy final pasted output")
axes[1].imshow(cv2.cvtColor(combined, cv2.COLOR_BGR2RGB))
axes[1].set_title("Ordered reconstruction")
for ax in axes:
    ax.axis("off")
fig.tight_layout()
fig.savefig(RESULTS / "figure_legacy_vs_reconstructed.png", dpi=220, bbox_inches="tight")
plt.close(fig)

print(json.dumps(res.diagnostics, indent=2))
print("Minutiae")
for m in res.minutiae:
    print(m)
print("Singular points")
for p in res.singular_points:
    print(p)
