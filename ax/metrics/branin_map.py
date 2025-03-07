#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from random import random
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from ax.core.base_trial import BaseTrial
from ax.core.map_data import MapData, MapKeyInfo
from ax.core.map_metric import MapMetricFetchResult
from ax.core.metric import MetricFetchE
from ax.metrics.noisy_function_map import NoisyFunctionMapMetric
from ax.utils.common.result import Err, Ok
from ax.utils.common.typeutils import checked_cast
from ax.utils.measurement.synthetic_functions import branin
from pyre_extensions import none_throws

FIDELITY = [0.1, 0.4, 0.7, 1.0]


class BraninTimestampMapMetric(NoisyFunctionMapMetric):
    def __init__(
        self,
        name: str,
        param_names: Iterable[str],
        noise_sd: float = 0.0,
        lower_is_better: bool | None = None,
        rate: float | None = None,
        cache_evaluations: bool = True,
    ) -> None:
        """A Branin map metric with an optional multiplicative factor
        of `1 + exp(-rate * t)` where `t` is the runtime of the trial.
        If the multiplicative factor is used, then at `t = 0`, the function
        is twice the usual value, while as `t` becomes large, the values
        approach the standard Branin values.

        Args:
            name: Name of the metric.
            param_names: An ordered list of names of parameters to be passed
                to the deterministic function.
            noise_sd: Scale of normal noise added to the function result.
            lower_is_better: Flag for metrics which should be minimized.
            rate: Parameter of the multiplicative factor.
        """
        self.rate = rate
        # pyre-fixme[4]: Attribute must be annotated.
        self._trial_index_to_timestamp = defaultdict(int)

        super().__init__(
            name=name,
            param_names=param_names,
            noise_sd=noise_sd,
            lower_is_better=lower_is_better,
            cache_evaluations=cache_evaluations,
        )

    def __eq__(self, o: BraninTimestampMapMetric) -> bool:
        """Ignore _timestamp on equality checks"""
        return (
            self.name == o.name
            and self.param_names == o.param_names
            and self.noise_sd == o.noise_sd
            and self.lower_is_better == o.lower_is_better
        )

    def fetch_trial_data(
        self, trial: BaseTrial, noisy: bool = True, **kwargs: Any
    ) -> MapMetricFetchResult:
        try:
            if (
                self._trial_index_to_timestamp[trial.index] == 0
                or trial.status.is_running
            ):
                self._trial_index_to_timestamp[trial.index] += 1

            datas = []
            for timestamp in range(self._trial_index_to_timestamp[trial.index]):
                res = [
                    self.f(
                        np.fromiter(arm.parameters.values(), dtype=float),
                        timestamp=timestamp,
                    )
                    for arm in trial.arms
                ]

                df = pd.DataFrame(
                    {
                        "arm_name": [arm.name for arm in trial.arms],
                        "metric_name": self.name,
                        "sem": self.noise_sd if noisy else 0.0,
                        "trial_index": trial.index,
                        "mean": [item["mean"] for item in res],
                        self.map_key_info.key: [
                            item[self.map_key_info.key] for item in res
                        ],
                    }
                )

                datas.append(MapData(df=df, map_key_infos=[self.map_key_info]))

            return Ok(value=MapData.from_multiple_map_data(datas))

        except Exception as e:
            return Err(
                MetricFetchE(message=f"Failed to fetch {self.name}", exception=e)
            )

    # pyre-fixme[14]: `f` overrides method defined in `NoisyFunctionMapMetric`
    #  inconsistently.
    def f(self, x: npt.NDArray, timestamp: int) -> Mapping[str, Any]:
        x1, x2 = x

        if self.rate is not None:
            weight = 1.0 + np.exp(-none_throws(self.rate) * timestamp)
        else:
            weight = 1.0

        mean = checked_cast(float, branin(x1=x1, x2=x2)) * weight

        return {"mean": mean, "timestamp": timestamp}


class BraninFidelityMapMetric(NoisyFunctionMapMetric):
    map_key_info: MapKeyInfo[float] = MapKeyInfo(key="fidelity", default_value=0.0)

    def __init__(
        self,
        name: str,
        param_names: Iterable[str],
        noise_sd: float = 0.0,
        lower_is_better: bool | None = None,
    ) -> None:
        super().__init__(
            name=name,
            param_names=param_names,
            noise_sd=noise_sd,
            lower_is_better=lower_is_better,
        )

        self.index = -1

    def fetch_trial_data(
        self, trial: BaseTrial, noisy: bool = True, **kwargs: Any
    ) -> MapMetricFetchResult:
        self.index = -1

        return super().fetch_trial_data(
            trial=trial,
            noisy=noisy,
            **kwargs,
        )

    def f(self, x: npt.NDArray) -> Mapping[str, Any]:
        if self.index < len(FIDELITY):
            self.index += 1

        x1, x2 = x
        fidelity = FIDELITY[self.index]

        fidelity_penalty = random() * math.pow(1.0 - fidelity, 2.0)
        mean = checked_cast(float, branin(x1=x1, x2=x2)) - fidelity_penalty

        return {"mean": mean, "fidelity": fidelity}
