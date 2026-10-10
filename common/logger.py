import logging

logger = logging.getLogger('miniflux_ai')
if not logger.handlers:
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('%(asctime)s - %(filename)s - %(levelname)s - %(message)s'))
    logger.addHandler(console)
logger.setLevel(logging.INFO)
