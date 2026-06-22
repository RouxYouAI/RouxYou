import functools
import logging
import time

def retry(times=3, base_delay=0.5, exceptions=(Exception,)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(times):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < times - 1:
                        delay = base_delay * (2 ** attempt)
                        logging.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.2f}s...")
                        time.sleep(delay)
                    else:
                        logging.error(f"All {times} attempts failed. Last error: {e}")
            raise last_exception
        return wrapper
    return decorator