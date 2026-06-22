import logging
from contextlib import contextmanager
from time import perf_counter

@contextmanager
def timed_block(label):
    start = perf_counter()
    yield
    end = perf_counter()
    elapsed = end - start
    logging.info(f'{label}: {elapsed:.6f}s')