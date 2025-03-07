# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ax.benchmark.benchmark_metric import BenchmarkMapMetric, BenchmarkMetric
from ax.benchmark.benchmark_test_function import BenchmarkTestFunction
from ax.benchmark.benchmark_test_functions.botorch_test import BoTorchTestFunction

from ax.core.objective import MultiObjective, Objective
from ax.core.optimization_config import (
    MultiObjectiveOptimizationConfig,
    ObjectiveThreshold,
    OptimizationConfig,
)
from ax.core.outcome_constraint import OutcomeConstraint
from ax.core.parameter import ChoiceParameter, ParameterType, RangeParameter
from ax.core.search_space import SearchSpace
from ax.core.trial import BaseTrial
from ax.core.types import ComparisonOp, TParamValue
from ax.utils.common.base import Base
from botorch.test_functions.base import (
    BaseTestProblem,
    ConstrainedBaseTestProblem,
    MultiObjectiveTestProblem,
)


def _get_name(
    test_problem: BaseTestProblem,
    observe_noise_sd: bool,
    dim: int | None = None,
) -> str:
    """
    Get a string name describing the problem, in a format such as
    "hartmann_fixed_noise_6d" or "jenatton" (where the latter would
    not have fixed noise and have the default dimensionality).
    """
    base_name = f"{test_problem.__class__.__name__}"
    observed_noise = "_observed_noise" if observe_noise_sd else ""
    dim_str = "" if dim is None else f"_{dim}d"
    return f"{base_name}{observed_noise}{dim_str}"


