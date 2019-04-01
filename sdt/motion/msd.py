"""Analyze mean square displacements (MSDs)"""
from collections import OrderedDict
import math
import types
import warnings

import pandas as pd
import numpy as np
import scipy.optimize

from .. import config, helper


def _displacements(particle_data, n_lag, disp_list):
    """Do the actual calculation of displacements

    Calculate all possible displacements for each coordinate and each lag time
    for a single particle.

    Parameters
    ----------
    particle_data : numpy.ndarray
        First column is the frame number, the other columns are particle
        coordinates. Has to be sorted according to ascending frame number.
    n_lag : int
        Maximum number of time lags to consider.
    disp_list : list of lists of numpy.arrays, shape(m, n)
        Results will be appended to the list. For the i-th lag time, the
        displacements (a 2D numpy.ndarray where each column stands for a
        coordinate and each row for one displacement data set) will be appended
        to the i-th list entry. If the entry does not exist, it will be
        created.

        After calling this function for each particle, the items of
        disp_list can for example turned into one large array containing all
        coordinate displacements using :py:func:`numpy.concatenate`.
    """
    ndim = particle_data.shape[1] - 1

    # fill gaps with NaNs
    frames = np.round(particle_data[:, 0]).astype(int)
    start = frames[0]
    end = frames[-1]
    pdata = np.full((end - start + 1, ndim), np.NaN)
    pdata[frames - start] = particle_data[:, 1:]

    # there can be at most len(pdata) - 1 steps
    n_lag = round(min(len(pdata)-1, n_lag))

    for i in range(1, n_lag + 1):
        # calculate coordinate differences for each time lag
        disp = pdata[i:] - pdata[:-i]
        try:
            disp_list[i-1].append(disp)
        except IndexError:
            if i - 1 != len(disp_list):
                # something is really wrong — this should never happen
                raise RuntimeError("Displacement list index skipped.")
            disp_list.append([disp])


def _square_displacements(disp_list):
    """Calculate square displacements

    Parameters
    ----------
    disp_list : list
        Coordinate displacements as generated by :py:func:`_displacements`.

    Returns
    -------
    list of numpy.ndarrays, shape(n)
        The i-th list entry is the 1D-array of square displacements for the
        i-th lag time.
    """
    sd_list = []
    for d in disp_list:
        # For each time lag, concatenate the coordinate differences
        # Check if concatenation is necessary for performance gain
        if len(d) == 1:
            d = d[0]
        else:
            d = np.concatenate(d)
        # calculate square displacements
        d = np.sum(d**2, axis=1)
        # get rid of NaNs from gaps
        d = d[~np.isnan(d)]
        sd_list.append(d)
    return sd_list


