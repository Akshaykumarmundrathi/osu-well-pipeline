import os
import cv2

from grid.extractors import (
    extract_grid_region_adaptive,
    extract_grid_region_otsu,
    extract_grid_region_canny,
    extract_grid_region_hough,
    extract_grid_region_rotated,
    extract_grid_region_corners,
)
from grid.filters import is_valid_candidate
from pdf.pdf_manager import PDFDocumentManager


def extract_grid_region_combined(cv_image):
    """
    Tries multiple methods and applies specific filters for the target grid type.
    Returns the best candidate (image crop and bounding box) that passes filters.
    """
    # --- Define TARGET PROPERTIES for the specific "well location" grid ---
    # --- !!! IMPORTANT: Replace these placeholder values !!! ---
    # --- Determine these experimentally by analyzing logs from sample PDFs ---
    TARGET_ASPECT_RATIO_MIN = 0.85  # Expecting square shape (e.g., 0.9)
    TARGET_ASPECT_RATIO_MAX = 1.15  # Expecting square shape (e.g., 1.1)
    TARGET_WIDTH_MIN  = 280         # Example: Min expected width in pixels (e.g., 400)
    TARGET_WIDTH_MAX  = 850         # Example: Max expected width in pixels (e.g., 700)
    TARGET_HEIGHT_MIN = 280         # Example: Min expected height in pixels (e.g., 400)
    TARGET_HEIGHT_MAX = 850         # Example: Max expected height in pixels (e.g., 700)
    TARGET_MIN_AREA   = TARGET_WIDTH_MIN * TARGET_HEIGHT_MIN  # Optional minimum area check
    # --- End of Placeholder Values ---

    candidates = []
    methods = [
        extract_grid_region_adaptive,
        extract_grid_region_otsu,
        extract_grid_region_canny,
        extract_grid_region_hough,
        extract_grid_region_rotated,
        extract_grid_region_corners,
    ]

    # print(f"\nAttempting grid detection on image with shape {cv_image.shape}") # Debug
    for func in methods:
        # print(f"  Trying method: {func.__name__}") # Debug
        grid_region, bbox = func(cv_image)  # Expect bbox = (x, y, w, h)

        if bbox is not None and grid_region is not None and grid_region.size > 0:
            x, y, w, h = bbox
            # Check 1: Basic validity (already done in is_valid_candidate)
            if w > 0 and h > 0:
                # Check 2: Overall size relative to page (reject if too large)
                if is_valid_candidate(bbox, cv_image):
                    aspect_ratio = float(w) / h
                    area = w * h

                    # --- APPLY TARGETED FILTERS ---
                    is_correct_aspect_ratio = TARGET_ASPECT_RATIO_MIN <= aspect_ratio <= TARGET_ASPECT_RATIO_MAX
                    is_correct_width  = TARGET_WIDTH_MIN  <= w <= TARGET_WIDTH_MAX
                    is_correct_height = TARGET_HEIGHT_MIN <= h <= TARGET_HEIGHT_MAX
                    is_correct_area   = area >= TARGET_MIN_AREA  # Optional area check

                    # Check if all target conditions are met
                    if is_correct_aspect_ratio and is_correct_width and is_correct_height and is_correct_area:
                        # print(f"    Method {func.__name__} PASSED filters: bbox={bbox}, AR={aspect_ratio:.2f}, Area={area}") # Debug
                        candidates.append({'method': func.__name__, 'region': grid_region, 'bbox': bbox, 'area': area})
                    # else: # Debugging why filters failed
                    #     fail_reasons = []
                    #     if not is_correct_aspect_ratio: fail_reasons.append(f"AR {aspect_ratio:.2f} out of range [{TARGET_ASPECT_RATIO_MIN:.2f}-{TARGET_ASPECT_RATIO_MAX:.2f}]")
                    #     if not is_correct_width: fail_reasons.append(f"W {w} out of range [{TARGET_WIDTH_MIN}-{TARGET_WIDTH_MAX}]")
                    #     if not is_correct_height: fail_reasons.append(f"H {h} out of range [{TARGET_HEIGHT_MIN}-{TARGET_HEIGHT_MAX}]")
                    #     if not is_correct_area: fail_reasons.append(f"Area {area} < min {TARGET_MIN_AREA}")
                    #     # print(f"    Method {func.__name__} FAILED filters: {'; '.join(fail_reasons)}") # Debug
                # else:
                    # print(f"    Method {func.__name__} FAILED is_valid_candidate check") # Debug
            # else:
                # print(f"    Method {func.__name__} returned invalid bbox dimensions: {bbox}") # Debug
        # else:
             # print(f"    Method {func.__name__} returned None") # Debug

    if candidates:
        # Select the best candidate - currently based on largest area
        # You could add more sophisticated selection logic if needed
        # (e.g., prioritize methods known to work well for square grids)
        best_candidate_dict = max(candidates, key=lambda c: c['area'])
        # print(f"  Selected best candidate from method {best_candidate_dict['method']} with area {best_candidate_dict['area']}") # Debug
        return best_candidate_dict['region'], best_candidate_dict['bbox']

    # print("  No candidate passed all filters.") # Debug
    return None, None


