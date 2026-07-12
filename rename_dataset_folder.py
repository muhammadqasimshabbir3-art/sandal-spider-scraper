import os
import json
from PIL import Image

# ============================================================
# Configuration
# ============================================================
ROOT_DIR = "dataset/Zappos"
MIN_WIDTH = 128
MIN_HEIGHT = 128

# ============================================================
# Get folders
# ============================================================
folders = sorted(
    f for f in os.listdir(ROOT_DIR)
    if os.path.isdir(os.path.join(ROOT_DIR, f))
)

folder_mapping = {}
removed_images = {}

# ============================================================
# Rename folders and remove small images
# ============================================================
for idx, folder in enumerate(folders, start=1):
    old_path = os.path.join(ROOT_DIR, folder)
    new_name = str(idx)
    new_path = os.path.join(ROOT_DIR, new_name)

    # Rename folder
    os.rename(old_path, new_path)

    folder_mapping[new_name] = folder
    removed_images[new_name] = []

    # Walk through all images inside the folder
    for root, _, files in os.walk(new_path):
        for file in files:
            if not file.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp")):
                continue

            image_path = os.path.join(root, file)

            try:
                with Image.open(image_path) as img:
                    width, height = img.size

                if width < MIN_WIDTH or height < MIN_HEIGHT:
                    os.remove(image_path)

                    removed_images[new_name].append({
                        "filename": os.path.relpath(image_path, new_path),
                        "width": width,
                        "height": height
                    })

            except Exception as e:
                print(f"Could not process {image_path}: {e}")

# ============================================================
# Save metadata
# ============================================================
metadata = {
    "folder_mapping": folder_mapping,
    "removed_images": removed_images
}

output_json = os.path.join(ROOT_DIR, "dataset_metadata.json")

with open(output_json, "w", encoding="utf-8") as f:
    json.dump(metadata, f, indent=4, ensure_ascii=False)

# ============================================================
# Summary
# ============================================================
total_removed = sum(len(v) for v in removed_images.values())

print(f"Renamed {len(folder_mapping)} folders.")
print(f"Removed {total_removed} images smaller than {MIN_WIDTH}x{MIN_HEIGHT}.")
print(f"Metadata saved to: {output_json}")