import warnings
import numpy as np
from skimage.measure import regionprops
from scipy.ndimage.morphology import grey_dilation
import pandas as pd
import image_utils
import cv2


warnings.simplefilter(action="ignore", category=FutureWarning)


def safe_crop(image, bbox):
    y1, x1, y2, x2 = bbox
    img_h, img_w = image.shape[:2]
    is_single_channel = len(image.shape) == 2
    if x1 < 0:
        pad_x1 = 0 - x1
        new_x1 = 0
    else:
        pad_x1 = 0
        new_x1 = x1
    if y1 < 0:
        pad_y1 = 0 - y1
        new_y1 = 0
    else:
        pad_y1 = 0
        new_y1 = y1
    if x2 > img_w - 1:
        pad_x2 = x2 - (img_w - 1)
        new_x2 = img_w - 1
    else:
        pad_x2 = 0
        new_x2 = x2
    if y2 > img_h - 1:
        pad_y2 = y2 - (img_h - 1)
        new_y2 = img_h - 1
    else:
        pad_y2 = 0
        new_y2 = y2

    patch = image[new_y1:new_y2, new_x1:new_x2]
    patch = (
        np.pad(
            patch,
            ((pad_y1, pad_y2), (pad_x1, pad_x2)),
            mode="constant",
            constant_values=0,
        )
        if is_single_channel
        else np.pad(
            patch,
            ((pad_y1, pad_y2), (pad_x1, pad_x2), (0, 0)),
            mode="constant",
            constant_values=0,
        )
    )
    return patch, (new_y1, new_x1, new_y2, new_x2)


# Optional code to generate the segmented cell crops
def generate_crops(image_stack, image_suffixes, cell_mask, nuclei_mask, crop_size, crop_bitdepth, crop_mask, mask_cell, output_folder, output_prefix):
    regions = regionprops(cell_mask)

    cell_bboxes = []
    for region in regions:
        image_cp = image_stack[0][0].copy()

        bbox = region.bbox
        bbox_center = [(bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2]
        h, w = bbox[2] - bbox[0], bbox[3] - bbox[1]
        fixed_bbox = (
            bbox_center[0] - crop_size // 2,
            bbox_center[1] - crop_size // 2,
            bbox_center[0] + crop_size // 2,
            bbox_center[1] + crop_size // 2,
        )

        if crop_mask:
            this_cell_mask = cell_mask.copy()
            this_cell_mask[this_cell_mask != region.label] = 0
            this_cell_mask[this_cell_mask == region.label] = 1
            this_cell_mask = grey_dilation(this_cell_mask, size=7)
            curr_cell_mask, _ = safe_crop(this_cell_mask, fixed_bbox)
            cv2.imwrite(f"{output_folder}/{output_prefix}cell{region.label}_cellmask.png", np.uint8(curr_cell_mask * 255))
            if nuclei_mask is not None:
                this_nuclei_mask = nuclei_mask.copy()
                this_nuclei_mask[this_nuclei_mask != region.label] = 0
                this_nuclei_mask[this_nuclei_mask == region.label] = 1
                this_nuclei_mask = grey_dilation(this_nuclei_mask, size=7)
                curr_nuclei_mask, _ = safe_crop(this_nuclei_mask, fixed_bbox)
                cv2.imwrite(f"{output_folder}/{output_prefix}cell{region.label}_nucleimask.png", np.uint8(curr_nuclei_mask * 255))

        for curr_img_index in range(len(image_stack)):
            if curr_img_index != 0:
                image_cp = image_stack[curr_img_index][0].copy()

            suffix = image_suffixes[curr_img_index]
            cell_crop, _ = safe_crop(image_cp, fixed_bbox)
            cv2.imwrite(f"{output_folder}/{output_prefix}cell{region.label}_crop_{suffix}.png", image_utils.convert_bitdepth(cell_crop, crop_bitdepth))

            if mask_cell:
                this_cell_mask = cell_mask == region.label
                this_cell_mask = grey_dilation(this_cell_mask, size=7)
                image_cp[this_cell_mask == 0] = 0
                cell_mask_crop, _ = safe_crop(image_cp, fixed_bbox)
                cv2.imwrite(f"{output_folder}/{output_prefix}cell{region.label}_crop_masked_{suffix}.png", image_utils.convert_bitdepth(cell_mask_crop, crop_bitdepth))

        new_center = (crop_size // 2, crop_size // 2)
        new_bbox = (
            new_center[0] - h // 2,
            new_center[1] - w // 2,
            new_center[0] + h // 2,
            new_center[1] + w // 2,
        )
        cell_bboxes.append(
            {
                "id" : output_prefix,
                "cell": region.label,
                "y1": new_bbox[0],
                "x1": new_bbox[1],
                "y2": new_bbox[2],
                "x2": new_bbox[3],
            }
        )

    return pd.DataFrame(cell_bboxes)