# =====================================================
# GRID PIPELINE
# =====================================================

def process_pdf_for_grids(
    pdf_path,
    output_root="grid_images_final",
    log_folder="logs",
    resolution_multiplier=2
):
    """
    Processes a single PDF using centralized PDF manager.

    - Streams pages one at a time
    - Extracts grids
    - Saves grids
    - Creates detailed logs
    """
    base_filename = os.path.splitext(os.path.basename(pdf_path))[0]

    pdf_output_folder = os.path.join(output_root, base_filename)
    os.makedirs(pdf_output_folder, exist_ok=True)
    os.makedirs(log_folder, exist_ok=True)

    log_file_path = os.path.join(log_folder, f"{base_filename}_log.txt")

    with open(log_file_path, "w", encoding="utf-8") as log_file:

        log_file.write(f"Processing PDF: {pdf_path}\n")

        print(
            f"Processing grids for: "
            f"'{os.path.basename(pdf_path)}' "
            f"({resolution_multiplier}x resolution)"
        )

        found_any_page = False

        try:
            manager = PDFDocumentManager(
                pdf_path,
                resolution_multiplier=resolution_multiplier
            )

            for page_num, cv_img in manager.iter_cv2_pages():

                found_any_page = True
                log_file.write(f"\n--- Page {page_num} ---\n")

                try:
                    grid_img, bbox = extract_grid_region_combined(cv_img)

                    if grid_img is not None:
                        output_filename = (
                            f"{base_filename}_page_{page_num}_grid.png"
                        )
                        output_path = os.path.join(pdf_output_folder, output_filename)
                        cv2.imwrite(output_path, grid_img)

                        log_file.write(f"✅ Grid extracted and saved: {output_path}\n")
                        print(f"  Saved grid: {output_path}")
                    else:
                        log_file.write("⚠️ No grid detected on this page.\n")
                        print(f"  No grid detected on page {page_num}")

                except Exception as e:
                    log_file.write(f"❌ Error processing page {page_num}: {str(e)}\n")
                    print(f"  Error processing page {page_num}: {e}")

        except Exception as e:
            log_file.write(f"\n❌ Fatal PDF processing error: {str(e)}\n")
            print(f"Fatal Error processing {pdf_path}: {e}")

        if not found_any_page:
            log_file.write(f"\n❌ No pages could be converted from {pdf_path}. Skipped.\n")
            print(f"Could not convert any pages from {pdf_path}. Skipping file.")

    print(
        f"Grid processing complete for {pdf_path}. "
        f"Log saved to {log_file_path}\n"
    )


def process_all_pdfs_for_grids(folder_path, output_folder, log_folder="logs"):
    """
    Loops through all PDF files in the folder, extracts grids using filtered detection,
    saves them, and creates a separate log file for each PDF processed.
    """
    os.makedirs(output_folder, exist_ok=True)
    os.makedirs(log_folder, exist_ok=True)

    if not os.path.isdir(folder_path):
        print(f"Error: Input folder not found: {folder_path}")
        return

    print(f"Starting processing in folder: {folder_path}")
    pdf_files_processed = 0

    for file_name in os.listdir(folder_path):
        if not file_name.lower().endswith(".pdf"):
            print(f"Skipping non-PDF file: {file_name}")
            continue

        pdf_path = os.path.join(folder_path, file_name)
        print(f"\n>>> Processing file: {file_name}")

        try:
            process_pdf_for_grids(pdf_path, output_folder, log_folder)
            pdf_files_processed += 1
        except Exception as e:
            print(f"!!! Fatal Error processing file {file_name}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\nFinished processing all files. Processed {pdf_files_processed} PDF(s).")


def stage_grid_extraction():
    base_dir = os.getcwd()
    pdf_input_folder   = os.path.join(base_dir, "pdfs")
    grid_output_folder = os.path.join(base_dir, "grid_images_final")
    log_output_folder  = os.path.join(base_dir, "logs_final")

    os.makedirs(grid_output_folder, exist_ok=True)
    os.makedirs(log_output_folder,  exist_ok=True)

    if not os.path.isdir(pdf_input_folder):
        raise FileNotFoundError(f"PDF folder not found: {pdf_input_folder}")

    process_all_pdfs_for_grids(
        pdf_input_folder,
        grid_output_folder,
        log_output_folder
    )
