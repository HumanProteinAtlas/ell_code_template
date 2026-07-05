import numpy as np
from skimage.exposure import rescale_intensity
from skimage.io import imsave
from scipy import ndimage as ndi
from skimage.segmentation import watershed, relabel_sequential


def sharpen(image):
    pl, ph = np.percentile(image, (0.1, 99.9))
    img_rescale = rescale_intensity(image, in_range=(pl, ph))
    return img_rescale


def adaptive_hist(image):
    q10 = np.percentile(image, 10.0)
    image[image < q10] = q10
    return image


def assign_cyto_region(cyto_mask, seg_nuclei, result, fill_only_unfilled=False):
    target = cyto_mask if not fill_only_unfilled else (cyto_mask & (result == 0))
    if not target.any():
        return

    seeds = np.where(cyto_mask, seg_nuclei, 0)
    nuc_ids = np.unique(seeds)
    nuc_ids = nuc_ids[nuc_ids != 0]

    if len(nuc_ids) == 0:
        dilated_labeled = ndi.grey_dilation(seg_nuclei, size=3)
        seeds = np.where(cyto_mask, dilated_labeled, 0)
        nuc_ids = np.unique(seeds)
        nuc_ids = nuc_ids[nuc_ids != 0]
        if len(nuc_ids) == 0:
            return

    if len(nuc_ids) == 1:
        result[target] = nuc_ids[0]
    else:
        dist = ndi.distance_transform_edt(cyto_mask)
        wl = watershed(-dist, seeds, mask=cyto_mask)
        if fill_only_unfilled:
            result[target] = wl[target]
        else:
            result[cyto_mask] = wl[cyto_mask]


def merge_segmentations(seg_nuclei, seg_cyto1, seg_cyto2, nuc_diameter):
    height, width = seg_nuclei.shape
    result = np.zeros((height, width), dtype=np.int32)
    unique_nucs = sorted(n for n in np.unique(seg_nuclei) if n != 0)

    if seg_cyto1 is not None:
        nuc_primary_cyto1 = {}
        for nuc in unique_nucs:
            nuc_pixels = seg_cyto1[seg_nuclei == nuc]
            vals, cnts = np.unique(nuc_pixels, return_counts=True)
            nz = vals != 0
            if nz.any():
                nuc_primary_cyto1[nuc] = vals[nz][np.argmax(cnts[nz])]

        for c1 in set(nuc_primary_cyto1.values()):
            assign_cyto_region(seg_cyto1 == c1, seg_nuclei, result, fill_only_unfilled=False)

    if seg_cyto2 is not None:
        nuc_primary_cyto2 = {}
        for nuc in unique_nucs:
            nuc_pixels = seg_cyto2[seg_nuclei == nuc]
            vals, cnts = np.unique(nuc_pixels, return_counts=True)
            nz = vals != 0
            if nz.any():
                nuc_primary_cyto2[nuc] = vals[nz][np.argmax(cnts[nz])]

        for c2 in set(nuc_primary_cyto2.values()):
            assign_cyto_region(seg_cyto2 == c2, seg_nuclei, result, fill_only_unfilled=True)

    for nuc in unique_nucs:
        result[seg_nuclei == nuc] = nuc

    return relabel_sequential(result)[0]


def segment(model_nuc, model_cyto, nuclei_img, cyto_img1, cyto_img2, nuc_diameter, cell_diameter, output_folder,
            output_prefix, model_cpsam=None, use_cpsam=False):
    channels = [1, 0]
    nuclei_masks, flows, styles = model_nuc.eval(
        np.stack([nuclei_img, np.zeros_like(nuclei_img)]),
        channels=channels,
        diameter=nuc_diameter,
    )
    imsave(output_folder + "/" + output_prefix + "nuclei_mask.png", nuclei_masks, check_contrast=False)

    if cyto_img1 is not None:
        cell_masks = None

        if use_cpsam and model_cpsam is not None:
            # Single CellposeSAM forward pass on all channels stacked as (H, W, 3).
            # The model sees nuclear + cyto1 + cyto2 simultaneously, so no secondary
            # channel is needed in the merge step.
            ch0 = sharpen(adaptive_hist(nuclei_img)).astype(np.float32)
            ch1 = sharpen(adaptive_hist(cyto_img1)).astype(np.float32)
            ch2 = sharpen(adaptive_hist(cyto_img2)).astype(np.float32) if cyto_img2 is not None \
                  else np.zeros_like(ch0, dtype=np.float32)
            rgb = np.stack([ch0, ch1, ch2], axis=-1)

            sam_masks, flows, styles = model_cpsam.eval(
                rgb,
                diameter=cell_diameter,
                flow_threshold=0.8,
                cellprob_threshold=-0.4,
                normalize=False,
            )
            imsave(output_folder + "/" + output_prefix + "sam_raw_mask.png", sam_masks, check_contrast=False)

            cell_masks = merge_segmentations(nuclei_masks, sam_masks, None, nuc_diameter)

        else:
            channels = [1, 2]
            cyto1_masks, flows, styles = model_cyto.eval(
                np.stack([sharpen(adaptive_hist(cyto_img1)), nuclei_masks]),
                channels=channels,
                diameter=cell_diameter,
                flow_threshold=0.8,
                cellprob_threshold=-0.4
            )
            imsave(output_folder + "/" + output_prefix + "cyto1_mask.png", cyto1_masks, check_contrast=False)

            if cyto_img2 is not None:
                cyto2_masks, flows, styles = model_cyto.eval(
                    np.stack([sharpen(adaptive_hist(cyto_img2)), nuclei_masks]),
                    channels=channels,
                    diameter=cell_diameter,
                    flow_threshold=0.8,
                    cellprob_threshold=-0.4
                )
                imsave(output_folder + "/" + output_prefix + "cyto2_mask.png", cyto2_masks)

                cell_masks = merge_segmentations(nuclei_masks, cyto1_masks, cyto2_masks, nuc_diameter)
            else:
                cell_masks = merge_segmentations(nuclei_masks, cyto1_masks, None, nuc_diameter)

        imsave(output_folder + "/" + output_prefix + "cell_mask.png", cell_masks, check_contrast=False)
