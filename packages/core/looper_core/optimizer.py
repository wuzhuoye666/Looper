from __future__ import annotations

import itertools
import math
import random
from collections.abc import Mapping, Sequence
from typing import Any

import optuna
from optuna.distributions import (
    CategoricalDistribution,
    FloatDistribution,
    IntDistribution,
)
from optuna.samplers import NSGAIISampler, TPESampler
from optuna.trial import TrialState, create_trial

from looper_core.contracts import Direction, OptimizerSpec, SearchParameter


class SearchSpaceExhausted(RuntimeError):
    pass


def _active(parameter: SearchParameter, selected: Mapping[str, Any]) -> bool:
    if not parameter.when:
        return True
    source = str(parameter.when.get("parameter", ""))
    operator = parameter.when.get("operator", "eq")
    expected = parameter.when.get("value")
    actual = selected.get(source)
    if operator == "eq":
        return actual == expected
    if operator == "neq":
        return actual != expected
    if operator == "in":
        return actual in expected
    if operator == "not_in":
        return actual not in expected
    return False


def _values(parameter: SearchParameter) -> list[Any]:
    if parameter.type == "boolean":
        return [False, True]
    if parameter.type == "categorical":
        return list(parameter.choices or [])
    assert parameter.minimum is not None and parameter.maximum is not None
    if parameter.type == "integer":
        step = int(parameter.step or 1)
        return list(range(int(parameter.minimum), int(parameter.maximum) + 1, step))
    step = parameter.step
    if step is None:
        return [float(parameter.minimum), float(parameter.maximum)]
    count = int(math.floor((parameter.maximum - parameter.minimum) / step))
    return [float(parameter.minimum + index * step) for index in range(count + 1)]


def grid_candidates(search_space: Mapping[str, SearchParameter]) -> list[dict[str, Any]]:
    names = list(search_space)
    pools = [_values(search_space[name]) for name in names]
    candidates: list[dict[str, Any]] = []
    for combination in itertools.product(*pools):
        selected = dict(zip(names, combination, strict=True))
        selected = {
            name: value for name, value in selected.items() if _active(search_space[name], selected)
        }
        if selected not in candidates:
            candidates.append(selected)
    return candidates


def random_candidate(
    search_space: Mapping[str, SearchParameter], seed: int, sequence: int
) -> dict[str, Any]:
    generator = random.Random(seed + sequence * 104729)
    selected: dict[str, Any] = {}
    for name, parameter in search_space.items():
        if not _active(parameter, selected):
            continue
        if parameter.type in {"categorical", "boolean"}:
            selected[name] = generator.choice(_values(parameter))
            continue
        assert parameter.minimum is not None and parameter.maximum is not None
        if parameter.log:
            sampled = math.exp(
                generator.uniform(math.log(parameter.minimum), math.log(parameter.maximum))
            )
        else:
            sampled = generator.uniform(parameter.minimum, parameter.maximum)
        if parameter.type == "integer":
            step = int(parameter.step or 1)
            sampled = int(parameter.minimum) + round((sampled - parameter.minimum) / step) * step
            sampled = min(int(parameter.maximum), max(int(parameter.minimum), sampled))
        elif parameter.step:
            sampled = (
                parameter.minimum
                + round((sampled - parameter.minimum) / parameter.step) * parameter.step
            )
            sampled = min(parameter.maximum, max(parameter.minimum, sampled))
        selected[name] = sampled
    return selected


def _distribution(parameter: SearchParameter) -> optuna.distributions.BaseDistribution:
    if parameter.type == "boolean":
        return CategoricalDistribution(choices=[False, True])
    if parameter.type == "categorical":
        return CategoricalDistribution(choices=list(parameter.choices or []))
    assert parameter.minimum is not None and parameter.maximum is not None
    if parameter.type == "integer":
        return IntDistribution(
            int(parameter.minimum),
            int(parameter.maximum),
            step=int(parameter.step or 1),
            log=parameter.log,
        )
    return FloatDistribution(
        float(parameter.minimum),
        float(parameter.maximum),
        step=float(parameter.step) if parameter.step else None,
        log=parameter.log,
    )


def optuna_candidate(
    search_space: Mapping[str, SearchParameter],
    optimizer: OptimizerSpec,
    objective_directions: Sequence[Direction],
    history: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    sampler = (
        NSGAIISampler(seed=optimizer.seed)
        if optimizer.type == "optuna-nsga2"
        else TPESampler(seed=optimizer.seed, multivariate=False)
    )
    study = optuna.create_study(
        directions=[direction.value for direction in objective_directions], sampler=sampler
    )
    all_distributions = {name: _distribution(parameter) for name, parameter in search_space.items()}
    for item in history:
        params = {
            name: value
            for name, value in item.get("parameters", {}).items()
            if name in all_distributions
        }
        distributions = {name: all_distributions[name] for name in params}
        values = item.get("values")
        state = TrialState.COMPLETE if values is not None else TrialState.FAIL
        trial = create_trial(
            params=params,
            distributions=distributions,
            values=[float(value) for value in values] if values is not None else None,
            state=state,
            user_attrs={"candidate_id": item.get("id")},
        )
        study.add_trial(trial)
    asked = study.ask(fixed_distributions=all_distributions)
    selected = dict(asked.params)
    return {
        name: value for name, value in selected.items() if _active(search_space[name], selected)
    }


def suggest_candidate(
    search_space: Mapping[str, SearchParameter],
    optimizer: OptimizerSpec,
    sequence: int,
    existing: Sequence[Mapping[str, Any]],
    objective_directions: Sequence[Direction],
    history: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    existing_parameters = [dict(item.get("parameters", item)) for item in existing]
    if optimizer.type == "grid":
        for candidate in grid_candidates(search_space):
            if candidate not in existing_parameters:
                return candidate
        raise SearchSpaceExhausted("the grid search space is exhausted")

    for offset in range(256):
        if optimizer.type == "random":
            candidate = random_candidate(search_space, optimizer.seed, sequence + offset)
        else:
            candidate = optuna_candidate(
                search_space,
                optimizer,
                objective_directions,
                [
                    *history,
                    *(
                        {"parameters": value}
                        for value in existing_parameters
                        if value not in [item.get("parameters") for item in history]
                    ),
                ],
            )
        if candidate not in existing_parameters:
            return candidate
    raise SearchSpaceExhausted("could not produce a unique candidate")