class Msd:
    """Calculate and analyze mean square displacements (MSDs)

    from single moleclue tracking data.
    """
    @config.set_columns
    def __init__(self, data, frame_rate, n_lag=100, n_boot=100, ensemble=True,
                 e_name="ensemble", random_state=None, columns={}):
        """Parameters
        ----------
        data : pandas.DataFrame or iterable of pandas.DataFrame
            Tracking data. Either a single DataFrame or a collection of
            DataFrames.
        frame_rate : float
            Frames rate
        n_lag : int or inf, optional
            Number of lag times (time steps) to consider at most. Defaults to
            100.
        n_boot : int, optional
            Number of bootstrapping iterations for calculation of errors.
            Set to 0 to turn off bootstrapping for performance gain, in which
            case there will be no errors on the fit results. Defaults to 100.
        ensemble : bool, optional
            Whether to calculate the MSDs for the whole data set or for each
            trajectory individually. Defaults to True.

        Other parameters
        ----------------
        e_name : str, optional
            If the `ensemble` parameter is `True`, use this as the name for
            the dataset. It shows up in the MSD DataFrame (see
            :py:meth:`get_msd`) and in plots. Defaults to "ensemble".
        random_state : numpy.random.RandomState or None, optional
            :py:class:`numpy.random.RandomState` instance to use for
            random sampling for bootstrapping. If `None`, use create a new
            instance with ``seed=None``. Defaults to `None`.
        columns : dict, optional
            Override default column names as defined in
            :py:attr:`config.columns`. Relevant names are `coords`, `particle`,
            and `time`. This means, if your DataFrame has coordinate columns
            "x", "y", and "z" and the time column "alt_frame", set
            ``columns={"coords": ["x", "y", "z"], "time": "alt_frame"}``.
        """
        self._omit_file_label = False
        if isinstance(data, pd.DataFrame):
            data = {0: data}
            # Only one file, there is no need for a MultiIndex and stuff
            self._omit_file_label = True
        elif not isinstance(data, dict):
            data = OrderedDict([(i, d) for i, d in enumerate(data)])

        # Calculate square displacements
        square_disp = OrderedDict()
        ensemble_disp_list = []
        for file, trc in data.items():
            trc_sorted = trc.sort_values([columns["particle"],
                                          columns["time"]])
            trc_split = helper.split_dataframe(
                trc_sorted, columns["particle"],
                [columns["time"]] + columns["coords"], sort=False)
            if ensemble:
                for p, trc_p in trc_split:
                    _displacements(trc_p, n_lag, ensemble_disp_list)
            else:
                for p, trc_p in trc_split:
                    disp_list = []
                    _displacements(trc_p, n_lag, disp_list)
                    if self._omit_file_label:
                        key = p
                    else:
                        key = (file, p)
                    square_disp[key] = _square_displacements(disp_list)
        if ensemble:
            square_disp[e_name] = _square_displacements(ensemble_disp_list)

        # Generate bootstrapped data if desired
        if n_boot > 1:
            if random_state is None:
                random_state = np.random.RandomState()
            self._msd_set = OrderedDict()
            for p, sds_p in square_disp.items():
                msds_p = np.empty((len(sds_p), n_boot))
                for i, sd in enumerate(sds_p):
                    if len(sd) > 0:
                        b = random_state.choice(sd, (len(sd), n_boot),
                                                replace=True)
                        m = np.mean(b, axis=0)
                    else:
                        # No data for this lag time
                        m = np.NaN
                    msds_p[i, :] = m
                self._msd_set[p] = msds_p
            self._msds = OrderedDict([(p, np.mean(m, axis=1))
                                      for p, m in self._msd_set.items()])
            # Use corrected sample std as a less biased estimator of the
            # population  std
            self._err = OrderedDict([(p, np.std(m, axis=1, ddof=1))
                                     for p, m in self._msd_set.items()])
        else:
            self._msds = OrderedDict([
                (p, np.array([np.mean(v) if len(v) > 0 else np.NaN
                              for v in s]))
                for p, s in square_disp.items()])
            # Use corrected sample std as a less biased estimator of the
            # population  std
            self._err = OrderedDict([
                (p, np.array([np.std(v, ddof=1) / np.sqrt(len(v))
                              if len(v) > 1 else np.NaN
                              for v in s]))
                for p, s in square_disp.items()])
            self._msd_set = OrderedDict([(p, m[:, None])
                                         for p, m in self._msds.items()])

        self.frame_rate = frame_rate

    @classmethod
    def _from_data(cls, data, e_name="ensemble"):
        """Create class instance from pre-calculated data

        With this, it is possible to create a class instance from legacy
        data as created by :py:func:`emsd`.

        For legacy interop purposes only.

        Parameters
        ----------
        data : pandas.DataFrame
            Input data
        e_name : str, optional
            Name to be given to the input dataset. Defaults to "ensemble".

        Returns
        -------
        class instance
            Instance create from `data`
        """
        ret = cls([], 1, 1, 0)
        if isinstance(data, pd.DataFrame):
            if "lagt" in data.columns and "msd" in data.columns:
                # old `emsd` function output
                msds = data["msd"].values
                if "stderr" in data.columns:
                    err = data["stderr"].values
                else:
                    err = np.full_like(msds, np.NaN)
                ret._msds = OrderedDict([(e_name, msds)])
                ret._err = OrderedDict([(e_name, err)])
                ret._msd_set = OrderedDict([(e_name, msds[:, None])])

                lt0, lt1 = data["lagt"].iloc[:2]
                ret.frame_rate = 1 / (lt1 - lt0)

                return ret
        raise ValueError("data in unrecognized format")

    def _get_lagtimes(self, n):
        """Get first `n` lag times

        Parameters
        ----------
        n : int
            Number of lag times

        Returns
        -------
        numpy.ndarray, shape(n)
            Lag times in ascending order
        """
        return np.arange(1, n + 1) / self.frame_rate

    def get_msd(self):
        """Get MSD and error DataFrames

        The columns contain data for different lag times. Each row corresponds
        to one trajectory. The row index is either the particle number if a
        single DataFrame was passed to :py:meth:`__init__` or a tuple
        identifying both the DataFrame and the particle. If
        :py:meth:`__init__` was called with ``ensemble=True``, there will only
        be one column with index `e_name`.

        Returns
        -------
        msds : pandas.DataFrame
            Mean square displacements
        err : pandas.DataFrame
            Standard errors of the mean square displacements. If
            bootstrapping was used, these are the standard deviations of the
            MSD results from bootstrapping. Otherwise, these are caleculated
            as the standard deviation of square displacements divided by the
            number of samples.
        """
        msds = pd.DataFrame(list(self._msds.values()))
        msds.index = pd.Index(self._msds.keys())
        err = pd.DataFrame(list(self._err.values()))
        err.index = pd.Index(self._err.keys())
        if isinstance(msds.index, pd.MultiIndex):
            msds.index.names = ("file", "particle")
            err.index.names = ("file", "particle")
        else:
            msds.index.name = "particle"
            err.index.name = "particle"
        cols = pd.Index(self._get_lagtimes(msds.shape[1]), name="lagt")
        msds.columns = err.columns = cols
        return msds, err

    def fit(self, model, *args, **kwargs):
        """Fit a model function to the MSD data

        Parameters
        ----------
        model : {"anomalous", "brownian"}
            Type of model to fit
        n_lag : int or inf, optional
            Number of lag times to use for fitting. Defaults to 2 for the
            Brownian model and `inf` for anomalous diffusion.
        exposure_time : float, optional
            Exposure time. Defaults to 0, i.e. no exposure time compensation
        initial : tuple of float, len 3, optional
            Initial guess for fitting anomalous diffusion. The tuple entries
            are diffusion coefficient, positional accuracy, and alpha.
        """
        if not isinstance(model, str):
            return model(self, *args, **kwargs)
        model = model.lower()
        if model.startswith("anomalous"):
            return AnomalousDiffusion(self, *args, **kwargs)
        if model.startswith("brownian"):
            return BrownianMotion(self, *args, **kwargs)

        raise ValueError("Unknown model: " + model)


