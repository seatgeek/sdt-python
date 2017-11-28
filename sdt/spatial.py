"""Functions dealing with the spacial aspect of data"""
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree


_pos_columns = ["x", "y"]


def _has_near_neighbor_impl(data, r):
    """Implementation of finding near neighbors using KD trees

    Parameters
    ----------
    data : array-like, shape(n, m)
        n data points of dimension m
    r : float
        Maximum distance for data points to be considered near neighbors

    Returns
    -------
    numpy.ndarray, shape(n)
        For each data point this is 1 if it has neighbors closer than `r` and
        0 if it has not.
    """
    # Find data points with near neighbors
    t = cKDTree(data)
    nn = np.unique(t.query_pairs(r, output_type="ndarray"))
    # Record those data points
    hn = np.zeros(len(data), dtype=int)
    hn[nn] = 1
    return hn


def has_near_neighbor(data, r, pos_columns=_pos_columns):
    """Check whether localized features have near neighbors

    Given a :py:class:`pandas.DataFrame` `data` with localization data, each
    data point is checked whether other points (in the same frame) are closer
    than `r`.

    The results will be written in a "has_neighbor" column of the `data`
    DataFrame.

    Parameters
    ----------
    data : pandas.DataFrame
        Localization data. The "has_neighbor" column will be
        appended/overwritten with the results.
    r : float
        Maximum distance for data points to be considered near neighbors.

    Other parameters
    ----------------
    pos_colums : list of str, optional
        Names of the columns describing the x and the y coordinate of the
        features in :py:class:`pandas.DataFrame`s. Defaults to ["x", "y"].
    """
    if not len(data):
        data["has_neighbor"] = []
        return
    if "frame" in data.columns:
        data_arr = data[pos_columns + ["frame"]].values

        # Sort so that `diff` works below
        sort_idx = np.argsort(data_arr[:, -1])
        data_arr = data_arr[sort_idx]

        # Split data according to frame number
        frame_bounds = np.nonzero(np.diff(data_arr[:, -1]))[0] + 1
        data_split = np.split(data_arr[:, :-1], frame_bounds)

        # List of array of indices of data points with near neighbors
        has_neighbor = np.concatenate([_has_near_neighbor_impl(s, r)
                                       for s in data_split])

        # Get the reverse of sort_idx s. t. all(x[sort_idx][rev_sort_idx] == x)
        ran = np.arange(len(data_arr), dtype=int)
        rev_sort_idx = np.empty_like(ran)
        rev_sort_idx[sort_idx] = ran

        # Undo sorting
        has_neighbor = has_neighbor[rev_sort_idx]
    else:
        has_neighbor = _has_near_neighbor_impl(data[pos_columns], r)

    # Append column to data frame
    data["has_neighbor"] = has_neighbor


def interpolate_coords(tracks, pos_columns=_pos_columns):
    """Interpolate coordinates for missing localizations

    For each particle in `tracks`, interpolate coordinates for frames
    where no localization was detected.

    Parameters
    ----------
    tracks : pandas.DataFrame
        Tracking data

    Returns
    -------
    pandas.DataFrame
        Tracking data with missing frames interpolated. An "interp" column
        is added. If False, the localization was detected previously. If
        True, it was added via interpolation by this method.

    Other parameters
    ----------------
    pos_colums : list of str, optional
        Names of the columns describing the x and the y coordinate of the
        features in :py:class:`pandas.DataFrames`. Defaults to ["x", "y"].
    """
    tracks = tracks.copy()
    arr = tracks[pos_columns + ["particle", "frame"]].values
    particles = np.unique(arr[:, -2])
    missing_coords = []
    missing_fno = []
    missing_pno = []
    for p in particles:
        a = arr[arr[:, -2] == p]  # get particle p
        a = a[np.argsort(a[:, -1])]  # sort according to frame number
        frames = a[:, -1].astype(np.int)  # frame numbers
        # get missing frame numbers
        miss = list(set(range(frames[0], frames[-1]+1)) - set(frames))
        miss = np.array(miss, dtype=np.int)

        coords = []
        for c in a[:, :-2].T:
            # for missing frames interpolate each coordinate
            x = np.interp(miss, frames, c)
            coords.append(x)
        missing_coords.append(np.column_stack(coords))
        missing_pno.append(np.full(len(miss), p, dtype=np.int))
        missing_fno.append(miss)

    if not missing_coords:
        tracks["interp"] = 0
        ret = tracks.sort_values(["particle", "frame"])
        return tracks.reset_index(drop=True)

    missing_coords = np.concatenate(missing_coords)
    missing_fno = np.concatenate(missing_fno)
    missing_pno = np.concatenate(missing_pno)
    missing_df = pd.DataFrame(missing_coords, columns=pos_columns)
    missing_df["particle"] = missing_pno
    missing_df["frame"] = missing_fno
    # Don't use bool below. Otherwise, the `values` attribute of the DataFrame
    # will have "object" dtype.
    missing_df["interp"] = 1
    tracks["interp"] = 0

    ret = pd.merge(tracks, missing_df, "outer")
    ret.sort_values(["particle", "frame"], inplace=True)
    return ret.reset_index(drop=True)
