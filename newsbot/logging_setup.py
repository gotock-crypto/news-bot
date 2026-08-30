import logging
def setup(level='INFO'):
    logging.basicConfig(level=getattr(logging,str(level).upper(),logging.INFO),format='%(asctime)s %(levelname)s %(name)s: %(message)s')