class AnomalousDiffusion:
    """Fit anomalous diffusion parameters to MSD values

    Fit a function :math:`msd(t_\text{lag}) = 4*D*t_text{lag}^\alpha + 4*pa**2`
    to the tlag-vs.-MSD graph, where :math:`D` is the diffusion coefficient,
    :math:`pa` is the positional accuracy (uncertainty), and :math:`alpha`
    the anomalous diffusion exponent.
    """
    _fit_parameters = ["D", "PA", "alpha"]

    def __init__(self, msd, n_lag=np.inf, exposure_time=0.,
                 initial=(0.5, 0.05, 1.)):
        """Parameters
        ----------
        msd : MSD
            :py:class:`MSD` instance to get MSD information from
        n_lag : int or inf, optional
            Maximum number of lag times to use for fitting. Defaults to
            `inf`, i.e. using all.
        exposure_time : float, optional
            Exposure time. Defaults to 0, i.e. no exposure time correction
        initial : tuple of float, optional
            Initial guesses for the fitting for :math:`D`, :math:`pa`, and
            :math:`alpha`. Defaults to ``(0.5, 0.05, 1.)``.
        """
        def residual(x, lagt, target):
            d, pa, alpha = x.reshape((3, -1))
            r = self.theoretical(lagt, d, pa, alpha, exposure_time,
                                 squeeze_result=False)
            return np.ravel(r.T - target.T)

        # Something is wrong with this Jacobian
