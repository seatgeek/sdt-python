# SPDX-FileCopyrightText: 2020 Lukas Schrangl <lukas.schrangl@tuwien.ac.at>
#
# SPDX-License-Identifier: BSD-3-Clause

import math
from pathlib import Path
import threading
from typing import Dict, Sequence, Union

import ipywidgets
import numpy as np
import pims
import traitlets


class ImageSelector(ipywidgets.HBox):
    """UI element to select an image sequence and frame number

    Given a list of file names, images, or image sequences, this allows the
    user to chose one of entry and also to select a frame number. The selected
    image is available via the :py:attr:`output` traitlet.
    """
    images = traitlets.Union([traitlets.Dict(), traitlets.List()])
    """Images or sequences to select from. Image sequences can be passed as
    3D :py:class:`numpy.ndarray`, as lists of 2D arrays, or as paths to image
    files, which will be opened using :py:mod:`pims`. Single images are
    represented as 2D arrays.

    This attribute can be a list of ``(key, img)`` tuples where ``key`` is the
    name to display and value an image (sequence), a dict mapping ``key`` to
    ``img`` (which will be converted to a list of tuples) or a plain list of
    image (sequence).
    """
    output = traitlets.Instance(np.ndarray, allow_none=True)
    """2D array representing the currently selected frame."""

    def __init__(self, images: Union[Sequence, Dict] = [], *args, **kwargs):
        """Parameters
        ---------
        images
            List of image (sequences) to populate :py:attr:`images`.
        """
        self._file_sel = ipywidgets.Dropdown(description="image")
        self._file_sel.observe(self._file_changed, "value")
        self._frame_sel = ipywidgets.BoundedIntText(description="frame",
                                                    min=0, max=0)
        self._frame_sel.observe(self._frame_changed, "value")
        super().__init__([self._file_sel, self._frame_sel], *args, **kwargs)

        self.images = images
        self._cur_image = None
        self._cur_image_opened = False
        self._frame_changed_lock = threading.Lock()

    @traitlets.validate("images")
    def _make_images_list(self, proposal):
        """Validator for the :py:attr:`images` traitlet

        Turns dictionaries into lists of tuples.
        """
        images = proposal["value"]
        if len(images) == 0:
            return []
        if isinstance(images, dict):
            return list(images.items())
        return images

    @traitlets.observe("images")
    def _set_file_options(self, change=None):
        """Set the options for the sequence selection dropdown element"""
        if len(self.images) == 0:
            self._file_sel.options = []
            return

        n_figures = int(math.log10(len(self.images)))
        generic_key_pattern = "<{{:0{}}}>".format(n_figures)

        opts = []
        for n, img in enumerate(self.images):
            if isinstance(img, tuple):
                opts.append(img[0])
                continue
            if isinstance(img, str):
                img = Path(str)
            if isinstance(img, Path):
                opts.append("{} ({})".format(img.name, str(img.parent)))
                continue
            opts.append(generic_key_pattern.format(n))
        self._file_sel.options = opts

    def _file_changed(self, change=None):
        """Call-back upon change of the currently selected sequence"""
        if self._cur_image_opened:
            self._cur_image.close()
            self._cur_image_opened = False

        if self._file_sel.value is None:
            # No file selected
            with self._frame_changed_lock:
                self._frame_sel.max = 0
            self._cur_image = None
            self.output = None
            return

        img = self.images[self._file_sel.index]
        if isinstance(img, tuple):
            # TODO: What if there is a tuple of images instead of (key, value)?
            img = img[0]

        if isinstance(img, np.ndarray) and img.ndim == 2:
            # Single image
            img = img[None, ...]
        elif isinstance(img, (str, Path)):
            # Open…
            img = pims.open(str(img))
            self._cur_image_opened = True

        self._cur_image = img

        with self._frame_changed_lock:
            # Disable potential update at this point. Will be explicitly
            # updated below.
            self._frame_sel.max = len(img)

        self._frame_changed()

    def _frame_changed(self, change=None):
        """Call-back upon change of the currently selected frame number"""
        if self._frame_changed_lock.locked():
            return
        self.output = self._cur_image[self._frame_sel.value]
