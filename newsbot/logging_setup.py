import logging

def setup(level="INFO"):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