#        def jacobian(x, lagt, target):
#            # Does not support exposure time correction
#            n_lag, n_data = target.shape
#            ret = np.zeros((target.size, n_data * 3))
#            x = x.reshape((3, -1))
#            for i in range(n_data):
#                cur_rows = slice(i * n_lag, (i + 1) * n_lag)
#                d, pa, alpha = x[:, i]
#                ret[cur_rows, i] = 4 * lagt**alpha
#                ret[cur_rows, i + n_data] = 8 * pa
#                ret[cur_rows, i + 2 * n_data] = \
#                    4 * d * np.log(lagt) * lagt**alpha
#            return ret

        initial = np.asarray(initial)
        self._results = OrderedDict()
        self._err = OrderedDict()
        for particle, m in msd._msd_set.items():
            nl = min(n_lag, m.shape[0])
            lagt = np.arange(1, nl + 1) / msd.frame_rate
            target = m[:nl, :]
            init = np.repeat(initial, target.shape[1])
            f = scipy.optimize.least_squares(
                residual, init,  # TODO: bounds
                kwargs={"lagt": lagt, "target": target})

            r = f.x.reshape((3, -1))
            self._results[particle] = np.mean(r, axis=1)
            if r.shape[1] > 1:
                # Use corrected sample std as a less biased estimator of the
                # population  std
                self._err[particle] = np.std(r, axis=1, ddof=1)

        self._msd = msd
        self.exposure_time = exposure_time

    @staticmethod
    def exposure_time_corr(t, alpha, exposure_time, n=100,
                           force_numeric=False):
        r"""Correct lag times for the movement of particles during exposure

        When particles move during exposure, it appears as if the lag times
        change according to

        .. math:: t_\text{app}^\alpha = \lim_{n\rightarrow\infty} \frac{1}{n^2}
            \sum_{m_1 = 0}^{n-1} \sum_{m_2 = 0}^{n-1} |t +
            \frac{t_\text{exp}}{n}(m_1 - m_2)|^\alpha -
            |\frac{t_\text{exp}}{n}(m_1 - m_2)|^\alpha.

        For :math:`\alpha=1`, :math:`t_\text{app} = t - t_\text{exp} / 3`. For
        :math:`t_\text{exp} = 0` or :math:`\alpha = 2`,
        :math:`t_\text{app} = t`. For other parameter values, the sum is
        computed numerically using a sufficiently large `n` (100 by default).

        See [Goul2000]_ for details.

        Parameters
        ----------
        t : numpy.ndarray
            Lag times
        alpha : float
            Anomalous diffusion exponent
        exposure_time : float
            Exposure time
        n : int, optional
            Number of summands for the numeric calculation. The complexity
            of the algorithm is O(n²). Defaults to 100.

        Returns
        -------
        numpy.ndarray
            Apparent lag times that account for diffusion during exposure

        Other parameters
        ----------------
        force_numeric : bool, optional
            If True, do not return the analytical solutions for
            :math:`\alpha \in \{1, 2\}` and :math:`t_\text{exp} = 0`, but
            calculate numerically. Useful for testing.
        """
        if not force_numeric:
            if (math.isclose(exposure_time, 0) or
                    math.isclose(exposure_time, 2)):
                return t
            if math.isclose(alpha, 1):
                return t - exposure_time / 3

        m = np.arange(n)
        m_diff = exposure_time / n * (m[:, None] - m[None, :])
        s = (np.abs(t[:, None, None] + m_diff[None, ...])**alpha -
             np.abs(m_diff[None, ...])**alpha)
        return (np.sum(s, axis=(1, 2)) / n**2)**(1/alpha)

    @staticmethod
    def theoretical(t, d, pa, alpha=1, exposure_time=0, squeeze_result=True):
        r"""Calculate theoretical MSDs for different lag times

        Calculate :math:`msd(t_\text{lag}) = 4 D t_\text{app}^\alpha + 4 pa^2`,
        where :math:`t_\text{app}` is the apparent time lag which takes into
        account particle motion during exposure; see
        :py:meth:`exposure_time_corr`.

        Parameters
        ----------
        t : array-like or scalar
            Lag times
        d : float
            Diffusion coefficient
        pa : float
            Positional accuracy.
        alpha : float, optional
            Anomalous diffusion exponent. Defaults to 1.
        exposure_time : float, optional
            Exposure time. Defaults to 0.
        squeeze_result : bool, optional
            If `True`, return the result as a scalar type or 1D array if
            possible. Otherwise, always return a 2D array. Defaults to `True`.

        Returns
        -------
        numpy.ndarray or scalar
            Calculated theoretical MSDs
        """
        t = np.array(t, ndmin=1, copy=False)
        d = np.array(d, ndmin=1, copy=False)
        pa = np.array(pa, ndmin=1, copy=False)
        alpha = np.array(alpha, ndmin=1, copy=False)
        if d.shape != pa.shape or d.shape != alpha.shape:
            raise ValueError("`d`, `pa`, and `alpha` should have same shape.")
        if t.ndim > 1 or d.ndim > 1 or pa.ndim > 1:
            raise ValueError("Number of dimensions of `t`, `d`, `pa`, and "
                             "`alpha` need to be less than 2")

        ic = 4 * pa**2
        ic[pa < 0] *= -1
        t_corr = np.empty((len(t), len(alpha)), dtype=float)
        for i, a in enumerate(alpha):
            t_corr[:, i] = AnomalousDiffusion.exposure_time_corr(
                t, a, exposure_time)

        ret = 4 * d[None, :] * t_corr**alpha[None, :] + ic[None, :]

        if squeeze_result:
            if ret.size == 1:
                return np.asscalar(ret)
            return np.squeeze(ret)

        return ret

    def get_results(self):
        """Get fit results

        Returns
        -------
        results : pandas.DataFrame
            Fit results. Columns are the fit paramaters. Each row represents
            one particle.
        errors : pandas.DataFrame
            Fit results standard errors. If no bootstrapping was performed
            for calculation of MSDs, this is empty.
        """
        res_df = pd.DataFrame(self._results, index=self._fit_parameters).T
        err_df = pd.DataFrame(self._err, index=self._fit_parameters).T
        return res_df, err_df

    def plot(self, show_legend=True, ax=None):
        """Plot lag time vs. MSD with fitted theoretical curve

        Parameters
        ----------
        show_legend : bool, optional
            Whether to add a legend to the plot. Defaults to `True`.
        ax : matplotlib.axes.Axes or None, optional
            Axes to use for plotting. If `None`, use ``pyplot.gca()``.
            Defaults to `None`.
        """
        import matplotlib.pyplot as plt
        if ax is None:
            ax = plt.gca()

        ax.set_xlabel("lag time [s]")
        ax.set_ylabel("MSD [μm²]")

        for p in self._results:
            if isinstance(p, tuple):
                name = "/".join(str(p2) for p2 in p)
            else:
                name = str(p)
            m = self._msd._msds[p]
            e = self._msd._err[p]
            lt = self._msd._get_lagtimes(len(m))
            eb = ax.errorbar(lt, m, yerr=e, linestyle="none", marker="o",
                             markerfacecolor="none")
            self._plot_single(p, lt[-1], name, ax, eb[0].get_color())

        if show_legend:
            ax.legend(loc=0)

    @staticmethod
    def _value_with_error(name, unit, value, err=np.NaN, formatter=".2g"):
        """Write a value with a name, a unit and an error

        Parameters
        ----------
        name : str
            Value name
        unit : str
            Physical unit
        value : number
            Value
        err : number, optional
            Error of the value. If `NaN`, it is ignored. Defaults to `NaN`.
        formatter : str, optional
            Formatter for the numbers. Defaults to ".2g".

        Returns
        -------
        str
            String of the form "<name>: <value> ± <err> <unit>" if an error
            was specified, otherwise "<name>: <value> <unit>".
        """
        if not math.isfinite(err):
            s = f"{{name}} = {{value:{formatter}}} {{unit}}"
        else:
            s = f"{{name}} = {{value:{formatter}}} ± {{err:{formatter}}} {{unit}}"
        return s.format(name=name, value=value, err=err, unit=unit)

    def _plot_single(self, data_id, n_lag, name, ax, color):
        """Plot a single theoretical curve

        Parameters
        ----------
        data_id
            A key in :py:attr:`_results` to plot
        n_lag : int
            Number of lag times to plot
        name : str
            Name of the data set given by `data_id` to be printed in the legend
        ax : matplotlib.axes.Axes
            Axes to use for plotting
        color : str
            Color of the plotted line
        """
        d, pa, alpha = self._results[data_id]
        d_err, pa_err, alpha_err = self._err.get(data_id, (np.NaN,) * 3)

        x = np.linspace(0, n_lag, 100)
        y = self.theoretical(x, d, pa, alpha, self.exposure_time)

        legend = []
        if name:
            legend.append(name)
        legend.append(self._value_with_error("D", "μm²/s$^\alpha$", d, d_err))
        legend.append(self._value_with_error("PA", "nm",
                                             pa * 1000, pa_err * 1000, ".0f"))
        legend.append(self._value_with_error("α", "", alpha, alpha_err))
        legend = "\n".join(legend)

        ax.plot(x, y, c=color, label=legend)


