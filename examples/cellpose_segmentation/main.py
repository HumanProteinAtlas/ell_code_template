import datetime
import logging
import os
from pathlib import Path

import cellpose_segmentation
import image_utils

import click


logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
# console =
# logging.getLogger().addHandler(console)
logger = logging.getLogger("Cellpose segmentation")

@click.group()
def cli():
    pass

@cli.command("process_batch", help="Process a batch of images")
@click.option('--input', '-i', default='./input', help='Input folder')
def process_batch():
    pass

@cli.command("process_image", help="Process a single image")
@click.option('--nuclei', '-n', help='Nuclei image', type=click.Path(exists=True))
@click.option('--nuclei_diameter', '-N', help='Nuclei diameter', type=int)
@click.option('--cytosol', '-c', help='Cytoplasm image', type=click.Path(exists=True))
@click.option('--cytosol_diameter', '-C', help='Cytoplasm diameter', type=int)
@click.option('--output_folder', '-o', help='Output folder', type=click.Path(exists=False, file_okay=False, dir_okay=True))
@click.option('--output_prefix', '-p', help='Output image prefix', type=str)
@click.option('--gpu/--cpu', '-g/-u', is_flag=True, help='Use GPU/CPU', type=bool, default=False)
def process_image(nuclei: Path, nuclei_diameter: int, cytosol: Path, cytosol_diameter: int, output_folder: Path, output_prefix: str, gpu: bool):
    # Log the start time and the final configuration so you can keep track of what you did
    start = datetime.datetime.now()
    logger.info('Start: ' + start.strftime("%Y/%m/%d %H:%M:%S"))
    from cellpose import models

    # We load the model
    model_nuc = models.CellposeModel(gpu=gpu, model_type='nuclei')
    model_cyto = models.CellposeModel(gpu=gpu, model_type='cyto3')
    logger.info('Using cyto3 for cell segmentation')

    os.makedirs(output_folder, exist_ok=True)
    # We load the images as numpy arrays
    nuclei_img = image_utils.read_grayscale_image(nuclei)
    cyto_img1 = image_utils.read_grayscale_image(cytosol)
    # Segmentation
    cellpose_segmentation.segment(
        model_nuc=model_nuc, model_cyto=model_cyto,
        nuclei_img=nuclei_img, cyto_img1=cyto_img1, cyto_img2=None,
        nuc_diameter=nuclei_diameter, cell_diameter=cytosol_diameter,
        output_folder=output_folder,
        output_prefix=output_prefix+"_"
    )

    logger.info(f"- Saved results for {output_prefix}")
    stop = datetime.datetime.now()
    logger.info(f'End: {stop.strftime("%Y/%m/%d %H:%M:%S")}, time: {(stop - start).total_seconds():.2f}s')

if __name__ == '__main__':
    cli()