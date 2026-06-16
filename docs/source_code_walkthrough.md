# Source-code walkthrough

This companion explains the canonical module in execution order. Line numbers refer to `src/fingerprint_pipeline.py` in release 1.0.0.

## Lines 1-97 - data model and configuration

- The module docstring defines the boundary: classical-vision research prototype, not a production AFIS and not an ISO encoder.
- `PipelineConfig` gathers every threshold in one immutable object. This replaces hidden notebook state.
- `Minutia` stores position, semantic type, one or three skeleton-derived branch directions, branch lengths, coherence, ROI distance, and a clearly named *quality proxy*.
- `SingularPoint` stores position, core/delta label, Poincare index, experimental arm cues, and coherence.
- `PipelineResult` keeps intermediate images so each transformation is auditable.

## Lines 99-159 - input and ROI

- `load_grayscale` fails early when the file is absent or not a 2-D image.
- `normalize_and_clahe` reproduces the useful opening notebook cells.
- `largest_component` removes isolated foreground islands.
- `compute_roi_mask` is intentionally tuned to the supplied white-background laboratory image. Dark pixels are joined morphologically, the largest contour is filled through its convex hull, and an eroded mask defines a safe interior for minutiae.

## Lines 160-230 - axial orientation field

- `_smooth_axial_field` smooths `cos(2 theta)` and `sin(2 theta)`, not `theta` directly. This handles the equivalence of ridge axes separated by pi.
- `estimate_orientation_fields` computes Sobel gradients and the local structure tensor (`Jxx`, `Jyy`, `Jxy`).
- The tensor's principal gradient axis is rotated by pi/2 to obtain the ridge tangent.
- A dense field supports local refinement; a 16-pixel block field supports visualization and the Poincare loop.
- Coherence is a reliability proxy derived from the tensor eigenvalue contrast.

## Lines 231-324 - ridge map and skeleton

- `binarize_and_skeletonize` uses global Otsu inversion because the single supplied image is already high contrast. The abandoned pixel-wise Gabor/adaptive path is not silently presented as superior.
- Small foreground objects are removed, then `skeletonize` produces a one-pixel topology.
- `prune_short_endpoint_branches` removes only short endpoint-to-junction thorns, preventing an aggressive cleanup from deleting legitimate ridge endings.

## Lines 325-566 - local graph geometry

- `trace_branch` follows the smoothest unvisited continuation on the 8-connected skeleton.
- A bifurcation is often a multi-pixel junction. `branch_descriptors_for_candidate_cluster` therefore collapses the entire candidate cluster and nearby zone before tracing connected arms.
- `_candidate_pixels` applies crossing number; `_cluster_candidates` merges neighbouring pixels of the same candidate type.
- `suppress_broken_ridge_endpoint_pairs` detects short, facing endpoint pairs that likely represent one broken ridge rather than two real endings.

## Lines 567-636 - conservative minutiae

Each consolidated candidate is accepted only when:

1. it lies inside the eroded ROI and far enough from the foreground boundary;
2. local coherence exceeds the threshold;
3. its reconstructed arm count equals one for an ending or three for a bifurcation;
4. all arms are sufficiently long;
5. it is not too close to a previously accepted point; and
6. it is not one side of a short facing endpoint pair.

The output is deliberately smaller than the legacy display. This is a *candidate-quality trade-off*, not a measured accuracy improvement, because no ground-truth minutiae are supplied.

## Lines 637-808 - cores and deltas

- `wrap_axial_difference` keeps orientation increments inside `[-pi/2, pi/2]`.
- `poincare_index_block` sums those increments around an eight-block loop.
- Candidate blocks are clustered; the strongest candidate is refined by evaluating dense Poincare loops in a pixel neighbourhood.
- `estimate_singularity_branches` looks for radial/tangent alignment peaks on a ring. Its arms help visualize topology, but they are not treated as validated core/delta directions.

## Lines 809-938 - orchestration, visualization, export

- `run_pipeline` is the single ordered entry point and merges diagnostics from all stages.
- `ensure_bgr` eliminates the notebook's repeated grayscale/BGR conversion failure.
- Drawing functions use separate symbols for endings, bifurcations, cores, and deltas.
- `save_result_tables` emits CSV and JSON with explicit field names, including radians and pixel units.
