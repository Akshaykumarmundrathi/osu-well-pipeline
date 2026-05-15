from grid.scoring import stage_grid_extraction
from location.location_extractor import stage_location_extraction
from county.county_extractor import stage_county_image_extraction


def main():
    print("🚀 Starting OSU Grid Processing Pipeline")
    stage_grid_extraction()
    stage_location_extraction()
    stage_county_image_extraction()


if __name__ == "__main__":
    main()
