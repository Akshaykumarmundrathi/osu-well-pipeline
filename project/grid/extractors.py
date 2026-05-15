import cv2
import numpy as np


def extract_grid_region_adaptive(cv_image):
    """ Attempts extraction using adaptive thresholding... """
    gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    # Apply GaussianBlur to reduce noise before thresholding
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                   cv2.THRESH_BINARY_INV, blockSize=15, C=8)  # Adjusted C

    # Extract horizontal lines
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))  # Increased kernel width
    horizontal_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel, iterations=2)  # Increased iterations

    # Extract vertical lines
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))  # Increased kernel height
    vertical_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel, iterations=2)  # Increased iterations

    # Combine horizontal and vertical lines:
    grid_binary = cv2.addWeighted(horizontal_lines, 0.5, vertical_lines, 0.5, 0.0)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))  # Increased kernel size
    grid_binary = cv2.dilate(grid_binary, kernel, iterations=2)  # Increased iterations

    contours, _ = cv2.findContours(grid_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    grid_candidate = None
    max_area = 0
    for cnt in contours:
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4:
            x, y, w, h = cv2.boundingRect(approx)
            area = w * h
            # Keep the basic size check, specific filters applied in combined func
            if area > max_area and w > 50 and h > 50:  # Reduced min size slightly
                max_area = area
                grid_candidate = (x, y, w, h)

    if grid_candidate:
        x, y, w, h = grid_candidate
        # Add buffer to capture full lines slightly outside contour
        buffer = 5
        y_start, y_end = max(0, y - buffer), min(cv_image.shape[0], y + h + buffer)
        x_start, x_end = max(0, x - buffer), min(cv_image.shape[1], x + w + buffer)
        # Ensure extracted coordinates are valid
        if y_end > y_start and x_end > x_start:
            return cv_image[y_start:y_end, x_start:x_end], (x, y, w, h)  # Return original bbox
    return None, None


def extract_grid_region_otsu(cv_image):
    """ Uses Otsu's thresholding... """
    gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)  # Add blur
    ret, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dilated = cv2.dilate(binary, kernel, iterations=3)  # Increased iterations

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    grid_candidate = None
    max_area = 0
    for cnt in contours:
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4:
            x, y, w, h = cv2.boundingRect(approx)
            area = w * h
            # Keep basic size check
            if area > max_area and w > 50 and h > 50:  # Reduced min size slightly
                max_area = area
                grid_candidate = (x, y, w, h)

    if grid_candidate:
        x, y, w, h = grid_candidate
        buffer = 5
        y_start, y_end = max(0, y - buffer), min(cv_image.shape[0], y + h + buffer)
        x_start, x_end = max(0, x - buffer), min(cv_image.shape[1], x + w + buffer)
        if y_end > y_start and x_end > x_start:
            return cv_image[y_start:y_end, x_start:x_end], (x, y, w, h)  # Return original bbox
    return None, None


def extract_grid_region_canny(cv_image):
    """ Uses Canny edge detection... """
    gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    # Use median blur for salt-and-pepper noise, or Gaussian for general noise
    # filtered = cv2.medianBlur(gray, 5)
    filtered = cv2.GaussianBlur(gray, (5, 5), 0)
    # Adjust Canny thresholds if needed
    edges = cv2.Canny(filtered, threshold1=50, threshold2=150)  # Wider range, adjust based on testing
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dilated = cv2.dilate(edges, kernel, iterations=2)  # Increase iterations slightly

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    grid_candidate = None
    max_area = 0
    for cnt in contours:
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4:
            x, y, w, h = cv2.boundingRect(approx)
            area = w * h
            # Keep basic size check
            if area > max_area and w > 50 and h > 50:  # Reduced min size slightly
                max_area = area
                grid_candidate = (x, y, w, h)

    if grid_candidate:
        x, y, w, h = grid_candidate
        buffer = 5
        y_start, y_end = max(0, y - buffer), min(cv_image.shape[0], y + h + buffer)
        x_start, x_end = max(0, x - buffer), min(cv_image.shape[1], x + w + buffer)
        if y_end > y_start and x_end > x_start:
            return cv_image[y_start:y_end, x_start:x_end], (x, y, w, h)  # Return original bbox
    return None, None