class BrownianMotion(AnomalousDiffusion):
    """Fit Brownian motion parameters to MSD values

    Fit a function :math:`msd(t_\text{lag}) = 4*D*t_\text{lag} + 4*pa**2` to
    the tlag-vs.-MSD graph, where :math:`D` is the diffusion coefficient and
    :math:`pa` is the positional accuracy (uncertainty).
    """
    _fit_parameters = ["D", "PA"]

    def __init__(self, msd, n_lag=2, exposure_time=0):
        """Parameters
        ----------
        msd : MSD
            :py:class:`MSD` instance to get MSD information from
        n_lag : int or inf, optional
            Maximum number of lag times to use for fitting. Defaults to 2.
        exposure_time : float, optional
            Exposure time. Defaults to 0, i.e. no exposure time correction
        """
        self._results = OrderedDict()
        self._err = OrderedDict()
        for particle, m in msd._msd_set.items():
            nl = min(n_lag, m.shape[0])
            if nl == 2:
                s = (m[1, :] - m[0, :]) * msd.frame_rate
                i = m[0, :] - s * (1 / msd.frame_rate - exposure_time / 3)
            else:
                lagt = np.arange(1, nl + 1) / msd.frame_rate
                s, i = np.polyfit(lagt - exposure_time / 3, m[:nl, :], 1)

            d = s / 4
            pa = np.sqrt(i.astype(complex)) / 2
            pa = np.where(i > 0, np.real(pa), -np.imag(pa))

            self._results[particle] = [d.mean(), pa.mean()]
            if len(d) > 1:
                # Use corrected sample std as a less biased estimator of the
                # population std
                self._err[particle] = [np.std(d, ddof=1), np.std(pa, ddof=1)]

        self._msd = msd
        self.exposure_time = exposure_time

    @staticmethod
    def theoretical(t, d, pa, exposure_time=0):
        r"""Calculate theoretical MSDs for different lag times

        Calculate :math:`msd(t_\text{lag}) = 4 D t_\text{app}^\alpha + 4 pa^2`,
        where :math:`t_\text{app}` is the apparent time lag which takes into
        account particle motion during exposure; see
        :py:meth:`exposure_time_corr`.

        Parameters
        ----------
        t : array-like or scalar
            Lag times
        d : float
            Diffusion coefficient
        pa : float
            Positional accuracy.
        alpha : float, optional
            Anomalous diffusion exponent. Defaults to 1.
        exposure_time : float, optional
            Exposure time. Defaults to 0.
        squeeze_result : bool, optional
            If `True`, return the result as a scalar type or 1D array if
            possible. Otherwise, always return a 2D array. Defaults to `True`.

        Returns
        -------
        numpy.ndarray or scalar
            Calculated theoretical MSDs
        """
        return AnomalousDiffusion.theoretical(t, d, pa, np.ones_like(d),
                                              exposure_time)

    def _plot_single(self, data_id, n_lag, name, ax, color):
        d, pa = self._results[data_id]
        d_err, pa_err = self._err.get(data_id, (np.NaN,) * 2)

        x = np.linspace(0, n_lag, 100)
        y = self.theoretical(x, d, pa, self.exposure_time)

        legend = []
        if name:
            legend.append(name)
        legend.append(self._value_with_error("D", "μm²/s", d, d_err))
        legend.append(self._value_with_error("PA", "nm",
                                             pa * 1000, pa_err * 1000, ".0f"))
        legend = "\n".join(legend)

        ax.plot(x, y, c=color, label=legend)


