from __future__ import annotations

import json
import string

from pydantic import Field, field_validator

from looper_core.contracts import StrictModel
from looper_core.system_opt.executor import CommandRunner, OperationStatus
from looper_core.system_opt.scoring import MeasurementBatch


class MeasurementCommandSpec(StrictModel):
    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, le=86400)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, argv: list[str]) -> list[str]:
        formatter = string.Formatter()
        for argument in argv:
            if not argument or "\x00" in argument or "\n" in argument or "\r" in argument:
                raise ValueError("measurement argv must contain non-empty single-line values")
            fields = {
                field_name
                for _, field_name, _, _ in formatter.parse(argument)
                if field_name is not None
            }
            if fields - {"repeats"}:
                raise ValueError("measurement argv only supports the {repeats} placeholder")
        return argv

    def render(self, repeats: int) -> list[str]:
        return [argument.format(repeats=repeats) for argument in self.argv]


class CommandMeasurementAdapter:
    def __init__(self, spec: MeasurementCommandSpec, runner: CommandRunner) -> None:
        self.spec = spec
        self.runner = runner

    def __call__(self, repeats: int) -> MeasurementBatch:
        result = self.runner.run(
            self.spec.render(repeats), timeout_seconds=self.spec.timeout_seconds
        )
        if result.status != OperationStatus.SUCCEEDED:
            raise RuntimeError(result.stderr or f"measurement command {result.status.value}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ValueError("measurement command stdout is not valid JSON") from error
        return MeasurementBatch.model_validate(payload)


__all__ = ["CommandMeasurementAdapter", "MeasurementCommandSpec"]
