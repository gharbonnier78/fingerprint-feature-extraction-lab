# Canonical execution order

The original notebook is not safe to run top-to-bottom. The reconstructed order is:

1. **Environment and imports** - fixed dependencies, no mid-notebook `pip uninstall`/`pip install`.
2. **Input loading** - read `images/input/1.jpg` in grayscale and assert a non-empty 2-D array.
3. **Photometric preprocessing** - min-max normalization followed by CLAHE (`clipLimit=2.0`, `tileGridSize=(8,8)`).
4. **Foreground segmentation** - threshold the white background, close/dilate ridge fragments, keep the largest component, fill its convex hull, and erode a conservative interior mask.
5. **Orientation and coherence** - Sobel gradients, local structure tensor, ridge tangent `theta = 0.5 atan2(2Jxy,Jxx-Jyy) + pi/2`, and double-angle smoothing.
6. **Ridge binarization** - Otsu inverse threshold inside the ROI; remove tiny connected objects.
7. **Skeletonization** - one-pixel skeleton with `skimage.morphology.skeletonize`; prune only very short endpoint-to-junction spurs.
8. **Raw minutia candidates** - Rutovitz crossing number on the 8-neighbour ring (`CN=1` ending, `CN=3` bifurcation).
9. **Candidate consolidation** - cluster adjacent candidate pixels because a real junction often occupies several skeleton pixels.
10. **Branch reconstruction** - collapse the local junction zone, trace each connected arm, and derive directed branch angles from the skeleton rather than assigning all directions from a block field.
11. **Quality gates** - conservative interior distance, orientation coherence, exact branch count, branch length, inter-candidate separation, and broken-ridge endpoint-pair suppression.
12. **Core/delta candidates** - Poincare index on the block orientation field, spatial clustering, then dense local search for a higher-resolution point.
13. **Singularity arm visualization** - experimental ring-alignment peaks; two cues for each core and three for each delta. These are visualization cues, not validated ISO directions.
14. **Export** - PNG stage images, CSV tables, and JSON diagnostics. No ISO FMR binary is emitted.

## Blocks that must remain outside the default run

- The legacy ISO/IEC 19794-2 writer/parser cells (roughly 118-260).
- Any claim that passing the notebook's own parser proves conformance.
- Manual expert-reference comparison unless the missing annotation files are supplied.
- Cells that mutate OpenCV packages during execution.
- Alternative “thinning” cells that are only erosion/contour operations rather than a verified skeletonization algorithm.
- Google Drive copy/list boilerplate.

## Function-level map

| Ordered stage | Canonical function | Legacy origin |
|---|---|---|
| Load | `load_grayscale` | 0-3 |
| Normalize/CLAHE | `normalize_and_clahe` | 4-7 |
| ROI | `compute_roi_mask` | 8-20 and 21-26 |
| Orientation/coherence | `estimate_orientation_fields` | 8-20, 440-468 |
| Binary/skeleton | `binarize_and_skeletonize` | 21-26, 440-477 |
| CN candidates | `_candidate_pixels` | 21-26, 35-42 |
| Branch geometry | `branch_descriptors_for_candidate_cluster` | 261-289 plus reconstruction |
| Minutia filtering | `extract_and_filter_minutiae` | 21-26, 261-289 |
| Poincare | `detect_singular_points` | 27-42 |
| Draw/export | `draw_*`, `save_result_tables` | scattered visualization/export cells |
