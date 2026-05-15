def is_valid_candidate(bbox, cv_image, threshold=0.8):
    """
    Checks if the candidate bounding box occupies less than the specified fraction
    of the image dimensions. Returns True if acceptable, else False.
    """
    H, W = cv_image.shape[:2]
    x, y, w, h = bbox
    # Basic check for non-zero dimensions added here for safety
    if w <= 0 or h <= 0:
        return False
    if w > threshold * W or h > threshold * H:
        # print(f"Rejected by is_valid_candidate: bbox={bbox}, threshold={threshold}, W={W}, H={H}") # Debug print
        return False
    return True
