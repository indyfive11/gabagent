import os
import shutil

def get_terminal_width():
    # Attempt to get width from shutil
    columns, _ = shutil.get_terminal_size(fallback=(80, 24))
    return columns
