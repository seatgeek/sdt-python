"""Module containing a class for analyzing and filtering smFRET data"""
from collections import defaultdict
import functools
import numbers
import itertools
from contextlib import contextmanager

import numpy as np
import pandas as pd
from scipy.interpolate import interp1d

from .. import helper, changepoint, config, roi


@contextmanager
def numeric_exc_type(df):
    """Temporarily turn ("fret", "exc_type") column from categorical to int

    This is useful e.g. in :py:func:`helper.split_dataframe` so that the
    resulting split array does not have `object` dtype.

    Expample
    --------
    >>> tracks["fret", "exc_type"].dtype
    CategoricalDtype(categories=['a', 'd'], ordered=False)
    >>> with numeric_exc_type(tracks) as exc_cats:
    >>>     tracks["fret", "exc_type"].dtype
    dtype('int64')
    >>>     exc_cats[0]
    "a"

    ``exc_cats`` is an array that holds old categories. It can be used to find
    out which (new) integer corresponds to which category

    When leaving the ``with`` block, the old categorical column is restored.
    This works only for the original DataFrame, but not for any copies made
    within the block!

    Parameters
    ----------
    df : pandas.DataFrame
        Dataframe for which to temporarily use an integer ("fret", "exc_type")
        column.

    Yields
    ------
    pandas.Index
        Maps integers to categories
    """
    exc_types = df["fret", "exc_type"].copy()
    exc_cats = exc_types.cat.categories.copy()
    exc_types.cat.categories = list(range(len(exc_cats)))
    df["fret", "exc_type"] = exc_types.astype(int)

    try:
        yield exc_cats
    finally:
        exc_types.cat.categories = exc_cats
        df["fret", "exc_type"] = exc_types


