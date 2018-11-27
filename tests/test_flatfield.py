import os

import pandas as pd
import numpy as np
from scipy import ndimage
import pytest

from sdt import flatfield


@pytest.fixture(params=["normal", "reload"])
def corr_factory(request, tmp_path):
    """Construct a Corrector object normally or by saving and reloading"""
    def factory(*args, **kwargs):
        corr = flatfield.Corrector(*args, **kwargs)
        if request.param == "reload":
            corr.save(tmp_path / "fc.npz")
            return flatfield.Corrector.load(tmp_path / "fc.npz")
        else:
            return corr
    return factory


class TestCorrector:
    def setup_method(self):
        self.img_shape = (100, 150)
        self.y, self.x = np.indices(self.img_shape)
        self.xc = 20
        self.yc = 50
        self.wx = 40
        self.wy = 20
        self.amp = 2

        self.img = self._make_gauss(self.x, self.y)
        self.fit_result = dict(amplitude=self.amp, center=(self.xc, self.yc),
                               sigma=(self.wx, self.wy), offset=0, rotation=0)

    def _make_gauss(self, x, y):
        argx = -(x - self.xc)**2 / (2 * self.wx**2)
        argy = -(y - self.yc)**2 / (2 * self.wy**2)
        return self.amp * np.exp(argx) * np.exp(argy)

    def test_init_img_nofit(self, corr_factory):
        """flatfield.Corrector.__init__: image data, no fit"""
        imgs = []
        for amp in range(3, 0, -1):
            img = amp * self.img
            imgs.append(img)

        corr = corr_factory(imgs, gaussian_fit=False)
        np.testing.assert_allclose(corr.avg_img, self.img / self.amp)
        np.testing.assert_allclose(corr.corr_img, self.img / self.img.max())
        assert corr.fit_result is None

    def test_init_img_smooth(self, corr_factory):
        """flatfield.Corrector.__init__: image data, smoothing"""
        imgs = []
        for amp in range(3, 0, -1):
            img = amp * self.img
            imgs.append(img)

        corr = corr_factory(imgs, gaussian_fit=False, smooth_sigma=1.)
        np.testing.assert_allclose(corr.avg_img, self.img / self.amp)
        exp_corr_img = ndimage.gaussian_filter(self.img / self.img.max(),
                                               sigma=1.)
        np.testing.assert_allclose(corr.corr_img, exp_corr_img)
        assert corr.fit_result is None

    def test_init_bg_scalar(self, corr_factory):
        """flatfield.Corrector.__init__: scalar `bg` parameter"""
        bg = 200.
        imgs = [self.img + bg] * 3

        corr = corr_factory(imgs, bg=bg, gaussian_fit=False)
        np.testing.assert_allclose(corr.avg_img, self.img / self.amp)
        np.testing.assert_allclose(corr.corr_img, self.img / self.img.max())
        assert corr.fit_result is None

    def test_init_bg_array(self, corr_factory):
        """flatfield.Corrector.__init__: array `bg` parameter"""
        bg = np.full(self.img.shape, 100.)
        bg[:, bg.shape[1]//2:] = 200.
        imgs = [self.img + bg] * 3

        corr = corr_factory(imgs, bg=bg, gaussian_fit=False)
        np.testing.assert_allclose(corr.avg_img, self.img / self.amp)
        np.testing.assert_allclose(corr.corr_img, self.img / self.img.max())
        assert corr.fit_result is None

    def _check_fit_result(self, res, expected):
        assert res.keys() == expected.keys()
        for k in expected:
            i = res[k]
            e = expected[k]
            np.testing.assert_allclose(i, e, atol=1e-10)

    def test_init_img_fit(self, corr_factory):
        """flatfield.Corrector.__init__: image data, fit"""
        imgs = []
        for amp in range(3, 0, -1):
            img = amp * self.img
            imgs.append(img)

        corr = corr_factory(imgs, gaussian_fit=True)
        np.testing.assert_allclose(corr.avg_img, self.img / self.img.max())
        np.testing.assert_allclose(corr.corr_img, self.img / self.img.max())

        expected = self.fit_result.copy()
        expected["amplitude"] = 1
        self._check_fit_result(corr.fit_result, expected)

    def test_init_list(self, corr_factory):
        """flatfield.Corrector.__init__: list of data points, not weighted"""
        y, x = [i.flatten() for i in np.indices(self.img_shape)]
        x = x[::10]
        y = y[::10]
        data = np.column_stack([x, y, self._make_gauss(x, y)])
        df = pd.DataFrame(data, columns=["x", "y", "mass"])

        corr = corr_factory(df, density_weight=False, shape=self.img_shape)

        np.testing.assert_allclose(corr.avg_img, self.img / self.img.max(),
                                   rtol=1e-5)
        np.testing.assert_allclose(corr.corr_img, self.img / self.img.max(),
                                   rtol=1e-5)

        self._check_fit_result(corr.fit_result, self.fit_result)

    def test_init_list_weighted(self, corr_factory):
        """flatfield.Corrector.__init__: list of data points, weighted"""
        y, x = [i.flatten() for i in np.indices(self.img_shape)]
        x = x[::10]
        y = y[::10]
        data = np.column_stack([x, y, self._make_gauss(x, y)])
        df = pd.DataFrame(data, columns=["x", "y", "mass"])

        corr = corr_factory(df, density_weight=True, shape=self.img_shape)

        np.testing.assert_allclose(corr.avg_img, self.img / self.img.max(),
                                   rtol=1e-5)
        np.testing.assert_allclose(corr.corr_img, self.img / self.img.max(),
                                   rtol=1e-5)

        self._check_fit_result(corr.fit_result, self.fit_result)

    def test_feature_correction(self, corr_factory):
        """flatfield.Corrector.__call__: single molecule data correction"""
        x = np.concatenate(
            [np.arange(self.img_shape[1]),
             np.full(self.img_shape[0], self.img_shape[1] // 2)])
        y = np.concatenate(
            [np.full(self.img_shape[1], self.img_shape[0] // 2),
             np.arange(self.img_shape[0])])
        mass_orig = np.full(len(x), 100)
        mass = mass_orig * self._make_gauss(x, y) / self.amp
        pdata = pd.DataFrame(dict(x=x, y=y, mass=mass))
        pdata1 = pdata.copy()
        pdata2 = pdata.copy()

        corr_img = corr_factory([self.img], gaussian_fit=False)
        corr_img(pdata, inplace=True)
        np.testing.assert_allclose(pdata["mass"].tolist(), mass_orig)

        corr_gauss = corr_factory([self.img], gaussian_fit=True)
        pdata1 = corr_gauss(pdata1)
        np.testing.assert_allclose(pdata1["mass"].tolist(), mass_orig,
                                   rtol=1e-5)

        pdata2["alt_mass"] = pdata2["mass"]
        corr_img(pdata2, inplace=True, columns={"corr": ["mass", "alt_mass"]})
        np.testing.assert_allclose(pdata2["mass"].values, mass_orig, rtol=1e-5)
        np.testing.assert_allclose(pdata2["alt_mass"].values, mass_orig,
                                   rtol=1e-5)

    def test_image_correction_with_img(self, corr_factory):
        """flatfield.Corrector.__call__: image correction, no fit"""
        corr_img = corr_factory([self.img], gaussian_fit=False)
        np.testing.assert_allclose(corr_img(self.img),
                                   np.full(self.img.shape, self.amp))

    def test_image_correction_bg(self, corr_factory):
        """flatfield.Corrector.__call__: image correction, background"""
        bg1 = 200
        corr = corr_factory([self.img + bg1], bg=bg1, gaussian_fit=False)
        np.testing.assert_allclose(corr(self.img + bg1),
                                   np.full(self.img.shape, self.amp))

        bg2 = 300
        np.testing.assert_allclose(corr(self.img + bg2, bg=bg2),
                                   np.full(self.img.shape, self.amp))


    def test_image_correction_with_gauss(self, corr_factory):
        """flatfield.Corrector.__call__: image correction, fit"""
        corr_g = corr_factory([self.img], gaussian_fit=True)
        np.testing.assert_allclose(corr_g(self.img),
                                   np.full(self.img.shape, 2),
                                   rtol=1e-5)

    def test_get_factors_img(self, corr_factory):
        """flatfield.Corrector.get_factors: no fit"""
        corr = corr_factory([self.img], gaussian_fit=False)
        i, j = np.indices(self.img.shape)
        fact = corr.get_factors(j, i)
        np.testing.assert_allclose(1 / fact, self.img / self.img.max())

    def test_get_factors_gauss(self, corr_factory):
        """flatfield.Corrector.get_factors: fit"""
        corr = corr_factory([self.img], gaussian_fit=True)
        i, j = np.indices(self.img.shape)
        fact = corr.get_factors(j, i)
        np.testing.assert_allclose(1 / fact, self.img / self.img.max(),
                                   rtol=1e-5)
