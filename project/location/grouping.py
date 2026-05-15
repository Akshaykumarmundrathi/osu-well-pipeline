def find_keywords_lists(text_annotations, keywords, extend_right=400, padding_height=50):
    """
    Loops over text annotations (skipping the first full-text annotation) and collects
    bounding boxes (with vertical padding and right extension) for each keyword variation.
    Returns three lists: one for "section", one for "township", and one for "range".
    """
    sections  = []
    townships = []
    ranges    = []

    for annotation in text_annotations[1:]:
        text = annotation.description.lower()
        poly = annotation.bounding_poly

        x_min = min(vertex.x for vertex in poly.vertices)
        y_min = min(vertex.y for vertex in poly.vertices) - padding_height
        x_max = max(vertex.x for vertex in poly.vertices)
        y_max = max(vertex.y for vertex in poly.vertices) + padding_height

        bbox = [x_min, y_min, x_max + extend_right, y_max]

        for var in keywords["section"]:
            if var in text:
                sections.append(bbox)
                break

        for var in keywords["township"]:
            if var in text:
                townships.append(bbox)
                break

        for var in keywords["range"]:
            if var in text:
                ranges.append(bbox)
                break

    return sections, townships, ranges


def vertical_overlap(y1_min, y1_max, y2_min, y2_max):
    """
    Computes the ratio of vertical overlap between two boxes.
    Returns the overlap fraction (0 to 1).
    """
    overlap = max(0, min(y1_max, y2_max) - max(y1_min, y2_min))
    height1 = y1_max - y1_min
    height2 = y2_max - y2_min
    return overlap / min(height1, height2) if min(height1, height2) > 0 else 0


def choose_group(sections, townships, ranges, min_overlap=0.5):
    """
    Attempts to choose a group where a candidate "section" has at least one candidate
    "township" and one candidate "range" that begin to the right of the section
    and have sufficient vertical overlap.

    Returns a dictionary with keys: "section", "township", and "range".

    If no valid group is found based solely on "section", then if townships and ranges exist,
    returns a fallback group with a unified bounding box.
    """
    for s in sections:
        s_x_min, s_y_min, s_x_max, s_y_max = s
        candidate_t = None
        candidate_r = None

        for t in townships:
            t_x_min, t_y_min, t_x_max, t_y_max = t
            if (
                t_x_min > s_x_max
                and vertical_overlap(s_y_min, s_y_max, t_y_min, t_y_max) >= min_overlap
            ):
                candidate_t = t
                break

        for r in ranges:
            r_x_min, r_y_min, r_x_max, r_y_max = r
            if (
                r_x_min > s_x_max
                and vertical_overlap(s_y_min, s_y_max, r_y_min, r_y_max) >= min_overlap
            ):
                candidate_r = r
                break

        if candidate_t and candidate_r:
            return {"section": s, "township": candidate_t, "range": candidate_r}

    # Fallback: if no grouped section found but townships and ranges exist,
    # use the union of all township and range boxes and adjust boundaries.
    if townships and ranges:
        union_boxes = townships + ranges
        union_x_min = min(box[0] for box in union_boxes)
        union_y_min = min(box[1] for box in union_boxes)
        union_x_max = max(box[2] for box in union_boxes)
        union_y_max = max(box[3] for box in union_boxes)

        return {
            "section":  None,
            "township": None,
            "range":    None,
            "unified": [
                union_x_min - 300,
                union_y_min - 80,
                union_x_max + 180,
                union_y_max + 80,
            ],
        }
    return None


def get_unified_bounding_box(group, section_right_extension=200):
    """
    Returns a unified bounding box from a group dictionary.
    If a "section" candidate exists, use its box and extend its right edge further.
    Otherwise, return the fallback unified box.
    """
    if group is None:
        return None

    if group.get("section") is not None:
        s = group["section"]
        return [s[0], s[1], s[2] + section_right_extension, s[3]]

    elif "unified" in group:
        return group["unified"]

    return None