# Old API

@config.set_columns
def imsd(data, pixel_size, fps, max_lagtime=100, columns={}):
    """Calculate mean square displacements from tracking data for each particle

    Parameters
    ----------
    data : pandas.DataFrame
        Tracking data
    pixel_size : float
        width of a pixel in micrometers
    fps : float
        Frames per second
    max_lagtime : int, optional
        Maximum number of time lags to consider. Defaults to 100.

    Returns
    -------
    pandas.DataFrame([0, ..., n])
        For each lag time and each particle/trajectory return the calculated
        mean square displacement.

    Other parameters
    ----------------
    columns : dict, optional
        Override default column names as defined in :py:attr:`config.columns`.
        Relevant names are `coords`, `particle`, and `time`.
        This means, if your DataFrame has coordinate columns "x" and "z" and
        the time column "alt_frame", set ``columns={"coords": ["x", "z"],
        "time": "alt_frame"}``.
    """
    warnings.warn("This function is deprecated. Use the `Msd` class instead.",
                  np.VisibleDeprecationWarning)
    msd_cls = Msd(data, fps, max_lagtime, n_boot=0, ensemble=False,
                  columns=columns)
    return msd_cls.get_msd()[0].T


@config.set_columns
def emsd(data, pixel_size, fps, max_lagtime=100, columns={}):
    """Calculate ensemble mean square displacements from tracking data

    This is equivalent to consecutively calling :func:`all_displacements`,
    :func:`all_square_displacements`, and
    :func:`emsd_from_square_displacements`.

    Parameters
    ----------
    data : list of pandas.DataFrames or pandas.DataFrame
        Tracking data
    pixel_size : float
        width of a pixel in micrometers
    fps : float
        Frames per second
    max_lagtime : int, optional
        Maximum number of time lags to consider. Defaults to 100.

    Returns
    -------
    pandas.DataFrame([msd, stderr, lagt])
        For each lag time return the calculated mean square displacement and
        standard error.

    Other parameters
    ----------------
    columns : dict, optional
        Override default column names as defined in :py:attr:`config.columns`.
        Relevant names are `coords`, `particle`, and `time`.
        This means, if your DataFrame has coordinate columns "x" and "z" and
        the time column "alt_frame", set ``columns={"coords": ["x", "z"],
        "time": "alt_frame"}``.
    """
    warnings.warn("This function is deprecated. Use the `Msd` class instead.",
                  np.VisibleDeprecationWarning)
    msd_cls = Msd(data, fps, max_lagtime, n_boot=0, columns=columns)
    msd = msd_cls.get_msd()
    msd[0].index = ["msd"]
    msd[1].index = ["stderr"]
    ret = pd.concat(msd).T
    ret["lagt"] = ret.index
    ret.index.name = None
    ret.columns.name = None
    return ret