@dataclass(kw_only=True, repr=True)
class BenchmarkProblem(Base):
    """
    Problem against which different methods can be benchmarked.

    Defines how data is generated, the objective (via the OptimizationConfig),
    and the SearchSpace.

    Args:
        name: Can be generated programmatically with `_get_name`.
        optimization_config: Defines the objective of optimization. Metrics must
            be `BenchmarkMetric`s.
        num_trials: Number of optimization iterations to run. BatchTrials count
            as one trial.
        optimal_value: The best ground-truth objective value. Hypervolume for
            multi-objective problems. If the best value is not known, it is
            conventional to set it to a value that is almost certainly better
            than the best value, so that a benchmark's score will not exceed 100%.
        search_space: The search space.
        test_function: A `BenchmarkTestFunction`, which will generate noiseless
            data. This will be used by a `BenchmarkRunner`.
        noise_std: Describes how noise is added to the output of the
            `test_function`. If a float, IID random normal noise with that
            standard deviation is added. A list of floats, or a dict whose keys
            match `test_functions.outcome_names`, sets different noise
            standard deviations for the different outcomes produced by the
            `test_function`. This will be used by a `BenchmarkRunner`.
        report_inference_value_as_trace: Whether the ``optimization_trace`` on a
            ``BenchmarkResult`` should use the ``oracle_trace`` (if False,
            default) or the ``inference_trace``. See ``BenchmarkResult`` for
            more information. Currently, this is only supported for
            single-objective problems.
        n_best_points: Number of points for a best-point selector to recommend.
            Currently, only ``n_best_points=1`` is supported.
        trial_runtime_func: A function that takes a trial and returns the
            (virtual) time it takes to run that trial, which is 1 by default.
    """

    name: str
    optimization_config: OptimizationConfig
    num_trials: int
    test_function: BenchmarkTestFunction
    noise_std: float | Sequence[float] | Mapping[str, float] = 0.0
    optimal_value: float

    search_space: SearchSpace = field(repr=False)
    report_inference_value_as_trace: bool = False
    n_best_points: int = 1
    trial_runtime_func: Callable[[BaseTrial], int] | None = None
    target_fidelity_and_task: Mapping[str, TParamValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate inputs
        if self.n_best_points != 1:
            raise NotImplementedError("Only `n_best_points=1` is currently supported.")
        if self.report_inference_value_as_trace and self.is_moo:
            raise NotImplementedError(
                "Inference trace is not supported for MOO. Please set "
                "`report_inference_value_as_trace` to False."
            )

        # Validate that names on optimization config are contained in names on
        # test function
        objective = self.optimization_config.objective
        if isinstance(objective, MultiObjective):
            objective_names = {obj.metric.name for obj in objective.objectives}
        else:
            objective_names = {objective.metric.name}

        test_function_names = set(self.test_function.outcome_names)
        missing = objective_names - test_function_names
        if len(missing) > 0:
            raise ValueError(
                "The following objectives are defined on "
                "`optimization_config` but not included in "
                f"`runner.test_function.outcome_names`: {missing}."
            )

        constraints = self.optimization_config.outcome_constraints
        constraint_names = {c.metric.name for c in constraints}
        missing = constraint_names - test_function_names
        if len(missing) > 0:
            raise ValueError(
                "The following constraints are defined on "
                "`optimization_config` but not included in "
                f"`runner.test_function.outcome_names`: {missing}."
            )

        self.target_fidelity_and_task = {
            p.name: p.target_value
            for p in self.search_space.parameters.values()
            if (isinstance(p, ChoiceParameter) and p.is_task) or p.is_fidelity
        }

    @property
    def is_moo(self) -> bool:
        """Whether the problem is multi-objective."""
        return isinstance(self.optimization_config, MultiObjectiveOptimizationConfig)


def _get_constraints(
    constraint_names: Sequence[str],
    observe_noise_sd: bool,
    use_map_metric: bool = False,
) -> list[OutcomeConstraint]:
    """
    Create a list of ``OutcomeConstraint``s.

    Args:
        constraint_names: Names of the constraints. One constraint will be
            created for each.
        observe_noise_sd: Whether the standard deviation of the observation
            noise is observed, for each constraint. This doesn't handle the case
            where only some of the outcomes have noise levels observed.
        use_map_metric: Whether to use a ``BenchmarkMapMetric``.


    """
    metric_cls = BenchmarkMapMetric if use_map_metric else BenchmarkMetric
    outcome_constraints = [
        OutcomeConstraint(
            metric=metric_cls(
                name=name,
                lower_is_better=False,  # positive slack = feasible
                observe_noise_sd=observe_noise_sd,
            ),
            op=ComparisonOp.GEQ,
            bound=0.0,
            relative=False,
        )
        for name in constraint_names
    ]
    return outcome_constraints


def get_soo_opt_config(
    *,
    outcome_names: Sequence[str],
    lower_is_better: bool = True,
    observe_noise_sd: bool = False,
    use_map_metric: bool = False,
) -> OptimizationConfig:
    """
    Create a single-objective ``OptimizationConfig``, potentially with
    constraints.

    Args:
        outcome_names: Names of the outcomes. If ``outcome_names`` has more than
            one element, constraints will be created. The first element of
            ``outcome_names`` will be the name of the ``BenchmarkMetric`` on
            the objective, and others (if pressent) will be for constraints.
        lower_is_better: Whether the objective is a minimization problem. This
            only affects objectives, not constraints; for constraints, higher is
            better (feasible).
        observe_noise_sd: Whether the standard deviation of the observation
            noise is observed. Applies to all objective and constraints.
        use_map_metric: Whether to use a ``BenchmarkMapMetric``.
    """
    metric_cls = BenchmarkMapMetric if use_map_metric else BenchmarkMetric
    objective = Objective(
        metric=metric_cls(
            name=outcome_names[0],
            lower_is_better=lower_is_better,
            observe_noise_sd=observe_noise_sd,
        ),
        minimize=lower_is_better,
    )

    outcome_constraints = _get_constraints(
        constraint_names=outcome_names[1:],
        observe_noise_sd=observe_noise_sd,
        use_map_metric=use_map_metric,
    )

    return OptimizationConfig(
        objective=objective, outcome_constraints=outcome_constraints
    )


def get_moo_opt_config(
    *,
    outcome_names: Sequence[str],
    ref_point: Sequence[float],
    num_constraints: int = 0,
    lower_is_better: bool = True,
    observe_noise_sd: bool = False,
    use_map_metric: bool = False,
) -> MultiObjectiveOptimizationConfig:
    """
    Create a ``MultiObjectiveOptimizationConfig``, potentially with constraints.

    Args:
        outcome_names: Names of the outcomes. If ``num_constraints`` is greater
            than zero, the last ``num_constraints`` elements of
            ``outcome_names`` will become the names of ``BenchmarkMetric``s on
            constraints, and the others will correspond to the objectives.
        ref_point: Objective thresholds for the objective metrics. Note:
            Although this method requires providing a threshold for each
            objective, this is not required in general and could be enabled for
            this method.
        num_constraints: Number of constraints.
        lower_is_better: Whether the objectives are lower-is-better. Applies to
            all objectives and not to constraints. For constraints, higher is
            better (feasible). Note: Ax allows different metrics to have
            different values of ``lower_is_better``; that isn't enabled for this
            method, but could be.
        observe_noise_sd: Whether the standard deviation of the observation
        noise is observed. Applies to all objective and constraints.
    """
    n_objectives = len(outcome_names) - num_constraints
    metric_cls = BenchmarkMapMetric if use_map_metric else BenchmarkMetric
    objective_metrics = [
        metric_cls(
            name=outcome_names[i],
            lower_is_better=lower_is_better,
            observe_noise_sd=observe_noise_sd,
        )
        for i in range(n_objectives)
    ]
    constraints = _get_constraints(
        constraint_names=outcome_names[n_objectives:],
        observe_noise_sd=observe_noise_sd,
    )
    optimization_config = MultiObjectiveOptimizationConfig(
        objective=MultiObjective(
            objectives=[
                Objective(metric=metric, minimize=lower_is_better)
                for metric in objective_metrics
            ]
        ),
        objective_thresholds=[
            ObjectiveThreshold(
                metric=metric,
                bound=ref_p,
                relative=False,
                op=ComparisonOp.LEQ if metric.lower_is_better else ComparisonOp.GEQ,
            )
            for ref_p, metric in zip(ref_point, objective_metrics, strict=True)
        ],
        outcome_constraints=constraints,
    )
    return optimization_config


def get_continuous_search_space(bounds: list[tuple[float, float]]) -> SearchSpace:
    return SearchSpace(
        parameters=[
            RangeParameter(
                name=f"x{i}",
                parameter_type=ParameterType.FLOAT,
                lower=lower,
                upper=upper,
            )
            for i, (lower, upper) in enumerate(bounds)
        ]
    )


def create_problem_from_botorch(
    *,
    test_problem_class: type[BaseTestProblem],
    test_problem_kwargs: dict[str, Any],
    noise_std: float | list[float] = 0.0,
    num_trials: int,
    name: str | None = None,
    lower_is_better: bool = True,
    observe_noise_sd: bool = False,
    search_space: SearchSpace | None = None,
    report_inference_value_as_trace: bool = False,
    trial_runtime_func: Callable[[BaseTrial], int] | None = None,
) -> BenchmarkProblem:
    """
    Create a ``BenchmarkProblem`` from a BoTorch ``BaseTestProblem``.

    The resulting ``BenchmarkProblem``'s ``test_function`` is constructed from
    the ``BaseTestProblem`` class (``test_problem_class``) and its arguments
    (``test_problem_kwargs``). All other fields are passed to
    ``BenchmarkProblem`` if they are specified and populated with reasonable
    defaults otherwise. ``num_trials``, however, must be specified.

    Args:
        test_problem_class: The BoTorch test problem class which will be used
            to define the `search_space`, `optimization_config`, and `runner`.
        test_problem_kwargs: Keyword arguments used to instantiate the
            `test_problem_class`.
        noise_std: Standard deviation of synthetic noise added to outcomes. If a
            float, the same noise level is used for all objectives.
        lower_is_better: Whether this is a minimization problem. For MOO, this
            applies to all objectives.
        num_trials: Simply the `num_trials` of the `BenchmarkProblem` created.
        name: This and the following arguments are all passed to
            ``BenchmarkProblem`` if specified and populated with reasonable
            defaults otherwise.
        observe_noise_sd: Whether the standard deviation of the observation noise is
            observed or not (in which case it must be inferred by the model).
            This is separate from whether synthetic noise is added to the
            problem, which is controlled by the `noise_std` of the test problem.
        search_space: If provided, the `search_space` of the `BenchmarkProblem`.
            Otherwise, a `SearchSpace` with all `RangeParameter`s is created
            from the bounds of the test problem.
        report_inference_value_as_trace: If True, indicates that the
            ``optimization_trace`` on a ``BenchmarkResult`` ought to be the
            ``inference_trace``; otherwise, it will be the ``oracle_trace``.
            See ``BenchmarkResult`` for more information.
        trial_runtime_func: A function that takes a trial and returns how long
            it takes to run that trial.
    """
    # pyre-fixme [45]: Invalid class instantiation
    test_problem = test_problem_class(**test_problem_kwargs)
    is_constrained = isinstance(test_problem, ConstrainedBaseTestProblem)

    search_space = (
        get_continuous_search_space(test_problem._bounds)
        if search_space is None
        else search_space
    )

    dim = test_problem_kwargs.get("dim", None)

    n_obj = test_problem.num_objectives
    if name is None:
        name = _get_name(
            test_problem=test_problem,
            observe_noise_sd=observe_noise_sd,
            dim=dim,
        )

    num_constraints = test_problem.num_constraints if is_constrained else 0
    if isinstance(test_problem, MultiObjectiveTestProblem):
        objective_names = [f"{name}_{i}" for i in range(n_obj)]
    else:
        objective_names = [name]

    constraint_names = [f"constraint_slack_{i}" for i in range(num_constraints)]
    outcome_names = objective_names + constraint_names

    test_function = BoTorchTestFunction(
        botorch_problem=test_problem, outcome_names=outcome_names
    )

    if isinstance(test_problem, MultiObjectiveTestProblem):
        optimization_config = get_moo_opt_config(
            num_constraints=num_constraints,
            lower_is_better=lower_is_better,
            observe_noise_sd=observe_noise_sd,
            outcome_names=test_function.outcome_names,
            ref_point=test_problem._ref_point,
        )
    else:
        optimization_config = get_soo_opt_config(
            outcome_names=test_function.outcome_names,
            lower_is_better=lower_is_better,
            observe_noise_sd=observe_noise_sd,
        )

    optimal_value = (
        test_problem.max_hv
        if isinstance(test_problem, MultiObjectiveTestProblem)
        else test_problem.optimal_value
    )

    return BenchmarkProblem(
        name=name,
        search_space=search_space,
        optimization_config=optimization_config,
        test_function=test_function,
        noise_std=noise_std,
        num_trials=num_trials,
        optimal_value=optimal_value,
        report_inference_value_as_trace=report_inference_value_as_trace,
        trial_runtime_func=trial_runtime_func,
    )
