def is_valid_candidate(bbox, cv_image, threshold=0.92):
    """
    Checks if the candidate bounding box occupies less than `threshold` fraction
    of the image dimensions.  Returns True if acceptable, else False.

    The threshold was raised from 0.80 to 0.92.  The old 0.80 was too aggressive
    for tight anchor-crop images (495×419 px) where the PLSS grid legitimately
    fills ~81% of the crop in each dimension.  0.92 still rejects contours that
    span essentially the entire image while accepting valid PLSS grids.

    On full-page images (1578×1220 px) a PLSS grid (≈400×345 px) takes up only
    ~25% in each dimension, so it is unaffected by this change.
    """
    H, W = cv_image.shape[:2]
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return False
    if w > threshold * W or h > threshold * H:
        return False
    return True