def fit_msd(emsd, max_lagtime=2, exposure_time=0, model="brownian"):
    """Get the diffusion coefficient and positional accuracy from MSDs

    Fit a function :math:`msd(t) = 4*D*t^\alpha + 4*pa**2` to the
    tlag-vs.-MSD graph, where :math:`D` is the diffusion coefficient and
    :math:`pa` is the positional accuracy (uncertainty) and :math:`alpha`
    the anomalous diffusion exponent.

    Parameters
    ----------
    emsd : DataFrame([lagt, msd])
        MSD data as computed by `emsd`
    max_lagtime : int, optional
        Use the first `max_lagtime` lag times for fitting only. Defaults to 2.
    exposure_time : float, optional
        Correct positional accuracy for motion during exposure. Settings to 0
        turns this off. Defaults to 0.
    model : {"brownian", "anomalous"}
        If "brownian", set :math:`\alpha=1`. Otherwise, also fit
        :math:`\alpha`.

    Returns
    -------
    d : float
        Diffusion coefficient
    pa : float
        Positional accuracy. If this is negative, the fitted graph's
        intercept was negative (i. e. not meaningful).
    alpha : float
        Anomalous diffusion exponent. Only returned if ``model="anomalous"``.
    """
    warnings.warn("This function is deprecated. Use the `Msd` class instead.",
                  np.VisibleDeprecationWarning)
    msd_cls = Msd._from_data(emsd)
    fit_args = {"exposure_time": exposure_time}
    if model == "brownian":
        fit_args["n_lag"] = max_lagtime
    fit_res = msd_cls.fit(model, **fit_args)
    return fit_res._results["ensemble"]


