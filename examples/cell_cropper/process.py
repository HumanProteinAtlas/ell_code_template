import datetime
import logging
import os
import pandas as pd
from pathlib import Path

import cell_cropper
import image_utils


# This is the log configuration. It will log everything to a file AND the console
logging.basicConfig(filename='log.txt', encoding='utf-8', format='%(levelname)s: %(message)s', filemode='w', level=logging.INFO)
console = logging.StreamHandler()
logging.getLogger().addHandler(console)
logger = logging.getLogger("HPA cell cropper")

# This is the general configuration variable. We are going to use the special key "log" in the dictionary to use the log in our code
config = { "log": logger}

# If you want to use constants with your script, add them here
config["crop_size"] = 640
config["crop_bitdepth"] = 16
config["crop_mask"] = True
config["mask_cell"] = True

# Log the start time and the final configuration so you can keep track of what you did
config["log"].info('Start: ' + datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S"))
config["log"].info('Parameters used:')
config["log"].info(config)
config["log"].info('----------')


# If we provide a "path_list.csv" file, we run our code for each pair of input/output sub-folders
if os.path.exists("./path_list.csv"):
    with open("./path_list.csv", 'r') as path_list:
        path_list = path_list.readlines()

    df = pd.DataFrame(columns=['id', 'cell', 'x1', 'y1', 'x2', 'y2'])

    for curr_set in path_list:
        if curr_set.strip() != "" and not curr_set.startswith("#"):
            curr_set_arr = [v.strip() for v in curr_set.split(",")]
            # New format: cell_mask,nuclei_mask,crop_folder,output_prefix,image1,image2,...
            cell_mask_path  = curr_set_arr[0]
            nuclei_mask_path = curr_set_arr[1]
            crop_folder     = curr_set_arr[2]
            output_prefix   = curr_set_arr[3]
            image_paths     = curr_set_arr[4:]

            # We create the output folder
            os.makedirs(crop_folder, exist_ok=True)

            # Build image stack and derive per-image suffixes from filenames
            image_stack = []
            image_suffixes = []
            for img_path in image_paths:
                image_stack.append([image_utils.read_grayscale_image(img_path)])
                stem = Path(img_path).stem  # filename without extension
                suffix = stem[len(output_prefix):] if stem.startswith(output_prefix) else stem
                image_suffixes.append(suffix)

            cell_mask = image_utils.read_grayscale_image(cell_mask_path)
            nuclei_mask = None
            if nuclei_mask_path != "":
                nuclei_mask = image_utils.read_grayscale_image(nuclei_mask_path)

            # Single cell crops
            cell_bbox_df = cell_cropper.generate_crops(image_stack, image_suffixes, cell_mask, nuclei_mask, config["crop_size"], config["crop_bitdepth"], config["crop_mask"], config["mask_cell"], crop_folder, output_prefix)
            df = pd.concat([df, cell_bbox_df], ignore_index=True)

            config["log"].info("- Saved results for " + output_prefix)

    # We store the cell crops bboxes and ids for easy localization
    df.to_csv("crop_info.csv", index=False)


config["log"].info('----------')
config["log"].info('End: ' + datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S"))