# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Ensemble Prediction Interval

This is an Ensemble method to estimate the prediction interval for any forecasting models
The high level idea is to estimate the empirical error matrix (S) from a specific
forecasting model, and then calculate mean (m) and standard deviation (std) from S.
When doing forecasting, adjust the original fcst by a random sample generated from N(m, std).
Do this procedure for m times (ensemble), based on which, generate fcst/fcst_upper/fcst_lower.
"""

import logging
from typing import Optional, Tuple, Type

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy
from kats.consts import Params, TimeSeriesData
from kats.models.model import Model

_LOGGER: logging.Logger = logging.getLogger()


def mean_confidence_interval(
    data: np.ndarray,
    confidence: float = 0.8,
) -> Tuple[float, float, float]:
    data = 1.0 * np.array(data)
    dof = len(data)
    # calculate mean and std
    m, se = np.mean(data), np.std(data)
    h = se * scipy.stats.t.ppf((1 + confidence) / 2.0, dof - 1)
    return m, m - h, m + h


class ensemble_predict_interval:
    """
        Class for Ensemble Predict Interval.

        The steps are listed as follows:
        1. Split the ts into k (k = n_block + 1) parts, p_1 to p_k. Each part has size as block_size.
        2. For given model and params, train the model with data p_i, and predict next block_size step,
            and calculate the bias vector by comparing it to p_(i+1).
        3. We get an error matrix S with shape n_block * block_size from step 2, and then calculate mean (m)
            and standard deviation (std) from S.
        4. When doing forecasting, adjust the original fcst by a random sample generated from N(m, std).
        5. Repeat step 4 for m times (ensemble_size), based on which, generate fcst/fcst_upper/fcst_lower.

    Attributes:
        model: forecasting model
        model_params: forecasting model parameters
        ts: the time series data in `TimeSeriesData` format
        block_size: the size of blocks
        n_block: number of block we want to split the ts into
        ensemble_size: size of ensemble

    >>> # Example
    >>> val = np.arange(180)/6+np.sin(np.pi*np.arange(180)/6)*20++np.cos(np.arange(180))*20+np.random.randn(180)*10
    >>> ts = TimeSeriesData(pd.DataFrame({'time': pd.date_range('2021-05-06', periods = 180), 'val':val}))
    >>> hist_ts, test_ts = ts[:120], ts[120:]
    >>> epi = ensemble_predict_interval(
            model=ProphetModel,
            model_params=ProphetParams(seasonality_mode='additive'),
            ts=hist_ts,
            n_block=5,
            ensemble_size=10,
        )
    >>> res = epi.get_projection(step=60)
    >>> res.head()
    >>> # visualization
    >>> epi.pi_comparison_plot(test_ts)
    """

    def __init__(
        self,
        model: Type[Model[Params]],
        model_params: Params,
        ts: TimeSeriesData,
        block_size: Optional[int] = None,
        n_block: Optional[int] = None,
        ensemble_size: int = 10,
    ) -> None:
        self.model: Type[Model[Params]] = model
        self.params: Params = model_params

        if block_size is None and n_block is None:
            raise ValueError(
                "Please provide an initial value for either block_size or n_block."
            )
        elif block_size is None:
            assert n_block is not None
            if n_block < 5:
                raise ValueError(
                    f"The given n_block is {n_block}. Please provide a larger block_size."
                )
            self.n_block: int = n_block
            block_size = len(ts) // (self.n_block + 1)
            if block_size < 10:
                raise ValueError(
                    f"Block_size is {block_size}. Please provide a longer TS or a smaller n_block."
                )
            self.block_size: int = block_size
        elif n_block is None:
            assert block_size is not None
            if block_size < 10:
                raise ValueError(
                    f"The given block_size is {block_size}. Please provide a larger n_block."
                )
            self.block_size: int = block_size
            n_block = len(ts) // self.block_size - 1
            if n_block < 5:
                raise ValueError(
                    f"n_block is {n_block}. Please provide a longer TS or a smaller block_size."
                )
            self.n_block: int = n_block
        else:
            if block_size < 10:
                raise ValueError(
                    f"The given block_size is {block_size}. Please provide a larger n_block."
                )
            if n_block < 5:
                raise ValueError(
                    f"The given n_block is {n_block}. Please provide a larger block_size."
                )
            if len(ts) < (n_block + 1) * block_size:
                raise ValueError(
                    f"The given TS has length {len(ts)}, which is samller than (n_block + 1) * block_size. Please provide a longer TS."
                )
            self.block_size: int = block_size
            self.n_block: int = n_block

        self.ts: TimeSeriesData = ts[-self.block_size * (self.n_block + 1) :]

        # infer freqency, data granularity
        self.freq: str = str(int(self.ts.infer_freq_robust().total_seconds())) + "s"

        if ensemble_size < 4:
            raise ValueError(
                f"The given ensemble_size is {ensemble_size}. Please provide a larger n_block."
            )
        self.ensemble_size: int = ensemble_size

        self.error_matrix: np.ndarray = np.empty([self.n_block, self.block_size])
        self.error_matrix_flag: bool = False

        self.ensemble_fcst: np.ndarray = np.empty([1, 1])
        self.projection_flag: bool = False

    def _get_error_matrix(self) -> None:
        if self.error_matrix_flag:
            return

        for i in range(self.n_block):
            train_ts = TimeSeriesData(
                pd.DataFrame(
                    {
                        "time": self.ts[
                            i * self.block_size : (i + 1) * self.block_size
                        ].time.to_list(),
                        "value": self.ts[
                            i * self.block_size : (i + 1) * self.block_size
                        ].value.to_list(),
                    }
                )
            )
            m = self.model(train_ts, self.params)
            m.fit()
            pred = m.predict(steps=self.block_size, freq=self.freq)
            assert pred is not None
            fcst = pred["fcst"]

            # calculate error for each block
            sigma = np.asarray(
                self.ts[
                    (i + 1) * self.block_size : (i + 2) * self.block_size
                ].value.to_list()
            ) - np.asarray(fcst)

            # store error in S
            self.error_matrix[i, :] = sigma

        self.error_matrix_flag = True

    def _projection(self, step: int = 30, rolling_based: bool = False) -> None:

        if not self.error_matrix_flag:
            self._get_error_matrix()

        future_block = step // self.block_size + int(step % self.block_size > 0)

        ensemble_fcst = np.zeros([self.ensemble_size, future_block * self.block_size])

        for k in range(self.ensemble_size):
            if not rolling_based:
                training_data = TimeSeriesData(
                    pd.DataFrame(
                        {
                            "time": self.ts[-self.block_size :].time.to_list(),
                            "value": self.ts[-self.block_size :].value.to_list(),
                        }
                    )
                )
            else:
                training_data = TimeSeriesData(
                    pd.DataFrame(
                        {
                            "time": self.ts[:].time.to_list(),
                            "value": self.ts[:].value.to_list(),
                        }
                    )
                )
            for i in range(future_block):
                m = self.model(training_data, self.params)
                m.fit()
                pred = m.predict(steps=self.block_size, freq=self.freq)
                assert pred is not None
                fcst = np.asarray(pred["fcst"])

                fcst[:] += np.random.multivariate_normal(
                    self.error_matrix.mean(0), np.cov(self.error_matrix.T), 1
                )[0]

                ensemble_fcst[
                    k, i * self.block_size : (i + 1) * self.block_size
                ] = fcst.copy()

                # refresh training_data
                if not rolling_based:
                    training_data = TimeSeriesData(
                        pd.DataFrame(
                            {"time": pred["time"].tolist(), "value": list(fcst)}
                        )
                    )
                else:
                    training_data.extend(
                        TimeSeriesData(
                            pd.DataFrame(
                                {"time": pred["time"].tolist(), "value": list(fcst)}
                            )
                        )
                    )

        self.ensemble_fcst = ensemble_fcst[:, :step]

    def get_projection(
        self,
        step: int = 30,
        rolling_based: bool = False,
        confidence_level: float = 0.8,
    ) -> pd.DataFrame:
        self._projection(step=step, rolling_based=rolling_based)
        mi, low, up = np.zeros(step), np.zeros(step), np.zeros(step)

        for i in range(step):
            mi[i], low[i], up[i] = mean_confidence_interval(
                self.ensemble_fcst[:, i], confidence_level
            )

        res = pd.DataFrame(
            {"fcst": list(mi), "fcst_lower": list(low), "fcst_upper": list(up)}
        )

        self.projection_flag = True
        return res

    def pi_comparison_plot(
        self,
        test_ts: Optional[TimeSeriesData] = None,
        confidence_level: float = 0.8,
        figure_size: Tuple[int, int] = (10, 5),
        test_data_only: bool = False,
    ) -> None:
        if test_data_only and not test_ts:
            raise ValueError("Please provide test_ts.")

        T = self.ensemble_fcst.shape[1]
        if not self.projection_flag:
            raise ValueError("Please train and predit the model first.")

        if test_data_only:
            hist_data = []
            hist_data_len = 0
        else:
            hist_data = self.ts.value
            hist_data_len = len(hist_data)

        mi, low, up = np.zeros(T), np.zeros(T), np.zeros(T)

        for i in range(T):
            mi[i], low[i], up[i] = mean_confidence_interval(
                self.ensemble_fcst[:, i], confidence_level
            )

        fig, ax = plt.subplots()

        ax.plot(
            range(hist_data_len, T + hist_data_len),
            mi,
            lw=1,
            color="g",
            alpha=1,
            label="Predicted value from EPI",
        )
        ax.fill_between(
            range(hist_data_len, T + hist_data_len),
            low,
            up,
            color="g",
            alpha=0.4,
            label="Predict interval band from EPI",
        )

        if hist_data_len > 0:
            ax.plot(
                range(0, hist_data_len),
                hist_data[:hist_data_len],
                lw=1,
                color="blue",
                alpha=1,
                label="Historical value",
            )
        if test_ts is not None:
            ax.plot(
                range(hist_data_len, min(len(test_ts), T) + hist_data_len),
                test_ts.value[: min(len(test_ts), T)],
                lw=1,
                color="r",
                alpha=1,
                label="Test value",
            )

        ax.set_xlabel("Time")
        ax.set_ylabel("Value")

        plt.legend(loc="upper left")
        fig.set_size_inches(*figure_size)
        plt.show()