def plot_msd(emsd, d=None, pa=None, max_lagtime=100, show_legend=True, ax=None,
             exposure_time=0., alpha=1., fit_max_lagtime=2,
             fit_model="brownian"):
    """Plot lag time vs. MSD and the fit as calculated by `fit_msd`.

    Parameters
    ----------
    emsd : DataFrame([lagt, msd, stderr])
        MSD data as computed by `emsd`. If the stderr column is not present,
        no error bars will be plotted.
    d : float or None, optional
        Diffusion coefficient (see :py:func:`fit_msd`). If `None`, use
        :py:func:`fit_msd` to calculate it.
    pa : float
        Positional accuracy (see :py:func:`fit_msd`) If `None`, use
        :py:func:`fit_msd` to calculate it.
    max_lagtime : int, optional
        Maximum number of lag times to plot. Defaults to 100.
    show_legend : bool, optional
        Whether to show the legend (the values of the diffusion coefficient D
        and the positional accuracy) in the plot. Defaults to True.
    ax : matplotlib.axes.Axes or None, optional
        If given, use this axes object to draw the plot. If None, use the
        result of `matplotlib.pyplot.gca`.
    exposure_time : float, optional
        Correct positional accuracy for motion during exposure. Settings to 0
        turns this off. This has to match the exposure time of the
        :py:func:`fit_msd` call. Defaults to 0.
    alpha : float, optional
        Anomalous diffusion exponent. Defaults to 1.
    fit_max_lagtime : int
        Passed as `max_lagtime` parameter to :py:func:`fit_msd` if either `d`
        or `pa` is `None`. Defaults to 2.
    fit_model : str
        Passed as `model` parameter to :py:func:`fit_msd` if either `d`
        or `pa` is `None`. Defaults to "brownian".

    Returns
    -------
    d : float
        Diffusion coefficient
    pa : float
        Positional accuracy. If this is negative, the fitted graph's
        intercept was negative (i. e. not meaningful).
    alpha : float
        Anomalous diffusion exponent.
    """
    warnings.warn("This function is deprecated. Use the `Msd` class instead.",
                  np.VisibleDeprecationWarning)
    msd_cls = Msd._from_data(emsd)

    if d is None or pa is None:
        fit_args = {"exposure_time": exposure_time}
        if fit_model == "brownian":
            fit_args["n_lag"] = fit_max_lagtime
        fit_res = msd_cls.fit(fit_model, **fit_args)
        fit_res.plot(show_legend, ax)
    else:
        fit_res = types.SimpleNamespace(
            _results={"ensemble": [d, pa, alpha]}, _err={},
            exposure_time=exposure_time)
        AnomalousDiffusion.plot(fit_res, show_legend, ax)

    r = fit_res._results
    if len(r) == 3:
        return tuple(r)
    return r[0], r[1], 1