def extract_grid_region_hough(cv_image):
    """ Uses probabilistic Hough transform... """
    gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150, apertureSize=3)

    # Adjust HoughLinesP parameters - crucial step
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180,
                            threshold=50,       # Lower threshold to detect more lines initially
                            minLineLength=50,   # Adjust based on expected cell size
                            maxLineGap=10)      # Adjust based on line continuity

    if lines is None:
        # print("Hough: No lines found") # Debug
        return None, None

    horizontal_lines, vertical_lines = [], []
    min_h_lines = 5  # Require at least N horizontal lines
    min_v_lines = 5  # Require at least N vertical lines

    for line in lines:
        x1, y1, x2, y2 = line[0]
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        length = np.sqrt((x2-x1)**2 + (y2-y1)**2)

        # More strict angle check
        if abs(angle) < 5:  # Horizontal
            # Filter very short lines that might be noise/text fragments
            if length > cv_image.shape[1] * 0.1:  # E.g. line must be > 10% of image width
                horizontal_lines.append(line[0])
        elif abs(abs(angle) - 90) < 5:  # Vertical
            if length > cv_image.shape[0] * 0.1:  # E.g. line must be > 10% of image height
                vertical_lines.append(line[0])

    # Check if enough lines of BOTH orientations were found
    if len(horizontal_lines) < min_h_lines or len(vertical_lines) < min_v_lines:
        # print(f"Hough: Not enough lines found (H:{len(horizontal_lines)}/{min_h_lines}, V:{len(vertical_lines)}/{min_v_lines})") # Debug
        return None, None

    # Collect all coordinates from the filtered lines
    x_coords, y_coords = [], []
    for line in horizontal_lines:
        x_coords.extend([line[0], line[2]])
        y_coords.extend([line[1], line[3]])
    for line in vertical_lines:
        x_coords.extend([line[0], line[2]])
        y_coords.extend([line[1], line[3]])

    if not x_coords or not y_coords:
        # print("Hough: No coordinates collected") # Debug
        return None, None

    # Calculate bounding box
    x_min, x_max = min(x_coords), max(x_coords)
    y_min, y_max = min(y_coords), max(y_coords)
    w, h = x_max - x_min, y_max - y_min

    # Basic check for valid dimensions before returning
    if w <= 10 or h <= 10:  # Prevent tiny boxes
        # print(f"Hough: BBox too small (W:{w}, H:{h})") # Debug
        return None, None

    # Return candidate bbox (x,y,w,h) - specific filters applied later
    candidate_bbox = (x_min, y_min, w, h)

    # Crop the image with a small buffer
    buffer = 5
    y_start, y_end = max(0, y_min - buffer), min(cv_image.shape[0], y_max + buffer)
    x_start, x_end = max(0, x_min - buffer), min(cv_image.shape[1], x_max + buffer)

    if y_end > y_start and x_end > x_start:
        grid_region = cv_image[y_start:y_end, x_start:x_end]
        return grid_region, candidate_bbox
    else:
        # print(f"Hough: Invalid crop dimensions after buffer ({x_start},{y_start}) -> ({x_end},{y_end})") # Debug
        return None, None


def extract_grid_region_rotated(cv_image):
    """ Uses cv2.minAreaRect... """
    gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)  # Add blur
    ret, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dilated = cv2.dilate(binary, kernel, iterations=2)  # Adjusted iterations

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best_rect_contour = None
    max_area = 0

    # Find the contour that results in the largest minimum area rectangle
    for cnt in contours:
        # Filter small noise contours early
        if cv2.contourArea(cnt) < 2500:  # Increased minimum contour area
            continue
        rect = cv2.minAreaRect(cnt)  # ((center_x, center_y), (width, height), angle)
        area = rect[1][0] * rect[1][1]
        if area > max_area:
            # Basic check: width and height from minAreaRect should be reasonable
            if rect[1][0] > 10 and rect[1][1] > 10:
                max_area = area
                best_rect_contour = cnt  # Store the contour itself

    if best_rect_contour is None:
        return None, None

    # Get the bounding box of the *contour* that gave the best minAreaRect
    x, y, w, h = cv2.boundingRect(best_rect_contour)
    candidate_bbox = (x, y, w, h)

    # Optional: Use the minAreaRect to crop precisely for rotated grids
    # This part is more complex if you need to deskew the image.
    # For now, we return the axis-aligned bounding box of the contour.

    # Crop using the axis-aligned bounding box with buffer
    buffer = 5
    y_start, y_end = max(0, y - buffer), min(cv_image.shape[0], y + h + buffer)
    x_start, x_end = max(0, x - buffer), min(cv_image.shape[1], x + w + buffer)

    if y_end > y_start and x_end > x_start:
        grid_region = cv_image[y_start:y_end, x_start:x_end]
        return grid_region, candidate_bbox
    else:
        return None, None


def extract_grid_region_corners(cv_image):
    """ Uses corner detection (goodFeaturesToTrack)... """
    gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # Parameters for corner detection - may need tuning
    maxCorners = 150  # Max corners to detect (e.g., 9x9 grid has 81 internal corners + borders)
    qualityLevel = 0.01
    minDistance = 10  # Minimum distance between detected corners

    corners = cv2.goodFeaturesToTrack(blurred, maxCorners=maxCorners,
                                       qualityLevel=qualityLevel, minDistance=minDistance)

    if corners is None or len(corners) < 10:  # Require a minimum number of corners
        # print(f"Corners: Not enough corners found ({len(corners) if corners is not None else 0})") # Debug
        return None, None

    corners = np.int32(corners).reshape(-1, 2)

    # Calculate bounding box from detected corners
    x_min, y_min = np.min(corners, axis=0)
    x_max, y_max = np.max(corners, axis=0)
    w = x_max - x_min
    h = y_max - y_min

    # Basic check for minimal size
    if w < 50 or h < 50:
        # print(f"Corners: BBox too small (W:{w}, H:{h})") # Debug
        return None, None

    candidate_bbox = (x_min, y_min, w, h)

    # Crop the image using the bounding box with a buffer
    buffer = 5
    y_start, y_end = max(0, y_min - buffer), min(cv_image.shape[0], y_max + buffer)
    x_start, x_end = max(0, x_min - buffer), min(cv_image.shape[1], x_max + buffer)

    if y_end > y_start and x_end > x_start:
        grid_region = cv_image[y_start:y_end, x_start:x_end]
        return grid_region, candidate_bbox
    else:
        # print(f"Corners: Invalid crop dimensions after buffer ({x_start},{y_start}) -> ({x_end},{y_end})") # Debug
        return None, None