class SmFretAnalyzer:
    """Class for analyzing and filtering of smFRET data

    This provides various analysis filtering methods which act on the
    :py:attr:`tracks` attribute.
    """
    @config.set_columns
    def __init__(self, tracks, excitation_seq, cp_detector=None, columns={}):
        """Parameters
        ----------
        tracks : pandas.DataFrame
            smFRET tracking data as produced by :py:class:`SmFretTracker` by
            running its :py:meth:`SmFretTracker.track` method.
        excitation_seq : str or list-like of characters
            Excitation sequence. "d" stands for donor, "a" for acceptor,
            anything else describes other kinds of frames which are to be
            ignored.

            One needs only specify the shortest sequence that is repeated,
            i. e. "ddddaddddadddda" is the same as "dddda".
        cp_detector : changepoint detector or None, optional
            If `None`, create a :py:class:`changepoint.Pelt` instance with
            ``model="l2"``, ``min_size=1``, and ``jump=1``.

        Other parameters
        ----------------
        columns : dict, optional
            Override default column names as defined in
            :py:attr:`config.columns`. Relevant names are `coords` and `time`.
            This means, if your DataFrame has
            coordinate columns "x" and "z" and the time column "alt_frame", set
            ``columns={"coords": ["x", "z"], "time": "alt_frame"}``. This
            parameters sets the :py:attr:`columns` attribute.
        """
        self.tracks = tracks.copy()
        """Filtered smFRET tracking data"""
        self.tracks_orig = tracks.copy()
        """Unfiltered (original) smFRET tracking data"""
        if cp_detector is None:
            cp_detector = changepoint.Pelt("l2", min_size=1, jump=1)
        self.cp_detector = cp_detector
        """Changepoint detector class instance used to perform acceptor
        bleaching detection.
        """

        self.excitation_seq = np.array(list(excitation_seq))

        self.columns = columns
        """dict of column names in DataFrames. Defaults are taken from
        :py:attr:`config.columns`.
        """

    @property
    def excitation_seq(self):
        """pandas.Series of CategoricalDtype describing the excitation
        sequence. Typically, "d" would stand for donor, "a" for acceptor.

        One needs only specify the shortest sequence that is repeated,
        i. e. "ddddaddddadddda" is the same as "dddda".
        """
        return self._exc_seq

    @property
    def excitation_frames(self):
        """dict mapping the excitation types in :py:attr:`excitation_seq` to
        the corresponding frame numbers (modulo the length of
        py:attr:`excitation_seq`).
        """
        return self._exc_frames

    @excitation_seq.setter
    def excitation_seq(self, v):
        self._exc_seq = pd.Series(list(v), dtype="category")
        self._exc_frames = defaultdict(
            lambda : np.empty(0, dtype=int),
            {k: np.nonzero(self._exc_seq == k)[0]
             for k in self._exc_seq.dtype.categories})

    def analyze(self, keep_d_mass=False, invalid_nan=True,
                a_mass_interp="linear"):
        r"""Calculate FRET-related values

        This includes apparent FRET efficiencies, FRET stoichiometries,
        the total brightness (mass) upon donor excitation, and the acceptor
        brightness (mass) upon direct excitation, which is interpolated for
        donor excitation datapoints in order to allow for calculation of
        stoichiometries.

        A column specifying whether the entry originates from donor or
        acceptor excitation is also added: ("fret", "exc_type"). It is 0
        for donor and 1 for acceptor excitation; see the
        :py:meth:`flag_excitation_type` method and
        :py:attr:`exc_type_nums`.

        For each localization in `tracks`, the total brightness upon donor
        excitation is calculated by taking the sum of ``("donor", "mass")``
        and ``("acceptor", "mass")`` values. It is added as a
        ``("fret", "d_mass")`` column to the `tracks` DataFrame. The
        apparent FRET efficiency (acceptor brightness (mass) divided by sum of
        donor and acceptor brightnesses) is added as a
        ``("fret", "eff")`` column to the `tracks` DataFrame.

        The stoichiometry value :math:`S` is given as

        .. math:: S = \frac{F_{DD} + F_{DA}}{F_{DD} + F_{DA} + F_{AA}}

        as in [Upho2010]_. :math:`F_{DD}` is the donor brightness upon donor
        excitation, :math:`F_{DA}` is the acceptor brightness upon donor
        excitation, and :math:`F_{AA}` is the acceptor brightness upon
        acceptor excitation. The latter is calculated by interpolation for
        frames with donor excitation.

        :math:`F_{AA}` is append as a ``("fret", "a_mass")`` column.
        The stoichiometry value is added in the ``("fret", "stoi")`` column.

        Parameters
        ----------
        tracks : pandas.DataFrame
            smFRET tracking data as produced by the
            :py:meth:`SmFretTracker.track`
        keep_d_mass : bool, optional
            If a ``("fret", "d_mass")`` column is already present in `tracks`,
            use that instead of overwriting it with the sum of
            ``("donor", "mass")`` and ``("acceptor", "mass")`` values. Useful
            if :py:meth:`track` was called with ``d_mass=True``.
        invalid_nan : bool, optional
            If True, all "d_mass", "eff", and "stoi" values for excitation
            types other than donor excitation are set to NaN, since the values
            don't make sense. Defaults to True.
        a_mass_interp : {"linear", "nearest"}, optional
            How to interpolate the acceptor mass upon direct excitation in
            donor excitation frames. Defaults to "linear".
        """
        self.tracks.sort_values(
            [("fret", "particle"), ("donor", self.columns["time"])],
            inplace=True)

        # Excitation type, needed below
        self.flag_excitation_type()

        # Calculate brightness upon acceptor excitation. This requires
        # interpolation
        cols = [("donor", self.columns["mass"]),
                ("acceptor", self.columns["mass"]),
                ("donor", self.columns["time"]),
                ("fret", "exc_type")]
        if ("fret", "has_neighbor") in self.tracks.columns:
            cols.append(("fret", "has_neighbor"))
            has_nn = True
        else:
            has_nn = False

        a_mass = []
        with numeric_exc_type(self.tracks) as exc_cats:
            for p, t in helper.split_dataframe(
                    self.tracks, ("fret", "particle"), cols, sort=False):
                # Direct acceptor excitation
                ad_p_mask = (t[:, 3] == np.nonzero(exc_cats == "a")[0])
                # Locs without neighbors
                if has_nn:
                    nn_p_mask = ~t[:, -1].astype(bool)
                else:
                    nn_p_mask = np.ones(len(t), dtype=bool)
                # Only use locs with direct accept ex and no neighbors
                a_direct = t[ad_p_mask & nn_p_mask, 1:3]

                if len(a_direct) == 0:
                    # No direct acceptor excitation, cannot do anything
                    a_mass.append(np.full(len(t), np.NaN))
                    continue
                elif len(a_direct) == 1:
                    # Only one direct acceptor excitation; use this value for
                    # all data points of this particle
                    a_mass.append(np.full(len(t), a_direct[0, 0]))
                    continue
                else:
                    # Enough direct acceptor excitations for interpolation
                    # Values are sorted.
                    y, x = a_direct.T
                    a_mass_func = interp1d(
                        x, y, a_mass_interp, copy=False,
                        fill_value=(y[0], y[-1]), assume_sorted=True,
                        bounds_error=False)
                    # Calculate (interpolated) mass upon direct acceptor
                    # excitation
                    a_mass.append(a_mass_func(t[:, 2]))
        a_mass = np.concatenate(a_mass)

        # Total mass upon donor excitation
        if keep_d_mass and ("fret", "d_mass") in self.tracks.columns:
            d_mass = self.tracks["fret", "d_mass"].copy()
        else:
            d_mass = (self.tracks["donor", self.columns["mass"]] +
                      self.tracks["acceptor", self.columns["mass"]])

        with np.errstate(divide="ignore", invalid="ignore"):
            # ignore divide by zero and 0 / 0
            # FRET efficiency
            eff = self.tracks["acceptor", self.columns["mass"]] / d_mass
            # FRET stoichiometry
            stoi = d_mass / (d_mass + a_mass)

        if invalid_nan:
            # For direct acceptor excitation, FRET efficiency and stoichiometry
            # are not sensible
            nd_mask = (self.tracks["fret", "exc_type"] != "d")
            eff[nd_mask] = np.NaN
            stoi[nd_mask] = np.NaN
            d_mass[nd_mask] = np.NaN

        self.tracks["fret", "eff"] = eff
        self.tracks["fret", "stoi"] = stoi
        self.tracks["fret", "d_mass"] = d_mass
        self.tracks["fret", "a_mass"] = a_mass
        self.tracks.reindex(columns=self.tracks.columns.sortlevel(0)[0])

    def flag_excitation_type(self):
        """Add a column indicating excitation type (donor/acceptor/...)

        Add  ("fret", "exc_type") column. It is of "category" type.
        """
        frames = self.tracks["acceptor", self.columns["time"]]
        self.tracks["fret", "exc_type"] = self.excitation_seq[
             frames % len(self.excitation_seq)].values

    def segment_a_mass(self, **kwargs):
        """Segment tracks by changepoint detection in the acceptor mass

        Changepoint detection is run on the acceptor brightness time trace.
        This appends py:attr:`tracks` with a ``("fret", "a_seg")`` column. For
        each localization, this holds the number of the segment it belongs to.

        **:py:attr:`tracks` will be sorted according to
        ``("fret", "particle")`` and ``("donor", self.columns["time"])`` in the
        process.**

        Parameters
        ----------
        **kwargs
            Keyword arguments to pass to :py:attr:`cp_detector`
            `find_changepoints` method.

        Examples
        --------
        Pass ``penalty=1e6`` to the changepoint detector's
        ``find_changepoints`` method.

        >>> ana.segment_a_mass(penalty=1e6)
        """
        time_col = ("donor", self.columns["time"])
        self.tracks.sort_values([("fret", "particle"), time_col], inplace=True)

        with numeric_exc_type(self.tracks) as exc_cats:
            trc_split = helper.split_dataframe(
                self.tracks, ("fret", "particle"),
                [("fret", "a_mass"), time_col, ("fret", "exc_type")],
                type="array", sort=False)

            acc_exc_num = np.nonzero(exc_cats == "a")[0]

            segments = []
            for p, trc_p in trc_split:
                acc_mask = trc_p[:, 2] == acc_exc_num
                m_a = trc_p[acc_mask, 0]
                m_a_pos = np.nonzero(acc_mask)[0]

                # Find changepoints if there are no NaNs
                if np.any(~np.isfinite(m_a)):
                    segments.append(np.full(len(trc_p), -1))
                    continue
                cp = self.cp_detector.find_changepoints(m_a, **kwargs)
                if not len(cp):
                    segments.append(np.zeros(len(trc_p)))
                    continue

                # Number the segments
                seg = np.empty(len(trc_p), dtype=int)
                # Move changepoint forward to right after the previous acceptor
                # frame, meaning all donor frames between that and the
                # changepoint already belong to the new segment.
                cp_pos = m_a_pos[np.maximum(np.add(cp, -1), 0)] + 1
                for i, s, e in zip(itertools.count(),
                                itertools.chain([0], cp_pos),
                                itertools.chain(cp_pos, [len(trc_p)])):
                    seg[s:e] = i

                segments.append(seg)

        self.tracks["fret", "a_seg"] = np.concatenate(segments)

    def acceptor_bleach_step(self, brightness_thresh, truncate=True):
        """Find tracks where the acceptor bleaches in a single step

        After acceptor mass changepoint detection has been performed (see
        :py:meth:`SmFretAnalyzer.segment_a_mass`), this method can be used
        to filter out any trajectories where the acceptor does not bleach in
        a single step.

        Only if the median brightness for each but the first step is below
        `brightness_thresh`, accept the track.

        Parameters
        ----------
        brightness_thresh : float
            Consider acceptor bleached if brightness ("fret", "a_mass") median
            is below this value.
        truncate : bool, optional
            If `True`, remove data after the bleach step.

        Examples
        --------
        Consider acceptors with a brightness ("fret", "a_mass") of less than
        500 counts bleached.

        >>> filt.acceptor_bleach_step(500)
        """
        time_col = ("donor", self.columns["time"])
        self.tracks.sort_values([("fret", "particle"), time_col], inplace=True)

        with numeric_exc_type(self.tracks) as exc_cats:
            trc_split = helper.split_dataframe(
                self.tracks, ("fret", "particle"),
                [("fret", "a_mass"), ("fret", "exc_type"), ("fret", "a_seg")],
                type="array", sort=False)

            acc_exc_num = np.nonzero(exc_cats == "a")[0]

            good = []
            for p, trc_p in trc_split:
                # Make step function
                cps = np.nonzero(np.diff(trc_p[:, 2]))[0] + 1  # changepoints
                split = np.array_split(trc_p[:, (0, 1)], cps)
                med = [np.median(s[s[:, 1] == acc_exc_num, 0]) for s in split]

                # See if only the first step is above brightness_thresh
                if len(med) > 1 and all(m < brightness_thresh
                                        for m in med[1:]):
                    if truncate:
                        # Add data before bleach step
                        g = np.zeros(len(trc_p), dtype=bool)
                        g[:cps[0]] = True
                    else:
                        g = np.ones(len(trc_p), dtype=bool)
                else:
                    g = np.zeros(len(trc_p), dtype=bool)

                good.append(g)

        self.tracks = self.tracks[np.concatenate(good)]

    def eval(self, expr, mi_sep="_"):
        """Call ``eval(expr)`` for `tracks`

        Flatten the column MultiIndex and call the resulting DataFrame's
        `eval` method.

        Parameters
        ----------
        expr : str
            Argument for eval. See :py:meth:`pandas.DataFrame.eval` for
            details.

        Returns
        -------
        pandas.Series, dtype(bool)
            Boolean Series indicating whether an entry fulfills `expr` or not.

        Examples
        --------
        Get a boolean array indicating lines where ("fret", "a_mass") <= 500
        in :py:attr:`tracks`

        >>> filt.eval("fret_a_mass > 500")
        0     True
        1     True
        2    False
        dtype: bool

        Other parameters
        ----------------
        mi_sep : str, optional
            Use this to separate levels when flattening the column
            MultiIndex. Defaults to "_".
        """
        if not len(self.tracks):
            return pd.Series([], dtype=bool)

        old_columns = self.tracks.columns
        try:
            self.tracks.columns = helper.flatten_multiindex(old_columns,
                                                            mi_sep)
            e = self.tracks.eval(expr)
        except Exception:
            raise
        finally:
            self.tracks.columns = old_columns

        return e

    def query(self, expr, mi_sep="_"):
        """Filter features according to column values

        Flatten the column MultiIndex and filter the resulting DataFrame's
        `eval` method.

        Parameters
        ----------
        expr : str
            Filter expression. See :py:meth:`pandas.DataFrame.eval` for
            details.

        Examples
        --------
        Remove lines where ("fret", "a_mass") <= 500 from :py:attr:`tracks`

        >>> filt.query("fret_a_mass > 500")

        Other parameters
        ----------------
        mi_sep : str, optional
            Use this to separate levels when flattening the column
            MultiIndex. Defaults to "_".
        """
        self.tracks = self.tracks[self.eval(expr, mi_sep)]

    def filter_particles(self, expr, min_count=1, mi_sep="_"):
        """Remove particles that don't fulfill `expr` enough times

        Any particle that does not fulfill `expr` at least `min_count` times
        is removed from :py:attr:`tracks`.

        The column MultiIndex is flattened for this purpose.

        Parameters
        ----------
        expr : str
            Filter expression. See :py:meth:`pandas.DataFrame.eval` for
            details.
        min_count : int, optional
            Minimum number of times a particle has to fulfill expr. If
            negative, this means "all but ``abs(min_count)``". If 0, it has
            to be fulfilled in all frames.

        Examples
        --------
        Remove any particles where not ("fret", "a_mass") > 500 at least twice
        from :py:attr:`tracks`.

        >>> # acceptor mass has to be > 500 in at least 2 frames
        >>> filt.filter_particles("fret_a_mass > 500", 2)
        >>> # acceptor mass may be <= 500 in no more than one frame
        >>> filt.filter_particles("fret_a_mass > 500", -1)

        Other parameters
        ----------------
        mi_sep : str, optional
            Use this to separate levels when flattening the column
            MultiIndex. Defaults to "_".
        """
        e = self.eval(expr, mi_sep)
        p = self.tracks.loc[e, ("fret", "particle")].values
        p, c = np.unique(p, return_counts=True)
        if min_count <= 0:
            p2 = self.tracks.loc[self.tracks["fret", "particle"].isin(p),
                                 ("fret", "particle")].values
            min_count = np.unique(p2, return_counts=True)[1] + min_count
        good_p = p[c >= min_count]
        self.tracks = self.tracks[self.tracks["fret", "particle"].isin(good_p)]

    def image_mask(self, mask, channel):
        """Filter using a boolean mask image

        Remove all lines where coordinates lie in a region where `mask` is
        `False`.

        Parameters
        ----------
        mask : numpy.ndarray, dtype(bool) or list of (key, numpy.ndarray)
            Mask image(s). If this is a single array, apply it to the whole
            :py:attr:`tracks` DataFrame. This can also be a list of
            (key, mask), in which case each mask is applied separately to
            ``self.tracks.loc[key]``.
        channel : {"donor", "acceptor"}
            Channel to use for the filtering

        Examples
        --------
        Create a 2D boolean mask to remove any features that do not have
        x and y coordinates between 50 and 100 in the donor channel.

        >>> mask = numpy.zeros((200, 200), dtype=bool)
        >>> mask[50:100, 50:100] = True
        >>> filt.image_mask(mask, "donor")

        If :py:attr:`tracks` has a MultiIndex index, where e.g. the first
        level is "file1", "file2", … and different masks should be applied
        for each file, this is possible by passing a list of
        (key, mask) pairs.

        >>> masks = [("file%i" % i, numpy.ones((10*i, 10*i), dtype=bool))
        ...          for i in range(1, 11)]
        >>> filt.image_mask(masks, "donor")
        """
        cols = {"coords": [(channel, c) for c in self.columns["coords"]]}

        if isinstance(mask, np.ndarray):
            r = roi.MaskROI(mask)
            self.tracks = r(self.tracks, columns=cols)
        else:
            ret = []
            for k, v in mask:
                try:
                    r = roi.MaskROI(v)
                    m = r(self.tracks.loc[k], columns=cols)
                except KeyError:
                    # No tracking data for the current image
                    continue
                ret.append((k, m))
            self.tracks = pd.concat([r[1] for r in ret],
                                    keys=[r[0] for r in ret])

    def reset(self):
        """Undo any filtering

        Reset :py:attr:`tracks` to the initial state.
        """
        self.tracks = self.tracks_orig.copy()
