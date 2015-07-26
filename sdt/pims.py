# -*- coding: utf-8 -*-
"""PIMS plugins for image sequences created by SDT software"""

import logging
from datetime import datetime
import json
from contextlib import suppress
from collections import OrderedDict
import base64

import dateutil

import numpy as np
import pims

_logger = logging.getLogger(__name__)

SdtComments = {
    #format of dict entries:
    # name: (comment_number, slice, converter, [factor, optional])
    #
    # relevant_characters = metadata["comments"][comment_number][slice]
    # data = converter(relevant_characters)
    # metadata[name] = data*factor #if factor is given
    "SDTcontrol major version": (4, slice(66, 68), int),
    "SDTcontrol minor version": (4, slice(68, 70), int),
    "SDT controller name": (4, slice(0, 6), str),
    "exposure time": (1, slice(64, 73), float, 10**-6),
    "color code": (4, slice(10, 14), str),
    "detection channels": (4, slice(15, 16), int),
    "background subtraction": (4, 14, lambda x: x == "B"),
    "EM active": (4, 32, lambda x: x == "E"),
    "EM gain": (4, slice(28,32), int),
    "laser modulation": (4, 33, lambda x: x == "A"),
    "pixel size": (4, slice(25, 28), float, 0.1),
    "method": (4, slice(6,10), str),
    "grid": (4, slice(16,25), float, 10**-6),
    "N macro": (1, slice(0, 4), int),
    "delay macro": (1, slice(10, 19), float, 10**-3),
    "N mini": (1, slice(4, 7), int),
    "delay mini": (1, slice(19, 28), float, 10**-6),
    "N micro": (1, slice(7, 10), int),
    "delay micro": (1, slice(28, 37), float, 10**-6),
    "subpics": (1, slice(7, 10), int),
    "shutter delay": (1, slice(73, 79), float, 10**-6),
    "prebleach delay": (1, slice(37, 46), float, 10**-6),
    "bleach time": (1, slice(46, 55), float, 10**-6),
    "recovery time": (1, slice(55,64), float, 10**-6)
}

months = {
    #Convert SDTcontrol month strings to month numbers
    "Jan": 1, "Feb": 2, "Mär": 3, "Mar": 3, "Apr": 4, "Mai": 5, "May": 5,
    "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Okt": 10, "Oct": 10, "Nov": 11,
    "Dez": 12, "Dec": 12
}

methods = {
    #TODO: complete
    "SEQU": "Sequence", "SETO": "Sequence TOCCSL", "KINE": "Kinetics",
    "SEAR": "Sequence arbitrary"
}

class SdtSpeStack(pims.SpeStack):
    """Specialized version of pims.SpeStack for SDTcontrol

    If special metadata written by SDTcontrol is found, it will be decoded to
    something human-readable. Also, there is the option to split the large
    kinetics mode images into smaller subimages.

    The class_priority is set to 20, so that importing `sdt.pims` should be
    enough to make `pims.open`() automatically select this class for reading
    SPE files.

    Attributes:
        metadata (dict): Contains SDTcontrol metadata
    """
    class_priority = 20 #>10, so use instead of SpeStack

    def __init__(self, filename, process_func=None, dtype=None, as_grey=False,
                 char_encoding="latin1", split_kinetics=True):
        """Create an iterable object that returns image data as numpy arrays

        Args:
            filename (str): Name of the SPE file
            process_func (callable, optional): Takes one image array as its
                sole argument. It is applied to each image. Defaults to None.
            dtype (numpy.dtype, optional): Which data type to convert the
                images too. No conversion if None. Defaults to None.
            as_grey (bool, optional): Convert image to greyscale. Do not use
                in conjunction with process_func. Defaults to False.
            char_encoding (str, optional): Specifies what character encoding
                is used for metatdata strings. Defaults to "latin1".
            split_kinetics (bool, optional): Whether to split the large
                kinetics mode images into smaller subimages. Defaults to True.
        """
        super().__init__(filename, process_func, dtype, as_grey, char_encoding)

        #Parse SDTcontrol comments
        comments = self.metadata["comments"]
        if comments[4][70:] == "COMVER0500":
            for name, spec in SdtComments.items():
                try:
                    v = spec[2](comments[spec[0]][spec[1]])
                    if len(spec) >= 4:
                        v *= spec[3]
                    self.metadata[name] = v
                except:
                    pass
            comment = comments[0] + comments[2]
            self.metadata["comment"] = comment.strip()
            self.metadata.pop("comments", None)
        else:
            _logger.info("SDTcontrol comments not found.")

        #Get date and time in a usable format
        date = self.metadata["date"]
        time = self.metadata["ExperimentTimeLocal"]
        try:
            month = months[date[2:5]]
            self.metadata["DateTime"] = datetime(
                int(date[5:9]), month, int(date[0:2]), int(time[0:2]),
                int(time[2:4]), int(time[4:6]))
        except:
            _logger.info("Decoding of date failed.")
        self.metadata.pop("date", None)
        self.metadata.pop("ExperimentTimeLocal", None)

        #Rename this
        self.metadata["laser mod script"] = self.metadata["spare4"].decode(
            char_encoding)
        self.metadata.pop("spare4", None)
        if self.metadata.get("readoutMode", "") != "kinetics":
            self.metadata.pop("WindowSize", None)

        #Get rid of unused data
        self.metadata.pop("ExperimentTimeUTC", None)
        self.metadata.pop("exp_sec", None)

        #Necessary to split kinetics mode images
        self._is_kinetics = (split_kinetics &
            (self.metadata.get("readoutMode", "") == "kinetics"))
        if self._is_kinetics:
            self._subpic_height = self.metadata["WindowSize"]
            self._no_subpics = round(
                super().frame_shape[1]/self._subpic_height)
        else:
            self._subpic_height = super().frame_shape[1]
            self._no_subpics = 1

    @property
    def frame_shape(self):
        return super().frame_shape[0], self._subpic_height

    def __len__(self):
        return super().__len__()*self._no_subpics

    def get_frame(self, j):
        if self._is_kinetics:
            full = super().get_frame(int(j/self._no_subpics))
            start_row = (j % self._no_subpics)*self._subpic_height
            return pims.Frame(full[start_row:start_row+self._subpic_height, :],
                         frame_no=j, metadata=self.metadata)
        else:
            return super().get_frame(j)


class SdtTiffStack(pims.TiffStack):
    """Version of pims.TiffStack extended for SDT needs

    This tries to read metadata that has been serialized as JSON using
    `save_as_tiff`().

    The class_priority is set to 20, so that importing `sdt.pims` should be
    enough to make `pims.open`() automatically select this class for reading
    TIFF files.
    """
    class_priority = 20 #>10, so use instead of any builtin TIFF reader

    def get_frame(self, j):
        f = super().get_frame(j)
        md = {}
        with suppress(Exception):
            md = json.loads(f.metadata["ImageDescription"],
                            object_pairs_hook=OrderedDict)
        if md:
            f.metadata.pop("ImageDescription", None)
            #restore resemblence with original (pre-JSON) data type
            with suppress(Exception):
                md["DateTime"] = dateutil.parser.parse(md["DateTime"])
            with suppress(Exception):
                rs = md["ROIs"]
                dtype = dict(names=list(rs[0].keys()),
                             formats=["<H"]*len(rs[0]))
                md["ROIs"] = np.rec.fromrecords(
                    [list(r.values()) for r in rs], dtype=dtype)
            with suppress(Exception):
                md["spare4"] = base64.b64decode(md["spare4"].encode("latin1"))
        f.metadata.update(md)
        return f